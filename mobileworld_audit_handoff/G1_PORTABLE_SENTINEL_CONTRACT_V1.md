# G1 Portable Sentinel Contract v1

Status: **ACCEPTED for ALE-320 / G1.2**
Document type: Architecture Decision Record and normative contract
Contract ID: `mobileworld.g1.portable-sentinel/contract-v1`
Authorization: `DECISION_LOG.md` D-022
Depends on: locked `G1_CAUSAL_REPLAY_PROTOCOL_V1.md` and the published ALE-319 registry
Decision date: 2026-08-26 UTC

## 1. Decision

G1 uses a portable, evaluation-time transformation contract rather than model-checkpoint-specific
mutation code. The contract has four separable layers:

```text
untouched host-native application request
  -> History Codec extraction
  -> canonical History IR
  -> curated Transformation Plan
  -> Sentinel Core validation and History Codec rendering
  -> protocol validator
  -> Provider Codec interface
  -> derived G1 sidecar
```

The shared core owns identity, coordinates, provenance, transformation semantics, reversibility,
validation, and derived audit output. Thin codecs own history-representation syntax or provider
transport mechanics. A codec is selected by a model-visible history contract, never merely by a
model checkpoint name.

This is the smallest contract needed to unblock later G1 stories. ALE-320 defines CPU-only types,
interfaces, registries, validation, synthetic fixtures, and conformance checks. It does not
materialize a decision capsule, curate a treatment, invoke a provider, use a GPU, execute an
action, or implement a live Qwen or MAI adapter.

The keywords **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative in this document.

## 2. Scope and authority boundary

Every G1 Transformation Plan accepted by this contract MUST set these two provenance flags
exactly to:

```json
{"curated": true, "deployment_prediction": false}
```

The core validates and applies a supplied plan. It MUST NOT extract semantic claims, decide
whether a claim is true or relevant, generate a correction, inspect a task outcome, choose an
arm, or act as an online policy. Structural extraction by a History Codec is not semantic claim
inference.

Collector v1 remains passive, lossless, append-only, label-free, and zero-intervention. History
IRs, plans, rendered requests, labels, diffs, provider results, and sidecars are derived G1 data;
they MUST NOT be written into raw collector events. Real derived data remains outside Git. The
repository may contain only contracts, schemas, code, hashes, non-secret references, and
synthetic or redacted fixtures.

The following work belongs to later stories and is not authorized by this contract:

- G1.3 decision-capsule materialization;
- G1.4 exact-request replay execution and a live Provider Codec;
- G1.5 production Qwen and MAI History Codecs;
- G1.6 gold transformations or accepted next-action sets;
- G1.7+ preflight, treatment responses, analysis, or decision publication;
- automatic claim extraction, verdict prediction, rubric generation/tracking, or runtime gating;
- live prompt interception, GUI actions, task branching, model service use, or GPU use.

## 3. Layer ownership

| Layer | Owns | Must not own |
| --- | --- | --- |
| Sentinel Core / History IR | canonical identities, exact source bindings, plan validation, deterministic operation application, reversible mappings, warnings and hashes | model checkpoints, provider SDK calls, semantic verdict inference, host syntax guesses |
| History Codec | region identification, structural extraction, host-family rendering, shell repair, role/order/multimodal/tool invariants, capability declaration | provider transport, plan curation, correctness prediction |
| Provider Codec | pinned application-layer send arguments, response capture, structured-action/error normalization | history meaning, target selection, mutation decisions |
| Protocol validator and sidecar | pre-invocation invariant checks, exact diff, fallback state, hashes, evidence references, final derived audit record | silent repair, silent Original fallback, raw Collector mutation |

The formal package is `mobile_world.offline.causal_replay`. It MUST NOT import from
`sentinel_mvp`. The legacy package is a behavioral reference only; its migration disposition is
recorded in `G1_SENTINEL_MVP_MIGRATION.md`.

## 4. Canonical History IR

### 4.1 Request identity

A History IR describes one untouched application-layer request and MUST include:

- contract/schema version, host identity, and one frozen history-family ID;
- the untouched request's SHA-256 binding under the contract-versioned canonical JSON encoding;
- explicit task, current-observation, history, system, and tool-protocol regions, each marked
  `PRESENT`, `COLOCATED`, or `ABSENT_NOT_IN_HOST_CONTRACT` rather than silently omitted;
- every non-history region whose equality the protocol validator will protect.

