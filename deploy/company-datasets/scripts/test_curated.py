import json,tempfile,unittest,wave
from pathlib import Path
from export_curated import curate,trim_wav,resolve_audio

def field(name,value,kind='textarea'):
    return {'from_name':name,'type':kind,'value':{'text':value} if kind=='textarea' else {'choices':value} if kind=='choices' else {'number':value}}

class CuratedTests(unittest.TestCase):
    def test_negative_pair_is_separate_and_source_audio_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);audio=root/'call.wav'
            with wave.open(str(audio),'wb') as f:f.setparams((1,2,8000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*16000)
            before=audio.read_bytes()
            results=[field('disposition',['approved'],'choices'),field('privacy_review',['redacted'],'choices'),
                     field('quality_review',['human_confirmed'],'choices'),field('training_use',['preference_pair'],'choices'),
                     field('preference_context',['Customer asks about a leak']),field('chosen_reply',['Ask where the leak is']),
                     field('rejected_reply',['Dismiss the caller']),field('preference_reason',['Clarify the problem']),
                     field('clip_start',.25,'number'),field('clip_end',1.25,'number')]
            task={'data':{'source_id':'one-call','audio':'/data/local-files/?d=hcp/call.wav'},'annotations':[{'result':results}]}
            manifest=curate([task],root/'out',{'hcp':root})
            self.assertEqual(manifest['preference_pairs'],1);self.assertEqual(manifest['clips'],1)
            clip=next((root/'out').glob('*/*.wav'))
            with wave.open(str(clip)) as f:self.assertEqual(f.getnframes(),8000)
            self.assertEqual(audio.read_bytes(),before)
            self.assertEqual(curate([task],root/'out',{'hcp':root}),manifest)
    def test_path_escape_and_bad_bounds_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):resolve_audio('/data/local-files/?d=hcp/../secret.wav',{'hcp':tmp})
            with self.assertRaises(ValueError):trim_wav(Path(tmp)/'a.wav',Path(tmp)/'b.wav',5,1)

if __name__=='__main__':unittest.main()
