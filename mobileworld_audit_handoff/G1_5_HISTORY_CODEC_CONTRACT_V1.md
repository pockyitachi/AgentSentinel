# G1.5 Qwen / MAI History Codec Contract v1

Status: **CPU checkpoint contract locked; live-smoke execution deferred and unauthorized**

Issue: **ALE-323 / G1.5**

Decision authority: **`G1_5_DECISION_LOG.md` D-028**

## 1. Contract boundary

This contract adds two family-specific History Codecs to the accepted G1.2 portable contract:

- `mobileworld.g1.history-codec.qwen-flat-progress` for Qwen flat task-progress text;
- `mobileworld.g1.history-codec.mai-raw-replay` for MAI prior-assistant raw replay.

It does not amend the frozen G1.2 IR, plan, renderer, diff, registry, or conformance contract and does
not amend G1.3 capsules or the G1.4 runner. The shared core continues to own span operations,
five-arm plan semantics, deterministic ordering, target-only diff, source mapping, reversibility,
pre-send validation, and unsupported-treatment behavior. Each new Codec owns only exact host syntax,
region/record extraction, source provenance, protected shells, related-content links, and a host-valid
correction anchor. Shared code MUST NOT branch on a model/checkpoint ID or Qwen/MAI prompt layout.

## 2. Stage split and authorization

The CPU checkpoint and live smoke are separate acceptance stages.

The CPU stage MAY copy an input JSON value in memory, parse it, construct a History IR, consume an
already curated Transformation Plan, render all five arms, validate invariants, produce machine/human
diffs, and run deterministic tests. It MUST have no provider/client/endpoint/response parameter and
MUST stop with provider decision `BLOCK`, invocation allowed `false`, invocation count `0`, treatment
response count `0`, network/GPU use `false`, and GUI action count `0`.

The live-smoke stage is not authorized by this contract. Its exact minimum matrix is the 10 logical
invocations frozen in D-028. Completing CPU extraction/rendering MUST NOT set `live_ready=true`, enable
a runner entrypoint, loosen a formal capsule guard, or be described as live proof.

## 3. Inputs and immutable binding catalog

`extract(application_request)` consumes the exact application-layer SDK arguments captured immediately
before send. The Codec MUST deep-copy or read without mutating the caller value and MUST bind
`HistoryIR.raw_request_sha256` to canonical request bytes.

The Codec MUST NOT infer a misleading claim, truth value, correction, oracle superset, or sham target.
Editable spans arrive through an immutable curated binding catalog. Every binding contains:

- a stable binding ID, the exact source-request SHA-256, and exact JSON container path;
- non-empty char and UTF-8 byte half-open coordinates;
- exact source text and its SHA-256;
- exactly `EDITABLE_CLAIM` or `BENIGN_SHAM` role.

Extraction MUST first match every binding's source-request digest, then re-resolve every path and
recheck text, digest, both coordinate systems, uniqueness, non-overlap, structural membership, and
protected-boundary separation. Bindings from different request digests MUST NOT form one catalog. The
Codec MUST NOT search for a duplicate string, relocate a stale coordinate, fuzzy-match, infer a
replacement target, or repair the request. Each record provenance binds the complete catalog digest and
source-request digest. Claim IDs bind the record, exact coordinate, and span digest. A plan separately
binds host/family/codec/version, source request, record ID, span coordinates/text digest, operation,
arm, and evidence where required.

The catalog and each record assignment are canonicalized by exact JSON path, coordinate, digest, and
binding ID. The exact same ordered binding sequence MUST generate both `editable_spans` and the
record's `curated_binding_ids`; caller order may not change the IR or associate an ID with another
span. Non-binding values or mutable/non-canonical JSON paths raise a typed error before set/sort logic.

Captured fixtures in Git are structure-preserving, secret-free surrogates derived from observed request
shapes. They record the source publication/request digests only as provenance. They copy no formal
request bytes, task meaning, history meaning, image, secret, endpoint, or credential and are not formal
G1 cases, G1.6 adjudications, or treatment inputs.

## 4. Common capability declaration

Both Codecs declare:

- `level=VALIDITY_TRANSFORMATION`, `scope=LIVE`, `contract_version=v1`;
- operations `DROP` and `REPLACE` and all five `ArmKind` values;
- exact preservation of roles, order, multimodal blocks, tool adjacency, and protocol shell;
- `live_ready=false` and `opaque_or_server_managed=false`.

`scope=LIVE` describes the intended representation fidelity. Only a later versioned live authority and
capability seal may change readiness; tests, caller assertions, `dataclasses.replace`, CLI flags, or
fallback logic MUST NOT do so.