“Request” here means the exact application-layer object captured at the Collector/provider SDK
boundary defined by `EVENT_CONTRACT_V1.md`, not a pretty print, reconstructed prompt string, or
claim about the SDK's later HTTP wire body.

The caller-owned source request accompanies the IR as the immutable rendering input and is copied
into the derived sidecar; the IR binds it by hash rather than owning or mutating it. Canonical JSON
for this contract is UTF-8, key-sorted, compact, non-ASCII-preserving, contains no non-finite
numbers, and has no trailing line feed. A second canonicalization variant is not permitted within
the same contract version.

Extraction and rendering MUST operate on deep copies. The caller-owned request and every nested
value remain unchanged. `ORIGINAL` rendering MUST return a semantically identical request,
preserve every non-history value, and have identical canonical JSON bytes. A codec-proven
semantic equivalence exception must name the exact serialization-only field and may not cover any
model-visible or sampling value.

That History Codec invariant applies before replay-envelope preparation. A later Provider Codec
may add only the G1.1-authorized, preregistered experimental `seed` to every arm in the same
paired block and may vary only schema-enumerated transport-volatiles. The seed value and every
other model/decoding parameter MUST be identical across arms. Neither allowance is a history edit
or a general semantic-equivalence exception.

### 4.2 Records and coordinates

Each canonical record MUST retain all coordinates necessary to recover its exact source:

- stable `record_id` and record hash;
- request JSON Pointer/path;
- original message, content-block, and representation-record indices;
- role and author identity without normalization into a different speaker;
- exact source text;
- half-open Unicode-code-point and UTF-8-byte spans;
- write time, exposure time, and provenance;
- content modality and any blob/hash reference;
- typed relationships to paired tool calls/results, observations, folded source versions, or
  aligned action/conclusion records.

When semantic regions share one host string, each region carries an exact hash-bound text slice
in addition to its request path. This lets the validator protect co-located task/tool text while
editing only the history slice. A region absent from the real host contract has no invented path
or bytes and records an explicit absence reason.

A host that lacks one coordinate or timestamp dimension records the field as explicitly
unavailable; it MUST NOT fabricate a value. Message/block/record ordering is the host-native
ordering, not a core-created ordering.

Stable IDs are derived from immutable host/family identity and exact source coordinates/hashes.
They MUST NOT depend on the order of a future curation file or on the selected arm.

### 4.3 Claims and source spans

Claims are optional curated annotations over records. The core never creates them. Each claim
MUST bind a unique `claim_id`, one record, non-empty character and UTF-8 byte ranges, exact source
text, and source-text hash. Evidence references belong to the curated plan operation that uses
the claim. The two coordinate systems MUST resolve to the same UTF-8 bytes.

Whole-record edits are allowed only when the plan explicitly binds the whole record. There is no
implicit whole-record default. Missing, ambiguous, empty, overlapping, out-of-range, text-
mismatched, or hash-mismatched targets invalidate the treatment. Fuzzy text search or relocation
is forbidden.

### 4.4 Relationships and versioned history

Relationships are first-class IR objects rather than text conventions. At minimum the IR can
represent:

- assistant message to surrounding observation;
- tool call to its tool result, including the host call ID when present;
- action/conclusion to its result or next observation;
- current folded/rolling record to its model-visible source record or content-addressed external
  source-version reference;
- H, L, and M section membership and version lineage;
- current screenshot and the request block that exposes it.

Extraction MUST preserve these relationships. A transformation cannot orphan a host-required
tool result, reorder a pair, or silently merge separately addressable records.

A recursively carried source version that is no longer model-visible is represented by a typed,
content-addressed external source-version reference. It is not reinserted into the current
request merely to make lineage visible to the IR.

## 5. Transformation Plan

### 5.1 Binding and provenance

A plan is an immutable, versioned, curated instruction. It MUST bind:

- the untouched request hash, host ID, history-family ID, and plan ID;
- one protocol arm;
- `curated=true` and `deployment_prediction=false`;
- exact ordered target spans and their record/text/hash bindings;
- hash-bound evidence references supplied by the later frozen curation artifact, whose own gate
  proves availability by the request cut-off;
- any Sentinel-authored correction bytes and their evidence provenance;
- any separately declared delimiter/shell repair with its exact span, hash, and precondition;
- the codec capability/version required to render the plan.

