# Instructions for AgentSentinel coding agents

Last synchronized with the owner-facing repository state: **2026-09-05 UTC**.
Linear workflow state is owner-managed and is not changed by repository agents.

This directory preserves the scientific contracts and provenance for the
MobileWorld history-integrity project. The implementation tree is
`../MobileWorld/`.

## 1. Current authority

The current mainline is **Runtime Sentinel MVP and MobileWorld Validation**
(Linear ALE-318). The owner explicitly moved the former formal causal-replay
pipeline out of the critical path on 2026-09-01.

This file and the repository-root `AGENTS.md` supersede older active-stage
language that identifies ALE-324 as G1.6 human curation or prohibits every
runtime Sentinel implementation. Historical contracts, receipts, hashes,
limitations, and frozen-artifact rules remain facts; they no longer decide the
current story sequence where they conflict with this pivot.

Never merge or cherry-pick unmerged D-037 commit `478740c` from
`codex/ale-324-g16-ai-curation-amendment`. It describes the superseded
AI-replicate curation plan.

The pivot authorizes only the next CPU/fake runtime engineering tranche. It
does not authorize a real target model or provider/client, external network,
GPU, model loading/serving, MobileWorld GUI/tool/action, backend restore,
prefix/live replay, or treatment generation. All prior smoke authority is
consumed.

## 2. Current project ledger

### Completed motivation evidence

- Epic 1 / ALE-298 is complete for six representative history families:
  MAI-UI raw replay, Qwen flat progress, GELab rolling summary, UI-Venus flat
  previous actions, GUI-Owl hybrid-collapsed history, and MemGUI structured
  H/L/M folding.
- Each model used the canonical 117-task GUI-only suite: 702 model-task cases.
- The final audit reports 116 strict-MHR cases and 94 cases with observed local
  harm. Failure-link analysis remains observational; every dataset has
  `causal_claim_supported=false`.
- Canonical outputs are under `../motivation study/`.

### Accepted engineering foundation

- Collector v1 is implemented, passive, default-off, fail-open, append-only,
  event-sourced, label-free, and lossless for observable application-layer
  model I/O and `S_t -> I_t -> P_t -> A_t -> R_t -> S_{t+1}`.
- ALE-319 / G1.1 is complete: frozen causal protocol and case registry.
- ALE-320 / G1.2 is complete: portable History IR/Core, exact-span binding,
  deterministic renderer, reversible mapping, invariant validator, sidecar,
  provider interfaces, and six-family fixture conformance.
- ALE-321 / G1.3 is complete: active v1.1 immutable publication with 190
  capsules and 0 exclusions; 38 reserve controls remain census-only.
- ALE-322 / G1.4 is closed only under D-035's engineering scope:
  `NONFORMAL_LIVE_SMOKE_PASSED` / `DEFERRED_TO_G1_7_NOT_AUTHORIZED`.
- ALE-323 / G1.5 is closed only under D-036's engineering scope:
  `CPU_CODEC_IMPLEMENTATION_COMPLETE_NONFORMAL_COMPATIBILITY_PASSED` /
  `DEFERRED_TO_G1_7_NOT_AUTHORIZED`. Both v1 Codecs remain
  `live_ready=false`.

### Historical incomplete work

The former G1.6 checkpoint contains four owner-authored non-formal solo Action
locks and a separate publication of 186 D-033 AI-only Action labels. They are
not independent human review, gold, admission, or a formal seal. The annotation
site is stopped. Preserve the artifacts, but do not restart the site or treat
them as a prerequisite without a new owner decision.

Old G1.6–G1.11 plans are **superseded/deferred**, not deleted and not complete.
The 190-unit/five-arm/1,140-review/adjudication/seal/replay path may only be
revived as a separate causal-evaluation effort after an explicit owner decision.

### Active runtime work

