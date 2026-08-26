# G1 Causal Replay Protocol v1

Status: **LOCKED for ALE-319 / G1.1**  
Protocol ID: `mobileworld.g1.causal-replay/protocol-v1`  
Authorization: `DECISION_LOG.md` D-021  
Decision date: 2026-08-26 UTC

## 1. Question and scope

G1 asks one narrow causal question: at a natural GUI-agent decision where the frozen
motivation audit found an explicitly reused misleading history premise, does changing only
the model-visible history view improve the actor's **next structured action** while every
other model input and configuration is held fixed?

G1 is an offline derived experiment. It does not modify Collector v1, execute the generated
action, branch a full task, change model weights, or implement a runtime Sentinel. Every
protocol, registry, transformation, gold, run, and outcome record must contain:

```json
{"curated": true, "deployment_prediction": false}
```

The live study hosts are fixed:

| Role | Model | History family |
| --- | --- | --- |
| Primary | Qwen3-VL-8B-Instruct | `flat_progress` |
| Replication | MAI-UI-8B | `raw_replay` |

A substitution requires a versioned amendment made before any response from the replacement
model is observed.

## 2. Paired unit and estimand

The paired unit is:

```text
task × frozen decision capsule/request state × model configuration × decoding seed
```

The two technical repeats are nested observations within that paired unit; they are not separate
scientific units.

Within a unit, task instruction and parameters, current GUI, request images, system policy,
tool schemas, message roles, all non-history content, model revision, provider codec, parser,
decoding configuration, seed, and repeat isolation are fixed. Only the curated history arm may
change. The historical capture did not send a provider seed. Formal replay adds the frozen
experimental `seed` as an explicitly allowed replay-control field to **every arm in the same
block**, including `ORIGINAL`; it is not a history edit and may not vary within a pair.

For task `t`, decision case `c`, seed `s`, repeat `r`, and arm `a`, let `Y_tcsr(a)=1` only when
the parsed next action matches an independently curated accepted-action predicate. The primary
model-specific estimand is the task-equally-weighted change in gold alignment:

```text
Delta_m(a) = mean_over_tasks(
  mean_over_cases_in_task(
    mean_over_seeds_and_repeats(Y(a) - Y(ORIGINAL))
  )
)
```

The co-primary contrasts are `MASK - ORIGINAL` and
`MASK_CORRECTION - ORIGINAL`. `ORACLE_CLEAN - ORIGINAL` is a secondary curated reference
contrast, intended as an oracle-style benchmark but not assumed to be a numerical upper bound.
This estimand applies only to audited natural decisions with explicit uptake; it is not an ATE
over all GUI decisions and is not a task-success or success-rate estimand.

## 3. Frozen candidate universe

The strict-MHR **intervention census** is final-outcome-blind, local-harm-blind, and
treatment-blind. It uses only the frozen task cards, final motivation reviews, exact request
reconstruction, and raw request/state blobs. Outcome sidecars and failure-link artifacts are
forbidden builder inputs. The separate clean-control pool may use the already frozen pre-G1
natural-trajectory `NO_VISIBLE_HARM` review label to identify benign candidates; it never uses a
G1 treatment response or final-task failure label.

An intervention candidate must satisfy all of the following before G1.6:

1. task review coverage is `SUFFICIENT`;
2. collection integrity and capture are complete, reconstructed decisions equal captured
   decisions, and the card reports no dropped candidate;
3. the exact source-to-target exposure was actually in the target request;
4. target-span provenance is `EXACT` or `HIGH`;
5. adjudicated validity is `REFUTED` or `STALE`;
6. uptake is `EXPLICIT_USE`;
7. the frozen strict-MHR low-confound gate is met (`NONE` or
   `CURRENT_GUI_CONTRADICTS_PREMISE`);
8. the application-layer SDK request and current GUI image resolve from immutable blobs and
   match their size and SHA-256 bindings;
9. the audited request exposure resolves exactly under the representation-specific mapping. For
   Qwen, G1.1 can bind the conclusion span and its enclosing `Step N` record. For MAI, whose audit
   exposure is an entire raw assistant reply, G1.1 binds the complete raw record and may record a
   uniquely parsed `<thinking>` element only as a non-editable curation envelope; it does not
   infer a premise edit span;
10. the original next action was accepted by the pinned production parser and is neither an
    unknown nor environment-failure placeholder;
