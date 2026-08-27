# G1 Replay Capsule Contract v1

Status: **ACCEPTED for ALE-321 / G1.3**
Document type: Normative materialization, validation, and publication contract
Contract ID: `mobileworld.g1.replay-capsule/contract-v1`
Authorization: `DECISION_LOG.md` D-023
Depends on: the locked G1.1 protocol and registry and the accepted G1.2 portable Sentinel contract
Decision date: 2026-08-27 UTC

## 1. Decision

G1.3 converts each frozen, preregistered G1 decision unit in scope into either one immutable,
self-validating ReplayCapsule or one deterministic exclusion. A ReplayCapsule is the complete
frozen scientific input later consumed by the replay harness. It binds the exact captured
application-layer request and its causal pre-call context while placing natural post-call evidence
behind a sealed audit boundary.

Materialization is an offline, CPU-only, read-only derivation from Collector v1 and the G1.1
registry. It does not render a treatment, invoke a provider, run a model, use a GPU, restore an
environment, replay a prefix, execute an action, or modify raw data. All capsule records set:

```json
{"curated": true, "deployment_prediction": false}
```

The keywords **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative in this document.

## 2. Authority and frozen dependencies

The following contracts remain authoritative and byte-frozen:

- `G1_CAUSAL_REPLAY_PROTOCOL_V1.md` and `G1_LOCKED_ANALYSIS_PLAN_V1.md`;
- all 25 G1.1 contract files listed by
  `g1/source_registry_inputs.v1.json`, the repository registry lock, and the six-file external
  publication identified by that lock;
- `G1_PORTABLE_SENTINEL_CONTRACT_V1.md`, its five G1.2 schemas, and the accepted
  `mobile_world.offline.causal_replay` interfaces and conformance fixtures;
- Collector v1 event semantics, artifact serialization, raw events, blobs, manifests, and
  integrity reports.

The G1.1 field named `frozen_capsule` is a pre-gold identity and source-binding summary. A G1.3
ReplayCapsule is a new, richer derived record. It MUST bind the G1.1 record and its existing
`frozen_capsule.capsule_sha256`; it MUST NOT rewrite that record, reuse that digest as the G1.3
body digest, or imply that the G1.1 summary already contained the complete G1.3 artifact.

The legacy G1.1 capsule digest includes the captured natural-action hash in its historical identity,
and the G1.1 task-stream hash covers the complete stream, including post-request events. Both are
valid source-integrity bindings but are future-bearing. They therefore live only in sealed
`source_provenance`/integrity bindings and MUST NOT appear in `runtime`, `ACTION_GOLD`, or
`TRANSFORMATION`. Pre-cutoff runtime evidence is bound by its exact event-line/artifact hashes and
request cut-off, never by dereferencing the full future-bearing stream as a runtime input.

Collector `request_view` is an inspectable logical projection. The authoritative request source is
the Collector `sdk_arguments_snapshot_blob` artifact graph plus all of its content-addressed
leaves. G1.3 MUST independently rehydrate that graph and verify it against `request_view`; it MUST
NOT promote a pretty print, flattened prompt, audit card, or reconstructed history string to the
authoritative request.

## 3. Frozen target population and accounting

The G1.3 target population is exactly 190 frozen G1.1 units:

| Population | Input status | Count | G1.3 disposition |
| --- | --- | ---: | --- |
| Strict-MHR intervention candidates | `CANDIDATE_FROZEN` | 152 | exactly one valid capsule or one stable exclusion |
| Selected clean controls | `SELECTED` | 38 | exactly one valid capsule or one stable exclusion |
| Reserve clean controls | `RESERVE` | 38 | out of G1.3 scope; census-only, no capsule and no exclusion |

The 152 strict units comprise 139 Qwen `flat_progress` decisions and 13 MAI `raw_replay`
decisions. The 38 selected controls comprise 30 Qwen and 8 MAI decisions. G1.3 MUST derive this
census from the hash-locked registry bytes rather than from hard-coded row numbers, while the
expected counts above remain required invariants.

Every in-scope `case_id` or `control_id` MUST appear exactly once in the output manifest's
`materialized` or `excluded` partition. It cannot appear in both, appear twice, or disappear.
Thus:

```text
target_count = materialized_count + excluded_count = 190
reserve_count = 38
```

Reserve controls MUST be recorded only in the manifest's input census and out-of-scope count.
Turning a reserve control into a capsule or an exclusion changes the preregistered population and
is forbidden without a new protocol version. G1.3 materialization also does not change a
`CANDIDATE_FROZEN` row to G1.6 `INCLUDED` or `EXCLUDED`; those are later admission states.

