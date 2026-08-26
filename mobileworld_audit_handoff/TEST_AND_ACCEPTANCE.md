# Runtime Audit Collector：测试、验收与 Definition of Done

## 0. 验收原则

Collector 的价值取决于两个条件同时成立：

1. 记录足够完整，未来可更换错误分类和评测方法而无需重跑任务；
2. 记录过程没有改变 agent 原本收到的输入、返回的输出或执行行为。

只验证“生成了 JSON 文件”远远不够。以下测试是正式采集前的硬门槛。

---

## 1. 测试分层

```text
Unit tests
  schema / serializer / blob / writer / context

Component tests
  Base non-stream / stream / retry / UIINS

Runner integration tests
  transitions / terminal / max-step / score / failure

Concurrency tests
  multiple task attempts / shared blob store / context isolation

Server smoke tests
  real MobileWorld environment + representative agents

Nine-agent compatibility smoke
  all registered adapters, small task sample only
```

测试目录建议：

```text
tests/runtime/audit/
├── test_schema.py
├── test_blob_store.py
├── test_serializer.py
├── test_base_nonstream.py
├── test_base_stream.py
├── test_uiins_capture.py
├── test_runner_transitions.py
├── test_terminal_paths.py
├── test_concurrency.py
├── test_retries.py
├── test_integrity.py
└── test_feature_off_parity.py
```

---

## 2. Feature-off parity

这是最高优先级测试。

### 2.1 无副作用

当 `enable_audit=false`：

- audit目录不存在；
- model client收到的 payload与修改前 golden payload深度相等；
- agent返回的 prediction/action相等；
- env收到的 action相等；
- exception类型和传播相等；
- retry计数、sleep调用、token usage相等；
- stream chunk对象顺序与身份不被替换；
- 现有 `TrajLogger` 文件格式不变。

### 2.2 推荐方法

在修改代码前用 fake OpenAI client/fake env保存 baseline fixtures。修改后分别在 flag off/on运行同一 deterministic fixture：

```text
off vs baseline
  必须完全一致

on vs baseline
  model payload、return、action、env call必须一致；只允许额外本地audit I/O和计时差异
```

禁止用真实非确定模型输出来判断 parity。

---

## 3. Request/response 完整性

### 3.1 Non-stream request

构造包含以下字段的 fake request：

```text
system/user/assistant messages
多个 content blocks
data URL image
tools + tool_choice
temperature/top_p
max_tokens转换
extra_body
provider-specific unknown field
```

断言：

- 采集的是实际 SDK调用payload；
- message顺序、role、block顺序不变；
- authoritative artifact graph rehydration hash等于采集前 SDK payload canonical hash；
- recorder外置图片没有修改原 messages；
- API key/header没有写入事件。

### 3.2 Model-specific kwargs mutation

分别测试：

```text
claude model path
gpt/o1 max_completion_tokens path
kimi extra_body + reasoning_content path
ordinary model path
```

Raw request必须反映 SDK实际收到的最终参数，而不是 helper入口参数。

### 3.3 Non-stream response

fake response包含：

```text
content
reasoning_content
usage + cached tokens
finish_reason
provider extension
None fields
```

断言 raw response可重建，normalized convenience fields正确，返回给 adapter的字符串完全不变。

### 3.4 Retry

模拟：

1. 第一次抛 timeout、第二次成功；
2. max_tokens错误触发立即参数转换重试；
3. 所有次数失败；
4. adapter外层再次调用 Base helper；
5. task-level device unhealthy retry。

断言：

- 每个 application-visible SDK invocation 都有 request 和 success/failure 闭合事件；
- visible attempt 有序；SDK 内部透明 HTTP retries 只记录配置，不伪造事件；
- task retry生成新 task_run_id；
- 旧 attempt不被 `reset_traj()` 覆盖；
- 不把失败 response从 raw中删除。

---

## 4. Streaming 测试

Seed streaming必须单独验收。

### 4.1 正常流

fake stream至少包含：

```text
reasoning delta
content delta
empty choices chunk
usage-bearing final chunk
```

断言：

- consumer收到同样数量、同样顺序、同样内容的 chunk；
- collector未提前消费 generator；
- 每个 chunk都可回链 model_call_id/request_id；
- usage和completion事件存在；
- 离线拼接结果与 Seed最终 `P_t` 一致。

