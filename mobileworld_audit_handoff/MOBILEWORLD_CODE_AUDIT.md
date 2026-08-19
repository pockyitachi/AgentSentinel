# MobileWorld `0dcd098`：Agent History 与 Logger 接入审计

## 0. 审计范围和结论

审计对象：官方 MobileWorld `main` commit

```text
0dcd0980eac64d76f498f93568a1ec0594b743c4
commit date: 2026-08-04
```

所有路径均相对于 MobileWorld repository root。

核心结论：

> 9 个内置 registered adapters 都会将某种过去信息送入后续模型调用，但它们不是同一套 history 实现。4 个回放 raw assistant responses，2 个扁平化进展，1 个 rolling summary，1 个 hybrid collapse，1 个 structured folding。BaseAgent 没有统一 history schema；因此 motivation audit 必须捕获每次真正发送给 provider 的最终 request，而不能只读取统一的 `history` 字段。

另一个跨实现的共同风险是：历史文字/结论/summary 往往比产生它们时的旧 GUI 图片保留更久，导致模型接触到缺乏原始视觉证据的旧 claim。

---

## 1. 权威注册表

文件：`src/mobile_world/agents/registry.py`

- imports：约 lines 12–21；
- `AGENT_CONFIGS`：约 lines 24–52；
- 动态 `.py` agent loading：约 lines 55–103；
- `create_agent()`：约 lines 106–142。

9 个内置注册名：

| Registry name | Class | Implementation |
|---|---|---|
| `qwen3vl` | `Qwen3VLAgentMCP` | `src/mobile_world/agents/implementations/qwen3vl.py` |
| `planner_executor` | `PlannerExecutorAgentMCP` | `src/mobile_world/agents/implementations/planner_executor.py` |
| `mai_ui_agent` | `MAIUINaivigationAgent` | `src/mobile_world/agents/implementations/mai_ui_agent.py` |
| `general_e2e` | `GeneralE2EAgentMCP` | `src/mobile_world/agents/implementations/general_e2e_agent.py` |
| `seed_agent` | `SeedAgent` | `src/mobile_world/agents/implementations/seed_agent.py` |
| `gelab_agent` | `GelabAgent` | `src/mobile_world/agents/implementations/gelab_agent.py` |
| `ui_venus_agent` | `VenusNaviAgent` | `src/mobile_world/agents/implementations/ui_venus_agent.py` |
| `gui_owl_1_5` | `GUIOWL15AgentMCP` | `src/mobile_world/agents/implementations/gui_owl_1_5.py` |
| `memgui` | `MemGUIAgent` | `src/mobile_world/agents/implementations/memgui_agent.py` |

`uiins` 不在 registry；它是 `planner_executor` 可调用的内部 grounding model，定义于 `src/mobile_world/agents/grounding/uiins.py`。它直接调用自己的 `OpenAI` client（约 lines 58–63、140–151），会绕过 `BaseAgent.openai_chat_completions_create()`，collector 必须单独 hook 并标注 `call_role=grounder`。

---

## 2. MobileWorld 当前 runner 和 logger 实际保存什么

### 2.1 Runner 时序

文件：`src/mobile_world/core/runner.py`

`_execute_single_task()` 的核心流程：

- 初始化 task 和 `obs`：lines 50–56；
- 把当前 `obs.screenshot/tool_call/ask_user_response` 交给 `agent.predict()`：lines 58–69；
- 立即用当前 `obs` 记录 trajectory：lines 70–78；
- 再执行 parsed action 并把返回值覆盖给 `obs`：lines 86–96；
- max-step 终止检查：lines 99–101；
- 最终 score/reason：lines 103–105。

所以运行时确实存在：

```text
S_t → P_t → A_t → execute_action(A_t) → S_{t+1}
```

但当前日志先写 `S_t/P_t/A_t`，随后才执行动作。`S_{t+1}` 通常只在下一轮作为 step `t+1` 的 pre screenshot 被保存，没有显式 transition ID；若 action 后马上达到 max-step，则新 `S_{t+1}` 不会进入下一轮 trajectory，可能完全丢失。

