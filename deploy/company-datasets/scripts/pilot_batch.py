"""Bounded, resumable CPU transcription pilot. Never approves training examples."""
import argparse, collections, fcntl, hashlib, json, os, time, wave
from pathlib import Path
from urllib.parse import quote
import service

BATCH = 'archive-pilot-20260911-v1'
ROOT = service.STATE / 'pilots' / BATCH
BINS = [(8,30),(30,60),(60,120),(120,240),(240,480),(480,900)]

def select(rows, per_bin=20):
    selected=[]
    for low,high in BINS:
        months=collections.defaultdict(list)
        for row in rows:
            if low <= row['duration_seconds'] < high:
                months[row['month']].append(row)
        for values in months.values():
            values.sort(key=lambda r: hashlib.sha256((BATCH+r['source_id']).encode()).hexdigest())
        keys=sorted(months,key=lambda k:hashlib.sha256((BATCH+k).encode()).hexdigest())
        count=0
        while keys and count<per_bin:
            for key in list(keys):
                if count>=per_bin:break
                selected.append({**months[key].pop(), 'duration_bin':f'{low}-{high}'})
                count+=1
                if not months[key]:keys.remove(key)
    return selected

def save(path,value):
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,indent=2));tmp.replace(path)

def manifest():
    path=ROOT/'manifest.json'
    if path.exists():return json.loads(path.read_text())
    with service.db() as db:
        existing={r[0] for r in db.execute('SELECT id FROM recordings')}
    rows=[];invalid=0
    for p in sorted(service.ARCHIVE.glob('*.wav')):
        if p.is_symlink():continue
        source_id=service.identity(p.stem)
        if source_id in existing:continue
        try:
            with wave.open(str(p),'rb') as w:
                duration=w.getnframes()/w.getframerate();channels=w.getnchannels();rate=w.getframerate()
            parts=p.name[:10].split('-');month=f'{parts[2]}-{parts[0]}' if len(parts)==3 else 'unknown'
            rows.append(dict(source_id=source_id,path=str(p),month=month,duration_seconds=duration,channels=channels,sample_rate=rate))
        except (OSError,wave.Error,EOFError,ZeroDivisionError):invalid+=1
    picked=select(rows)
    data={'batch_id':BATCH,'selection':'duration/month stratified, deterministic; intents and outcomes not yet known','training_eligible':False,'human_approved':False,'candidate_count':len(rows),'invalid_headers':invalid,'calls':picked}
    save(path,data);return data

def run(limit=None):
    from faster_whisper import WhisperModel
    model=WhisperModel('base.en',device='cpu',compute_type='int8',cpu_threads=1,num_workers=1,download_root=str(service.STATE/'whisper-cache'),local_files_only=True)
    batch=manifest();done=0
    for row in batch['calls']:
        ident=row['source_id'];receipt=ROOT/(ident+'.imported.json')
        if receipt.exists():continue
        start=time.time()
        try:
            target=ROOT/(ident+'.transcript.json')
            if not target.exists():
                segments,info=model.transcribe(row['path'],language='en',vad_filter=True,beam_size=5,word_timestamps=True)
                entries=[]
                for s in segments:
                    entries.append({'start':s.start,'end':s.end,'text':s.text,'avg_logprob':s.avg_logprob,'no_speech_prob':s.no_speech_prob,'words':[{'start':w.start,'end':w.end,'word':w.word,'probability':w.probability} for w in (s.words or [])]})
                save(target,{'text':' '.join(s['text'].strip() for s in entries),'segments':entries,'model':'faster-whisper-base.en','device':'cpu','word_timestamps':True,'speaker_labels_verified':False,'training_eligible':False,'batch_id':BATCH,'elapsed_seconds':time.time()-start})
            draft=json.loads(target.read_text())
            if not draft['text'].strip():raise ValueError('empty_transcript_needs_audio_review')
            # Adopt interrupted imports before creating a new task.
            match=next((t for t in service.tasks() if t.get('data',{}).get('source_id')==ident),None)
            if match is None:
                service.api(f'/api/projects/{service.PROJECT}/import','POST',[{'data':{'source_id':ident,'audio':'/data/local-files/?d=hcp/'+quote(Path(row['path']).name,safe=''),'draft_transcript':draft['text'],'pipeline':'archive-pilot','batch_id':BATCH,'review_required':True,'human_approved':False,'training_eligible':False,'transcription_model':draft['model'],'duration_seconds':row['duration_seconds'],'speaker_labels_verified':False}}])
                match=next(t for t in service.tasks() if t.get('data',{}).get('source_id')==ident)
            with service.db() as db:
                db.execute("INSERT OR IGNORE INTO recordings (id,path,audio,draft,state,created,task_id) VALUES (?,?,?,?,'imported',?,?)",(ident,row['path'],match['data']['audio'],draft['text'],time.time(),match['id']))
            save(receipt,{'task_id':match['id'],'source_id':ident,'training_eligible':False})
            print(json.dumps({'event':'pilot_imported','source_id':ident,'task_id':match['id'],'audio_seconds':row['duration_seconds'],'elapsed_seconds':round(time.time()-start,2)}),flush=True)
            done+=1
        except Exception as exc:
            save(ROOT/(ident+'.error.json'),{'error_type':type(exc).__name__,'at':time.time()})
            print(json.dumps({'event':'pilot_error','source_id':ident,'error_type':type(exc).__name__}),flush=True)
        if limit and done>=limit:break
    print(json.dumps({'event':'pilot_pass_finished','imported_this_pass':done}),flush=True)

if __name__=='__main__':
    os.umask(0o077);ROOT.mkdir(parents=True,exist_ok=True)
    lock=open(ROOT/'worker.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');parser.add_argument('--limit',type=int);args=parser.parse_args()
    if args.prepare:
        data=manifest();print(json.dumps({'batch_id':BATCH,'calls':len(data['calls']),'audio_hours':round(sum(r['duration_seconds'] for r in data['calls'])/3600,2),'bins':dict(collections.Counter(r['duration_bin'] for r in data['calls']))}))
    else:run(args.limit)
