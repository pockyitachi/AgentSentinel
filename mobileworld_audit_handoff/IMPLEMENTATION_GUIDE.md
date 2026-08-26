# MobileWorld 无损运行时审计采集器：实现指南

## 0. 文档用途

本文档是服务器端实现规格，不是概念草稿。目标是在官方 MobileWorld 指定快照中实现一个：

> event-sourced、lossless、label-free、默认关闭、对 agent 决策零干预的 Runtime Audit Collector。

它服务于当前的 motivation study：调查 GUI agent 的错误、失效或偏航历史是否仍被注入后续模型输入，以及这些历史是否被后续输出复用。当前阶段不实现 Sentinel，不判断历史对错，也不修改任何 prompt。

本文所有源码定位均以以下仓库快照为准：

```text
repository: Tongyi-MAI/MobileWorld
branch: main
commit: 0dcd0980eac64d76f498f93568a1ec0594b743c4
commit date: 2026-08-04
Python: >=3.12,<3.13
```

开始工作前必须运行：

```bash
git rev-parse HEAD
git status --short
```

若 `HEAD` 不是上述 commit，不得静默套用行号；先记录差异，再按符号重新定位调用链。

---

## 1. 当前研究阶段与严格边界

### 1.1 本阶段要回答的问题

1. 模型在每个决策点实际收到了什么完整输入？
2. 输入中实际暴露了哪些 previous-step 文本、summary、folded memory 或旧截图？
3. 模型返回了什么完整原始输出，包括失败调用和重试？
4. 对应的 GUI transition 是什么：动作前状态、解析 action、动作执行结果、动作后状态？
5. 任务最终 score/reason 是什么？

### 1.2 本阶段明确不做

- 不生成 task rubric。
- 不调用额外 LLM 判断历史真伪。
- 不输出 `KEEP / DROP / REPLACE`。
- 不过滤、改写、重排或总结 prompt。
- 不改变九个 agent 的历史策略。
- 不把 `HTTP 200` 或页面变化当作语义成功。
- 不在 collection code 中定义 `weak noise`、`strong mislead` 等评测标签。
- 不把观察性相关写成因果结论。

### 1.3 规范符号

后续所有代码、schema 和离线分析统一使用：

```text
T       task instruction
S_t     第 t 次决策前，runner 持有的 GUI observation
I_t     第 t 次 actor 模型调用实际发送的完整 request
P_t     第 t 次 agent.predict() 返回给 runner 的 exact prediction；可由一个或多个 provider response 组装/转换
A_t     与 P_t 一同返回给 runner 的 parsed JSONAction
R_t     执行动作得到的 transport/tool/user execution evidence
S_{t+1} 执行 A_t 后得到的新 observation
H_t     I_t 中实际存在的历史部分，不是假定的 agent 内部 history
R_i     可追踪的历史 record/claim 来源
```

一条可观察 transition 是：

```text
S_t -> I_t -> P_t -> A_t -> R_t -> S_{t+1}
```

Collector 记录证据，不解释证据。

---

## 2. 官方快照的真实调用链

### 2.1 九个内置顶层 agent

权威注册表：

```text
src/mobile_world/agents/registry.py
AGENT_CONFIGS
```

注册的九个 adapter：

```text
qwen3vl
planner_executor
mai_ui_agent
general_e2e
seed_agent
gelab_agent
ui_venus_agent
gui_owl_1_5
memgui
```

框架还支持从 `.py` 文件动态加载 `BaseAgent` 子类，因此“九个”是内置 adapter 数量，不是框架能力上限。

### 2.2 顶层模型调用的共同入口

文件：

```text
src/mobile_world/agents/base.py
BaseAgent.openai_chat_completions_create()
```

九个内置顶层 adapter 都调用这个 helper。这里是捕获最终 actor request 的最佳公共边界，因为 adapter 已经完成了各自的：

- history replay；
- screenshot pruning；
- flat progress 拼接；
- rolling summary；
- folding/memory rendering。

必须记录 helper 真正传给 `self.openai_client.chat.completions.create(...)` 的 payload，而不是 adapter 构造前的内部数组，也不是 pretty-print 日志。

### 2.3 唯一必须单独覆盖的模型调用

文件：

```text
src/mobile_world/agents/grounding/uiins.py
UIINSGroundingAgent.predict()
```