### 2.2 当前 TrajLogger

文件：`src/mobile_world/runtime/utils/trajectory_logger.py`

`TrajLogger.log_traj()`（约 lines 101–157）保存：

- task goal、step；
- `prediction`；
- parsed action；
- 当前 observation 的 ask-user/tool result；
- 当前 screenshot；
- token usage。

它不保存：

- adapter 裁剪/折叠后的完整 model request；
- provider raw response object、retry attempts 或 streaming chunks；
- action 与 post-state 的显式关联；
- 最后 action 后必然可取回的 post screenshot；
- provider request 中实际使用的处理后历史图片集合。

### 2.3 环境 transition 暴露情况

文件：`src/mobile_world/runtime/client.py`

普通 GUI action：

- POST `/step`：约 lines 148–164；
- status/body 只进入 debug log；
- action 后调用 `get_screenshot(wait_to_stabilize=True)`：line 169；
- 返回包含新 screenshot、可能 ask-user response 的 `Observation`：lines 170–179。

MCP action：

- `AndroidMCPEnvClient.execute_action()`：约 lines 384–399；
- tool result 进入 `Observation.tool_call`，并同时获取 action 后 screenshot。

`Observation` 定义于 `src/mobile_world/runtime/utils/models.py:483–487`，字段为 screenshot、accessibility tree、ask-user response、tool call。当前 accessibility tree 实际不支持（`client.py:134–146`）。

因此无需重做环境观测；新 collector 主要是把 runner 已经拥有的对象显式关联并持久化。HTTP response 若要结构化保存，需要在 client/runner 边界额外暴露，但 transport success 不能被当作语义 success。

---

## 3. BaseAgent 的公共模型调用边界

文件：`src/mobile_world/agents/base.py`

`BaseAgent`（约 lines 15–54）只定义 initialize/predict/done/reset，没有：

```text
get_history()
set_history()
filter_history()
build_messages()
```

`openai_chat_completions_create()` 位于约 lines 76–137：

- streaming branch：lines 84–94；
- non-stream retry loop：lines 95–137；
- 某些 model 会在调用前改写 kwargs，如 `max_tokens → max_completion_tokens`；
- non-stream 最终只返回 stripped content string，provider raw response object 被丢弃；
- stream 使用 `_wrap_stream_with_usage_logging()`，Seed 在 adapter 内消费 chunks 并拼接 reasoning/content。

9 个顶层 builtin actor adapters 都通过这个 helper 发 actor call，因此它是捕获最终 request/response 的主要公共边界。但要做到真正 lossless：

1. 每个实际 provider attempt 前，在所有参数适配完成后记录 request；
2. 保存 provider raw response，而不只保存 helper 返回的 content；
3. streaming 保存所有 chunks、usage 和最终组合；
4. 失败 attempt/exception 也写 event；
5. request capture 不能原地修改 `messages/kwargs`；
6. Planner 的 UIINS direct client 需额外 hook。

这只是统一 capture boundary，不是统一 history manipulation boundary。到达 Base helper 时，history 已经被各 adapter replay、flatten、summarize 或 fold；这正是 collector 应保存的“模型真实输入”。

---

## 4. 九个 adapter 的 history 表示

### 总表

| Agent | 后续 prompt 中的过去信息 | 默认视觉历史 | Family |
|---|---|---:|---|
| Seed | 所有旧 assistant response，拆为 `reasoning_content + content` | 最近 3 张，包含当前图 | Raw replay |
| General E2E | 所有旧 raw assistant response | 最近 3 张；旧图变占位文字 | Raw replay |
| MAI-UI | 所有旧 raw assistant response | 最近 3 张；更旧 image messages 删除 | Raw replay |
| Planner-Executor | 所有旧 planner response；grounder history 不回放到 planner | 最近 3 张；旧图变占位文字 | Raw replay |
| Qwen3VL | 所有旧 conclusion 拼成单个 `Step 1...` progress text | 只有当前图 | Flat progress |
| UI-Venus | 旧 `<think> + <action>` 拼成 previous actions | 只有当前图 | Flat history |
| Gelab | 上一条 action 中的一个 rolling summary | 只有当前图 | Rolling summary |
| GUI-OWL 1.5 | 旧 conclusions 折成文字；最近窗口保留 raw message pairs | 默认只有当前图 | Hybrid collapse |
| MemGUI | state summaries + latest interaction + memory state | 只有当前图 | Structured folding |