### 4.2 中途异常

fake generator在第 N 个 chunk后抛异常：

- 前 N 个 chunk均保存；
- `model_attempt_failed` 存在且引用partial chunks；
- 原异常类型原样抛给 adapter；
- Seed外层重试产生新 call/attempt记录；
- 失败流不伪装成 completed。

### 4.3 Consumer提前停止

显式只消费部分 chunks后关闭 generator，断言该 SDK invocation 以唯一的 `model_response(stream_state="consumer_abandoned")` 闭合并引用所有已观察 chunks。不要将未消费内容虚构为 provider response。

---

## 5. 图片测试

### 5.1 Request image

- PNG/JPEG data URL各一例；
- 重复图片只写一个 decoded blob；
- canonical base64可逐字重建；
- non-canonical base64走 encoded ASCII fallback；
- MIME、尺寸、byte length、hash正确；
- HTTP image URL原样保存且不触发下载。

### 5.2 Observation image

- RGB、RGBA、灰度 PIL image；
- 保存后 pixel hash一致；
- width/height/mode一致；
- 原 PIL object未被 resize/convert/mutate。

### 5.3 Agent resize差异

构造 observation原图与 adapter request resize图不同的 fixture，断言两者拥有不同 blob/ref，不能把 `S_t` 错当作 model request image。

---

## 6. Runner transition 测试矩阵

每种路径都断言事件顺序和关联 ID。

| 路径 | 是否执行 env | 是否要求 post-state | 预期 |
|---|---:|---:|---|
| 普通 click/scroll/input | 是 | 是 | `S_t -> A_t -> S_{t+1}` 完整 |
| `ANSWER` | 是 | 是 | 执行后终止，post-state仍保存 |
| `FINISHED` | 否 | 否 | `not_executed/terminal_action` |
| `UNKNOWN` | 否 | 否 | 同上 |
| `ENV_FAIL` | 否 | 否 | 同上 |
| prediction is `None` | 否 | 否 | `agent_decision(parse_outcome=returned_prediction_none)` + `transition_not_executed(reason=prediction_none)` |
| `agent.predict()`异常 | 否 | 否 | `agent_decision(parse_outcome=raised)` + `transition_not_executed(reason=prediction_exception)`，task随后按原路径结束/抛出 |
| env execute异常 | 尝试 | 无或部分 | `transition_failed` 含 exception |
| max-step最后一步 | 是 | 是 | break前保存最后post-state |
| MCP action | 是 | 是 | tool result与新截图保存 |
| ask-user | 是 | 是 | user response与新截图保存 |

### 6.1 对齐不依赖相邻行

打乱JSONL读取顺序后，使用 IDs仍能重建同一 DecisionRecord。若只能依赖“下一行是post-state”，测试失败。

### 6.2 Score与失败

- `task_ended.environment_evaluation` 保存 float score和完整 reason；
- evaluator异常明确标记，不默认为 score 0；
- teardown response/exception可审计；
- `task_ended.runtime_status` 只使用契约值 `completed/aborted/crashed`；collection 是否完整由 `capture_complete` 与 `missing_artifacts` 独立表达，任务成败由 score/reason 独立表达。

---

## 7. UIINS / Planner 验收

使用 fake planner与fake UIINS：

- 同一 step至少有一个 `actor` call和一个 `grounder` call；
- 两者 request不混合；
- grounder使用 executor model metadata；
- grounder screenshot是其实际resize后的 request image；
- UIINS显式3次 retry均可观察；
- SDK `max_retries=3` 写入配置，但不能伪造内部HTTP attempt；
- 最终 A_t与 planner/executor fallback路径一致。

Planner无需grounder的 action类型也要测试，确保不会虚构 grounder事件。

---

## 8. 并发与 crash safety

### 8.1 Thread isolation

并发运行至少 20 个 fake task attempts，复用少量线程和environment：

- event中的 task/step不串线；
- ContextVar在finally中reset；
- thread ID复用不导致目录覆盖；
- `seq`在各 task stream内严格递增；
- shared blob dedup正确。

### 8.2 Atomicity

模拟：

- blob写到一半进程退出；
- JSONL尾部半行；
- 两线程同时写相同hash；
- 磁盘满/permission error。

