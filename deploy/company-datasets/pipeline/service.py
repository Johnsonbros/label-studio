"""Durable recording intake, review queue and quarterly candidate preparation."""
import contextlib, hashlib, hmac, json, os, re, sqlite3, threading, time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, parse_qs, urlparse
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, Request, HTTPException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

STATE = Path(os.environ.get('STATE_DIR', '/state'))
ARCHIVE = Path(os.environ.get('ARCHIVE_DIR', '/archive'))
BASE = os.environ.get('LABEL_STUDIO_URL', 'http://company-label-studio:8080')
PROJECT = int(os.environ.get('PROJECT_ID', '1'))
PUBLIC = os.environ.get('PUBLIC_URL', 'https://datasets.aisyncservices.com')
TZ = ZoneInfo('America/New_York')
SID_RE = re.compile(r'RE[0-9a-fA-F]{32}')
STOP = threading.Event()

def db():
    c = sqlite3.connect(STATE / 'pipeline.sqlite', timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c

def init():
    os.umask(0o077)
    for d in ['', 'media', 'transcripts', 'quarterly', 'models']:
        (STATE / d).mkdir(parents=True, exist_ok=True)
    os.chown(STATE / 'media', 1001, 1001)
    (STATE / 'media').chmod(0o750)
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS recordings (
          id TEXT PRIMARY KEY, sid TEXT, path TEXT, audio TEXT, task_id INTEGER,
          draft TEXT, state TEXT NOT NULL, attempts INTEGER DEFAULT 0,
          retry_at REAL DEFAULT 0, error TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS quarters (quarter TEXT PRIMARY KEY, status TEXT,
          detail TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS judgments (task_id INTEGER PRIMARY KEY, state TEXT, retry_at REAL DEFAULT 0);
        ''')
        c.execute("INSERT OR IGNORE INTO meta VALUES ('started', ?)", (str(time.time()),))

def meta(key, value=None):
    with db() as c:
        if value is not None:
            c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))
        row = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

def identity(name):
    sid = SID_RE.search(name)
    return hashlib.sha256(('recording:' + sid.group(0).lower() if sid else 'file:' + name).encode()).hexdigest()

def api(path, method='GET', body=None):
    with httpx.Client(timeout=90) as client:
        r = client.request(method, BASE + path, json=body,
            headers={'Authorization': 'Token ' + os.environ['LABEL_STUDIO_USER_TOKEN']})
        r.raise_for_status()
        return r.json()

def tasks():
    page = 1
    while True:
        data = api(f'/api/tasks/?project={PROJECT}&page={page}&page_size=100')
        rows = data['tasks']
        yield from rows
        if not rows or page * 100 >= data['total']: break
        page += 1

def reconcile():
    # Adopt prior pilot tasks and interrupted imports using the recording identity.
    existing = {}
    for task in tasks():
        data = task.get('data', {})
        audio = data.get('audio', '')
        name = Path(unquote(parse_qs(urlparse(audio).query).get('d', [''])[0])).name
        if name: existing[identity(name)] = task['id']
    started = float(meta('started'))
    candidates = sorted(ARCHIVE.glob('*.wav'), key=lambda p: p.stat().st_mtime, reverse=True)
    with db() as c:
        for p in candidates:
            if p.is_symlink() or time.time() - p.stat().st_mtime < 120: continue
            key = identity(p.stem)
            tp = ARCHIVE / 'transcripts' / (p.stem + '.json')
            draft = ''
            if tp.is_file() and not tp.is_symlink():
                try:
                    raw = json.loads(tp.read_text())
                    raw = raw.get('whisper_response', raw)
                    draft = raw.get('text') or raw.get('transcript') or ''
                except (ValueError, OSError, AttributeError): pass
            # Existing untranscribed backlog is inventoried separately, not flooded into review.
            if p.stat().st_mtime < started and not draft and key not in existing: continue
            sid = SID_RE.search(p.stem)
            c.execute('INSERT OR IGNORE INTO recordings (id,sid,path,audio,draft,state,created) VALUES (?,?,?,?,?,?,?)',
                (key, sid.group(0) if sid else None, str(p), '/data/local-files/?d=hcp/' + quote(p.name, safe=''),
                 draft, 'ready' if draft else 'transcribe', time.time()))
        for key, task_id in existing.items():
            c.execute("UPDATE recordings SET task_id=?, state='imported', error=NULL WHERE id=?", (task_id, key))
    meta('last_scan', datetime.now(TZ).isoformat())

def enqueue(sid):
    if not SID_RE.fullmatch(sid): raise ValueError('invalid recording id')
    with db() as c:
        cur = c.execute("INSERT OR IGNORE INTO recordings (id,sid,state,created) VALUES (?,?,'download',?)",
                        (identity(sid), sid, time.time()))
        return cur.rowcount == 1

def twilio_client():
    return Client(os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN'])

def allowed_call(call):
    numbers = set(os.environ['COMPANY_PHONE_NUMBERS'].split(','))
    return call.to in numbers or call._from in numbers

def poll_twilio():
    client = twilio_client()
    # Overlapping windows repair missed callbacks; the ledger deduplicates them.
    for rec in client.recordings.stream(date_created_after=datetime.now(TZ).date()-timedelta(days=7)):
        if rec.status == 'completed' and rec.call_sid and allowed_call(client.calls(rec.call_sid).fetch()):
            enqueue(rec.sid)
    meta('last_twilio_poll', datetime.now(TZ).isoformat())

def download(row):
    client = twilio_client()
    rec = client.recordings(row['sid']).fetch()
    if rec.status != 'completed': raise ValueError('recording_not_complete')
    if not rec.call_sid or not allowed_call(client.calls(rec.call_sid).fetch()):
        with db() as c: c.execute("UPDATE recordings SET state='excluded',error='not_company_call' WHERE id=?", (row['id'],))
        return
    # Construct an account-owned URL; never fetch a URL supplied in a webhook.
    url = f"https://api.twilio.com/2010-04-01/Accounts/{os.environ['TWILIO_ACCOUNT_SID']}/Recordings/{row['sid']}.wav"
    path = STATE / 'media' / (row['sid'] + '.wav')
    temporary = path.with_suffix('.part')
    with httpx.Client(timeout=120, follow_redirects=True, max_redirects=3) as client:
        with client.stream('GET', url, auth=(os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN'])) as r:
            r.raise_for_status()
            total = 0
            with temporary.open('wb') as f:
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > 256 * 1024 * 1024: raise ValueError('recording_too_large')
                    f.write(chunk)
    import wave
    with wave.open(str(temporary)) as wav:
        if wav.getnframes() < 1: raise ValueError('empty_audio')
    temporary.replace(path)
    os.chown(path, 1001, 1001)
    path.chmod(0o640)
    with db() as c: c.execute("UPDATE recordings SET path=?,audio=?,state='transcribe' WHERE id=?",
        (str(path), '/data/local-files/?d=daily/' + path.name, row['id']))

_whisper = None
def transcribe(row):
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel('base.en', device='cpu', compute_type='int8', cpu_threads=2,
                                download_root=str(STATE / 'whisper-cache'))
    segments, info = _whisper.transcribe(row['path'], vad_filter=True, beam_size=5)
    result = [{'start':s.start, 'end':s.end, 'text':s.text} for s in segments]
    text = ' '.join(s['text'].strip() for s in result).strip()
    (STATE / 'transcripts' / (row['id'] + '.json')).write_text(json.dumps({'text':text, 'segments':result}))
    with db() as c: c.execute("UPDATE recordings SET draft=?,state='ready' WHERE id=?", (text, row['id']))

def import_ready(row):
    # Use a reconciliation lookup before every POST, including after uncertain network failures.
    for task in tasks():
        if task.get('data', {}).get('source_id') == row['id']:
            with db() as c: c.execute("UPDATE recordings SET task_id=?,state='imported' WHERE id=?", (task['id'],row['id']))
            return
    data = {'source_id':row['id'], 'audio':row['audio'], 'draft_transcript':row['draft'] or '',
            'pipeline':'daily-call-intake', 'review_required':True}
    api(f'/api/projects/{PROJECT}/import', 'POST', [{'data':data}])
    # Next reconciliation resolves the task ID even if this process is interrupted here.
    with db() as c: c.execute("UPDATE recordings SET state='imported' WHERE id=?", (row['id'],))

def conversation(text):
    matches = list(re.finditer(r'(?m)^(CUSTOMER|STAFF):\s*', text))
    if not matches or text[:matches[0].start()].strip(): return None
    result = []
    for i,m in enumerate(matches):
        part = text[m.end():matches[i+1].start() if i+1 < len(matches) else len(text)].strip()
        if not part or re.search(r'(?m)^(UNKNOWN|OTHER):',part): return None
        role = 'user' if m[1] == 'CUSTOMER' else 'assistant'
        if not result and role == 'assistant': continue
        if result and result[-1]['role'] == role: result[-1]['content'] += '\n' + part
        else: result.append({'role':role,'content':part})
    if result and result[-1]['role'] == 'user': result.pop()
    return result if len(result)>=2 else None

def quarterly():
    from export_reviewed import export
    from export_reviewed import split_for
    from tool_contracts import approved_traces,snapshot
    now = datetime.now(TZ)
    quarter = f'{now.year}-Q{(now.month-1)//3+1}'
    # First scheduled candidate: October 1, 2026; later quarters retry daily until ready.
    if now.date().isoformat() < os.environ.get('FIRST_TRAINING_DATE','2026-10-01'): return
    with db() as c:
        old = c.execute('SELECT status FROM quarters WHERE quarter=?',(quarter,)).fetchone()
    if old and old[0] in ['queued','waiting_for_gpu','training','complete','failed','interrupted']: return
    # Refresh before validating traces, including runs triggered by review webhooks.
    if os.environ.get('PUBLIC_MCP_URL'):snapshot()
    native = api(f'/api/projects/{PROJECT}/export?exportType=JSON&download_all_tasks=true')
    directory = STATE / 'quarterly' / quarter
    directory.mkdir(exist_ok=True)
    raw = directory / 'native.json'
    raw.write_text(json.dumps(native))
    exported, manifest = export(raw, directory / 'reviewed')
    version=meta('tool_contract_version')
    if not version and (STATE/'tool-traces'/'approved.jsonl').exists():raise ValueError('No current MCP contract for tool traces')
    tool_rows=approved_traces(STATE/'tool-traces',json.loads((STATE/'tools'/(version+'.json')).read_text())) if version else []
    counts = {'train':0,'eval':0}
    tool_counts={'train':0,'eval':0}
    for split in counts:
        records = []
        for line in (exported / (split+'.jsonl')).read_text().splitlines():
            row = json.loads(line)
            turns = conversation(row['text'])
            if turns:
                records.append({'lane':'cory_public','channel':'phone','source_id':row['source_id'],
                    'messages':[{'role':'system','content':'You are Cory, the Johnson Bros Plumbing phone receptionist. Help customers with plumbing service requests. Never claim an action was completed unless a tool confirmed it.'}]+turns})
        counts[split] = len(records)
        selected=[r for r in tool_rows if split_for(r['source_id'],10)==split]
        tool_counts[split]=len(selected)
        records.extend(selected)
        (directory / (split+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in records))
    enough = counts['train'] >= 100 and counts['eval'] >= 10
    status = 'queued' if enough else 'waiting_for_reviewed_calls'
    job = {'quarter':quarter,'status':status,'counts':counts,'tool_counts':tool_counts,'tool_contract_version':version,'base_model':os.environ.get('TRAIN_BASE_MODEL','Qwen/Qwen3-8B'),
           'auto_promote':False,'created_at':now.isoformat(), 'eval_sha256':hashlib.sha256((directory/'eval.jsonl').read_bytes()).hexdigest(), 'data_sha256':hashlib.sha256((directory/'train.jsonl').read_bytes()).hexdigest()}
    temp = directory / 'job.tmp';temp.write_text(json.dumps(job,indent=2));temp.replace(directory/'job.json')
    with db() as c: c.execute('INSERT OR REPLACE INTO quarters VALUES (?,?,?,?)',(quarter,status,json.dumps(counts),time.time()))

def refresh_review():
    from export_reviewed import accepted, reviewed_text, positive_quality, text_field
    from export_curated import curate
    rows=api(f'/api/projects/{PROJECT}/export?exportType=JSON&download_all_tasks=true')
    approved=0;positive=0
    for row in rows:
        annotations=[a for a in row.get('annotations',[]) if accepted(a)]
        if len(annotations)==1 and reviewed_text(annotations[0]):
            approved+=1
            if positive_quality(annotations[0]) and text_field(annotations[0],'training_excerpt'):positive+=1
    curated=curate(rows,STATE/'curated',{'hcp':ARCHIVE,'daily':STATE/'media'})
    meta('review_summary',json.dumps({'tasks':len(rows),'approved_redacted':approved,'positive_quality_examples':positive,
           'preference_pairs':curated['preference_pairs'],'clips':curated['clips'],'checked_at':datetime.now(TZ).isoformat()}))

def refresh_knowledge():
    try:
        from knowledge import sync_knowledge
        sync_knowledge()
        meta('knowledge_error','')
    except Exception as e:
        meta('knowledge_error',type(e).__name__)

def score_next():
    from ml_backend import predict_task,VERSION,LOCK
    day=datetime.now(TZ).date().isoformat()
    if meta('score_day')!=day:meta('score_day',day);meta('scored_today',0)
    count=int(meta('scored_today') or 0)
    if count>=int(os.environ.get('DAILY_QUALITY_LIMIT','10')):return
    with db() as c:
        done={r['task_id']:dict(r) for r in c.execute('SELECT * FROM judgments')}
        has_grades=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_quality_grades'").fetchone()
        graded={r[0] for r in c.execute('SELECT task_id FROM call_quality_grades WHERE model_version=?',(VERSION,))} if has_grades else set()
    for task in tasks():
        old=done.get(task['id'])
        if old and ((old['state']=='scored' and task['id'] in graded) or old['retry_at']>time.time()):continue
        if not LOCK.acquire(blocking=False):return
        try:
            existing=api(f"/api/predictions/?task={task['id']}")
            if not any(p.get('model_version')==VERSION for p in existing):
                prediction=predict_task(task)
                if not any(r['from_name']=='quality_score' for r in prediction['result']):raise ValueError('No grounded score')
                api('/api/predictions/','POST',{'task':task['id'],**prediction})
            with db() as c:c.execute('INSERT OR REPLACE INTO judgments VALUES (?,?,?)',(task['id'],'scored',0))
            meta('scored_today',count+1)
        except Exception as e:
            with db() as c:c.execute('INSERT OR REPLACE INTO judgments VALUES (?,?,?)',(task['id'],'retry',time.time()+3600))
            meta('judge_last_error',type(e).__name__)
        finally:LOCK.release()
        return

def work():
    while not STOP.is_set():
        try:
            now = datetime.now(TZ)
            day = now.date().isoformat()
            requested=meta('review_requested')
            if requested and requested!=meta('review_processed') and time.time()-float(requested)>5:
                refresh_review()
                refresh_knowledge()
                quarterly()
                meta('review_processed',requested)
            if meta('last_daily') != day:
                reconcile()
                try:
                    poll_twilio()
                    meta('poll_error','')
                except Exception as e:
                    meta('poll_error',type(e).__name__)
                quarterly()
                refresh_review()
                from tool_contracts import snapshot
                refresh_knowledge()
                try:
                    snapshot()
                    meta('tool_contract_error','')
                except Exception as e:meta('tool_contract_error',type(e).__name__)
                meta('last_daily',day)
                with db() as source, sqlite3.connect(STATE / 'pipeline-backup.sqlite') as target:
                    source.backup(target)
            with db() as c:
                row = c.execute("SELECT * FROM recordings WHERE state IN ('download','transcribe','ready') AND retry_at<=? ORDER BY created DESC LIMIT 1",(time.time(),)).fetchone()
            if row:
                try:
                    {'download':download,'transcribe':transcribe,'ready':import_ready}[row['state']](row)
                    meta('last_work',datetime.now(TZ).isoformat())
                except Exception as e:
                    with db() as c:
                        c.execute('UPDATE recordings SET attempts=attempts+1,retry_at=?,error=? WHERE id=?',
                            (time.time()+min(86400,60*2**min(row['attempts'],10)),type(e).__name__,row['id']))
            else:
                score_next()
            meta('heartbeat',time.time())
            meta('worker_error','')
        except Exception as e:
            meta('worker_error',type(e).__name__)
        STOP.wait(2 if 'row' in locals() and row else 60)

@contextlib.asynccontextmanager
async def lifespan(app):
    init()
    thread = threading.Thread(target=work,daemon=True);thread.start()
    yield
    STOP.set()

app = FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.post('/knowledge/search')
async def knowledge_search(request:Request):
    expected='Bearer '+os.environ['KNOWLEDGE_READ_TOKEN']
    if not hmac.compare_digest(request.headers.get('Authorization',''),expected):raise HTTPException(403)
    state=json.loads(meta('knowledge_status') or '{}')
    if meta('knowledge_error') or time.time()-state.get('checked_at',0)>26*3600:raise HTTPException(503,'Knowledge review sync is unavailable or stale')
    data=await request.json()
    from knowledge import search
    from starlette.concurrency import run_in_threadpool
    try:results=await run_in_threadpool(search,data.get('query',''),data.get('limit',5))
    except (ValueError,TypeError):raise HTTPException(400,'Invalid query')
    return {'results':results,'scope':'approved_public_company_facts','answer_if_empty':'No verified fact found; use the approved live business tools or staff handoff.'}

@app.post('/webhooks/label-studio')
async def label_webhook(request:Request):
    if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+os.environ['LABEL_WEBHOOK_TOKEN']):
        raise HTTPException(403,'Invalid webhook credential')
    body=await request.body()
    if len(body)>32768:raise HTTPException(413)
    try:payload=json.loads(body)
    except ValueError:raise HTTPException(400,'Invalid JSON')
    if not isinstance(payload,dict):raise HTTPException(400)
    action=payload.get('action')
    allowed={'ANNOTATION_CREATED','ANNOTATIONS_CREATED','ANNOTATION_UPDATED','ANNOTATIONS_DELETED','TASKS_CREATED','TASKS_DELETED','PROJECT_UPDATED'}
    if action not in allowed:raise HTTPException(400,'Unsupported action')
    meta('review_requested',time.time())
    meta('last_label_event',action)
    return {'accepted':True}

@app.get('/health')
def health(): return {'ok':True}

@app.post('/webhooks/twilio/recording')
async def webhook(request:Request):
    body = await request.body()
    if len(body)>32768: raise HTTPException(413)
    form = await request.form()
    url = PUBLIC + request.url.path + ('?' + request.url.query if request.url.query else '')
    if not RequestValidator(os.environ['TWILIO_AUTH_TOKEN']).validate(url,form,request.headers.get('X-Twilio-Signature','')):
        raise HTTPException(403,'Invalid signature')
    if form.get('AccountSid') != os.environ['TWILIO_ACCOUNT_SID']: raise HTTPException(403)
    if form.get('RecordingStatus') != 'completed': return {'accepted':False,'reason':'not_completed'}
    sid = form.get('RecordingSid','')
    if not SID_RE.fullmatch(sid): raise HTTPException(400,'Invalid recording identifier')
    return {'accepted':True,'new':enqueue(sid)}

@app.get('/status')
def status(request:Request):
    if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+os.environ['ADMIN_TOKEN']): raise HTTPException(401)
    with db() as c:
        counts = dict(c.execute('SELECT state,count(*) FROM recordings GROUP BY state').fetchall())
        errors = c.execute('SELECT count(*) FROM recordings WHERE error IS NOT NULL').fetchone()[0]
        quarters = [dict(r) for r in c.execute('SELECT * FROM quarters')]
    return {'recordings':counts,'retrying_or_excluded':errors,'last_daily':meta('last_daily'),
            'heartbeat':meta('heartbeat'),'worker_error':meta('worker_error'),'poll_error':meta('poll_error'),
            'review':meta('review_summary'),'last_label_event':meta('last_label_event'),'quarters':quarters,
            'quality':{'scored_today':meta('scored_today'),'last_error':meta('judge_last_error')},
            'knowledge':{'status':meta('knowledge_status'),'last_error':meta('knowledge_error'),'live_cory_connected':meta('live_cory_connected')=='true'},
            'tools':{'contract_version':meta('tool_contract_version'),'count':meta('tool_contract_count'),'changed':meta('tool_contract_changed'),'compatibility':meta('tool_compatibility'),'last_error':meta('tool_contract_error')}}

from ml_backend import router as ml_router
app.include_router(ml_router,prefix='/ml')
