# R2.2 Evidence-Grounded Sentinel Policy Contract v1

Status: **LOCKED for ALE-325 CPU/offline/fake SHADOW implementation**
Contract ID: `mobileworld.runtime.evidence-grounded-sentinel-policy/contract-v1`
Evidence packet schema: `schemas/r2_2/evidence_packet.v1.schema.json`
Proposal schema: `schemas/r2_2/policy_proposal.v1.schema.json`
Policy receipt schema: `schemas/r2_2/policy_receipt.v1.schema.json`
Decision date: 2026-09-02 UTC

## 1. Decision and authority

R2.2 adds one versioned, replaceable history-validity policy behind the
accepted R2.1 pre-call seam:

```text
exact actor-bound request plus causally available evidence
  -> role-projected EvidencePacketV1
  -> untrusted semantic backend
  -> RuntimePolicyProposalV1
  -> deterministic proposal admission
  -> SHADOW-only would-edit result
  -> exact Original actor request
```

The initial backend is `GPT56SentinelPolicy`. Its production-shaped request
uses the OpenAI Responses API with the exact requested model ID
`gpt-5.6-sol`, `reasoning.effort=medium`, text plus the current screenshot,
and strict schema-constrained output. The backend is replaceable: a
deterministic rule system, independent VLM/LLM, or future trained verifier can
implement the same packet/proposal boundary without changing the R2.1 seam.

This contract authorizes only CPU/offline tests with an injected fake
Responses transport. It does **not** authorize creation or use of a live
provider client, external network, model inference, local or project GPU,
model loading/serving, MobileWorld backend/container/emulator, GUI/tool/action,
replay, or treatment execution. Real runtime evidence plumbing and any live
Responses call belong to R2.4 and require separate owner resource/run
authorization and a new receipt authority profile.

R2.2 is `SHADOW_ONLY`. A GPT-backed policy configured for `OFF` is not
evaluated. Configuring it for `ACTIVE` MUST be rejected before transport and
MUST preserve exact Original. The R2.1 deterministic fake-policy ACTIVE tests
remain valid but do not authorize this automatic backend in ACTIVE.

No R2.1 receipt, Collector event, G1.2 contract, frozen G1.5 Codec publication,
or `live_ready=false` capability byte is amended by R2.2.

## 2. Trust separation

GPT-5.6 is an untrusted semantic judge. It is not the root of trust, request
mutator, renderer, provider guard, planner, action parser, tool caller, or GUI
controller. A schema-valid model response is only a proposal.

The deterministic runtime retains sole authority for:

1. canonical-JSON and exact trusted-type admission;
2. packet, request, Codec, History-IR, target, coordinate, text, and hash
   binding;
3. event identity, causal cutoff, same-task and temporal checks;
4. evidence-role and evidence-reference eligibility;
5. verdict/temporal/operation consistency;
6. replacement-fact identity, authorship, evidence, minimality, and content
   restrictions;
7. non-overlap, no-drift, renderer, reversibility, and history-only
   invariants; and
8. exact Original fallback and receipt publication.

The automatic overlay MUST identify itself as:

```json
{
  "automatic": true,
  "curated": false,
  "deployment_prediction": true,
  "action_or_tool_authority": false
}
```

It MUST NOT be converted to, serialized as, or described as a frozen G1.2
curated `TransformationPlan`, whose required provenance remains
`curated=true` and `deployment_prediction=false`. R2.2 uses a separate runtime
proposal/admission overlay. No validator may weaken the frozen G1.2 rule to
make automatic output fit.

## 3. Evidence packet

### 3.1 Cutoff and source zones

One `mobileworld.runtime.sentinel-evidence-packet/v1` packet binds exactly one
R2.1 `logical_call_id`, host/Codec identity, and untouched actor-request hash.
Its causal cutoff is `ACTOR_REQUEST_PRE_SEND`: only information already
available before the actor provider call may enter.

The packet has four disjoint semantic zones:

