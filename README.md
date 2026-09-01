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

Status as of 2026-08-31:

| Workstream | Status | Scope |
| --- | --- | --- |
| Runtime audit collector | Implemented and validated for the completed studies | Default-off, passive, fail-open, event-sourced, append-only, and label-free capture of exact application-layer model I/O and `S_t -> I_t -> P_t -> A_t -> R_t -> S_{t+1}` |
| Epic 1: motivation investigation | **Complete** | Six separately reported, integrity-validated 117-task MobileWorld datasets, exact history reconstruction, outcome-blind MHR and local-harm audits, and an outcome-aware observational failure-link review |
| G1.1: causal-replay protocol and registry | **Complete** | CPU-only frozen protocol, schemas, model/config manifest, pre-gold case registry, controls, and locked analysis plan; no treatment response was generated |
| G1.2: portable Sentinel contract | **Complete** | CPU-only canonical History IR/Core, codec/provider interfaces, fail-closed validation, schemas, sidecars, and six-family fixture conformance |
| G1.3: immutable decision capsules | **Complete; v1.1 corrected** | CPU-only formal publication of 190 immutable capsules with zero exclusions (152 strict + 38 selected clean); Amendment 1 adds explicit fail-closed authorization guards; 38 reserve controls remain census-only; no model/provider/GPU/GUI/replay |
| G1.4: exact-request replay runner | **Engineering delivery closed; formal replay deferred** | D-035 accepts the CPU/fake runner and sealed non-formal two-model compatibility smoke as `NONFORMAL_LIVE_SMOKE_PASSED`; formal Provider Codec, serving/isolation, treatment, and replay proof remain `DEFERRED_TO_G1_7_NOT_AUTHORIZED` |
| G1.5: Qwen/MAI History Codecs | **Engineering delivery closed; formal live readiness deferred** | D-036 accepts the content-bound CPU Codecs, five-arm preview/conformance, and non-formal prompt/parser compatibility as `CPU_CODEC_IMPLEMENTATION_COMPLETE_NONFORMAL_COMPATIBILITY_PASSED`; both v1 Codecs remain `live_ready=false`, and formal readiness is transferred to G1.7 |
| G1.6: curated gold workspace | **AI-only 186-unit research labels plus four human solo locks; human/formal curation remains incomplete** | The private loopback-only site retains four owner-authored `SOLO_FIRST_PASS` Action locks. D-033 separately publishes the other 186 units as content-addressed `AI_ONLY_ACTION_LABELS`; those labels are non-human, non-formal, non-promotable, do not advance Transformation, and leave target-actor provider/GPU/replay paths disabled |

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
ALE-322 / G1.4 is closed only under D-035's bounded engineering scope:
`NONFORMAL_LIVE_SMOKE_PASSED` / `DEFERRED_TO_G1_7_NOT_AUTHORIZED`. ALE-323 /
G1.5 is likewise closed only under D-036's bounded engineering scope:
`CPU_CODEC_IMPLEMENTATION_COMPLETE_NONFORMAL_COMPATIBILITY_PASSED` /
`DEFERRED_TO_G1_7_NOT_AUTHORIZED`. Its Qwen flat-progress and MAI raw-replay
Codecs, five-arm preview/conformance surface, content-bound CPU publication,
and fail-closed runner integration are complete. The ten D-035 arm-shaped calls
are non-formal production-prompt/parser compatibility observations only; they
did not execute the formal History Codec-to-Provider Codec path. Both v1 Codecs
remain `live_ready=false`. Formal Provider Codec, complete per-attempt evidence,
serving/backend/session/KV isolation, live admission, and replay authority are
G1.7 duties and remain unauthorized. ALE-324 / G1.6 has a private human-curation
workspace checkpoint. Commit `3f7ccbef542aa37664fe2fb74ab54551ac5d5405`
adds the D-030 one-person `SOLO_FIRST_PASS`: it is a separate, non-promotable
precursor journal and never satisfies independent review. D-031 additionally freezes three
isolated, blind Agent A/B/C candidate streams for the 190 Action-Gold packets. Their 570 outputs
and 585 atomic suggestions are untrusted AI assistance, not evidence or reviews. D-033 then
authorizes three fresh, mutually isolated Codex research agents to decide the remaining 186 units
in exact 62-unit shards and publish a fourth, repo-external `AI_ONLY_ACTION_LABELS` dataset. It
binds—but never copies or rewrites—the four owner-locked journal events. The 186 labels are
explicitly non-human, non-formal, cannot enter either annotation journal, cannot open
Transformation, and cannot complete ALE-324. Candidate generation and AI-only compilation are
offline and the website has no generate/regenerate endpoint. None of these checkpoints is a live
proof or completed formal gold publication. G1.7 and owner authorization for any formal live/GPU
proof remain outstanding. The checked-in AGENTS, STATUS, and the
applicable G1.4/G1.5/G1.6 additive decision logs remain authoritative.

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
[canonical six-model report](<motivation study/misleading_history_audit_report.md>)
for definitions, per-model evidence, and limitations. The safe
[machine-readable public projection](<motivation study/epic1_failure_link_audit_v1/>)
contains the frozen schemas, aggregate metrics, resolution manifests, driver-freeze
provenance, and publication hashes without raw trajectories, reviewer text, or
screenshots. The corrected GUI-Owl v3 result above supersedes the retracted earlier
v2 audit.

