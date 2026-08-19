# MobileWorld Audit Event Contract v1

Contract identifier: `mobileworld.audit.event/v1`  
Scope: immutable raw collection only  
Companion design: [`COLLECTOR_DESIGN.md`](./COLLECTOR_DESIGN.md)

Normative terms `MUST`, `MUST NOT`, `SHOULD`, and `MAY` have their usual requirements meaning.

## 1. Meaning of “exact” and “lossless”

In this contract, the authoritative raw model input is:

> the exact semantic application-layer argument object passed by MobileWorld to the provider SDK, captured immediately before each application-visible SDK method invocation and after adapter rendering, history pruning/folding, and model-specific argument rewrites.

It includes the complete model-visible payload and generation parameters. It excludes transport credentials.

This contract **does not claim to capture the provider SDK's final HTTP wire body**. An SDK may internally transform the argument object after MobileWorld calls it. Optional HTTP tracing can be added later as a distinct event family, but it MUST NOT replace the application-layer request record.

Likewise, one `request_id` identifies one application-visible `chat.completions.create(...)` invocation. The provider SDK may perform transparent HTTP retries inside that invocation. Unless separately instrumented, those hidden transport attempts are not observable and MUST NOT be invented as raw events; retain the SDK retry configuration in the manifest.

Artifactization MUST operate on a deep copy or serialized snapshot. It MUST NOT replace data URLs, mutate message dictionaries, consume streams, alter chunks, or otherwise change the objects on the real call path. Rehydration MUST recover message roles, message order, content-part order, text, tool structures, provider fields, and exact image bytes.

“Label-free” means that the raw stream contains observed runtime facts and collector-quality metadata, but no judgment about previous-step correctness, use, severity, causality, or rubric alignment.

## 2. Serialization primitives

### 2.1 JSON values

Event files are UTF-8 JSON Lines. Each physical line is one complete JSON object followed by `\n`. NaN and Infinity are forbidden. Unknown SDK/Pydantic objects MUST be serialized through their official JSON/model-dump method with their fully qualified class and package version retained.

If a value cannot be faithfully represented inline, it MUST be serialized into a blob and replaced only in the collector's copied artifact graph by a typed `BlobRef`. The original live object MUST remain untouched. Repeated large values such as data URLs SHOULD be externalized before the authoritative graph is stored so content addressing deduplicates them across requests.

### 2.2 `BlobRef`

Every blob reference has this exact shape:

```json
{
  "algorithm": "sha256",
  "digest": "64 lowercase hexadecimal characters",
  "byte_length": 123,
  "media_type": "application/json",
  "relative_path": "blobs/sha256/ab/abcdef..."
}
```

Constraints:

- `digest` is SHA-256 over the exact stored bytes.
- `relative_path` is relative to the run root and MUST resolve to the digest-addressed file.
- Blob bytes MUST NOT be transcoded, recompressed, pretty-printed, or overwritten at an existing digest.
- The writer MUST verify both digest and byte length after an atomic install.

### 2.3 Externalized data URL

In an inspectable request view, a model-visible data URL MAY be represented as:

```json
{
  "$externalized_data_url": {
    "original_text_blob": {"...": "BlobRef for the exact UTF-8 data URL string"},
    "content_blob": {"...": "BlobRef for the exact decoded bytes"},
    "media_type": "image/png",
    "base64_alphabet": "standard",
    "content_path": "messages[3].content[1].image_url.url"
  }
}
```

`original_text_blob` permits byte-for-byte reconstruction of the application-layer argument; `content_blob` directly identifies the actual image bytes. The same decoded image may be shared by many requests through its digest.

### 2.4 Observation image

An observation screenshot reference has this shape:

```json
{
  "pixel_blob": {"...": "BlobRef for canonical lossless PNG pixels"},
  "source_blob": null,
  "width": 1080,
  "height": 2400,
  "mode": "RGB",
  "representation": "canonical_png_from_runtime_pixels"
}
```