11. the model call is proven to depend only on the serialized request. If it can consult live
    external state, a restorable or deterministically reproducible backend checkpoint is
    mandatory.

The resulting census is frozen before gold or treatment responses:

- Qwen: 139 unique target decisions across 35 tasks;
- MAI: 13 unique target decisions across 7 tasks.

No intervention candidate is sampled using local harm, final outcome, failure-link status, task
score, or a treatment response. Multiple audited premises at the same provider call form one
decision case;
they must not be emitted as duplicate independent cases. G1.1 freezes an ordered set of exact
audited exposure bindings for that call. A case ID depends only on the model, task run, provider
call, request/state capsule, and model configuration; it never depends on candidate ordering or a
future curated edit span. Repository or source-config aliases such as `source_key` are provenance
metadata and are not part of the paired-unit identity.

For `raw_replay`, the frozen source binding contains the request path, message index, full-record
character and UTF-8 byte bounds, and full-record SHA-256. Its `focal_edit_spans` are empty,
`edit_span_status=G1_6_PENDING`, and every treatment arm remains `execution_ready=false` in G1.1.
If the raw reply has one unambiguous `<thinking>` element, its exact bounds and hash may be stored
as `curation_envelope`; that envelope is not itself an adjudicated premise or treatment span.

G1.6 independently freezes the ordered `focal_target_set`: the smallest semantically self-contained
misleading premise spans covering all strict-MHR audit targets at the provider call, sorted by
`message index × content-item index × start offset × span hash`. Each exact-offset/hash span
must lie within its source record and any declared curation envelope, may not overlap another
target, and for MAI may not intersect the `<tool_call>` tag, payload, or valid action record. The
validator rejects a raw-replay G1.1 candidate with an executable edit, an `INCLUDED` case whose
span is still pending, or an arm plan that does not bind the sealed span-set hash.

G1.6 also freezes an ordered `oracle_target_set` containing every
independently confirmed misleading premise relevant to that decision. It must be a superset of
the focal set. These sets, rather than an implementation-time tie-break, define each arm.

## 4. G1.1 versus G1.6 admission

G1.1 freezes source identities, eligibility facts, request/state hashes, target spans, the
candidate census, exclusion codes, schemas, and deterministic admission rules. It does **not**
invent an accepted action set, correction, oracle view, or sham span from the observed action.

Registry states are:

- `CANDIDATE_FROZEN`: eligible pre-treatment decision, pending independent gold curation;
- `INCLUDED`: G1.6 gold action set and all required transformation plans exist, pass independent
  review, and resolve by content hash;
- `EXCLUDED`: one of the predeclared reasons applies before any treatment response exists.

G1.6 may append content-addressed gold/transformation bundles but may not delete, reorder, or
silently replace G1.1 candidates. Its `curation_and_admission_sealed` registry is a deterministic
seal of the frozen census plus those bundles. It may set `admission_ready=true`, but it must keep
`execution_ready=false` and `treatment_response_generation_allowed=false`. Only G1.7 may produce
an execution-ready replay pack after also sealing the serving image, seed support, provider codec,
parser/scorer, isolation, and analysis implementation. The G1.6 validator must reject `INCLUDED`
if any gold or arm reference is missing, hash-mismatched, or future-leaking.

The seal parses, schema-validates, and cross-validates the referenced accepted-action bundle,
transformation plan, and their review ledgers; an opaque byte blob is never sufficient. Each
applicable artifact has two distinct primary reviewer identities, a frozen resolution record when
they materially disagree, and a non-empty final accepted predicate/plan. Evidence locators,
cut-off steps, and oracle target membership live inside the content-hashed artifact bytes rather
than in an unbound registry-side annotation.

An `EXCLUDED` disposition is equally fail-closed. A curation reason must be the exact final
`EXCLUDE` resolution of a two-reviewer, same-unit, same-input ledger (plus an identity-disjoint
adjudicator on material disagreement). `NO_GOLD_CONSENSUS` is closed by the action-gold ledger;
`NO_VALID_CORRECTION`, `NO_VALID_ORACLE_VIEW`, and `NO_MATCHED_SHAM` are closed by the
transformation ledger. A mechanical reason must equal the failure code reproduced by the pinned
validator. The admission receipt distinguishes included validation from validated exclusion,
marks inapplicable checks as `NOT_APPLICABLE`, and records `exclusion_reason_valid=true`; a
self-reported reason or an early return without evidence cannot remove a frozen candidate.

