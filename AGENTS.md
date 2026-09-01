# Agent instructions

This is the AgentSentinel monorepo. The active implementation target is
`MobileWorld/`, but the authoritative scope and contracts live in
`mobileworld_audit_handoff/`.

Before changing code, read these files completely:

1. `mobileworld_audit_handoff/AGENTS.md`
2. `mobileworld_audit_handoff/PROJECT_CONTEXT.md`
3. `mobileworld_audit_handoff/DECISION_LOG.md`
4. `mobileworld_audit_handoff/G1_4_DECISION_LOG.md`
5. `mobileworld_audit_handoff/G1_5_DECISION_LOG.md`
6. `mobileworld_audit_handoff/G1_6_DECISION_LOG.md`
7. `mobileworld_audit_handoff/EVENT_CONTRACT_V1.md`
8. `mobileworld_audit_handoff/SERVER_AGENT_INSTRUCTIONS.md`
9. `mobileworld_audit_handoff/STATUS.md`
10. `mobileworld_audit_handoff/G1_CAUSAL_REPLAY_PROTOCOL_V1.md`
11. `mobileworld_audit_handoff/G1_LOCKED_ANALYSIS_PLAN_V1.md`
12. `mobileworld_audit_handoff/G1_PORTABLE_SENTINEL_CONTRACT_V1.md`
13. `mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1.md`
14. `mobileworld_audit_handoff/G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md`
15. `mobileworld_audit_handoff/G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md`
16. `mobileworld_audit_handoff/G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md`
17. `mobileworld_audit_handoff/G1_4_NONFORMAL_LIVE_SMOKE_ENGINEERING_CLOSE_AMENDMENT_V1.md`
18. `mobileworld_audit_handoff/schemas/g1_4/nonformal_live_smoke_manifest.v1.schema.json`
19. `mobileworld_audit_handoff/g1_4/nonformal_live_smoke_manifest.v1.json`
20. `mobileworld_audit_handoff/g1_4/nonformal_live_smoke_install_record.v1.json`
21. `mobileworld_audit_handoff/G1_5_HISTORY_CODEC_CONTRACT_V1.md`
22. `mobileworld_audit_handoff/G1_5_HISTORY_CODEC_CAPABILITIES_V1.md`
23. `mobileworld_audit_handoff/G1_5_NONFORMAL_COMPATIBILITY_ENGINEERING_CLOSE_AMENDMENT_V1.md`
24. `mobileworld_audit_handoff/G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md`
25. `mobileworld_audit_handoff/G1_6_SOLO_FIRST_PASS_AMENDMENT_V1.md`
26. `mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_ASSISTANCE_AMENDMENT_V1.md`
27. `mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md`
28. `mobileworld_audit_handoff/G1_6_AI_ONLY_ACTION_LABELS_AMENDMENT_V1.md`
29. `mobileworld_audit_handoff/G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md`
30. `mobileworld_audit_handoff/G1_SENTINEL_MVP_MIGRATION.md`
31. `mobileworld_audit_handoff/g1/registry.lock.v1.json`
32. `mobileworld_audit_handoff/schemas/g1_3/replay_capsule.v1_1.schema.json`
33. `mobileworld_audit_handoff/schemas/g1_3/capsule_manifest.v1_1.schema.json`
34. `mobileworld_audit_handoff/schemas/g1_3/capsule_integrity.v1_1.schema.json`
35. `mobileworld_audit_handoff/schemas/g1_3/field_visibility.schema.json`
36. `mobileworld_audit_handoff/schemas/g1_3/capsule_exclusion.schema.json`

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

Closed engineering scope: ALE-322 / G1.4 has a validated CPU/fake checkpoint at
commit `bf099a1a00f38edc33b6c5cbb1ab5d12d53bd18c`, covering the exact-request
runner, invariant/diff guards, deterministic scheduling and idempotent derived
attempt storage, blinded exports, versioned schemas/CLI, an in-process fake
provider, and an injectable OpenAI-compatible Provider Codec exercised only
through fake SDK clients. Formal v1.1 capsules remain read-only and retain
`execution_ready=false`, `provider_invocation_allowed=false`, and
`treatment_response_generation_allowed=false`. D-035 now closes the bounded
engineering delivery as `NONFORMAL_LIVE_SMOKE_PASSED`, while formal replay is
exactly `DEFERRED_TO_G1_7_NOT_AUTHORIZED`; there is no active G1.4 GPU/model,
provider, replay, treatment, or action authority.

`G1_4_DECISION_LOG.md` D-026 historically authorized inert/code-only preparation
for a possible live/GPU proof: static frozen-model binding, pure call/block/launch
and caller-injected response records, injected-only capacity assessment, schemas,
and CPU tests. That preparation and the later D-034 smoke authority are consumed;
they do not authorize a client, network, subprocess, GPU probe/use, model load,
provider send, replay, treatment, or action.