`source_blob` MUST be populated when the original environment response bytes are available. A canonical lossless PNG is sufficient to preserve the exact runtime pixel matrix but MUST NOT be described as the original environment wire encoding.

## 3. Common event envelope

Every event MUST contain exactly these common fields, plus the event-specific `payload`:

```json
{
  "schema_version": "mobileworld.audit.event/v1",
  "event_id": "UUIDv7 or ULID",
  "event_type": "event name",
  "run_id": "UUIDv7 or ULID",
  "task_run_id": "UUIDv7 or ULID, or null for run-only events",
  "stream_id": "run_id or task_run_id",
  "seq": 1,
  "wall_time": "RFC3339 timestamp with timezone",
  "monotonic_ns": 123456789,
  "caused_by_event_id": null,
  "producer": {
    "component": "mobile_world.audit",
    "version": "collector code version",
    "process_id": 123,
    "worker_id": "non-secret worker identifier"
  },
  "payload": {}
}
```

Rules:

- `event_id`, `run_id`, and `task_run_id` MUST be globally unique IDs, not derived only from task names or thread IDs.
- `stream_id` is `run_id` for the run lifecycle stream and `task_run_id` for a task stream.
- `seq` starts at 1 and increases strictly by one within a stream.
- `seq` and explicit causal IDs define order. Wall-clock time does not.
- `monotonic_ns` comes from a monotonic clock and is only comparable inside the originating process/boot domain.
- `caused_by_event_id` points to the immediate causal predecessor when one exists.
- `worker_id` MUST NOT contain a username, credential, full backend URL, or signed query string.
- Additional producer metadata belongs in the run manifest, not ad hoc envelope keys.

## 4. Identity and correlation fields

The following IDs have distinct meanings:

| Field | Meaning |
|---|---|
| `run_id` | One evaluation invocation and its configuration. |
| `task_run_id` | One physical attempt to run one task. A whole-task retry gets a new ID. |
| `step_id` | One call to the agent decision loop at index `t`. |
| `model_call_id` | One logical model invocation requested by an adapter/component. |
| `request_id` | One application-visible provider SDK invocation within a logical model call. |
| `retry_group_id` | Optional group spanning wrapper/adapter invocations that pursue the same intended model result. |
| `decision_id` | The value returned by one `agent.predict()` invocation. |
| `execution_id` | One attempted environment/tool/user action execution. |

All IDs MUST be emitted at their first event and reused verbatim. `attempt_index`, `adapter_attempt_index`, and `step_index` are 1-based; `chunk_index` is 0-based to match common stream APIs.

`attempt_index` counts application-visible SDK invocations inside one `model_call_id`. `adapter_attempt_index` counts wrapper invocations inside a `retry_group_id`. This distinction is required because some retries live inside the shared API helper while Seed also retries the entire streaming inference from the adapter. A component with no outer retry MAY set `retry_group_id` to `model_call_id` and `adapter_attempt_index` to 1.

## 5. Event types

### 5.1 `run_started`

Stream: run lifecycle stream. Payload:

```json
{
  "collector_mode": "fail_open_with_incomplete_marker",
  "repository": {
    "url": "https://github.com/Tongyi-MAI/MobileWorld",
    "commit": "40 hexadecimal characters",
    "dirty": false,
    "diff_blob": null
  },
  "runtime": {
    "python": "version",
    "mobileworld": "version or null",
    "openai_sdk": "version",
    "platform": "non-secret platform description"
  },
  "configuration": {
    "suite_family": "mobile_world",
    "agent_type": "seed_agent",
    "model_name": "provider model identifier",
    "max_step": 50,
    "max_concurrency": 1,
    "audit_enabled": true,
    "additional_config": {}
  },
  "excluded_secrets": ["api_key", "authorization_headers", "cookies"]
}
```

`additional_config` MUST exclude secrets. A dirty code run SHOULD retain a secret-scanned diff as `diff_blob` or at minimum a diff hash and `dirty: true`.

### 5.2 `task_started`