“最近 3 张”都包括当前 `S_t`，不是 3 个完整 previous steps。文字与图片窗口并不一致。

---

## 5. 逐 adapter 审计

### 5.1 Seed (`seed_agent`)

文件：`src/mobile_world/agents/implementations/seed_agent.py`

关键位置：

- default `history_n=3`：约 lines 145–173；
- state arrays：lines 183–185；
- `_get_user_message()`：lines 234–263；
- `_build_messages()`：lines 265–310；
- replay old responses：lines 283–299；
- reverse-scan image pruning：lines 300–308；
- response 写入 history：约 line 430。

行为：所有旧 `history_responses` 都作为 assistant text 再次发送；thinking 和 visible content 被拆成 `reasoning_content/content`。只删除超过最近 3 张的 image messages，因此可出现“很久以前的文字仍在，但产生它的 GUI 图已不在”。

若 observation 含 tool result 或 ask-user response，`_get_user_message()` 使用文字分支而不是 screenshot 分支；该 observation 的 screenshot 虽保存在内部 tuple 中，却不进入该次 prompt。

Collector implication：必须保存 Seed streaming chunks、拼接后的 `P_t`、Base helper 实际 request，以及 adapter 内所有未发送 image refs（可选 provenance snapshot）。

### 5.2 General E2E (`general_e2e`)

文件：`src/mobile_world/agents/implementations/general_e2e_agent.py`

关键位置：

- `history_n_images=3`：约 lines 246–251；
- `_get_user_message()`：lines 260–295；
- `_hide_history_images()`：lines 297–321；
- history/message assembly：lines 352–391；
- model call：lines 401–408；
- response append：line 447。

行为：所有旧 raw responses 都被 replay；最近 3 张图片保留，更旧图片 content 被替换成 literal `(Previous turn, screen not shown)`，不是删除旧 assistant text。

tool result/ask-user response 与 screenshot 是 `if/elif/else`，有 result 时不把同 observation screenshot 发给模型。

### 5.3 MAI-UI (`mai_ui_agent`)

文件：`src/mobile_world/agents/implementations/mai_ui_agent.py`

关键位置：

- default `history_n=3`：约 lines 133–159；
- `_get_user_message()`：lines 174–198；
- `_hide_history_images()`：lines 200–226；
- `_build_messages()`：lines 228–260；
- predict/model call：lines 262–293；
- response append：line 312。

行为：所有旧 assistant responses 保留；超出最近 3 张的 user image messages 被整个删除。与 General E2E 不同，它不留下 screen-not-shown 占位。tool/user result 同样会替代该 observation screenshot。

### 5.4 Planner-Executor (`planner_executor`)

文件：`src/mobile_world/agents/implementations/planner_executor.py`

关键位置：

- executor initialization：约 lines 149–174；
- planner `history_n_images=3` 和 arrays：lines 176–180；
- old image placeholder：lines 217–235；
- planner message assembly：lines 251–289；
- planner model call：lines 296–303；
- grounder call：lines 341–395；
- planner response append：lines 397–400。

行为：顶层 planner 以 raw replay 方式保留所有旧 planner responses，只保留最近 3 张图片。坐标类 action 会为当前 step 调内部 executor/grounder；planner history 保存的是 plan，不是 executor 的 raw grounding response。

Collector implication：一个 decision step 可有多个 model calls。必须用 `call_role=actor|grounder`、`parent_request_id`、attempt/sequence 区分，不能把 UIINS 输出误认为新的环境 step。

### 5.5 Qwen3VL (`qwen3vl`)