Closed engineering scope: ALE-323 / G1.5 is accepted under D-036 as
`CPU_CODEC_IMPLEMENTATION_COMPLETE_NONFORMAL_COMPATIBILITY_PASSED`; its exact
formal-live-readiness state is `DEFERRED_TO_G1_7_NOT_AUTHORIZED`. The accepted
delivery is the pure Qwen flat-progress and MAI raw-replay Codec implementation,
exact external curated-span bindings, five-arm render/diff/reversibility and
preview surfaces, secret-free fixtures and CPU publication, conformance/schema
tests, fail-closed G1.4 integration, and ten D-035 non-formal prompt/parser
compatibility observations. Those observations did not execute the formal
History Codec-to-Provider Codec path and do not satisfy the D-028 formal matrix.
Both v1 Codecs remain `live_ready=false`; all formal matrix, Provider Codec,
complete per-attempt evidence, serving/isolation, and live seal duties are
transferred to G1.7. No further standalone G1.5 live run is authorized.

Active authorized scope: ALE-324 / G1.6 is the CPU-only, human-in-the-loop gold
curation workspace defined by `G1_6_DECISION_LOG.md` D-029 and
`G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md`. Work may project blinded
reviewer packets from the immutable G1.3 publication, render no-send G1.5 CPU
previews using locally hash-verified tokenizers, collect independent human
reviews/adjudications in a repo-external append-only journal, and validate the
workspace schemas. It may not infer, choose, or improve a target, correction,
sham, oracle, accepted action, or adjudication on behalf of a human. Formal
bundle export, admission/sealing, replay, and treatment generation remain
blocked until their separate versioned gates are satisfied.

`G1_6_DECISION_LOG.md` D-030 and
`G1_6_SOLO_FIRST_PASS_AMENDMENT_V1.md` additionally authorize a mechanically
non-formal one-person precursor workspace. It must use a separate root, key,
manifest, and journal; enforce the global Action Gold → Transformation →
Consistency stage order; and keep every independence, resolution, promotion,
export, admission, replay, and seal authority false. It cannot replace or be
promoted into the formal double-blind workspace.

`G1_6_DECISION_LOG.md` D-031 additionally authorizes exactly three isolated Codex streams to
prepare offline, non-authoritative `ACTION_GOLD` candidate predicates for the solo curator. The
website may only display already frozen candidates and append separate per-item human decisions;
it must never generate, rank, merge, auto-apply, save, lock, or promote a candidate. The only narrow
exception is D-032: after the same solo curator makes every explicit three-way decision, one separate
final click may let the server derive, validate, and append a non-formal solo lock from frozen
candidate bytes while holding the decision-journal lock. It never becomes formal review or
publication authority. These streams
are not human reviewers, their outputs never count as independent review, and candidate exposure
permanently excludes that principal from future formal G1.6 reviewer/adjudicator roles.

`G1_6_DECISION_LOG.md` D-033 additionally authorizes three isolated Codex research agents to
review the remaining 186 units as three exact 62-unit shards and publish a separate
`AI_ONLY_ACTION_LABELS` research dataset. The agents may only retain/reject existing frozen D-031
candidates or exclude a unit; they may not generate new predicates or read human/peer/history/future
material. The publication is non-human, non-formal, cannot write or advance either annotation
journal, and must keep every review/export/admission/promotion/replay authority false.

Collector v1 remains event-sourced, lossless, label-free, zero-intervention,
and byte-immutable. Do not invoke the target actor model or any project provider/client, use an
external network or a GPU, load/serve project model weights, execute a MobileWorld/generated
GUI/tool/action, restore a backend, run a deterministic prefix or live replay, generate a treatment
response, automatically decide claim validity or choose a formal intervention, or implement runtime
Sentinel behavior. The deterministic fake-provider conformance path, provider-free G1.5 CPU
checkpoint, explicitly human-authored G1.6 CPU workspace, the already authorized three-stream
D-031 offline candidate campaign, and the isolated D-033 AI-only research publication are the only
permitted substitutes. D-031 is the sole candidate-suggestion exception: its outputs remain
untrusted and require individual human decisions, and the
annotation website itself must never invoke Codex or another model/provider.
The separate D-033 AI-only label publication is the only additional semantic-labeling exception;
it remains isolated from both human journals and cannot be treated as human review or gold.
The sole socket exception is the owner-started, single-process D-029 annotation
site bound to loopback with same-origin/CSRF checks and no remote assets. D-030
also permits only the owner's single-port SSH local forward from client
`127.0.0.1:8766` to server `127.0.0.1:8766`; reverse/dynamic forwarding,
wildcard binds, shared proxies, and external hosting remain forbidden.
Human clicks and form entry inside that annotation site are authorized curation
inputs; they must never be converted into or executed as a MobileWorld action.
Store real capsule, collection, and
replay data outside the Git repository. Preserve unrelated user changes and
record server findings and completed phases in
`mobileworld_audit_handoff/STATUS.md`. ALE-322's exact bounded engineering-close
state is `NONFORMAL_LIVE_SMOKE_PASSED`; it MUST NOT be called formal live proof
or replay-ready. Its exact formal-replay state is
`DEFERRED_TO_G1_7_NOT_AUTHORIZED`. ALE-323 is closed only under D-036's bounded
engineering scope; both v1 Codecs remain `live_ready=false`, and G1.7 formal
readiness remains unauthorized. ALE-324 must not be marked complete before all 190 units are independently
reviewed, adjudicated where required, formally exported, validated, and sealed.

