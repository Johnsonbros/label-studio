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
    now = datetime.now(TZ)
    quarter = f'{now.year}-Q{(now.month-1)//3+1}'
    # First scheduled candidate: October 1, 2026; later quarters retry daily until ready.
    if now.date().isoformat() < os.environ.get('FIRST_TRAINING_DATE','2026-10-01'): return
    with db() as c:
        old = c.execute('SELECT status FROM quarters WHERE quarter=?',(quarter,)).fetchone()
    if old and old[0] in ['queued','waiting_for_gpu','training','complete','failed','interrupted']: return
    native = api(f'/api/projects/{PROJECT}/export?exportType=JSON&download_all_tasks=true')
    directory = STATE / 'quarterly' / quarter
    directory.mkdir(exist_ok=True)
    raw = directory / 'native.json'
    raw.write_text(json.dumps(native))
    exported, manifest = export(raw, directory / 'reviewed')
    counts = {'train':0,'eval':0}
    for split in counts:
        records = []
        for line in (exported / (split+'.jsonl')).read_text().splitlines():
            row = json.loads(line)
            turns = conversation(row['text'])
            if turns:
                records.append({'lane':'cory_public','channel':'phone','source_id':row['source_id'],
                    'messages':[{'role':'system','content':'You are Cory, the Johnson Bros Plumbing phone receptionist. Help customers with plumbing service requests. Never claim an action was completed unless a tool confirmed it.'}]+turns})
        (directory / (split+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in records))
        counts[split] = len(records)
    enough = counts['train'] >= 100 and counts['eval'] >= 10
    status = 'queued' if enough else 'waiting_for_reviewed_calls'
    job = {'quarter':quarter,'status':status,'counts':counts,'base_model':os.environ.get('TRAIN_BASE_MODEL','Qwen/Qwen3-8B'),
           'auto_promote':False,'created_at':now.isoformat(), 'data_sha256':hashlib.sha256((directory/'train.jsonl').read_bytes()).hexdigest()}
    temp = directory / 'job.tmp';temp.write_text(json.dumps(job,indent=2));temp.replace(directory/'job.json')
    with db() as c: c.execute('INSERT OR REPLACE INTO quarters VALUES (?,?,?,?)',(quarter,status,json.dumps(counts),time.time()))

def refresh_review():
    from export_reviewed import accepted, reviewed_text
    rows=api(f'/api/projects/{PROJECT}/export?exportType=JSON&download_all_tasks=true')
    approved=0
    for row in rows:
        annotations=[a for a in row.get('annotations',[]) if accepted(a)]
        if len(annotations)==1 and reviewed_text(annotations[0]):approved+=1
    meta('review_summary',json.dumps({'tasks':len(rows),'approved_redacted':approved,'checked_at':datetime.now(TZ).isoformat()}))

def work():
    while not STOP.is_set():
        try:
            now = datetime.now(TZ)
            day = now.date().isoformat()
            requested=meta('review_requested')
            if requested and requested!=meta('review_processed') and time.time()-float(requested)>5:
                refresh_review()
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
            'review':meta('review_summary'),'last_label_event':meta('last_label_event'),'quarters':quarters}

from ml_backend import router as ml_router
app.include_router(ml_router,prefix='/ml')
