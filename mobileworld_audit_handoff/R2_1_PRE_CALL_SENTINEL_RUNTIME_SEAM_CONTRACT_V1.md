# R2.1 Pre-Call Sentinel Runtime Seam Contract v1

Status: **LOCKED for ALE-324 CPU/fake implementation**
Contract ID: `mobileworld.runtime.prompt-sentinel/contract-v1`
Receipt schema: `schemas/r2_1/sentinel_receipt.v1.schema.json`
Decision date: 2026-09-01 UTC

## 1. Decision and boundary

Runtime Sentinel has one shared interception point after a host assembles the
exact actor request and immediately before the existing provider call:

```text
host-native actor request
  -> PromptSentinel.before_model_call(...)
  -> existing provider transport
  -> existing response normalization and action parser
```

The versioned interface is:

```text
PromptSentinel.before_model_call(
    request,
    context,
    history_codec_id,
    call_role,
) -> SentinelResult
```

`SentinelContext` MUST bind a non-secret `host_id` and a stable
`logical_call_id`. For an admitted request, `SentinelResult` MUST contain an
untouched raw-request snapshot, a separately constructed final request, and one
schema-valid receipt. It MUST NOT contain or choose a provider response, parsed
action, GUI action, or environment transition.

The v1 semantic admission domain is the existing G1.2 canonical-JSON provider
request envelope. An SDK argument containing an opaque, non-JSON Python object
is outside that domain: BaseAgent MUST pass the exact Original objects through,
MUST NOT start Codec or policy work, and emits no synthetic hash or typed v1
receipt for that unadmitted call. The integration backstop logs only a fixed
diagnostic. A future typed treatment of opaque SDK values requires a versioned
semantic-projection plus opaque-passthrough contract.

Admission MUST inspect the caller's exact Python tree before any JSON round
trip, canonical hash, logical-call cache lookup, or receipt transaction. The
admitted tree uses only built-in JSON scalars, finite floats, exact `list`
arrays, and exact `dict` objects whose keys are exact strings. Serializer-
coercible values such as tuples or non-string dictionary keys remain outside
the domain; they MUST NOT be converted to lists/strings or collide with an
already cached admitted request.

This is an additive runtime overlay. It does not edit or weaken the accepted
G1.2 contracts, alter Collector v1 events, modify a frozen G1.3/G1.4/G1.5
publication, or change either G1.5 v1 Codec's `live_ready=false` declaration.

## 2. Modes and configuration

Mode defaults to `OFF`. A process-global kill switch and an explicit per-host
mode are required. The kill switch can only force the effective mode to `OFF`;
it cannot enable `SHADOW` or `ACTIVE`.

| Effective mode | Sentinel work | Actor-bound request |
| --- | --- | --- |
| `OFF` | no history extraction, policy evaluation, or rendering | exact Original |
| `SHADOW` | evaluate, render, and validate a deterministic would-edit candidate | exact Original |
| `ACTIVE` | evaluate, render, and validate a deterministic candidate | validated candidate, otherwise exact Original |

`call_role=sentinel` MUST bypass extraction and policy evaluation and return
Original, preventing recursive Sentinel invocation. Bypass precedence for the
receipt is `CALL_ROLE_SENTINEL`, then `GLOBAL_KILL_SWITCH`, then `MODE_OFF`.
Bypass is not a failure fallback.

`would_edit=true` means that a complete candidate differs from Original and has
passed all validation. `edit_applied=true` is permitted only in effective
`ACTIVE` mode for that validated candidate. `SHADOW` therefore may record
`would_edit=true` but MUST always record `edit_applied=false` and send Original.
On a typed fallback, `configured_mode` retains the requested host mode while
`effective_mode=OFF` records that only Original may proceed.

R2.1 permits only deterministic no-op or fake policy backends and in-process
fake providers. `ACTIVE` is test-only in this story. No real provider/client,
model call, network, GPU, model load/service, MobileWorld backend, GUI/tool
action, replay, or returned-action execution is authorized.