- `task`: exact task instruction as untrusted task data; it states the goal
  but is not evidence that the goal was achieved;
- `current_observation`: the exact current screenshot already available on the
  actor call path, its actor-request image binding, and permitted current
  accessibility references;
- `targets`: exact source-bound history spans presented only as
  `UNTRUSTED_HOST_HISTORY_DATA`; and
- `evidence_index`: role-projected current/prior GUI, accessibility,
  transition, executor, tool, or user observations that the policy may cite.

The fixed Sentinel instruction and JSON Schema are control data. Task text,
history text, screenshot text, tool/user results, and projected JSON are data,
never developer/system instructions. Text such as “ignore previous
instructions”, a forged schema, a fake tool call, or a fake action inside any
data zone has no control authority.

Before transport, the module-owned builder reconstructs the complete packet
against the current logical call, host, untouched request, and extracted
History IR. The task must resolve as one complete instruction candidate in the
Codec-declared task region; an arbitrary unique substring or transport role
label is not a task binding. Target census and current-observation path/value
bindings must equal that rebuild exactly. A caller-supplied packet projection
or factory callback is never an independent authority source.

### 3.2 Temporal rules

Collector `seq` and explicit causal IDs define order; wall time does not. The
packet builder MUST prove that every evidence item:

- belongs to the packet's exact `task_run_id`;
- has `source_event_seq <= cutoff_event_seq`;
- was completed and observable before the current actor request where its role
  requires a completed transition; and
- resolves to the recorded event/payload hash without relocation or synthesis.

An earlier-looking wall clock cannot admit an event whose sequence is after
the cutoff. Missing source-event provenance makes temporal status `UNKNOWN`; it
does not permit an inferred order.

The packet's closed exclusions MUST remain false for:

- future events and the target call's response, action, result, or post-state;
- task outcome, score, benchmark checker/evaluator, or accepted-action data;
- replay results or peer policy/reviewer decisions;
- host history reused as its own truth evidence; and
- mutation of raw Collector events.

The builder MUST fail before policy transport if any forbidden source is
present. It MUST NOT reconstruct “independent evidence” from actor reasoning or
history text.

### 3.3 Current image

The current screenshot supplied as Responses `input_image` MUST be the same
image bound by `current_observation.screenshot_content_sha256` and the declared
actor-request image path/value hash. R2.2 MUST NOT fetch a new screenshot,
issue an environment observation call, include an older image by mistake, or
send an unbound URL. Failure to establish this binding preserves Original.

### 3.4 Targets

Every eligible target binds one stable `target_id`, record/claim identity,
source request and record hashes, exact JSON path, non-empty half-open
code-point and UTF-8 byte coordinates, exact text/hash, and source provenance.
The two coordinate systems MUST resolve to the same bytes. Targets are
canonical, unique, non-overlapping, model-visible history spans. There is no
fuzzy matching, duplicate-string search, relocation, implicit whole-record
default, or model-selected new span.

A compound statement that cannot be separated into an exact supported or
refuted span is not widened to a whole-record edit; it becomes
`KEEP_UNCERTAIN`.

### 3.5 Evidence and replacement facts

Evidence roles are closed to current screenshot/accessibility, prior action
attempt or transition fact, prior post-UI state, executor transport result,
agent-visible tool result, and user response. Each entry binds its source event
and payload hash plus a bounded text, canonical-JSON-text, or image-reference
projection. Evidence text remains untrusted data.

The role projection is a closed mapping, not a caller-selected label:

| Evidence role | Required semantic scope | Eligible Collector event type | `caused_by_event_id` | Projection |
| --- | --- | --- | --- | --- |
| `CURRENT_UI_SCREENSHOT` | `CURRENT_STATE_ONLY` | `step_started` | null | `IMAGE_REFERENCE` |
| `CURRENT_ACCESSIBILITY` | `ACCESSIBILITY_STATE_ONLY` | `step_started` | null | text or canonical-JSON text |
| `PRIOR_ACTION_ATTEMPT` | `PAST_EVENT_FACT` | `action_execution_started` | required | text or canonical-JSON text |
| `PRIOR_TRANSITION_STATUS` | `PAST_EVENT_FACT` | `transition_completed`, `transition_failed`, or `transition_not_executed` | required | text or canonical-JSON text |
| `PRIOR_POST_UI_STATE` | `PAST_EVENT_FACT` | `transition_completed` or `transition_failed` | required | text or canonical-JSON text |
| `EXECUTOR_TRANSPORT_RESULT` | `EXECUTION_TRANSPORT_ONLY` | `transition_completed` or `transition_failed` | required | text or canonical-JSON text |
| `AGENT_VISIBLE_TOOL_RESULT` | `TOOL_OR_USER_CONTENT` | `transition_completed` | required | text or canonical-JSON text |
| `USER_RESPONSE` | `TOOL_OR_USER_CONTENT` | `transition_completed` | required | text or canonical-JSON text |

The packet schema rejects every other role/scope/event/causal-parent/projection
combination. In particular, `agent_decision` is not an eligible evidence event,
current evidence cannot claim a prior execution parent, and every prior-event
role must bind one.

The v1 safe REPLACE boundary uses a finite `replacement_facts` pool. Each fact
is bound to one exact target, exact text/hash, one or more exact evidence
references, `author=SENTINEL`, and explicit negative action/tool and
retroactive-actor-speech attestations. A proposal selects only an exact
`replacement_fact_id`; free-form model text never becomes replacement bytes.
A replacement fact is declarative history data, not an imperative. Leading UI
or state-changing commands (for example open, delete, submit, or save), action
coordinates, tool/function calls, and retroactive actor voice fail before a
proposal can reference the fact.
A future backend-generated correction must first pass this same independent
fact admission and become a detached trusted fact before it can be referenced.

## 4. Structured proposal

The backend returns exactly one
`mobileworld.runtime.sentinel-policy-proposal/v1` JSON object and no prefix,
suffix, Markdown, tool call, or second object. Every nested object is closed.
It contains only packet/hash binding, proposal status, fixed automatic
provenance, and one bounded decision per target.

For each target, the vocabulary is:

```text
factual_verdict:    SUPPORTED | REFUTED | UNVERIFIABLE
temporal_validity:  ACTIVE | INVALIDATED | UNKNOWN | N_A
proposed_operation: KEEP | DROP | REPLACE | KEEP_UNCERTAIN
fallback_status:    NONE | ABSTAIN_TO_ORIGINAL
```

Each decision also includes exact evidence ID/hash references and relation,
integer `confidence_millis` in `[0, 1000]`, one closed reason code, closed
uncertainty codes, a one-line bounded conclusion summary, and a nullable exact
replacement-fact ID. The conclusion summary is audit explanation only. It is
never passed to the renderer, actor prompt, parser, tool layer, or action path,
and it MUST NOT contain a hidden reasoning trace.

Proposal status is an untrusted model declaration:

- `COMPLETE`: no target abstains;
- `PARTIAL_ABSTAIN`: at least one but not all targets abstain; and
- `ABSTAIN`: all targets abstain, or the clean-history packet has zero targets
  and `decisions=[]`.

Deterministic admission MUST require exactly one decision per packet target,
except the explicit zero-target clean-history form. Unknown, missing,
duplicate, reordered-to-a-different-target, or extra decisions reject the
complete proposal atomically. A complete proposal hash is bound before later
admission rejection; hashing records what was evaluated and grants no
authority.

The proposal schema mechanically requires the same local claim shapes as the
trusted Python contract:

- `KEEP` is `SUPPORTED` plus `ACTIVE` or `N_A`, cites at least one `SUPPORTS`
  reference, uses `DIRECT_EVIDENCE_SUPPORT`, has no uncertainty code, and uses
  fallback `NONE`;
