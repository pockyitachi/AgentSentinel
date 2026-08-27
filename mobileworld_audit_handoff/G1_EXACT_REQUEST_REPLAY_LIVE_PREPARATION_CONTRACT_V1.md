# G1 Exact-Request Replay Live Preparation Contract v1

Status: **AUTHORIZED inert/code-only preparation for ALE-322 / G1.4**
Document type: Additive no-execution preparation contract
Contract ID: `mobileworld.g1.exact-request-replay-live-preparation/contract-v1`
Depends on: `mobileworld.g1.exact-request-replay/contract-v1`
Authorization: `G1_4_DECISION_LOG.md` D-026
Date: 2026-08-27 UTC

## 1. Decision and authority

The owner authorized preparation of the remaining G1.4 code needed for a future GPU/live proof
while explicitly directing that no GPU or live path be started until the owner gives a later
instruction. This contract therefore authorizes only deterministic, CPU-only, inert preparation.
It does not authorize a live proof, a provider invocation, treatment-response generation, or use
of any runtime resource.

The following preparation components are in scope:

1. static binding of the already frozen Qwen and MAI model, endpoint, SDK, decoding, parser, and
   serving-configuration declarations;
2. pure rendering of an SDK call plan as canonical data without creating or calling a client;
3. pure rendering of a model-service launch plan as canonical data without starting a process;
4. pure projection of a caller-injected OpenAI-compatible response envelope without invoking a
   provider or host parser;
5. deterministic assessment of a caller-injected resource snapshot without probing the host;
6. CPU-only schemas, fixtures, tests, and no-execution inspection output needed to validate those
   data transformations.

The outputs are preparation evidence only. They are not an invocation plan, a run-ready seal, a
serving receipt, a treatment response, or scientific replay evidence.

## 2. Additive version boundary

This contract is additive to, and does not silently redefine,
`mobileworld.g1.exact-request-replay/contract-v1` or
`mobileworld.g1.exact-request-replay-runner/v1`. The existing G1.4 v1 schemas and CPU/fake
checkpoint remain historically identifiable with their current semantics.

In particular:

- `OpenAICompatibleProviderCodec.send` remains mechanically fail-only with
  `LIVE_TRANSPORT_DEFERRED`;
- `execute_live_arm` remains mechanically fail-only with `LIVE_EXECUTION_DEFERRED`;
- the current CLI remains no-send and cannot gain a flag, environment-variable switch, or caller
  assertion that enables transport;
- `FAKE_CONFORMANCE` remains the only executable provider domain under the existing runner v1.

Prepared code may define pure records, validators, and renderers, but it MUST NOT connect those
records to either hard-disabled method. A future connection requires a new owner authorization,
the applicable downstream seals, and an explicit versioned live-execution contract or amendment.

## 3. Static model-configuration binding

The static binder MAY read the repository's frozen
`g1/model_config_manifest.v1.json` and derive a closed, deterministic projection for exactly
`qwen3vl_8b` and `mai_ui_8b`. It MUST bind, without reinterpretation:

- model ID, repository, revision, served name, and checkpoint artifact hashes;
- endpoint origin/path, SDK method/version, timeout, stream mode, and hidden-retry policy;
- every captured or formal model/sampling argument and the preregistered replay-seed rule;
- parser implementation paths, symbols, and hashes;
- the complete declared vLLM serving configuration and serving-environment package pins;
- the manifest's unresolved seed-support, serving-image, backend/isolation, and run-readiness
  states.

Binding is a data operation only. It MUST NOT inspect, import, mmap, deserialize, tokenize with,
or load a checkpoint; import CUDA, vLLM, torch, a provider SDK, or a production actor; resolve a
live endpoint; read a credential; or claim that any manifest-declared file or service was checked
at runtime. Missing, duplicate, unsupported, or internally inconsistent declarations fail closed
with stable errors and no inferred substitute.

## 4. Inert SDK call-plan renderer

The SDK call-plan renderer produces canonical JSON data describing what a later separately
authorized transport would have to call. It MUST:

- preserve the exact application-layer request and every message, role, tool, image block,
  unknown SDK argument, model setting, and sampling setting;
- permit only the frozen replay seed as an application-argument delta and require the same seed
  across a paired block;