The G1.1 publication is explicitly `admission_ready=false`, `execution_ready=false`,
`run_ready=false`, `included_count=0`, and
`gold_validation_status=NOT_APPLICABLE_PRE_G1_6`. Its dry validator must nevertheless report zero
unresolved **source/capsule** references and zero forbidden post-target references in every
pre-gold evidence channel. It must not use an empty gold set to claim that curated gold or
transformations already passed future-leakage validation. G1.6 publishes a new append-only
admission ledger and curation/admission seal; it never rewrites this candidate registry and never
authorizes treatment generation.

Allowed exclusion codes are:

```text
SOURCE_REFERENCE_UNRESOLVED
REQUEST_HASH_MISMATCH
STATE_HASH_MISMATCH
TARGET_SPAN_UNRESOLVED
PROVENANCE_BELOW_HIGH
NOT_REFUTED_OR_STALE
NO_EXPLICIT_UPTAKE
NOT_STRICT_MHR
ORIGINAL_ACTION_UNPARSEABLE
BACKEND_CHECKPOINT_REQUIRED_BUT_MISSING
FUTURE_EVIDENCE_LEAKAGE
NO_GOLD_CONSENSUS
NO_VALID_CORRECTION
NO_VALID_ORACLE_VIEW
NO_MATCHED_SHAM
ARM_PROTOCOL_INVALID
DUPLICATE_CAPSULE
```

Natural success/failure, harm type, failure-link label, and replay behavior are never exclusion
reasons.

## 5. History arms

Every edit is expressed against a hash-pinned record and exact character/UTF-8 span. Renderer
grammar repair is recorded separately and may not add semantic content.

### `ORIGINAL`

The host-native request is reconstructed without a history edit. The application-layer messages,
images, tools, roles, policy, and captured decoding arguments must be byte-equivalent or
codec-proven semantically equivalent to the frozen application request. The only permitted replay
envelope deltas are transport-volatiles enumerated by schema (for example a new request ID) and the
pre-registered experimental `seed`. The same seed and envelope rules apply to every arm in the
paired block. Reports must distinguish the historical natural action from the seeded replayed
`ORIGINAL`; natural-action agreement is descriptive, not an invariance requirement.

### `MASK`

Remove every span in the case's frozen `focal_target_set` and no other semantic span. Edits are
applied in descending byte-offset order within each record. Delimiter repair may remove now-empty
syntax but must not change another record or insert an explanation, next-step hint, or correction.
A repair is valid only when applying all selected target deletions makes that exact syntax empty or
orphaned: a `Step`/`Thought` marker may be removed only when no non-whitespace semantic content
remains in its record, paired `<thinking>` tags only when their interior becomes whitespace-only,
and a separator only when it becomes an isolated record boundary. Mere adjacency to a target is
not sufficient.

### `MASK_CORRECTION`

Remove the same complete `focal_target_set` and insert, at each corresponding semantic record
location, the shortest evidence-supported correction. The correction may state the corrected
historical fact; it may not recommend an action, expose a gold predicate, or use post-target
evidence.
Delimiter repair is evaluated independently for this arm against the final transformed record,
including every non-empty correction insertion. It is not copied from `MASK`: syntax retained
around a correction is not empty merely because `MASK` would have emptied it.

### `ORACLE_CLEAN`

Remove exactly the frozen `oracle_target_set` and preserve every other request record. This is an
offline curated reference arm, not a deployable policy or a guaranteed numerical upper bound;
removing more misleading content need not improve a non-monotonic model.

### `SHAM_BENIGN_EDIT`

