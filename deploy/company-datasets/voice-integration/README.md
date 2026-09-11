# Cory voice knowledge adapter

The adapter is enabled in the deployed voice bridge. Configure private
`COMPANY_KNOWLEDGE_URL` and `COMPANY_KNOWLEDGE_TOKEN` in the bridge environment.
The supplied patch records the integration against the inspected live bridge;
it is not a patch against every historical bridge revision. Preserve concurrent
bridge changes when porting it. No secrets or call transcripts are included.

The adapter returns at most three current, approved public facts with citations.
It rejects wrong scopes and stale facts, bounds query size and request duration,
and returns an explicit failure so the agent can use existing business tools.
Readiness is exposed as `company_knowledge_enabled` in the bridge health response.
This indicates configuration, not a guarantee that the model selects the tool.

Validation: adapter tests cover expiry/revocation, scope, backend failure and invalid
queries. A live lookup from the voice container returned approved office-hour facts.
Existing voice-action tests were updated locally to use the current action ledger
and require a real available-window fixture before booking. No customer SMS or
booking was issued during these tests.

The accompanying `config/cory-tool-scenarios.json` contains 16 synthetic evaluation
cases derived from observed calls. They are not approved training traces, and they
have not yet been run against a trained candidate. Recent logs include unsupported
action narration without corresponding tool results. Preventing that behavior at
runtime remains a separate implementation task; retrieval does not fix it.

Important: the public MCP contract and the voice bridge tool interface differ.
For example, the bridge accepts flattened booking address fields and injects caller
identity, while the public MCP takes a nested address. Curated voice-training traces
must be checked against the actual serving interface before inclusion; a snapshot
of only public MCP tools is insufficient to certify voice-tool compatibility.