- require SDK hidden retries to remain zero and leave visible retries to the harness;
- bind the SDK method/version, endpoint revision, timeout, stream policy, encoded-request bytes,
  and all relevant hashes;
- contain no credential value, Authorization header, cookie, client object, socket, callback,
  response, or executable transport handle.

Rendering a plan MUST NOT instantiate or invoke a provider client or client factory. It MUST NOT
perform DNS resolution, open a socket, send HTTP, inspect endpoint health, run a seed canary, or
produce a provider response. A rendered call plan records `provider_invocation_allowed=false` and
is not accepted by the existing `send` interface.

A standalone call descriptor proves only that the registered seed was added to its supplied base
application request. It records `source_invariance_validated=false`. A paired-block descriptor
MUST apply one registered seed to every member and bind the complete call set, but it records
`formal_plan_pairing_validated=false` until the governed G1.5/G1.6 inputs exist. Neither record is
admissible as a formal invocation plan.

The response projector accepts only caller-injected data. It preserves the exact SDK content
string and separately derives the exact `str.strip()` input used by the pinned MobileWorld host
parser. It MUST bind the original envelope and projection hashes, MUST NOT invoke the host parser,
and MUST NOT claim that any provider response was observed. Its output is fixture/data-normalization
evidence only and cannot be fed into the existing runner as a live result.

## 5. Inert launch-plan renderer

The launch-plan renderer produces a closed data record for later human and G1.7 review. It MAY
render an ordered argument vector and environment-name allowlist from the frozen model manifest.
It MUST NOT:

- start or signal a subprocess, shell, tmux session, container, service, or scheduler job;
- import or call vLLM, torch, CUDA, NVML, Docker, or a model-serving API;
- reserve a port, connect to an endpoint, acquire a lock, change an environment variable, or read
  a credential;
- read, hash, repair, download, copy, or load model weights;
- claim that the rendered launch is compatible, healthy, isolated, reproducible, or sealed.

The rendered plan is not a serving-image receipt. Its launch and environment bindings remain
unverified until the separately governed G1.7 preflight.

The frozen manifest's `generation_config_mode="model"` is a semantic manifest value, not a
literal vLLM argument. For the pinned vLLM 0.11.0 CLI, the inert renderer MUST record and apply the
closed mapping `model -> auto`, where `auto` means loading the served model's generation
configuration. It MUST NOT render the literal value `model`, which vLLM would interpret as a
configuration path. This mapping is preparation data only and remains unexecuted.

## 6. Injected resource-snapshot assessment

A resource assessor MAY evaluate a caller-supplied, schema-valid snapshot as pure input data. It
MAY compute whether the supplied values meet declared memory, isolation, port, and concurrency
constraints and return deterministic reasons. It MUST NOT collect the snapshot itself.

The assessor MUST NOT call `nvidia-smi`, NVML, CUDA, `/proc`, `ps`, `ss`, Docker, a scheduler, a
cloud API, or any other host/resource discovery path. It MUST NOT allocate memory, select or claim
a GPU, contact another user's process, stop a service, or convert an assessment into permission.
An injected snapshot is advisory evidence only; `resource_snapshot_injected=true` does not imply
`gpu_used`, `gpu_reserved`, or `provider_invocation_allowed`.

## 7. Versioned preparation records

The six additive Draft 2020-12 records governed by this contract are:

| Schema file | Schema identifier |
|---|---|
| `live_preparation.schema.json` | `mobileworld.g1.exact-request-replay-live-preparation-receipt/v1` |
| `openai_chat_call_plan.schema.json` | `mobileworld.g1.inert-openai-chat-call-descriptor/v1` |
| `openai_chat_call_block.schema.json` | `mobileworld.g1.inert-openai-chat-block-descriptor/v1` |
| `vllm_launch_plan.schema.json` | `mobileworld.g1.inert-vllm-launch-plan/v1` |
| `openai_chat_response_projection.schema.json` | `mobileworld.g1.openai-chat-response-projection/v1` |
| `injected_gpu_capacity_assessment.schema.json` | `mobileworld.g1.injected-gpu-inventory-assessment/v1` |

