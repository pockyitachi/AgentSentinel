# AgentSentinel

AgentSentinel studies runtime integrity for the task-local execution histories
that GUI agents feed back into later model calls. A previous reasoning trace,
action conclusion, progress summary, or folded memory can be false when
written, become stale, or remain factually true while belonging to an inactive
task branch. Later decisions may still treat that record as a premise even when
the current GUI or execution evidence contradicts it.

The long-term design is a model-agnostic gate at the model-call boundary. It
would validate claim-level host-native history against time-aligned GUI and
execution evidence, assess task-path relevance through an independent
versioned multi-path rubric, and reconstruct a protocol-valid active-history
view immediately before the unchanged actor is called. The automatic online
gate and rubric tracker are proposed work; they are not implemented results.

## Project status

Status as of 2026-08-27:

| Workstream | Status | Scope |
| --- | --- | --- |
| Runtime audit collector | Implemented and validated for the completed studies | Default-off, passive, fail-open, event-sourced, append-only, and label-free capture of exact application-layer model I/O and `S_t -> I_t -> P_t -> A_t -> R_t -> S_{t+1}` |
| Epic 1: motivation investigation | **Complete** | Six separately reported, integrity-validated 117-task MobileWorld datasets, exact history reconstruction, outcome-blind MHR and local-harm audits, and an outcome-aware observational failure-link review |
| G1.1: causal-replay protocol and registry | **Complete** | CPU-only frozen protocol, schemas, model/config manifest, pre-gold case registry, controls, and locked analysis plan; no treatment response was generated |
| G1.2: portable Sentinel contract | **Complete** | CPU-only canonical History IR/Core, codec/provider interfaces, fail-closed validation, schemas, sidecars, and six-family fixture conformance |
| G1.3: immutable decision capsules | **Complete; v1.1 corrected** | CPU-only formal publication of 190 immutable capsules with zero exclusions (152 strict + 38 selected clean); Amendment 1 adds explicit fail-closed authorization guards; 38 reserve controls remain census-only; no model/provider/GPU/GUI/replay |
| G1.4: exact-request replay runner | **CPU-only build authorized; live proof deferred** | Runner, invariant/diff guards, fake-provider conformance, scheduling, idempotent attempt storage, blinded export, schemas, and CLI may be built without network/model/GPU/GUI/action execution; story remains incomplete pending separately authorized live/GPU proof |

G1.2 is merged and accepted. ALE-321 / G1.3 formally froze and published all
190 targets—152 strict-MHR candidates plus 38 selected clean controls—as
deterministic, self-validating capsules with zero exclusions, without changing
Collector v1 or reconstructing treatment requests. The separate 38 reserve
clean controls remain census-only and out of capsule/exclusion scope. G1.3 was
offline and CPU-only: no model/provider invocation, GPU, GUI/action/replay,
automatic truth inference, intervention choice, or online Sentinel occurred.
Contract Amendment 1 published the corrected v1.1 artifact at manifest SHA
`8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402`.
The former v1 publication remains immutable and historically identifiable, but
is superseded for formal G1 use.
ALE-322 / G1.4 is now active only for its CPU implementation tranche. Real
model/provider/network calls, GPU use, GUI/action/live replay, treatment-response
generation, and G1.5+ remain unauthorized; formal capsules retain all three
false safety guards. Passing CPU and fake-provider tests must not be reported as
ALE-322 completion. The checked-in AGENTS, STATUS, and DECISION_LOG remain
authoritative.

## Epic 1 results

Epic 1 evaluated six host-native history representations on the same canonical
MobileWorld GUI-only task set: 117 tasks per model and 702 model-task cases in
total. A strict **misleading-history reuse (MHR)** case requires a false or
stale model-generated history record to be present in a later actor request and
explicitly reused by the later decision. We separately report the subset of
MHR cases with observed local harm; local harm is an attribute of the reviewed
reuse chain, not a second history event.

