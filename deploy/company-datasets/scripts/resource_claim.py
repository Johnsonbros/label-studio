"""Cooperative, atomic resource claims. All launchers must opt in."""
import argparse, contextlib, fcntl, json, os, time, uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'pipeline-state'/'coordination'
RESOURCES={'gpu-window','cory-voice','label-studio','dataset-pipeline'}

def conflicts(a,b):
    return a==b or {a,b}=={'gpu-window','cory-voice'}

@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'claims.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=STATE/'claims.json'
        data=json.loads(path.read_text()) if path.exists() else {'version':1,'claims':[]}
        yield data
        tmp=STATE/'claims.json.tmp';tmp.write_text(json.dumps(data,indent=2));tmp.replace(path)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['show','acquire','release','handoff'])
    p.add_argument('--resource',choices=sorted(RESOURCES));p.add_argument('--owner');p.add_argument('--intent');p.add_argument('--claim-id');p.add_argument('--note');a=p.parse_args()
    with locked() as board:
        if a.action=='show':print(json.dumps(board,indent=2));return
        if not a.owner:p.error('--owner required')
        if a.action=='handoff':
            if not a.note:p.error('--note required')
            stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
            with (ROOT/'HANDOFF.md').open('a') as f:f.write(f'\n## {stamp} — {a.owner}\n\n{a.note}\n')
            print('Appended handoff');return
        if a.action=='acquire':
            if not a.resource or not a.intent:p.error('--resource and --intent required')
            blockers=[c for c in board['claims'] if conflicts(c['resource'],a.resource)]
            if blockers:raise SystemExit('RESOURCE_BUSY '+json.dumps(blockers))
            claim={'id':str(uuid.uuid4()),'resource':a.resource,'owner':a.owner,'intent':a.intent,'acquired_at':time.time()}
            board['claims'].append(claim);print(json.dumps(claim));return
        found=next((c for c in board['claims'] if c['id']==a.claim_id and c['owner']==a.owner),None)
        if not found:raise SystemExit('Matching owner and claim ID required')
        board['claims'].remove(found);print('Released '+found['id'])

if __name__=='__main__':main()
