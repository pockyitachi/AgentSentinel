# AgentSentinel

AgentSentinel is a research monorepo for studying whether pre-steps injected
into a GUI agent's next prompt can become invalid, noisy, or misleading. The
current engineering phase is deliberately narrower than the eventual online
Sentinel: it builds a lossless, label-free runtime collector for MobileWorld,
then evaluates collected runs with a separate versioned offline pipeline.

## Repository layout

```text
AgentSentinel/
├── MobileWorld/                 # vendored upstream snapshot; implementation target
├── mobileworld_audit_handoff/   # authoritative design and server instructions
├── seed_baseline_audit/         # prior observational audit artifacts
├── sentinel_mvp/                # early prototype; not the current implementation target
└── proposal-*.md                # proposal and presentation materials
```

## Current task

Before modifying code, read in order:

1. [`mobileworld_audit_handoff/AGENTS.md`](mobileworld_audit_handoff/AGENTS.md)
2. [`mobileworld_audit_handoff/SERVER_AGENT_INSTRUCTIONS.md`](mobileworld_audit_handoff/SERVER_AGENT_INSTRUCTIONS.md)
3. [`mobileworld_audit_handoff/STATUS.md`](mobileworld_audit_handoff/STATUS.md)

The immediate deliverable is an event-sourced, lossless, label-free collector.
It must not classify history, generate rubrics, filter prompts, or implement the
full Sentinel middleware.

## Clone on the server

The MobileWorld snapshot contains three upstream resource submodules, so clone
recursively:

```bash
git clone --recurse-submodules https://github.com/pockyitachi/AgentSentinel.git
cd AgentSentinel/MobileWorld
```

Write runtime audit data outside the repository, for example under
`/shared/linqiang/mobileworld_audit_data/`.

## MobileWorld provenance

`MobileWorld/` was imported from
[`Tongyi-MAI/MobileWorld`](https://github.com/Tongyi-MAI/MobileWorld) at commit
`0dcd0980eac64d76f498f93568a1ec0594b743c4`. It is tracked as source inside this
monorepo so collector changes can be committed here without modifying or
pushing to the upstream repository. See [`UPSTREAM.md`](UPSTREAM.md).