## Repository layout

```text
AgentSentinel/
├── MobileWorld/                 # instrumented benchmark/runtime and audit tooling
├── motivation study/            # canonical Epic 1 report and publishable result projection
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
[`DECISION_LOG.md`](mobileworld_audit_handoff/DECISION_LOG.md); current G1.4
authorization is append-only in
[`G1_4_DECISION_LOG.md`](mobileworld_audit_handoff/G1_4_DECISION_LOG.md). The G1.1 causal
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
[`schemas/g1_3/`](mobileworld_audit_handoff/schemas/g1_3/). The G1.4 CPU/fake
runner and additive inert-preparation contracts are
[`G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md`](mobileworld_audit_handoff/G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md)
and
[`G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md`](mobileworld_audit_handoff/G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md);
their nine original plus six additive schemas live under
[`schemas/g1_4/`](mobileworld_audit_handoff/schemas/g1_4/).
The G1.5 CPU Codec, engineering-close amendment, and G1.6 human-curation boundaries are recorded in
[`G1_5_DECISION_LOG.md`](mobileworld_audit_handoff/G1_5_DECISION_LOG.md),
[`G1_5_NONFORMAL_COMPATIBILITY_ENGINEERING_CLOSE_AMENDMENT_V1.md`](mobileworld_audit_handoff/G1_5_NONFORMAL_COMPATIBILITY_ENGINEERING_CLOSE_AMENDMENT_V1.md),
[`G1_6_DECISION_LOG.md`](mobileworld_audit_handoff/G1_6_DECISION_LOG.md), and
[`G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md`](mobileworld_audit_handoff/G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md).
The additive non-authoritative candidate boundary, frozen prompt, and closed
machine schemas are
[`G1_6_AI_ACTION_CANDIDATE_ASSISTANCE_AMENDMENT_V1.md`](mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_ASSISTANCE_AMENDMENT_V1.md),
[`G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md`](mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md),
and [`schemas/g1_6_ai/`](mobileworld_audit_handoff/schemas/g1_6_ai/).
The separate D-033 non-human publication boundary and its four schemas are
[`G1_6_AI_ONLY_ACTION_LABELS_AMENDMENT_V1.md`](mobileworld_audit_handoff/G1_6_AI_ONLY_ACTION_LABELS_AMENDMENT_V1.md)
and [`schemas/g1_6_ai_only/`](mobileworld_audit_handoff/schemas/g1_6_ai_only/).
The owner-only local website procedure is
[`G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md`](mobileworld_audit_handoff/G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md).

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