- `DROP` and `REPLACE` have at least one reference, no uncertainty code, and
  fallback `NONE`; their basis is either `REFUTED` with a `REFUTES` reference
  and `DIRECT_EVIDENCE_REFUTATION`, or `SUPPORTED` plus `INVALIDATED` with both
  `SUPPORTS` and `INVALIDATES` references and
  `LATER_EVIDENCE_INVALIDATES`;
- `UNVERIFIABLE` or `UNKNOWN` forces `KEEP_UNCERTAIN`, at least one uncertainty
  code, and `ABSTAIN_TO_ORIGINAL`; without either uncertainty state,
  `KEEP_UNCERTAIN` is forbidden; and
- only `REPLACE` carries a non-null exact `replacement_fact_id`.

JSON Schema cannot express uniqueness by one nested ID, equality to packet
target/evidence/fact sets, evidence hash lookup, role-based evidence strength,
or event-sequence comparison. Exact constructors and deterministic admission
therefore still reject duplicate decision/target/evidence IDs and enforce all
cross-document bindings and temporal rules described in Section 5.

## 5. Deterministic semantic admission

Schema validity is necessary but insufficient. After detached exact-type
snapshotting, admission applies at least these rules:

| Evidence conclusion | Admitted operation |
| --- | --- |
| directly supported and still active (or genuinely atemporal) | `KEEP` |
| directly refuted by eligible evidence available at cutoff | `DROP`, or `REPLACE` using an independently admitted fact |
| previously supported but invalidated by later, pre-cutoff eligible evidence | `DROP`, or `REPLACE` using an independently admitted fact |
| unverifiable, temporal provenance missing/unknown, conflicting, compound, or ambiguous | `KEEP_UNCERTAIN` |

Additional mandatory rules:

- `SUPPORTED`, `REFUTED`, and `INVALIDATED` MUST cite eligible exact evidence;
- current-screen absence alone cannot refute a past event;
- HTTP 200, action-attempted, `transition_completed`, executor return, or a
  generic success field proves only the corresponding transport/executor fact
  and cannot alone prove task-semantic success;
- task instruction proves the requested goal, not completion;
- actor history/reasoning cannot corroborate itself;
- evidence after the cutoff, from another task, or from a forbidden role is
  invalid even if its text is persuasive;
- uncertainty is not converted to a destructive operation by a confidence
  threshold; and
- a replacement MUST match one exact packet fact, retain `SENTINEL`
  authorship, remain minimal, cite direct eligible evidence, and contain no GUI
  action, next-step recommendation, coordinate, tool call, role impersonation,
  accepted-action predicate, or retroactive actor speech.

An admitted runtime plan is rebuilt by Sentinel from the detached packet and
proposal snapshots. Neither a backend serializer nor backend-owned object is
used for hashing, census, admission, or rendering. Decision rationale and any
unrecognized model text are never renderer inputs.

The renderer and invariant validator still own the final history-only gate.
System policy, task, tools, current screenshot/accessibility blocks, message
roles/order, multimodal order, model/sampling settings, provider envelope, and
every non-history byte MUST remain unchanged. Partial admission or partial
rendering is forbidden.

## 6. GPT56SentinelPolicy transport profile

The v1 backend constructs one production-shaped `responses.create` request
through an injected `SentinelResponsesTransport`. It does not instantiate a
default client, read an environment API key, or obtain network authority.

The request profile is pinned or content-hash-bound as follows:

- requested model: `gpt-5.6-sol`;
- reasoning effort: `medium`;
- fixed Sentinel instruction prompt and its SHA-256;
- one user-data input containing the canonical evidence packet plus exactly
  the bound current screenshot;
- strict `json_schema` text format using the checked-in proposal schema and
  exact schema SHA-256;
- low output verbosity, bounded output tokens, no temperature override;
- `tools=[]`, tool choice disabled, parallel tool calls disabled;
- `store=false`, `stream=false`, and truncation disabled; and
- OpenAI SDK retries disabled (`max_retries=0`) with one explicit bounded
  timeout below the R2.1 policy budget.