`SHADOW` and `ACTIVE` require an explicitly configured receipt sink and a
successful receipt-transaction admission before request validation, Codec
extraction, or policy evaluation. A missing sink, legacy emit-only sink, or
failed admission is `SIDECAR_FAILURE`, evaluates no policy, and forces Original.
The admitted transaction is committed only after the final result is known. A
commit failure can therefore occur after policy evaluation, but still forces a
typed `SIDECAR_FAILURE` and exact Original before any actor provider send.
Default `OFF` and recursion/kill bypasses remain safe without a sink because
they perform no semantic work; their receipt publication is best-effort.

The configured policy deadline is a real bounded wait. A policy worker that
does not finish before the deadline is abandoned as a daemon computation and
the actor immediately receives Original; its late return can never update the
cached result or reach the actor provider. The worker receives detached copies
of the admitted request, call context, and validated History IR; the
authoritative raw request, context, and IR used by admission and rendering are
never lent to replaceable policy code. The global switch is checked both before
semantic work and again at candidate-selection time. A monotonic
activation generation detects both a held activation and an activate-then-
deactivate pulse during evaluation; either discards the candidate as a typed
invariant fallback to Original.
Candidate selection is the kill-switch linearization point for a logical call:
activation after that point applies to new logical decisions, while an already
validated immutable result remains unchanged across its in-flight retries.

## 3. One evaluation per logical actor decision

The host boundary MUST allocate one stable `logical_call_id` after request
assembly and before the first provider attempt. One logical actor decision has
at most one policy evaluation. The result is cached by logical-call identity
and raw-request hash, and the receipt records whether evaluation occurred with
`policy_evaluated`. That field is tracked explicitly at the policy boundary;
request, Codec, History-IR snapshot, or other pre-policy failures MUST record
`false` rather than infer evaluation from the fallback category.

The same validated `SentinelResult` MUST be reused without semantic
re-evaluation across:

- every BaseAgent application-visible transport retry;
- every adapter parse retry for the same decision, including Qwen's outer
  parse-retry loop; and
- stream creation and all later stream iteration/attempt handling.

A retry may receive a defensive copy of the cached final request. Its
history-bearing messages and all Sentinel-controlled semantic values MUST match
the cached result. The one pre-existing Base transport exception remains the
provider compatibility translation from `max_tokens` to the semantically
equivalent `max_completion_tokens` after a provider rejects the former. That
downstream wire alias MUST NOT re-run Sentinel or alter history, and Collector
attempt capture remains responsible for the exact physical payload. The
lightweight Sentinel receipt binds the hook's pre-transport raw/final request;
it does not pretend that the provider alias has identical canonical bytes.
Provider attempt IDs remain distinct and may reference the same logical-call
ID. The runtime produces one receipt for the logical call, not one per provider
attempt.

After any non-bypass `SHADOW` or `ACTIVE` attempt, including a typed fallback
that occurred before policy evaluation, reusing a logical-call ID with a
different raw-request hash is `REQUEST_DRIFT`. It MUST NOT trigger another
policy evaluation or apply the cached candidate to the changed request; only
the current Original may proceed, with the typed fallback returned. A logical
call already bypassed before semantic work remains a bypass for the current
Original and does not manufacture a failure solely because its request changed.

## 4. Transformation and invariant rules

For an admitted canonical-JSON request, the caller-owned request and every
nested value MUST remain unchanged. Runtime code works from a deep snapshot,
binds it with the G1.2 canonical JSON SHA-256, and builds the candidate and final
request as separate objects. No mutation is committed until the entire
candidate has passed validation.

Only exact spans declared by the selected History Codec are eligible for the
policy vocabulary `KEEP`, `DROP`, `REPLACE`, or `KEEP_UNCERTAIN`. R2.1 executes
only deterministic test `DROP` plans; `KEEP` and `KEEP_UNCERTAIN` produce no
plan. `REPLACE` execution is deliberately rejected in this story because the
accepted G1.2 correction renderer inserts a Sentinel-authored block beside the
current observation, which would violate R2.1's strict history-only boundary.
The shared seam and policy MUST NOT branch on a target checkpoint/model name.
Representation differences belong only in a registered, versioned Codec.

The runtime overlay MUST reuse the accepted G1.2 semantics for exact coordinate
binding, deterministic rendering, target-only diff, reversibility, and
invariant validation. It may reuse G1.5 Qwen flat-progress and MAI raw-replay
extraction/rendering through an explicit runtime capability overlay, but MUST
NOT reinterpret their frozen scientific readiness. G1.4 fake-provider capture
and invariance harnesses are test assets, not runtime dependencies.