文件：`src/mobile_world/agents/implementations/qwen3vl.py`

关键位置：

- state arrays：约 lines 206–210；
- append current screenshot：line 222；
- tool/user result 追加到上一 conclusion：lines 225–232；
- conclusions flattening：lines 233–241；
- single user request + current screenshot：lines 243–266；
- model call：lines 274–280；
- raw response/conclusion append：lines 304–306。

行为：旧 raw responses/thoughts 不直接 replay；所有旧 conclusions 被拼成一个 `Step 1: ...; Step 2: ...` progress text，并与当前 screenshot 放在同一 user message。旧图片不进入 request。

研究风险：conclusion/action language 是 action 执行前由 actor 生成，却在后续 prompt 中作为 task progress 呈现，可能将 intention 升格为已完成事实。tool/user result 后续会被追加进上一 conclusion，但 provenance 已被压扁。

### 5.6 UI-Venus (`ui_venus_agent`)

文件：`src/mobile_world/agents/implementations/ui_venus_agent.py`

关键位置：

- `history_length=0`：lines 246–274；
- `_build_query()`：lines 288–300；
- current-only image request：lines 324–350；
- parse failure 也 append history：lines 358–390；
- successful history append：lines 392–401。

行为：历史被渲染成 `Step i: <think>...</think><action>...</action>`，旧 screenshot、conclusion 和 status 不进入下一 prompt，仅当前 screenshot 进入。

明确实现陷阱：默认 `history_length=0`，但代码使用 `self.history[-self.history_length:]`；Python 中 `-0 == 0`，所以实际得到全部 history，而不是空历史。parse-failed `StepData(status="failed")` 也进入同一个 history，`_build_query()` 没有按 status 过滤。

### 5.7 Gelab (`gelab_agent`)

文件：`src/mobile_world/agents/implementations/gelab_agent.py`

关键位置：

- action/environment state：约 lines 190–192；
- last action summary selection：lines 199–215；
- summary + current screenshot request：lines 216–231；
- current environment append/model call：lines 233–257；
- parsed action append：line 272。

行为：只把 `self.actions[-1]["summary"]` 作为一个 rolling summary 注入；当前 request 只有当前 screenshot。summary 是模型在生成当前 action 时同一次 response 中输出，早于该 action 的环境执行，因此不能当作 post-state 已验证事实。

另一个边界：当 user comment 存在、但 summary 为空时，代码返回 `暂无历史操作`，没有把 user comment 拼进去（lines 206–212）。

### 5.8 GUI-OWL 1.5 (`gui_owl_1_5`)

文件：`src/mobile_world/agents/implementations/gui_owl_1_5.py`

关键位置：

- default `history_n=1`：lines 235–269；
- `_format_previous_steps()`：lines 306–339；
- current observation append：lines 354–369；
- raw-vs-text window calculation：lines 379–388；
- collapsed history render：lines 398–431；
- recent interleaved messages：lines 433–456；
- response/conclusion state update：约 lines 502–505。

行为：`history_n` 包含当前 observation，因此默认 1 时 `keep_as_messages=0`；所有完成的旧 turns 都压成 `StepN: <conclusion> Tool response: ...` 文本，只有当前 screenshot 保留。若配置增大，最近 `history_n-1` turns 才以 raw assistant/user image pair 保留。

明确错配风险：`_format_previous_steps()` 把 `conclusions[i]` 与 `history_user_content[i]` 的 tool/user result 配在一起。但 action `i` 的执行结果实际随下一 observation `i+1` 到达；因此 collapsed result 存在 off-by-one，对应错 step，最新 action result 也可能未进入其 step summary。这属于 `RESULT_MISALIGNMENT` 的直接代码证据。

### 5.9 MemGUI (`memgui`)

文件：`src/mobile_world/agents/implementations/memgui_agent.py`

关键位置：

- maintained state schema：lines 230–275；
- state/latest/memory formatting：lines 281–312；
- destructive overlap replacement：lines 318–347；
- request build with current image：lines 388–422；
- model call：lines 428–445；
- apply/auto folding：lines 458–475；
- latest interaction state update：lines 497–510。

