# G1 Exact-Request Replay Runner Contract v1

Status: CPU implementation contract for ALE-322 / G1.4
Contract ID: `mobileworld.g1.exact-request-replay/contract-v1`
Runner ID: `mobileworld.g1.exact-request-replay-runner/v1`
Protocol: `mobileworld.g1.causal-replay/protocol-v1`
Date: 2026-08-27 UTC

## 1. Decision and current authorization

ALE-322 builds the reusable state-frozen next-action replay harness. The current owner
authorization is deliberately narrower than the story's final live acceptance: CPU-only code,
schemas, deterministic scheduling, invariance proofs, append-only storage, blinded exports, and
deterministic fake-provider conformance may be completed now. Real model/provider/network/GPU
invocation, GUI or generated-action execution, backend restore, live replay, and treatment response
generation remain prohibited pending a later explicit resource review.

The phrase “about 90%” is scheduling language, not an execution-readiness claim. Any CPU readiness
manifest emitted for this tranche MUST keep all of these false:

- `live_transport_validation_complete`;
- `live_history_codec_ready`;
- `curated_transformations_ready`;
- `run_ready_seal_present`;
- `provider_invocation_allowed`;
- `treatment_response_generation_allowed`;
- `formal_replay_ready`.

ALE-322 remains `IN_PROGRESS_LIVE_PROOF_DEFERRED` after the CPU deliverable. It MUST NOT unblock a
formal G1 run by itself.

## 2. Frozen inputs and additive boundary

The runner consumes but MUST NOT modify:

1. G1.1 protocol, arm catalog, schedule semantics, registry, locked analysis plan, run schema, and
   outcome schema;
2. the accepted G1.2 core, History IR, codec registries, rendering logic, pre-send validator,
   provider authorization guard, Provider Result, and sidecar semantics;
3. the active G1.3 v1.1 formal ReplayCapsule publication and its deny-by-default field visibility.

The active G1.3 publication is identified by manifest
`8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402` and capsule set
`7d0e85c523c2b20b3f0b820c2e846cbb84957d4ae78e46d7090c6ce78ae9fbed`. It contains 190
capsules and zero exclusions. A runner loader MUST require active v1.1 plus a source-bound,
byte-identical directory-validation receipt.

Every formal capsule intentionally carries:

- `execution_ready=false`;
- `provider_invocation_allowed=false`;
- `treatment_response_generation_allowed=false`;
- `provider_invoked=false`.

The last field is build telemetry, not permission. The runner MUST NOT flip, reinterpret, copy with
a different value, or otherwise bypass any of these fields. A later G1.7 run-ready seal is a
separate additive authority; it is not materialized in G1.4.

All G1.4 artifacts are derived and repo-external. They are never appended to Collector v1 raw event
streams and never change capsule bytes.

## 3. Four operational layers

### 3.1 Capsule loader

The loader MUST:

- accept only a content-addressed active v1.1 formal publication;
- require a `SOURCE_BOUND` receipt with formal, structural, exact-file-set, read-only, and
  source-rebuild-byte-identical checks all exactly true;
- resolve one manifest unit exactly once and verify its capsule file and inner body hashes;
- rehydrate the authoritative canonical semantic-request artifact, never the inspectable
  `request_view`;
- bind the exact decoding configuration and parser descriptor artifacts;
- require the three authorization/readiness fields and all no-execution telemetry fields to be
  exact Boolean false;
- expose only the allowlisted pre-cutoff runtime projection to the harness.

The returned runner object MUST omit source provenance, curator-only channels, natural response,
post-action audit, outcome, checker, and adjudicator data. Parsing a full capsule is a validator
operation; making those roots reachable from renderer/provider/scorer code is forbidden.

The current 190 capsules use `SERIALIZED_REQUEST_ONLY`. `EXACT_CHECKPOINT` and
`DETERMINISTIC_PREFIX_REPLAY` MUST block until a separately sealed restorer proves exact state
equivalence. G1.4 does not run either restore path.

### 3.2 History rendering and invariance

The runner delegates history semantics to the registry-resolved G1.2 History Codec and core. It
MUST validate the complete paired plan set before encoding any arm in a block. Scientific execution
uses `G1_SCIENTIFIC + BLOCK`; fail-open is not a scientific fallback.

For every arm, the following order is mandatory:

1. bind capsule semantic request, History IR, curated plan, complete plan set, codec identity, and
   plan-set profile;
2. render through the G1.2 core;
3. independently recompute the G1.2 pre-send receipt;
4. prove reversible source mapping;
5. prove all capsule-owned frozen regions, roles, ordering, tool protocol, task, system policy,
   current observation, every image data URL, model settings, sampling settings, and unknown SDK
   arguments are unchanged;
6. recompute and match the capsule non-history projection;
7. only after the entire block passes, encode provider SDK arguments and bind their exact bytes.

If any render, plan, capsule, or invariance check fails, no arm in that block may be encoded or
sent. The pure encoder and G1.2 final authorization guard run only after every arm passes those
checks; if either then fails, no arm may be sent. A future batch preflight MUST apply the same rule
across the whole paired unit before the first real invocation.