## 4. ReplayCapsule envelope and outer body hash

### 4.1 Envelope

Each emitted capsule is one canonical JSON object with this outer shape:

```json
{
  "schema_version": "mobileworld.g1.replay-capsule/v1",
  "record_type": "g1_replay_capsule_envelope",
  "capsule_body_sha256": "<64 lowercase hexadecimal characters>",
  "capsule": {}
}
```

All scientific content, including unit identity, source bindings, the pre-cutoff sections, the
sealed audit suffix, and their internal projection hashes, lives inside `capsule`. The outer
`capsule_body_sha256` is:

```text
sha256(canonical_json_bytes(capsule, newline=false))
```

Canonical JSON is the G1.2 encoding: UTF-8; keys sorted lexicographically; compact separators;
non-ASCII characters preserved; non-finite numbers forbidden; no trailing line feed. The emitted
capsule file is the canonical outer object followed by exactly one LF. The manifest separately
binds the exact capsule-file SHA-256 and byte count.

The `capsule` body MUST NOT contain a copy of `capsule_body_sha256`, and no inner fragment hash may
substitute for the outer body hash. A validator MUST recompute the body hash from parsed content,
recompute the whole-file hash from exact bytes, and require both. The G1.1
`frozen_capsule.capsule_sha256`, the
Collector request-view hash, the canonical SDK-request hash, the runtime-projection hash, and the
sealed-audit hash are distinct named digests with distinct subjects.

### 4.2 Required body topology

The `capsule` body has these schema-level roots and three ordered conceptual zones:

```text
unit + source_provenance                        # identity/provenance
runtime                                        # pre-cutoff runtime projection
  model_visible
  non_history_envelope
  treatment_surface
curator_only                                   # pre-cutoff curator projections
  action_gold
  transformation
post_action_audit                              # sealed audit suffix
field_visibility + artifact_closure
integrity_binding + safety
```

JSON object key order has no semantics, but this topology is structural: the later renderer may
receive only the allowlisted `runtime` roots; curator exports may receive only their named
`curator_only` projection; no renderer or curator accessor may traverse `post_action_audit`. The
outer body hash covers all zones so that moving evidence across the boundary changes capsule
identity.

The body MUST include independent hashes of the runtime projection, each curator projection, and
the sealed audit suffix. Those hashes are defense in depth and do not replace
`capsule_body_sha256`.

## 5. Exact source identity and provenance

`identity_and_provenance` MUST bind, without aliases as substitutes:

- the G1.1 registry publication ID, registry filename, exact line/record hash, and source
  `case_id` or `control_id`;
- unit kind (`STRICT_MHR` or `CLEAN_CONTROL`), its frozen registry state
  (`CANDIDATE_FROZEN` or `SELECTED`), source key, study role, and population position;
- source dataset ID, source run ID, task run ID, raw task-stream relative path and SHA-256;
- target step, step ID, request-event ID and sequence, request ID, model-call ID, decision-event
  ID, monotonic timestamp, wall-clock timestamp, and the exact request cut-off;
- host adapter, history family, model identity and revision, provider application protocol,
  decoding/sampling configuration, production parser identity, and all applicable manifest or
  implementation hashes;
- task name and catalog index, exact model-visible instruction reference/hash, the frozen complete
  task-parameter hash, and explicit provenance for any available parameters;
- environment, container/image, Android/emulator, APK, backend, and source-code provenance that
  was actually captured.

Missing historical provenance MUST NOT be invented. A non-required provenance field may use an
explicit typed `UNAVAILABLE_FROM_CAPTURE` state with its source manifest reason. Missing information
that is required to reconstruct the request, prove state identity, resolve the target, or choose
the state-access mode is an exclusion, not an `unknown` value silently accepted as valid.

Absolute machine paths are locator configuration, not scientific identity. Published capsules
use content-addressed source-root IDs and safe relative paths. They MUST NOT embed API keys,
authorization headers, credentials, host-specific temporary paths, build times, process IDs, or
other non-deterministic machine state.

## 6. Exact application-layer SDK request

### 6.1 Meaning of exact request

For G1.3, the exact request is the semantic application-layer argument mapping MobileWorld passed
at the last observable boundary before the provider SDK invocation. It includes ordered messages,
roles, content parts, text, images, tool schemas/calls/results, model identity, and captured
sampling/decoding fields. It is **not** an assertion about a later SDK-generated HTTP body, HTTP
headers, SDK-internal retry representation, or wire serialization.