For every valid candidate, all of the following remain invariant:

- system policy, task instruction, tools and provider protocol;
- current observation, screenshot and all other multimodal blocks;
- message roles, order, block order, and tool-call/result adjacency;
- model, sampling, decoding, streaming, and other non-history arguments; and
- every history byte outside the exact declared targets and insertions.

`REPLACE` content remains explicitly Sentinel-authored and cannot be attributed
to an earlier actor when a later runtime-plan overlay authorizes it. A policy
proposal never directly mutates or authorizes a request: the Codec renderer and
independent invariant validator own that gate. R2.1 rejects every renderer
result with a list insertion, including any change to the current-observation
block list.

## 5. Typed Original fallback

Any incomplete or invalid candidate is discarded atomically. The final request
MUST be exact Original, `edit_applied=false`, and the receipt MUST carry exactly
one of these stable reasons:

```text
POLICY_TIMEOUT
POLICY_EXCEPTION
INVALID_POLICY_OUTPUT
INVALID_REQUEST_SCHEMA
UNSUPPORTED_HISTORY_FAMILY
AMBIGUOUS_HISTORY_SPAN
HISTORY_EXTRACTION_FAILURE
RENDERER_FAILURE
INVARIANT_FAILURE
SIDECAR_FAILURE
REQUEST_DRIFT
```

No error path may send a partially rendered request, silently relocate a span,
infer a fallback Codec, retry the policy, or count Original as an applied edit.
Typed fallback and bypass fields expose stable codes; validation checks contain
only bounded, safe-grammar internal identifiers. Unsafe or externally supplied
error codes are replaced by a fixed redaction code. Raw exception text,
credentials, or request content MUST NOT be copied into the lightweight receipt.

## 6. Receipt and restricted details

Each admitted canonical-JSON logical call produces one lightweight receipt conforming to
`mobileworld.runtime.sentinel-receipt/v1`. It binds:

- logical call, host, call role, configured/effective mode, bypass and kill
  switch state;
- Codec and policy identities;
- a canonical hash of every structurally valid, canonicalizable complete policy
  output (decision records plus the optional transformation plan), without
  embedding that output, including an output later rejected by R2.1 admission;
- raw, candidate, and final canonical request SHA-256 values (the candidate
  hash equals Original when no complete candidate exists);
- decision kinds, the policy-output hash, whether evaluation occurred, and
  would-edit/applied state;
- typed fallback, validation status/checks, exact-diff SHA-256, and latency.

The policy-output type/structure gate runs before canonicalization. It accepts
only a recursively closed graph of exact trusted base dataclasses, exact tuple
containers, exact expected enums and scalars, and acyclic strict built-in JSON
values.
Policy-owned subclasses, custom containers, and other untrusted graph types are
invalid output. Instance serializer shadows on otherwise exact base objects are
ignored: they are neither copied into the snapshot nor invoked.

The runtime MUST rebuild every admitted field into one detached Sentinel-owned
snapshot and produce canonical bytes with a module-owned serializer. It MUST
never call a policy-owned `to_dict()` or later consume the policy's original
object graph. The policy-output hash, decision census, duplicate and operation-
binding checks, R2.1/G1.2 admission, and renderer MUST all consume that one
snapshot. Once the snapshot is canonicalizable, its hash is fixed before
decision-ID uniqueness, operation binding, R2.1 operation admission, or G1.2
plan validation. Hashing records the rejected output; it does not authorize it.
An arbitrary, subclassed, or non-canonicalizable return retains the empty-
output sentinel because no complete trusted snapshot was admitted for hashing.

The same reference-isolation rule applies at the Codec boundary. A Codec's
extracted History IR is recursively rebuilt from exact trusted base dataclasses
before validation; replaceable policy code receives only another detached copy.
Cycles, subclasses, custom containers, or other non-canonical values in the
extracted IR fail as `HISTORY_EXTRACTION_FAILURE` before policy evaluation and
therefore record `policy_evaluated=false`.
Before calling a Codec renderer, Sentinel computes the authoritative G1.2
result from the trusted IR and policy snapshot. The renderer receives detached
request/IR/plan copies, and its returned result must match that precomputed
result through a recursively exact, field-owned canonical projection. Sentinel
uses only the precomputed result for candidate bytes and the exact-diff hash.
Renderer subclasses, virtual serializers, or mutations of renderer-owned
copies therefore cannot change the receipt-bound target or actor request.

