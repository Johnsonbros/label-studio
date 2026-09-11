# Recorded-call dataset service

This deployment extension connects recorded calls to a Label Studio review project.
It runs separately from Label Studio core; upstream application files are unchanged.

## Behavior

- Signed Twilio recording-completed callbacks enter a persistent SQLite queue.
- A daily seven-day overlapping Twilio reconciliation repairs missed callbacks.
  Only calls involving configured company phone numbers are eligible.
- New WAV files in the mounted archive are discovered daily. Existing transcribed
  archive calls are adopted; the old untranscribed backlog is not bulk-imported.
- CPU `base.en` Whisper creates draft transcripts. Source audio stays unchanged.
- Stable recording IDs deduplicate filename aliases, repeated callbacks and retries.
- Label Studio receives draft tasks. Humans correct speaker roles and redact text.
- Authenticated Label Studio annotation/task webhooks refresh review readiness.
- The ML backend connects the existing local Cory Qwen3.5 9B brain and returns
  speaker-region and transcript predictions. It checks the live speech pool before
  each bounded request and refuses work when the pool is busy or unreachable.
  A call arriving during an in-flight request can still contend briefly; this is
  cooperative priority, not a scheduler reservation. Prediction results are cached.

## Quarterly candidates

Starting on `FIRST_TRAINING_DATE`, each calendar quarter prepares a candidate once
at least 100 training calls and 10 held-out calls qualify. Calls require exactly one
approved, redacted human annotation with a corrected transcript, a human-confirmed
quality score of at least 80, no critical failures, and a positive training excerpt.
The 80-point cutoff is a provisional editorial gate; calibrate it with reviewers.
The excerpt
must contain unambiguous `CUSTOMER:` and `STAFF:` turns. Drafts and model predictions
alone never qualify. Splits are stable by source ID.

## Quality review and negative examples

Apply `config/audio-review.xml` to the call project. The local judge proposes five
subscores totaling 100: accuracy/appropriate uncertainty 25, listening 20,
professionalism 20, next step 25, efficiency 10. Every subscore must cite an exact
transcript quote; timestamps are derived from the source segment. Ungrounded
judgments retry and never become approval. Default background limit: 10 calls/day.
Audio delivery and external booking/policy verification require separate review.
Compare the first 20–30 draft judgments with human grades before trusting the rubric.
A lost booking is not automatically poor service.

Reviewers select positive example, preference pair, or exclusion. Preserve what was
actually said in the corrected transcript; put an improved response in the separate
chosen-reply field. `export_curated.py` exports same-context chosen/rejected pairs
with provenance and call-level splits. These are staged for preference training;
the current QLoRA trainer does not run DPO. They never enter positive-only SFT.
Optional start/end seconds create a new audio clip without altering the original.
Clips remain private and are not audio-redacted by transcript redaction.

## Verified knowledge retrieval

Use a separate project with `config/knowledge-review.xml`, set `KNOWLEDGE_PROJECT_ID`,
and configure its annotation webhook. Only one explicitly approved public-company
annotation with a source URL, graph relationship, and 1–365 day review interval
qualifies. Raw call transcripts and customer records are never indexed.

CPU `BAAI/bge-small-en-v1.5` embeddings use the existing internal Qdrant service,
in collection `cory_public_company_facts_v1`. SQLite stores typed subject/relation/
object edges, provenance, approval and expiry. Search filters company/visibility,
then rechecks revocation and expiry against SQLite and attaches related facts.
Revoked vectors may remain stored but cannot pass the authoritative review filter.

Private `POST /knowledge/search` accepts `{ "query": "...", "limit": 5 }` and requires
`Authorization: Bearer <KNOWLEDGE_READ_TOKEN>`. Failed or stale review synchronization
returns 503. Do not expose this token or the private curator MCP to callers.
The deployed Cory bridge now exposes the narrowly scoped `search_company_knowledge`
tool; see `voice-integration/`. Twelve facts were verified against company pages;
four proposals remain unapproved. Verification provenance identifies Codex as the
reviewer, not a human. Availability, booking and customer-specific information
must continue to use live operational tools.

## Tool-call training

Set `PUBLIC_MCP_URL` to Cory's actual operational MCP endpoint. Daily initialization
and `tools/list` support JSON or SSE and snapshot names, descriptions, input/output
schemas and annotations under `/state/tools/<contract-hash>.json`.

Place reviewed, redacted actual traces in `/state/tool-traces/approved.jsonl`.
Each row requires `source_id` (same stable call identity used by transcript data),
`contract_version`, `review_status: "approved_redacted"`, `reviewer`, `reviewed_at`,
`outcome_verified: true`, and chronological `messages`. Assistant `tool_calls` need
unique IDs, function names and arguments; each tool response needs the matching
`tool_call_id`. Include the final assistant reply. Do not infer real executions
from human phone recordings or label an unexecuted suggestion as a successful call.