Runtime Epic 2 has 4/6 repository engineering stories accepted. ALE-324 / R2.1
has an accepted CPU/fake checkpoint at commit
`18a4d9e6c8a3ed4ddac7cab5392a3335bae45b46`. ALE-325 / R2.2 has an accepted
CPU/offline/injected-fake-Responses, SHADOW-only checkpoint at commit
`3940ff7484c0236ac321ea210fc8266e282e5d27`. ALE-326 / R2.3 has an accepted
CPU/offline/injected-fake, SHADOW-only checkpoint at commit
`54381b7b56b06d5aa262005af62b65269b4cf0a6`, accepted through mainline merge
`2aa0a268b7d709cf05d524e74c3fba8612f64003`. ALE-327 / R2.4 and ALE-328 /
R2.5 have an initial joint CPU-only/offline/injected-fake repository-preparation
implementation at `344e1c42596a4dca717da66374eeca3d936c3f61`; owner review classified it
**NO-GO**. The current R2.4 implementation/source checkpoint is
`2f5eae0dba43908a44017bf8f3ab6916b56db095`. Its six-case action-free live
smoke and all six official Collector integrity checks passed, and the owner has
accepted the bounded R2.4 runtime vertical slice. The exact authority used by
that run is consumed; it grants no rerun or pilot permission. Runtime Epic 2 is
therefore 4/6 accepted; R2.5 remains a separately authorized next stage.
R2.1
supersedes the `58820a8` checkpoint after recursively trusted policy-output and
History-IR snapshots,
precomputed renderer-result binding, worker-owned policy-evaluation census, and
detached receipt-transaction hardening. Linear workflow state remains owner-
managed. The repository dependency sequence is:

```text
R2.1 / ALE-324
    -> { R2.2 / ALE-325 evidence-grounded policy
       , R2.3 / ALE-326 multi-path rubric }
    -> R2.4 / ALE-327 Qwen + MAI vertical slices
    -> R2.5 / ALE-328 real 20–30 task MobileWorld pilot
    -> R2.6 / ALE-329 conditional six-family and GUI-117 expansion
```

## 3. Method boundary

The runtime system operates after the host has assembled the exact actor
request and immediately before the provider/model invocation:

```text
host-native request
    -> pre-call Sentinel seam
    -> representation-specific History Codec
    -> evidence-grounded validity policy
    -> independent rubric/path-relevance component
    -> history-only validation and rendering
    -> unchanged actor provider/parser/action path
```

Sentinel's product is a governed, protocol-valid active-history view. Sentinel
must never choose, parse, execute, or recommend the GUI action.

The two semantic axes remain independent:

1. **History validity:** use only causally available GUI/execution evidence to
   support `KEEP`, `DROP`, `REPLACE`, or `KEEP_UNCERTAIN`.
2. **Task-path relevance:** use a versioned, instruction-grounded multi-path
   AND-OR rubric to describe active/inactive/unknown paths. The rubric is not
   truth evidence and does not choose an action. `ARCHIVE` begins SHADOW-only.

## 4. Accepted scope: R2.1 CPU/fake seam

R2.1 created a small additive runtime decision/contract without editing
byte-frozen G1 contracts to manufacture runtime authorization.

The accepted shared interface operates after request assembly and before
transport retry:

```text
PromptSentinel.before_model_call(request, context, history_codec_id, call_role)
    -> SentinelResult
```

Required behavior:

- default mode `OFF`, plus explicit `SHADOW` and `ACTIVE`;
- global kill switch and per-host configuration;
- monotonic kill-switch activation detection, including an activation pulse
  that returns to inactive before candidate selection;
- one Sentinel evaluation per logical actor decision, identified by a stable
  logical-call ID, with the same validated result reused across BaseAgent
  transport retries, adapter-level parse retries (including Qwen's outer
  parse-retry loop), and the streaming path;
- `call_role=sentinel` bypass to prevent recursion;
- immutable raw request and a separately constructed final request;
- strict finite canonical-JSON admission before copying, hashing, cache lookup,
  or sidecar admission; serializer-coercible non-JSON values pass through the
  fixed-message BaseAgent backstop as the exact Original with no semantic work
  or receipt;
- exact history-only edits with system/task/tools/current observation/images,
  roles/order, model/sampling parameters, and all non-history content preserved;
- typed fallback to Original on timeout, exception, invalid output, unsupported
  family, ambiguous/overlapping span, render failure, or invariant failure;
- raw/candidate/final request hashes, canonical policy-output and exact-diff
  hashes, codec/mode, decision kinds, validation, fallback, and latency in a
  derived repo-external sidecar; R2.1 persists no request views, operation
  payloads, or exact-diff preimages, and any future detail channel requires a
  separate access-controlled versioned contract with no secret or
  chain-of-thought logging;
