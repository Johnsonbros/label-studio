# TJB public MCP v1 candidate

Endpoint: https://thejohnsonbros.com/mcp

The checked-in `public-mcp-v1-candidate.json` is the live tools/list baseline,
including input and output schemas. It is a release candidate, not a declaration
that every workflow is production-complete. Private customer-history and direct
HCP write tools are intentionally outside this public contract.

## Stable behavior required before training release

- `check_service_area` checks the requested location; generic company text must not
  broaden the approved service area.
- `get_availability` returns actual windows. Preserve date/time meanings, ISO
  timestamps and America/New_York daylight-saving handling.
- `request_booking` requires explicit permission, caller details and nested address;
  it starts SMS verification and returns the pending booking identifier. It is not
  a confirmed appointment.
- `confirm_booking` takes the actual pending identifier and caller-supplied code.
  Only `status: confirmed` plus `booked: true` permits a confirmed-booking claim.
  Office-follow-up outcomes remain unbooked. Duplicate confirmation must preserve
  the original outcome without creating a second appointment.
- `request_callback` needs permission and complete callback details; a failure
  never permits claiming that a callback was queued.
- Preserve machine-readable outcome/error meanings and the structured/text result
  equivalence used by different MCP clients.
- Ordinary business-information, catalog and document lookups remain reads.

## Change policy

Keep the v1 tool names, accepted arguments, consent requirements and existing result
meanings compatible. Internal CRM/HCP refactors, performance fixes and changing
availability data do not by themselves require retraining. New business facts belong
in current retrieval or tool data, not automatically in model weights.

The dataset monitor compares the current live contract against the candidate:
removed tools or input/output/annotation changes require compatibility review;
description-only changes and added tools are reported separately. This comparison
is conservative and does not prove behavioral compatibility. Even description edits
can affect tool selection and should run the existing model evaluations.

Keep the model-visible tool set pinned when adding tools. If an incompatible v2 is
needed, preserve v1 behind an adapter while evaluating v2. Do not silently change
the meaning of an existing success flag. Retraining is a decision made after tests
show a learned behavior needs changing, never an automatic response to a server SHA
or tool-list hash changing. Existing exact-hash trace validation still requires
review/revalidation when its snapshot changes; that is not a model training run.

## Remaining release gates

1. Align Cory's model-visible serving interface with this public contract. The
   current voice wrapper differs, particularly for booking addresses and binding
   caller identity. Preserve caller identity and consent checks in any adapter.
2. Run end-to-end isolated sandbox cases for booking, callback, invalid/expired
   codes, duplicate submissions, unavailable slots, rate limits and backend failure.
   TJB already contains sandbox and booking regression tests; run the candidate's
   exact server build through the protected Preview release process.
3. Run the current small model through the task suite, including the observed
   false SMS/callback narration case. Tool-schema compatibility cannot prevent a
   model from claiming an action without executing it; runtime outcome enforcement
   and model evaluation are separate requirements.
4. Pin each released model to its base-model/tokenizer version, serving tool
   contract, system prompt, dataset revision and evaluation results.

The monitor is installed in the dataset service. TJB PR #1384 merged the matching
baseline and a real MCP tools/list compatibility test into protected Preview on
2026-09-10 after all seven required checks passed. The existing integration-test
job now checks tool names, input/output schemas and safety annotations against v1.
This enforces structural compatibility in Preview CI; the behavioral and serving
interface gates above still apply.

TJB change: https://git.aisyncservices.com/johnsonbros/johnsonbros/pulls/1384