The first implementation targets the repository-pinned OpenAI Python SDK
`1.106.1` request surface and records the actual SDK version. A version change
requires offline compatibility tests and a new request-config hash; it cannot
silently change the schema, retry, image, reasoning, or storage behavior.

The response adapter extracts only bounded metadata, one structured-output
text value, and usage. It MUST NOT dump a provider SDK object or invoke an
overridable response serializer. Refusal, incomplete status, empty/multiple
output, invalid JSON, extra properties, wrong model metadata, or any schema
failure produces typed Original fallback.

The current R2.2 acceptance transport is fake. Its receipt records
`transport_kind=FAKE`, `transport_authority=CPU_OFFLINE_FAKE`, and false for
external-network, model-call, local-GPU, and MobileWorld-action attempts. A
production transport adapter may be implemented as an inert injectable
boundary, but it MUST NOT be invoked under this contract.

The receipt schema reserves exactly one second, closed pair for later use:
`OPENAI_RESPONSES/EXPLICIT_OWNER_AUTHORIZATION`. If such a transport records
one call, network and model-attempt attestations are both true. Schema support
is not run authority: that pair is invalid for this checkpoint and may be used
only in R2.4 after a new explicit owner authorization.

## 7. One evaluation and recursion prevention

One logical actor decision may perform at most one policy transport call.
Internal SDK retry is zero. The result is bound to the R2.1
`logical_call_id` and the R2.1 logical-call cache reuses it across BaseAgent
provider retries, adapter parse retries including Qwen's outer loop, and
streaming attempts. Concurrency over the same logical call MUST converge on
one evaluation rather than issue duplicate calls.

For this CPU/offline checkpoint, entry into the seam-owned
`run_transport(call)` callback is the transport-start linearization point.
Cancellation prevents a callback that has not crossed that gate from starting.
A callback already linearized as in flight is not asynchronously killed by
Python; the authorized fake transport is bounded and has no network, model, or
external side effect, and a timeout still suppresses every late R2.2 receipt and
policy output. This narrow rule is not a live-transport design. R2.4 MUST add a
cancellable transport/attempt boundary and complete attempt receipt before the
reserved OpenAI transport profile may be invoked.

Every Sentinel-owned call is marked `call_role=sentinel` at any shared runtime
boundary. R2.1 bypasses such a call before Codec or policy work, preventing
recursion. A Sentinel call has no actor tools and cannot execute or recommend a
GUI action.

## 8. SHADOW result and fallback

R2.2 may construct and validate an in-memory would-edit candidate for metrics
and audit. It MUST always return the exact Original actor-bound request:

```text
effective actor mode = SHADOW
edit_applied = false
final actor request hash = raw actor request hash
```

The proposal, runtime plan, and candidate never write into the caller request.
Any timeout, exception, invalid packet/schema/model output, target/evidence
drift, ambiguity, overlap, forbidden evidence, replacement failure, renderer
failure, invariant failure, or R2.2 receipt failure discards the complete
candidate and preserves Original with a typed, safe-grammar failure code. Raw
exception or provider text is not a failure code.

There is no automatic policy retry, alternative model, fuzzy span recovery,
partial decision use, silent operation downgrade, or fallback to a different
Codec. `KEEP_UNCERTAIN` is an explicit non-editing disposition, not a hidden
successful treatment.

The accepted R2.1 v1 receipt requires at least one recorded decision for a
`PASSED` evaluation and is not modified by this story. A zero-target
clean-history packet is valid at the standalone R2.2 policy layer (`ABSTAIN`,
`decisions=[]`, zero material operations, and an admitted R2.2 receipt), but
the current R2.1 bridge cannot represent that decisionless success. It returns
exact Original with typed `INVARIANT_FAILURE` and check
`r2_2_zero_target_r21_v1_unrepresentable`. R2.4 must either add a versioned
bridge/receipt representation or skip the semantic call through an explicitly
versioned no-editable-history bypass. It MUST NOT synthesize a nonexistent
target decision.