Delete one complete, independently curated benign history span with no entailment, contradiction,
lexical alias, task-hard-requirement, or accepted-action-discriminant relation to the focal fact.
The operation is deletion, not free-form replacement; only the same deterministic empty-delimiter
repair allowed for `MASK` may follow it. Prefer a span in the same history record; otherwise its
history-depth difference is at most one. Length is measured with the exact model tokenizer at the
pinned model revision, with special tokens disabled. When a focal set contains more than one
span, tokenize each raw span separately and sum those counts in frozen order; no synthetic
delimiter is tokenized. The benign span must be 80–125% of that focal-union count or differ by at
most four tokens, with inclusive endpoints. Implement this without floating point: it matches
exactly when
`(5*benign_tokens >= 4*focal_tokens && 4*benign_tokens <= 5*focal_tokens) ||
abs(benign_tokens-focal_tokens) <= 4`. Its
structural bucket must match the first focal span's bucket: the same provider message role,
content-item kind, representation record class, and the same leading/middle/trailing third of its
containing record by character offset. For a non-empty record of `N` Unicode code points and a
span starting at zero-based code-point offset `s`, the third index is exactly
`min(2, floor(3*s/N))`, mapped to `LEADING`, `MIDDLE`, `TRAILING`; empty records are ineligible.
History depth is counted backward from the target request over complete semantic history records:
the most recent complete record has depth 1, and spans in the same record share one depth. The
registry freezes the tokenizer reference, the focal
union and benign token counts, both location buckets, and the exact deletion plan. All other
content is unchanged.

Separate clean-control cases use a frozen `SUPPORTED`, actually exposed, `EXACT/HIGH` benign
record with `NO_VISIBLE_HARM`. They run only `ORIGINAL` and `SHAM_BENIGN_EDIT`. G1.1 freezes a
deterministic pool; G1.6 must still confirm the span is benign.

## 6. Accepted next-action sets

G1.6 separates two independently reviewed views. Accepted-action curators receive only the exact
model-visible task-instruction bytes, current GUI/accessibility state, and non-history tool or
ask-user evidence available no later than the request cut-off. A public task field is allowed only
when its source provenance proves `model_visible_at_or_before_request=true`; the complete task
parameter object remains hash-pinned eligibility/capsule metadata and is never copied wholesale
into either curator view. Hidden generator/checker targets, success predicates, evaluator state,
and benchmark-private parameters are forbidden regardless of field name. Curators do **not** see
the model-visible history,
misleading target, natural target prediction/action, target post-state, later trajectory,
benchmark evaluator, final outcome, or any replay response. Transformation curators may see the
exact pre-response history and pre-cut-off evidence needed to define `MASK`, correction,
oracle-clean, and sham plans, but they see none of the natural target prediction/action or later
evidence. Gold-action review and transformation review use disjoint reviewer identities; each
artifact requires at least two independent reviews and frozen resolution before admission.
Every action-gold curation input and accepted-action bundle must contain at least one exact
`task_instruction` reference and at least one exact `target_pre` current-GUI reference; neither
can substitute for the other. Every transformation curation input must contain at least one exact
`source_history` reference to the history being edited. Tool and ask-user evidence remain
optional, subject to the same cut-off and visibility rules.

The accepted set is a closed set of predicates over the production parser's normalized action:

- click: one or more screenshot-grounded polygons/bounding boxes and explicit tolerance;
- drag: allowed start/end regions, direction, and minimum displacement;
- input: Unicode normalization plus exact allowed text variants;
- navigation/tool/ask-user/answer/finished: typed field predicates;
- any coordinate or text tolerance is case-local and frozen before replay.

For an exact normalized-action predicate, the declared action type must equal the referenced
normalized action's type. Text-variant predicates use the fixed production mapping
`input_text|answer|finished|ask_user -> text`, `open_app -> app_name`, and
`status -> goal_status`; any other action-type/field combination is invalid.

If curators cannot enumerate all reasonable one-step actions, the case is excluded as
`NO_GOLD_CONSENSUS`; unlisted but reasonable actions must not be forced into `WRONG`.

For corrections, "shortest" means the fewest tokens under the case's pinned model tokenizer with
special tokens disabled. Ties break by UTF-8 byte length, then Unicode code-point length, then
lexicographic UTF-8 bytes. Two curators work independently; any disagreement in accepted-action
predicates, target sets, correction bytes, sham span, or delimiter repair goes to a third,
identity-disjoint adjudicator. All original reviews and the adjudication are hash-pinned.

Outcome precedence is machine-fixed:

1. exhausted provider attempts → `MISSING`;
2. exactly one production-valid structured action is present: wait, zero-displacement gesture, or
   outside-viewport click → `ACCEPTABLE` only if explicitly gold-allowed, otherwise `NO_OP`;
   another accepted-predicate match → `ACCEPTABLE`; every other substantive action → `WRONG`;
3. no legal action and the pinned explicit-refusal classifier matches → `REFUSAL`;
4. zero legal actions without refusal, or more than one legal action → `UNPARSEABLE`.

