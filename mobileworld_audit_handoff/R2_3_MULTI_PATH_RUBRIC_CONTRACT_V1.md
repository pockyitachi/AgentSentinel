# R2.3 Multi-Path Rubric Generation and Tracking Contract v1

Status: **CPU/OFFLINE/INJECTED-FAKE SHADOW CANDIDATE for ALE-326**

Contract ID: `mobileworld.runtime.multi-path-rubric/contract-v1`

Rubric schema: `schemas/r2_3/rubric.v1.schema.json`

Tracking packet schema: `schemas/r2_3/tracking_packet.v1.schema.json`

Tracker output schema: `schemas/r2_3/tracker_output.v1.schema.json`

Receipt schema: `schemas/r2_3/rubric_receipt.v1.schema.json`

Topology comparison schema: `schemas/r2_3/topology_comparison.v1.schema.json`
Decision date: 2026-09-02 UTC

## 1. Decision and authority

R2.3 adds a task-path relevance axis that is separate from R2.2 history
factual validity:

```text
task start instruction
  -> one frozen, versioned AND/OR rubric

task instruction + current GUI + completed transition evidence + prior state
  -> history-free milestone proposal
  -> deterministic admission and graph evaluation
  -> milestone state + viable/inactive/unknown paths + frontier

frozen rubric state + explicit record/path bindings
  -> record relevance + RETAIN

separately claimed R2.2 support hashes
  -> input-bound only; no ARCHIVE without a trusted R2.2 resolver
```

The rubric has four constant false authority fields:

```text
factual_truth_authority = false
history_edit_authority = false
action_or_tool_authority = false
archive_execution_authority = false
```

It never verifies a history claim, chooses or recommends a GUI action, creates
a tool call, changes a model request, authorizes `DROP` or `REPLACE`, or
executes `ARCHIVE`. The unchanged actor remains responsible for actions.

This checkpoint authorizes only CPU/offline execution through an injected fake
backend. It does not authorize a provider/client, model inference, network,
GPU, model loading/serving, MobileWorld backend/emulator/container, GUI/tool/
action, replay, or treatment execution. Its execution scope is
`SHADOW_ONLY`.

## 2. Trust separation and canonical binding

A replaceable rubric backend is an untrusted semantic proposer. Deterministic
runtime code exclusively owns:

1. exact trusted-type and canonical-JSON admission;
2. task text, Unicode code-point, UTF-8 byte, and SHA-256 binding;
3. milestone, gate, path, graph-reference, uniqueness, reachability, and cycle
   validation;
4. version and explicit-revision validation;
5. evidence identity, same-task, and causal-cutoff checks;
6. milestone evidence strength checks;
7. deterministic path viability, frontier, relevance, and fail-closed
   `ARCHIVE_SHADOW` admission;
8. state compare-and-swap and sidecar publication; and
9. all no-authority and no-resource-use guarantees.

Backend output is rebuilt into exact module-owned frozen dataclasses. Hashing,
census, admission, graph evaluation, and downstream linking consume that one
snapshot. No backend-owned `to_dict`, serializer, subclass, custom container,
or mutable object graph is authoritative. Once a complete output is
canonicalizable, its canonical hash is bound before later ID or semantic
admission rejection. Hashing records what was proposed; it grants no authority.

Session construction first rebuilds the task and both backend descriptors into
private authority snapshots. Before each generate, revise, or track call, the
runtime freezes a private request/packet/prior-state snapshot and its canonical
input/CAS hashes. The replaceable backend receives a second detached copy.
Admission, state transition, cache identity, and receipts read only the private
authority graph and pre-call hashes; backend mutation of its copy cannot alter
them. Public rubric/state properties, packet/revision builders, operation
results, adapters, and every cache hit likewise return fresh detached graphs,
never the session or cache authority objects.