Stream: task stream, `seq = 1`. Payload:

```json
{
  "task_name": "CartManagementTask",
  "task_goal": "exact instruction passed to agent.initialize, or null if retrieval failed",
  "task_goal_status": "resolved",
  "task_index": 1,
  "suite_family": "mobile_world",
  "agent": {
    "adapter": "seed_agent",
    "model": "Seed-2.0-Pro",
    "configuration": {}
  },
  "environment": {
    "backend_id": "pseudonymous backend ID",
    "device_id": "emulator-5554"
  },
  "whole_task_attempt_index": 1
}
```

`task_goal` is raw model-relevant data and MUST be retained exactly. `task_goal_status` is `resolved` or `retrieval_failed`. Allocate the `task_run_id` before goal/environment initialization so a failed attempt remains visible; if retrieval fails, emit `task_started` with a null goal and close it with `task_ended(runtime_status="crashed")`.

### 5.3 `step_started`

Payload:

```json
{
  "step_id": "unique ID",
  "step_index": 3,
  "observation": {
    "screenshot": {"...": "Observation image"},
    "accessibility_tree": null,
    "tool_call": null,
    "ask_user_response": null
  },
  "agent_observation_keys": ["screenshot", "tool_call", "ask_user_response"]
}
```

This observation is `S_t`. Preserve explicit nulls. If an object is large, use a `BlobRef` while retaining its type and serialization metadata.

### 5.4 `adapter_state_snapshot` (optional)

Payload:

```json
{
  "step_id": "unique ID",
  "phase": "before_render",
  "adapter": "memgui",
  "state_class": "fully.qualified.ClassName or plain_mapping",
  "state_snapshot_blob": {"...": "BlobRef"},
  "state_view": {},
  "seen_by_model": false
}
```

Allowed `phase` values are `before_render`, `after_response`, `after_decision`, or a namespaced adapter-specific string. This event MUST NOT include derived reliability or provenance judgments.

### 5.5 `model_request`

One event is required for **every application-visible provider SDK invocation**, including wrapper retry invocations that fail.

Payload:

```json
{
  "step_id": "unique ID",
  "model_call_id": "logical call ID",
  "retry_group_id": "retry group ID",
  "adapter_attempt_index": 1,
  "request_id": "SDK invocation ID",
  "attempt_index": 1,
  "call_role": "actor",
  "component": "mobile_world.agents.implementations.seed_agent",
  "sdk": {
    "package": "openai",
    "version": "installed version",
    "method": "chat.completions.create"
  },
  "endpoint": {
    "origin": "https://model.example",
    "path": "/v1/chat/completions",
    "query_removed": true
  },
  "stream": true,
  "sdk_arguments_snapshot_blob": {"...": "BlobRef for authoritative lossless artifact graph"},
  "request_view": {
    "model": "Seed-2.0-Pro",
    "messages": [],
    "temperature": 0.7,
    "stream": true,
    "stream_options": {"include_usage": true}
  },
  "request_images": [
    {
      "content_path": "messages[3].content[1].image_url.url",
      "original_text_blob": {"...": "BlobRef"},
      "content_blob": {"...": "BlobRef"},
      "media_type": "image/png",
      "width": 1080,
      "height": 2400,
      "capture_status": "captured"
    }
  ],
  "excluded_transport_fields": ["api_key", "authorization_headers"]
}
```

`request_view` MUST preserve the full logical shape and ordering. It MAY mirror the authoritative artifact graph inline for inspection and MAY externalize large data URLs as specified in section 2.3. It MUST NOT be a flattened prompt string.

`sdk_arguments_snapshot_blob` is authoritative for application-layer reconstruction. It contains a canonical, typed, losslessly artifactized JSON graph generated from a private snapshot at the last application boundary before the SDK invocation. Large data URLs and non-JSON values may be typed references to other blobs; rehydrating the graph MUST reproduce the canonical serialized application arguments exactly. It does not claim to be the final HTTP wire body. A full inline base64 copy per request is neither required nor recommended.

