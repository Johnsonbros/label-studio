#!/usr/bin/env python3
"""Export reviewed text, not trainer-ready conversations.

Only one non-cancelled, non-skipped annotation may contain both required
choices. Multiple accepted annotations or duplicate reviewed source IDs are
ambiguous and skipped. draft_transcript is never used as reviewed text.
Splits use source_id, so all segments of a call must share that source_id.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

def digest(data):
    return hashlib.sha256(data).hexdigest()

def choices(annotation, name):
    values = [r.get("value", {}).get("choices") for r in annotation.get("result", [])
              if r.get("from_name") == name and r.get("type") == "choices"]
    return values[0] if len(values) == 1 else None

def accepted(annotation):
    return (not annotation.get("was_cancelled", False)
            and not annotation.get("skipped", False)
            and choices(annotation, "disposition") == ["approved"]
            and choices(annotation, "privacy_review") == ["redacted"])

def reviewed_text(annotation):
    matches = [r for r in annotation.get("result", [])
               if r.get("from_name") == "transcript" and r.get("type") == "textarea"]
    if len(matches) != 1:
        return None
    parts = matches[0].get("value", {}).get("text")
    if not isinstance(parts, list) or not parts or not all(isinstance(p, str) for p in parts):
        return None
    text = "\n".join(parts).strip()
    return text or None

def text_field(annotation, name):
    values=[r.get('value',{}).get('text') for r in annotation.get('result',[]) if r.get('from_name')==name and r.get('type')=='textarea']
    if len(values)!=1 or not isinstance(values[0],list) or not all(isinstance(x,str) for x in values[0]):return None
    return '\n'.join(values[0]).strip() or None

def number_field(annotation, name):
    values=[r.get('value',{}).get('number') for r in annotation.get('result',[]) if r.get('from_name')==name and r.get('type')=='number']
    if len(values)!=1 or isinstance(values[0],bool) or not isinstance(values[0],(int,float)):return None
    import math
    return values[0] if math.isfinite(values[0]) else None

def positive_quality(annotation):
    score=number_field(annotation,'quality_score')
    return (choices(annotation,'training_use')==['positive_example']
            and choices(annotation,'quality_review')==['human_confirmed']
            and choices(annotation,'critical_failures')==['none']
            and score is not None and 80<=score<=100)

def split_for(source_id, eval_percent):
    return "eval" if int(digest(source_id.encode()), 16) % 100 < eval_percent else "train"

def export(source, output_root, eval_percent=10):
    if not 0 <= eval_percent <= 100:
        raise ValueError("eval-percent must be 0..100")
    raw = Path(source).read_bytes()
    tasks = json.loads(raw)
    if not isinstance(tasks, list):
        raise ValueError("Expected Label Studio native JSON list")
    counts = Counter()
    pending = []
    for task in tasks:
        if not isinstance(task, dict):
            counts["invalid_task"] += 1
            continue
        annotations = task.get("annotations", [])
        if not isinstance(annotations, list):
            counts["invalid_annotations"] += 1
            continue
        approved = [a for a in annotations if isinstance(a, dict) and accepted(a)]
        if len(approved) != 1:
            counts["ambiguous_annotations" if len(approved) > 1 else "not_approved_redacted"] += 1
            continue
        data = task.get("data", {})
        source_id = data.get("source_id") if isinstance(data, dict) else None
        if not isinstance(source_id, str) or not source_id.strip():
            counts["missing_source_id"] += 1
            continue
        text = reviewed_text(approved[0])
        if text is None:
            counts["missing_reviewed_transcript"] += 1
            continue
        if not positive_quality(approved[0]):
            counts['not_positive_quality_reviewed'] += 1
            continue
        excerpt=text_field(approved[0],'training_excerpt')
        if not excerpt:
            counts['missing_training_excerpt'] += 1
            continue
        pending.append({"source_id": source_id.strip(), "task_id": task.get("id"),
                        "annotation_id": approved[0].get("id"), "text": excerpt,
                        "quality_score":number_field(approved[0],'quality_score'),
                        "disposition": "approved", "privacy_review": "redacted",
                        "artifact_type": "reviewed_transcript"})
    frequencies = Counter(row["source_id"] for row in pending)
    rows = []
    for row in pending:
        if frequencies[row["source_id"]] > 1:
            counts["duplicate_source_id"] += 1
            continue
        row["split"] = split_for(row["source_id"], eval_percent)
        rows.append(row)
    rows.sort(key=lambda row: row["source_id"])
    now = datetime.now(timezone.utc)
    version = now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    directory = Path(output_root) / version
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    files = {}
    for name, selected in [
        ("reviewed.jsonl", rows),
        ("train.jsonl", [r for r in rows if r["split"] == "train"]),
        ("eval.jsonl", [r for r in rows if r["split"] == "eval"])
    ]:
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in selected).encode()
        path = directory / name
        path.write_bytes(content)
        path.chmod(0o600)
        files[name] = {"sha256": digest(content), "count": len(selected)}
    manifest = {
        "schema_version": 1, "version": version, "created_at": now.isoformat(),
        "artifact_type": "reviewed_transcripts_not_trainer_ready",
        "input_sha256": digest(raw), "input_task_count": len(tasks),
        "exported_count": len(rows), "skipped_counts": dict(counts),
        "source_ids": [r["source_id"] for r in rows], "files": files,
        "split": {"algorithm": "sha256(source_id) integer modulo 100",
                  "eval_percent": eval_percent, "unit": "source_id"},
        "notes": [
            "Human-corrected annotation transcript only; draft_transcript ignored.",
            "Multiple accepted annotations and duplicate reviewed source IDs skipped.",
            "Speaker roles are not guaranteed; additional transformation and evaluation required.",
            "Use a stable call-level source_id; never include customer identifiers in IDs."
        ]
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    path.chmod(0o600)
    return directory, manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parents[1] / "exports")
    parser.add_argument("--eval-percent", type=int, default=10)
    args = parser.parse_args()
    directory, manifest = export(args.input, args.output_root, args.eval_percent)
    print(json.dumps({"export_directory": str(directory), "exported_count": manifest["exported_count"],
                      "skipped_counts": manifest["skipped_counts"]}))

if __name__ == "__main__":
    main()