行为：下一 request 包含三个 actor-managed text blocks：

1. `state_summaries`（folded action history）；
2. `latest_interaction`（最近 UI observation/action intent/action summary）；
3. `memory_state`（folded UI state）；

再加当前 screenshot。旧 raw screenshots/responses 不作为 history replay 进入 request。

与其他 adapter 相比，MemGUI 在新 `S_t` 已到达后可以让 actor 为过去 step 输出 folding directive，因此它具有有限的 next-screen-conditioned reflection。但不能将其等同于本项目未来的独立 Sentinel：

- folding 仍由同一个 actor 生成，没有独立 verifier；
- 没有 `supported/refuted/unknown` verdict、evidence citation 或 task rubric；
- overlapping summaries 会被破坏性替换；
- parser 允许 fold range 的 `end_step <= current_step`，而 current step 的 action 在 request 时尚未执行；
- 如果缺少 directive，auto-fold 使用本次 response 的 current `action_intent` 去总结 `current_step-1`（lines 466–474），存在时序/语义错标风险。

Collector 必须同时保存实际 request 和可选 internal snapshot，才能区分“模型看见的 folded history”与“folding 前曾存在但已不再可见的 raw state”。

---

## 6. 对 collector 设计的直接要求

从上述实现可推出以下不可省略的 capture points：

### 6.1 Model-call events

主要 hook：`BaseAgent.openai_chat_completions_create()`。每次 application-visible SDK invocation 按 `EVENT_CONTRACT_V1.md` 写：

```text
model_request
model_stream_chunk*         # streaming only
model_response              # successful/abandoned terminal
model_attempt_failed        # failed terminal; 与 model_response 二选一
```

额外 hook：`src/mobile_world/agents/grounding/uiins.py` 的 direct OpenAI call。

事件需要 `run_id/task_run_id/step_id/model_call_id/request_id/retry_group_id/call_role/attempt_index/seq`，否则 Planner 的多个 calls 和 retry 无法对齐。

### 6.2 Runner transition events

在 runner 中把 action 执行前后对象关联：

```text
step_started(S_t)
agent_decision(P_t, A_t, source model-call IDs)
action_execution_started(A_t)
transition_completed(R_t, S_{t+1})
# 或 transition_failed / transition_not_executed
```

不要只依赖 step `t+1` 的 screenshot 来反推 post-state。terminal action、exception 和 max-step 都要有显式状态。

### 6.3 Request images vs environment screenshots

至少区分：

```text
environment_observation_blob   # runner/client 得到的 S_t
request_image_blob             # adapter 处理后实际放进 I_t 的图
```

二者可能 hash 相同，也可能因 resize/encode 不同而不同。必须记录 request 中每个 image part 的 message/content index 和 blob ref，才能重建最终输入。

### 6.4 Adapter state snapshots

内部 snapshot 应通过 adapter-specific serializers 或严格白名单实现，不能把任意 Python object/pickle 当作长期 schema。标明：

```text
seen_by_model = false
adapter_type
state_schema_version
captured_before_or_after_render
```

它用于 provenance research，不用于定义 actual exposure；actual exposure 只以保存的 final request 为准。

---

## 7. 本次审计对研究 motivation 的支持与边界

源码可以支持：

- 所有 9 个内置 agent 都会在后续 decision 中使用某种 past information；
- history 与视觉 evidence 的保留方式普遍不对称；
- summary/folding agent 也可能引入 corruption/misalignment，而不只是 raw replay agent；
- MobileWorld 当前日志不足以无损、直接核验每次真实 exposure；
- 因此实现 lossless audit collector 是必要的研究基础设施。

源码本身不能支持：

- 某种错误实际发生了多少次；
- 模型是否真的采用某条错误 history；
- 错误 history 是否导致 task failure；
- Sentinel 是否会改善下一步 action 或成功率。

这些必须由新 collector 的真实 server runs 和独立 offline evaluation 回答。