The capsule MUST bind all of the following independently:

1. the exact raw `model_request` physical JSONL-line bytes and `event_line_sha256`, together with
   the containing task-stream hash;
2. the Collector `sdk_arguments_snapshot_blob` artifact-graph reference, graph bytes/hash, graph
   version, and every transitive leaf reference;
3. the rehydrated semantic request under the Collector serializer, its serializer/version, and
   `sdk_arguments_canonical_sha256`;
4. the exact captured `request_view`, its canonical SHA-256, and validation that it is the
   inspectable projection of the authoritative graph;
5. every request image's request path, original text/data-URL reference when present, exact image
   bytes/hash/length/media type, and dimensions when captured.

`event_line_sha256` hashes the exact physical event line including its single terminating LF and
therefore binds the complete Collector event envelope and payload, not merely its `messages`
child. The ReplayCapsule outer `capsule_body_sha256` then binds that source hash together with the
rest of the capsule. Both levels are required.

Every referenced artifact MUST be a regular file under its declared immutable source root, MUST
have the declared byte count and SHA-256, MUST rehydrate without a dangling or cyclic reference,
and MUST reproduce the canonical semantic request. Large images and other binaries remain
content-addressed outside Git; a capsule may bind them by verified reference and need not copy
their bytes inline.

Integrity is evaluated over the selected unit's complete transitive artifact closure. A run-level
incomplete marker caused solely by an unrelated task does not exclude an otherwise complete unit;
conversely, a complete run flag cannot excuse a missing referenced artifact. The capsule binds any
real source validation receipt that exists and always emits its own G1.3 integrity result. It MUST
NOT invent or claim a per-run Collector integrity report that the source did not publish.

### 6.2 Original form and semantic recoverability

The original artifact graph and event bytes are preserved by hash/reference even when the
rehydrated semantic request is also stored as a canonical derived artifact. The capsule MUST make
both the captured serialized form and semantic request recoverable. Canonicalization may remove
irrelevant object-serialization variability only under the versioned Collector serializer; it
MUST NOT normalize roles, reorder messages or keys whose order is semantic, flatten multimodal
blocks, re-encode images, drop provider fields, or rewrite text.

G1.3 does not add the future experimental replay `seed`. The captured request remains exactly the
natural pre-call request. Adding the preregistered seed equally to every arm is a later replay-
envelope operation governed by G1.1 and G1.4/G1.7, not capsule materialization.

## 7. Five visibility classes and structural isolation

Every data-bearing field offered to a scientific consumer, and every leaf or exact slice of the
semantic request, MUST have exactly one of these visibility classes:

| Class | Meaning | May reach a later model request after a separately authorized codec/guard? |
| --- | --- | --- |
| `FROZEN_MODEL_VISIBLE` | exact captured model-visible bytes that no history treatment may alter, including system/task/current-observation/tool-protocol or other protected content | yes, unchanged only |
| `FROZEN_NON_HISTORY_ENVELOPE` | fixed non-history request/configuration fields such as model, sampling, parser, host envelope, and protected transport semantics | yes, unchanged except later schema-enumerated paired replay controls |
| `MUTABLE_HISTORY_TREATMENT` | the exact active-history region and hash-bound records in which a later curated plan may name edits | yes, only through a validated later G1 plan |
| `CURATOR_ONLY` | offline evidence, lineage, hidden metadata, or curation projection that is never an automatic renderer input | no |
| `POST_ACTION_AUDIT_ONLY` | target response/action/result/post-state or any later/outcome/checker evidence | never |

In G1.3 every class has `direct_provider_input=false`. The first three may be consumed only by the
separately authorized History Codec, replay harness, or protocol validator named in the policy;
that later consumption is not a send authorization.

The class map is coordinate based. It MUST bind JSON Pointer/path, container hash, and, for a
co-located string, half-open Unicode-code-point and UTF-8-byte ranges plus exact slice hash. Every
semantic-request leaf is covered exactly once by one of the first three classes. Exact aliases or
references in curator/audit sections point back to that ownership; duplicated bytes do not create
a second modifiable copy.