`planner_executor` 的 UIINS grounder 创建了独立 `OpenAI` client，并直接执行：

```python
self.vlm.chat.completions.create(...)
```

它不经过 `BaseAgent.openai_chat_completions_create()`。必须添加相同的采集 hook，并标注：

```text
call_role = "grounder"
```

顶层九个 agent 的决策调用标注：

```text
call_role = "actor"
```

不得把 planner prompt 与坐标 grounder prompt 合并成一次调用。

### 2.4 标准 benchmark runner 的 transition 边界

文件：

```text
src/mobile_world/core/runner.py
_execute_single_task()
```

该函数当前顺序为：

```text
obs = env.initialize_task(...)
agent.initialize(task_goal)

prediction, action = agent.predict(obs fields)
traj_logger.log_traj(..., obs, ...)
obs = env.execute_action(action)  # 非 terminal；ANSWER 也会执行
score, reason = env.get_task_score(...)
```

所以 runner 在运行时已经持有构造 transition 所需的大部分对象，只是现有 `TrajLogger` 没有把它们显式绑定为一条不可变记录。

Collector hook 的正确顺序是：

```text
begin_step(S_t)
  -> agent.predict()
  -> record_decision(P_t, A_t)
  -> env.execute_action(A_t)
  -> complete_step(S_{t+1})
```

关键要求：动作执行返回后立刻保存 `S_{t+1}`。不能等待下一轮把它当作新的 pre-state 再保存，否则 max-step 的最后一个 post-state 会丢失。

### 2.5 环境执行细节

文件：

```text
src/mobile_world/runtime/client.py
AndroidEnvClient.execute_action()
AndroidMCPEnvClient.execute_action()
```

普通 GUI action：

- POST `/step`；
- 当前只把 `status_code` 和 `response.text` 打进 debug log；
- 等待稳定后重新截图；
- `ask_user` 会从 response body 提取用户回复；
- 返回新的 `Observation`。

MCP action：

- 调用 `client.call_tool_sync(...)`；
- 对 HTML 结果做现有 `_truncate_tool_call()` 处理；
- 获取新截图；
- 将结果放入返回 observation 的 `tool_call`。

为保持 collection 灵活性，启用 audit 时应捕获现有函数中已经可见的 transport/tool evidence，并由 runner写入 `transition_completed/failed`，但不得改变 `Observation` 的公共结构或 agent 收到的字段。

### 2.6 现有 TrajLogger 不能替代新 Collector

文件：

```text
src/mobile_world/runtime/utils/trajectory_logger.py
TrajLogger
```

现有 logger：

- 保存动作前截图；
- 保存解析后的 prediction/action；
- 保存 score；
- 会重写整个 `traj.json`；
- 不保存最终 model request；
- 不保存 provider raw response；
- 不显式关联 `S_t --A_t--> S_{t+1}`；
- 最后一步 post-state 可能缺失。

新 Collector 与它并行工作，不删除、不替换现有日志，第一版也不要重构它。

---

## 3. 总体架构

### 3.1 推荐新增模块

在 MobileWorld 仓库中新增：

```text
src/mobile_world/runtime/audit/
├── __init__.py
├── config.py
├── context.py
├── schemas.py
├── serializer.py
├── blob_store.py
├── recorder.py
└── null_recorder.py
```

建议职责：

```text
config.py
  feature flag、输出根目录、失败策略、stream chunk 开关

context.py
  当前 run/task-attempt/step/logical-call 的 ContextVar；不得使用进程级可变全局变量

schemas.py
  schema_version、事件 envelope、类型约束

serializer.py
  OpenAI/Pydantic/PIL/exception 的无副作用序列化与二进制外置

blob_store.py
  SHA-256 content-addressed blob store、原子写入、校验

recorder.py
  per-task-run append-only recorder、seq、flush、manifest.start/final

null_recorder.py
  feature flag 关闭时的 no-op 实现
```

测试建议放在：

```text
tests/runtime/audit/
```

离线 evaluator 不放进 `runtime/audit/`，见 `OFFLINE_EVALUATION_DESIGN.md`。

### 3.2 Feature flag

推荐 CLI：

```text
--enable-audit
--audit-log-root PATH
--audit-store-stream-chunks / --no-audit-store-stream-chunks
```

默认值：

