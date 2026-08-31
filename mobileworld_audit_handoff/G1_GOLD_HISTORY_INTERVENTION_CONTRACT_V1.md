# G1 Gold History Intervention Contract v1

Status: **AUTHORIZED CPU/manual curation contract for ALE-324 / G1.6**

Contract ID: `mobileworld.g1.gold-history-intervention/contract-v1`

Authorization: `G1_6_DECISION_LOG.md` D-029

Protocol: `mobileworld.g1.causal-replay/protocol-v1`

Date: 2026-08-27 UTC

## 1. Decision and scope

G1.6 turns the 190 frozen G1.3 curator inputs into independently reviewed accepted-next-action
gold, complete history Transformation Plans, arm plans, and a fail-closed admission disposition.
All semantic choices are made by humans before any treatment response exists. A private,
loopback-only annotation website may present the evidence and explicit schema-backed choices;
the website is an interface to curation, not a model, judge, verifier, or automatic intervention
policy.

This contract authorizes:

1. read-only, CPU-only validation and projection of the active formal G1.3 v1.1 publication;
2. repo-external, content-addressed reviewer packets for `ACTION_GOLD`, `TRANSFORMATION`, and the
   post-curation descriptive `CONSISTENCY_AUDIT`;
3. local human drafts, two independent finalized reviews per unit/channel, and required third-party
   adjudication;
4. deterministic mechanical checks, previews, append-only storage, conformance tests, and export;
5. formal G1.6 artifacts only under the already frozen G1.1 output schemas.

It does not authorize any provider/model call, GPU, model-weight or tokenizer download, external
network, external hosting, task execution, MobileWorld/generated GUI/tool/action execution,
backend restore, prefix or live replay, treatment response, result-informed curation, automatic
semantic inference, or runtime Sentinel behavior. Human clicks inside the annotation webpage are
authorized annotation input and are not generated MobileWorld actions.

Every formal plan, gold, review, arm, and admission artifact retains:

```json
{"curated": true, "deployment_prediction": false}
```

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative.

## 2. Frozen authority and additive boundary

The following remain immutable inputs:

- the G1.1 protocol, analysis plan, pre-gold registry publication, arm catalog, model/config
  manifest, selection ledger, and every schema under `schemas/g1/`;
- the accepted G1.2 portable Sentinel contract, package, schemas, and conformance fixtures;
- the active G1.3 v1.1 publication with manifest/content address
  `8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402` and capsule-set
  SHA-256 `7d0e85c523c2b20b3f0b820c2e846cbb84957d4ae78e46d7090c6ce78ae9fbed`;
- Collector v1 raw streams, blobs, manifests, exact request artifacts, and all source evidence.

The active publication contains exactly 190 capsules and zero G1.3 exclusions:

| Unit | Qwen | MAI | Total |
| --- | ---: | ---: | ---: |
| strict-MHR candidates | 139 | 13 | 152 |
| selected clean controls | 30 | 8 | 38 |
| **G1.6 target units** | **169** | **21** | **190** |

The separate 38 reserve clean controls remain census-only and outside G1.6 review, proposal,
admission, and exclusion accounting. G1.6 MUST derive the exact unit set from the frozen
publication and registry rather than from this table or hard-coded row positions.

The schemas in `schemas/g1/` already define the formal G1.6 outputs and are authoritative:

- `curation_input_manifest.schema.json` and `curation_evidence.schema.json`;
- `action_gold_bundle.schema.json`;
- `transformation_plan.schema.json`;
- `review_ledger.schema.json`;
- `arm.schema.json`;
- `admission.schema.json` and `admission_validation.schema.json`;
- `admission_seal.schema.json`.

They MUST NOT be edited, copied with looser constraints, or re-versioned by ALE-324. The additive
`schemas/g1_6/` records describe only the private annotation workspace, role-projected packets,
assignment-scoped CPU previews, intermediate human proposals, and append-only journal. Those
records are not scientific output and cannot substitute for validation against the frozen formal
schemas.

## 3. Semantic authority is human-only

### 3.1 Decisions that require a human

Only an assigned human reviewer may decide or author:

- whether the unit is `ACCEPT` or `EXCLUDE` for that curation channel;
- every accepted next-action predicate, alternative, coordinate region, direction, text variant,
  typed field, and tolerance;
- the final ordered focal target set and oracle target set;
- every correction byte and whether it is the shortest sufficient evidence-supported correction;
- every protected span, sham span, semantic-independence judgment, and sham-match judgment;
- whether delimiter repair is necessary and which eligible syntax bytes it removes;
- whether the historical natural action is history-consistent and whether it is inconsistent with
  the target-pre GUI/task, only in the separately gated descriptive audit;
- the resolved payload or exclusion reason after material disagreement.

No program, heuristic, model, prior natural action, benchmark outcome, majority vote, or first
review may select or fill these values on the reviewer's behalf.

### 3.2 Allowed mechanical assistance

The annotation implementation MAY perform only semantics-free operations whose result follows
uniquely from frozen bytes and selected human inputs:

- rehydrate and hash-check exact evidence and reject broken references;
- enforce event cut-off and channel/role allowlists;
- render exact text, images, JSON, coordinates, and source lineage read-only;
- highlight an already frozen exact Qwen source binding, clearly labeled as source provenance
  rather than a recommended G1.6 answer;