The refusal classifier, action-count rule, and production parser are code- and schema-hashed
before replay. Refusal text accompanied by one valid action is scored from that action, not as a
refusal. `answer` and `finished` are acceptable only when the gold set explicitly permits them.

## 7. Evidence separation and future-leakage rule

The registry has three disjoint evidence channels:

- `eligibility_only_refs` may bind the natural target prediction/action because explicit uptake
  and original parseability are eligibility facts;
- `action_gold_refs` may bind exact model-visible task-instruction bytes, current `S_t`,
  non-history accessibility/tool/ask-user evidence, and other fields carrying verified
  `model_visible_at_or_before_request=true` provenance; it must not expose the complete hidden
  task-parameter object, model-visible history, or any natural target output;
- `transformation_refs` may additionally bind exact pre-response history records and prior-step
  evidence needed to define mask/correction/oracle/sham edits.

The first channel must include both `task_instruction` and `target_pre`; the second must include
`source_history`. These are minimum required roles, not optional examples.

Neither `action_gold_refs` nor `transformation_refs` may reference target `P_t`, target `A_t`,
target transition or `S_t+1`, later steps, `task_ended`, outcomes, evaluator/checker output,
failure-link cards, or treatment responses. Eligibility lineage may retain hashes of the frozen
audit but is never shown to either curator group. The validator enforces the two distinct
allowlists and rejects a reference copied across channels.

## 8. Integrity and contamination

- All canonical source, registry, arm, gold, run, and outcome bytes are SHA-256 bound.
- Source blobs are rehashed at build, seal, preflight, and run time.
- A non-history field mismatch invalidates the arm before provider invocation.
- Generated actions are never executed and responses are never fed into another request.
- Every arm invocation starts without conversation, KV-cache, or session carry-over.
- A provider/HTTP 5xx/timeout gets at most two retries after the first attempt; request bytes,
  seed, and decoding config must remain identical.
- Parser failure, refusal, empty output, and no-op are outcomes and are not retried.
- Missing seeds and cases are not replaced after responses are visible.
- Fixing contamination requires a new protocol/run version; observed results cannot choose which
  old records are retained.

A G1 run record is an immutable invocation plan and therefore always has `status=PLANNED`.
Attempt, completion, parse, missingness, and retry state live only in append-only outcome records;
the plan is never rewritten to represent execution state.

The captured Qwen and MAI calls use only serialized messages/images and do not query a live
backend. G1.7 must re-prove `backend_dependency=NONE`. Any later live dependency changes the case
to checkpoint-required.

## 9. Minimum admission-sealed sample

The admission rule is census-first: include every candidate that satisfies the frozen G1.6
curation and arm validators. G1.7 may not mark the replay pack execution-ready unless the
admission-sealed registry has at least:

| Stratum | Intervention cases | Distinct original tasks | Clean cases | Distinct clean tasks |
| --- | ---: | ---: | ---: | ---: |
| Qwen primary | 30 | 25 of 35 | 30 | 30 |
| MAI replication | 8 | 5 of 7 | 8 | 7 |

Falling below a minimum cannot be repaired by relaxing criteria after treatment responses.

## 10. Storage and immutability

Full registries, gold bundles, reconstructed requests, screenshots, treatment responses, and run
outcomes live under a new repo-external content-addressed root. The repository stores only
protocol/schema/code, hashes, manifests, and non-secret references. Publication is write-once;
an amendment creates a new protocol and registry ID rather than replacing a frozen file.

G1.1 freezes the complete analysis algorithm and thresholds in the locked analysis plan, but the
G1.10 executable analyzer does not yet exist and no hash is invented for it. Before the first
treatment response, G1.7 must hash-seal an implementation plus deterministic conformance vectors
that reproduce this plan exactly. A semantic mismatch requires a versioned pre-response
amendment; it may not be repaired after outcomes are observed.

## 11. Explicit limitations

- Cases are enriched for natural explicit uptake and do not represent all GUI decisions.
- Masking changes semantics and token layout; sham controls estimate but cannot eliminate every
  attention/layout effect.
- `MASK_CORRECTION` identifies a deletion-plus-correction joint intervention.
- `ORACLE_CLEAN` is a curated reference benchmark, not a guaranteed numerical upper bound.
- Accepted next action is a surrogate endpoint; G1 makes no task-completion, SR, or production
  Sentinel claim.
- G1.1 creates no treatment model response and no deployment prediction.
