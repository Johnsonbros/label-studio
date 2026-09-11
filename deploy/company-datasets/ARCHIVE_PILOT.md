# First archive transcription pilot

Batch `archive-pilot-20260911-v1` selects 120 previously unqueued WAV recordings, deterministically stratified by recording month and duration. There are 20 per band: 8–30, 30–60, 60–120, 120–240, 240–480, and 480–900 seconds. Selection is not an assertion about intent, outcome, recording consent, or quality. The private manifest includes original filenames and must stay on the server.

The bounded worker uses the already cached faster-whisper base.en model on one CPU thread, with word timestamps, VAD and segment confidence information. It uses no GPU and makes no model download. These are draft transcripts: human review against the audio is required, particularly for names, addresses, prices, speaker attribution and poor audio. Word timestamps are stored under `pipeline-state/pilots/<batch>/`; the original archive remains read-only.

Each completed draft is imported into Label Studio project 1 with `batch_id`, `review_required:true`, `human_approved:false`, and `training_eligible:false`. The worker never creates annotations, approves examples, starts a training run, or invents tool-call traces. Mono audio speaker labels remain unverified. The existing review pipeline can propose judgments, but those are not human approvals.

After review, identify intent/outcome coverage gaps and deliberately select more examples. Use a separate validated MCP workflow suite for tool calls. Use current curated company facts through public MCP retrieval; old conversations are not authoritative company policy. Do not automatically include simulation logs or split related customer conversations across train/eval sets.

Resume with `docker compose -f compose.pilot.yml up -d`. A per-batch lock prevents duplicate workers. Transcript and import receipts are durable. Imports reconcile existing source identities before retrying. Per-call errors remain visible as error receipts; restarting retries incomplete entries. A completed batch exits successfully and does not loop.

Progress: count `*.transcript.json`, `*.imported.json`, and `*.error.json` in the batch directory. Errors contain exception types only; worker logs do not print transcript contents or caller names. Never publish the manifest, transcripts, or import receipts.
