# Cory reviewed SFT v2

Schema: config/cory_sft_record.v2.schema.json. Validator: pipeline/sft_v2.py. Version cory_sft_record/2.0; taxonomy 1.0.0. The old pinned reference remains unchanged. This implementation creates no training rows.

Canonical tool calls use id, type=function, function{name,arguments:object}. Assistant content may be null only with tool calls. Each call needs exactly one tool result before the next conversational turn, and a final plain assistant response. Arguments and structured JSON results are checked against the pinned public MCP schemas.

Rows record primary/secondary topics; complete pinned tool definitions and exposed subset; source/group SHA identifiers; source kind; human reviewer/date/status; redaction and consent-review status; originating model/tokenizer revision and prompt hash. The trainer currently accepts phone only, even though the schema can represent web/sms.

The v2 training loader rejects owner simulations, benchmark fixtures, held-out rows, unapproved/redaction-pending records, unresolved consent, contract drift, unexposed tools, malformed linkage and source/group leakage. Evaluation-loss rows must be human-approved, redacted, held out and training-ineligible; owner simulations are rejected there too. Unknown/stripped schema markers and mixed schema datasets fail closed. Existing legacy exports continue under their old validation, not retroactive v2 assurances. Exporters have not been migrated by this release.

Tool examples require separately reviewed receipts in /state/reviewed-tool-receipts/<sha256>.json. Exact fields: tool_call_id, tool_name, arguments, result, execution_kind (production or sandbox), source_id. The bytes must match the referenced SHA, and all values must match the training trace. A hash establishes integrity, not truthful execution: admission to this trusted receipt store requires evidence review. Never manufacture a receipt from historical dialogue. Sandbox results do not establish successful production booking.

Thirteen isolated tests cover eligibility, held-out loading, consent, redaction, null assistant tool calls, arguments/results/receipts, duplicates, contract drift and related-call leakage, including the actual loader function. No model load, QLoRA run, customer routing change or promotion is part of this update. Trainer image: company-dataset-trainer:sft-v2-20260911.