- translate a user selection into matching half-open Unicode-code-point and UTF-8 byte ranges;
- calculate SHA-256, byte count, code-point count, pinned-tokenizer token count, structural bucket,
  history depth, and the protocol's integer sham length test;
- check non-overlap, containment, protected bytes, target-set inclusion, canonical order, and
  correction tie-break fields;
- render Original/Mask/Mask+Correction/Oracle/Sham previews from a draft without invoking a model;
- compute exact target-only diffs and reversible mappings;
- validate frozen schemas, identity separation, conflicts, and admission rules;
- run the selected CPU-only Qwen/MAI History Codecs over already curated drafts to produce read-only
  canonical History IR, renderer previews, target-only diffs, and reversible mappings;
- aggregate progress from authoritative append-only events.

If the pinned tokenizer artifact is unavailable locally, the implementation MUST block token-
dependent finalization. It MUST NOT download a tokenizer, substitute another tokenizer, estimate
from characters, or silently accept a human-entered token count. No model weights are loaded.

### 3.3 Forbidden automation and suggestion leakage

The implementation MUST NOT perform claim extraction, truth/relevance inference, correction
generation, paraphrasing, action recommendation, image understanding, OCR used to propose gold,
sham recommendation, span ranking, automatic adjudication, outcome-based selection, or any
LLM-as-judge path. Search and filtering may locate exact source bytes or UI records but may not
rank semantic answers. Natural target prediction/action and post-target facts cannot be used as
hidden features even if not displayed.

For MAI `raw_replay`, `edit_span_status=G1_6_PENDING` and `focal_edit_spans=[]` remain authoritative
until the transformation reviewers independently select the smallest semantically self-contained
premise spans. A parsed `<thinking>` element is only a non-editable curation envelope. The UI MUST
protect the complete `<tool_call>` tag/payload and valid action record and MUST reject any selected
span that intersects them. It may not propose a span from the historical natural action.

## 4. Curator input construction and role visibility

### 4.1 Common packet rules and two-level hashes

Packet generation begins only after source-bound validation of the active G1.3 publication. For
each unit/channel, the builder MUST parse and validate the frozen curation input manifest and all
referenced curation-evidence records, rehash their transitive artifacts, and bind:

- exact unit/capsule/registry identity;
- request cut-off event ID and sequence;
- input-manifest bytes and SHA-256;
- the ordered evidence set and each projection SHA-256;
- the channel input policy and role-neutral base visibility;
- a reviewer-neutral source-packet SHA-256; and
- an assignment-packet SHA-256 after the server adds assignment, reviewer commitment, role,
  packet-scoped opaque tokens, and adjudication peer references.

`source_packet_sha256` is computed from the canonical reviewer-neutral projection containing only
the contract version, channel, input policy, exact unit/capsule/registry/request-cutoff binding,
input-manifest hash, ordered stable evidence IDs and projection hashes, role-neutral base
visibility, and, for the descriptive audit only, its post-curation resolution-set hash. It excludes
reviewer identity, review role, assignment ID, opaque browser tokens, peer-review references,
timestamps, and presentation state. PRIMARY, SECONDARY, and any ADJUDICATOR for one unit/channel
MUST share this digest. Their `assignment_packet_sha256` values normally differ and MUST NOT be
used to decide same-input agreement. `source_packet_id` is `g1packet-` plus the first 24 hex
characters of `source_packet_sha256`; `assignment_packet_sha256` is SHA-256 of the complete
schema-valid assignment-projected browser packet with only that digest field omitted.

The browser never receives a whole ReplayCapsule, source-root path, whole task stream, sealed audit
suffix, or general-purpose artifact resolver. It receives only a server-built packet and opaque
packet-scoped evidence URLs. Evidence endpoints MUST re-check workspace, authenticated reviewer,
assignment, unit, channel, role, source-packet digest, assignment-packet digest, and exact evidence
ID on every request.

### 4.2 `ACTION_GOLD`

An `ACTION_GOLD` packet MUST include at least one exact `task_instruction` and one exact
`target_pre` current-GUI reference. It MAY include request-cut-off-eligible `tool_response` or
`ask_user_response` evidence.

It MUST NOT contain or resolve:

- model-visible history, source history, the misleading target, or any transformation preview;
- natural source or target prediction/action, target result/post-state, or later step;
- hidden benchmark parameters, checker/evaluator state, task outcome, failure-link evidence;
- action-gold or transformation proposals from another reviewer/channel;
- replay response, generated action, seed result, or treatment metadata.

The server sets `history_visible=false`, `accepted_action_visible=false`,
`peer_reviews_visible=false`, `natural_target_output_visible=false`, and
`later_trajectory_visible=false` for both initial reviewers. A client request cannot weaken them.

### 4.3 `TRANSFORMATION`

A `TRANSFORMATION` packet MUST include at least one exact `source_history` reference. It MAY also
include eligible `source_pre`, `target_pre`, `task_instruction`, `tool_response`, and
`ask_user_response` evidence needed to curate the history arms.

It MUST NOT contain or resolve:

- natural target prediction/action, target result/post-state, or later step;
- accepted-action predicates, action-gold drafts/reviews/resolution, or scoring tolerance;
- hidden outcome/checker/evaluator/failure-link material;
- another transformation review before adjudication opens;
- replay response, generated action, or treatment result.

The server sets `history_visible=true`, `accepted_action_visible=false`,
`peer_reviews_visible=false`, `natural_target_output_visible=false`, and
`later_trajectory_visible=false` for both initial reviewers.