Every trusted projection and snapshot uses the same bounded traversal: active-
path identity rejects cycles while permitting ordinary shared DAG references,
maximum depth is 64, and maximum visits across containers, dataclasses, enums,
and primitive leaves are 262,144. Cycle,
depth, node-budget, and defensive interpreter-recursion failures are stable
typed contract rejections. A non-canonical cyclic or over-budget backend output
has null raw/parsed hashes rather than being re-traversed in the fallback path;
a complete finite canonical output still binds its real hash before later
semantic rejection.

## 3. Task-start rubric

One `mobileworld.runtime.multi-path-rubric/v1` object binds one task run and the
exact `task_started` instruction text/hash. It contains:

- exact instruction spans for instruction-cited hard requirements,
  constraints, and terminal requirements;
- derived and optional state checkpoints;
- explicit acyclic `AND` / `OR` gates;
- one or more named `LEGAL_ALTERNATIVE` paths;
- exactly one `OTHER_UNKNOWN` escape path;
- a common root when requirements apply across paths;
- backend prompt/schema/config provenance;
- one revision record and graph version; and
- the constant false authority object.

The schema is closed and has no preferred path, next-action, click sequence,
coordinate, action, tool, factual-verdict, history-operation, or active archive
field.

### 3.1 Exact instruction spans

Each cited span binds:

```text
span_id
role = HARD_REQUIREMENT | CONSTRAINT | TERMINAL_REQUIREMENT
char_start / char_end
utf8_byte_start / utf8_byte_end
exact_text / span_sha256
```

Code-point and UTF-8 coordinates must resolve to the same non-empty bytes in
the frozen instruction. Spans are unique and non-overlapping. Each declared
span is consumed by exactly one matching instruction-bound milestone.
Instruction-bound milestone text/hash must equal the exact span; it cannot be
paraphrased or silently rewritten.

`DERIVED_CHECKPOINT` and `OPTIONAL_CHECKPOINT` carry no instruction-span ID and
cannot use `INSTRUCTION_REQUIREMENT` provenance. They may describe progress,
but cannot become global hard requirements or independently make the task
complete/failed. Semantic completeness of initial requirement extraction is a
safety-evaluation question; structural admission prevents an unanchored node
from being represented as an instruction-cited requirement.

### 3.2 AND/OR graph and alternative paths

Milestones are leaves. Gates have operator `AND` or `OR` and at least two
explicit child references. References resolve only to a declared milestone or
gate. IDs are unique, milestone/gate namespaces are disjoint, every node is
reachable from a common or legal-path root, and the gate graph is acyclic.

Every `LEGAL_ALTERNATIVE` has a root. Exactly one `OTHER_UNKNOWN` path has no
root and is never forced viable or inactive. The graph does not encode a
preferred click sequence. Multiple legal paths may remain viable and may
contribute frontier items simultaneously.

## 4. Frozen generation and explicit revision

Task initialization invokes `RubricBuilderBackendV1.generate` at most once per
task run. The admitted version-1 graph is frozen. Repeated initialization,
per-step retries, actor transport retries, or parse retries reuse it without a
second generation call. A failed task-start attempt does not trigger implicit
per-step regeneration.

A later graph change requires `RubricRevisionRequestV1` and an
`EXPLICIT_REVISION` record that binds:

- the same rubric and task-run identity;
- the previous version and canonical rubric hash;
- a revision event and closed reason code;
- exactly `previous_version + 1`;
- the exact added/removed instruction-requirement key set; and
- the exact changed milestone/gate/path node-ID set.

Requirement keys bind task hash, role, both coordinate systems, and span hash.
A rewrite is therefore explicit remove-plus-add. Runtime recomputes the old/new
sets; incomplete, invented, or surplus deltas fail admission. A changed task
hash requires `TASK_INSTRUCTION_CHANGED`; that reason is invalid when the task
hash did not change.

## 5. History-free runtime tracking packet

The only runtime grounding input is
`mobileworld.runtime.rubric-tracking-packet/v1`. Its module-owned builder
accepts the exact task instruction, current observation, completed transition
evidence, prior rubric state, and causal cutoff. Its type surface contains no
History IR, actor-history record, actor reasoning, or R2.2 EvidencePacket.

The packet's topology is fixed to:

```json
{
  "kind": "ISOLATED_HISTORY_FREE",
  "independent_grounding_claim_eligible": true
}
```

Its closed exclusions require false for natural-language actor history,
History IR, history-policy output used as truth, future events, task outcome,
benchmark checker, replay result, and Collector mutation. These flags are not
caller attestations: the closed module-owned packet builder and source-event
allowlist are the authority.

Current screenshot/accessibility evidence comes only from the bound
`step_started` observation. Runtime evidence comes only from completed,
failed, or not-executed transition events belonging to the same task and no
later than the Collector-sequence cutoff. Tool/user result projections require
`transition_completed`. No new screenshot or environment observation is
requested by R2.3.

## 6. Milestone proposal and admitted state

The backend returns a complete `MILESTONE_PROPOSAL` with exactly one record per
frozen milestone. States are:

```text
pending | in_progress | satisfied | violated | unknown
```

Evidence references bind exact packet evidence ID and payload hash.
`satisfied` requires supporting evidence, `violated` requires refuting
evidence, and `in_progress` requires progress evidence. `pending` carries no
evidence. Ambiguous, conflicting, or insufficient GUI evidence yields
`unknown`; it is an admitted abstention, not a forced branch choice.

Generic `COMPLETED_TRANSITION_STATUS`, executor success, HTTP success, action
attempt, or screenshot change is weak evidence. It cannot alone establish
semantic satisfaction or violation. Task instruction describes the requested
goal, not proof of completion.

Because v1 has no typed semantic-claim projection for post-transition UI,
`COMPLETED_POST_UI_STATE` and free-form `AGENT_VISIBLE_TOOL_RESULT` are
conservatively weak as a whole. A future version may admit typed semantic
post-state/tool-result claims, but v1 requires independent current GUI or
user-response evidence for a decisive state.

Each decisive state must cite at least one non-weak reference with the matching
`SUPPORTS_STATE` or `REFUTES_STATE` relation. Mixed support and refutation is
not resolved by ordering or by transition success; it must yield `unknown`.
Any non-`pending` state may be carried forward without new evidence only
through `PRESERVE_PRIOR_STATE`; the state and its prior evidence references
must be copied exactly. It cannot silently regress to `pending`.

The trusted runtime derives the complete `TRACKING_STATE` rather than trusting
backend-supplied path/frontier fields. One module-owned memoized DAG analysis
is shared by state construction and validation: viability and satisfaction for
each shared graph reference are computed once, and each path's frontier walk
visits a shared reference at most once. The validator recomputes path states
and frontier from the admitted milestone records and rejects any mismatch.

A blocking instruction-cited milestone with admitted `violated` evidence may
make a path inactive. A blocking `unknown` makes the unresolved path unknown.
Otherwise pending/in-progress requirements leave the path viable. Derived or
optional checkpoint failure does not create an invented hard failure. When an
`OR` contains any blocking-bearing child, non-blocking siblings cannot rescue
a violated blocking alternative; an all-non-blocking checkpoint gate remains
viable and may still contribute frontier items. `OTHER_UNKNOWN` remains
unknown.

The frontier is a deterministic canonical tuple of unique
`(path_id, milestone_id)` pairs from non-inactive paths. It may contain
multiple alternatives and is not a next action. Each state version binds the
prior private-authority canonical state hash and advances through compare-and-
swap; a conflict preserves the prior state. The backend never receives that
authority object, only a detached packet copy.

## 7. Post-state record relevance and ARCHIVE

History records are linked only after rubric state is frozen for the logical
call. `PathRelevanceInterfaceV1` receives record IDs and explicit path IDs; it
does not feed record text back into grounding.

The link input and `linkage_id` bind a module-owned canonical JSON projection
of the state hash, logical-call ID, record/path bindings, and supported-record
bindings. Delimiter-concatenated identifiers are not a valid binding format.

Relevance values are:

```text
active_path | inactive_branch | path_independent | unknown
```

The runtime deterministically derives them:

- any viable linked path -> `active_path`;
- otherwise any unknown linked path -> `unknown`;
- all linked paths inactive -> `inactive_branch`;
- explicit path-independent binding -> `path_independent`;
- no path binding -> `unknown`.

`ARCHIVE_SHADOW` is a schema-reserved relevance disposition, not a history
operation. This checkpoint has no trusted resolver that can prove a
record-level `SUPPORTED + KEEP` result from the committed R2.2 receipt,
policy output, and evidence packet. Two syntactically valid SHA-256 values are
therefore never archive authority. The runtime emits
`supported_record_binding_sha256=null` and `RETAIN` for every record, and the
validator rejects any non-null support binding or `ARCHIVE_SHADOW` disposition
with `R22_SUPPORT_RESOLVER_REQUIRED`.

A later version may admit `ARCHIVE_SHADOW` only after a module-owned resolver
mechanically verifies the committed R2.2 receipt/output/evidence chain, exact
logical call and record coverage, and `SUPPORTED + KEEP` for every eligible
target in the record, in addition to inactive linked routes,
`ISOLATED_HISTORY_FREE`, and `SHADOW_ONLY`. No R2.1/R2.2 renderer consumes
`ARCHIVE_SHADOW` in R2.3.

## 8. Actor-visible state

Actor-visible rubric status is independently configured and defaults to
disabled. When enabled, only a deterministic status-only projection may be
constructed and the runtime recomputes the exact text from the milestone/path
census before admitting a state packet; it is not backend free-form guidance.
R2.3 does not inject it
into an actor request. Disabling it does not change rubric tracking, record
relevance, or any independently configured history filtering.

## 9. Joint versus isolated topology contract

R2.3 does not freeze the R2.4 call topology. The comparison schema represents:

1. `ISOLATED_HISTORY_FREE`, eligible for an independent-grounding claim; and
2. `JOINT_NON_INDEPENDENT`, never eligible for that claim.

Each run has separate rubric input/output/receipt hashes, latency, status, and
failure. An admitted joint run additionally binds separate history-policy
input/output hashes. Joint results cannot occupy the isolated slot, silently
replace isolated output, or support an independence claim. The v1 comparison
constants remain:

```text
deployment_decision = UNDECIDED_R2_4
independent_grounding_source = ISOLATED_ONLY
joint_may_replace_isolated = false
deployment_topology_frozen = false
```

The CPU checkpoint may leave `joint=null` or `NOT_RUN`; it makes no joint model
call.

## 10. Receipts, state storage, and metrics

Every admitted operation, backend attempt, and canonical input rejected after
preflight uses the separate hash-only
`mobileworld.runtime.rubric-receipt/v1` schema. Untrusted/non-canonical values
that cannot be safely identified or hashed may fail before sidecar admission.
Operations are task-start generation, explicit revision, tracking, relevance
linking, and topology comparison. The receipt records:

- task/logical-call and topology identity;
- injected-fake backend ID/version and CPU/offline authority;
- prompt, input schema, output schema, configuration, input, backend output,
  parsed output, admitted output, rubric, and state hashes as stages permit;
- typed status/fallback and bounded validation checks;
- backend calls and cumulative task-start/revision/tracking/link call census;
- packet/backend/admission/state-update/total latency, including elapsed
  backend time on exception, timeout, or rejected output;
- milestone/path/frontier/relevance/archive/unknown census; and
- constant false network/model/GPU/action/mutation/persistence attestations.

The CPU/injected-fake checkpoint makes no model call, so model identity is
`NOT_APPLICABLE`; `model_call_attempted=false` is the mechanical provenance
record. A live backend and requested/returned model fields require a versioned
R2.4 receipt extension and separate owner authorization.

Only `TRACK` has a checked-in backend-input schema in this checkpoint, namely
the history-free tracking-packet schema. Task-start, revision, and deterministic
link inputs remain module-owned typed projections without separate JSON Schema
files, so their receipt `input_schema_sha256` is absent rather than falsely
reusing an output-schema hash. Every stage still binds its exact input hash and
its actual rubric/tracker output schema.

