"""Version MCP contracts and validate curated tool-use traces; never execute tools."""
import hashlib,json,os
from pathlib import Path
import httpx
from jsonschema import Draft202012Validator

def compatibility_report(baseline,current):
    """Conservative structural check; behavior must also pass regression evaluations."""
    old={t['name']:t for t in baseline['tools']};new={t['name']:t for t in current['tools']}
    removed=sorted(set(old)-set(new));added=sorted(set(new)-set(old));changed=[];descriptions=[]
    for name in sorted(set(old)&set(new)):
        fields=[k for k in ['inputSchema','outputSchema','annotations'] if old[name].get(k)!=new[name].get(k)]
        if fields:changed.append({'tool':name,'fields':fields})
        if old[name].get('description')!=new[name].get('description'):descriptions.append(name)
    review=bool(removed or changed)
    return {'baseline_version':baseline['version'],'current_version':current['version'],
            'classification':'compatibility_review_required' if review else 'existing_tool_shapes_preserved',
            'removed_tools':removed,'changed_contracts':changed,'added_tools':added,'description_changes':descriptions,
            'automatic_retraining':False,'behavior_regression_tests_required':True,
            'note':'Preserved schemas do not prove unchanged behavior. Keep the approved model tool surface pinned; additions need evaluation before exposure.'}

def contract_hash(tools):
    canonical=[{k:t[k] for k in ['name','description','inputSchema','outputSchema','annotations'] if k in t} for t in tools]
    return hashlib.sha256(json.dumps(sorted(canonical,key=lambda x:x['name']),sort_keys=True,separators=(',',':')).encode()).hexdigest()

def rpc_json(response):
    if 'text/event-stream' not in response.headers.get('content-type',''):return response.json()
    for event in response.text.replace('\r\n','\n').split('\n\n'):
        data='\n'.join(line[5:].strip() for line in event.splitlines() if line.startswith('data:'))
        if data:
            value=json.loads(data)
            if 'result' in value or 'error' in value:return value
    raise ValueError('No JSON-RPC response in event stream')

def snapshot():
    from service import STATE,meta
    url=os.environ.get('PUBLIC_MCP_URL')
    if not url:return
    headers={'Accept':'application/json, text/event-stream','User-Agent':'AiSync-Dataset-Pipeline/1.0'}
    with httpx.Client(timeout=30) as c:
        r=c.post(url,headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'dataset-contract-monitor','version':'1'}}})
        r.raise_for_status()
        headers['Mcp-Session-Id']=r.headers['mcp-session-id']
        c.post(url,headers=headers,json={'jsonrpc':'2.0','method':'notifications/initialized'})
        r=c.post(url,headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}});r.raise_for_status()
        tools=rpc_json(r)['result']['tools']
    version=contract_hash(tools)
    directory=STATE/'tools';directory.mkdir(exist_ok=True)
    path=directory/(version+'.json');path.write_text(json.dumps({'version':version,'tools':tools},indent=2))
    previous=meta('tool_contract_version')
    meta('tool_contract_changed',bool(previous and previous!=version))
    meta('tool_contract_version',version)
    meta('tool_contract_count',len(tools))
    baseline=Path('/app/public-mcp-v1-candidate.json')
    if baseline.exists():
        report=compatibility_report(json.loads(baseline.read_text()),{'version':version,'tools':tools})
        meta('tool_compatibility',json.dumps(report))
    return version

def validate_trace(trace,contract):
    if trace.get('contract_version')!=contract['version']:raise ValueError('Tool contract changed; revalidate this trace')
    if trace.get('review_status')!='approved_redacted':raise ValueError('Trace requires review and redaction')
    tools={t['name']:t for t in contract['tools']};pending=set();seen=set();calls=0
    for message in trace.get('messages',[]):
        if message.get('role') not in ['system','user','assistant','tool']:raise ValueError('Invalid message role')
        if pending and message.get('role')!='tool':raise ValueError('Missing tool results before next conversation turn')
        for call in message.get('tool_calls',[]):
            if message.get('role')!='assistant':raise ValueError('Only assistant messages can call tools')
            fn=call['function'];name=fn['name']
            if name not in tools:raise ValueError('Tool is outside the public contract')
            args=fn['arguments'];args=json.loads(args) if isinstance(args,str) else args
            Draft202012Validator(tools[name]['inputSchema']).validate(args)
            if not isinstance(call.get('id'),str) or not call['id'] or call['id'] in seen:raise ValueError('Duplicate or missing tool-call ID')
            pending.add(call['id']);seen.add(call['id']);calls+=1
        if message.get('role')=='tool':
            if message.get('tool_call_id') not in pending:raise ValueError('Tool result without matching call')
            pending.remove(message['tool_call_id'])
    if pending or calls==0:raise ValueError('Incomplete tool trace')
    return True

def approved_traces(directory,contract):
    """Load reviewer-curated traces only. Raw production logs are never training input."""
    path=Path(directory)/'approved.jsonl'
    if not path.exists():return []
    rows=[];sources=set()
    for line in path.read_text().splitlines():
        if not line.strip():continue
        row=json.loads(line)
        validate_trace(row,contract)
        source=row.get('source_id')
        if not isinstance(source,str) or not source.strip() or source in sources:raise ValueError('Missing or duplicate trace source_id')
        if not row.get('reviewer') or not row.get('reviewed_at') or row.get('outcome_verified') is not True:raise ValueError('Tool outcome requires human verification')
        if row['messages'][-1].get('role')!='assistant':raise ValueError('Trace must include final assistant response')
        sources.add(source)
        rows.append({**row,'lane':'cory_public','channel':'phone_tool','tools':contract['tools']})
    return rows