The outer `unit`, `source_provenance`, `field_visibility`, `artifact_closure`,
`integrity_binding`, and `safety` objects are verification metadata, not a sixth
payload-visibility class. Only the protocol or audit validator may consume them; the visibility
policy MUST list all six as `validator_metadata_roots` and `forbidden_runtime_roots`. They MUST
NOT be renderer, harness, curator, or direct-provider input roots. Any data-bearing artifact they
reference is classified at its consumer-facing root under the five classes above.

Pre-cutoff event references exposed under `runtime` or either curator channel bind the exact
event-line hash and a prefix-through-event hash only. The full task-stream hash covers the natural
response and later suffix, so it is future-bearing metadata and MUST remain under
`source_provenance` or `post_action_audit`; neither its field name nor its digest value may appear
in a runtime or curator projection.

`MUTABLE_HISTORY_TREATMENT` marks a structural region, not an approved edit. G1.3 MUST NOT infer a
claim, choose a target set, correction, oracle removal, or sham span. A future G1.6 plan may edit
only its independently sealed exact targets inside that region. Untargeted bytes remain protected.

The runtime projection is an explicit allowlist consisting only of the authoritative semantic
request, its first-three-class coordinate map, model/provider/decoding bindings, state-access
descriptor, and integrity hashes needed by a later preflight. It MUST NOT contain a convenience
pointer to the whole capsule body, curator projections, eligibility review text, or audit suffix.

The `runtime.non_history_envelope.replay_binding` is the self-contained, hash-bound projection a
later harness is allowed to consume. It includes the host adapter/component, served model and
revision, model-manifest and model-record hashes, SDK method/version, endpoint, stream mode, the
exact decoding-configuration artifact, and the production parser binding/artifact. These values
MUST equal their validator-only `source_provenance` aliases. Captured transport fields explicitly
excluded by Collector remain guard inputs with `excluded_transport_fields_send_eligible=false`;
they are never application arguments to send. Actor/prompt implementation metadata remains
validator/codec compatibility provenance and is not an instruction to rerun prompt construction.

## 8. Semantic region partition

The pre-cutoff runtime projection MUST identify these schema regions independently:

- `SYSTEM`;
- `TASK`;
- `HISTORY`;
- `CURRENT_OBSERVATION`;
- `TOOL_PROTOCOL`;
- `PROVIDER_CONTROL`.

Each region records `PRESENT`, `COLOCATED`, or `ABSENT_NOT_IN_HOST_CONTRACT`, its exact request
path(s), visibility class, source coordinates/hashes, ordering, and any relationship invariants.
The ownership partition for `SYSTEM`, `TASK`, `HISTORY`, `CURRENT_OBSERVATION`, and
`PROVIDER_CONTROL` uses non-overlapping hash-bound leaves or slices. `TOOL_PROTOCOL` is a protected
semantic overlay: when the host contract embeds tool protocol in `SYSTEM` or tool calls/results in
`HISTORY`, it MAY share those exact bindings or slices. That overlay records a relationship only;
it does not create a second visibility owner, duplicate request content, or make the protected
content editable. All non-`TOOL_PROTOCOL` co-located slices remain non-overlapping. The builder
cannot classify an entire container as history when it also contains task, current-state, system,
or tool text.

The partition MUST preserve roles, message/content ordering, multimodal blocks, tool-call/result
adjacency, assistant-message identity, host-required shells, and the unique current observation.
All non-history request leaves must be recoverable and provably unchanged. An absent region is
explicit and cannot be synthesized. Any overlap other than an exact declared `TOOL_PROTOCOL`
overlay, any gap in the ownership partition, multiple plausible history partitions, or loss of a
protected non-history slice is an exclusion.

The current-state binding includes the exact current screenshot used in the request, UI tree or
accessibility snapshot when captured, observation/state hash, capture timestamp, and the last
causally available completed transition at or before the request cut-off. When the source
contract explicitly contains no UI tree, the capsule records `ABSENT_IN_CAPTURE`; when the source
cannot establish whether one existed, it records `UNAVAILABLE_FROM_CAPTURE`. It MUST NOT
reconstruct one from a later screen. The target action's transition and `S_(t+1)` are never the
last pre-cutoff transition.

The current screenshot is selected by its host-declared or structurally unique current-block
coordinate and then verified by digest. Digest equality alone is not a selector because an earlier
history screenshot may have identical pixels. Every same-digest occurrence at another request
path remains recorded as a non-current occurrence.

## 9. Target exposure and MAI pending-span rule

Every capsule MUST bind each G1.1 target-history record to the request actually sent using:

- candidate ID and frozen registry binding;
- request, message, content-block, and representation-record coordinates;
- exact source record text/hash;
- half-open character and UTF-8 byte spans resolving to identical bytes;
- source/exposure step, provenance confidence, container hash, and stable record identity;
- proof that the target lies within `HISTORY` and was model-visible at the target request.

Resolution is exact and unique. Fuzzy search, best-match relocation, whitespace normalization,
first-match selection, whole-record fallback, or moving a target to a nearby record is forbidden.
Multiple frozen audit premises sharing one provider call remain one capsule with one ordered
binding set; they are not duplicate units.

For Qwen `flat_progress`, G1.3 preserves the G1.1 conclusion span, exact enclosing `Step N`
record, and their hashes. It does not expand the target.

For every MAI `raw_replay` strict case or selected control, G1.3 MUST preserve:

```text
edit_span_status = G1_6_PENDING
focal_edit_spans = []
treatment_plan_present = false
treatment_execution_ready = false
```

The complete assistant reply remains the frozen exposure record. A uniquely parsed `<thinking>`
element may remain a `NON_EDITABLE_G1_6_CURATION_ENVELOPE`; it is not a premise span and cannot be
sent to the G1.2 renderer as an executable edit. Only independent G1.6 curation may freeze the
smallest semantically self-contained premise span while protecting `<tool_call>` and valid action
bytes. G1.3 MUST reject any MAI source row or capsule that fabricates a focal edit span or marks a
treatment execution-ready.

## 10. Pre-cutoff evidence and curator channels

### 10.1 Cut-off

The frozen request event is the visibility cut-off. Every pre-cutoff evidence reference records
its source event ID, event sequence, role, exact projection path, content hash, and proof that its
sequence is no later than the request event. Content that becomes available only in the target
response, target action, target transition, `S_(t+1)`, a later step, task termination, evaluator,
checker, failure-link review, outcome, or replay response is forbidden from all pre-cutoff and
curator projections.

Eligibility lineage may bind the frozen natural target prediction/action because G1.1 needed
explicit uptake and parseability to select the already frozen population. That lineage is
validator-only `unit`/`source_provenance` metadata, is unavailable to both curator channels, and
can never become a runtime or treatment input.

### 10.2 `ACTION_GOLD`

The `ACTION_GOLD` projection is a content-hashed allowlist for later independent accepted-action
curation. It MUST contain at least:

- exact model-visible `task_instruction`; and
- exact target-pre current GUI evidence (`target_pre`).

It MAY contain pre-cutoff, non-history accessibility, tool-response, or ask-user evidence whose
provenance proves model visibility at or before the request. It MUST NOT contain model-visible
history, the misleading target, full hidden task parameters, source reasoning/action/post-state,
the natural target response/action, later evidence, outcome, or checker/evaluator data.

### 10.3 `TRANSFORMATION`

The `TRANSFORMATION` projection is a separately content-hashed allowlist for later independent
history-plan curation. It MUST contain at least one exact `source_history` reference and MAY
contain task instruction, target-pre GUI, and other eligible pre-cutoff evidence needed to define
Mask, correction, Oracle-clean, or sham candidates. It MUST NOT contain the natural target
prediction/action, target result/post-state, later evidence, task outcome, evaluator/checker data,
failure-link material, accepted-action predicates, or replay responses.

The two channel manifests are independent. A reference cannot be copied from one channel merely
because it is available in the other; each must satisfy its own allowlist. Neither channel contains
a transformation decision or action gold in G1.3. Their status is `G1_6_PENDING`, and their role
is to freeze eligible inputs for the later identity-disjoint G1.6 reviews.

## 11. Sealed post-action audit suffix

The `sealed_audit_suffix` retains natural-trajectory evidence for provenance and later audit only:

- the captured original provider response/attempt records, raw and normalized response artifacts,
  parser input, and their hashes;
- the captured production-parsed natural action and parser identity/hash;
- the executor/tool/user result and transport evidence;
- the target transition disposition and post-state screenshot/UI tree/state hash when executed;
- an explicit typed absence when the source contract validly records `transition_not_executed`;
- task-ended, evaluator/checker, score, later-step, or outcome references if retained at all.

Every suffix item is `POST_ACTION_AUDIT_ONLY`, binds its source event ID/sequence and exact
artifact hashes, and is physically outside the runtime and curator projections. The natural action
is a descriptive reference, not an expected replay result, gold label, invariance requirement, or
model input. Stochastic agreement with a future seeded `ORIGINAL` is measured separately.