The receipt stores no task text, screenshot, backend output, request view,
credential, provider header, reasoning summary, or chain of thought. Graph and
state payloads needed for later tracking belong to a separate derived rubric
state store, never raw Collector events. The CPU checkpoint uses an in-memory
transactional store/sink. Receipt or state commit failure discards the update,
preserves prior state, and emits no archive proposal.

Receipt input, prior-state, and final-state hashes bind the private authority
snapshots fixed before the backend call and the independently admitted final
state. They never bind a backend-mutated call copy or a caller-mutated public
result.

Metrics use only closed operation/status/state/path/relevance/stage labels.
Task IDs, record IDs, logical-call IDs, raw reasons, or text are not metric
labels. Runtime counts cover generation, revision, tracking, linking, cache
reuse, unknown/abstention, the reserved archive census (zero without a
resolver), and latency.

Invented requirement, false completion, legal-alternative false deviation,
and false archive are measured only against explicit offline fixture/pilot
labels with denominators. The rubric is never its own safety oracle. No
task-success improvement or causal-effect metric is exported by R2.3.

## 11. CPU/offline acceptance

Acceptance requires deterministic injected-fake tests proving:

1. at-most-once task-start generation, frozen reuse, concurrency convergence,
   and no per-step regeneration;
2. exact Unicode character/byte/hash binding and rejection of span drift,
   overlap, unbound spans, unanchored hard requirements, graph cycles, unknown
   references, and unreachable nodes;
3. exact revision version/hash/requirement/node delta enforcement;
4. simultaneous viable alternative routes and a permanent OTHER/unknown route
   without a preferred click sequence;
5. history-free packet construction and recursive absence of actor history,
   History IR, future/outcome/checker/replay data, and raw-event mutation;
6. ambiguous GUI state becoming `unknown`, and weak transition status not
   settling semantic completion/violation;
7. memoized shared-DAG path/frontier computation, independent recomputation,
   and state hash/CAS binding;
8. all four relevance classes, unconditional `RETAIN` without a trusted R2.2
   resolver, non-authority of arbitrary support hashes, and rejection of
   `ARCHIVE_SHADOW`;
9. closed-schema rejection of factual authority, `DROP`, `REPLACE`, active
   `ARCHIVE`, next-action, tool/action, and unexpected fields;
10. actor-visible state disablement without changing rubric/relevance/history
    configuration;
11. explicit joint non-independence and prohibition on silently replacing the
    isolated result;
12. private pre-call task/request/packet/state snapshots and hashes, a separate
    detached backend input for generate/revise/track, and rejection of backend
    attempts to mutate or rebind input/CAS state;
13. fresh detached public properties, packet/revision outputs, operation
    results, adapters, and cache hits whose mutation cannot alter session or
    cache authority;
14. bounded trusted traversal with shared-DAG acceptance and typed cycle,
    depth, and node-budget fallback without an escaping `RecursionError`;
15. exact trusted output snapshots, complete finite rejected-output hash
    binding, typed fallback, transactional receipt/state behavior,
    schema/projection parity, and affected CPU regressions; and
16. zero live provider/model/network/GPU/backend/GUI/tool/action/replay use.

Passing establishes only a CPU/offline/injected-fake, SHADOW-only rubric
engineering checkpoint. It does not establish semantic accuracy, live latency
or cost, safe active archive, production readiness, task-success improvement,
or causal effect.

## 12. R2.4 handoff

R2.4 must separately add and authorize real Qwen/MAI task/current-GUI/
transition plumbing, external state/receipt resources, any live replaceable
backend transport, cancellable attempt receipts, secrets authority, a trusted
record-level R2.2 `SUPPORTED + KEEP` resolver before any `ARCHIVE_SHADOW`, and
the joint-versus-isolated runtime comparison. It must decide the call topology
from measured latency and safety without relabeling a joint result as
independent.

Until then, missing live inputs are availability facts, not permission to use
actor history as truth, invoke a provider, mutate an actor request, or execute
ARCHIVE.
