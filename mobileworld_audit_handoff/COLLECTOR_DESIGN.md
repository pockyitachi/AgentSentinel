# MobileWorld Runtime Audit Collector Design

Status: implementation design, no collector code in this handoff  
Target repository: official MobileWorld commit `0dcd0980eac64d76f498f93568a1ec0594b743c4`  
Raw event contract: [`EVENT_CONTRACT_V1.md`](./EVENT_CONTRACT_V1.md)

## 1. What we are building now

This phase is a **motivation study**, not the Sentinel implementation.

We need to determine, from natural MobileWorld runs:

1. whether an incorrect, stale, off-track, or otherwise unreliable previous-step statement was actually present in a later model input;
2. whether the later model output ignored it, behaved consistently with it, or explicitly reused it;
3. whether an observable harmful action or task failure followed.

The code to build now is therefore a passive runtime collector. It records facts that existed during the original run. It does not decide whether any fact is wrong.

The governing rule is:

> Raw collection is event-sourced, lossless, label-free, append-only, and behavior-preserving. Every error definition and every evaluation label belongs to a separate, versioned offline pipeline.

The collector has two capture surfaces:

- **Model I/O:** the exact final SDK arguments for every application-visible model SDK invocation, every visible wrapper/adapter retry, every streaming chunk, the raw provider response, and a normalized response view. SDK-internal hidden HTTP retries are not fabricated.
- **Runtime transition:** the current observation `S_t`, the prediction returned by the agent `P_t`, the parsed action `A_t`, the environment/tool/user result, and the post-action observation `S_{t+1}`.

The collector is not a prompt pretty-printer. It must preserve message roles, ordering, multimodal content placement, tool definitions, tool call identifiers, provider-specific fields, model parameters, retries, and the exact request images.

## 2. Explicit non-goals

Do **not** implement any of the following in this phase:

- Sentinel runtime decisions;
- rubric generation or rubric tracking;
- claim extraction;
- `KEEP`, `DROP`, `REPLACE`, or `ABSTAIN` operations;
- prompt filtering or rewriting;
- a misleading-history classifier;
- weak/strong noise labels;
- LLM-as-judge evaluation;
- automatic attribution of task failure;
- counterfactual replay;
- changes to an agent's native history policy.

In particular, none of these fields belongs in raw events:

```text
history_status
uptake_evidence
downstream_effect
severity
rubric_alignment
sentinel_verdict
```

An environment-produced task score and reason are raw runtime outcomes, so they **do** belong in `task_ended`.

## 3. Why collection and evaluation must be separate

Our first taxonomy will probably change. Today we may distinguish `WEAK_NOISE`, `POSSIBLE_MISLEAD`, and `EXPLICIT_USE`; later we may split false-success claims from stale state or change the denominator. Re-running GUI agents is expensive and may not reproduce the same state.

The raw run must therefore support any later evaluator that needs to reconstruct:

```text
the exact input seen by the model at decision t
+ the model's complete output
+ S_t -> P_t -> A_t -> S_{t+1}
+ tool/user results
+ final task score
```

Offline outputs are disposable and versioned:

```text
raw run (immutable)
    -> normalization v1
    -> exposure reconstruction v1
    -> annotations/taxonomy v1
    -> metrics v1

the same raw run
    -> normalization v2
    -> exposure reconstruction v2
    -> annotations/taxonomy v2
    -> metrics v2
```

Changing evaluation code must never edit the raw run and must never require a new MobileWorld run unless the originally collected event stream is incomplete.

## 4. Data captured for each decision step

For each decision step `t`, reconstructability requires the following causal chain:

```text
step_started(S_t)
    -> zero or more model calls
       -> one model_request per application-visible SDK invocation
       -> zero or more model_stream_chunk events
       -> model_response or model_attempt_failed
    -> agent_decision(P_t, A_t)
    -> action_execution_started(A_t), when an environment action is executed
    -> transition_completed(S_t, A_t, execution result, S_{t+1})
       or transition_failed / transition_not_executed
```

There may be more than one model call in a step. For example, planner and grounding calls must not be conflated. Every call has a `call_role` and `component` path. The final `agent_decision` links all source model calls.

### 4.1 `S_t`: pre-decision observation