```text
enable_audit = false
audit_log_root = unset；启用时必须显式指定 Git 工作树之外的受限目录
audit_store_stream_chunks = true
runtime collector policy = fail_open_with_incomplete_marker（固定，不是 CLI 选项）
```

需要在以下链路显式透传：

```text
src/mobile_world/core/subcommands/eval.py
  _add_common_arguments()/configure_parser()/execute()

src/mobile_world/core/runner.py
  run_agent_with_evaluation()
  _process_task_on_env()
  _execute_single_task()
```

禁止仅靠隐式环境变量启用，以免研究 run 的配置无法从命令和 manifest 复原。可以额外支持环境变量作为默认值，但 manifest 必须写入解析后的最终值。

### 3.3 默认关闭时的行为

当 `enable_audit=false`：

- 不创建 audit 目录；
- 不复制 messages；
- 不序列化 response；
- 不计算图片 hash；
- 不增加模型调用；
- 不改变 retry 次数、异常类型、sleep、返回值或执行顺序；
- 九个 agent 和现有 `TrajLogger` 的输出保持原样。

使用 `NullRecorder`，避免业务路径散布文件系统判断，但必须保证 no-op 调用本身不会触发重序列化。

### 3.4 启用时的失败策略

所有真实 eval/runtime collection 使用权威契约定义的唯一策略：

```text
collector_mode = fail_open_with_incomplete_marker
```

即使 collector 开启，采集故障也不能改变原 agent trajectory。若 storage/serialization 失败：

1. 采集只作用于副本，不污染 live object；
2. 原 model/environment call、response、action和异常传播继续原路径；
3. 尽最大可能通过 emergency append-only stream 写 `collector_error`；
4. `task_ended.capture_complete` 与 `manifest.final.capture_complete` 设为 `false`；
5. 缺失 artifact 写入 `missing_artifacts`，相关 run 不进入依赖该证据的分析。

collector error 不得中止task、触发MobileWorld task retry或替换原返回值/异常，否则会改变trajectory。完整性故障在运行后交给 offline integrity checker 判定；CI 通过 fault-injection test 断言故障被标记、checker拒绝不完整run，同时原agent/env路径继续，不提供可进入真实runtime的严格失败模式。

---

## 4. Event-sourced 数据模型

### 4.1 不可变事件 envelope

`EVENT_CONTRACT_V1.md` 是 raw schema 的唯一权威。每个 JSONL event 的公共 envelope 必须恰好包含契约字段；task/step/call相关字段放入 event-specific `payload`：

```json
{
  "schema_version": "mobileworld.audit.event/v1",
  "event_id": "UUIDv7-or-ULID",
  "event_type": "model_request",
  "run_id": "UUIDv7-or-ULID",
  "task_run_id": "UUIDv7-or-ULID",
  "stream_id": "task_run_id",
  "seq": 23,
  "wall_time": "2026-08-18T20:00:00.000000Z",
  "monotonic_ns": 123456789,
  "caused_by_event_id": null,
  "producer": {
    "component": "mobile_world.audit",
    "version": "collector-version",
    "process_id": 123,
    "worker_id": "non-secret-worker-id"
  },
  "payload": {}
}
```

约束：

- `seq` 在单个 `stream_id` 内从1开始严格加一；
- 关联依赖 ID，不依赖相邻行；
- event 写出后不修改；
- task auto-retry 是新的 `task_run_id`；
- provider retry 使用同一个 `model_call_id`、不同 `request_id/attempt_index`；
- Planner-Executor 顶层 planner call 使用 `call_role=actor`，UIINS 使用 `call_role=grounder`；两者使用不同 `model_call_id`，但共享 step/task IDs。

### 4.2 必需事件类型

第一版必须支持：

```text
run_started
task_started
step_started
adapter_state_snapshot       # optional、无标签
model_request
model_stream_chunk           # streaming 时
model_response
model_attempt_failed
agent_decision
action_execution_started
transition_completed
transition_failed
transition_not_executed
collector_error
task_ended
run_ended
```

`model_response` 必须同时包含：

- provider response 的 raw serializable form（non-stream）；或所有 chunks 的组装状态（stream）；
- normalized convenience fields；
- usage；
- finish reason；
- response/chunk completeness；
- 返回给 adapter 的文本值。

