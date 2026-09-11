"""Approved public company facts: typed graph + isolated Qdrant collection."""
import hashlib,json,os,sqlite3,time,uuid
from pathlib import Path
import httpx
COLLECTION='cory_public_company_facts_v1'
COMPANY='johnson-bros'
MODEL='BAAI/bge-small-en-v1.5'
_embedding=None

def connection():
    from service import STATE
    c=sqlite3.connect(STATE/'knowledge.sqlite',timeout=30);c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, subject TEXT, relation TEXT, object TEXT, text TEXT, source TEXT, expires REAL, approved INTEGER, revision TEXT)')
    return c

def vector_api(path,method='GET',body=None):
    r=httpx.request(method,'http://qdrant:6333'+path,json=body,timeout=60)
    r.raise_for_status();return r.json()

def init_collection():
    r=httpx.get('http://qdrant:6333/collections/'+COLLECTION,timeout=10)
    if r.status_code==404:vector_api('/collections/'+COLLECTION,'PUT',{'vectors':{'size':384,'distance':'Cosine'}})
    else:r.raise_for_status()

def embed(text):
    global _embedding
    from fastembed import TextEmbedding
    from service import STATE
    if _embedding is None:_embedding=TextEmbedding(MODEL,cache_dir=str(STATE/'embedding-cache'),threads=2)
    return next(_embedding.embed([text])).tolist()

def text_field(annotation,name):
    from export_reviewed import text_field as get
    return get(annotation,name)

def sync_knowledge():
    from service import api,meta
    from export_reviewed import choices
    project=os.environ.get('KNOWLEDGE_PROJECT_ID')
    if not project:return
    init_collection()
    rows=api(f'/api/projects/{int(project)}/export?exportType=JSON&download_all_tasks=true')
    current=[]
    for task in rows:
        annotations=[a for a in task.get('annotations',[]) if not a.get('was_cancelled') and
            choices(a,'fact_review')==['approved'] and choices(a,'fact_visibility')==['public_company_information']]
        if len(annotations)!=1:continue
        a=annotations[0]
        fields={k:text_field(a,k) for k in ['fact_text','fact_subject','fact_relation','fact_object','fact_source']}
        if not all(fields.values()):continue
        # Facts need provenance and explicit human approval; raw call transcripts are never indexed.
        if not fields['fact_source'].startswith(('https://','http://')):continue
        from export_reviewed import number_field
        days=number_field(a,'review_interval_days')
        if days is None or not 1<=days<=365:continue
        from datetime import datetime
        try:reviewed=datetime.fromisoformat(a['updated_at'].replace('Z','+00:00')).timestamp()
        except (KeyError,ValueError):continue
        expires=reviewed+days*86400
        if expires<=time.time():continue
        key=str(uuid.uuid5(uuid.NAMESPACE_URL,f'{COMPANY}:knowledge-task:{task["id"]}'))
        revision=hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()
        current.append(key)
        with connection() as c:old=c.execute('SELECT revision FROM facts WHERE id=?',(key,)).fetchone()
        if not old or old['revision']!=revision:
            vector_api('/collections/'+COLLECTION+'/points?wait=true','PUT',{'points':[{'id':key,'vector':embed(fields['fact_text']),
                'payload':{'company':COMPANY,'visibility':'public','fact_id':key}}]})
        with connection() as c:c.execute('INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?,?,?)',
            (key,fields['fact_subject'],fields['fact_relation'],fields['fact_object'],fields['fact_text'],fields['fact_source'],expires,1,revision))
    with connection() as c:
        all_ids=[r[0] for r in c.execute('SELECT id FROM facts WHERE approved=1')]
        for key in set(all_ids)-set(current):c.execute('UPDATE facts SET approved=0 WHERE id=?',(key,))
    meta('knowledge_status',json.dumps({'approved_current_facts':len(current),'project':int(project),'checked_at':time.time()}))

def search(query,limit=5):
    if not isinstance(query,str) or not 1<=len(query.strip())<=500:raise ValueError('Query must contain 1-500 characters')
    limit=max(1,min(int(limit),5))
    with connection() as c:
        if not c.execute('SELECT 1 FROM facts WHERE approved=1 AND expires>? LIMIT 1',(time.time(),)).fetchone():return []
    data=vector_api('/collections/'+COLLECTION+'/points/search','POST',{'vector':embed(query),'limit':20,'with_payload':True,
        'filter':{'must':[{'key':'company','match':{'value':COMPANY}},{'key':'visibility','match':{'value':'public'}}]}})
    out=[]
    with connection() as c:
        for hit in data['result']:
            fact=c.execute('SELECT * FROM facts WHERE id=? AND approved=1 AND expires>?',(str(hit['id']),time.time())).fetchone()
            if fact:
                item=dict(fact);item['similarity']=hit['score']
                item['related_facts']=[dict(x) for x in c.execute('SELECT subject,relation,object,text,source FROM facts WHERE approved=1 AND expires>? AND (subject=? OR subject=?) LIMIT 8',(time.time(),fact['subject'],fact['object']))]
                out.append(item)
            if len(out)>=limit:break
    return out
