# G1.6 Solo First-Pass Amendment V1

Contract ID: `mobileworld.g1.gold-history-intervention-solo-first-pass/amendment-v1`

This additive amendment implements D-030 without changing the formal double-blind contract.

## 1. Purpose

`SOLO_FIRST_PASS` lets one real human record complete candidate judgments in the private CPU-only
workspace. It is a precursor research layer, not a review stage in the formal G1.6 protocol.

## 2. Global stage order

The server MUST expose and accept writes in this order:

1. all 190 `ACTION_GOLD` first passes are immutably locked;
2. all 190 `TRANSFORMATION` first passes are immutably locked;
3. all 190 preliminary `CONSISTENCY_AUDIT` first passes are immutably locked.

No later-stage packet, image, preview, draft, or lock may be served before the prior stage is
complete. This protects Action Gold from history visibility and protects both earlier stages from
the natural-action evidence shown only in Consistency.

## 3. Identity and authority

The repo-external solo registry contains exactly one canonical UTF-8 principal and one owner-issued
secret. The same principal may enter the three `*_PRIMARY` user-interface surfaces only because
the workspace manifest and every event identify the tier as `NON_FORMAL_SOLO_FIRST_PASS`.

Every solo event MUST assert:

- `counts_as_independent_review=false`;
- `formal_resolution_eligible=false`;
- `admission_eligible=false`;
- `promotion_allowed=false`;
- `replay_eligible=false`;
- `cross_channel_exposed=true`.

Aliases, duplicate accounts, Secondary roles, Adjudicator roles, formal review submission,
resolution, adjudication, formal export, admission, seal, provider execution, model loading, GPU,
external network, replay, and MobileWorld action execution are forbidden.

## 4. Storage and future formal work

Solo state uses an atomically claimed write-once workspace-mode marker, a distinct assignment key,
a distinct write-once manifest, and `solo-first-pass-events.jsonl`; it MUST NOT share the formal
journal or root. Drafts are append-only snapshots. `SOLO_FIRST_PASS_LOCKED` is immutable and may
only be idempotently reused with identical bytes.

The precursor receipt binds the active G1.3 publication, workspace ID, event count, journal head,
per-stage lock counts, and all false authority guards. It cannot be imported or promoted as a
formal review. Future formal work MUST use a new root, owner registry, assignment key, and blind
reviewer packets; the solo precursor stays hidden until independent review is complete.

## 5. Runtime and schemas

The normative additive schemas are:

- `schemas/g1_6/solo_annotation_workspace.schema.json`;
- `schemas/g1_6/solo_annotation_event.schema.json`.

The existing formal workspace, event, packet, proposal, and frozen G1.1 output schemas retain their
original authority. Reusing their proposal and role-projected packet shapes does not grant formal
authority because the solo manifest, separate journal, event kind, and false guards dominate.

The local service may run inside one owner-authorized detached `tmux` session, but remains a single
process bound only to `127.0.0.1`, with no reloader, proxy headers, worker children, external hosting,
telemetry, remote assets, or other capability expansion.

The owner's browser may reach that server only through an owner-started SSH local forward with the
exact shape `-L 127.0.0.1:8766:127.0.0.1:8766` and `ExitOnForwardFailure=yes`. Reverse or dynamic
forwarding, `GatewayPorts`, wildcard client binds, shared proxies, and third-party tunnels/hosting
remain forbidden.