- canonical hashing of every correctly typed policy output before duplicate-ID,
  operation-binding, or later admission rejection; hashing never grants
  admission;
- sidecar transaction admission before SHADOW/ACTIVE semantic work, with
  admission failure before policy and commit failure before provider transport;
- existing provider transport, response normalization, action parser, runner,
  and action execution remain unchanged;
- deterministic no-op/fake policy and fake-provider tests only in R2.1.

R2.1 executes only deterministic `DROP`. Every `REPLACE` plan and renderer list
insertion fails closed to Original. R2.2 adds the separate versioned runtime
proposal/admission overlay below instead of representing automatic proposals
as frozen G1.2 curated plans.

`OFF` and `SHADOW` must send byte/semantic-equivalent Original requests.
`ACTIVE` may send a transformed request only inside deterministic CPU/fake
tests until a later live authorization exists.

## 4A. Accepted scope: R2.2 CPU/offline/fake SHADOW policy

R2.2 adds a separate automatic runtime proposal/admission overlay without
changing the frozen G1.2 curated-plan provenance or the accepted R2.1 v1
receipt. The owner-accepted repository implementation is commit
`3940ff7484c0236ac321ea210fc8266e282e5d27`.

Its module-owned evidence builder binds one exact actor request, logical call,
host/Codec identity, complete task instruction, current screenshot, causal
cutoff, role-projected evidence, and exact History IR targets. The injected
fake Responses transport returns a closed proposal; independent deterministic
admission supports `KEEP`, `DROP`, and `KEEP_UNCERTAIN`. Transition status is
weak evidence, and invalidation must follow the target plus every cited
support. `REPLACE` is schema-reserved but fails closed before admission and
rendering until a typed/template fact representation exists. Admitted DROP
operations are source-bound, history-only, reversible, and SHADOW-only.

R2.2 emits a separate hash-only policy receipt and best-effort low-cardinality
metrics. It reuses the R2.1 logical-call cache, recursion bypass, deadline, and
Original fallback. Qwen and MAI captured fixtures cover the admitted decisions
and REPLACE rejection, but neither frozen G1.5 Codec becomes `live_ready=true`.

The accepted checkpoint used no live model/provider, network, GPU, backend, GUI,
tool, action, replay, or external data source. The checked-in OpenAI adapter is
inert without explicit owner authorization. R2.4 must still add real evidence
plumbing, runtime target discovery, a cancellable live transport/attempt
receipt, secret/resource authority, and a versioned zero-target bridge.

## 4B. Accepted scope: R2.3 CPU/offline/fake SHADOW rubric

R2.3 adds the independent rubric/path-relevance axis without treating task
relevance as factual validity or action advice. Commit
`54381b7b56b06d5aa262005af62b65269b4cf0a6`, accepted through mainline merge
`2aa0a268b7d709cf05d524e74c3fba8612f64003`, provides exact Unicode instruction
spans, an acyclic versioned `AND` / `OR` graph with legal alternatives and
`OTHER_UNKNOWN`, generate-once caching, and explicit hash-bound revision.

The tracker consumes a history-free packet bound to the task, causal cutoff,
one current-observation event, prior completed transitions, and prior rubric
state. It excludes actor history, request/model fields, History IR, and future
outcomes. Generic transition status, post-UI change, and free-form tool results
are weak; conflicting or insufficient evidence yields `unknown`. The runtime,
not the fake backend, uses one memoized shared-DAG analysis to derive path
viability and alternative-aware frontier, and independently recomputes them in
state validation. Backend inputs and rubric/revision/proposal outputs are
detached trusted snapshots before dispatch, hashing, or storage: private
pre-call authority graphs and hashes remain internal, while backends and every
public result/cache hit receive fresh detached copies. The common graph walker
fails closed on cycles, depth beyond 64, or more than 262,144 total visits,
including primitive leaves.
Semantic graph validation and path/frontier derivation use explicit stacks and
children-first dynamic programming through all 512 gates, independent of the
Python recursion limit and gate declaration order.

