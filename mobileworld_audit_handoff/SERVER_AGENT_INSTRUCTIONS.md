# 给服务器端 Coding Agent 的工作指令

## 1. 你的任务

你需要在服务器 clone 的 AgentSentinel monorepo 内，对其中的 `MobileWorld/` 实现 Runtime Audit Collector，并随后实现独立、版本化的 Offline History Audit pipeline。不要使用服务器上其他既有 MobileWorld checkout。

当前第一优先级只有 Collector：

> event-sourced、lossless、label-free；记录实际完整模型输入/输出和 GUI transition；默认关闭；不改变任何 agent prompt或行为。

不要实现 Sentinel。不要提前写 classifier、rubric generator、`KEEP/DROP/REPLACE` 或在线 middleware。

---

## 2. 必读顺序

开始改代码前，完整阅读：

1. `README.md` 与 `AGENTS.md`：目录入口和强制范围。
2. `PROJECT_CONTEXT.md` 与 `DECISION_LOG.md`：研究背景、统一符号、locked decisions。
3. `MOBILEWORLD_CODE_AUDIT.md`：指定快照的真实调用链和九adapter差异。
4. `COLLECTOR_DESIGN.md`：总体架构。
5. `EVENT_CONTRACT_V1.md`：raw schema唯一权威；不得由其他示例另造event名/ID。
6. `IMPLEMENTATION_GUIDE.md`：具体hook和分阶段操作。
7. `TEST_AND_ACCEPTANCE.md`：测试与完成门槛。
8. `OFFLINE_EVALUATION_DESIGN.md`：未来版本化评测；现在不要把标签逻辑写进collector。
9. `STATUS.md`：当前完成度和服务器追加记录。

若这些文档与仓库实际代码冲突：

- 以仓库指定 commit的真实调用链为事实；
- 不得静默改变研究目标；
- 在 `STATUS.md` 的“Server findings / deviations”记录冲突、证据和选择；
- 若冲突会改变采集语义，暂停并请求项目 owner确认。

---

## 3. 仓库发现与 freeze

本交接目录和 `MobileWorld/` 位于同一个 AgentSentinel monorepo。先在 monorepo 根目录核验：

```bash
git rev-parse --show-toplevel
test -d MobileWorld/src/mobile_world
git status --short
```

然后进入同一次 checkout 中的 `MobileWorld/`，核对导入来源：

```bash
cd MobileWorld
test -f ../UPSTREAM.md
git -C .. remote -v
git -C .. status --short
```

`UPSTREAM.md` 中应记录的 MobileWorld 源码基线是：

```text
0dcd0980eac64d76f498f93568a1ec0594b743c4
```

注意：AgentSentinel 是新的顶层仓库，因此当前 `git rev-parse HEAD` 应是
AgentSentinel 自己的提交，**不会**等于上述 MobileWorld 上游 commit。

规则：

- 不要切换到服务器上其他 MobileWorld repo；本目录是唯一实现目标。
- 不要 reset、checkout或删除用户现有改动。
- 若 worktree dirty，先检查是否与目标文件重叠。
- 保存基线输出。
- 在 AgentSentinel 顶层仓库新建明确分支，例如 `audit/lossless-collector-v1`；若执行环境有自己的worktree策略，遵守该策略。
- 每个 phase小提交，不混入格式化全仓或无关refactor。

---

## 4. 实施流程（严格按阶段）

### Phase 0：只读核验与基线

必须核对符号，而不是只相信行号：

```text
src/mobile_world/agents/registry.py::AGENT_CONFIGS
src/mobile_world/agents/base.py::BaseAgent.openai_chat_completions_create
src/mobile_world/agents/base.py::_wrap_stream_with_usage_logging
src/mobile_world/agents/grounding/uiins.py::UIINSGroundingAgent.predict
src/mobile_world/core/runner.py::_execute_single_task
src/mobile_world/core/runner.py::_process_task_on_env
src/mobile_world/core/runner.py::run_agent_with_evaluation
src/mobile_world/runtime/client.py::AndroidEnvClient.execute_action
src/mobile_world/runtime/client.py::AndroidMCPEnvClient.execute_action
src/mobile_world/runtime/utils/trajectory_logger.py::TrajLogger
src/mobile_world/core/subcommands/eval.py
```

输出一个短的 implementation map并写入 STATUS。建立 deterministic fake client/env baseline tests，再改实现。

### Phase 1：基础组件

实现：

```text
schema version + event envelope
NullRecorder
per-task append-only recorder
ContextVar binding
content-addressed atomic blob store
serializer
integrity checker
```

先完成单元测试，不接入 model/runner。

### Phase 2：模型 I/O

按 `IMPLEMENTATION_GUIDE.md` 接入：

```text
Base non-stream
Base streaming
Base retries/errors
UIINS direct grounder
```

每次物理SDK调用前记录最终payload；response/chunks原样返回。不要 monkeypatch OpenAI全局类作为生产实现。

### Phase 3：Transition

在 runner显式记录：

```text
S_t -> P_t -> A_t -> R_t -> S_{t+1}
```