### 4.4 Adjudication visibility

Adjudication is opened only when two finalized same-channel reviews over the exact same
reviewer-neutral `source_packet_sha256` have a mechanically detected material disagreement. The
adjudicator packet contains the
same role-projected evidence as that channel plus immutable references to exactly those two
finalized proposals. It never gains evidence from the other channel.

An action-gold adjudicator still has `history_visible=false`; a transformation adjudicator still
has `accepted_action_visible=false`. `peer_reviews_visible=true` applies only to the adjudicator.
The implementation may identify disagreement fields mechanically but MUST NOT choose a winner,
merge semantic values, or preselect a resolution.

### 4.5 Post-curation `CONSISTENCY_AUDIT`

The original-action research task is isolated in a third, descriptive channel. Its packets MUST
NOT be issued until both `ACTION_GOLD` and `TRANSFORMATION` have immutable resolution for the unit;
the packet binds the digest of that resolution set. It contains only the exact task instruction,
target-pre GUI, source history, and historical natural normalized action/parse outcome. It excludes
accepted-action gold, correction/oracle/sham decisions, post-state, later trajectory, outcome,
checker/evaluator material, and every replay/treatment response.

Two new blind reviewers independently select exactly one label:

- `HISTORY_CONSISTENT_GUI_TASK_INCONSISTENT`;
- `HISTORY_AND_GUI_TASK_CONSISTENT`;
- `HISTORY_INCONSISTENT`;
- `AMBIGUOUS`; or
- `UNPARSEABLE_ORIGINAL_ACTION`.

Each reviewer supplies separate history-consistency and GUI/task-consistency rationale. Material
label disagreement requires identity-disjoint adjudication. The resolved label is descriptive
audit metadata only: it MUST NOT alter action gold, transformation, arm construction, inclusion,
exclusion, scoring, replay, or any treatment. G1.6 completion nevertheless requires the descriptive
task to be resolved for all 190 units so no requested human research is silently omitted.

## 5. Reviewer identity, independence, and finality

Reviewer identity is established server-side from one owner-maintained repo-external registry of
stable canonical UTF-8 principal IDs. One random 32-byte workspace key is created exactly once and
stored mode-restricted outside Git. The immutable manifest records only `SHA256(key)`. Exact
formulas are:

```text
identity_commitment = HMAC-SHA256(
  key,
  UTF8("mobileworld.g1.gold-curation.reviewer/v1\0" + workspace_id + "\0")
  || canonical_principal_id_utf8)

assignment_mac = HMAC-SHA256(
  key,
  UTF8("mobileworld.g1.gold-curation.assignment/v1\0" + workspace_id + "\0"
       + channel + "\0" + role + "\0" + unit_id))
assignment_id = "g1assignment-" + first_32_lower_hex(assignment_mac)
```

The restricted owner registry is mode `0600` JSON with exact closed shape
`{schema_version:"mobileworld.g1.owner-reviewer-registry/v1",
principals:[{principal_id,role,adjudication_channel,access_secret}]}`. Each principal appears once
and has one of the six composite channel/stage initial-review roles or the generic `ADJUDICATOR`
role. `adjudication_channel` MUST be null for an initial reviewer and exactly one of the three
channels for an adjudicator; every session and assignment is server-limited to that pinned channel.
IDs are non-empty exact UTF-8 strings and each access secret is at least 16 UTF-8 bytes. Its
manifest-bound semantic projection replaces every secret with
`access_secret_sha256 = SHA256(access_secret.encode("utf-8"))`, retains the bound
`adjudication_channel`, sorts principals by exact principal ID bytes, then uses the
Section 8.1 canonical JSON and SHA-256. Plaintext secrets never enter the workspace manifest,
journal, packet, logs, or Git. The registry semantic-bytes SHA-256 is pinned in the write-once
workspace manifest. The registry MUST reject duplicate principals, aliases, per-registration
salts, or client-selected commitments; no alias mapping is accepted at authentication time.
The browser receives only an HttpOnly/SameSite session and commitment; identity, role, channel,
unit, and packet binding are never accepted from request bodies, query parameters, or browser
storage. One commitment is valid for exactly one channel-role set in a workspace.

For each unit and each applicable channel:

1. exactly one `PRIMARY` and one `SECONDARY` independently review the same input-manifest and
   reviewer-neutral source-packet hashes;
2. the two reviewer identity commitments MUST differ;
3. neither reviewer may see the other's draft, final proposal, validation errors, or adjudication
   before both reviews finalize;
4. when material disagreement exists, exactly one `ADJUDICATOR` with a third identity resolves it;
5. action-gold, transformation, and consistency-audit reviewer identity sets MUST be mutually
   disjoint across the G1.6 workspace/publication;
6. an adjudicator identity MUST also be disjoint from both initial reviewers and from every
   identity admitted to either other channel in that workspace/publication.

An assignment violating these constraints is rejected before packet issuance. A finalized review
is immutable. A reviewer may save any number of drafts before finalization; each save creates a new
closed inline payload in a separately hashed append-only journal event. A finalized mistake requires a new explicitly
superseding workspace/publication version and never an overwrite, delete, or hidden unlock.

## 6. Web workbenches and explicit human choices

### 6.1 Dashboard and assignment

