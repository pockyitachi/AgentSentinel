# AgentSentinel agent instructions

Last synchronized with the owner-facing repository state: **2026-09-01 UTC**.
Linear workflow state is owner-managed and is not changed by repository agents.

This is the AgentSentinel monorepo. `MobileWorld/` is the active implementation
tree. `mobileworld_audit_handoff/` contains the historical contracts, evidence
provenance, and the detailed task instructions.

## Current authority and precedence

The owner has pivoted the active project from the former paper-grade G1 causal
replay chain to a **Runtime Sentinel MVP**. This file and
`mobileworld_audit_handoff/AGENTS.md` are the current execution authority for
that pivot.

Where older `README.md`, `STATUS.md`, `PROJECT_CONTEXT.md`,
`SERVER_AGENT_INSTRUCTIONS.md`, G1.6 contracts, or dated status entries still
call ALE-324 a human-curation task or prohibit all runtime Sentinel work, those
sentences are historical and are superseded for current execution by these two
AGENTS files. Their recorded evidence, hashes, limitations, and frozen-artifact
rules remain valid.

Do **not** merge or cherry-pick commit
`478740c` from `codex/ale-324-g16-ai-curation-amendment`. It implements the
superseded D-037 AI-replicate curation route and is not current authority.

This pivot does not authorize live model/provider calls, external network use,
GPU use, MobileWorld GUI/tool/action execution, backend restore, or replay.
Those operations still require a separate, explicit owner authorization.

## Progress snapshot

| Workstream | Current state |
| --- | --- |
| Epic 1 motivation investigation | **Complete: 6/6 models**, 117 tasks per model, 702 model-task cases. The frozen audit reports 116 strict-MHR cases and 94 cases with observed local harm. All results remain observational and `causal_claim_supported=false`. |
| Collector v1 | **Complete** for the six-model study: default-off, passive, fail-open, event-sourced, append-only, lossless for observable application-layer runtime data, and label-free. |
| G1.1–G1.3 / ALE-319–321 | **Complete**: frozen replay protocol/registry, portable History IR/Core contracts, and an immutable v1.1 publication of 190 capsules with 0 exclusions. |
| G1.4 / ALE-322 | **Engineering scope closed** as `NONFORMAL_LIVE_SMOKE_PASSED`; formal Provider Codec, isolation, treatment, and replay proof remain `DEFERRED_TO_G1_7_NOT_AUTHORIZED`. |
| G1.5 / ALE-323 | **Engineering scope closed** as `CPU_CODEC_IMPLEMENTATION_COMPLETE_NONFORMAL_COMPATIBILITY_PASSED`; Qwen flat-progress and MAI raw-replay Codecs remain `live_ready=false`. |
| Old G1.6+ causal-replay path | **Superseded/deferred**, not deleted and not completed. Four owner solo locks plus 186 D-033 AI-only labels are historical non-formal research artifacts, not gold or a current prerequisite. |
| Runtime Epic 2 / ALE-318 | **In progress: 1/6 repository engineering stories complete.** ALE-324 / R2.1 has an accepted CPU/fake implementation checkpoint at commit `b1b541d65bb915f65c534b61e15b1049f20682cd`; Linear status remains owner-managed. |

Canonical Epic 1 deliverables are under `motivation study/`. Do not turn their
observational associations into causal or cross-model ranking claims.

## Runtime Sentinel objective

The intended production boundary is:

```text
host assembles the exact actor request
    -> PromptSentinel.before_model_call(...)
    -> History Codec extracts host-native model-visible history
    -> evidence-grounded policy proposes validity operations
    -> independent rubric component proposes task-path relevance
    -> invariant validator and renderer construct a protocol-valid request
    -> the unchanged actor model chooses the GUI action
```

Sentinel modifies only the model-bound history view. It does not replace the
planner, choose an action, parse an action, execute an action, or control the
environment.

`model-agnostic` means the seam, policy interface, validator, receipts, and
fallback semantics do not branch on target model identity. Each distinct
history representation may still require a thin registered extractor/renderer.

## Accepted implementation boundary: ALE-324 / R2.1

The repository now contains the accepted **Pre-Call Sentinel Runtime Seam**
checkpoint at commit `b1b541d65bb915f65c534b61e15b1049f20682cd`.
Its executable acceptance remains CPU-only and fake-provider-only until
separately authorized otherwise.

The versioned decision/contract and shared seam define this interface:

```text
PromptSentinel.before_model_call(
    request,
    context,
    history_codec_id,
    call_role,
) -> SentinelResult
```

R2.1 may use only deterministic no-op/fake policies. Automatic claim validity,
correction generation, multi-path rubric semantics, and real model-backed
Sentinel decisions belong to later stories.

The executable R2.1 edit surface is deterministic `DROP` only. Every `REPLACE`
plan and renderer list insertion fails closed to Original. R2.2 must add a
versioned runtime proposal/admission overlay and must not mislabel automatic
proposals as frozen G1.2 curated plans.

