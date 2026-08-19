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

Current scope: implement and validate the event-sourced, lossless, label-free,
zero-intervention collector. Do not implement online Sentinel, runtime labels,
rubrics, or prompt filtering during this phase. Store real collection data
outside the Git repository. Preserve unrelated user changes and record server
findings and completed phases in `mobileworld_audit_handoff/STATUS.md`.

