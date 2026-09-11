"""Versioned draft topic grades, kept separate from human approval."""
import hashlib,json,time
from pathlib import Path

TAXONOMY=json.loads((Path(__file__).parent/'call-topic-taxonomy.v1.json').read_text())
TOPICS=[t['id'] for t in TAXONOMY['topics']]
CONTEXT_SCHEMA={'type':'object','properties':{
 'primary_topic':{'type':'string','enum':TOPICS},
 'secondary_topics':{'type':'array','items':{'type':'string','enum':TOPICS}},
 'topic_evidence':{'type':'array','items':{'type':'object','properties':{'topic':{'type':'string','enum':TOPICS},'quote':{'type':'string'}},'required':['topic','quote']}},
 'urgency':{'type':'string','enum':TAXONOMY['axes']['urgency']},
 'workflow':{'type':'string','enum':TAXONOMY['axes']['workflow']},
 'outcome':{'type':'string','enum':['information_provided','claimed_unverified','declined','abandoned','unresolved','unknown']},
 'critical_evidence':{'type':'array','items':{'type':'object','properties':{'flag':{'type':'string'},'quote':{'type':'string'},'reason':{'type':'string'}},'required':['flag','quote','reason']}}
},'required':['primary_topic','secondary_topics','topic_evidence','urgency','workflow','outcome','critical_evidence']}
PROMPT='\nClassify context using taxonomy '+TAXONOMY['version']+': '+json.dumps(TAXONOMY['topics'])+'''
Return context with primary_topic, secondary_topics, topic_evidence, urgency, workflow, outcome, critical_evidence. Quote an exact segment for each assigned topic and suspected critical flag. Use unclassified when no topic can be grounded. Secondary topics cannot duplicate the primary. A spoken claim of booking, callback or transfer is claimed_unverified: no system receipts are supplied. Critical flags need an exact quote and explanation; distinguish suspicion from fact. Historical staff are not required to use today's MCP or SMS flow. Never infer speaker roles or an agent failure solely from the caller's words. Audio tone and external company factual accuracy are not verified here.'''

def grounded(quote,segments):
    if not isinstance(quote,str) or not quote.strip():raise ValueError('Missing quote')
    matches=[s for s in segments if quote.strip() in s['text']]
    if not matches:raise ValueError('Ungrounded context evidence')
    return {k:matches[0][k] for k in ['start','end']}

def validate_context(raw,segments,flags):
    c=raw.get('context',{});primary=c.get('primary_topic');secondary=c.get('secondary_topics')
    if primary not in TOPICS or not isinstance(secondary,list) or any(t not in TOPICS for t in secondary) or len(set(secondary))!=len(secondary) or primary in secondary:raise ValueError('Invalid topics')
    selected=set([primary]+secondary);evidence=[]
    for e in c.get('topic_evidence',[]):
        if e.get('topic') not in selected:raise ValueError('Evidence for unassigned topic')
        evidence.append({**e,**grounded(e.get('quote'),segments)})
    if selected-{'unclassified'}-{e['topic'] for e in evidence}:raise ValueError('Missing topic evidence')
    for axis in ['urgency','workflow','outcome']:
        if c.get(axis) not in CONTEXT_SCHEMA['properties'][axis]['enum']:raise ValueError('Invalid context '+axis)
    critical=[]
    for e in c.get('critical_evidence',[]):
        if e.get('flag') not in flags or not str(e.get('reason','')).strip():raise ValueError('Invalid critical evidence')
        critical.append({**e,**grounded(e.get('quote'),segments)})
    if set(flags)!={e['flag'] for e in critical}:raise ValueError('Missing critical evidence')
    return {**c,'topic_evidence':evidence,'critical_evidence':critical,'taxonomy_version':TAXONOMY['version'],'external_outcome_verified':False}

def persist(task,judgment,segments,model_version):
    import service
    digest=hashlib.sha256(json.dumps(segments,sort_keys=True).encode()).hexdigest()
    context=judgment['context'];scores=judgment['scores']
    with service.db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS call_quality_grades (
          task_id INTEGER NOT NULL, model_version TEXT NOT NULL, transcript_sha256 TEXT NOT NULL,
          taxonomy_version TEXT NOT NULL, rubric_version TEXT NOT NULL, primary_topic TEXT NOT NULL,
          accuracy INTEGER, listening INTEGER, professionalism INTEGER, next_step INTEGER, efficiency INTEGER,
          total INTEGER, confidence TEXT, critical_count INTEGER, review_status TEXT NOT NULL,
          source_id TEXT, judgment_json TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(task_id,model_version,transcript_sha256))''')
        c.execute('CREATE INDEX IF NOT EXISTS call_quality_topic ON call_quality_grades(primary_topic,model_version)')
        c.execute('INSERT OR REPLACE INTO call_quality_grades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (task['id'],model_version,digest,context['taxonomy_version'],judgment['rubric_version'],context['primary_topic'],
           *[scores[k] for k in ['accuracy','listening','professionalism','next_step','efficiency']],judgment['total'],
           judgment['confidence'],len(judgment['critical_flags']),'machine_draft',task.get('data',{}).get('source_id'),json.dumps(judgment),time.time()))
