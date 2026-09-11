#!/usr/bin/env python3
"""Pre-annotate Label Studio call-review tasks with speaker turns and a draft transcript.

For every task in the project that lacks a prediction from this MODEL_VERSION:
  1. load the existing Whisper transcript JSON (segments with start/end) for the task's audio,
  2. ask the LOCAL voice brain (Ollama, loopback only, no cloud) which speaker said each segment,
  3. post ONE prediction: a speaker region per segment, the per-region text, and a whole-call
     transcript laid out as speaker turns.

Predictions are suggestions the reviewer corrects; nothing here approves training data.
Transcript text is never printed. The brain is shared with the live phone receptionist, so
each request waits for the speech pool to be idle. Re-running is idempotent per model version.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_VERSION = "preannotate-v1"
SPEAKERS = ["staff", "customer", "other", "unknown"]
LABEL_STUDIO = os.environ.get("LABEL_STUDIO_URL", "http://127.0.0.1:8895")
BRAIN = os.environ.get("BRAIN_URL", "http://127.0.0.1:11436")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "qwen3.5:9b")
SPEECH_POOL = os.environ.get("SPEECH_POOL_URL", "http://127.0.0.1:8767/v1/pool")
RECORDINGS = Path(os.environ.get("RECORDINGS_ROOT", "/mnt/user/appdata/hcp-calls"))
CHUNK = 40  # segments per brain request

ROLE_PROMPT = (
    "You are labeling a recorded phone call for Johnson Bros. Plumbing & Drain Cleaning "
    "(Quincy, Massachusetts). Speakers: 'staff' = the company side (dispatcher or plumber, "
    "often Nate); 'customer' = the caller or homeowner; 'other' = automated systems, voicemail "
    "prompts, call screening, vendors, or anyone who is neither; 'unknown' = cannot tell. "
    "Each numbered segment is one automatic-transcript line in time order. Segments can be "
    "mis-split; judge by content and turn-taking. Return JSON with exactly one entry per index."
)
ROLE_SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "speaker": {"type": "string", "enum": SPEAKERS}},
        "required": ["i", "speaker"]}}},
    "required": ["labels"],
}


def env_token():
    env = dict(line.strip().split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line)
    return env["LABEL_STUDIO_USER_TOKEN"]


def ls_api(token, path, data=None, method=None):
    request = urllib.request.Request(
        LABEL_STUDIO + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Token " + token, "Content-Type": "application/json"},
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
        return json.loads(body) if body else None


def project_id():
    return json.loads((ROOT / "config/project.json").read_text())["id"]


def all_tasks(token, project):
    page, out = 1, []
    while True:
        response = ls_api(token, f"/api/tasks/?project={project}&page={page}&page_size=100")
        out.extend(response["tasks"])
        if len(out) >= response["total"] or not response["tasks"]:
            return out
        page += 1


def transcript_for(task):
    audio = urllib.parse.unquote(task["data"]["audio"].split("d=hcp/")[-1])
    path = RECORDINGS / "transcripts" / (Path(audio).stem + ".json")
    if not path.is_file() or path.is_symlink():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload = payload.get("whisper_response", payload)
    segments = [
        {"start": float(s["start"]), "end": float(s["end"]), "text": str(s.get("text", "")).strip()}
        for s in payload.get("segments") or []
        if isinstance(s, dict) and s.get("text", "").strip()
    ]
    return segments or None


def wait_for_idle_pool(max_wait=600):
    deadline = time.time() + max_wait
    while True:
        try:
            with urllib.request.urlopen(SPEECH_POOL, timeout=5) as response:
                if json.load(response).get("in_use", 0) == 0:
                    return True
        except Exception:
            return True  # pool unreachable: nothing to protect
        if time.time() > deadline:
            return False
        time.sleep(5)


def ask_brain(prompt):
    body = {"model": BRAIN_MODEL, "stream": False, "think": False, "keep_alive": -1, "format": ROLE_SCHEMA,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0, "num_predict": 4096, "num_ctx": 65536}}
    if not wait_for_idle_pool():
        raise RuntimeError("speech pool busy for too long; not contending with live calls")
    request = urllib.request.Request(BRAIN + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        content = json.load(response)["message"]["content"]
    return json.loads(content).get("labels", [])


def label_chunk(chunk, context_lines):
    """Label one chunk numbered locally from 0; retry once for the indexes the model skipped."""
    found = {}
    numbered = "\n".join(f"[{k}] {s['text']}" for k, s in enumerate(chunk))
    context = ("Preceding lines (already labeled, context only, do NOT return them):\n"
               + "\n".join(context_lines) + "\n\n") if context_lines else ""
    prompt = (ROLE_PROMPT + "\n\n" + context + f"Label ALL {len(chunk)} segments, indexes 0 to {len(chunk) - 1}:\n"
              + numbered)
    for attempt in range(2):
        for item in ask_brain(prompt):
            i = item.get("i")
            if isinstance(i, int) and 0 <= i < len(chunk) and item.get("speaker") in SPEAKERS and i not in found:
                found[i] = item["speaker"]
        missing = [k for k in range(len(chunk)) if k not in found]
        if not missing or attempt:
            break
        prompt += ("\n\nYour previous answer skipped indexes " + ", ".join(map(str, missing[:60]))
                   + ". Return every index from 0 to " + str(len(chunk) - 1) + ".")
    return [found.get(k, "unknown") for k in range(len(chunk))]


def stub_runs(segments, min_run=4):
    """Indexes of Whisper hallucination stubs: runs of >= min_run one-word, sub-second segments."""
    stub = [len(s["text"].split()) <= 1 and (s["end"] - s["start"]) < 1.0 for s in segments]
    flagged, start = set(), None
    for i, is_stub in enumerate(stub + [False]):
        if is_stub and start is None:
            start = i
        elif not is_stub and start is not None:
            if i - start >= min_run:
                flagged.update(range(start, i))
            start = None
    return flagged


def brain_roles(segments):
    """Return a speaker label per segment: non_speech for stub runs, brain-labeled otherwise."""
    stubs = stub_runs(segments)
    speech = [i for i in range(len(segments)) if i not in stubs]
    labels = ["non_speech"] * len(segments)
    spoken = []
    for offset in range(0, len(speech), CHUNK):
        chunk_idx = speech[offset:offset + CHUNK]
        context_lines = [f"({spoken[j]}) {segments[speech[j]]['text']}" for j in range(max(0, offset - 3), offset)]
        spoken.extend(label_chunk([segments[i] for i in chunk_idx], context_lines))
    for k, i in enumerate(speech):
        labels[i] = spoken[k]
    return labels


def turns_text(segments, labels):
    turns = []
    for segment, label in zip(segments, labels):
        if turns and turns[-1][0] == label:
            turns[-1][1].append(segment["text"])
        else:
            turns.append([label, [segment["text"]]])
    return "\n".join(f"{label.upper()}: {' '.join(parts)}" for label, parts in turns)


def build_result(segments, labels):
    result = []
    for segment, label in zip(segments, labels):
        region = uuid.uuid4().hex[:10]
        value = {"start": segment["start"], "end": segment["end"]}
        result.append({"id": region, "from_name": "speaker", "to_name": "audio", "type": "labels",
                       "value": {**value, "labels": [label]}})
        result.append({"id": region, "from_name": "segment_transcript", "to_name": "audio", "type": "textarea",
                       "value": {**value, "text": [segment["text"]]}})
    result.append({"from_name": "transcript", "to_name": "audio", "type": "textarea",
                   "value": {"text": [turns_text(segments, labels)]}})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="stop after N tasks (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="write predictions to data/preannotations/ instead of posting")
    parser.add_argument("--force", action="store_true", help="post even if a prediction with this model version exists")
    args = parser.parse_args()
    token = env_token()
    project = project_id()
    done = skipped = failed = 0
    for task in all_tasks(token, project):
        if args.limit and done >= args.limit:
            break
        # The task list does not embed predictions; ask for them explicitly so re-runs never duplicate.
        existing = [p for p in ls_api(token, f"/api/predictions/?task={task['id']}")
                    if p.get("model_version") == MODEL_VERSION]
        if existing and not args.force:
            skipped += 1
            continue
        if existing and not args.dry_run:
            for prediction in existing:  # --force replaces instead of stacking a second suggestion
                ls_api(token, f"/api/predictions/{prediction['id']}/", method="DELETE")
        segments = transcript_for(task)
        if not segments:
            failed += 1
            print(json.dumps({"task": task["id"], "status": "no_transcript"}))
            continue
        started = time.time()
        try:
            labels = brain_roles(segments)
        except Exception as error:
            failed += 1
            print(json.dumps({"task": task["id"], "status": "brain_error", "error": str(error)[:200]}))
            continue
        prediction = {"task": task["id"], "model_version": MODEL_VERSION, "score": 0.5,
                      "result": build_result(segments, labels)}
        counts = {s: labels.count(s) for s in SPEAKERS if labels.count(s)}
        if args.dry_run:
            out = ROOT / "data" / "preannotations"
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"task-{task['id']}.json"
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
                json.dump(prediction, handle, indent=1)
        else:
            ls_api(token, "/api/predictions/", prediction)
        done += 1
        print(json.dumps({"task": task["id"], "status": "dry_run" if args.dry_run else "posted",
                          "segments": len(segments), "speakers": counts, "ms": int((time.time() - started) * 1000)}))
    print(json.dumps({"done": done, "skipped_existing": skipped, "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