The suffix has its own canonical SHA-256 and boundary receipt. The validator MUST prove that no
suffix object, hash-resolving convenience accessor, duplicated value, or derived field is reachable
from the runtime projection, `ACTION_GOLD`, or `TRANSFORMATION`. A whole-body pointer is forbidden
in those projections. Sealing is structural isolation, not encryption; access control and
restricted storage remain required.

Reading post-cutoff events to populate this suffix MUST occur only after population membership and
the request cut-off have been resolved from frozen G1.1 bytes. Post-cutoff content cannot influence
whether a unit is targeted, how a pre-cutoff region is partitioned, how a target is resolved, which
state-access mode is chosen, or which primary exclusion code is emitted for a pre-cutoff failure.

## 12. State-access descriptor union

Each capsule declares exactly one state-access mode:

### `SERIALIZED_REQUEST_ONLY`

Use only when the frozen model/config manifest and source call prove that next-action generation
depends exclusively on the rehydrated application-layer request and its embedded request images.
The descriptor records `backend_dependency=NONE`, the proof/source-manifest hash, and that no
checkpoint or prefix replay is required. The captured Qwen and MAI G1 calls are expected to use
this mode, but G1.7 must independently re-prove the claim before execution.

### `EXACT_CHECKPOINT`

Use when a model call can consult live external state and an exact restorable state exists. The
descriptor binds the checkpoint ID, bytes/reference/hash, environment/container/APK/backend
provenance, restoration contract/version, and required state-equivalence checks. G1.3 records the
descriptor only; it does not restore or test the checkpoint.

### `DETERMINISTIC_PREFIX_REPLAY`

Use when no exact checkpoint exists but a deterministic prefix recipe is the authorized state
source. The descriptor binds the initial-state reference, ordered prefix actions/results, all
artifact hashes, environment versions, termination boundary, and the exact pre-provider
state-equivalence checks required for later admission. G1.3 does not run the prefix or execute any
action.

The three variants are mutually exclusive. A live-state dependency with neither a complete
checkpoint nor a complete deterministic-prefix recipe is excluded as
`BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING`. A `SERIALIZED_REQUEST_ONLY` assertion without
manifest-bound proof is excluded as `BACKEND_DEPENDENCY_UNPROVEN`. A malformed prefix is excluded
as `PREFIX_REPLAY_RECIPE_INVALID`; an absent or invalid required checkpoint is excluded under the
checkpoint code above. The builder cannot downgrade a live dependency to
`SERIALIZED_REQUEST_ONLY` to retain a unit.

## 13. Deterministic builder and validator

The builder MUST:

1. verify the repository lock and exact external G1.1 registry file set before reading rows;
2. select exactly the frozen 190-unit population without outcome-dependent filtering;
3. open all source evidence read-only and reject paths outside their declared roots, symlinks,
   non-regular files, or hash/length mismatches;
4. parse only the raw prefix needed to establish the target request and pre-cutoff sections, then
   populate the separately sealed suffix without feeding it back into earlier decisions;
5. rehydrate and rehash every transitive request artifact and current-state artifact;
6. resolve the semantic partition and all target coordinates uniquely;
7. construct channel-specific projections and prove their allowlists and cut-offs;
8. validate the state-access union, five-class coverage, schema, outer body hash, and source
   immutability;
9. emit deterministic capsule, manifest, integrity, and exclusion bytes.

The validator MUST rebuild material identities from source evidence; it does not trust
caller-supplied region maps, visibility tags, target coordinates, hashes, channel membership, or
state-access claims. Validation never repairs an input silently. A valid capsule still has
`execution_ready=false`, `provider_invocation_allowed=false`, and
`treatment_response_generation_allowed=false`.

Directory validation has two explicit scopes. `STRUCTURAL_ONLY` proves canonical bytes, schemas,
content addressing, exact file closure, and internal hashes but sets `source_bound_valid=false`
and is not a formal publication receipt. `SOURCE_BOUND` additionally rebuilds the formal artifact
set from the frozen repository, registry, and Collector roots and requires byte identity; only it
may set `formal_publication_valid=true`. The publisher MUST perform this source-bound comparison
before installation and a source-bound readback after the atomic install.

No formal output may depend on traversal order, hash-map order, current time, locale, hostname,
absolute source root, process ID, temporary directory, file mtime, concurrency schedule, or error
discovery timing. Lists use contract-defined canonical order; sets are sorted by stable IDs and
hashes.

## 14. Stable exclusions

The G1.3 exclusion vocabulary is closed:

```text
REGISTRY_BINDING_INVALID
SOURCE_REFERENCE_UNRESOLVED
SOURCE_HASH_MISMATCH
RAW_EVENT_CHAIN_INVALID
BLOB_REFERENCE_INVALID
BLOB_MISSING
BLOB_HASH_MISMATCH
ARTIFACT_REHYDRATION_FAILED
REQUEST_HASH_MISMATCH
REQUEST_VIEW_MISMATCH
REQUEST_PARTITION_AMBIGUOUS
REQUEST_PARTITION_INCOMPLETE
NON_HISTORY_REGION_UNRECOVERABLE
STATE_HASH_MISMATCH
CURRENT_OBSERVATION_UNRESOLVED
CURRENT_SCREENSHOT_EXPOSURE_UNRESOLVED
TARGET_SPAN_UNRESOLVED
TARGET_SPAN_AMBIGUOUS
TARGET_SPAN_HASH_MISMATCH
TARGET_SPAN_COORDINATE_MISMATCH
TARGET_SET_OVERLAP
ORIGINAL_RESPONSE_UNRESOLVED
ORIGINAL_ACTION_UNRESOLVED
ORIGINAL_TRANSITION_UNRESOLVED
BACKEND_DEPENDENCY_UNPROVEN
BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING
PREFIX_REPLAY_RECIPE_INVALID
FUTURE_EVIDENCE_LEAKAGE
FIELD_VISIBILITY_INVALID
CURATOR_CHANNEL_VIOLATION
SCHEMA_VALIDATION_FAILED
DUPLICATE_CAPSULE
CAPSULE_HASH_MISMATCH
NONDETERMINISTIC_BUILD
```

Semantically identical G1.1 reason names are reused. Natural success/failure, harm, failure-link
classification, captured action content, and any future replay behavior are never reasons.

Each exclusion row binds the source unit ID, registry record hash, validator/contract identity,
one primary code, deterministic affected references/coordinates, and a stable human-readable
detail that contains no machine-local path or volatile text. When multiple checks fail, the
primary code is selected by the builder stage order in Section 13; remaining failures MAY be
sorted secondary diagnostics. Repeated builds MUST emit the same primary reason and exact row
bytes.

An excluded unit emits no partial capsule. A builder cannot invent a placeholder request, state,
span, checkpoint, prefix, or audit record; copy a nearby artifact; relax a hash; or silently move
the unit to reserve. Exclusions are G1.3 materialization dispositions only and do not rewrite the
G1.1 registry or pre-empt the separately governed G1.6 admission ledger.

## 15. Double-build and conformance

Two clean builds from the same immutable inputs and tool/schema versions MUST produce:

- the same exact output file set;
- byte-identical capsule files, manifest, integrity report, and exclusion ledger;
- identical outer body, runtime-projection, curator-channel, suffix, and aggregate hashes;
- the same 190-unit materialized/excluded partition and stable reason codes.

At minimum, conformance MUST prove:

1. all 190 target rows and only those rows are accounted for;
2. all 38 reserve controls remain out of scope;
3. every transitive blob exists, rehydrates, and matches length/hash;
4. the semantic SDK request, original artifact graph, request view, and non-history regions are
   recoverable and mutually consistent;
5. Original request semantics are unchanged and no replay seed was inserted;
6. every target resolves uniquely to active history with matching character/UTF-8 coordinates;
7. MAI focal spans remain `G1_6_PENDING` and non-executable;
8. the runtime and two curator projections contain no forbidden future/audit fields;
9. captured natural output is reachable only through the sealed audit suffix;
10. state-access descriptors form a valid exclusive union;
11. caller-owned parsed objects and all raw source bytes remain unchanged;
12. missing, ambiguous, or corrupted inputs produce exact stable exclusions rather than repair;
13. no provider/model/GPU/action/network execution path is reachable from the builder or
    validator.

Negative fixtures MUST cover hash drift, missing transitive blobs, request-view/artifact mismatch,
co-located-region ambiguity, dual-span disagreement, target duplication, future-evidence leakage,
MAI premature edit materialization, invalid state-access variants, audit-suffix reachability,
duplicate units, and repeated-build stability.

## 16. Manifest, integrity report, and write-once publication

The machine-readable G1.3 contracts live under `mobileworld_audit_handoff/schemas/g1_3/`:

- `replay_capsule.schema.json`;
- `field_visibility.schema.json` with policy version
  `mobileworld.g1.replay-capsule.field-visibility/v1`;