Plan order is canonical. Material edits are reconstructed from their validated, non-overlapping
source coordinates after the complete plan passes validation. No partial plan is applied.
Before any arm can be authorized, the validator also binds the complete paired plan set, requires
unique arms, and proves that `MASK` and `MASK_CORRECTION` use the same focal target set and that
`ORACLE_CLEAN` is its declared superset.

Each `EvidenceRef` has a non-empty immutable ID, lowercase SHA-256, non-empty role, and an
`event_seq` that is either null or a non-boolean non-negative integer. IDs are unique within an
operation, and one ID cannot resolve to different evidence tuples in one plan. G1.2 validates
these bindings only; the later frozen curation/admission artifact proves request-cut-off
availability and channel eligibility.

### 5.2 Canonical operation vocabulary

The portable vocabulary retains five legacy operation names, but their semantics are stricter:

| Operation | Portable meaning |
| --- | --- |
| `KEEP` | preserve the bound source exactly |
| `DROP` | delete only the bound target bytes; do not insert a marker or instruction |
| `REPLACE` | remove the bound target and add the curated correction as explicitly Sentinel-authored context at an exact insertion anchor bound to that target's corresponding semantic-record location |
| `ARCHIVE` | remove a curated inactive-path target from the active view only when a full-transformation codec explicitly supports it |
| `KEEP_UNCERTAIN` | preserve the source exactly and record the non-editing disposition |

These operations do not predict a verdict. `ARCHIVE` is not a synonym for G1 `MASK`, and
`KEEP_UNCERTAIN` is not permission to count a failed treatment as Original.

Within the G1.2 arm projection, `DROP` and `REPLACE` are the only executable material operations,
and `ORIGINAL` has no operations. `KEEP` and `KEEP_UNCERTAIN` remain canonical non-editing
vocabulary/capability dispositions but are not accepted as substitutes for a G1 scientific arm;
`ARCHIVE` is reserved for a future full-transformation contract. Retaining these names does not
authorize their execution in G1.

A correction MUST be the exact curated bytes, MUST cite evidence provenance, and MUST bind an
exact insertion anchor at each corresponding targeted semantic-record location. The anchor MUST
name and hash one concrete current-observation list item and freeze whether insertion occurs
immediately before or after it; a free list index or ancestor-container assertion is invalid. It MUST remain
distinguishable from every historical actor utterance. A renderer MUST NOT attribute it to an old
assistant or otherwise fabricate it as old actor speech. It may not recommend an action, expose
an accepted-action predicate, or use post-target evidence.

### 5.3 G1 arm projection

The locked G1.1 protocol remains authoritative:

| Arm | Required transformation |
| --- | --- |
| `ORIGINAL` | no history edit; exact host-native request |
| `MASK` | pure `DROP` of the complete frozen focal target set, plus only eligible declared empty-shell repair |
| `MASK_CORRECTION` | remove the same focal set and insert only its curated, evidence-grounded Sentinel correction; shell validity is recomputed for this arm |
| `ORACLE_CLEAN` | pure `DROP` of exactly the frozen oracle target set |
| `SHAM_BENIGN_EDIT` | pure `DROP` of exactly the frozen benign span, plus only eligible declared empty-shell repair |

The core rejects an arm/operation mismatch. A text suffix asking the model to ignore prior
content is neither `MASK` nor active-history reconstruction and MUST be rejected.

Paired-plan profiles are independent of a codec's maximum `supported_arms`. `PORTABLE_CORE` is
exactly `(ORIGINAL, MASK, MASK_CORRECTION, ORACLE_CLEAN)` and is restricted to fixture-only
conformance. `G1_STRICT_MHR` is exactly those four arms plus `SHAM_BENIGN_EDIT`;
`G1_CLEAN_CONTROL` is exactly `(ORIGINAL, SHAM_BENIGN_EDIT)`. The validator requires one plan per
arm in this canonical order and separately requires the registry-resolved codec capability to
cover the profile. The digest binds the profile, required arms, registry-resolved codec ID and
contract version, capability digest, and every plan hash. A `LIVE` codec cannot use
`PORTABLE_CORE` to bypass either frozen G1.1 profile.
This is canonical plan-set serialization order, not provider execution order; the frozen G1.1
counterbalanced/run order remains authoritative.

### 5.4 Render result and reversibility

Rendering returns a result separate from both the IR and plan. It MUST contain:

- the final application-layer request and final request hash;
- the untouched request hash;
- every exact before/after edit and any shell repair;
- a reversible source map from each retained output segment to its source record/span;
- a deletion map retaining the exact removed source bytes;
- provenance for every generated Sentinel segment;
- warnings, effective capability, requested arm, execution mode, and fallback state;
- a deterministic output hash.