### Required R2.1 behavior

- `OFF`: perform no Sentinel semantic work and send the original request.
- `SHADOW`: compute and record a fake/deterministic would-edit result, but send
  the original request.
- `ACTIVE`: in tests only, send a validated deterministic transformed request
  to the existing fake provider.
- Default to `OFF`; provide a global kill switch and explicit per-host mode.
- Invoke the seam exactly once per logical actor decision after host request
  assembly. Assign a stable logical-call identity and reuse the same validated
  `SentinelResult` across both BaseAgent transport retries and adapter-level
  parse retries, including Qwen's outer parse-retry loop. A streaming attempt
  must use that same result and may not trigger a second Sentinel evaluation.
- Mark Sentinel-owned calls as `call_role=sentinel`; they must bypass the hook
  to prevent recursion.
- Never mutate the caller's request. Preserve the immutable raw request and
  return a separate final request.
- Restrict changes to declared history spans. System policy, task instruction,
  tools, current screenshot and other multimodal blocks, roles/order,
  model/sampling settings, and all non-history bytes must remain invariant.
- Timeout, exception, invalid schema, ambiguous span, unsupported family,
  renderer failure, or invariant failure must produce a typed fallback to the
  original request. A partially transformed request must never be sent.
- Record a lightweight hash-only sidecar receipt: raw/candidate/final request
  hashes, canonical policy-output hash, exact-diff hash, codec, mode, decision
  kinds, validation, fallback reason, and latency. R2.1 does not persist request
  views, operation payloads, or exact-diff preimages. Any future detail channel
  requires a separate versioned, access-controlled, repo-external contract;
  never duplicate secrets or chain of thought into receipts.
- Keep the existing provider transport, response normalization, action parser,
  runner, and action execution path unchanged.

The likely common boundary is
`MobileWorld/src/mobile_world/agents/base.py::BaseAgent.openai_chat_completions_create`.
Production-shaped request construction for the first two hosts is in
`MobileWorld/src/mobile_world/agents/implementations/qwen3vl.py` and
`MobileWorld/src/mobile_world/agents/implementations/mai_ui_agent.py`.
Confirm the live code rather than trusting old line numbers.

## Runtime Epic 2 sequence

```text
ALE-324 / R2.1 pre-call seam
    -> { ALE-325 / R2.2 evidence-grounded SentinelPolicy (SHADOW first)
       , ALE-326 / R2.3 multi-path rubric tracker }
    -> ALE-327 / R2.4 Qwen + MAI runtime vertical slices
    -> ALE-328 / R2.5 frozen 20–30 task real MobileWorld pilot
    -> ALE-329 / R2.6 conditional six-family and GUI-117 expansion
```

R2.2 owns `KEEP / DROP / REPLACE / KEEP_UNCERTAIN` decisions grounded only in
causally available GUI/execution evidence. R2.3 is a separate AND-OR path axis:
it does not establish truth or recommend an action, and `ARCHIVE` remains
SHADOW-only initially. R2.4 live smoke and R2.5 real execution require new
resource/run authorization.

The formal 190-unit, five-arm, 1,140-review, adjudication, admission-seal, and
state-frozen replay program is no longer on the runtime MVP critical path. Its
artifacts may be used as regression fixtures or revived later in a separate
causal-evaluation epic after an explicit owner decision.

## Reuse; do not rebuild or rewrite

- Collector v1 under `MobileWorld/src/mobile_world/runtime/audit/` remains the
  immutable evidence layer.
- Reuse History IR, exact-span binding, target-only mutation, reversibility,
  invariant validation, and sidecar concepts from
  `MobileWorld/src/mobile_world/offline/causal_replay/`. Do not weaken the
  accepted G1.2 contracts to make runtime behavior fit.
- Reuse Qwen and MAI extraction/rendering logic from
  `MobileWorld/src/mobile_world/offline/g1_history_codecs/`, but do not mutate
  the frozen G1.5 publication or falsely flip either v1 Codec's
  `live_ready=false` declaration. Runtime readiness requires a new overlay.
- G1.3 capsules and the G1.4 runner/fake provider are optional regression/test
  assets, not online dependencies and not blockers for R2.1–R2.3.
- `sentinel_mvp/` is a legacy behavioral reference, not production runtime
  code and not an authority over the new seam.

## New-server resume checklist

Clone recursively and do not assume any old machine path, process, port, GPU,
virtual environment, credential, or external artifact exists:

The clone/submodule fetch below is an owner/operator bootstrap action. An agent
may run it only with explicit network authorization; this file does not grant
network access.

```bash
git clone --recurse-submodules https://github.com/pockyitachi/AgentSentinel.git
cd AgentSentinel
git status --short --branch
git rev-parse HEAD
git submodule status --recursive
test -d MobileWorld/src/mobile_world
```