The `*_plan.schema.json` filenames describe inert data plans/descriptors only. They do not authorize
or expose a send, service start, resource probe, or execution path. These six schemas are additive
to the nine schemas of the accepted CPU/fake runner checkpoint; `schemas/g1_4/` therefore contains
fifteen schemas without changing the historical identifiers or semantics of the original nine.

## 8. Closed no-execution state

Every readiness or preparation aggregate emitted under this contract MUST keep these exact Boolean
values:

```json
{
  "execution_ready": false,
  "live_transport_validation_complete": false,
  "live_history_codec_ready": false,
  "curated_transformations_ready": false,
  "run_ready_seal_present": false,
  "provider_invocation_allowed": false,
  "treatment_response_generation_allowed": false,
  "formal_replay_ready": false,
  "client_factory_invoked": false,
  "network_used": false,
  "subprocess_started": false,
  "gpu_probed": false,
  "gpu_used": false,
  "model_loaded": false,
  "provider_invoked": false,
  "replay_executed": false,
  "generated_action_executed": false
}
```

`live_code_prepared` or an equivalent implementation-status fact, if later recorded, MUST be
reported separately from these fields and MUST NOT imply any readiness or authorization.
Historical telemetry such as `provider_invoked=false` is not a substitute for the authorization
guards.

## 9. Explicit prohibitions

This preparation phase MUST NOT reach or cause:

- any real or external model/provider invocation or any provider client factory;
- any socket, DNS, HTTP, network, endpoint-health, or credential path;
- any subprocess, shell, tmux, Docker, scheduler, service-start, or port-binding path;
- any GPU probe, allocation, reservation, kernel, model-weight load, tokenizer/model execution, or
  model-service launch;
- any Qwen/MAI live endpoint or seed-support proof;
- any formal-capsule send, treatment response, natural task, prefix replay, live replay, backend
  restore, GUI/tool/action execution, or response-to-request feedback;
- any claim-validity inference, intervention choice, correction/gold generation, rubric work, or
  runtime Sentinel behavior;
- any mutation of Collector v1, raw data, G1.1, accepted G1.2, either G1.3 publication, or the
  formal v1.1 capsule authorization guards.

Returned or fixture actions remain inert data and are never executed. No error, CLI option, test
fixture, resource snapshot, or resume record may weaken these prohibitions.

## 10. Deferred dependencies and completion

This contract does not authorize or implement:

- G1.5 live Qwen and MAI History Codecs;
- G1.6 curated complete plan sets, accepted-action gold, or admission seals;
- G1.7 serving-image/config, seed-support, backend-dependency, isolation, scorer-key, restorer,
  run-ready, or execution-authorization seals.

ALE-322 remains `IN_PROGRESS_LIVE_PROOF_DEFERRED` after this preparation. A future live connection
requires all applicable G1.5/G1.6/G1.7 artifacts, an approved endpoint and GPU resource, and a new
explicit owner authorization. Only then may a versioned live path replace the present hard stop and
collect evidence that exact model/settings/tools/images were sent and the host parser's action was
returned unchanged. Even after such authorization, ALE-322 never executes the returned GUI action.

## 11. Preparation acceptance

The code-preparation tranche is conformant only if CPU-only tests prove that:

1. model bindings and rendered plans are deterministic, closed, and byte-stable;
2. Qwen and MAI fields equal the frozen manifest without silent defaults or inference;
3. the SDK call plan preserves all request fields and adds only the registered replay seed;
4. the paired block applies one seed consistently while remaining formally unpaired and
   non-admissible;
5. the caller-injected response projection preserves exact content, derives only the pinned host
   parser input, and invokes neither provider nor parser;
6. the launch plan is inert data and cannot invoke a process or service;
7. resource assessment consumes only an injected snapshot and performs no host probe;
8. the existing v1 `send` and `execute_live_arm` methods remain mechanically fail-only;
9. every readiness and authorization field in Section 8 remains exactly false;
10. no client factory, socket, network, subprocess, GPU, model, replay, GUI/action, or credential
   path is reached;
11. all frozen upstream artifacts remain byte-unchanged and no formal replay artifact is published.

Passing these checks records code preparation only. It is not live Provider Codec acceptance and
does not complete ALE-322.