The local site MUST expose progress by unit, model, unit kind, channel, role, and state without
revealing hidden peer answers. It MUST distinguish `NOT_ASSIGNED`, `DRAFTING`, `FINALIZED`,
`WAITING_FOR_PEER`, `ADJUDICATION_REQUIRED`, `ADJUDICATING`, `RESOLVED`, and `BLOCKED_INVALID_INPUT`.
Counts are derived from the journal and are not editable counters.

Before opening a packet, the site MUST show the current authenticated reviewer identity
commitment, assigned channel/role, reviewer-neutral source-packet digest, assignment-packet digest,
and a plain-language visibility notice.

### 6.2 Action-gold workbench

The action-gold page presents only the evidence allowed by Section 4.2 and requires an explicit
disposition:

- `ACCEPT`: enumerate the complete reasonable one-step accepted set; or
- `EXCLUDE`: choose an allowed frozen exclusion reason, normally `NO_GOLD_CONSENSUS` when the
  reasonable next actions cannot be enumerated.

For `ACCEPT`, the UI MUST expose the exact predicate options governed by the frozen
`action_gold_bundle.schema.json`:

- `EXACT_NORMALIZED_ACTION` for one exact production-normalized action;
- `POINT_REGION` for click/double-tap/long-press regions plus explicit pixel tolerance;
- `DRAG_REGION` for allowed start/end regions, direction, minimum displacement, and tolerance;
- `TEXT_VARIANTS` for the schema-allowed action type/field and exact normalized variants;
- `DIRECTION_SET` for allowed scroll/swipe directions.

Every predicate requires one or more visible packet evidence citations and a human-authored
rationale. The reviewer can add all legitimate alternatives and must explicitly attest that the
set is complete for the visible pre-cutoff state. Drawing a box/polygon or entering tolerance is a human
choice; the UI may validate geometry and viewport bounds but cannot infer the region from the
screenshot or natural action. `answer` and `finished` are accepted only when explicitly selected.
The original natural action is never shown or used as a default.

An `EXACT_NORMALIZED_ACTION` value is closed by the pinned production normalized-action
schema/parser binding, must carry the selected predicate action type, and is validated before the
event append. The open JSON object slot in the intermediate proposal schema is not permission for
unknown production action fields.

### 6.3 Transformation workbench

The transformation page presents only Section 4.3 evidence and offers explicit controls for:

- ordered focal target spans;
- ordered oracle target spans, constrained to a superset of the focal set;
- correction bytes and their pre-cutoff evidence references;
- protected spans and exact correction insertion anchors;
- a complete benign sham span and each semantic/matching attestation;
- per-arm delimiter/shell repair operations;
- read-only previews for `ORIGINAL`, `MASK`, `MASK_CORRECTION`, `ORACLE_CLEAN`, and
  `SHAM_BENIGN_EDIT` as applicable to the unit kind;
- `ACCEPT` or an allowed frozen `EXCLUDE` reason such as `NO_VALID_CORRECTION`,
  `NO_VALID_ORACLE_VIEW`, or `NO_MATCHED_SHAM`.

Every selected span uses half-open indices into one exact source record: `char_start`/`char_end`
count Unicode code points and `utf8_byte_start`/`utf8_byte_end` count bytes in that record's exact
UTF-8 encoding. The two slices MUST decode to the same `exact_text`, and `span_sha256` is SHA-256
of `exact_text.encode("utf-8")`. The server resolves the assignment-scoped record token back to
the one source record and rejects non-boundary offsets, mismatched text/digest, or an overlap with
a protected span before appending the event.

For strict units, focal spans are the smallest semantically self-contained premise spans covering
all frozen strict-MHR targets; oracle spans include every independently confirmed misleading
premise relevant to that decision. Exact targets are non-empty, non-overlapping, hash-bound, and
canonically ordered.

For correction minimality, the UI displays mechanically verified values under the pinned
tokenizer. The human authors evidence-supported correction alternatives. The final correction is
the fewest tokens; ties use UTF-8 byte length, then Unicode code-point length, then lexicographic
UTF-8 bytes. It may state only the corrected historical fact and cannot recommend an action,
expose accepted-action criteria, or use post-target evidence.

The CPU preview is generated from the exact canonical semantic request through the selected G1.5
History Codec and stops before provider encoding.  The internal preview receipt binds the capsule,
source packet, exact human preview inputs, G1.5 CPU publication, and complete validated preview.
Only an assignment-scoped closed browser projection may cross the HTTP boundary.  That projection
omits the codec/model/tokenizer identity, request/History-IR/plan/render/validation hashes, stable
record/binding/operation identities, JSON paths, and the internal human-diff text.  It replaces
necessary record, container, binding, operation, and repair references with assignment-scoped
tokens and regenerates the displayed diff only from the visible projection.  A matching sham and
all false/zero provider, network, GPU, replay, and action guards are required before the human may
confirm the preview.  Any change to correction candidates or rationale, evidence, selected or
protected spans, delimiter repairs, or sham selection invalidates the receipt and requires a fresh
preview.

For sham, the human confirms no entailment, contradiction, lexical alias, task-hard-requirement,
or accepted-action-discriminant relation to the focal fact. The chosen span uses the same role,
content kind, representation-record class, and record third as the first focal span; same record
is preferred, otherwise history-depth difference is at most one. The exact integer length check is:

```text
(5 * benign_tokens >= 4 * focal_tokens &&
 4 * benign_tokens <= 5 * focal_tokens) ||
abs(benign_tokens - focal_tokens) <= 4
```