Record the complete runtime `Observation` available to the agent:

- screenshot;
- accessibility tree, including explicit `null` when unavailable;
- `tool_call` result;
- `ask_user_response`.

The current MobileWorld runner passes screenshot, tool result, and user response into `predict()`. The collector must snapshot these values before `predict()` runs.

For a PIL screenshot, store a canonical lossless PNG of its exact pixel matrix, plus width, height, and mode. If the environment client can expose its original screenshot response bytes, retain those in a second blob as well. Never use a JPEG or a resized image as the only `S_t` record.

### 4.2 Exact final model request

Capture at the closest boundary before the provider SDK call, **after** all adapter rendering and all model-specific parameter rewrites, but **before** calling:

```python
client.chat.completions.create(...)
```

This placement matters. Capturing adapter arrays earlier is not enough: Seed may remove old images, Qwen may flatten history, Gelab may generate a rolling summary, and MemGUI may fold state. The research question concerns what the model actually received.

Each application-visible retry invocation is a new `model_request` event. If retry logic changes `max_tokens` to `max_completion_tokens`, both invocations must preserve their distinct final argument objects. Retries implemented outside `BaseAgent` also count: for example, Seed's adapter-level loop around streaming inference must preserve every invocation, every partial chunk, and whether each stream completed, failed partially, or was abandoned. SDK-internal transparent HTTP retries may be summarized through configuration but are not separately claimed unless actually instrumented.

The request record contains two complementary representations:

1. `sdk_arguments_snapshot_blob`: a content-addressed, typed **artifact graph** of the complete argument mapping passed to the SDK. Large repeated values are losslessly externalized to content-addressed leaves before this graph is stored. This is the authoritative reconstruction source.
2. `request_view`: the same logical shape in inspectable JSON, normally reusing the graph's blob references.

The snapshot includes at least:

- model name;
- messages in original order;
- system, user, assistant, and tool roles;
- every content part in original order;
- `reasoning_content`, when present in a request message;
- tool schemas and tool choice;
- tool calls and tool call IDs;
- response-format fields;
- temperature, top-p, token limits, stop configuration, seeds, and provider extras;
- `stream` and stream options;
- any other keyword passed to the SDK.

The exact transport authorization header and API key are deliberately excluded. They are credentials, not semantic model input. The event declares this exclusion explicitly.

Here, “exact final request” means the exact semantic application-layer payload MobileWorld hands to the SDK. We do not claim that this is the SDK's final serialized HTTP wire body, because the SDK may transform it internally. Artifactization runs only on a deep copy/snapshot and must be able to reconstruct role order, message order, content-part order, full text, tool structures, provider fields, and image bytes without changing the real `messages` object. Do not also embed the same multi-megabyte base64 in a unique full-request blob unless a policy explicitly requires that redundant copy; the artifact graph plus exact externalized leaves is authoritative and deduplicable.

### 4.3 Actual request images

Do not assume that the current environment screenshot is the image sent to the model. Adapters can resize, re-encode, retain older images, or remove them.

For every image in every request:

- retain its exact location in the message/content structure;
- retain the exact request snapshot containing its original data URL or URL value;
- decode embedded data URLs and store the exact decoded bytes in the content-addressed blob store without recompression;
- record media type, byte length, SHA-256, content-part path, dimensions when decodable, and capture status;
- retain every historical request image, not only the current screenshot.

For a remote URL, preserve the exact URL string. Collector v1 MUST NOT issue an additional network fetch merely for logging, because that could change timing, authorization state, or remote side effects. If the bytes are already available from the existing call path, they MAY be snapshotted without changing the request; otherwise record `capture_status: "url_preserved_content_unavailable"`. The application-layer request is still reconstructable, while image-content completeness is false and must be reported rather than silently assumed.

### 4.4 Raw and normalized model output

For a non-streaming response, store:

- an authoritative serialized raw SDK response blob;
- an inspectable raw response view;
- a normalized response containing choices, content, reasoning content, tool calls, finish reasons, and usage;
- the exact value returned upward by the wrapper when it differs from provider content.

For streaming:

- emit one `model_stream_chunk` event for every chunk, in observed yield order;
- serialize each raw SDK chunk before yielding it to the agent;
- never pre-consume or reorder the stream;
- on normal exhaustion, emit one terminal `model_response(stream_state="complete")` that references the chunk event IDs and stores the normalized assembled response;
- on deliberate consumer abandonment, emit one terminal `model_response(stream_state="consumer_abandoned")` with every chunk already observed;
- on stream iteration exception, emit `model_attempt_failed` as the **only** terminal event for that SDK invocation, including all partial chunk IDs and an optional aggregate derived only from those chunks.

If an SDK invocation raises, emit `model_attempt_failed` with the exception class and message and whether another invocation was planned. Every `request_id` has exactly one terminal `model_response` or `model_attempt_failed`, never both. Never discard failed attempts: malformed or partial outputs can matter to later history state.

### 4.5 `P_t` and `A_t`

`model_response` is not a substitute for the value returned by `agent.predict()`. Agents may parse, concatenate reasoning, transform output, make nested calls, or return a fallback.

After `predict()` returns, record `agent_decision` containing:

- the exact `prediction` value returned as `P_t`, including `null`;
- the complete parsed `JSONAction` dump as `A_t`, retaining explicit null fields when possible;
- the action model/class and serializer version;
- IDs of every model call used by the decision;
- a factual parse outcome or exception, without an evaluation label.

Do not infer a missing action from text offline if it was not the action actually returned at runtime.

### 4.6 Execution and `S_{t+1}`

Immediately before calling `env.execute_action(A_t)`, emit `action_execution_started`. Immediately after it returns, emit `transition_completed` with:

- the linked pre-observation event for `S_t`;
- the linked decision and exact `A_t`;
- GUI transport status/body when available;
- raw MCP tool result when applicable;
- raw ask-user response when applicable;
- the complete returned `Observation`, including post-action screenshot `S_{t+1}`;
- start/end monotonic times and duration.

This must occur before the runner tests the max-step boundary. Otherwise the post-state of the last executed action can be lost because it never becomes the next loop's `S_t`.

`HTTP 200`, a changed screenshot, or a returned observation is **not** semantic action success. Store them as execution facts only.

For `FINISHED`, `UNKNOWN`, or `ENV_FAIL`, where the runner does not execute an environment action, emit `transition_not_executed` with `post_observation: null` and the factual reason. If `execute_action()` raises, emit `transition_failed`; preserve any transport response and post-state that were available.

### 4.7 Task completion

Always attempt to emit `task_ended` from a `finally` path. It contains:

- completed, aborted, or crashed runtime status;
- termination source;
- final step index;
- environment evaluator score and reason exactly as returned;
- teardown outcome when available;
- cumulative token usage as reported by the agent;
- collector completeness and any missing-event diagnostics.

The score is not used by collection code to classify any history statement.

## 5. Optional adapter-state snapshots

The final model request proves what the model saw, but folded or summarized agents may make provenance difficult to recover. The collector may provide an optional, adapter-specific `adapter_state_snapshot` event at named phases such as `before_render`, `after_response`, or `after_decision`.

Examples include Seed history arrays, Qwen conclusions, Gelab rolling summary, GUI-OWL collapsed/recent partitions, and MemGUI summaries/latest interaction/UI memory.

These snapshots must be:

- raw copies only;
- marked `seen_by_model: false` unless the exact value is also present in a captured request;
- kept separate from the provider request;
- optional so that lack of an adapter plugin never blocks core request/transition collection.

They must not contain derived claim boundaries or reliability labels.

## 6. Storage layout

Recommended server layout:

```text
mobileworld_audit_data/
├── raw/
│   └── runs/<run_id>/
│       ├── manifest.start.json
│       ├── run.events.jsonl
│       ├── tasks/<task_run_id>/events.jsonl
│       ├── blobs/sha256/<first-two-hex>/<full-sha256>
│       └── manifest.final.json
└── derived/
    └── <evaluation_name>/<evaluation_version>/<evaluation_run_id>/...
```

Raw and derived roots must be physically or permission-wise separable. Evaluation processes get read-only access to `raw` and write access only to their own `derived` directory.

`manifest.start.json` records run configuration, repository commit, dirty-state flag and optional diff hash, collector version, schema version, agent configuration, environment versions, task selection, concurrency, and start time. It must not contain secrets.

