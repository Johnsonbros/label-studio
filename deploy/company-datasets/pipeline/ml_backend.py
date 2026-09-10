"""Label Studio protocol adapter for the existing local Cory brain."""
import base64,hashlib,hmac,json,os,threading,time
from pathlib import Path
from urllib.parse import parse_qs,urlparse,unquote
import httpx
from fastapi import APIRouter,Depends,HTTPException,Request
from preannotate import ROLE_PROMPT,ROLE_SCHEMA,build_result

VERSION='cory-qwen35-9b-labels-v1'
LOCK=threading.Lock()

def auth(request:Request):
    expected='Basic '+base64.b64encode(('label-studio:'+os.environ['ML_BASIC_PASSWORD']).encode()).decode()
    if not hmac.compare_digest(request.headers.get('Authorization',''),expected):raise HTTPException(401)

router=APIRouter(dependencies=[Depends(auth)])

@router.get('/health')
def health():return {'status':'UP','model_class':'CoryLocalCallLabeler'}

@router.post('/setup')
def setup():return {'model_version':VERSION}

def load_segments(task):
    from service import STATE,ARCHIVE
    data=task.get('data',{})
    audio=unquote(parse_qs(urlparse(data.get('audio','')).query).get('d',[''])[0])
    if audio.startswith('hcp/'):
        path=ARCHIVE/'transcripts'/(Path(audio).stem+'.json')
    else:
        source=data.get('source_id','')
        if len(source)!=64 or any(c not in '0123456789abcdef' for c in source):return []
        path=STATE/'transcripts'/(source+'.json')
    if path.is_symlink() or not path.is_file():return []
    raw=json.loads(path.read_text());raw=raw.get('whisper_response',raw)
    return [{'start':float(s['start']),'end':float(s['end']),'text':str(s['text'])} for s in raw.get('segments',[]) if s.get('text')]

def predict_task(task):
    from service import STATE
    segments=load_segments(task)
    if not segments:
        draft=task.get('data',{}).get('draft_transcript','')
        return {'model_version':VERSION,'score':0,'result':[{'from_name':'transcript','to_name':'audio','type':'textarea','value':{'text':[draft]}}] if draft else []}
    cache=STATE/'predictions';cache.mkdir(exist_ok=True)
    key=hashlib.sha256((VERSION+json.dumps(segments,sort_keys=True)).encode()).hexdigest()
    path=cache/(key+'.json')
    if path.exists():return json.loads(path.read_text())
    labels=[];started=time.monotonic()
    with httpx.Client(timeout=25) as client:
        for offset in range(0,len(segments),12):
            if time.monotonic()-started>70:raise HTTPException(503,'Long call prediction deferred; retry with existing draft.')
            try:
                pool=client.get('http://cory-local-speech:8765/v1/pool');pool.raise_for_status()
                if pool.json().get('in_use',1)!=0:raise HTTPException(503,'Cory is on a live call. Retry predictions later.')
            except httpx.HTTPError:raise HTTPException(503,'Unable to verify that the phone system is idle.')
            chunk=segments[offset:offset+12]
            prompt=ROLE_PROMPT+'\n'+ '\n'.join(f'[{i}] {x["text"]}' for i,x in enumerate(chunk))
            r=client.post('http://cory-local-brain:11434/api/chat',json={'model':'qwen3.5:9b','stream':False,'think':False,
                'keep_alive':-1,'format':ROLE_SCHEMA,'messages':[{'role':'user','content':prompt}],
                'options':{'temperature':0,'num_ctx':65536,'num_predict':1024}})
            r.raise_for_status()
            values=json.loads(r.json()['message']['content']).get('labels',[])
            found={x['i']:x['speaker'] for x in values if isinstance(x,dict) and isinstance(x.get('i'),int) and x.get('speaker') in ['staff','customer','unknown','other']}
            labels.extend(found.get(i,'unknown') for i in range(len(chunk)))
    prediction={'model_version':VERSION,'score':0.5,'result':build_result(segments,labels)}
    path.write_text(json.dumps(prediction));return prediction

@router.post('/predict')
def predict(payload:dict):
    tasks=payload.get('tasks',[])
    if not isinstance(tasks,list) or len(tasks)>10:raise HTTPException(400,'Request at most ten tasks at once.')
    if not LOCK.acquire(blocking=False):raise HTTPException(503,'Prediction already running; retry later.')
    try:return {'results':[predict_task(task) for task in tasks],'model_version':VERSION}
    finally:LOCK.release()

@router.post('/webhook')
@router.post('/train')
def train_event():
    from service import meta
    meta('review_requested',time.time())
    return {'model_version':VERSION,'status':'quarterly_schedule','training_started':False}
