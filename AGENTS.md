# Agent instructions

This is the AgentSentinel monorepo. The active implementation target is
`MobileWorld/`, but the authoritative scope and contracts live in
`mobileworld_audit_handoff/`.

Before changing code, read these files completely:

1. `mobileworld_audit_handoff/AGENTS.md`
2. `mobileworld_audit_handoff/PROJECT_CONTEXT.md`
3. `mobileworld_audit_handoff/DECISION_LOG.md`
4. `mobileworld_audit_handoff/EVENT_CONTRACT_V1.md`
5. `mobileworld_audit_handoff/SERVER_AGENT_INSTRUCTIONS.md`
6. `mobileworld_audit_handoff/STATUS.md`
7. `mobileworld_audit_handoff/G1_CAUSAL_REPLAY_PROTOCOL_V1.md`
8. `mobileworld_audit_handoff/G1_LOCKED_ANALYSIS_PLAN_V1.md`
9. `mobileworld_audit_handoff/G1_PORTABLE_SENTINEL_CONTRACT_V1.md`
10. `mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1.md`
11. `mobileworld_audit_handoff/G1_SENTINEL_MVP_MIGRATION.md`
12. `mobileworld_audit_handoff/g1/registry.lock.v1.json`
13. `mobileworld_audit_handoff/schemas/g1_3/replay_capsule.schema.json`
14. `mobileworld_audit_handoff/schemas/g1_3/field_visibility.schema.json`
15. `mobileworld_audit_handoff/schemas/g1_3/capsule_exclusion.schema.json`
16. `mobileworld_audit_handoff/schemas/g1_3/capsule_manifest.schema.json`
17. `mobileworld_audit_handoff/schemas/g1_3/capsule_integrity.schema.json`

Latest completed scope: ALE-321 / G1.3, a CPU-only offline derivation that
formally published immutable, self-validating decision capsules for exactly
190 frozen targets (152 strict-MHR candidates plus 38 selected clean controls)
from the frozen G1.1 registry and Collector v1 artifacts. The separate 38
reserve clean controls remain census-only and outside G1.3 capsule/exclusion
scope. ALE-322 / G1.4 and all later work remain unstarted and unapproved.
Collector v1 remains event-sourced, lossless, label-free, zero-intervention,
and byte-immutable. Do not invoke a model or provider, use a GPU, execute a
GUI/action/replay, infer claim validity, choose an intervention, implement
runtime Sentinel behavior, or start G1.4+. Store real capsule and collection
data outside the Git repository. Preserve unrelated user changes and record
server findings and completed phases in `mobileworld_audit_handoff/STATUS.md`.