Delimiter repair is a separately selected exact deletion. It is allowed only when that arm's full
edit leaves the named syntax empty or orphaned; adjacency alone is insufficient. The UI validates
the bytes separately after Mask, Mask+Correction, Oracle, and Sham. It cannot copy Mask repair to a
non-empty correction record automatically.

Clean controls expose Original, Sham, and one human-confirmed benign focal/reference anchor needed
by the unchanged frozen `focal_target_set` and `matched_focal_target_id` fields. The anchor is not a
misleading premise and is never masked or corrected. Correction, oracle, and Mask controls are
absent; the exporter emits empty correction/oracle sets, `applicable_arms=[ORIGINAL,
SHAM_BENIGN_EDIT]`, and the frozen `NOT_APPLICABLE` invariants. The selected control's anchor and
benign status must still be independently confirmed. Exactly one focal/reference anchor is stored
for an accepted clean control; its deterministically exported target ID is the sham edit's
`matched_focal_target_id`.

### 6.4 Adjudication workbench

The adjudication page opens only after the conflict gate. It shows the two immutable proposals
side by side, the mechanically computed frozen disagreement-field names, and the same channel
evidence. The adjudicator must enter a complete final proposal and explicitly resolve every listed
field; a one-click majority, copy-primary default, or automatic union/intersection is forbidden.

Material fields are exactly those in the frozen review-ledger schema:

- action gold: `DISPOSITION`, `ACCEPTED_ACTION_PREDICATES`, `ACTION_TOLERANCE`;
- transformation: `DISPOSITION`, `FOCAL_TARGET_SET`, `ORACLE_TARGET_SET`, `CORRECTION_BYTES`,
  `SHAM_SPAN`, `SHAM_MATCH`, `DELIMITER_REPAIR`.

For comparison purposes `DISPOSITION` is the pair `(disposition, exclusion_reason)`, so two
different exclusion reasons are a material disagreement even though the frozen ledger does not
name a separate `EXCLUSION_REASON` field. The descriptive channel adds only
`CONSISTENCY_LABEL`; it is never copied into the frozen formal review ledger.

### 6.5 Consistency-audit workbench

After the Section 4.5 gate, the site shows source history, task, target-pre GUI, and the historical
natural action side by side. It renders the five closed labels as radio options, requires separate
rationales for the history and GUI/task judgments, displays `descriptive only`, and requires an
explicit `no replay response used` attestation. It MUST NOT show action-gold answers or
transformation selections. Primary/secondary blindness and the adjudication UI operate exactly as
in the two formal channels.

## 7. Intermediate proposal and formal-export boundary

Website drafts and finalized reviewer proposals are intermediate annotation records. Each closed
proposal payload is stored inline in one canonical, hash-chained journal event and binds its
assignment, reviewer, role, channel, neutral source-packet hash, assignment-packet hash, and
payload hash. They do
not themselves satisfy `action_gold_bundle`, `transformation_plan`, `review_ledger`, `arm`,
`admission`, or `admission_seal`.

A formal-channel proposal can finalize as:

- `ACCEPT`, with one complete closed intermediate human payload. The exporter deterministically
  maps the resolved payload into a separate content-addressed formal candidate and records that
  candidate reference only after it passes the frozen action-gold-bundle or transformation-plan
  schema appropriate to the channel; or
- `EXCLUDE`, with no formal candidate and a channel-specific human reason: only
  `NO_GOLD_CONSENSUS` for action gold, or `TARGET_SPAN_UNRESOLVED`, `NO_VALID_CORRECTION`,
  `NO_VALID_ORACLE_VIEW`, or `NO_MATCHED_SHAM` for transformation.

Hash/provenance/duplicate/future-evidence/arm-protocol and other mechanical frozen reasons cannot
be chosen by a reviewer. They require a separately content-addressed validator receipt proving the
exact failed invariant before formal export may use them.

Before accepting finalization, the server MUST independently validate the closed proposal payload,
every cited evidence token, normalized action, span coordinate pair, reviewer
attestation, hash, cut-off, channel, unit, source packet, and assignment packet. Client-side
validation is advisory only. Formal export then:

1. verifies both independent proposals share exact unit/input and reviewer-neutral source-packet
   bindings while retaining their distinct assignment packets;
2. detects material disagreement using the canonical semantic projection below, not filenames or
   display text;
3. requires and validates adjudication when needed;
4. emits one frozen-schema `review_ledger` per unit/channel;
5. emits resolved gold/transformation/arm artifacts only from that ledger resolution;
6. validates every included or excluded admission record and its exact evidence branch;
7. accounts for all 190 target units exactly once before any G1.6 seal.

The material projection version is `mobileworld.g1.review-material-projection/v1`. It uses the
canonical JSON bytes in Section 8.1 and these closed rules:

- common: compare `(disposition, exclusion_reason)` as `DISPOSITION`; ignore proposal, bundle,
  plan, predicate, repair, evidence-ref and content-address path IDs;
- action: compare predicates as a set sorted by canonical bytes; sort direction/value/region sets;
  compare normalized-action referenced bytes by SHA-256; canonicalize a polygon by removing a
  duplicated closing vertex, rotating to the lexicographically least vertex, and choosing the
  lexicographically lesser of forward/reversed traversal; compare exact integer coordinates and
  tolerance separately as `ACCEPTED_ACTION_PREDICATES` and `ACTION_TOLERANCE`;