This checkpoint has no trusted record-level R2.2 `SUPPORTED + KEEP` resolver.
Support hashes are input-bound but have no archive authority; every relevance
result is `RETAIN`, and `ARCHIVE_SHADOW` remains schema-reserved. The accepted
R2.4 vertical slice did not install that resolver.
Receipts are hash-only, include measured backend-failure latency, call census is
cumulative, cache reuse is measured, and calibration labels are accepted only
as explicit frozen offline inputs. Qwen and MAI coverage uses captured CPU
fixtures and no host/model-specific runtime branch. This checkpoint is
owner-accepted within that bounded repository scope; Linear remains
owner-managed.
R2.4 is owner-accepted. R2.5 is mechanically next, but every new live/model/
provider, network, GPU, backend, GUI/tool/action, replay, and resource operation
still requires separate owner authority.

The shared candidate hook is
`../MobileWorld/src/mobile_world/agents/base.py::BaseAgent.openai_chat_completions_create`.
Confirm actual control flow, especially streaming and retry behavior, before
editing. First-host prompt builders are under:

- `../MobileWorld/src/mobile_world/agents/implementations/qwen3vl.py`
- `../MobileWorld/src/mobile_world/agents/implementations/mai_ui_agent.py`

## 4C. Accepted R2.4 scope and candidate R2.5 repository preparation

The initial implementation commit
`344e1c42596a4dca717da66374eeca3d936c3f61` prepared the two-host runtime
overlay, same-cutoff Collector evidence, independent history-free rubric and
history-policy orchestration, typed no-history behavior, sealed
authority/preflight/resource/attempt/audit/cleanup contracts, CPU topology
evidence, and tooling/protocol for a future 20-task / 80-cell pilot under CPU
fixtures and injected fakes. Owner review classified it **NO-GO**. The current
remediation and live-smoke source checkpoint
`2f5eae0dba43908a44017bf8f3ab6916b56db095` is owner-accepted for R2.4. It
retains the earlier strict SDK/HTTP
logging suppression, type-sensitive canonical-byte sealed-schema comparison,
module-owned request anchors, bounded cleanup, and fatal-dispatch repairs. The
reviewed repository branch is authorized for PR/merge. The checkpoint itself
grants no live execution; the successful smoke below used a separate exact
authority that is now consumed. It is not an R2.5 pilot result.

Every formed `RUBRIC` and `HISTORY_POLICY` attempt now retains the complete
authority, deadline/constraint, case lease, exact two-stage set, pricing,
transport, and canonical provider-request preimages. Durable proof validation
requires nine caller-known roots for authority, constraint, manifest,
preflight, case lease, stage, pricing, transport, and request. A received
`HISTORY_POLICY` provider response remains proof-bound if R2.2 receipt preparation,
publication, or deadline handling fails; the receipt is explicitly absent
post-dispatch and the policy result is not admitted. Production-audit `begin`
failures retain a module-sealed full pre-provider recovery receipt across root,
destination, temporary-file, write, file-fsync, directory-fsync, sink-begin,
and transaction-binding faults. Unit-evidence journals larger than 4 MiB use
an owner-only content-addressed blob with a compact bound reference; blob
publication/readback faults preserve full outer failure evidence while cleanup
and audit finalization continue. Post-dispatch `UNKNOWN` cost and
`TERMINATION_UNCONFIRMED` trip distinct one-way run-fatal reasons before actor
or later dispatch.

If deadline or cancellation handling races with an already-arrived valid
`COMPLETED` Responses envelope, `FAILED`, `CANCELLED_POST_DISPATCH`, and
`TERMINATION_UNCONFIRMED` attempts retain its detached hash, requested/returned
model, and, when provider usage is present, full usage including cached input
plus exact recomputed cost. Missing usage remains `UNKNOWN` and trips the
existing run-fatal rule. The late output remains unavailable to policy
admission, including when its known exact usage or cost exceeds authority. A
clean natural child exit can terminalize as a cooperative post-dispatch
cancellation. Durable rubric-proof validation now requires the complete
matching terminal attempt receipt rather than accepting a proof-local terminal
hash alone.
Cancellation signaling, deadline adjudication, and successful `COMPLETED`
publication are linearized by the same finalization lock. If cancellation or
deadline wins before terminal publication, an arrived response is retained
only as late evidence and cannot be returned, passed, or admitted. Durable
history-policy validation rejects every non-`COMPLETED` attempt paired with an
`ADMITTED` R2.2 receipt or admitted plan. Its only committed-receipt exception
is a strict pre-response `POLICY_TRANSPORT_ERROR` receipt with one dispatched
transport call, no returned-model/response/output/proposal/plan/usage fields,
and a zero semantic decision census.