Reversibility means the stored map and removed bytes can reconstruct the untouched history view
exactly; it does not authorize modifying raw data. The renderer MUST be deterministic and MUST
not mutate the IR, plan, or caller-owned request.

## 6. History Codec contract

A History Codec is keyed by a history-family contract and version. It MUST:

1. identify task, current-observation, active-history, system, and tool-protocol regions;
2. extract canonical records with exact coordinates and relationships;
3. render `ORIGINAL`, and render only those treatment arms listed in its capability declaration;
4. preserve roles, ordering, multimodal blocks, tool-call/result adjacency, current screenshots,
   host-required shells, and every non-history field;
5. declare supported operations, arms, relationship invariants, and fallback behavior;
6. reject or visibly downgrade unsupported mutations according to Section 8.

Every codec must round-trip Original. An unsupported treatment is not passed to a renderer as if
it were supported: scientific mode returns the typed fail-closed result before invocation, while
the future runtime contract may return only the explicit Original fallback defined in Section
8.3.

The codec may perform only syntax repair that the plan declares and the protocol permits. It
cannot infer an alternative target, convert a span edit into a whole-record edit, invent a
correction, or silently reconstruct history through a prompt suffix.

A new model reuses an existing codec when its model-visible application request has the same
history-family contract. A materially different role layout, record grammar, folding lineage,
multimodal policy, or tool protocol requires a new codec or codec version. Model names and
checkpoint IDs are metadata and MUST NOT appear in shared-core branching logic.

## 7. Provider Codec contract

Provider mechanics are separate from history semantics. A Provider Codec interface accepts only
an `AuthorizedProviderRequest` produced by the protocol guard after validation. It owns:

- the pinned provider endpoint/revision identity;
- the exact final application-layer request;
- model and sampling/decoding parameters without arm-specific changes;
- response and attempt capture;
- normalization of one host-structured action or a typed parser/provider/missing error into a
  versioned Provider Result; host action types retain explicit no-op, termination, or refusal
  semantics when present.

It MUST NOT select or render a history arm. Its registry key describes a provider application
protocol, not a model-specific history rule. Unsupported treatment validation occurs before any
Provider Codec method that can cause an invocation.

The guard recomputes the actual encoded-request byte hash and binds the provider codec ID and
contract version, endpoint revision, complete model/sampling-parameter hash, and paired-plan-set
hash. A raw prepared request is not a valid input to the send interface.

ALE-320 supplies only this interface, result schema, registry behavior, and no-call fakes for
tests. A real request sender and production response parser belong to G1.4.

## 8. Capability and fallback contract

### 8.1 Capability levels

Every History Codec declares exactly one level:

| Level | Meaning |
| --- | --- |
| `audit-only` | can identify/extract and reproduce Original, but cannot alter or annotate active history |
| `annotation-only` | can add clearly attributed sidecar/context annotations but cannot safely remove active-history bytes |
| `validity transformation` | can declare semantic support for exact `KEEP`, `DROP`, `REPLACE`, and `KEEP_UNCERTAIN` semantics, subject to an explicit arm allowlist |
| `full transformation` | can additionally declare semantic support for curated `ARCHIVE` and every declared host-family transformation while preserving protocol invariants |

The level is a summary. The authoritative capability object also enumerates supported operations,
supported arms, fixture/live scope, live readiness, opaque/server-managed state, and the required
role/order/multimodal/tool-pair/protocol-shell preservation invariants. Exact spans, correction
anchors, reversible mappings, and shell repairs are validated in the IR, plan, render receipt, and
pre-send receipt rather than inferred from the level. A level alone never implies that a particular
arm is safe.

### 8.2 Scientific execution

`G1_SCIENTIFIC` mode is fail-closed. If history is server-managed, opaque, ambiguously mapped,
unsafe to mutate, outside the codec's declared capability, or invalid under any invariant, the
validator MUST return a machine-readable unsupported reason before provider invocation. It MUST
NOT send Original, invoke a provider, or emit an observation counted as the requested treatment.

An unsupported scientific result records at least the requested arm, codec/capability identity,
reason code, `invocation_attempted=false`, and untouched request hash.