- transformation: compare focal and oracle targets in semantic ordinal order after replacing IDs
  with `(record_identity_sha256, request_path, char_start, char_end, utf8_byte_start,
  utf8_byte_end, span_sha256)`; sort source-candidate/evidence ID sets; compare correction exact
  UTF-8 bytes, sham span, sham-check values, and per-arm repairs sorted by
  `(arm,record,byte_start,byte_end,operation)`; and
- consistency audit: compare only the closed label as `CONSISTENCY_LABEL`.

Each finalized event stores the projection digest. Agreement means byte-identical projections.
The comparison code MUST be fixture-tested for reordered sets, different IDs/paths, rotated or
reversed polygons, Unicode spans, different exclusion reasons, and one-value material changes.

No partial unit is `INCLUDED`. A unit missing one applicable channel, a required reviewer,
adjudication, correction, oracle, sham, arm, or hash is unresolved or excluded under frozen rules;
it is never silently accepted. An exclusion requires the exact closed human-ledger or mechanical-
validator evidence prescribed by G1.1. Natural success/failure, local harm, final score, and later
replay behavior are never exclusion reasons.

## 8. Append-only storage and local-site security

### 8.1 Authoritative storage

Authoritative annotation-workspace state lives under a new owner-restricted root outside Git,
conceptually:

```text
<configured_annotation_root>/
  workspace-manifest.json
  assignment-key.bin
  packets/sha256/<prefix>/<digest>
  assignment-packets/sha256/<prefix>/<digest>
  annotation-events.jsonl
  exports/sha256/<publication_digest>/...
```

`assignment-key.bin` is the one random workspace key used for both identity commitments and
assignment IDs.  The owner reviewer registry and G1.5 codec-gate receipt are separate
repository-external, owner-restricted inputs and are not copied into the annotation root.  The
registry is read and validated once at workspace startup; every authoritative operation binds its
in-memory redacted semantic digest to the immutable manifest.  The codec-gate receipt is reopened
and fully rederived on every read, append, finalization, or export operation.  The manifest records
only the registry semantic digest and the key commitment.  The exact root and input paths are
runtime configuration and MUST NOT enter scientific identity or packet bytes. The manifest's
registry semantic binding is write-once; workspace manifest, source packets, assignment packets,
and exports are atomic write-once regular files. `annotation-events.jsonl` is the sole authoritative proposal journal: every draft,
final review, and adjudication is one new closed event with global zero-based contiguous sequence
and previous-event SHA-256. Dynamic packet/event indexes are not stored in the immutable workspace
bootstrap; later index snapshots and all search databases, thumbnails, and progress caches are
rebuildable and non-authoritative.

Canonical JSON is exactly `json.dumps(value, ensure_ascii=False, sort_keys=True,
separators=(",", ":"), allow_nan=False).encode("utf-8")`, with no BOM and no trailing newline.
Every `*_sha256` named by this contract is lowercase SHA-256 of those bytes unless the field
explicitly names raw UTF-8 or artifact bytes. The JSONL file adds exactly one `0x0a` after each
canonical event, but the newline is excluded from `event_sha256`. `payload_sha256` hashes the
payload object; `event_sha256` hashes the closed event with only `event_sha256` omitted;
`event_id` is `g1annotation-` plus the first 24 hex characters of the canonical event with
`event_id` and `event_sha256` omitted. Source packet objects live at
`packets/sha256/<digest[0:2]>/<digest>.json`; the filename digest MUST equal the rederived bytes.

Every event has a required `codec_gate_receipt_sha256` field. It is null for `DRAFT_SAVED`, so
draft capture remains available while the gate is closed. It is the exact SHA-256 of the currently
verified, repo-external G1.5 CPU codec-gate receipt for every `REVIEW_SUBMITTED` and
`ADJUDICATION_SUBMITTED` event. Before any journal read, append, or workspace-receipt export, the
store MUST reopen the configured gate receipt, rederive it from the bound G1.5 publication and all
referenced codec/conformance bytes, and require exact equality with the receipt verified when the
workspace process opened. Every formal event in one workspace MUST carry that same digest, and the
workspace export receipt MUST carry the identical `codec_gate_receipt_sha256`. A missing gate,
changed referenced byte, receipt mismatch, or mixed formal-event digest fails closed; it is not
accepted merely because the surrounding journal hashes were recomputed. This runtime-effective
gate binding does not mutate or relax the immutable workspace manifest's
`formal_annotation_open=false` bootstrap guard.

Every journal line MUST validate `annotation_event.schema.json`. In addition, a `DRAFT_SAVED` or
`REVIEW_SUBMITTED` payload MUST validate the root of `review_proposal.schema.json` and its
`proposal_kind` MUST equal the event channel. For `ADJUDICATION_SUBMITTED`, `resolved_payload` MUST
pass that same channel-specific validation, the two referenced review events and projection hashes
MUST rederive exactly, and every listed disagreement field MUST equal the deterministic comparison.
These two validations are one atomic append gate; the broad JSON type at the event schema's payload
slot is not permission to bypass the closed proposal schema.

The store MUST use no-follow opens, safe relative paths, atomic create/no-replace, digest and byte-
count verification, per-stream locking, durable flush at finalization, and collision rejection.
It MUST reject symlinks, hard-link surprises when policy cannot verify ownership, non-regular
files, path escape, cross-workspace reference, hash mismatch, truncated JSONL, duplicate sequence,
forked chain, and a content address whose existing bytes differ. It never repairs or deletes an
authoritative object in place.

