"""Separate preference pairs and lossless WAV excerpts; never modify source audio."""
import hashlib,json,wave
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs,urlparse
from export_reviewed import accepted,choices,text_field,number_field,positive_quality,split_for

def trim_wav(source,destination,start,end):
    if start is None or end is None or start<0 or end<=start:raise ValueError('invalid_clip_bounds')
    with wave.open(str(source),'rb') as audio:
        rate=audio.getframerate();duration=audio.getnframes()/rate
        if end>duration:raise ValueError('clip_exceeds_recording')
        first=round(start*rate);last=round(end*rate)
        if last<=first:raise ValueError('empty_clip')
        audio.setpos(first);content=audio.readframes(last-first);params=audio.getparams()
    with wave.open(str(destination),'wb') as target:
        target.setparams(params);target.writeframes(content)
    destination.chmod(0o600)

def resolve_audio(url,roots):
    value=parse_qs(urlparse(url).query).get('d',[''])[0]
    collection,separator,relative=value.partition('/')
    if not separator or collection not in roots:raise ValueError('unknown_audio_collection')
    root=Path(roots[collection]).resolve();path=root/relative
    if path.is_symlink() or root not in path.resolve().parents:raise ValueError('unsafe_audio_path')
    return path

def curate(tasks,output,roots):
    fingerprint=hashlib.sha256(json.dumps(tasks,sort_keys=True).encode()).hexdigest()
    directory=Path(output)/fingerprint[:20]
    if (directory/'manifest.json').exists():return json.loads((directory/'manifest.json').read_text())
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    pairs=[];clips=[];skips=Counter()
    ids=Counter(t.get('data',{}).get('source_id') for t in tasks)
    for task in tasks:
        source=task.get('data',{}).get('source_id')
        if not isinstance(source,str) or not source or ids[source]!=1:continue
        annotations=[a for a in task.get('annotations',[]) if accepted(a)]
        if len(annotations)!=1:continue
        a=annotations[0]
        if choices(a,'quality_review')!=['human_confirmed']:continue
        use=choices(a,'training_use')
        if use not in [['positive_example'],['preference_pair']]:continue
        if use==['positive_example'] and not positive_quality(a):continue
        split=split_for(source,10)
        if use==['preference_pair']:
            fields={k:text_field(a,n) for k,n in [('prompt','preference_context'),('chosen','chosen_reply'),('rejected','rejected_reply'),('reason','preference_reason')]}
            if all(fields.values()) and fields['chosen'].casefold()!=fields['rejected'].casefold():
                pairs.append({**fields,'source_id':source,'split':split,'artifact_type':'human_reviewed_preference_pair',
                              'chosen_provenance':'reviewer_authored_rewrite','rejected_provenance':'reviewer_selected_recorded_reply'})
            else:skips['incomplete_or_identical_pair']+=1
        start=number_field(a,'clip_start');end=number_field(a,'clip_end')
        if start is None and end is None:continue
        try:
            audio=resolve_audio(task['data']['audio'],roots)
            name=hashlib.sha256(f'{source}:{start}:{end}'.encode()).hexdigest()+'.wav'
            trim_wav(audio,directory/name,start,end)
            clips.append({'source_id':source,'split':split,'file':name,'start':start,'end':end,
                          'training_use':use[0],'text':text_field(a,'training_excerpt'),
                          'audio_redacted':False,'audio_represents':'actual_recording_not_the_chosen_rewrite'})
        except (ValueError,OSError,wave.Error,EOFError):skips['invalid_or_unavailable_clip']+=1
    files={}
    for name,rows in [('preferences.jsonl',pairs),('preferences-train.jsonl',[p for p in pairs if p['split']=='train']),
                      ('preferences-eval.jsonl',[p for p in pairs if p['split']=='eval']),('clips.jsonl',clips)]:
        data=''.join(json.dumps(r)+'\n' for r in rows).encode();p=directory/name;p.write_bytes(data);p.chmod(0o600)
        files[name]={'count':len(rows),'sha256':hashlib.sha256(data).hexdigest()}
    manifest={'input_sha256':fingerprint,'preference_pairs':len(pairs),'clips':len(clips),'skipped':dict(skips),'files':files,
              'preference_training':'staged_only_not_consumed_by_the_SFT_trainer','audio_policy':'private_unredacted_excerpts'}
    p=directory/'manifest.json';p.write_text(json.dumps(manifest,indent=2));p.chmod(0o600)
    return manifest