An opaque/server-managed IR may contain zero history records and must mark the history region
explicitly absent. To emit the typed block, the core validates the curated plan envelope and exact
plan-set profile without resolving unavailable target records, then returns
`BLOCKED_BEFORE_PROVIDER`, `effective_arm=null`, a machine-readable reason, and a sidecar with null
provider binding/attempt fields. Invalid provenance, source/codec/version binding, operation
envelope, or plan-set shape still fails validation rather than producing a schema-invalid block.

### 8.3 Future runtime fallback

A future deployment runtime MAY explicitly fail open to Original. That result MUST:

- render an exact Original request;
- set `fallback.state=EXPLICIT_ORIGINAL`;
- retain the requested treatment and machine-readable reason;
- set `fallback.count_as_treatment=false`;
- remain distinguishable from a genuine `ORIGINAL` arm in the sidecar and metrics.

This runtime option is a contract test only in G1.2. It is not authorization to deploy or invoke a
model. It becomes sendable only for a separately authorized `LIVE` codec; fixture-only codecs
remain blocked even when they demonstrate the fallback bytes.

## 9. Six-family compatibility map

ALE-320 freezes fixture-level compatibility for these exact representation IDs:

| Family ID | Reader-facing form | Fixture-level invariant |
| --- | --- | --- |
| `raw_replay` | raw assistant-message replay | preserve assistant-message identity, reasoning/tool payload boundaries, and surrounding observations |
| `flat_progress` | flat task-progress text | edit only the targeted `Step`/progress entry or span; preserve all sibling entries and non-history suffixes |
| `rolling_summary` | recursively carried summary | distinguish the current exposed summary from each source version and preserve its lineage |
| `flat_previous_actions` | flat previous-action trace | preserve action/result ordering when result records are model-visible; otherwise preserve their explicit host-visible absence; always preserve the unique current screenshot |
| `hybrid_folding` | hybrid collapsed action records | preserve aligned action-conclusion/result records and the protocol shell |
| `structured_folding` | structured H/L/M folding | keep H, L, and M sections separately addressable and versioned |

These mappings prove that one canonical IR can represent the six completed observational history
contracts. They are synthetic/redacted conformance fixtures, not production-ready live adapters.
Only `flat_progress` and `raw_replay` are planned as live G1 hosts, and their implementation
belongs to G1.5.

## 10. Registry contract

The History Codec registry maintains a family/version index and a codec-ID entry sealed to one
contract version. A provider-authorizing validator MUST resolve and check
`(codec_id, codec_contract_version)` through the
registry, then independently match the declared family and immutable capability to the
re-extracted IR; family lookup is discovery, not authorization. The Provider Codec registry
separately resolves `(provider_codec_id, provider_contract_version)`. Registration MUST reject
duplicate keys and a declared family/version mismatch.

Neither registry may select by checkpoint substring, infer a fallback family, or silently replace
an unavailable implementation. An unknown family is unsupported, not `raw_replay` by default.

## 11. Protocol validator

The final request validator runs after rendering and before any provider call. It MUST fail closed
when any of the following holds:

- source/request/record/span text or hash binding fails;
- plan provenance flags are not exactly the required curated values;
- plan, IR, codec, host, family, arm, or capability identity disagrees;
- targets overlap, are empty, are ambiguous, were fuzzily relocated, or do not match the sealed
  target set;
- a non-history value, role, ordering relation, multimodal block, tool pair, current observation,
  model field, or sampling field changes, except for the same-block preregistered seed and exact
  schema-enumerated transport-volatiles allowed by G1.1;
- a diff lies outside a target, approved correction insertion, or eligible declared shell repair;
- correction authorship/evidence provenance is absent or correction bytes appear as old actor
  speech;
- Original differs from the captured request beyond an explicitly allowed serialization-only
  equivalence;
- an unsupported scientific treatment would invoke a provider.

Errors are stable machine-readable codes accompanied by a human-readable detail and affected
coordinate. Validation never repairs the request silently.

The validator resolves the codec declaration through the registry, independently re-extracts the
History IR, re-renders the canonical result, rehashes every diff and insertion, checks continuous
copied/deleted source-map coverage, and reconstructs the untouched request from the rendered bytes
plus receipt. It does not trust caller-supplied capability, plan-set hash, receipt hashes, or
coordinates. The paired-plan-set digest is recomputed from the registry-resolved codec identity,
contract version, capability digest, and complete ordered plan set.

For a paired G1 unit, capability and source-binding preflight covers every planned arm before any
arm is eligible for provider invocation. A response from an earlier arm cannot determine whether
a later arm is supported.

