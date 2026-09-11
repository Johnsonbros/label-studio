import copy,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import service
import knowledge
from quality_judge import LIMITS,validate_judgment,prediction_fields
from tool_contracts import contract_hash,validate_trace,rpc_json,approved_traces,compatibility_report
import httpx

class FoundationTests(unittest.TestCase):
    def test_contract_compatibility_distinguishes_docs_from_schema_changes(self):
        baseline={'version':'v1','tools':[{'name':'book','description':'Book','inputSchema':{'type':'object'}}]}
        current=copy.deepcopy(baseline);current['version']='v2';current['tools'][0]['description']='Clearer booking instructions'
        report=compatibility_report(baseline,current)
        self.assertEqual(report['classification'],'existing_tool_shapes_preserved')
        self.assertFalse(report['automatic_retraining'])
        current['tools'][0]['inputSchema']['required']=['new_argument']
        self.assertEqual(compatibility_report(baseline,current)['classification'],'compatibility_review_required')
        self.assertEqual(compatibility_report(baseline,{'version':'v3','tools':[]})['removed_tools'],['book'])
    def test_judge_requires_grounded_evidence_for_every_score(self):
        raw={'scores':dict(LIMITS),'critical_flags':['unsafe_advice'],'confidence':'low',
             'evidence':[{'criterion':k,'quote':'Please explain the leak.','reason':'Example'} for k in LIMITS]}
        segments=[{'text':'Please explain the leak.','start':2,'end':5}]
        result=validate_judgment(raw,segments)
        self.assertEqual(result['total'],100)
        self.assertEqual(result['review_status'],'pending')
        self.assertEqual(result['evidence'][0]['start'],2)
        self.assertEqual(prediction_fields(result)[1]['value']['choices'],['pending'])
        raw['evidence'][0]['quote']='Invented quotation'
        with self.assertRaises(ValueError):validate_judgment(raw,segments)

    def test_tool_schema_contract_and_call_linkage(self):
        tools=[{'name':'check_service_area','inputSchema':{'type':'object','properties':{'town':{'type':'string'}},'required':['town'],'additionalProperties':False}}]
        version=contract_hash(tools);contract={'version':version,'tools':tools}
        trace={'contract_version':version,'review_status':'approved_redacted','messages':[
            {'role':'user','content':'Do you serve Quincy?'},
            {'role':'assistant','tool_calls':[{'id':'call1','function':{'name':'check_service_area','arguments':{'town':'Quincy'}}}]},
            {'role':'tool','tool_call_id':'call1','content':'{"served":true}'},
            {'role':'assistant','content':'Yes, we serve Quincy.'}]}
        self.assertTrue(validate_trace(trace,contract))
        duplicate=copy.deepcopy(trace);duplicate['messages']+=trace['messages'][1:]
        with self.assertRaises(ValueError):validate_trace(duplicate,contract)
        bad=copy.deepcopy(trace);bad['messages'][1]['tool_calls'][0]['function']['name']='admin_delete'
        with self.assertRaises(ValueError):validate_trace(bad,contract)
        changed=copy.deepcopy(tools);changed[0]['outputSchema']={'type':'string'}
        self.assertNotEqual(version,contract_hash(changed))
        with tempfile.TemporaryDirectory() as d:
            Path(d,'approved.jsonl').write_text(json.dumps(trace))
            with self.assertRaises(ValueError):approved_traces(d,contract)
        response=httpx.Response(200,headers={'content-type':'text/event-stream'},text='event: message\ndata: {"result":{"tools":[]}}\n\n')
        self.assertEqual(rpc_json(response),{'result':{'tools':[]}})

    def test_knowledge_rechecks_expiry_and_revocation(self):
        with tempfile.TemporaryDirectory() as d,patch.object(service,'STATE',Path(d)):
            with knowledge.connection() as c:
                for key,approved,expires in [('valid',1,time.time()+3600),('revoked',0,time.time()+3600),('expired',1,time.time()-1)]:
                    c.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?)',(key,'company','serves','town','Public fact','https://example.com',expires,approved,'v1'))
            hits={'result':[{'id':k,'score':.9} for k in ['revoked','expired','valid']]}
            with patch.object(knowledge,'embed',return_value=[0]*384),patch.object(knowledge,'vector_api',return_value=hits) as api:
                rows=knowledge.search('service area')
                self.assertEqual([r['id'] for r in rows],['valid'])
                self.assertEqual(len(rows[0]['related_facts']),1)
                self.assertEqual(api.call_args.args[2]['filter']['must'][0]['match']['value'],'johnson-bros')

if __name__=='__main__':unittest.main()