Cleanup teardown cannot initialize or reinitialize a backend. If
client/backend `/init` was not locally confirmed, cleanup records the
hash-bound `NOT_INITIALIZED_NO_IO` outcome without a dispatch callback,
resource re-attestation, or backend request. Local closure and any already-
created audit finalization still run.

The additive `R24_LIVE_SMOKE_ONLY` authority, preflight, executor, and CLI bind
only this fixed lifecycle:

```text
single shared-GPU lease + backend + Qwen start
  -> Qwen OFF / SHADOW / ACTIVE
  -> stop/reap Qwen, prove port clear, re-attest GPU and MAI snapshot
  -> start MAI
  -> MAI OFF / SHADOW / ACTIVE
  -> bounded cleanup
```

`SINGLE_GPU_SEQUENTIAL_SHARED` requires the same GPU index for both actors,
vLLM `gpu_memory_utilization=0.24`, and at least 51,200 MiB free at each
admission boundary. GPU process evidence includes PID, start time, process
group, session, UID, user, and memory. One project lease protects the complete
sequence; pre-existing co-tenants may remain, but new or identity-drifting
tenants fail closed and cleanup reaps only project-owned processes. The shared
handoff crosses no actor overlap: the next actor starts only after the prior
service/session/processes are gone and GPU plus immutable snapshot have been
re-attested.

The smoke-only manifest has no pilot, cohort, task-source, score, cell, or GUI-
action authority. Its executor and CLI do not import or invoke R2.5, and pilot
methods reject smoke scope before snapshot reads, reset, or dispatch. Secure
authority/input loading binds owner-only regular files and their path/inode
identity; the parent performs secret metadata/path checks with zero secret
reads. Every physical dispatch and terminal result has durable audit proof,
with full recovery projections and owner-only CAS for oversized journals. The
shared cleanup bound is independently recomputed as
`5 * shutdown_grace_seconds + 3 * ceil(health_poll_interval_ms / 1000) + 225`;
the default grace 10 seconds and poll 250 ms yield exactly 278 seconds, and a
smaller reserve fails before resource I/O. The legacy independent two-GPU
concurrent full-run path remains unchanged and is not reachable
through the smoke-only entrypoint.

The earlier transport-success run `r24-smoke-gpu5-20260905t115136z` remains
immutable and excluded because its six Collector raw runs all failed the
official integrity checker: the parser-only smoke task was marked `completed`
with no score. Commit `2f5eae0dba43908a44017bf8f3ab6916b56db095`
uses the Collector-v1 legal split instead: a successful no-action smoke task is
`aborted` with `score=null`, while its successfully finalized one-task run is
`completed`; failed smoke remains `crashed` and scored pilot behavior is
unchanged. No score is fabricated.

At that commit, R2.4 passed 646/646 and the complete CPU/offline MobileWorld
suite passed 2438/2438 in 625.79 seconds. Independent production-driver plus
audit-integrity coverage passed 150/150; Ruff check/format, configured source
mypy, Git diff checks, and red-team review passed. Exact owner manifest
`395c89f304d77823a2172ab5175661d30897fc31615f01f1dbc7ab8537d13bfb`
and preflight
`7974cf03759132e48e3507c11e49d08aeccc918c2dd2fa56a9ec854ac0fb765a`
authorized fresh run `r24-smoke-gpu5-integrity-retry-20260905t181000z`.
It completed Qwen then MAI `OFF / SHADOW / ACTIVE`: 6 parsed actor calls, 12
completed Sentinel OpenAI calls, 0 actions, exact cost `$0.173954`, and bounded
cleanup. All six new raw Collector roots pass
`mobileworld.audit.integrity/v1` with `valid=true`, `errors=[]`, and
`warnings=[]`. ACTIVE was an admitted `KEEP_UNCERTAIN` no-op, so this establishes
transport/parser/isolation/safety evidence only, not a material edit,
effectiveness, or causal effect.

The used authority is consumed and grants no further execution. No persistent
117-row executable source, selected cohort, frozen pilot manifest, or pilot
artifact was created. The frozen G1.5 Codecs remain `live_ready=false`; R2.3
and R2.4 are accepted, Runtime Epic 2 is 4/6 accepted, and R2.5 remains
separately unauthorized.

