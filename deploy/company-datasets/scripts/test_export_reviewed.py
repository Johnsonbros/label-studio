import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from export_reviewed import export, split_for
from backup import backup

def annotation():
    return {"id": 10, "result": [
        {"from_name": "disposition", "type": "choices", "value": {"choices": ["approved"]}},
        {"from_name": "privacy_review", "type": "choices", "value": {"choices": ["redacted"]}},
        {"from_name": "transcript", "type": "textarea", "value": {"text": ["Corrected synthetic text."]}}
    ]}

def task(source="synthetic-call-1"):
    return {"id": 1, "data": {"source_id": source, "draft_transcript": "Unreviewed synthetic draft."},
            "annotations": [annotation()]}

class ExportTests(unittest.TestCase):
    def run_export(self, tasks):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "input.json"
        source.write_text(json.dumps(tasks))
        return export(source, root / "exports")

    def test_approved_corrected_only_and_manifest(self):
        directory, manifest = self.run_export([task()])
        row = json.loads((directory / "reviewed.jsonl").read_text())
        self.assertEqual(row["text"], "Corrected synthetic text.")
        self.assertNotIn("draft_transcript", row)
        self.assertEqual(manifest["exported_count"], 1)
        for name, info in manifest["files"].items():
            self.assertEqual(info["sha256"], hashlib.sha256((directory / name).read_bytes()).hexdigest())

    def test_missing_reviewed_text_never_uses_draft(self):
        sample = task()
        sample["annotations"][0]["result"].pop()
        _, manifest = self.run_export([sample])
        self.assertEqual(manifest["exported_count"], 0)
        self.assertEqual(manifest["skipped_counts"]["missing_reviewed_transcript"], 1)

    def test_cancelled_unredacted_rejected(self):
        samples = [task(str(i)) for i in range(3)]
        samples[0]["annotations"][0]["was_cancelled"] = True
        samples[1]["annotations"][0]["result"][1]["value"]["choices"] = ["contains_pii"]
        samples[2]["annotations"][0]["result"][0]["value"]["choices"] = ["rejected"]
        _, manifest = self.run_export(samples)
        self.assertEqual(manifest["exported_count"], 0)

    def test_ambiguous_accepted_annotations_rejected(self):
        sample = task()
        sample["annotations"].append(copy.deepcopy(annotation()))
        _, manifest = self.run_export([sample])
        self.assertEqual(manifest["skipped_counts"]["ambiguous_annotations"], 1)

    def test_duplicate_source_ids_rejected(self):
        _, manifest = self.run_export([task(), task()])
        self.assertEqual(manifest["exported_count"], 0)
        self.assertEqual(manifest["skipped_counts"]["duplicate_source_id"], 2)

    def test_split_stable_across_order_and_disjoint(self):
        samples = [task("synthetic-" + str(i)) for i in range(100)]
        a, _ = self.run_export(samples)
        b, _ = self.run_export(list(reversed(samples)))
        self.assertEqual((a / "reviewed.jsonl").read_bytes(), (b / "reviewed.jsonl").read_bytes())
        train = {json.loads(x)["source_id"] for x in (a / "train.jsonl").read_text().splitlines()}
        evaluation = {json.loads(x)["source_id"] for x in (a / "eval.jsonl").read_text().splitlines()}
        self.assertFalse(train & evaluation)
        self.assertEqual(len(train | evaluation), 100)
        self.assertTrue(train and evaluation)

    def test_sqlite_backup_keeps_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            source = root / "data" / "label_studio.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE synthetic (value INTEGER)")
                connection.execute("INSERT INTO synthetic VALUES (42)")
            target = backup(root)
            self.assertTrue(source.exists())
            with sqlite3.connect(target / "label_studio.sqlite3") as connection:
                self.assertEqual(connection.execute("SELECT value FROM synthetic").fetchone(), (42,))
            self.assertTrue((target / "manifest.json").exists())

if __name__ == "__main__":
    unittest.main()
