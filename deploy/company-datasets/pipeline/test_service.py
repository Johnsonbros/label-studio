import os, tempfile, unittest
os.environ.setdefault('TWILIO_AUTH_TOKEN','test-secret')
os.environ.setdefault('TWILIO_ACCOUNT_SID','AC'+'1'*32)
os.environ.setdefault('ADMIN_TOKEN','private-test')
os.environ.setdefault('LABEL_WEBHOOK_TOKEN','webhook-test')
os.environ.setdefault('ML_BASIC_PASSWORD','ml-test')
from pathlib import Path
import service as s
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();s.STATE=Path(self.temp.name);s.init()
        self.client=TestClient(s.app)
    def tearDown(self): self.temp.cleanup()
    def test_unsigned_and_wrong_account_rejected(self):
        data={'RecordingSid':'RE'+'2'*32,'RecordingStatus':'completed','AccountSid':'AC'+'3'*32}
        self.assertEqual(self.client.post('/webhooks/twilio/recording',data=data).status_code,403)
        sig=RequestValidator(os.environ['TWILIO_AUTH_TOKEN']).compute_signature(s.PUBLIC+'/webhooks/twilio/recording',data)
        self.assertEqual(self.client.post('/webhooks/twilio/recording',data=data,headers={'X-Twilio-Signature':sig}).status_code,403)
    def test_signed_replay_deduplicated_and_persistent(self):
        data={'RecordingSid':'RE'+'2'*32,'RecordingStatus':'completed','AccountSid':os.environ['TWILIO_ACCOUNT_SID']}
        sig=RequestValidator(os.environ['TWILIO_AUTH_TOKEN']).compute_signature(s.PUBLIC+'/webhooks/twilio/recording',data)
        def send(): return self.client.post('/webhooks/twilio/recording',data=data,headers={'X-Twilio-Signature':sig})
        self.assertTrue(send().json()['new']);s.init();self.assertFalse(send().json()['new'])
        with s.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM recordings').fetchone()[0],1)
    def test_filename_alias_identity(self):
        sid='RE'+'a'*32
        self.assertEqual(s.identity(sid),s.identity('old_name_'+sid+'.wav'))
    def test_roles_and_ambiguity(self):
        self.assertIsNone(s.conversation('Unlabeled phone transcript'))
        self.assertIsNone(s.conversation('CUSTOMER: Hi\nUNKNOWN: uncertain\nSTAFF: Hello'))
        self.assertEqual(s.conversation('STAFF: Hello\nCUSTOMER: Leak\nSTAFF: Where?\nCUSTOMER: unfinished'),
                         [{'role':'user','content':'Leak'},{'role':'assistant','content':'Where?'}])
    def test_status_is_private(self):
        self.assertEqual(self.client.get('/status').status_code,401)
        self.assertEqual(self.client.get('/status',headers={'Authorization':'Bearer private-test'}).status_code,200)
    def test_invalid_recording_identifier(self):
        with self.assertRaises(ValueError):s.enqueue('../../secrets')
    def test_twilio_call_company_scope(self):
        from types import SimpleNamespace
        os.environ['COMPANY_PHONE_NUMBERS']='+16174753114'
        self.assertTrue(s.allowed_call(SimpleNamespace(to='+16174753114',_from='+15550000000')))
        self.assertTrue(s.allowed_call(SimpleNamespace(to='+15550000000',_from='+16174753114')))
        self.assertFalse(s.allowed_call(SimpleNamespace(to='+15550000001',_from='+15550000000')))
    def test_quarterly_requires_human_review(self):
        from unittest.mock import patch
        os.environ['FIRST_TRAINING_DATE']='2020-01-01'
        with patch.object(s,'api',return_value=[{'data':{'source_id':'a','draft_transcript':'CUSTOMER: Leak\nSTAFF: Hello'},'annotations':[]}]):s.quarterly()
        jobs=list((s.STATE/'quarterly').glob('*/job.json'))
        import json
        self.assertEqual(json.loads(jobs[0].read_text())['status'],'waiting_for_reviewed_calls')
    def test_label_webhook_requires_auth_and_queues_refresh(self):
        data={'action':'ANNOTATION_UPDATED'}
        self.assertEqual(self.client.post('/webhooks/label-studio',json=data).status_code,403)
        r=self.client.post('/webhooks/label-studio',json=data,headers={'Authorization':'Bearer webhook-test'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(s.meta('last_label_event'),'ANNOTATION_UPDATED')
    def test_ml_connected_and_train_does_not_start_training(self):
        import base64
        headers={'Authorization':'Basic '+base64.b64encode(b'label-studio:ml-test').decode()}
        self.assertEqual(self.client.get('/ml/health').status_code,401)
        self.assertEqual(self.client.get('/ml/health',headers=headers).json()['status'],'UP')
        self.assertFalse(self.client.post('/ml/train',headers=headers,json={}).json()['training_started'])

if __name__=='__main__':unittest.main()