Browser local storage, session storage, IndexedDB, cookies, and service-worker caches MUST NOT hold
authoritative proposals or evidence. They MAY retain non-sensitive display preferences. Sensitive
packet responses use no-store headers and are not written to console logs.

### 8.2 Loopback-only web boundary

The annotation server MAY bind only explicit loopback addresses (`127.0.0.1` and, when supported,
`::1`). It MUST reject wildcard/non-loopback bind configuration, forwarded-host assumptions, and
non-loopback peers. Static HTML/CSS/JavaScript and fonts are served locally; there are no CDN,
analytics, remote assets, telemetry, external authentication callbacks, or service workers.

State-changing requests require an authenticated local reviewer session in an HttpOnly/SameSite
cookie, same-origin checks, and an anti-CSRF token distinct from the cookie. The server applies
security headers, packet-scoped evidence authorization,
bounded request sizes, strict content types, and safe image/media serving. It MUST NOT expose a
file-browser, arbitrary path, raw capsule, shell, command, plugin, or URL-fetch endpoint.

JSON Schema resolution uses an explicit, hash-pinned in-memory registry containing the local
frozen G1 schemas and the additive G1.6 schemas. A validator MUST NOT resolve
`agentsentinel.local`, another `$id`, or any missing `$ref` through DNS or HTTP; an absent local
schema binding is a fail-closed validation error.

Loopback browser/server traffic is the only socket use authorized by D-029. One owner-invoked,
foreground, single-process annotation server with no reloader or child workers is expressly
allowed. The implementation MUST fail closed before every other subprocess/service launch, DNS,
external HTTP(S), provider SDK, Docker, GPU, model/model-serving, or external-hosting path.
Publishing the site to an intranet, tunnel, cloud, or public host requires
a new owner authorization and data-transfer review.

## 9. Admission and readiness semantics

G1.6 is census-first: every frozen candidate/control receives one validated `INCLUDED` or
`EXCLUDED` admission record. An included unit has all applicable, independently reviewed,
hash-resolved gold/transformation/arm artifacts. An excluded unit has the exact frozen exclusion
evidence. The seal cannot be emitted while any target unit is undisposed.

The final frozen `admission_seal.schema.json` requires the protocol's intervention/clean coverage
and sample minima. If the resolved census cannot satisfy that schema and its validator, the system
reports the exact blocking/exclusion evidence and does not invent an admission-ready seal by
relaxing criteria or adding reserve controls.

Before formal annotation may open, a repo-external content-addressed gate receipt MUST bind exactly
one selected Qwen `flat_progress` codec and one selected MAI `raw_replay` codec from the G1.5 CPU
publication: codec ID/contract version, implementation SHA-256, capability SHA-256, canonical
History-IR schema SHA-256, renderer SHA-256, tokenizer binding, and conformance receipt. Missing,
fixture-only, hash-mismatched, or insufficient capabilities block packet finalization. No live
provider capability is needed or exercised.

Before the G1.6 seal, every accepted transformation plan MUST be validated through its selected
registry-resolved codec on the exact frozen request: deterministic extraction of the accepted G1.2
History IR; Original round-trip; applicable-arm rendering; exact target-only diff; preservation of
all non-target request values; per-arm delimiter rules; and a reversible mapping from rendered
bytes to source record/Unicode-code-point/UTF-8-byte coordinates. This is CPU-only rendering and
MUST NOT create a provider client, send a request, load weights, or execute a replay.

A valid G1.6 seal may state:

```text
curation_and_admission_sealed = true
admission_ready = true
execution_ready = false
treatment_response_generation_allowed = false
treatment_response_count_at_seal = 0
next_required_gate = G1_7_PREFLIGHT_EXECUTION_SEAL
```

It does not alter any G1.3 capsule guard. Neither the site, a workspace manifest, a completed
review, nor the G1.6 seal may authorize a provider call. Any later replay still requires G1.5 live
History Codecs, all G1.7 serving/seed/backend/isolation/scorer/run-ready/execution seals, approved
resources, and a new explicit owner authorization.

### 9.1 Blinded scoring material

G1.6 creates a separate random 32-byte repo-external catalog key and exports a confidential,
content-addressed `mobileworld.g1.blinded-gold-catalog/v1`. For each unit,
`gold_key = "g1goldkey-" + HMAC-SHA256(catalog_key,
UTF8("mobileworld.g1.blinded-gold/v1\0" + publication_digest + "\0" + unit_id))`.
The public catalog commits only `SHA256(catalog_key)`. Each row
contains only this opaque `gold_key`, the frozen action-gold-bundle SHA-256, predicate-
language/scorer hashes, and schema version. A separately sealed mapping relates `gold_key` to
unit ID; neither row contains arm, hypothesis, plan, correction, history, model/provider, schedule,
or treatment metadata. The catalog and mapping are never sent to curators or scorers.

When G1.7 later joins this catalog to G1.4 `blinded_packet_binding`/`blinding_mapping`, the
scorer-visible lookup is only `(opaque_packet_id, normalized_action, generic_parser_outcome,
allowlisted_diagnostics, gold_key)`. The confidential join can resolve gold but MUST pass G1.4's
deny-set and ordering checks and MUST NOT reveal requested/effective arm or intended hypothesis.
G1.6 acceptance validates the catalog shape, key uniqueness, one-to-one 190-unit coverage, sealed
mapping hash, and negative leakage fixtures; it does not construct any treatment response.

