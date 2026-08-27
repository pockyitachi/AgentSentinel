# Agent instructions

This is the AgentSentinel monorepo. The active implementation target is
`MobileWorld/`, but the authoritative scope and contracts live in
`mobileworld_audit_handoff/`.

Before changing code, read these files completely:

1. `mobileworld_audit_handoff/AGENTS.md`
2. `mobileworld_audit_handoff/PROJECT_CONTEXT.md`
3. `mobileworld_audit_handoff/DECISION_LOG.md`
4. `mobileworld_audit_handoff/G1_4_DECISION_LOG.md`
5. `mobileworld_audit_handoff/EVENT_CONTRACT_V1.md`
6. `mobileworld_audit_handoff/SERVER_AGENT_INSTRUCTIONS.md`
7. `mobileworld_audit_handoff/STATUS.md`
8. `mobileworld_audit_handoff/G1_CAUSAL_REPLAY_PROTOCOL_V1.md`
9. `mobileworld_audit_handoff/G1_LOCKED_ANALYSIS_PLAN_V1.md`
10. `mobileworld_audit_handoff/G1_PORTABLE_SENTINEL_CONTRACT_V1.md`
11. `mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1.md`
12. `mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md`
13. `mobileworld_audit_handoff/G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md`
14. `mobileworld_audit_handoff/G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md`
15. `mobileworld_audit_handoff/G1_SENTINEL_MVP_MIGRATION.md`
16. `mobileworld_audit_handoff/g1/registry.lock.v1.json`
17. `mobileworld_audit_handoff/schemas/g1_3/replay_capsule.v1_1.schema.json`
18. `mobileworld_audit_handoff/schemas/g1_3/capsule_manifest.v1_1.schema.json`
19. `mobileworld_audit_handoff/schemas/g1_3/capsule_integrity.v1_1.schema.json`
20. `mobileworld_audit_handoff/schemas/g1_3/field_visibility.schema.json`
21. `mobileworld_audit_handoff/schemas/g1_3/capsule_exclusion.schema.json`

The historical `replay_capsule.schema.json`, `capsule_manifest.schema.json`, and
`capsule_integrity.schema.json` remain byte-frozen v1 references. Amendment 1
and the three `v1_1` schemas are authoritative for formal G1 use.

Latest completed scope: ALE-321 / G1.3, a CPU-only offline derivation that
formally published immutable, self-validating decision capsules for exactly
190 frozen targets (152 strict-MHR candidates plus 38 selected clean controls)
from the frozen G1.1 registry and Collector v1 artifacts. The separate 38
reserve clean controls remain census-only and outside G1.3 capsule/exclusion
scope. Contract Amendment 1 corrected the explicit fail-closed authorization
guards in a v1.1 content-addressed publication; the former v1 publication is
immutable and superseded for formal G1 use.

Active authorized scope: ALE-322 / G1.4 has a validated CPU/fake checkpoint at
commit `bf099a1a00f38edc33b6c5cbb1ab5d12d53bd18c`, covering the exact-request
runner, invariant/diff guards, deterministic scheduling and idempotent derived
attempt storage, blinded exports, versioned schemas/CLI, an in-process fake
provider, and an injectable OpenAI-compatible Provider Codec exercised only
through fake SDK clients. Formal v1.1 capsules remain read-only and retain
`execution_ready=false`, `provider_invocation_allowed=false`, and
`treatment_response_generation_allowed=false`; the CPU tranche must fail closed
before every external send.

`G1_4_DECISION_LOG.md` D-026 additionally authorizes inert/code-only preparation
for a future live/GPU proof: static frozen-model binding, pure call/block/launch
and caller-injected response records, injected-only capacity assessment, schemas,
and CPU tests. It does not authorize a client, network, subprocess, GPU probe or
use, model load, provider send, replay, or action. All live entrypoints remain
mechanically disabled pending a new owner authorization and downstream seals.

Collector v1 remains event-sourced, lossless, label-free, zero-intervention,
and byte-immutable. Do not invoke any real model/provider, use the network or a
GPU, load/serve model weights, execute a GUI/tool/action, restore a backend,
run a deterministic prefix or live replay, generate a treatment response,
infer claim validity, choose an intervention, implement runtime Sentinel
behavior, or start G1.5+. The deterministic fake-provider conformance path is
the only permitted execution substitute. Store real capsule, collection, and
replay data outside the Git repository. Preserve unrelated user changes and
record server findings and completed phases in
`mobileworld_audit_handoff/STATUS.md`. Do not mark ALE-322 complete until a
separately authorized live/GPU proof satisfies its remaining acceptance gates.