## 5. Reusable implementation assets

- Collector: `../MobileWorld/src/mobile_world/runtime/audit/`
- Portable IR/Core/validator/sidecar:
  `../MobileWorld/src/mobile_world/offline/causal_replay/`
- Exact-request fake runner/provider/invariance tests:
  `../MobileWorld/src/mobile_world/offline/causal_replay_runner/`
- Qwen/MAI extraction, rendering, diff, preview:
  `../MobileWorld/src/mobile_world/offline/g1_history_codecs/`
- R2.2 evidence, policy, admission, renderer, receipt, and metrics:
  `../MobileWorld/src/mobile_world/runtime/sentinel/r2_2/`
- R2.3 rubric plus R2.4/R2.5 CPU preparation, authority, audit, and analysis:
  `../MobileWorld/src/mobile_world/runtime/sentinel/r2_3/`,
  `../MobileWorld/src/mobile_world/runtime/sentinel/r2_4/`, and
  `../MobileWorld/src/mobile_world/runtime/sentinel/r2_5/`
- Relevant tests:
  `../MobileWorld/tests/runtime/audit/`,
  `../MobileWorld/tests/runtime/sentinel/test_r2_2_policy.py`,
  `../MobileWorld/tests/runtime/sentinel/test_r2_3_rubric.py`,
  `../MobileWorld/tests/runtime/sentinel/test_r2_4_*.py`,
  `../MobileWorld/tests/runtime/sentinel/test_r2_5_*.py`,
  `../MobileWorld/tests/offline/test_portable_causal_replay_contract.py`,
  `../MobileWorld/tests/offline/test_causal_replay_runner.py`, and
  `../MobileWorld/tests/offline/test_g1_history_codecs.py`.

Reuse these semantics, but keep accepted/frozen publications and hashes
unchanged. Do not import the offline causal runner into the live actor path as
a hidden planner. Create a narrow runtime layer and explicit capability
overlays. Core/policy logic may not switch behavior based on target model name;
history-family differences belong in registered Codecs.

## 6. Mandatory safety invariants

- Collector v1 raw events remain label-free and byte-immutable.
- Never write `wrong`, `misleading`, rubric, `KEEP`, `DROP`, `REPLACE`, or
  `ARCHIVE` semantics back into raw events.
- Do not mutate model responses, parsed actions, tool results, environment
  state, or retry semantics.
- Do not add a second actor call, screenshot request, or environment action.
- Preserve multimodal order, tool-call/result adjacency, provider envelope,
  host parser behavior, and all non-history request fields.
- Never treat HTTP success, screenshot change, executor success, or task
  failure as automatic proof of semantic success/failure.
- Unknown or unverifiable evidence must retain history and abstain/fallback.
- Current-screen absence alone does not refute a historical event; future
  `S_{t+1}`, outcomes, checkers, or later trajectory data are unavailable at
  call `t`.
- Keep raw and final request views, decisions, evidence references, exact
  reversible mappings, and fallback state auditable.
- No API key, Authorization header, cookie, access secret, or full environment
  dump may enter Git or audit logs.
- Do not reset, delete, overwrite, or reformat unrelated user changes.

## 7. Authorization boundary

Currently permitted:

- documentation and additive R2.1/R2.2/R2.3 runtime-contract work;
- CPU-only implementation and tests for OFF/SHADOW/ACTIVE seam mechanics;
- deterministic fake/no-op R2.1 policy and the checked-in R2.2 automatic
  policy under injected fake Responses transport in SHADOW only;
- the checked-in R2.3 rubric/tracker under injected fake backends in SHADOW
  only, including derived relevance receipts; `ARCHIVE_SHADOW` remains
  schema-reserved and all current dispositions are `RETAIN`;
- the checked-in R2.4/R2.5 contracts, CPU orchestration, topology evidence,
  sealed production doubles, audit/analysis, and authority-building mechanics
  under CPU fixtures and injected fakes only;
- the additive shared-single-GPU smoke-only authority, preflight, executor,
  lifecycle, and CLI mechanics under CPU fixtures and injected fakes only;
- existing in-process fake provider and secret-free fixtures;
- read-only inspection and hash validation of historical artifacts.

