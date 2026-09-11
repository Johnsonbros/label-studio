import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from sft_v2 import validate_record,assert_disjoint,VALIDATOR
from tool_contracts import contract_hash

class V2Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  tools=[{'name':'check_service_area','inputSchema':{'type':'object','properties':{'town':{'type':'string'}},'required':['town'],'additionalProperties':False},'outputSchema':{'type':'object','properties':{'served':{'type':'boolean'}},'required':['served']}}]
  self.contract={'tools':tools,'version':contract_hash(tools)}
  p={'source_id':'a'*64,'group_id':'b'*64,'source_kind':'synthetic','reviewer':'fixture-reviewer','reviewed_at':'2026-09-11T00:00:00Z','review_status':'human_approved','training_eligible':True,'redaction_status':'approved','consent_review_status':'not_applicable','consent_basis':'Test fixture only','model_id':'fixture','tokenizer_revision':'fixture-revision','prompt_sha256':'c'*64,'tool_receipts':[]}
  receipt={'source_id':p['source_id'],'tool_call_id':'call1','tool_name':'check_service_area','arguments':{'town':'Quincy'},'result':{'served':True},'execution_kind':'sandbox'}
  raw=json.dumps(receipt).encode();digest=hashlib.sha256(raw).hexdigest();(self.root/(digest+'.json')).write_bytes(raw)
  p['tool_receipts']=[{'tool_call_id':'call1','sha256':digest,'execution_kind':'sandbox'}]
  self.row={'schema':'cory_sft_record/2.0','lane':'cory_public','channel':'phone','taxonomy_version':'1.0.0','topics':{'primary':'service_area','secondary':[]},'contract_version':self.contract['version'],'tools':tools,'tools_available':['check_service_area'],'provenance':p,'eval_holdout':False,'messages':[{'role':'user','content':'Do you serve Quincy?'},{'role':'assistant','content':None,'tool_calls':[{'id':'call1','type':'function','function':{'name':'check_service_area','arguments':{'town':'Quincy'}}}]},{'role':'tool','name':'check_service_area','tool_call_id':'call1','content':'{"served":true}'},{'role':'assistant','content':'Yes.'}]}
 def tearDown(self):self.tmp.cleanup()
 def valid(self,row=None,**kw):return validate_record(row or self.row,self.contract,receipt_root=self.root,**kw)
 def test_valid_tool_only_assistant(self):self.assertEqual(self.valid()['channel'],'phone_tool')
 def test_reject_owner_even_if_claims_approved(self):
  self.row['provenance']['source_kind']='owner_test'
  with self.assertRaises(ValueError):self.valid()
 def test_review_redaction_and_consent(self):
  for key,value in [('review_status','machine_draft'),('redaction_status','pending'),('training_eligible',False)]:
   row=copy.deepcopy(self.row);row['provenance'][key]=value
   with self.assertRaises(ValueError):self.valid(row)
  self.row['provenance']['source_kind']='archive_call'
  with self.assertRaises(ValueError):self.valid()
 def test_holdout_cannot_train_but_can_evaluate(self):
  self.row['eval_holdout']=True
  with self.assertRaises(ValueError):self.valid()
  self.row['provenance']['training_eligible']=False
  self.assertEqual(self.valid(purpose='eval')['source_id'],'a'*64)
 def test_private_or_unexposed_tool(self):
  self.row['tools_available']=[]
  with self.assertRaises(ValueError):self.valid()
 def test_wrong_arguments_and_result_link(self):
  row=copy.deepcopy(self.row);row['messages'][1]['tool_calls'][0]['function']['arguments']={}
  with self.assertRaises(Exception):self.valid(row)
  self.row['messages'][2]['tool_call_id']='orphan'
  with self.assertRaises(ValueError):self.valid()
 def test_changed_receipt_or_missing_store(self):
  self.row['messages'][2]['content']='{"served":false}'
  with self.assertRaises(ValueError):self.valid()
  with self.assertRaises(ValueError):validate_record(self.row,self.contract)
 def test_duplicate_calls(self):
  self.row['messages'][1]['tool_calls']*=2
  with self.assertRaises(ValueError):self.valid()
 def test_contract_drift(self):
  self.row['contract_version']='d'*64
  with self.assertRaises(ValueError):self.valid()
 def test_null_plain_assistant_rejected(self):
  self.row['messages'][-1]['content']=None
  with self.assertRaises(Exception):self.valid()
 def test_group_leakage(self):
  with self.assertRaises(ValueError):assert_disjoint([{'source_id':'a','group_id':'x'}],[{'source_id':'b','group_id':'x'}])
 def test_benchmark_fixture_cannot_train(self):
  self.row['provenance']['source_kind']='benchmark_fixture'
  with self.assertRaises(ValueError):self.valid()
 def test_actual_loader_enforces_v2_holdout_and_schema_dispatch(self):
  import ast
  from unittest.mock import patch
  import sft_v2
  source=Path(__file__).with_name('train_candidate.py')
  if not source.exists():source=Path('/app/train_candidate.py')
  function=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='load')
  contract_path=self.root/'contract.json';contract_path.write_text(json.dumps(self.contract))
  def paths(p):return contract_path if str(p)=='/app/public-mcp-v1-candidate.json' else Path(p)
  scope={'json':json,'Path':paths};exec(compile(ast.Module(body=[function],type_ignores=[]),'loader-test','exec'),scope)
  path=self.root/'input.jsonl';self.row['eval_holdout']=True;self.row['provenance']['training_eligible']=False;path.write_text(json.dumps(self.row)+'\n')
  real=sft_v2.validate_record
  with patch.object(sft_v2,'validate_record',side_effect=lambda r,c,**kw:real(r,c,purpose=kw['purpose'],receipt_root=self.root)):
   with self.assertRaises(ValueError):scope['load'](path,purpose='train')
   self.assertEqual(scope['load'](path,purpose='eval')[0]['group_id'],'b'*64)
   del self.row['schema'];path.write_text(json.dumps(self.row)+'\n')
   with self.assertRaises(ValueError):scope['load'](path,purpose='train')

if __name__=='__main__':unittest.main()
