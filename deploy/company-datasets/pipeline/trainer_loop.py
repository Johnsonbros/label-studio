"""Consume quarterly candidate jobs when GPU capacity is available. Never deploy."""
import hashlib,json,os,sqlite3,subprocess,time
from pathlib import Path
ROOT=Path('/state')

def set_status(job,path,status,detail=''):
    job['status']=status;job['detail']=detail;job['updated_at']=time.time()
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(job,indent=2));tmp.replace(path)
    with sqlite3.connect(ROOT/'pipeline.sqlite',timeout=30) as c:
        c.execute('UPDATE quarters SET status=?,detail=?,updated=? WHERE quarter=?',
                  (status,detail,time.time(),job['quarter']))

def free_memory():
    r=subprocess.run(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=15)
    if r.returncode:return 0
    return int(r.stdout.splitlines()[0])

def process(path):
    job=json.loads(path.read_text())
    if job['status'] not in ['queued','waiting_for_gpu','training']:return
    if job['status']=='training':
        set_status(job,path,'interrupted','Trainer restarted; candidate requires a new attempt')
        return
    if free_memory()<16000:
        set_status(job,path,'waiting_for_gpu','Requires at least 16000 MiB free; live services remain running')
        return
    data=path.parent/'train.jsonl';evaluation=path.parent/'eval.jsonl'
    if hashlib.sha256(data.read_bytes()).hexdigest()!=job['data_sha256']:
        set_status(job,path,'failed','Dataset hash mismatch');return
    if hashlib.sha256(evaluation.read_bytes()).hexdigest()!=job.get('eval_sha256'):
        set_status(job,path,'failed','Evaluation dataset hash mismatch');return
    output=ROOT/'models'/job['quarter'];output.mkdir(parents=True,exist_ok=True)
    set_status(job,path,'training')
    with (output/'training.log').open('a') as log:
        child=subprocess.Popen(['python','/app/train_candidate.py','--model',job['base_model'],
            '--data',str(data),'--eval-data',str(evaluation),'--output',str(output)],stdout=log,stderr=subprocess.STDOUT)
        while child.poll() is None:
            time.sleep(15)
            if free_memory()<1500:
                child.terminate()
                try:child.wait(timeout=30)
                except subprocess.TimeoutExpired:child.kill();child.wait()
                set_status(job,path,'failed','GPU capacity fell below reserve; stopped candidate training');return
    if child.returncode:
        set_status(job,path,'failed','See private training log');return
    set_status(job,path,'complete','Candidate saved; held-out loss measured. Call/tool/safety evaluation required before deployment.')

def main():
    os.umask(0o077)
    while True:
        try:
            for path in sorted((ROOT/'quarterly').glob('*/job.json')):process(path)
        except Exception as e:
            (ROOT/'trainer-status.json').write_text(json.dumps({'status':'error','type':type(e).__name__,'time':time.time()}))
        time.sleep(300)

if __name__=='__main__':main()