Owner public-evidence exception (2026-09-01): notwithstanding the default
repo-external rule above, the owner explicitly authorizes the public Git
publication of exactly the 39 content-addressed PNG screenshots referenced by
the canonical Epic 1 report under `motivation study/report_assets/screenshots/`
and the single fixed PDF
`motivation study/misleading_history_audit_report_20260825.pdf`. This exception
does not cover raw cards, requests, trajectories, model responses, reviewer
text, receipts, logs, replay data, or any other Collector blob. The repository
is public, and the owner accepts that these exact bytes may persist in Git
history, forks, and caches even after a later deletion. The screenshots contain
synthetic/demo credential-like and identity-like fixture values as well as
third-party application UI, trademarks, and imagery whose separate
redistribution rights were not independently verified. Any medical-looking
text visible in a benchmark screenshot is inert research evidence, not medical
advice or endorsement. This documentation publication grants no model,
provider, network, GPU, replay, treatment, GUI, tool, or action authority.

Owner final failure-link archive exception (2026-09-01): as a separate,
additive exception, the owner explicitly authorizes public Git publication of
exactly `motivation study/failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03/`.
The authorized tree is exactly 2,842 regular files / 119,555,475 bytes with
path-sorted inventory SHA-256
`a97f9d4541c339d3cb6782bf499eed61ade9bfe68270419b7a62f500f4aa944a`.
It contains raw cards and requests, model responses, reviewer text and
rationales, operational receipts, logs, and machine-local paths. No confirmed
live secret was found in the pre-publication review, but the repository is
public and these bytes may remain permanently in Git history, forks, mirrors,
and caches. The deleted `_02` attempt is not authorized, and this exception
does not cover any other collection, capsule, replay, or audit data. The v1/v2
publication locks remain unchanged historical safe/report-publication scopes;
they do not bind or certify this later raw-archive exception. This publication
creates no model, provider, network, GPU, replay, treatment, GUI, tool, or
action authority.

Owner direct-smoke boundary (2026-08-30): notwithstanding the historical
deferred/backlog language above, the owner authorized one non-formal direct
smoke on GPU 0 with `CUDA_VISIBLE_DEVICES=0`, a loopback-only service at
`127.0.0.1:18007`, Qwen followed by MAI, and exactly 22 secret-free synthetic
calls. Cleanup may target only children and sessions created by this smoke; it
must not read private `/proc` details of, signal, or take action against any
foreign process, and it must not execute any returned action. Read-only
GPU/process baseline checks, including a final `nvidia-smi`, remain allowed.
Any failure ends the attempt with no retry. The former D-034
authority/shim/formal-evidence chain is obsolete and must not be reused or
treated as a gate.

GPU 0 outcome and replacement authority (2026-08-31): the GPU 0 attempt failed
safely before model loading with 0/22 calls because the installed vLLM does not
support `--swap-space`; that attempt is closed and must not be retried. The
owner authorizes exactly one replacement attempt on GPU 4 with
`CUDA_VISIBLE_DEVICES=4`, the same loopback-only `127.0.0.1:18007` service,
Qwen followed by MAI, and exactly 22 secret-free synthetic calls. It may clean
up only its own children/session and must not signal, modify, stop, or otherwise
act on `taoz` or any foreign process. Any failure ends the replacement attempt
immediately with no retry. The obsolete authority/shim/formal-evidence chain
remains prohibited.

GPU 4 outcome and next-fix boundary (2026-08-31): the replacement attempt
entered Qwen model loading/PROFILE, then failed safely with 0/22 calls because
the bundled Triton `ptxas` was non-executable (`0644`, `EACCES`). MAI was not
started; `taoz` PID 217927, its baseline/memory, and the loopback port were
unchanged or restored to baseline. This attempt is closed and must not be
retried. The only authorized follow-up fix is to point the smoke's child-process
environment at system CUDA tool paths; it must not `chmod` or otherwise modify
the shared venv.

Final GPU 4 outcome and engineering close (2026-08-31): after the system-CUDA
tool-path fix and production-prompt/parser correction, the one authorized smoke
completed Qwen 11/11 followed by MAI 11/11, for exact 22/22 HTTP-200,
host-parseable calls with zero retry and zero generated-action execution. The
three artifacts are sealed read-only under the D-035 content-addressed bundle.
This is `NONFORMAL_LIVE_SMOKE_PASSED` engineering evidence only; formal Provider
Codec, serving-environment, isolation, treatment, and replay proof remain false
and deferred to future G1.7 consideration. The GPU/model authority is consumed;
no further GPU/model attempt is authorized.
