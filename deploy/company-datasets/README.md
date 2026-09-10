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
approved, redacted human annotation with a corrected transcript. The transcript
must contain unambiguous `CUSTOMER:` and `STAFF:` turns. Drafts and model predictions
alone never qualify. Splits are stable by source ID.

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
docker run --rm --entrypoint python company-dataset-pipeline:local -m unittest test_service
```

Provide these variables in a private Docker env file (mode 600):

- `LABEL_STUDIO_URL`, `LABEL_STUDIO_USER_TOKEN`, `PROJECT_ID`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `COMPANY_PHONE_NUMBERS` (comma separated)
- `PUBLIC_URL` (external HTTPS origin used for Twilio signature validation)
- `ADMIN_TOKEN`, `LABEL_WEBHOOK_TOKEN`, `ML_BASIC_PASSWORD` (independent random secrets)
- `FIRST_TRAINING_DATE`, `TRAIN_BASE_MODEL` (default `Qwen/Qwen3-8B`)

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