Normalized 字段只是便利派生，不能替代 raw 字段。

### 4.3 Run manifest

每个 run分别写不可变 `manifest.start.json` 与新增的 `manifest.final.json`；开始文件不能被结束过程覆盖。start manifest至少包含：

```json
{
  "raw_schema_version": "mobileworld.audit.event/v1",
  "run_id": "...",
  "repository": "Tongyi-MAI/MobileWorld",
  "git_commit": "0dcd0980eac64d76f498f93568a1ec0594b743c4",
  "git_dirty": false,
  "python_version": "3.12.x",
  "mobile_world_version": "0.1.0",
  "agent_type": "seed_agent",
  "model_name": "...",
  "suite_family": "mobile_world",
  "resolved_cli_config": {},
  "resolved_agent_runtime_config": {},
  "environment_image": "...",
  "started_at_utc": "...",
  "collection_policy": {
    "label_free": true,
    "prompt_intervention": false,
    "collector_mode": "fail_open_with_incomplete_marker",
    "stream_chunks": true
  }
}
```

不得写 API key、Authorization header、cookie 或 `.env` 内容。

### 4.4 推荐磁盘布局

```text
<audit_log_root>/raw/runs/<run_id>/
├── manifest.start.json
├── run.events.jsonl
├── tasks/
│   └── <task_run_id>/
│       └── events.jsonl
├── blobs/
│   └── sha256/
│       └── ab/
│           └── <full_sha256>
└── manifest.final.json
```

不要把 `derived/` 写在 runtime collector 的代码路径中。离线 pipeline 可以在 run 根目录旁创建独立输出，但 raw 目录必须只读。

---

## 5. 完整模型 request 的采集

### 5.1 何谓“完整输入”

必须记录 SDK 调用的实际参数对象：

```text
model
messages（原顺序、role、content blocks、reasoning_content 等 provider 扩展）
tools/tool_choice（若存在）
temperature/top_p/max_tokens/max_completion_tokens
stream/stream_options
extra_body
seed、penalties 和其他 kwargs
```

不要只保存：

- 拼接后的纯文本；
- `pretty_print_messages()` 输出；
- adapter 的 `history_responses`；
- SDK 调用前、尚未完成模型兼容转换的 kwargs。

### 5.2 Base helper 中的准确时机

`BaseAgent.openai_chat_completions_create()` 会按模型名修改 kwargs：

- `claude`：修改 `max_tokens` 并删除 `temperature`；
- GPT/o1：把 `max_tokens` 改成 `max_completion_tokens`；
- Kimi：增加 `extra_body.enable_thinking`；
- streaming：增加 `stream_options.include_usage=true`。

因此每个 `model_request` 必须在这些转换完成后、紧邻实际 SDK call 前采集。

重要：传给 SDK 的原始 `messages` 对象必须保持不变。Collector 只能深度序列化副本，不能把 `image_url` 原地替换成 `image_ref` 后再发送给模型。

### 5.3 Logical call 与 application-visible SDK attempt

必须区分：

```text
model_call_id
  一个 adapter 希望取得一次模型输出的逻辑调用

request_id / attempt_index
  Base helper 或 adapter 外层 retry 产生的、代码中实际可见的 SDK method invocation
```

当前 Base 的 non-stream helper 有自己的 while retry；多个 adapter 还有外层 retry。Seed 的 streaming helper由 adapter 外层重试。最安全的记录方式：

- 每次代码实际执行 `chat.completions.create()` 前生成/递增 visible attempt；
- 每次成功、异常都闭合对应 attempt；
- adapter 外层再次进入 Base helper时，可以生成新的 `model_call_id`，并用 `retry_group_id/adapter_attempt_index`连接；
- 离线 normalizer再根据 step/call role/时间判断它们是否属于同一次 decision 的 retry group；不要在 raw collector 中猜测。

若实现者能无侵入地从 runner 注入 `decision_id`，所有 actor logical calls 应共享该 `decision_id`；这是推荐做法。

### 5.4 Non-stream response

记录 SDK response 的：

```python
response.model_dump(mode="json", exclude_none=False)
```

如 provider 返回非标准对象，serializer 应：

1. 优先 `model_dump`；
2. 其次安全遍历公开字段；
3. 最后才使用 `repr`，同时标记 `serialization_fidelity="repr_fallback"`，并将对应 task/run 的 `capture_complete=false`；
4. 不得因无法序列化某个扩展字段而悄悄删除整次 response。