## 9. Receipts and metrics

R2.1 continues to publish its unchanged
`mobileworld.runtime.sentinel-receipt/v1` hash-only actor-seam receipt. R2.2
adds a separate
`mobileworld.runtime.sentinel-policy-receipt/v1` record for policy provenance.
The two schemas and sinks are not interchangeable.

The R2.2 receipt binds:

- logical call, host, packet, and policy identities;
- `SHADOW_ONLY`, exact requested and returned model metadata, API method,
  actual SDK version, reasoning effort, retry and fake-authority profile;
- prompt, output schema, request configuration, evidence packet, current
  image, response envelope, provider output, and canonical proposal hashes;
- the detached admitted runtime-plan hash when admission succeeds;
- response ID/status/service tier and bounded token usage when returned;
- evaluation status, typed failure, deterministic validation checks, and at
  most one transport call;
- packet-build, transport, parse, admission, and total latency;
- target/decision, `KEEP`/`DROP`/`REPLACE`/`KEEP_UNCERTAIN`, material-edit,
  and abstention census; and
- false network/model/GPU/action and persistence attestations for the CPU fake
  checkpoint.

Its stage matrix is closed as follows:

| Evaluation status | Packet/image binding | Transport | Response envelope | Parsed proposal | Admitted plan | Decision census |
| --- | --- | --- | --- | --- | --- | --- |
| `ADMITTED` | present | exactly 1 | complete, `status=completed`, returned model pinned | present | present | admitted census |
| `EVIDENCE_REJECTED` | absent | 0 | absent | absent | absent | all zero; target count zero |
| `TRANSPORT_ERROR` | present | exactly 1 | absent | absent | absent | all zero |
| `INVALID_RESPONSE` | present | exactly 1 | complete trusted envelope | absent | absent | all zero |
| `ADMISSION_REJECTED` | present | exactly 1 | complete trusted envelope | present | absent | all zero |
| `INTERNAL_ERROR` | stage-dependent but internally bound | 0 or 1 | present only as a complete trusted envelope | present only after a complete envelope | absent | all zero |

Only `ADMITTED` has a null failure code; every failure has a typed non-null
code. Packet ID, packet hash, and current-image hash are jointly absent or
present. A response envelope, when present, binds the pinned returned model,
response ID, completed status, output hash, and one transport call. Parsed
proposal provenance requires such an envelope, and admitted-plan provenance
requires a parsed proposal. Zero transport calls force all response, proposal,
plan, usage, and transport-attempt fields absent/false and transport latency to
zero; one transport call requires a completed packet/image binding.

Draft 2020-12 cannot state the receipt's integer arithmetic. The trusted
receipt constructor additionally requires total tokens to equal input plus
output tokens, phase latencies not to exceed total latency, decision count to
equal the four-operation census and, for `ADMITTED`, equal target count,
material count to equal `DROP + REPLACE`, and abstain count to equal
`KEEP_UNCERTAIN`.

The receipt is hash-only. It embeds no task/history/evidence text, screenshot,
request view, model output, proposal payload, exact diff, credential, provider
header, hidden reasoning item, reasoning summary, or chain of thought. The
fixed prompt and schema are recovered by their checked-in bytes plus hashes;
the output is bound by hash and bounded census. Any future restricted detail
channel must be separately versioned, access-controlled, repository-external,
credential-excluded, and explicitly authorized.

Receipt-transaction admission occurs before fake policy transport. Commit
failure prevents use of the proposal and preserves Original. R2.2 data is
derived sidecar data and MUST NOT be written into raw Collector events.
Receipt preparation performs all validation, serialization, and potentially
slow work without publication. Only an exact module-owned prepared publication
may cross the seam-owned `publish_receipt` gate, whose final in-memory append is
constant-time. Deadline cancellation therefore cannot be delayed by receipt
preparation and no receipt may appear after the actor receives its fallback.
This CPU checkpoint trusts module-private in-process sink state. Resistance to
arbitrary same-process code rewriting private locks or containers would require
process isolation and is not claimed by R2.2.