`manifest.final.json` records end time, task streams, byte counts, blob counts, SHA-256 checksums, completeness, and collector errors. Finalization creates a new file; it does not rewrite `manifest.start.json` or event streams.

## 7. Event ordering and concurrency

MobileWorld can run tasks concurrently. Wall-clock timestamps do not provide a reliable total order.

Use these rules:

- one append-only event stream per `task_run_id`;
- a strictly increasing `seq` allocated by a single writer queue or a per-task lock;
- a separate run-level stream for run lifecycle events;
- stable causal IDs (`step_id`, `model_call_id`, `request_id`, `decision_id`, `execution_id`);
- `caused_by_event_id` for the immediate causal parent;
- `chunk_index` for streaming order;
- `attempt_index` for retry order;
- monotonic timestamps for durations and wall time only for cross-system inspection;
- no claim of a global ordering across task streams.

Blob creation is safe under concurrency by writing to a temporary file, verifying its digest and byte count, and atomically installing it at the digest path. Existing blobs must be verified, never overwritten.

Thread-local state alone is insufficient if async calls or worker boundaries are introduced. Propagate audit context explicitly or with `contextvars`, and pass it into direct clients such as the planner's UIINS grounder.

## 8. Immutability and integrity

Raw collection is append-only:

- never update or delete an emitted event;
- never replace a blob at an existing digest path;
- never add evaluation fields to raw events;
- never “fix” a malformed event in place;
- emit a later `collector_error` or superseding factual event when necessary;
- start a new run when collection code or schema semantics change.

Each finalized task stream receives a SHA-256 checksum in `manifest.final.json`. Each referenced blob is already addressed by its SHA-256. Finalization should set raw files read-only and, on the server, use an immutable/object-lock policy when available.

Canonical JSON for hashing uses UTF-8, sorted object keys, no insignificant whitespace, and normalized newline handling. Event file line order remains the authoritative task order; canonicalization is only for integrity verification.

## 9. Privacy and secret handling

Lossless GUI and prompt collection can contain names, emails, messages, account data, task secrets, and user replies. Treat the entire raw root as restricted research data.

Required controls:

- directory permissions default to owner-only;
- encryption at rest and in transit on the server;
- no raw event payloads in console/debug logs;
- documented retention and deletion policy;
- access audit for raw artifacts;
- derived redacted exports for sharing;
- never commit collected runs to Git.

Never persist:

- API keys;
- authorization headers;
- cookies or session tokens used only for transport;
- complete environment-variable dumps;
- signed URL query credentials when they are not part of model-visible content.

Do not silently redact model-visible task text, screenshots, user replies, tool results, or message content in the authoritative raw run; doing so would violate losslessness. If policy requires redaction before storage, mark the run `capture_complete: false` for analyses requiring the removed content and preserve a machine-readable redaction manifest. Prefer access control over modifying authoritative raw evidence.

## 10. Failure policy

The collector must not silently change the agent trajectory.

Recommended motivation-study mode is `fail_open_with_incomplete_marker`:

1. collection operates on deep copies or serialized snapshots;
2. a collection exception never mutates `messages`, response objects, chunks, observations, or actions;
3. the original model/environment call continues;
4. an emergency `collector_error` is emitted through a minimal fallback channel;
5. `task_ended.capture_complete` and `manifest.final.capture_complete` become `false`;
6. incomplete task runs are excluded from analyses needing the missing event.

A separate strict CI mode may fail the smoke test on any missing artifact. Do not use “lossless” to describe a run with an undisclosed collector failure.

## 11. Integration points in MobileWorld

Implementation should use minimal, explicitly gated hooks.

### 11.1 Configuration and lifecycle

Add an opt-in audit configuration to the evaluation command/runner. With auditing disabled, no active writer, provider wrapper, files, hashing, deep copy, or serialization should be created. A lightweight `NullRecorder` object is acceptable only if its methods are true no-ops and parity tests show no semantic change.

Create one run context per evaluation invocation and one task context per task attempt. A retry of a whole task after device failure gets a new `task_run_id`; do not overwrite the prior partial stream.

### 11.2 Provider boundary