Original is exact canonical semantic identity before the replay seed. The seed is not a history
edit. The runner adds the same preregistered seed to every arm in a pair block only after rendering,
and records separate hashes for captured request, rendered request, final SDK arguments, encoded
bytes, and model/transport parameters.

### 3.3 Provider boundary

The provider codec boundary is model-checkpoint agnostic. It receives the final semantic request,
the captured non-message SDK arguments, the preregistered replay seed, and explicit transport
policy. It MUST:

- preserve every captured model, sampling, tool, image, message, and unknown SDK argument;
- permit only the registered replay seed as an added application-layer delta;
- set SDK hidden retries to zero;
- leave visible retries to the harness;
- bind provider codec version, endpoint revision, served model/config, parser binding, exact encoded
  bytes, and parameter hash;
- retain response bytes, ordered chunks, transport errors, latency, and tokens when available;
- return the host parser's structured action without rewriting it;
- never import or call an action executor and never feed a response into a later request.

The OpenAI-compatible codec in this CPU deliverable implements encode and normalize. Its `send`
method is mechanically fail-only with `LIVE_TRANSPORT_DEFERRED`. It does not accept a Boolean flag,
environment variable, or caller assertion as an override. The later live proof must add a sealed
authority and validate it before transport.

The deterministic fake provider is the only executable provider in this contract. Its execution
domain is `FAKE_CONFORMANCE`, endpoint is `fake://network-forbidden/v1`, and every exchange records
`simulated=true`, `external_provider_invoked=false`, and `gpu_used=false`. A fake response is test
data, not a treatment response and is never scientifically count-eligible.

### 3.4 Attempt state machine and derived store

The append-only event order is:

```text
PLANNED
├─→ PREFLIGHT_BLOCKED  [terminal, zero provider attempts]
└─→ PREFLIGHT_ALLOWED
    → ATTEMPT_STARTED
→ CHUNK*
→ RETURNED | FAILED
→ PARSED | PARSE_FAILED
→ TERMINAL
```

Identity admission runs before an `InvocationPlan` exists. A malformed capsule, schedule, registry,
or plan set rejected at that earlier stage returns a stable fail-closed error and creates no logical
attempt, because inventing a run ID from invalid input would be misleading. Once the plan set and
all deterministic bindings establish byte-final `InvocationPlan` identities, the runner creates
those identities before render/encode validation. With a derived store supplied, any later block
failure uses `record_preflight_blocked` to materialize the exact
`PLANNED → PREFLIGHT_BLOCKED` branch for every planned arm; it is idempotent only for the same plan
bytes and reason code. Neither a rejected admission nor a blocked preflight reaches a sender or
normalizer.

Events carry stable sequence numbers and a hash chain. Logical `run_id` hashes protocol, unit,
capsule, plan set, selected plan, arm, seed, repeat, schedule, history/provider/parser/config/model,
and code identities. It excludes clocks, absolute paths, PIDs, ports, and host metadata.

Artifacts use content addresses and write-once creation. A same-ID rerun with an existing terminal
record returns the existing result without another send only after rehydrating and cross-binding the
exact invocation plan, selected and paired plans, invariance/render/validation receipts, final SDK
arguments, target diff, encoded request, blinding commitment, every chunk/exchange/response, parser
event, and terminal event. A missing, substituted, or internally inconsistent required artifact is
an error; it is never repaired or overwritten. If `ATTEMPT_STARTED` exists without a committed
terminal record, delivery or subsequent handling is ambiguous; this CPU runner does not reconstruct
in-flight responses, so automatic resend MUST fail closed. A later intentional recovery needs a new
explicitly linked attempt and cannot silently substitute for the original.

The artifact store's terminal-envelope reader and writer are package-private structural primitives;
they do not constitute a completion proof and are not public runner APIs. The runner MUST apply the
same full prepared-arm closure described above before writing a terminal envelope and again before
returning, reusing, or exporting one. A caller cannot opt out of that proof with a Boolean receipt,
callback, or structural-only validation mode.

## 4. Locked schedule and repeatability

The schedule is deterministic position balancing, not runtime randomness. For UTF-8 bytes
`salt|model_id|unit_id` with salt `mobileworld-g1-arm-order-v1-20260826`:

- initial rotation is `sha256(input).digest[0] % arm_count`;
- direction is `+1` when `digest[1] % 2 == 0`, otherwise `-1`;
- for zero-based block `b` and position `j`, the arm is
  `base_arms[(j + initial_rotation + direction*b) % arm_count]`;
- blocks are seeds `[1729, 2718, 31415]`, each with repeat 1 then repeat 2.

Strict units use all five arms and produce 30 logical calls. Clean controls use Original and Sham
and produce 12. Each strict arm's position-count range is at most one; clean first position is 3/3.
The seed, final encoded bytes, decoding configuration, and timeout remain identical across visible
retries of one logical run.

## 5. Retry and parser rules

