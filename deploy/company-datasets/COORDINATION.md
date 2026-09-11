# Shared Cory work protocol

Canonical root: `/mnt/user/appdata/company-datasets` (Unraid persistent storage).

- Codex owns ingestion, Label Studio integration, grading, correction workflow, dataset packaging and trainer orchestration.
- Claude owns the live voice stack, public MCP integration, runtime safeguards and benchmark/promotion evaluation.
- The owner or dispatcher supplies human reference labels and approves customer-facing model promotion. This document does not authorize a candidate rollout.
- Claude may append to HANDOFF.md; coordinate ownership before editing other dataset repo files. Public copies must never include transcripts, customer identities, secrets, or private manifests.

## Contracts

`config/call-topic-taxonomy.v1.json` is version 1.0.0 of the shared topic IDs. Primary topic plus secondary topics describe what the caller needs. Workflow, urgency, outcome and error tags are separate axes. No forced inference: use unknown or unclassified when evidence is missing. A service topic can coexist with pricing or booking as a secondary topic. Taxonomy changes require a version bump and a handoff entry before consumers adopt them.

`config/cory_sft_record.schema.json` is an exact pinned copy of the existing cory-public-model schema. Its SHA is recorded in `config/training-contract.json`. No historical training rows are migrated automatically. That legacy schema alone does NOT validate modern tool-call conversations: it requires nonempty string content and does not validate tool-call IDs, arguments or results. The existing tool trace validator remains mandatory. A replacement SFT schema is a separate coordinated versioned change, not an incidental taxonomy edit.

Taxonomy is published for adoption; neither judge nor benchmark is claimed to consume it yet. Five grading categories and topic tagging can use these IDs, but missing evidence must remain unknown. Historical human calls are not graded for lacking today's MCP calls.

## Cooperative claims

Use `python3 scripts/resource_claim.py show` before any resource change.

Acquire: `python3 scripts/resource_claim.py acquire --resource gpu-window --owner claude --intent 'Measured transcription benchmark; no customer routing change'`

Release with the returned ID: `python3 scripts/resource_claim.py release --owner claude --claim-id ID`

`gpu-window` conflicts with `cory-voice`; identical resources conflict. Label Studio and dataset-pipeline changes have separate claims. These claims serialize cooperating operators using an OS file lock. They do not prevent arbitrary docker commands, and current cron/trainer launchers do not yet consult them. Launcher integration is required before calling this enforced scheduling. Inspect live processes, GPU usage and active calls even after acquiring a claim.

Claims never automatically expire: an elapsed timer does not prove a GPU job stopped. The acquiring owner releases only after the operation ends and checks pass. An abandoned claim requires reconciliation with the actual running job and coordination with its owner; do not silently overwrite it. State lives in `pipeline-state/coordination/claims.json`, not git.

Append handoffs atomically: `python3 scripts/resource_claim.py handoff --owner claude --note 'Changed: ... Deployed: ... Validation: ... Next: ...'`

No simultaneous restarts. A GPU window requires a measured duration estimate and an idle speech lane; plan customer overflow before any voice downtime. The CPU archive pilot currently needs no GPU window.