Instrument `BaseAgent.openai_chat_completions_create` immediately before every `self.openai_client.chat.completions.create` call and around its response/stream. Capture after existing model-specific parameter transformations.

Audit direct provider calls that bypass this helper, especially the planner executor's UIINS grounding client. Use the same hook utility and set a distinct `call_role`, rather than duplicating serialization logic.

Before the nine-agent smoke test, search again for direct SDK calls:

```bash
rg -n 'chat\.completions\.create|responses\.create' src/mobile_world
```

Every agent-side result must either be captured or explicitly documented as out of scope.

### 11.3 Runner boundary

In `_execute_single_task`:

- allocate `task_run_id` when the task attempt begins; emit `task_started` once goal retrieval succeeds and before environment initialization, or with a factual null-goal retrieval failure so the crashed attempt is not invisible;
- emit `step_started` before `agent.predict()`;
- emit `agent_decision` immediately after `predict()` returns;
- emit execution/transition events around `env.execute_action()`;
- emit `task_ended` after score collection, with a fallback in `finally` for exceptions.

Do not reconstruct transitions later by pairing neighboring trajectory files. Link them during execution.

### 11.4 Environment boundary

The current client already receives the GUI step HTTP response but normally only logs it. Add a passive observation hook or a structured return side channel so the collector can retain response status/body without changing the `Observation` passed to existing agents.

For MCP actions, preserve the raw tool result before any in-place truncation or Markdown conversion, plus the exact post-processing result actually supplied to the agent. Record both with clear names. Do not remove the existing behavior.

## 12. Acceptance tests before research runs

The server implementation is not complete until all of these pass:

1. **Disabled equivalence:** audit disabled follows the original code path and creates no artifacts.
2. **No mutation:** hash/deep-compare messages before and after capture; object content reaching the SDK is unchanged.
3. **Request reconstruction:** rehydrate externalized blobs and reproduce the canonical pre-artifactization SDK argument snapshot byte-for-byte.
4. **Image fidelity:** every request image's decoded bytes match its content blob hash; historical images and current images are both represented.
5. **Retry coverage:** every application-visible SDK invocation has its own request and terminal success/failure event; the manifest discloses unobserved SDK-internal retry configuration.
6. **Streaming transparency:** chunk order/content seen by the agent matches the no-audit wrapper; early abandonment is recorded.
7. **Decision linkage:** `P_t` and the actual returned `A_t` link to all model calls for the step.
8. **Transition linkage:** an executed action always links `S_t` to the returned `S_{t+1}`.
9. **Last-step capture:** the post-state of the action at `max_step` is saved even though there is no next decision loop.
10. **Terminal action:** non-executed terminal actions have an explicit event and no invented post-state.
11. **Exception capture:** model, parser, transport, and environment exceptions yield factual events and an incomplete marker when needed.
12. **Concurrency:** parallel tasks have independent increasing sequences, unique IDs, and valid atomic blobs.
13. **Privacy:** keys and authorization headers do not appear in recursive artifact scans.
14. **Nine-agent smoke test:** two or three short tasks per registered adapter validate request shape and call roles, including nested grounder calls and MemGUI folding.
15. **Raw-schema lint:** reject evaluation-label fields from collector-defined schema/metadata, while preserving identical words or keys when they occur inside captured task/model/tool/provider data.

For behavior comparison, use deterministic settings where supported and compare audit-disabled versus audit-enabled outputs. Where the remote model is nondeterministic, test request equality and collector transparency with a recorded/fake provider.

## 13. Definition of done for this phase

The collector phase is done when a completed MobileWorld task can be reconstructed without reading ordinary console logs:

- every actual agent-side model request and attempt is present;
- every response/chunk is present in raw and normalized form;
- every image actually included in a model request is recoverable;
- every step exposes `S_t`, `P_t`, `A_t`, and the executed transition to `S_{t+1}` or an explicit non-execution/failure event;
- tool/user results and final task score are present;
- collection completeness is machine-checkable;
- no evaluator, taxonomy, rubric, or Sentinel judgment exists in raw data;
- a new offline evaluator can be written later without re-running MobileWorld.

Only after this data is collected should the offline history-exposure study define weak noise, possible uptake, explicit use, and harmful propagation.