The lightweight receipt is hash-only: it MUST NOT embed a request view, history
text, exact diff bytes, evidence text, provider credential, secret, or chain of
thought. Its `request_views_persisted` and `exact_diffs_persisted` fields are
therefore always `false`; they describe the contents of this receipt.

For this bounded R2.1 CPU tranche, “record operations and exact diff” means
binding the complete policy output and exact in-memory diff by canonical
SHA-256 in the lightweight receipt. Their preimages are intentionally not
persisted by the v1 sink, so this receipt alone is not a standalone replay or
operation reconstruction artifact. A future restricted-detail channel requires
its own versioned contract and explicit configuration; it is not silently
manufactured by this story.

An external lightweight-receipt sink MUST reserve and sync a private owner-only
transaction inode before semantic work. Commit MUST replace its fixed non-secret
admission probe with the complete receipt, sync and validate that inode, then
atomically publish the final logical-call name without replacement. Admission
or commit failure before publication leaves no final receipt; after atomic
publication, cleanup failure cannot retroactively force the actor to Original
against an already visible ACTIVE receipt.

The runtime validates its authoritative `SentinelResult` before commit and lends
the transaction only a detached receipt copy. A transaction MUST NOT mutate that
copy; the seam compares its canonical value again before releasing the actor
request and treats detected mutation as `SIDECAR_FAILURE`. Thus a replaceable
sink cannot change the authoritative receipt by retaining or modifying the
object reference it receives. A custom sink that publishes bytes other than the
detached value violates this transaction contract and is not a compliant v1
sink.

The v1 external sink uses Linux anonymous `O_TMPFILE` storage and an fd-bound
`/proc/self/fd` no-replace hard link. Lack of either capability fails safe as
`SIDECAR_FAILURE`; cross-platform publication requires a later implementation
with equivalent fd-bound admission and atomicity guarantees.

Request views and exact diff bytes MAY be written only when an explicitly
configured access-controlled root is outside the Git repository. Such a
restricted detail artifact MUST be atomically written, content-addressed,
credential-excluded/redacted under the Collector policy, and bound by the
receipt's hashes. If no compliant root exists, the runtime computes hashes and
validation in memory and persists no views or diff bytes. Derived runtime data
MUST NOT be written back into raw Collector events.

## 7. Acceptance and consequences

R2.1 is accepted only when CPU tests prove:

1. default `OFF`, global kill-switch, and Sentinel-call bypass parity with the
   existing actor-bound request;
2. `SHADOW` computes at most once but sends Original;
3. fake `ACTIVE` sends only a fully validated history-only candidate;
4. transport retries, Qwen adapter retries, and streaming reuse one result and
   one logical-call ID;
5. caller immutability, exact non-history invariants, held and pulsed kill-switch
   activation, pre-policy sidecar admission, post-policy commit failure, atomic
   fallback, strict pre-copy/pre-cache JSON-domain rejection, recursively
   trusted and detached policy-output snapshotting, rejected-output hashing
   before later admission checks, acyclic History-IR snapshot admission,
   explicit policy-evaluation census, detached sidecar commit inputs, every
   typed failure reason, and the closed receipt schema;
6. the existing fake provider receives the expected request while provider
   response normalization and host action parsing remain unchanged; and
7. no live provider/model/GPU/backend/action path was invoked.

Completion of R2.1 unblocks interface work in ALE-325/R2.2 and ALE-326/R2.3. It
does not yet admit an automatic policy plan for ACTIVE execution: the reused
G1.2 `TransformationPlan` deliberately requires `curated=true` and
`deployment_prediction=false`. R2.2 MUST introduce an additive, versioned
runtime proposal/admission overlay rather than falsely claiming that an
automatic proposal is curated or weakening the G1.2 validator. R2.1 also does
not establish an evidence-grounded policy, rubric, Qwen/MAI live vertical
slice, performance improvement, or causal effect.