Each extraction MUST expose SYSTEM, TASK, HISTORY, CURRENT_OBSERVATION, and TOOL_PROTOCOL regions;
provider-control fields are separately preserved when present. Record IDs, record/claim/relationship
order, representation indices, char/UTF-8 coordinates, hashes, warnings, and serialized IR MUST be
deterministic across repeated extraction.

## 5. Qwen flat-progress grammar

The admitted request has exactly two ordered messages: one system message with one text block, then one
user message with one text block followed by exactly one current `image_url` block. The user text has
one query marker, one exact task-progress marker, a non-empty sequence of contiguous `Step 1..N`
entries, an exact `; ` terminator per entry, and the final newline used by the host adapter.

For each entry the semantic conclusion is the editable interval. `Step N: `, the trailing `; `, and any
external suffix are protected. The host independently appends at most one suffix of each kind, so a
tool result and ask-user response may both be present only in this exact order:

- `; Tool call result: <tool_response>opaque non-empty adapter payload</tool_response>`; then
- `; Ask user response: ...` with a non-empty response.

Either suffix may occur alone or neither may occur. A tool wrapper MUST be terminal unless the exact
ask-user suffix immediately follows its closing tag. Missing, empty, repeated, reversed, nested,
unbalanced, or non-contiguous wrappers are ambiguous and MUST block extraction. The tool payload is
protected opaque text because the host strips quote/newline characters while flattening conclusions;
the Codec MUST NOT reinterpret it as JSON. Masking the whole selected conclusion therefore leaves a
valid `Step N: ; ` shell and preserves every external result exactly. A correction is a new
Sentinel-authored text content block inserted immediately before the current screenshot; it never
rewrites actor-authored progress.

The flat host does not escape an actor conclusion that itself contains the exact `; Step N: ` delimiter,
so request bytes alone cannot distinguish such content from a host-added boundary. Formal use MUST
retain the G1.3 capture-provenance boundary admission and request-hash-bound curated catalog; the
standalone parser does not claim provenance-free disambiguation of an adversarial delimiter. This CPU
checkpoint exercises only admitted captured shapes, and `live_ready=false` remains mandatory.

## 6. MAI raw-replay grammar

The admitted request starts with scalar system text and one task user text block, contains at least one
prior assistant scalar response, and ends with one user message containing exactly one current
`image_url` block. A historical assistant record contains exactly one ordered tool-call JSON-object
shell and one of the two raw thinking shapes emitted/accepted by the host adapter: canonical
`<thinking>...</thinking><tool_call>...</tool_call>`, or the preserved legacy provider response
`...</think><tool_call>...</tool_call>` for which the adapter adds/normalizes the opening/closing tag
only in its local parser copy. The captured request is never normalized. The non-empty trimmed
thinking interior is the only editable interval; all wrappers, inter-wrapper whitespace, and the
complete tool-call payload are protected.

Only whitespace may precede the canonical opening tag, appear between the thinking close and tool-call
open, or follow the tool-call close. Non-whitespace actor text outside the admitted thinking wrapper is
not silently protected; it makes the raw shell ambiguous and blocks extraction.

Visible historical user observations contain one text or image block. Tool-result/ask-response text
MUST immediately follow its assistant record. The initial screenshot may legally remain before the
first assistant; older screenshot messages may instead have been removed by the host history-retention
rule, so consecutive assistant records are also valid and MUST NOT be treated as broken alternation. A
post-action visible observation is linked to its immediately preceding assistant action and retained
byte-for-byte. An orphan/misordered visible text result, unsupported role/block, empty assistant
reasoning, malformed tool JSON, or ambiguous wrapper blocks extraction.

G1.5 v1 admits the formal-corpus current-call shape whose final user message is exactly one screenshot.
The host can also construct a final text user message when the immediately current observation is a
tool/ask result; that distinct source shape is outside this v1 CPU publication and fails closed rather
than being coerced to a screenshot. Extending it requires a new captured fixture, contract version, and
capability receipt.

Masking the complete selected reasoning leaves the assistant message and its exact original shell
(`<thinking></thinking><tool_call>...</tool_call>` or `</think><tool_call>...</tool_call>`) intact. A
correction is a new Sentinel-authored text block inserted before the current image inside the current
user content list. It never edits an old assistant message and therefore cannot fabricate retroactive
actor speech.

## 7. Five-arm rendering and invariants

All rendering delegates to the frozen G1.2 renderer under `G1_SCIENTIFIC` plus `BLOCK`:

- ORIGINAL: canonical request bytes are unchanged; no diff or insertion exists.
- MASK: exactly the curated focal span set is deleted.
- MASK_CORRECTION: exactly the focal span set is deleted and one evidence-bound, explicitly
  `SENTINEL` correction is inserted at the declared current-context anchor.
- ORACLE_CLEAN: exactly the frozen curated superset is deleted; unrelated history is not regenerated,
  summarized, or normalized.
- SHAM_BENIGN_EDIT: exactly one span declared `BENIGN_SHAM` is deleted.

System, task, tool schema/protocol, provider/model/sampling fields, message roles/order, block order,
non-target history, every image/data URL, current observation, and caller inputs MUST remain unchanged.
Every destructive diff records raw record/claim identity, exact path, original char coordinates/text,
operation identity, and rendered mapping. `restore_original(render_result)` MUST reproduce the exact
source value. Repeated extraction/rendering and human diff output MUST be byte deterministic.

The current frozen G1.2 core rejects multiple corrections that share one insertion point with
`AMBIGUOUS_CORRECTION_ANCHOR`. G1.5 v1 supports a single focal correction per request and MUST fail
closed for that multi-correction shape; it does not change the core or select an implicit ordering.

## 8. CPU checkpoint and G1.4 runner integration

The provider-free checkpoint validates the five-plan paired set with the registered real Codec, renders
each arm, runs G1.2 pre-send validation and G1.4 capsule-aware invariance, and emits a
`mobileworld.g1.history-codec-cpu-checkpoint/v1` record. Before doing so it rechecks that the capsule
safety guards and response count remain exact false/zero. Every arm receipt MUST still block provider
invocation while proving target-only diff and reversible source mapping.

G1.4 integration tests register the real Codec with `HistoryCodecRegistry` and a deterministic fake
Provider Codec, then prove preflight stops before provider encode, send, or normalize because
`live_ready=false` and the capsule forbids invocation. LIVE domain preflight and `execute_live_arm`
remain `LIVE_EXECUTION_DEFERRED`. This is interface integration, not a fake or live response test.

The checked-in `g1_5/cpu_publication_manifest.v1.json` content-addresses the two deterministic
checkpoint receipts and selected Codec bindings. Each selection binds codec ID/version/family,
implementation bytes, the complete capability declaration/digest, secret-free source fixture/request,
and receipt bytes. Shared bindings freeze the accepted G1.2 History-IR schema and renderer bytes. Host
text spans use exact JSON paths, Unicode-code-point offsets, and UTF-8 byte offsets with no text
normalization; no model tokenizer is consulted, so the separately hashed coordinate binding records
`tokenizer_required=false`. A later G1.6 repo-external gate may verify these hashes but may not infer
live readiness from them.

## 9. Failure and fallback rules

All structural, binding, IR, plan, render, invariant, capability, or authorization errors MUST be stable,
deterministic, `provider_invocation_allowed=false`, and occur before any provider boundary. The
capability/fallback matrix is normative in
`G1_5_HISTORY_CODEC_CAPABILITIES_V1.md`.

An unsupported treatment may yield only the frozen core's explicit
`BLOCKED_BEFORE_PROVIDER`/`UNSUPPORTED_ARM_OR_OPERATION` result with `effective_arm=null` and
`count_as_treatment=false`, or raise a stable blocking error. It MUST NOT emit Original bytes while
counting or reporting the request as a treatment.

## 10. Acceptance evidence

CPU acceptance requires both codecs to pass the same conformance matrix over schema-validated,
secret-free fixtures: deterministic extraction and IDs; multilingual/emoji dual coordinates; exact
regions and protected spans; Original, single/multiple Mask, Mask+correction, multi-target Oracle, and
Sham; empty semantic-content shell preservation; multimodal and observation adjacency preservation;
machine and checked-in human golden diffs; target-only invariance; exact reversibility; caller
immutability; stable negative cases; runner preflight blocking; frozen G1.1–G1.4/source-bound hashes;
schema meta-validation; lint, formatting, typing/compile checks; focused and full regression tests.

The CPU publication manifest and both checked-in checkpoint receipts MUST validate against their
Draft 2020-12 schemas, reproduce fresh in-memory checkpoint output byte-for-data, and resolve every
artifact reference to the recorded SHA-256. The publication file SHA-256 is the external binding key;
the manifest intentionally contains no self-referential digest or mutable environment path.

Passing this evidence establishes only `CPU_CHECKPOINT_IMPLEMENTED_LIVE_SMOKE_DEFERRED`. ALE-323
remains incomplete until the separately authorized D-028 live-smoke matrix is sealed.
