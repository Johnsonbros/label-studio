"""Fail-closed v2 training/eval ingestion. Never executes tools or grants approval."""
import hashlib,json
from pathlib import Path
from jsonschema import Draft202012Validator,FormatChecker
from tool_contracts import contract_hash,validate_trace

SCHEMA=json.loads((Path(__file__).parent/'cory_sft_record.v2.schema.json').read_text())
VALIDATOR=Draft202012Validator(SCHEMA,format_checker=FormatChecker())

def validate_record(row,contract,*,purpose='train',receipt_root=None):
    if purpose not in ['train','eval']:raise ValueError('Invalid loading purpose')
    VALIDATOR.validate(row)
    p=row['provenance']
    if p['source_kind']=='owner_test':raise ValueError('Owner simulations cannot enter model training or loss evaluation')
    if p['review_status']!='human_approved' or p['redaction_status']!='approved':raise ValueError('Human approval and redaction required')
    if p['source_kind'] in ['archive_call','live_call']:
        if p['consent_review_status']!='approved':raise ValueError('Consent review required')
    elif p['consent_review_status'] not in ['approved','not_applicable']:raise ValueError('Consent status unresolved')
    if purpose=='train' and (not p['training_eligible'] or row['eval_holdout'] or p['source_kind']=='benchmark_fixture'):raise ValueError('Not eligible for training')
    if purpose=='eval' and (not row['eval_holdout'] or p['training_eligible']):raise ValueError('Evaluation records must be held out and training-ineligible')
    if row['topics']['primary'] in row['topics']['secondary']:raise ValueError('Duplicate primary topic')
    if row['contract_version']!=contract['version'] or contract_hash(contract['tools'])!=contract['version']:raise ValueError('Untrusted contract hash')
    if contract_hash(row['tools'])!=contract['version']:raise ValueError('Embedded contract differs from pinned contract')
    available=set(row['tools_available']);definitions={t['name']:t for t in contract['tools']}
    if not available<=set(definitions):raise ValueError('Nonpublic tool exposed')
    if row['messages'][-1]['role']!='assistant' or row['messages'][-1].get('tool_calls'):raise ValueError('Final assistant response required')
    calls={};results={}
    for m in row['messages']:
        for c in m.get('tool_calls',[]):
            if c['function']['name'] not in available:raise ValueError('Called tool was not exposed')
            calls[c['id']]=c
        if m['role']=='tool':results[m['tool_call_id']]=m
    if calls:
        validate_trace({'contract_version':row['contract_version'],'review_status':'approved_redacted','messages':row['messages']},contract)
        receipts=p['tool_receipts']
        if len(receipts)!=len(calls) or {x['tool_call_id'] for x in receipts}!=set(calls):raise ValueError('Missing or duplicate receipt references')
        if receipt_root is None:raise ValueError('Trusted reviewed receipt store required')
        root=Path(receipt_root).resolve()
        for ref in receipts:
            path=root/(ref['sha256']+'.json')
            if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('Invalid receipt path')
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise ValueError('Receipt hash mismatch')
            receipt=json.loads(raw);call=calls[ref['tool_call_id']];fn=call['function'];result=results[call['id']]
            expected={'tool_call_id':call['id'],'tool_name':fn['name'],'arguments':fn['arguments'],'result':json.loads(result['content']),'execution_kind':ref['execution_kind'],'source_id':p['source_id']}
            if receipt!=expected or result['name']!=fn['name']:raise ValueError('Receipt differs from tool invocation/result')
            output=definitions[fn['name']].get('outputSchema')
            if output:Draft202012Validator(output).validate(receipt['result'])
    elif results or p['tool_receipts']:raise ValueError('Orphan tool result or receipt')
    # Preserve metadata and provide the existing tokenizer's expected envelope.
    return {**row,'source_id':p['source_id'],'group_id':p['group_id'],'channel':'phone_tool' if calls else row['channel'],
            'review_status':'approved_redacted','reviewer':p['reviewer'],'reviewed_at':p['reviewed_at'],'outcome_verified':bool(calls)}

def assert_disjoint(train,evaluation):
    for key in ['source_id','group_id']:
        a={r.get(key) for r in train if r.get(key)};b={r.get(key) for r in evaluation if r.get(key)}
        if a&b:raise ValueError('Train/eval '+key+' leakage')