| Model | History representation | MHR cases | MHR cases with observed local harm |
| --- | --- | ---: | ---: |
| MAI-UI-8B | Raw replay | 7/117 (5.98%) | 7/117 (5.98%) |
| Qwen3-VL-8B | Flat task progress | 35/117 (29.91%) | 32/117 (27.35%) |
| GELab-Zero-4B | Rolling summary | 33/117 (28.21%) | 29/117 (24.79%) |
| UI-Venus-1.5-8B | Flat previous actions | 3/117 (2.56%) | 1/117 (0.85%) |
| GUI-Owl-1.5-8B-Instruct | Hybrid-collapsed action history | 11/117 (9.40%) | 7/117 (5.98%) |
| MemGUI-8B-SFT | Structured H/L/M folding | 27/117 (23.08%) | 18/117 (15.38%) |
| **Total** | Six representation families | **116/702 (16.52%)** | **94/702 (13.39%)** |

Across the 702 cases, 128 succeeded and 574 failed. The audits identified 116
strict-MHR cases containing 272 reuse chains; 94 cases and 239 chains had
observed local harm. Among the 108 failed cases containing MHR, a separate
outcome-aware review found 10 explicit final-decision stops and 48 earlier
unrecovered derailments with a traceable connection to final failure: 58/108
failed MHR cases and 58/574 failures overall.

These are observational associations, not counterfactual effects. Every
dataset remains `causal_claim_supported=false`; the results do not establish a
cross-model ranking, prove that MHR alone caused a failure, or estimate how much
success would improve after deleting or correcting history. See the
[canonical six-model report](MobileWorld/docs/misleading_history_audit_report.md)
for definitions, per-model evidence, and limitations. The corrected GUI-Owl v3
result above supersedes the retracted earlier v2 audit.

## Repository layout

```text
AgentSentinel/
├── MobileWorld/                 # instrumented benchmark/runtime and audit tooling
├── mobileworld_audit_handoff/   # authoritative contracts, decisions, status, and G1 protocol
├── seed_baseline_audit/         # historical preliminary Seed investigation
├── sentinel_mvp/                # legacy single-host behavioral reference
└── proposal-*.md                # long-term method proposal and presentation material
```

The portable G1 contract is implemented under
`MobileWorld/src/mobile_world/offline/causal_replay/`, independently of
`sentinel_mvp`; that directory remains only a legacy reference. G1.3 adds a
separate offline capsule-materialization layer and does not modify the accepted
G1.2 contract.

## Working in this repository

Before changing implementation code, follow
[`mobileworld_audit_handoff/AGENTS.md`](mobileworld_audit_handoff/AGENTS.md).
The current authoritative state and locked decisions are in
[`STATUS.md`](mobileworld_audit_handoff/STATUS.md) and
[`DECISION_LOG.md`](mobileworld_audit_handoff/DECISION_LOG.md). The G1.1 causal
protocol and locked analysis plan are
[`G1_CAUSAL_REPLAY_PROTOCOL_V1.md`](mobileworld_audit_handoff/G1_CAUSAL_REPLAY_PROTOCOL_V1.md)
and
[`G1_LOCKED_ANALYSIS_PLAN_V1.md`](mobileworld_audit_handoff/G1_LOCKED_ANALYSIS_PLAN_V1.md).
The accepted portable contract and the active capsule contract are
[`G1_PORTABLE_SENTINEL_CONTRACT_V1.md`](mobileworld_audit_handoff/G1_PORTABLE_SENTINEL_CONTRACT_V1.md)
and
[`G1_REPLAY_CAPSULE_CONTRACT_V1.md`](mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1.md);
its active correction is
[`G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md`](mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md),
and the historical plus active G1.3 machine schemas live under
[`schemas/g1_3/`](mobileworld_audit_handoff/schemas/g1_3/).

Raw collections, derived audit artifacts, screenshots, and replay outputs must
remain outside the Git repository in restricted, versioned data roots. Git
contains code, schemas, reports, manifests, hashes, and non-secret references.

## Clone on a server

The MobileWorld snapshot contains three upstream resource submodules, so clone
recursively:

```bash
git clone --recurse-submodules https://github.com/pockyitachi/AgentSentinel.git
cd AgentSentinel/MobileWorld
```

## MobileWorld provenance

`MobileWorld/` was imported from
[`Tongyi-MAI/MobileWorld`](https://github.com/Tongyi-MAI/MobileWorld) at commit
`0dcd0980eac64d76f498f93568a1ec0594b743c4`. It is tracked as source in this
monorepo so AgentSentinel instrumentation and evaluation tooling can be
versioned without modifying the upstream repository. See
[`UPSTREAM.md`](UPSTREAM.md).