断言真实runtime始终让原agent/env路径继续并标记不完整，integrity checker能在运行后报告故障，不静默把不完整run视为可评测。CI使用fault-injection test断言checker拒绝这些run；不得通过runtime collector异常来中止task或smoke。

---

## 9. Raw integrity checker

正式run结束后必须自动或显式验证：

```text
manifest可读且commit/config完整
所有JSONL完整行可解析
schema version支持
event_id唯一
seq约束满足
所有引用ID存在
所有blob存在且SHA-256匹配
每个request attempt有success/failure终态
每个已执行action有execution终态
每个completed step有pre/decision/post或合法terminal原因
task score/completion状态一致
无禁止的credential key
```

输出 machine-readable `integrity_report.json`：

```json
{
  "valid": true,
  "errors": [],
  "warnings": [],
  "counts": {},
  "checked_at": "...",
  "checker_version": "..."
}
```

只有 `valid=true` 且 collection policy符合正式研究配置的run才能进入离线评测。

---

## 10. 九 agent server smoke 验收

### 10.1 先跑三条高风险路径

1. `seed_agent`：streaming、raw history、image window。
2. `planner_executor`：actor + UIINS direct grounder。
3. `memgui`：folded summaries/latest interaction/memory。

每个 1–2 个短任务，`max_concurrency=1`、`auto_retry=0`，人工检查 exact request。

### 10.2 再覆盖九个 adapter

每个 2–3 个短任务，确认：

```text
seed/general/MAI/planner      raw replay family
qwen/UI-Venus                flat history family
Gelab                         rolling summary
GUI-OWL                       hybrid collapsed
MemGUI                        structured folding
```

验收不是成功率，而是：

- 每个 actor call都采集；
- prompt表示与源码预期一致；
- 当前/旧图片数量可从 exact request验证；
- response/action/transition完整；
- integrity checker通过。

### 10.3 Concurrency smoke

九 agent单线程通过后，选一个稳定agent用 2–5 个environment并发短任务，检查串线与覆盖。

---

## 11. 必须运行的命令

```bash
uv sync --extra dev

uv run ruff check \
  src/mobile_world/runtime/audit \
  tests/runtime/audit

uv run pytest tests/runtime/audit -q
```

若对既有文件做了修改，还需对修改文件运行 ruff。项目现有全仓测试/类型检查若有与本次无关的历史失败，必须分别报告：

```text
command
exit code
本次相关失败
已存在/无关失败
```

不能用“仓库原本有问题”跳过新增测试。

---

## 12. Runtime Collector Definition of Done

以下全部满足才算完成：

- [ ] 工作基于 `0dcd098`，或差异已记录并重新定位。
- [ ] 九个内置 actor调用经共同hook覆盖。
- [ ] UIINS direct grounder调用单独覆盖。
- [ ] feature flag默认关闭。
- [ ] flag关闭时无audit文件、无payload/response/action变化。
- [ ] 实际SDK request可无损重建并校验hash。
- [ ] non-stream raw response、usage、provider扩展被保存。
- [ ] Seed stream chunks、正常结束与中途异常被保存。
- [ ] 所有可见retry和失败attempt被保存。
- [ ] request实际图片与observation图片分开保存。
- [ ] 图片外置不修改发送给模型的messages。
- [ ] `S_t/P_t/A_t/R_t/S_{t+1}` 用IDs显式关联。
- [ ] max-step最后post-state不丢失。
- [ ] terminal action有明确not-executed记录。
- [ ] GUI transport、MCP result和user response按可见程度保存。
- [ ] task score/reason/config/commit可回链。
- [ ] auto-retry不覆盖旧attempt。
- [ ] thread并发无串线。
- [ ] raw append-only且crash/incomplete可检测。
- [ ] credential扫描通过。
- [ ] collection代码无任何错误标签、judge或prompt intervention。
- [ ] unit/component/integration/concurrency测试通过。
- [ ] 三agent高风险smoke通过。
- [ ] 九agent小规模compatibility smoke通过。
- [ ] raw integrity report为valid。
- [ ] `STATUS.md` 已更新实际完成项、测试结果和已知限制。

未完成的项目必须留在 STATUS，不得以“后续优化”隐藏。