## 12. Derived G1 sidecar

The versioned sidecar is the complete audit envelope for one intended invocation. It stores:

- the untouched application-layer request, canonicalization contract, and hash;
- the canonical History IR and hash;
- the curated Transformation Plan and hash;
- the complete canonical paired plan set, its profile, and recomputed digest;
- codec/provider identities and capabilities;
- exact edits, repairs, evidence references, and reversible source mapping;
- final request and hash;
- validator result, warnings, requested/effective arm, and fallback state;
- provider attempts/response and response hash when a later authorized story invokes one;
- parsed structured action or normalized error when available;
- `invocation_attempted`; codec contract versions on capabilities, IR, plan, receipt, and registry
  binding; and provider contract versions on prepared/authorized requests, receipts, attempts,
  and Provider Results.

Real sidecars are derived G1 data outside Git. A sidecar does not mutate, annotate, or supersede
the Collector event that supplied its source. In ALE-320 conformance fixtures,
`invocation_attempted=false` and provider response/action fields are absent or explicitly
not-applicable.

Pre-send validation receipts always describe the state before transport and therefore record
`invocation_attempted=false`. The sidecar has a separate provider-attempt object; when a later
story authorizes transport, that object and any Provider Result must bind the same guarded
request, provider/endpoint, encoded bytes, and model-parameter hash.

## 13. Conformance kit

Each of the six fixture mappings MUST pass the same reusable tests:

1. structural extraction yields stable records, exact dual spans, region identities, and required
   relationships;
2. extract then Original-render is semantically identical to the captured fixture and preserves
   every non-history field;
3. Mask and correction diffs touch only exact targets and separately eligible shell bytes;
4. the reversible map reconstructs exact original history bytes;
5. the caller request, IR, and plan remain unchanged;
6. repeated execution of every arm and fallback yields the same request, diff, mapping, warnings,
   and hashes, while the source request, IR, and plan hashes remain unchanged;
7. correction context is Sentinel-authored and evidence-bound;
8. multimodal and tool-call/result invariants survive every supported arm;
9. unsupported `G1_SCIENTIFIC` treatment fails before a no-call provider fake can be invoked;
10. explicit future-runtime fail-open returns exact Original with visible fallback metadata.

The test kit also checks registry isolation: checkpoint names cannot alter core behavior, and
provider mechanics cannot alter history semantics.

## 14. Versioned artifacts

The Python types and interfaces live under
`MobileWorld/src/mobile_world/offline/causal_replay/`. Their machine-readable counterparts live
under `mobileworld_audit_handoff/schemas/g1_2/`:

- `history_ir.schema.json`;
- `transformation_plan.schema.json`;
- `codec_capabilities.schema.json`;
- `provider_result.schema.json`;
- `sidecar.schema.json`.

These are new G1.2 artifacts. The 25-file ALE-319/G1.1 contract, its 19 schemas, protocol hash,
registry publication, and `g1/registry.lock.v1.json` remain byte-frozen and MUST NOT be edited.

## 15. Acceptance traceability

| ALE-320 acceptance requirement | Contract mechanism |
| --- | --- |
| Original equivalence and non-history preservation | Sections 4.1, 6, 11, and conformance tests 2/8 |
| deterministic, non-mutating, reversible operations | Sections 4.1, 5.4, and conformance tests 4–6 |
| target-only Mask | Sections 5.2–5.3 and validator diff allowlist |
| Sentinel-authored correction with evidence | Sections 5.2 and 11 |
| all six fixture families | Section 9 and shared Section 13 kit |
| no checkpoint logic in core interfaces | Sections 3, 6, 7, and 10 |
| unsupported G1 treatment fails closed | Section 8.2 and conformance test 9 |
| only curated non-deployment plans | Sections 2 and 5.1 |
| explicit legacy compatibility | `G1_SENTINEL_MVP_MIGRATION.md` |
| Collector remains unchanged | Sections 2 and 12 |

## 16. Consequences

The contract requires explicit adaptation for each materially different host-visible history
representation, but that adaptation remains small and testable. Scientific treatments cannot gain
sample size through silent fallback. New codecs can be added without changing canonical
operation semantics, and later provider implementations can change transport libraries without
changing history meaning.

The cost is deliberate strictness: an ambiguous or partially supported transformation is
unsupported until its codec and fixture prove the necessary invariants. That is required for a
causal comparison in which history is the only intended difference.