`call_role` is an extensible string. For the frozen MobileWorld adapters, every top-level `agent.predict()` model call uses `actor`, including Planner-Executor's planner call; its nested UIINS call uses `grounder`. Future components MAY use `memory`, `environment_evaluator`, or `other:<name>`. The `component` path further disambiguates calls.

If bytes for a remote request image are not already available on the existing call path, preserve the exact URL in the authoritative snapshot and use `capture_status: "url_preserved_content_unavailable"`. Collector v1 MUST NOT issue an independent network fetch only for logging. Such a request is application-layer reconstructable but not image-content-complete.

### 5.6 `model_stream_chunk`

Emit exactly once for each chunk and before yielding that unchanged chunk to the consumer.

Payload:

```json
{
  "step_id": "unique ID",
  "model_call_id": "logical call ID",
  "retry_group_id": "retry group ID",
  "adapter_attempt_index": 1,
  "request_id": "SDK invocation ID",
  "attempt_index": 1,
  "chunk_index": 0,
  "raw_chunk_snapshot_blob": {"...": "BlobRef"},
  "chunk_view": {
    "id": "provider response ID",
    "choices": [],
    "usage": null
  }
}
```

The wrapper MUST record chunks lazily in the order received. It MUST NOT read ahead, aggregate before yielding, or return reconstructed chunk objects.

### 5.7 `model_response`

Emit once for a provider attempt that returns normally or whose consumer intentionally abandons an otherwise non-raising stream. A stream-iteration exception terminates with `model_attempt_failed`, not with `model_response`.

Payload:

```json
{
  "step_id": "unique ID",
  "model_call_id": "logical call ID",
  "retry_group_id": "retry group ID",
  "adapter_attempt_index": 1,
  "request_id": "SDK invocation ID",
  "attempt_index": 1,
  "response_mode": "stream",
  "raw_response": {
    "kind": "stream_chunks",
    "snapshot_blob": null,
    "chunk_event_ids": ["event ID"],
    "chunk_count": 1
  },
  "raw_response_view": null,
  "normalized_response": {
    "response_id": "provider response ID or null",
    "choices": [
      {
        "index": 0,
        "content": "assembled content or null",
        "reasoning_content": null,
        "tool_calls": [],
        "finish_reason": "stop"
      }
    ],
    "usage": {
      "prompt_tokens": 100,
      "completion_tokens": 20,
      "cached_tokens": 0,
      "provider_usage": {}
    }
  },
  "returned_value_snapshot_blob": null,
  "stream_state": "complete"
}
```

For non-streaming responses:

- `response_mode` is `non_stream`;
- `raw_response.kind` is `single_response`;
- `raw_response.snapshot_blob` is required;
- `chunk_event_ids` is empty and `chunk_count` is zero;
- `raw_response_view` contains the inspectable SDK response.

For streaming responses, raw chunks are authoritative. `stream_state` is `complete` or `consumer_abandoned`. An abandoned stream MUST retain every chunk yielded before abandonment. If the application wrapper returns a transformed scalar rather than the raw provider object, preserve that exact value in `returned_value_snapshot_blob`; `agent_decision.prediction_raw` later remains the authoritative value returned by `predict()`.

### 5.8 `model_attempt_failed`

Payload:

```json
{
  "step_id": "unique ID",
  "model_call_id": "logical call ID",
  "retry_group_id": "retry group ID",
  "adapter_attempt_index": 1,
  "request_id": "SDK invocation ID",
  "attempt_index": 1,
  "failure_phase": "provider_call",
  "exception": {
    "class": "fully.qualified.ExceptionClass",
    "message": "exact exception message",
    "details_blob": null
  },
  "partial_chunk_event_ids": [],
  "normalized_partial_response": null,
  "retry_planned": true
}
```

`failure_phase` is `request_serialization`, `provider_call`, `stream_iteration`, or `response_serialization`. A collector-only failure instead emits `collector_error`; it MUST NOT masquerade as a provider failure.