还要记录 Base 最终返回给 adapter 的 `final_content`，包括 Kimi reasoning 被拼进 `<think>` 的结果。这样可以同时研究 provider raw response 和 agent 实际存入 history 的文本。

### 5.5 Streaming response

Seed 使用 streaming。`_wrap_stream_with_usage_logging()` 当前逐 chunk yield，并在流结束后记录 usage。

新的 wrapper 必须：

1. 在 SDK call 前写 `model_request`；
2. 每消费一个 chunk，先无损序列化为 `model_stream_chunk`，再原样 yield 同一个 chunk object；
3. 不改变 chunk 顺序、对象内容、生成器异常和结束语义；
4. 正常耗尽后写 `model_response(stream_state=complete)`；
5. 中途异常时写 `model_attempt_failed` 并保留 partial chunk IDs，再原样重新抛出；
6. consumer 提前停止时以唯一的 `model_response(stream_state="consumer_abandoned")` 闭合该 `request_id`；
7. 保存 usage-bearing final chunk；
8. 支持将 chunks 离线重新拼接并与 adapter 返回的 `P_t` 对照。

不能为了日志先一次性消费完整 stream 再返回给 agent，这会改变 streaming 行为、内存和异常时机。

实现应在 generator wrapper 中显式处理 normal exhaustion、iteration exception 与 `GeneratorExit/close()` 三条路径，并保证每个 `request_id` 恰有一个 terminal event。若进程在 consumer 未 close 的情况下崩溃，integrity checker 应把该 request/run 标为 incomplete，不能在重启后伪造 abandoned/completed。

### 5.6 UIINS grounder

在 `UIINSGroundingAgent.predict()` 的 direct call 周围复用相同 recorder API：

```text
call_role = grounder
model_name = executor model
parent_decision_id = 当前 planner step decision
```

UIINS 自己有 `max_retries=3` SDK 配置和显式 `for attempt in range(3)`。尽可能捕获应用层每次 direct call；SDK 内部透明 retry 未必可见，manifest 应记录 SDK 的 `max_retries=3`，但不要声称已捕获无法观察到的内部 HTTP attempt。

---

## 6. 图片与二进制的无损外置

### 6.1 两类图片必须区分

```text
observation image
  runner/env 当前持有的 PIL pixels，即 S_t 或 S_{t+1}

request image
  adapter 最终放入 I_t 的 data URL/image content；可能 resize、compress、prune
```

只保存 observation screenshot 不足以重建模型输入。必须从最终 request payload 中提取模型实际收到的每张 request image。

### 6.2 Content-addressed blob store

所有 blob 用 SHA-256 寻址，原子写入：

```text
tmp file in same filesystem
write + flush + fsync
verify hash
os.replace(tmp, final)
```

同一内容只存一次。并发写同一 hash 必须安全。

### 6.3 Request image 外置规则

对 `data:<mime>;base64,<payload>`：

1. 在序列化副本中定位，不修改原 request；
2. 保存 decoded bytes blob；
3. 保存以下 metadata：

```json
{
  "type": "image_ref",
  "blob_id": "sha256:...",
  "mime_type": "image/png",
  "decoded_bytes": 12345,
  "data_url_prefix": "data:image/png;base64,",
  "encoded_sha256": "...",
  "encoded_length": 16460,
  "canonical_base64": true,
  "width": 1080,
  "height": 2400
}
```

4. 若原 base64 不是由 decoded bytes 标准编码可逐字节重建，额外保存 encoded ASCII blob；
5. acceptance test 必须重建 canonical request JSON，并校验 hash 与采集前 payload 一致。

HTTP image URL 不能下载替换；原 URL 原样保存并标注 `external_ref=true`。Collector 不进行额外网络请求。

### 6.4 Observation screenshot

MobileWorld client 已把服务端 PNG 解码为 PIL Image，原始 HTTP base64 payload不再暴露给 runner。第一版保存：

- 无损 PNG；
- width/height/mode；
- pixel hash；
- encoded PNG hash。

文档中必须准确称其为“对 runner 可见 pixels 的无损快照”，不能声称保存了服务端原始网络字节。

---