Not currently permitted:

- live/model-backed R2.2 policy calls, ACTIVE R2.2 transformation, or an
  unbound correction generator;
- live/model-backed rubric generation/tracking or active `ARCHIVE` (R2.3);
- promotion or use of an `OWNER_AUTHORIZED` R2.4/R2.5 run manifest without a
  new explicit owner approval of its exact digest;
- any new six-case R2.4 smoke or any R2.5 backend reset, model call, GUI
  action, score collection, or 80-cell pilot execution;
- a target actor/Sentinel model or any project provider/client;
- external network, GPU probe/use, weight loading/serving, or model endpoint;
- MobileWorld backend/container/emulator task execution, GUI/tool/action,
  prefix restore/replay, treatment generation, or a formal replay publication;
- restarting the old G1.6 annotation site;
- merging D-037 or reclassifying old AI-only labels as gold.

Advance to a later story only after its owner-selected scope is recorded.
Repository agents do not change Linear status; the owner manages that workflow
separately. Any live or real-environment work needs an additional owner
resource/run authorization.

## 8. Reading order

Before R2.1/R2.2 maintenance or dependent runtime changes, read completely:

1. repository-root `AGENTS.md`;
2. this file;
3. `PROJECT_CONTEXT.md` for notation and causal boundaries, while treating its
   old current-phase statements as historical;
4. `EVENT_CONTRACT_V1.md` for Collector event semantics;
5. `G1_PORTABLE_SENTINEL_CONTRACT_V1.md`;
6. `G1_5_HISTORY_CODEC_CONTRACT_V1.md`;
7. `G1_5_HISTORY_CODEC_CAPABILITIES_V1.md`;
8. `G1_5_NONFORMAL_COMPATIBILITY_ENGINEERING_CLOSE_AMENDMENT_V1.md`;
9. `R2_1_PRE_CALL_SENTINEL_RUNTIME_SEAM_CONTRACT_V1.md`;
10. `schemas/r2_1/sentinel_receipt.v1.schema.json`;
11. `R2_2_EVIDENCE_GROUNDED_SENTINEL_POLICY_CONTRACT_V1.md`;
12. all three schemas under `schemas/r2_2/`;
13. `R2_3_MULTI_PATH_RUBRIC_CONTRACT_V1.md` and the schemas under
    `schemas/r2_3/`;
14. `R2_4_QWEN_MAI_RUNTIME_VERTICAL_SLICES_CONTRACT_V1.md`,
    `R2_5_MOBILEWORLD_PILOT_PROTOCOL_V1.md`, and the schemas under
    `schemas/r2_4/` and `schemas/r2_5/`;
15. current implementation/test files listed in Section 5, including the
    `r2_2/`, `r2_3/`, `r2_4/`, and `r2_5/` runtime packages.

Read `DECISION_LOG.md`, `G1_4_DECISION_LOG.md`,
`G1_5_DECISION_LOG.md`, G1.1/G1.3/G1.4 contracts, and `STATUS.md` when
validating frozen provenance or touching those layers. Their dated active-stage
text is not the current Runtime Epic 2 plan. Read G1.6 documents only for
historical artifact maintenance; they are not R2.1 requirements.

`G1_SENTINEL_MVP_MIGRATION.md` is the 2026-08-26 legacy
`sentinel_mvp`-to-G1.2 compatibility note. It is not the Runtime Epic 2 plan.

## 9. New-server bootstrap

The Git repository is sufficient for R2.1–R2.3 CPU implementation. On a new
server:

The clone/submodule fetch below is an owner/operator bootstrap action. An agent
may run it only with explicit network authorization; this file grants none.

```bash
git clone --recurse-submodules https://github.com/pockyitachi/AgentSentinel.git
cd AgentSentinel
git status --short --branch
git rev-parse HEAD
git submodule status --recursive
test -d MobileWorld/src/mobile_world
```

Validate/recreate the Python environment from `MobileWorld/pyproject.toml`.
Do not assume the old `.venv`, `/shared/linqiang` path, GPU index, PID, tmux
session, port, model snapshot, credential, or container exists. At this handoff
the old G1.6 site and model services are stopped; ports 8766 and 18007 must not
be assumed live.

Recommended CPU baseline before R2.1 edits, using an already validated
environment or an offline dependency cache:

```bash
cd MobileWorld
uv run --offline pytest -q \
  tests/offline/test_portable_causal_replay_contract.py \
  tests/offline/test_g1_history_codecs.py \
  tests/runtime/audit
```

If the lock/cache is insufficient, stop for dependency-install/network
authorization rather than dropping `--offline`. Record exact commands and
results; do not hide environment-caused skips or failures.

Historical external artifacts are optional for R2.1–R2.3. If restored, verify
content rather than trusting the old path:

- active G1.3 manifest/content address
  `8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402`;
- G1.4 engineering-smoke manifest
  `f70cee09e4870f3b0ab8dcd0d187efacd49362731c976b0872b4243600305179`;
- G1.5 CPU publication manifest
  `cffd7f24bf09f2e18c012b2a96591064e8ba200378c7e9c920d6fdd8f068d018`.

Missing external artifacts block only tests that explicitly consume them, not
R2.1 seam design. Later R2.4/R2.5 live work additionally needs pinned Qwen/MAI
model snapshots, MobileWorld emulator/backend/container dependencies,
loopback-only model services, external mode-0600 secrets, repo-external run
roots, and a new owner authorization.

## 10. Data and publication policy

Raw collection, capsule, replay, live-run, credential, and new reviewer data
remain outside Git by default. Existing owner exceptions cover exactly the
published Epic 1 PDF, 39 screenshots, safe result projection, and exact `_03`
failure-link archive under `../motivation study/`. Do not expand those
exceptions or casually rewrite their bytes.

Frozen historical artifacts must not be deleted, chmod-repaired, overwritten,
or silently reclassified. The old G1.6 data remains non-formal. New runtime
policy/rubric decisions belong in a derived sidecar, never the raw Collector
event layer.

## 11. Delivery and stop conditions

For each story, report outcome, exact scope, files/commit, invariance evidence,
test commands/results, repo-external artifacts, limitations, unauthorized work
not performed, and the next repository dependency. Linear workflow updates are
performed separately by the owner.

Stop and request owner direction before any action that needs live model/GPU/
network/MobileWorld execution authority, changes scientific semantics, weakens
history-only invariants, requires destructive handling of user data, or would
merge/revive the superseded causal-replay path.

The R2.1 repository checkpoint is accepted only at commit
`18a4d9e6c8a3ed4ddac7cab5392a3335bae45b46`, where the shared seam, OFF/SHADOW
parity, fake ACTIVE history-only transformation, retry reuse, recursion bypass,
held/pulsed kill switch, typed fallback, pre-policy sidecar admission,
fd-bound transactional hash-only publication, strict pre-copy/pre-cache JSON
admission, complete rejected-output hashing, recursively detached policy/IR/
renderer boundaries, truthful worker-owned evaluation census, detached receipt
commit, and focused regressions were verified. Do not claim an automatic
live/model-backed policy, rubric, live transformation, success-rate gain, or
causal effect before the corresponding later work actually exists.

The corrected R2.2 repository checkpoint is accepted at
`3940ff7484c0236ac321ea210fc8266e282e5d27`. It tests a bounded
CPU/offline/fake SHADOW policy and its contracts, not live readiness,
effectiveness, or permission to start R2.4 resources. Linear remains
owner-managed.

The initial joint R2.4/R2.5 repository-preparation implementation is
`344e1c42596a4dca717da66374eeca3d936c3f61`; owner review classified it
**NO-GO**. The current R2.4 implementation/source checkpoint is
`2f5eae0dba43908a44017bf8f3ab6916b56db095`. Its complete MobileWorld
CPU/offline suite passed 2438/2438, and its exact-authority live run
`r24-smoke-gpu5-integrity-retry-20260905t181000z` plus six official Collector
integrity reports passed the evidence gate. The owner accepted this bounded
R2.4 result and authorized the reviewed branch for PR/merge; there is no current
live authority.

The consumed smoke authority created no persistent executable-task source,
selected cohort, frozen pilot manifest/hash, GUI action, or pilot artifact.
R2.5 remains unauthorized. R2.3 is accepted through merge
`2aa0a268b7d709cf05d524e74c3fba8612f64003`; R2.4 is accepted at
`2f5eae0dba43908a44017bf8f3ab6916b56db095`, and Runtime Epic 2 is 4/6
accepted.
