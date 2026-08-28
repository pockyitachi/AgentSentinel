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

Status as of 2026-08-28:

| Workstream | Status | Scope |
| --- | --- | --- |
| Runtime audit collector | Implemented and validated for the completed studies | Default-off, passive, fail-open, event-sourced, append-only, and label-free capture of exact application-layer model I/O and `S_t -> I_t -> P_t -> A_t -> R_t -> S_{t+1}` |
| Epic 1: motivation investigation | **Complete** | Six separately reported, integrity-validated 117-task MobileWorld datasets, exact history reconstruction, outcome-blind MHR and local-harm audits, and an outcome-aware observational failure-link review |
| G1.1: causal-replay protocol and registry | **Complete** | CPU-only frozen protocol, schemas, model/config manifest, pre-gold case registry, controls, and locked analysis plan; no treatment response was generated |
| G1.2: portable Sentinel contract | **Complete** | CPU-only canonical History IR/Core, codec/provider interfaces, fail-closed validation, schemas, sidecars, and six-family fixture conformance |
| G1.3: immutable decision capsules | **Complete; v1.1 corrected** | CPU-only formal publication of 190 immutable capsules with zero exclusions (152 strict + 38 selected clean); Amendment 1 adds explicit fail-closed authorization guards; 38 reserve controls remain census-only; no model/provider/GPU/GUI/replay |
| G1.4: exact-request replay runner | **CPU/fake checkpoint and inert live-proof code prepared; live proof deferred** | The validated CPU/fake runner now has additive D-026 no-execution preparation for static bindings, no-send descriptors, caller-injected response projection, inert launch plans, and injected-only GPU assessment; all readiness/authorization fields remain false and the story remains incomplete |
| G1.5: Qwen/MAI History Codecs | **CPU checkpoint implemented; live smoke deferred** | Exact flat-progress/raw-replay extraction, rendering, target-only diff, reversibility, human-bound CPU previews, and a content-bound CPU publication are implemented; no model/provider/GPU path is enabled |
| G1.6: curated gold workspace | **One-person non-formal first-pass checkpoint available; independent review and formal export pending** | The private loopback-only site has an isolated `SOLO_FIRST_PASS` over 190 packets plus three frozen D-031 Action-Gold candidate streams; every suggestion requires an explicit human decision, cannot count as independent review, and cannot be promoted/exported; target-actor provider/GPU/replay paths remain disabled |

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
ALE-322 / G1.4 has a validated CPU/fake implementation checkpoint at commit
`bf099a1a00f38edc33b6c5cbb1ab5d12d53bd18c` and additive D-026 inert live-proof
code preparation at commit `74b18c6bc0f4ce6c56c0e9b979cafec0b5298b6d`.
The additive tranche statically binds frozen model/config declarations, renders
no-send OpenAI call and paired-block descriptors, projects only caller-injected
response envelopes, renders an inert vLLM launch plan, and assesses only an
injected GPU inventory. It creates no client, performs no production network
access, starts no process, probes or uses no GPU, loads no model, and executes no
replay or action. All eight readiness/authorization fields and all nine safety
fields remain false, no formal run publication exists, and the story state is
`IN_PROGRESS_LIVE_PROOF_DEFERRED`. ALE-323 / G1.5 now has a provider-free CPU
History Codec checkpoint, and ALE-324 / G1.6 has a private human-curation
workspace checkpoint. Commit `3f7ccbef542aa37664fe2fb74ab54551ac5d5405`
adds the D-030 one-person `SOLO_FIRST_PASS`: it is a separate, non-promotable
precursor journal and never satisfies independent review. D-031 additionally
freezes three isolated, blind Agent A/B/C candidate streams for the 190
Action-Gold packets. Their 570 outputs and 585 atomic suggestions are untrusted
AI assistance, not evidence or reviews: the sole curator must inspect every item
and explicitly adopt, edit, supplement, or ignore it before separately saving
the still-nonformal solo form. Candidate generation is offline and the website
has no generate/regenerate endpoint. Neither checkpoint is a live proof or a
completed formal gold publication. G1.7 and owner authorization for any live/GPU proof remain
outstanding. Passing CPU or fake-provider tests must
not be reported as ALE-322 completion. The checked-in AGENTS, STATUS, and the
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
The G1.5 CPU codec and G1.6 human-curation boundaries are recorded in
[`G1_5_DECISION_LOG.md`](mobileworld_audit_handoff/G1_5_DECISION_LOG.md),
[`G1_6_DECISION_LOG.md`](mobileworld_audit_handoff/G1_6_DECISION_LOG.md), and
[`G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md`](mobileworld_audit_handoff/G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md).
The additive non-authoritative candidate boundary, frozen prompt, and closed
machine schemas are
[`G1_6_AI_ACTION_CANDIDATE_ASSISTANCE_AMENDMENT_V1.md`](mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_ASSISTANCE_AMENDMENT_V1.md),
[`G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md`](mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md),
and [`schemas/g1_6_ai/`](mobileworld_audit_handoff/schemas/g1_6_ai/).
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