## 7. GUI transition 的采集

### 7.1 Runner 层事件顺序

在 `_execute_single_task()` 中：

```text
task_started
  payload: T（若goal retrieval失败则按契约显式null/status）；在environment initialization前建立task attempt

step_started
  payload: step_id, S_t ref, tool_call, ask_user_response

agent.predict(...)

agent_decision
  payload: P_t（adapter返回值）, A_t model_dump, token usage snapshot

action_execution_started

env.execute_action(A_t)

transition_completed
  payload: S_{t+1} ref, tool_call, ask_user_response, transport ref

task_ended前确保当前step已有transition终态
```

`step_started` 中的 observation 必须与实际传给 `agent.predict()` 的字段一致。

### 7.2 Terminal actions

当前 runner 对 `ENV_FAIL`、`FINISHED`、`UNKNOWN` 不执行环境动作。记录：

```json
{
  "event_type": "transition_not_executed",
  "reason": "terminal_action",
  "post_observation": null
}
```

`ANSWER` 当前会执行 `env.execute_action(action)` 后终止，因此必须保存返回的 post-observation。

### 7.3 Max-step 边界

当前 runner 是执行非 terminal action 后再检查 `step >= max_step`。所以第 `max_step` 次 action 的 `S_{t+1}` 已存在。Collector 必须在 break 之前保存它。

### 7.4 Environment hook

为了保存 `R_t`，在 `AndroidEnvClient.execute_action()` 与 `AndroidMCPEnvClient.execute_action()` 内使用当前 ContextVar收集execution facts，再由runner闭合 `transition_completed/failed`：

普通 GUI action至少保存：

```text
request endpoint（不含 host credential）
action payload
status code
response body
elapsed time
exception（类型、message、traceback）
```

MCP action至少保存：

```text
action_name
action_json
tool result as observed before/after existing truncation，明确字段名
elapsed time
exception
```

若原始 MCP result 可能含敏感内容，仍不能在 collector 中做研究标签式删减；应采用预先声明的 security policy，并在 manifest 标记。默认只收任务环境数据，绝不收 API credentials。

不要改变 client 返回的 `Observation`，也不要增加额外截图请求。

### 7.5 异常路径

以下都要有闭合事件：

- `agent.predict()` 抛异常；
- prediction 为 `None`；
- response parse 失败但 adapter 返回 `UNKNOWN`；
- `env.execute_action()` 抛异常；
- `get_task_score()` 失败；
- device unhealthy 导致 task retry；
- teardown 失败。

Raw 数据中保留失败 attempt。现有 `traj_logger.reset_traj()` 不得重置或覆盖 audit attempt。

---

## 8. 并发与一致性

MobileWorld 使用 `joblib.Parallel(..., backend="threading")` 并发执行任务；线程和 environment 通过 queue 复用。

硬性要求：

- 每个 task attempt 独立 recorder 和 `events.jsonl`；
- 不使用单个全局 `current_step`；
- 使用 `contextvars.ContextVar` 或显式参数绑定当前 recorder；
- ContextVar 必须在 `finally` 中 reset，防止 environment/线程复用污染下一任务；
- blob store 可共享，但写入必须原子和并发安全；
- task 名只作为可读metadata，唯一性依赖 `task_run_id`；
- `run_events.jsonl` 若多线程共享，必须有 lock；更简单的方案是 task 文件独立、run completion 最后汇总；
- crash 后已完整写出的 JSONL 行保持可读，尾部半行由 integrity checker报告，不静默忽略。

不要依赖 thread ID 作为永久主键；线程会复用。thread ID只可作为 diagnostic metadata。

---

## 9. 运行配置与命令

### 9.1 安装与静态检查

```bash
uv sync --extra dev
uv run ruff check src/mobile_world/runtime/audit tests/runtime/audit
uv run pytest tests/runtime/audit -q
```

### 9.2 单任务 smoke run 模板

先通过服务器 secret manager/受限环境注入变量，并把 `MOBILEWORLD_AUDIT_ROOT` 指向 MobileWorld Git 工作树之外的受限数据目录；不要把变量解析后的 secret 写进命令日志：