For `failure_phase: "stream_iteration"`, `partial_chunk_event_ids` MUST list all chunks yielded before the exception. `normalized_partial_response` MAY preserve an aggregate derived only from those already-recorded chunks; constructing it MUST NOT consume the stream further.

All application-visible adapter/helper retries MUST be visible as distinct `model_request` plus exactly one terminal response/failure event per `request_id`. SDK-internal transparent HTTP retries are recorded only if a separate transport hook actually observes them. Seed streaming MUST preserve every partial chunk and distinguish failed-partial, complete, and abandoned streams.

### 5.9 `agent_decision`

Payload:

```json
{
  "step_id": "unique ID",
  "decision_id": "unique ID",
  "prediction_raw": "exact P_t returned by agent.predict, or null",
  "prediction_snapshot_blob": null,
  "parsed_action": {
    "class": "mobile_world.runtime.utils.models.JSONAction",
    "serializer": "pydantic model_dump",
    "serializer_version": "installed pydantic version",
    "value": {
      "action_type": "click",
      "x": 500,
      "y": 1200
    }
  },
  "parse_outcome": "returned",
  "parse_exception": null,
  "source_model_call_ids": ["logical call ID"]
}
```

If `prediction_raw` is binary or otherwise large, store it in `prediction_snapshot_blob` and put a typed placeholder in `prediction_raw`. Preserve all `JSONAction` fields; do not normalize coordinates or infer a different action.

`parsed_action` MAY be null when `predict()` returned no action or raised. `parse_outcome` is a factual runtime state such as `returned`, `returned_prediction_none`, `fallback_returned`, or `raised`; it is not a quality label. On `predict()` exception, emit this event from the runner exception boundary with `prediction_raw`/`parsed_action` null unless a value was actually available, retain the exception under `parse_exception`, and link every model call already observed. Do not invent a model prediction from exception text.

### 5.10 `action_execution_started`

Emit immediately before calling the environment/tool/user execution path.

Payload:

```json
{
  "step_id": "unique ID",
  "decision_id": "unique ID",
  "execution_id": "unique ID",
  "execution_kind": "gui",
  "action": {"action_type": "click", "x": 500, "y": 1200}
}
```

Initial `execution_kind` values are `gui`, `mcp`, `ask_user`, and `answer`. Terminal actions that are not executed do not emit this event.

### 5.11 `transition_completed`

Emit when `execute_action()` returns an observation, even if transport status is non-200 or the task semantics were not achieved.

Payload:

```json
{
  "step_id": "unique ID",
  "decision_id": "unique ID",
  "execution_id": "unique ID",
  "pre_observation_event_id": "step_started event ID containing S_t",
  "action_execution_event_id": "action_execution_started event ID",
  "action": {"action_type": "click", "x": 500, "y": 1200},
  "execution_result": {
    "kind": "gui_transport",
    "request_body_snapshot_blob": {"...": "BlobRef or null"},
    "http_status": 200,
    "response_body_blob": {"...": "BlobRef or null"},
    "response_headers": {},
    "raw_tool_result_blob": null,
    "agent_visible_tool_result": null,
    "ask_user_response": null,
    "exception": null
  },
  "post_observation": {
    "screenshot": {"...": "Observation image for S_t+1"},
    "accessibility_tree": null,
    "tool_call": null,
    "ask_user_response": null
  },
  "duration_ns": 900000000
}
```

`post_observation` is `S_{t+1}` and MUST be captured immediately when returned. It MUST be emitted even when this action reaches `max_step`; it does not depend on a next loop iteration.

For MCP:

- retain `raw_tool_result_blob` before in-place truncation/conversion when accessible;
- retain `agent_visible_tool_result`, exactly matching the result placed into the returned observation;
- use `execution_result.kind: "mcp_tool"`.

For ask-user, preserve the exact user response both in `execution_result.ask_user_response` and in the post-observation field that the runtime returned.

Response headers MUST have credential-bearing fields removed. A transport return is not a semantic-success label.

