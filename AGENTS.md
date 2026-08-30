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
17. `mobileworld_audit_handoff/G1_GPU_LIVE_SMOKE_CONTRACT_V1.md`
18. `mobileworld_audit_handoff/G1_5_HISTORY_CODEC_CONTRACT_V1.md`
19. `mobileworld_audit_handoff/G1_5_HISTORY_CODEC_CAPABILITIES_V1.md`
20. `mobileworld_audit_handoff/G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md`
21. `mobileworld_audit_handoff/G1_6_SOLO_FIRST_PASS_AMENDMENT_V1.md`
22. `mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_ASSISTANCE_AMENDMENT_V1.md`
23. `mobileworld_audit_handoff/G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md`
24. `mobileworld_audit_handoff/G1_6_AI_ONLY_ACTION_LABELS_AMENDMENT_V1.md`
25. `mobileworld_audit_handoff/G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md`
26. `mobileworld_audit_handoff/G1_SENTINEL_MVP_MIGRATION.md`
27. `mobileworld_audit_handoff/g1/registry.lock.v1.json`
28. `mobileworld_audit_handoff/schemas/g1_3/replay_capsule.v1_1.schema.json`
29. `mobileworld_audit_handoff/schemas/g1_3/capsule_manifest.v1_1.schema.json`
30. `mobileworld_audit_handoff/schemas/g1_3/capsule_integrity.v1_1.schema.json`
31. `mobileworld_audit_handoff/schemas/g1_3/field_visibility.schema.json`
32. `mobileworld_audit_handoff/schemas/g1_3/capsule_exclusion.schema.json`

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

Active authorized scope: ALE-322 / G1.4 has a validated CPU/fake checkpoint at
commit `bf099a1a00f38edc33b6c5cbb1ab5d12d53bd18c`, covering the exact-request
runner, invariant/diff guards, deterministic scheduling and idempotent derived
attempt storage, blinded exports, versioned schemas/CLI, an in-process fake
provider, and an injectable OpenAI-compatible Provider Codec exercised only
through fake SDK clients. Formal v1.1 capsules remain read-only and retain
`execution_ready=false`, `provider_invocation_allowed=false`, and
`treatment_response_generation_allowed=false`; the CPU tranche must fail closed
before every external send.

`G1_4_DECISION_LOG.md` D-026 additionally authorizes inert/code-only preparation
for a future live/GPU proof: static frozen-model binding, pure call/block/launch
and caller-injected response records, injected-only capacity assessment, schemas,
and CPU tests. It does not authorize a client, network, subprocess, GPU probe or
use, model load, provider send, replay, or action. All live entrypoints remain
mechanically disabled pending a new owner authorization and downstream seals.
D-034 is that new authority only for its separately gated synthetic smoke
entrypoint; it does not enable or weaken any formal entrypoint covered by D-026.

Active authorized scope: ALE-323 / G1.5 has a CPU-only History Codec checkpoint
defined by `G1_5_DECISION_LOG.md` D-028 and
`G1_5_HISTORY_CODEC_CONTRACT_V1.md`. Work is limited to pure Qwen flat-progress
and MAI raw-replay extraction/rendering, exact external curated-span bindings,
secret-free fixtures, conformance/schema tests, and fail-closed G1.4 interface
integration. Both codecs must remain `live_ready=false`; the exact 10-call
live-smoke matrix is not authorized by D-028 alone. D-034 now separately
authorizes only that exact submatrix inside the shared 22-call non-formal smoke
described below. ALE-323 remains incomplete until the D-034 evidence closure
passes.

`G1_4_DECISION_LOG.md` and `G1_5_DECISION_LOG.md` D-034 plus
`G1_GPU_LIVE_SMOKE_CONTRACT_V1.md` authorize exactly one owner-bound,
secret-free, synthetic non-case, loopback-only engineering smoke on shared
physical GPU 0 / UUID `GPU-991ac45f-e9e9-1c25-590c-fb49ca752965`.
The free-memory floor is exactly 64 GiB and must be rechecked before each model
start; insufficient capacity blocks without touching another process.
The exact matrix is 12 G1.4 canaries plus 10 G1.5 codec calls, with Qwen then
full guarded release then MAI, zero SDK retries, no streaming, no extra calls,
and no generated action execution or feedback. Only processes proven by the
batch's exact PID/UID/start-time/PGID/SID/model/GPU/port launch receipt may be
stopped. For a foreign PID, only its current UID and `/proc/<pid>/stat` start
time may be read for shared-card invariance/PID-reuse protection; it must never
be signaled, modified, or inspected through `cmdline`, `exe`, `environ`, `fd`,
`cwd`, `mem`, maps, stack, or any other `/proc` surface.
The current writable model cache is acceptable only with complete pre/post tree
hash identity and no formal or TOCTOU-free immutability claim. All formal
G1.3/G1.4/G1.5/G1.6 gates and capsule guard values remain unchanged.

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
and byte-immutable. Outside the exact D-034 smoke-only exception above, do not invoke the target actor model or any project provider/client, use an
external network or a GPU, load/serve project model weights, execute a MobileWorld/generated
GUI/tool/action, restore a backend, run a deterministic prefix or live replay, generate a treatment
response, automatically decide claim validity or choose a formal intervention, or implement runtime
Sentinel behavior. The deterministic fake-provider conformance path, provider-free G1.5 CPU
checkpoint, explicitly human-authored G1.6 CPU workspace, the already authorized three-stream
D-031 offline candidate campaign, the isolated D-033 AI-only research publication, and the exact
D-034 owner-bound GPU0 loopback smoke are the only
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
D-034 separately permits only the hash-bound local vLLM health/chat sockets on
`127.0.0.1:18007` during its guarded service lifecycles; it does not permit an
external endpoint, wildcard bind, remote provider, or any other listener.
Human clicks and form entry inside that annotation site are authorized curation
inputs; they must never be converted into or executed as a MobileWorld action.
Store real capsule, collection, and
replay data outside the Git repository. Preserve unrelated user changes and
record server findings and completed phases in
`mobileworld_audit_handoff/STATUS.md`. Do not mark ALE-322 or ALE-323 complete
until the exact D-034 live/GPU smoke proof satisfies its remaining acceptance
gates, and do not mark ALE-324 complete before all 190 units are independently
reviewed, adjudicated where required, formally exported, validated, and sealed.