Metrics expose low-cardinality counts with explicit denominators for:

- eligible, packetized, returned, and admitted targets (claim coverage);
- supported/refuted/unverifiable and active/invalidated/unknown/`N_A`;
- all four operations, would-edit, abstain, fallback, and error;
- invalid evidence reference, temporal, span, and forbidden-channel failures;
- one-evaluation/retry-reuse and any duplicate-evaluation violation; and
- packet, transport, parse, admission, and total latency.

The hash-bound receipt is the authoritative per-call record. The in-memory
metric accumulator is deliberately non-blocking and best-effort: contention or
an internally unavailable accumulator may drop that metric update, but it may
not delay the actor, invalidate an already committed receipt, change the
validated policy output, or force Original after publication. No fallible or
blocking semantic work occurs after the receipt commit.

Logical-call IDs, evidence IDs, target IDs, raw reasons, prompt text, and other
high-cardinality or sensitive values MUST NOT be metric labels. R2.2 exports no
task-success improvement or causal-effect metric.

## 10. CPU/offline acceptance

Acceptance requires deterministic, fake-transport tests for:

1. directly refuted, stale/invalidated, supported, unverifiable, and
   zero-target clean-history packets;
2. exact one-to-one target decisions; unknown/missing/duplicate target,
   overlap, UTF-8 drift, hash drift, missing evidence, and ambiguity rejection;
3. forbidden future/outcome/checker/replay/peer/cross-task evidence;
4. current-screen-absence-only and executor-success-only non-authorization;
5. `KEEP_UNCERTAIN` preservation for unsupported or uncertain claims;
6. replacement ID/text/hash/evidence/authorship/minimality and action/tool/
   actor-voice rejection;
7. task/history/tool/screenshot prompt-injection strings remaining data;
   task-substring and current-image factory substitution rejection before the
   fake transport;
8. refusal, incomplete response, invalid JSON/schema, extra action/next-step/
   tool fields, response metadata mismatch, timeout, exception, and receipt
   failure;
9. exactly one fake Responses call per logical decision, zero SDK retry,
   sentinel-call bypass, and reuse across transport/parse/streaming retries;
10. exact SHADOW Original parity for every keep/edit/abstain/failure result;
11. policy receipt/schema parity, Draft 2020-12 meta-validation, bounded
    best-effort non-blocking metrics, binder/output equality, caller
    immutability, and full affected CPU regressions; and
12. zero live provider/model/network/GPU/backend/replay/GUI/tool/action use.

Passing these tests establishes only a CPU/offline, fake-transport,
provenance-grounded SHADOW policy implementation. It does not prove semantic
accuracy, production latency/cost, Qwen/MAI live readiness, task-success
improvement, history-edit causal effect, or safe ACTIVE deployment.

## 11. R2.4 handoff

R2.4 must separately provide and authorize:

- real Qwen and MAI host plumbing for exact task, current screenshot,
  accessibility, completed transition/tool/user evidence, and source-bound
  runtime targets;
- a live Responses transport/client with external secret handling, explicit
  retry/timeout/Collector correlation, and a new authority/receipt profile;
- returned model/config and complete live-attempt evidence;
- production runtime Codec capability overlays without changing the frozen
  G1.5 v1 `live_ready=false` bytes; and
- a versioned representation or bypass for the zero-target clean-history
  bridge limitation without rewriting the accepted R2.1 v1 receipt; and
- separately authorized OFF/SHADOW/ACTIVE vertical-slice resource tests.

Until those gates exist, missing live evidence is an availability fact, not
permission to infer evidence from history, invoke a provider, or mark R2.2 as a
live Sentinel result.
