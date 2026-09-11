import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import service,ml_backend
from topic_grading import validate_context,persist
from quality_judge import LIMITS

class TopicTests(unittest.TestCase):
    def setUp(self):
        self.segments=[{'start':1.0,'end':4.0,'text':'My kitchen sink is clogged. Can you book a visit?'}]
        self.raw={'context':{'primary_topic':'drain_fixture_clog','secondary_topics':['booking_callback'],
          'topic_evidence':[{'topic':'drain_fixture_clog','quote':'My kitchen sink is clogged.'},{'topic':'booking_callback','quote':'Can you book a visit?'}],
          'urgency':'routine','workflow':'booking','outcome':'claimed_unverified','critical_evidence':[]}}
    def test_grounding_and_unverified_outcome(self):
        out=validate_context(self.raw,self.segments,[])
        self.assertEqual(out['topic_evidence'][0]['start'],1)
        self.assertFalse(out['external_outcome_verified'])
        bad=copy.deepcopy(self.raw);bad['context']['topic_evidence'][0]['quote']='Invented words'
        with self.assertRaises(ValueError):validate_context(bad,self.segments,[])
        bad=copy.deepcopy(self.raw);bad['context']['outcome']='booking_verified'
        with self.assertRaises(ValueError):validate_context(bad,self.segments,[])
    def test_flag_requires_quoted_evidence(self):
        with self.assertRaises(ValueError):validate_context(self.raw,self.segments,['unsafe_advice'])
        self.raw['context']['critical_evidence']=[{'flag':'unsafe_advice','quote':'not present','reason':'suspected'}]
        with self.assertRaises(ValueError):validate_context(self.raw,self.segments,['unsafe_advice'])
    def test_topic_ids_and_evidence_coverage(self):
        bad=copy.deepcopy(self.raw);bad['context']['primary_topic']='invented_topic'
        with self.assertRaises(ValueError):validate_context(bad,self.segments,[])
        bad=copy.deepcopy(self.raw);bad['context']['topic_evidence']=[]
        with self.assertRaises(ValueError):validate_context(bad,self.segments,[])
    def test_pilot_loader_reads_word_timestamp_transcript(self):
        with tempfile.TemporaryDirectory() as d,patch.object(service,'STATE',Path(d)):
            p=Path(d)/'pilots'/'archive-pilot-20260911-v1';p.mkdir(parents=True)
            (p/('a'*64+'.transcript.json')).write_text(json.dumps({'segments':self.segments}))
            task={'data':{'pipeline':'archive-pilot','batch_id':p.name,'source_id':'a'*64,'audio':'/data/local-files/?d=hcp/example.wav'}}
            self.assertEqual(ml_backend.load_segments(task),self.segments)
            task['data']['batch_id']='../../outside'
            self.assertEqual(ml_backend.load_segments(task),[])
    def test_store_is_versioned_idempotent_and_never_approves(self):
        with tempfile.TemporaryDirectory() as d,patch.object(service,'STATE',Path(d)):
            judgment={'scores':dict(LIMITS),'context':validate_context(self.raw,self.segments,[]),'total':100,'confidence':'low','critical_flags':[],'rubric_version':'jbp-phone-v1'}
            for _ in range(2):persist({'id':1,'data':{'source_id':'a'*64}},judgment,self.segments,'test-v3')
            with service.db() as db:
                rows=db.execute('SELECT * FROM call_quality_grades').fetchall()
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['review_status'],'machine_draft');self.assertEqual(rows[0]['primary_topic'],'drain_fixture_clog')

if __name__=='__main__':unittest.main()