```bash
uv run mw eval \
  --agent_type seed_agent \
  --task "$MW_SMOKE_TASK" \
  --max_round 5 \
  --model_name "$MW_MODEL_NAME" \
  --llm_base_url "$MW_LLM_BASE_URL" \
  --api_key "$MW_MODEL_API_KEY" \
  --log_file_root traj_logs/audit_smoke_seed \
  --enable-audit \
  --audit-log-root "$MOBILEWORLD_AUDIT_ROOT" \
  --max-concurrency 1 \
  --auto-retry 0
```

不要把带真实 key 的完整命令写入 manifest 或提交到 git。

### 9.3 九 agent smoke 范围

基础设施验证阶段每个 adapter只跑 2–3 个短任务；不是完整研究实验。首先确认 exact request 捕获：

```text
seed_agent          raw replay + streaming
general_e2e         raw replay + placeholder old images
mai_ui_agent        raw replay + deleted old image messages
planner_executor    actor + UIINS grounder two call roles
qwen3vl             flattened Task progress
ui_venus_agent      flattened Previous Actions
gelab_agent         rolling summary
gui_owl_1_5         hybrid collapsed history
memgui              folded summaries/latest interaction/UI memory
```

smoke 通过后，motivation study 再按研究计划选择代表 agent；不要一开始运行 `9 agents × all tasks`。

---

## 10. 分阶段实现顺序

### Phase 0：Freeze 与 fixture

1. 验证 commit 和 dirty state。
2. 记录九个 agent registry。
3. 为 Base helper、stream、UIINS、runner 建 fake fixtures。
4. 在不改代码前保存 feature-off baseline 行为。

### Phase 1：Schema、blob store、NullRecorder

1. 实现 raw schema version。
2. 实现原子 JSONL writer。
3. 实现 content-addressed blob store。
4. 实现 messages/response/PIL serializer。
5. 实现完整性检查器。

### Phase 2：Model I/O hooks

1. Base non-stream request/response/retry。
2. Base streaming chunks/completion/error。
3. UIINS direct request/response/retry。
4. 验证原 payload/object 未变。

### Phase 3：Runner transition hooks

1. run/task/attempt lifecycle。
2. `S_t/P_t/A_t/R_t/S_{t+1}` 显式绑定。
3. terminal、ANSWER、max-step、exception。
4. score/reason 与 teardown。

### Phase 4：Environment evidence 与 concurrency

1. GUI transport evidence。
2. MCP/user result evidence。
3. joblib threading stress。
4. auto-retry 不覆盖。

### Phase 5：Server smoke

1. feature off parity。
2. 先 Seed/Planner/MemGUI 三种高风险路径。
3. 九个 adapter各 2–3 个短任务。
4. 运行 integrity checker和 request reconstruction。

每个 phase 单独提交，测试通过后再进入下一 phase。

---

## 11. 原始数据可支持与不可支持的后续工作

完成本规格后，无需重跑环境即可：

- 重新定义 history error taxonomy；
- 重新切分 claim；
- 重建每个 target request 的真实 history exposure；
- 改变 weak/strong uptake 判定；
- 更换自动 verifier 或 judge prompt；
- 重做人类标注和统计；
- 对同一个静态 request 做离线 counterfactual model replay。

仅凭这些数据不能：

- 从任意中间 step恢复可交互 emulator状态；
- 执行另一个 action 后得到真实分支结果；
- 证明某条历史对失败有反事实因果作用。

若未来需要 live branch replay，必须另外做 emulator/backend checkpoint；不属于当前 collector。

---

## 12. 实现硬约束摘要

1. Raw collection 与 evaluation code物理分离。
2. Raw events append-only、schema-versioned、可校验。
3. Feature flag默认关闭；关闭时零文件、零语义变化。
4. 不修改发给模型的 request object。
5. 不修改返回给 adapter 的 response/chunks。
6. 不增加模型或环境调用。
7. 保存实际 SDK payload，不保存猜测的 prompt。
8. 保存所有可见 retry/失败，不只最终成功输出。
9. 保存 request 中实际图片，而不只是环境截图。
10. runner立即保存 action 后截图，包括最后一步。
11. UIINS grounder单独标记。
12. 并发任务和 auto-retry绝不覆盖或串线。
13. Collector不产生任何历史正确性标签。
14. API key、Authorization、cookie不得进入 raw dataset。
15. 未通过 `TEST_AND_ACCEPTANCE.md` 的 DoD 前，不启动正式数据采集。