### 5.12 `transition_failed`

Emit if action execution raises before returning an `Observation`.

Payload:

```json
{
  "step_id": "unique ID",
  "decision_id": "unique ID",
  "execution_id": "unique ID",
  "pre_observation_event_id": "step_started event ID",
  "action_execution_event_id": "action_execution_started event ID",
  "action": {},
  "available_execution_result": null,
  "post_observation": null,
  "exception": {
    "class": "fully.qualified.ExceptionClass",
    "message": "exact message",
    "details_blob": null
  },
  "duration_ns": 1000000
}
```

Never synthesize `S_{t+1}` after a failure. If a post-state was captured before the exception, preserve it in `post_observation` and describe its timing.

### 5.13 `transition_not_executed`

Emit for `FINISHED`, `UNKNOWN`, `ENV_FAIL`, prediction-none/prediction-exception, or another outcome that the runner intentionally does not send to the environment.

Payload:

```json
{
  "step_id": "unique ID",
  "decision_id": "unique ID",
  "pre_observation_event_id": "step_started event ID",
  "action": {"action_type": "finished"},
  "reason": "terminal_action",
  "post_observation": null
}
```

Do not treat the unchanged pre-observation as a fabricated `S_{t+1}`.

Initial factual `reason` values include `terminal_action`, `prediction_none`, and `prediction_exception`. The action may be null for the latter two.

### 5.14 `collector_error`

Payload:

```json
{
  "scope": "model_request",
  "related_event_id": null,
  "step_id": "unique ID or null",
  "exception": {
    "class": "fully.qualified.ExceptionClass",
    "message": "message safe for restricted raw storage"
  },
  "missing_artifacts": ["sdk_arguments_snapshot_blob"],
  "agent_execution_continued": true
}
```

A collector error makes the relevant task/run incomplete. It MUST NOT be converted into a history-error label. If the normal writer is unavailable, write the smallest equivalent record to a separate emergency append-only stream and reference it during finalization.

### 5.15 `task_ended`

Payload:

```json
{
  "runtime_status": "completed",
  "termination": {
    "source": "agent_terminal_action",
    "step_index": 12,
    "exception": null
  },
  "environment_evaluation": {
    "score": 0.0,
    "reason": "exact reason or null"
  },
  "teardown": {
    "returned": true,
    "result_snapshot_blob": null,
    "exception": null
  },
  "token_usage": {
    "prompt_tokens": 1000,
    "completion_tokens": 200,
    "cached_tokens": 0,
    "total_tokens": 1200
  },
  "capture_complete": true,
  "missing_artifacts": [],
  "collector_error_event_ids": []
}
```

`runtime_status` is `completed`, `aborted`, or `crashed`. `termination.source` is a factual extensible string such as `agent_terminal_action`, `answer_action`, `max_step`, `prediction_none`, `device_failure`, or `uncaught_exception`.

If the `max_step`th action was executable, its `transition_completed` or `transition_failed` event MUST precede `task_ended`; the post-observation MUST NOT be lost merely because no next decision step runs.

`capture_complete` is false if any required model request/response, request image, decision, transition, or task result artifact is missing. It says whether collection is complete, not whether the task succeeded.

### 5.16 `run_ended`

Stream: run lifecycle stream. Payload:

```json
{
  "runtime_status": "completed",
  "task_run_ids": [],
  "task_counts": {
    "started": 0,
    "completed": 0,
    "crashed": 0
  },
  "capture_complete": true,
  "collector_error_event_ids": [],
  "manifest_final_path": "manifest.final.json"
}
```

## 6. Valid event sequences

### 6.1 One normal streaming actor step

```text
step_started
model_request
model_stream_chunk * N
model_response(stream_state=complete)
agent_decision
action_execution_started
transition_completed
```

### 6.2 Application-visible helper retry

```text
model_request(attempt_index=1)
model_attempt_failed(retry_planned=true)
model_request(attempt_index=2)
model_response
```