At most three provider attempts are visible. Only `TIMEOUT`, `HTTP_5XX`, and
`CONNECTION_ERROR` are retryable. Parser failure, malformed response, explicit refusal, empty
response, and no-op are terminal and MUST NOT be retried. A returned valid response is never
discarded or retried to obtain a preferred action.

The parser is invoked once against the target-pre dimensions and exact response. Qwen/MAI parser
equivalence remains a live-code validation dependency; the G1.3 parser artifact is a descriptor,
not executable code. Host-specific parser adapters must be thin, registry-bound, and covered by
golden equivalence tests before live use. The only executable parser in the CPU fake domain is the
exact sealed `JsonActionParser`; custom adapters are normalize-only conformance inputs and MUST be
rejected by preflight execution until a later versioned live-parser declaration exists.

## 6. Blinded scoring boundary

The scorer packet contains only an opaque precommitted packet ID, the normalized action, a generic
parser outcome, and allowlisted non-identifying diagnostics. It MUST exclude requested/effective arm,
plan, target, correction, history, request/diff hashes, capsule/run/schedule identity, arm position,
seed, provider/endpoint identity, filenames, and reversible ordering clues.

The mapping from opaque packet ID to run/arm/schedule is a separate confidential artifact. It is
sealed before a response exists and is not supplied to a scorer. Presentation order is derived
only from opaque packet IDs and a separate scorer nonce, never from preregistered arm order. G1.4
does not perform gold scoring; G1.6 remains responsible for independently adjudicated acceptable
actions.

The seal commits an exact deny set derived from capsule/public binding, the complete replay-binding
model/provider/parser projection, invocation identity, and high-entropy plan, target, correction,
history, receipt, and diff values. Any exact or embedded match in scorer-visible action values fails
closed; values are never rewritten. The deny-set digest is stored only in the confidential mapping.

CPU fake execution precommits that mapping under the run's confidential derived-data namespace
before `ATTEMPT_STARTED`; the public `PLANNED` event carries only its digest and key commitment.
The deterministic fake-only key is conformance material, not a formal scorer secret. A later formal
pack MUST replace it with a separately held sealed key without changing the scorer projection.

The public API builds a blinded packet only from a fully closed terminal: it revalidates the entire
attempt artifact closure, derives the exact persisted seal, and cross-binds the terminal event,
normalized-action hash, diagnostics hash, packet hash, and mapping hash in a confidential
post-response receipt. The scorer packet is an immutable canonical-byte snapshot; presentation
ordering rechecks it against the union of every packet's sealed deny set. The confidential receipt
binds the unprojected terminal diagnostics hash separately from the allowlisted public diagnostics
hash carried by the scorer packet. Pure packet construction is internal conformance machinery and
is not a formal export path.

## 7. Versioned G1.4 schemas

The additive schemas under `mobileworld_audit_handoff/schemas/g1_4/` are:

- `invocation_plan.schema.json`;
- `invariance_report.schema.json`;
- `provider_exchange.schema.json`;
- `attempt_event.schema.json`;
- `terminal_attempt.schema.json`;
- `blinded_action.schema.json`;
- `blinding_mapping.schema.json`;
- `blinded_packet_binding.schema.json`;
- `cpu_manifest.schema.json`.

They do not replace the frozen G1.1 run/outcome schemas. Formal G1.1 run plans still require the
future G1.6/G1.7 plan, gold, codec, parser, scorer, schedule, admission, and run-ready hashes. G1.4
attempt and transport records are additive inputs to a later outcome assembly.

## 8. CPU conformance requirements

The fake suite MUST cover:

- success and exact unchanged parsed action;
- malformed response, refusal, empty response, no-op, and parser failure;
- timeout, HTTP 5xx, connection error, visible retry then success, and retry exhaustion;
- streaming chunks, partial stream failure, and ordering;
- unsupported capability and scientific fail-closed behavior;
- explicit future-runtime Original fallback with `count_as_treatment=false`;
- invariance failure before encoder/sender/normalizer;
- Original identity and target-only diff;
- exact schedule vectors and balance;
- terminal idempotence, collision rejection, ambiguous-delivery block, and no overwrite;
- recursive blinded-packet leakage rejection;
- schema meta-validation and runtime/schema parity;
- no socket/OpenAI client/model/GPU/GUI/action path;
- raw, G1.1, G1.2, and G1.3 byte immutability.

## 9. Deferred live completion

The remaining live proof is intentionally outside the current CPU authorization. ALE-322 can be
fully completed only after all of the following exist and the owner separately authorizes the
resource use:

1. registry-resolved live Qwen and MAI History Codecs from G1.5;
2. curated complete paired Transformation Plans and parser/gold material from G1.6;
3. G1.7 reproof of backend dependency, seed support, serving image/config, isolation, scorer, and a
   run-ready seal;
4. an approved model/provider/GPU endpoint;
5. live evidence that exact model/settings/tools/images are sent and that the host parser action is
   unchanged.

Until then, a real send is a contract violation even if endpoint credentials or GPU resources are
available.