- `capsule_exclusion.schema.json`;
- `capsule_manifest.schema.json`;
- `capsule_integrity.schema.json`.

The versioned manifest MUST bind:

- contract/schema/tool versions and implementation hashes;
- the G1.1 repository lock and six-file external publication identity;
- exact input file hashes and the 152/38/38 census;
- the sorted 190-unit disposition table;
- every capsule relative path, outer body hash, exact file SHA-256, and byte count;
- disjoint `capsule_files` and `artifact_files` lists, with every derived artifact reachable from
  at least one capsule closure and no unlisted or orphan payload;
- the exclusion-ledger and integrity-report hashes;
- aggregate hashes over the exact emitted file set;
- explicit safety flags showing no provider, model, GPU, action, replay, or raw mutation.

The integrity report MUST record all required checks and deterministic counts, including source
lock verification, transitive blob verification, request rehydration, partition coverage, target
resolution, visibility leakage, state-access validity, double-build identity, and before/after
hash equality for every referenced raw stream/blob. A check that was not performed is
`NOT_PERFORMED`, never fabricated as passing.

Real capsules, canonical request artifacts, images, and reports live under a new restricted,
repo-external content-addressed root. Build occurs in a new staging directory outside every raw
root and outside Git. Publication is installed only after complete validation under a
`sha256/<sha256(exact canonical manifest file bytes)>` destination, using an atomic no-replace
operation. The final root
contains only the exact declared regular-file set, no symlinks, and read-only files/directories.

If that destination already exists, the publisher may only verify exact equality and return the
existing publication. It MUST NOT overwrite, delete, chmod, merge into, or repair a mismatching
destination. An amendment or corrected source requires a new version and content address. No
capsule operation writes labels, references, or completion markers into a Collector raw root.

## 17. Explicit non-goals and downstream boundary

ALE-321 / G1.3 does not authorize:

- synthesizing a trajectory, checkpoint, state, evidence item, history record, or missing blob;
- choosing `MASK`, correction, Oracle-clean, or sham content;
- creating accepted next-action predicates or changing G1.6 admission state;
- applying a G1.2 Transformation Plan or rendering any non-Original request;
- implementing or invoking the G1.4 replay runner or a live Provider Codec;
- implementing the G1.5 live Qwen/MAI History Codecs;
- performing G1.6 curation, G1.7 preflight, G1.8/G1.9 experiments, or G1.10 analysis;
- opening a provider connection, loading model weights, using a GPU, executing a GUI/backend/tool
  action, restoring a checkpoint, or running a deterministic prefix;
- automatic claim extraction, truth/relevance inference, correction generation, rubric work,
  runtime prompt interception, runtime fallback, or an online Sentinel;
- modifying actor/runtime behavior, Collector code or semantics, raw events/blobs, the G1.1
  registry, or accepted G1.2 artifacts.

G1.3 only freezes what a later authorized story would need. A valid capsule is not evidence that a
live codec can transform it, a provider can reproduce the natural action, a checkpoint can be
restored, a G1.6 treatment is valid, or the experiment is ready to run.

## 18. Acceptance traceability

| ALE-321 requirement | Contract mechanism |
| --- | --- |
| one-to-one population mapping | Section 3 and manifest disposition partition |
| every blob exists and rehydrates | Sections 6, 13, and 15 |
| exact pre-call request and non-history recovery | Sections 6–8 |
| unique target exposure | Section 9 |
| no future/runtime leakage | Sections 7, 10, and 11 |
| checkpoint or deterministic reproduction | Section 12 exclusive union |
| captured action is reference only | Section 11 |
| deterministic double build | Sections 13 and 15 |
| stable fail-closed exclusion | Section 14 |
| immutable raw Collector evidence | Sections 2, 13, and 16 |
| repo-external write-once publication | Section 16 |
| zero provider/GPU/action execution | Sections 1, 15, and 17 |

## 19. Consequences

The ReplayCapsule becomes the only scientific source object that a later G1 harness may accept.
The harness no longer reconstructs a request, selects a source event, infers a region boundary, or
opens a natural trajectory differently for each arm. Runtime-visible bytes, curator inputs, and
post-action audit evidence are independently hash-bound and structurally separated before any
treatment exists.

This strictness can reduce the eventual sample: a malformed, incomplete, ambiguous, or
non-reproducible unit is excluded rather than repaired. That loss is preferable to silently
changing the paired unit or allowing future evidence into a causal treatment.