## 10. Conformance and acceptance

Before the workspace may accept any finalized formal human review, CPU-only tests MUST prove the
pre-annotation gates that do not depend on the later human answers:

1. active v1.1 source-bound publication validation and byte immutability;
2. exact 152/38 target and 38-reserve accounting;
3. packet determinism, hash binding, and no whole-capsule/general-resolver exposure;
4. all `ACTION_GOLD` forbidden-field and evidence-role negative cases;
5. all `TRANSFORMATION` forbidden-field and evidence-role negative cases;
6. primary/secondary double blindness and adjudicator-only peer visibility;
7. owner-registry authentication, exact workspace-key HMAC formulas, alias rejection, HttpOnly
   sessions, and within-/cross-channel reviewer identity disjointness;
8. draft append, finalized immutability, hash-chain verification, concurrency, no-follow, and path-
   escape rejection;
9. complete UI option coverage for every frozen action predicate, transformation field, exclusion
   reason, and material disagreement field;
10. Qwen exact binding display and MAI pending-span/protected-tool-call enforcement;
11. registry-resolved selected Qwen/MAI CPU codec binding, accepted History-IR extraction,
    Original round-trip, target-only diff, correction minimality, sham integer/bucket/depth,
    per-arm delimiter repair, renderer preservation, and reversible mapping checks without semantic
    generation;
12. fixture-level two-review agreement, disagreement routing, adjudication, and irreversible
    resolution;
13. loopback-only bind, same-origin/CSRF, packet-scoped evidence, no remote asset/fetch, and no
    authoritative browser storage;
14. zero provider/model/client, external network, non-annotation subprocess/service, GPU,
    model-weight, replay,
    MobileWorld/generated GUI/tool/action, treatment-response, auto-annotation, and raw/frozen-
    artifact mutation paths. This generated-action check does not prohibit the human's annotation-
    page clicks or the one owner-started foreground loopback annotation server.

The workspace may then collect immutable human reviews while it remains
`IN_PROGRESS_HUMAN_CURATION_REQUIRED`. Before a formal exporter may emit a gold publication or
admission seal, it MUST re-prove all pre-annotation gates and additionally prove:

1. every one of the 190 units has both required formal-channel resolutions and its two
   post-curation `CONSISTENCY_AUDIT` reviews plus any required adjudication;
2. deterministic projection into, and validation against, the unchanged frozen G1.1 output
   schemas with exact 190-unit admission/exclusion accounting; and
3. blinded-gold catalog uniqueness/coverage, sealed mapping binding, and scorer-visible
   arm/hypothesis leakage negatives.

Formal completion additionally requires every target unit to be resolved, all formal outputs to
pass schema and cross-artifact validation, independent rebuild/validation of the exact publication,
the two codec receipts, blinded-gold catalog, and repo-external content-addressed write-once sealing.
It also emits a deterministic content-addressed `mobileworld.g1.curation-disagreement-report/v1`
with denominators and counts/rates by channel, material field, model, history family, and unit kind;
primary/secondary agreement, adjudication-required/resolved/unresolved counts; the exact journal
head, proposal projection version, population digest, and report-body hash. Empty strata remain
explicit with denominator zero and rate null. This report is audit metadata, not an outcome and not
an admission input. A working website or partial review count
is only an implementation checkpoint and MUST NOT be reported as completed G1.6.

## 11. Versioned annotation records

The additive machine records under `mobileworld_audit_handoff/schemas/g1_6/` are:

| File | Identifier | Purpose |
| --- | --- | --- |
| `annotation_workspace.schema.json` | `mobileworld.g1.gold-curation-workspace-manifest/v1` | write-once workspace bootstrap, identity/server/storage policy and closed safety guards |
| `curator_packet.schema.json` | `mobileworld.g1.gold-curation-review-packet/v1` | exact assignment-projected browser packet with separate neutral-source and assignment digests |
| `browser_transformation_preview.schema.json` | `mobileworld.g1.gold-curation-browser-preview/v1` | assignment-scoped, identity-minimized CPU-only arm preview returned to the transformation workbench |
| `review_proposal.schema.json` | `mobileworld.g1.gold-curation-review-proposal/v1` | closed channel-specific human payload stored inline in an event |
| `annotation_event.schema.json` | `mobileworld.g1.gold-curation-annotation-event/v1` | authoritative flat append-only canonical JSONL event with exact formal-event G1.5 codec-gate receipt provenance |

These records deliberately remain distinct from formal G1.1 output schemas. The immutable
workspace manifest carries the complete closed false provider/GPU/model/replay/action/treatment
guard set. Every packet/event/proposal read or append MUST first validate that manifest and run the
same mandatory cross-record guard verifier; absence or any non-false value fails closed. The event
and proposal schemas therefore do not duplicate or weaken the frozen formal outputs or the
workspace guard set. None of these records is run-ready.

## 12. Explicit non-goals

ALE-324/G1.6 does not implement G1.5 live/provider codecs, G1.7 preflight or execution seals, G1.8/G1.9
treatment generation, G1.10 analysis, a deployment classifier, rubric, online middleware, model-
assisted annotation, full-task branch, action executor, or publicly hosted annotation service.
Generated transformations remain offline curated data. No generated action is executed, no
treatment response exists, and no response can be fed into another request.