并用 request/decision/transition IDs连接 model events。覆盖 terminal、ANSWER、max-step、exception、score、teardown。

### Phase 4：Environment evidence与并发

记录现有 client函数内部已暴露的 GUI HTTP/MCP/user evidence，不增加额外环境调用，不改变 `Observation` API。完成 thread isolation、auto-retry preservation、crash safety测试。

### Phase 5：服务器 smoke

顺序：

```text
feature-off deterministic parity
Seed smoke
Planner+UIINS smoke
MemGUI smoke
九adapter小样本
并发小样本
integrity validation
```

不要一开始运行九个agent全量任务。

### Phase 6：Offline pipeline

只有 Collector DoD通过、raw样本已验证后，才开始 `OFFLINE_EVALUATION_DESIGN.md`。Offline code不得被 MobileWorld runtime import。

---

## 5. Coding hard constraints

违反任意一条都视为实现失败：

1. 不实现或模拟 Sentinel在线判断。
2. 不修改任何 adapter的 history含义。
3. 不修改发送给模型的 `messages`、kwargs、工具定义或顺序。
4. 不修改返回给 adapter的response text、chunk对象、exception。
5. 不增加模型调用、截图请求或environment action。
6. feature flag默认关闭；关闭时不得创建audit artifact。
7. Raw collector中禁止 `REFUTED`、`WEAK_NOISE`、`EXPLICIT_USE` 等标签逻辑。
8. 所有raw event append-only；禁止就地重写早期事件来“补字段”。使用后续关联事件。
9. 所有retry/失败保留；禁止只存最终成功调用。
10. Request实际图片与observation图片必须区分。
11. 图片外置只能修改序列化副本。
12. UIINS grounder不得漏采或与planner actor混淆。
13. 并发上下文不得用裸全局 `current_task/current_step`。
14. task retry不得覆盖旧attempt。
15. API key、Authorization、cookies、完整环境变量不得落盘。
16. 不删除或替换现有 `TrajLogger`。
17. 不进行全仓格式化、依赖大升级或无关重构。
18. 不用网络请求补下载 request中的HTTP图片。
19. 不把HTTP成功、像素变化或模型自述当作语义成功。
20. 不把观察性结果写成因果结论。

---

## 6. 设计决策默认值

除非仓库事实迫使改变，采用：

```text
raw schema: mobileworld.audit.event/v1（以 EVENT_CONTRACT_V1.md 为唯一权威）
feature flag: --enable-audit
output flag: --audit-log-root
formal failure policy: fail_open_with_incomplete_marker
stream chunks: stored
event IDs: UUIDv7 or ULID
blob identity: SHA-256
time: UTC wall clock + monotonic_ns
writer: per task attempt JSONL
concurrency context: ContextVar + explicit per-agent recorder where useful
```

存在小型实现选择时自行作出可逆决定并写入 STATUS，不要为了命名细节阻塞。会改变实验语义的选择必须询问 owner。

---

## 7. 测试纪律

在每个 phase结束运行相关测试。最终至少运行：

```bash
uv sync --extra dev
uv run ruff check src/mobile_world/runtime/audit tests/runtime/audit
uv run pytest tests/runtime/audit -q
```

修改过的既有文件也运行 ruff。若全仓测试存在历史失败，报告命令、exit code和失败归因；不得隐瞒。

真实server smoke必须保存：

```text
exact command（redact key）
git commit
agent/model/task
audit run_id
integrity report
人工检查结论
```

---

## 8. 每次交付报告格式

更新 `STATUS.md` 后，向 owner报告：

```text
Outcome
  本phase完成了什么

Files changed
  文件列表与职责

Behavior guarantee
  如何验证没有修改agent input/output/action

Tests
  命令、通过数、失败数

Artifacts
  sample run_id / integrity report

Known limitations
  明确未覆盖内容

Next phase
  下一步且不越界
```

不要只说“logger完成”。必须给可验证证据。

---

## 9. 何时必须停止并询问

遇到以下情况停止扩展并询问 owner：

- 指定commit不存在或仓库调用链已根本变化；
- 要完整采集必须改变模型payload/agent语义；
- provider response含无法安全处理的credentials；
- server环境要求把 collector与classifier混合；
- 需要破坏用户dirty changes；
- 需要增加新的外部服务或付费模型调用；
- 真实smoke发现flag on改变模型payload或action；
- raw schema无法表达某个adapter的实际调用。

普通命名、内部类划分、测试fixture细节不需要询问，按本规格推进并记录。

---

## 10. 完成定义

Collector只有在 `TEST_AND_ACCEPTANCE.md` 所有 Runtime DoD 条目满足后才完成。之后 raw collection 与 offline evaluation仍是两个独立deliverable。

最终目标不是“有更多日志”，而是：

> 任意研究者可以从 immutable raw artifacts重建模型实际看到的 request、实际产生的 response和可观察 GUI transition，并在不重跑 MobileWorld任务的情况下重新定义、重新标注和重新统计 wrong previous-step exposure与uptake。