The quarterly exporter validates the current contract, argument schemas, public
tool membership and call/result linkage before including traces. Contract changes
block old traces until re-reviewed. Traces share call-level train/eval splitting.
Tool examples do not replace the minimum count of reviewed phone calls. Training
renders the tool schemas and supervises each assistant turn, including tool calls;
tool result tokens are context only. Overlength tool traces fail explicitly.
Train and evaluation file hashes are checked before a candidate run.

Unchanged schemas do not prove unchanged tool behavior. Before promotion, run a
separate held-out suite covering correct tool choice/arguments, failed and timed-out
tools, duplicate requests, booking verification, appropriate handoff, and retrieval
accuracy. Held-out loss alone is insufficient; preserve the current model for rollback.

The separate trainer waits for at least 16000 MiB free GPU memory. It never stops
live services. It fine-tunes a QLoRA candidate, measures held-out loss before and
after training, and saves an adapter plus evaluation metadata. It stops its own
training process if GPU free memory falls below 1500 MiB. Failed/interrupted jobs
remain visible for investigation and are not silently marked complete.

**No automatic deployment:** call-control, tool-use and safety evaluations remain
required before promoting a candidate into the live receptionist. This dataset
does not invent tool-call traces from human conversations. The quarterly calendar
does not guarantee a model when reviewed data or GPU capacity are unavailable.

## Configuration

Build the intake image from this directory:

```sh
docker build -f pipeline/Dockerfile -t company-dataset-pipeline:local .
docker run --rm --entrypoint python company-dataset-pipeline:local -m unittest test_service test_foundations
cd scripts && python -m unittest test_export_reviewed test_curated
```

Provide these variables in a private Docker env file (mode 600):

- `LABEL_STUDIO_URL`, `LABEL_STUDIO_USER_TOKEN`, `PROJECT_ID`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `COMPANY_PHONE_NUMBERS` (comma separated)
- `PUBLIC_URL` (external HTTPS origin used for Twilio signature validation)
- `ADMIN_TOKEN`, `LABEL_WEBHOOK_TOKEN`, `ML_BASIC_PASSWORD` (independent random secrets)
- `FIRST_TRAINING_DATE`, `TRAIN_BASE_MODEL` (default `Qwen/Qwen3-8B`)
- `KNOWLEDGE_PROJECT_ID`, `KNOWLEDGE_READ_TOKEN`, `PUBLIC_MCP_URL`, `DAILY_QUALITY_LIMIT`

Mount a persistent directory at `/state` and the read-only call archive at `/archive`.
The archive must contain WAV files and optional `transcripts/<audio-stem>.json`.
Mount `/state/media` read-only into Label Studio at `/recordings/daily`; enable
local files with document root `/recordings` and create a local-files storage for
that path. Media files are owned by Label Studio UID 1001 with restrictive permissions.

Only publish these intake routes behind HTTPS, with a 32 KiB request limit:

- `POST /webhooks/twilio/recording`: Twilio signature plus account validation.
- `POST /webhooks/label-studio`: `Authorization: Bearer <LABEL_WEBHOOK_TOKEN>`.
- `GET /health`: liveness only; use authenticated `/status` for worker readiness.

In Label Studio project Webhooks, configure `/webhooks/label-studio`, the bearer
header, `send_payload=false`, and annotation-created/updated/deleted plus
task-created/deleted events. The service re-reads authoritative project data.

In Machine Learning, use the **private** service URL ending `/ml`, Basic Auth user
`label-studio` and `ML_BASIC_PASSWORD`, with timeout 120 seconds. The adapter expects
the existing internal brain and speech-pool DNS names shown in `ml_backend.py`.
The `/ml/train` endpoint schedules a readiness refresh; it does not bypass quarterly
review thresholds or immediately fine-tune the live model.

The optional `mcp/` image wraps the pinned HumanSignal curator MCP with authenticated
pipeline status and verified knowledge search. Set its private `DATASET_ADMIN_TOKEN`
and `KNOWLEDGE_READ_TOKEN`, plus the credentials required by the upstream Label Studio
MCP. Run its stdio entrypoint through your MCP client; this administrative server is
for dataset curation and is not the public receptionist tool server.

The trainer Dockerfile uses the locally maintained CUDA/PyTorch trainer image.
Its tested runtime has PyTorch 2.6.0/CUDA 12.4 and Transformers 4.54.1. Build it only
where that base image exists, or supply an equivalent tested base. Run with NVIDIA
runtime access and the same `/state` volume; do not mount a Docker socket.

## Operations and recovery

`/status` requires `Authorization: Bearer <ADMIN_TOKEN>` and reports queue counts,
polling errors, last daily run, review readiness, and quarterly job states.
Recordings retry with exponential backoff. SQLite queue state survives restarts.
A consistent `pipeline-backup.sqlite` is written after daily reconciliation;
this is local recovery, not an offsite backup. Back up `/state`, Label Studio's
database, and private environment files through your normal backup system.

Keep all credentials, recordings, raw exports, model caches, datasets and model
weights out of Git. Unit tests use synthetic data. Live verification covered
signed webhooks, an actual local-model prediction, daily call ingestion, CPU
transcription, authenticated audio playback, CUDA availability and the target
model's tokenizer. A full quarterly fine-tuning run still requires eligible data
and available GPU capacity.