Then:

1. Read this file and `mobileworld_audit_handoff/AGENTS.md` completely.
2. Confirm the checkout is clean and based on current `origin/main`; preserve
   all unrelated user changes.
3. Inspect `MobileWorld/pyproject.toml` and recreate or validate the Python
   environment. Do not assume the old `.venv` is portable.
4. Run the focused G1.2 and G1.5 CPU tests before modifying the seam, then add
   focused R2.1 tests. Use the existing validated environment or an offline
   dependency cache; stop for dependency-install/network authorization rather
   than allowing a test command to fetch packages. Run the full suite before
   handoff when feasible.
5. Treat missing repo-external data as an availability fact, not permission to
   regenerate or alter frozen data. R2.1–R2.3 CPU work must not depend on the
   old server's raw datasets.

Useful historical external bindings, if deliberately restored for regression:

- G1.3 active manifest/content address:
  `8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402`
  (190 capsules, 0 exclusions, 1,600 files, 116,169,862 bytes).
- G1.4 engineering smoke bundle: `g1_4_engineering_close_20260831`, manifest
  SHA-256 `f70cee09e4870f3b0ab8dcd0d187efacd49362731c976b0872b4243600305179`.
- G1.5 CPU publication manifest SHA-256:
  `cffd7f24bf09f2e18c012b2a96591064e8ba200378c7e9c920d6fdd8f068d018`.

These paths were under the old server's `/shared/linqiang/...` tree. Bind any
restored data through configuration and verify hashes; never hardcode that
prefix on a new server.

R2.4/R2.5 later require separately provisioned Qwen and MAI model snapshots,
the MobileWorld backend/emulator/container environment, loopback model
services, external mode-0600 secrets, and repo-external output roots. None is
needed or authorized for R2.1 CPU acceptance. Do not reuse old GPU indices,
PIDs, tmux names, or port assumptions. At this handoff no project tmux session,
port 8766 annotation site, or port 18007 model service should be assumed live.

## Data, publication, and Git safety

- Raw collections, capsules, live run outputs, credentials, and new reviewer
  artifacts stay outside Git unless the owner creates an exact new exception.
- The already published Epic 1 PDF, 39 screenshot allowlist, safe result
  projection, and exact `_03` failure-link archive are owner-approved public
  evidence. Do not expand that allowlist or rewrite those bytes casually.
- Keep raw Collector events label-free. Runtime decisions belong in a derived
  sidecar and must never be written back into Collector v1 events.
- Never delete, overwrite, chmod, or repair frozen external publications.
- Never reset, discard, or overwrite unrelated user work. Use explicit paths
  for staging and destructive operations.
- Do not merge the superseded D-037 branch. New work should normally use a
  `codex/` branch unless the owner explicitly requests direct work on `main`.

## Task-specific reading

For R2.1 maintenance or dependent runtime work, read completely:

1. `mobileworld_audit_handoff/AGENTS.md`
2. `mobileworld_audit_handoff/PROJECT_CONTEXT.md` for notation and scientific
   boundaries; its old "current phase" prose is historical.
3. `mobileworld_audit_handoff/EVENT_CONTRACT_V1.md`
4. `mobileworld_audit_handoff/G1_PORTABLE_SENTINEL_CONTRACT_V1.md`
5. `mobileworld_audit_handoff/G1_5_HISTORY_CODEC_CONTRACT_V1.md`
6. `mobileworld_audit_handoff/G1_5_HISTORY_CODEC_CAPABILITIES_V1.md`
7. `mobileworld_audit_handoff/G1_5_NONFORMAL_COMPATIBILITY_ENGINEERING_CLOSE_AMENDMENT_V1.md`
8. `mobileworld_audit_handoff/R2_1_PRE_CALL_SENTINEL_RUNTIME_SEAM_CONTRACT_V1.md`
9. `mobileworld_audit_handoff/schemas/r2_1/sentinel_receipt.v1.schema.json`
10. The current BaseAgent, Qwen, MAI, Collector, G1.2, G1.4 fake-provider, and
    G1.5 Codec implementation/tests.

Read the G1.1/G1.3/G1.4/G1.6 contracts and `STATUS.md` when touching those
historical artifacts or validating their provenance. They are not mandatory
runtime-policy design documents and must not restore the superseded G1.6
critical path.

## Handoff discipline

Every delivery must report:

- outcome and exact scope;
- files changed and commit;
- behavior/invariance guarantees;
- test commands and results;
- artifacts created outside Git;
- known limitations and unauthorized work not performed;
- the next repository dependency and whether it is mechanically available.

Repository agents report evidence but do not change Linear status; the owner
performs that workflow update separately.

Do not claim a runtime Sentinel, rubric, live transformation, success-rate
improvement, or causal effect before the corresponding implementation and
evaluation have actually completed.