Helper-level retries share `model_call_id` and have different `request_id` values. Adapter-level retries share `retry_group_id`, have increasing `adapter_attempt_index`, and MAY have different `model_call_id` values. Both application-visible kinds must remain visible; hidden SDK transport retries are not fabricated.

### 6.3 Partial Seed stream

```text
model_request(stream=true)
model_stream_chunk * N
model_attempt_failed(failure_phase=stream_iteration,
                     partial_chunk_event_ids=[...],
                     normalized_partial_response={...})
```

`model_attempt_failed` is the single terminal event for this SDK invocation. It may carry a normalized partial aggregate derived only from recorded chunks. It MUST NOT be preceded or followed by `model_response` for the same `request_id`. No yielded chunk may be missing.

### 6.4 Planner with grounder

```text
step_started
model_request(call_role=actor, component=planner_executor)
model_response
model_request(call_role=grounder)
model_response
agent_decision(source_model_call_ids=[planner_call, grounder_call])
...
```

### 6.5 Terminal action

```text
step_started
...model events...
agent_decision(A_t=FINISHED)
transition_not_executed(post_observation=null)
task_ended
```

### 6.6 Max-step executable action

```text
step_started(step_index=max_step)
...model events...
agent_decision
action_execution_started
transition_completed(S_t+1 captured)
task_ended(termination.source=max_step)
```

## 7. Required invariants

A v1 validator MUST enforce:

1. task stream `seq` is contiguous and unique;
2. event IDs are unique across the run;
3. causal references point to earlier events in the same task stream, except documented run references;
4. every `model_request` has exactly one terminal `model_response` or `model_attempt_failed` record;
5. chunks for a request have contiguous `chunk_index` values starting at zero;
6. application-visible helper retries share a logical `model_call_id`, use increasing `attempt_index`, and use unique `request_id` values; adapter retries share a `retry_group_id` and increase `adapter_attempt_index`;
7. every returned `agent_decision` links an existing `step_started`;
8. every `transition_completed` links the exact decision, action-execution event, and pre-observation;
9. executed max-step actions include their returned post-observation;
10. terminal non-executed actions have `post_observation: null`;
11. every blob exists and matches digest and byte length;
12. rehydrating the authoritative SDK-argument artifact graph matches the canonical pre-artifactization application-argument snapshot; the inspectable request view is structurally consistent with it;
13. `capture_complete: true` is impossible when a required event/blob is absent;
14. the collector event schema and collector-generated metadata contain no evaluation-label fields;
15. no API key, authorization header, cookie, or configured secret appears in any event or blob.

Reserved evaluation-label keys in v1 include, case-insensitively:

```text
history_status, uptake_evidence, downstream_effect, severity,
rubric_alignment, sentinel_verdict, keep, drop, replace, abstain
```

This restriction applies to collector-defined schema/metadata, not to faithfully captured opaque application data. A task instruction, model message, tool schema/result, provider response, or adapter snapshot may naturally contain a word or JSON key such as `keep`; it MUST be preserved. The validator therefore checks the typed event schema and known collector metadata locations, not a recursive string/key blacklist over request/response content or blobs.

The ordinary word “status” remains allowed for factual provider, transport, runtime, and capture states.

## 8. Schema evolution

- Additive optional fields MAY be introduced under a new documented minor collector version while retaining `mobileworld.audit.event/v1` semantics.
- A changed meaning, changed required event order, changed application-layer capture boundary, or changed blob reconstruction rule requires `mobileworld.audit.event/v2`.
- Raw v1 events MUST never be migrated in place. A migration writes a derived normalized dataset or a new raw copy with an explicit provenance event.
- Evaluation schema versions are independent of the raw event version.

## 9. Example

[`examples/sample_events.jsonl`](./examples/sample_events.jsonl) shows one task step with a streamed actor response, an exact multimodal request view, `S_t`, `P_t`, `A_t`, `S_{t+1}`, and task completion. Its digests and IDs are illustrative syntactically valid values; referenced blob files are intentionally not included in the documentation bundle.
