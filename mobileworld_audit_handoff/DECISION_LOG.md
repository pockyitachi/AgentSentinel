# MobileWorld History Audit：决策记录

## 使用规则

本文记录已经在讨论中明确的项目决策。服务器端实现者应把标记为 **Locked** 的条目视为当前阶段约束；若代码需要偏离，应先记录新 decision 并说明理由，而不是静默改变研究口径。

基准日期：2026-08-18  
代码基准：MobileWorld `main@0dcd0980eac64d76f498f93568a1ec0594b743c4`

---

## D-001 — 研究对象是 GUI agent 的 task-local pre-steps

**状态：Locked**

研究重点不是外部 experience knowledge、长期记忆或一般 RAG，而是 agent 在同一任务运行中，把之前 step 的 reasoning、action、conclusion、summary/folded history 重新注入后续模型决策时产生的风险。

**理由：** 这与用户最初 proposal 的核心直觉一致：错误或失效的 pre-step 可能 mislead 当前 GUI action。

---

## D-002 — 先验证 motivation，不先实现完整 Sentinel

**状态：Locked**

当前阶段只回答问题是否真实、普遍且严重。先收集真实 request/response/transition，再离线评测 invalid exposure、uptake 和 harm。

**当前不实现：** rubric、在线 verifier、history filtering、`KEEP/DROP/REPLACE`、纠错、replanning、reflection hint、成功率提升实验。

**理由：** 在问题强度尚未有无损证据前实现完整系统，可能围绕不稳固的定义过早优化。

---

## D-003 — 官方 MobileWorld 快照是唯一代码口径

**状态：Locked**

以官方 `main@0dcd098` 为准，不再把旧 `/Users/apigo/Desktop/Projects/QR-MW` fork 与新结果混用。该官方快照有 9 个内置 registered adapters：

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

registry 也支持从 `.py` 动态加载自定义 agent；“9”是内置数量，不是框架理论上限。`uiins` 是 planner 的内部 grounding component，不算第 10 个顶层 agent，但其模型调用必须被 collector 捕获并标为 `call_role=grounder`。

---

## D-004 — 不能把九个 adapter 描述成同一套 history

**状态：Locked**

九个 adapter 都把某种过去信息提供给下一步，但表示方式分为：

1. raw replay：Seed、General E2E、MAI-UI、Planner-Executor；
2. flat progress/history：Qwen3VL、UI-Venus；
3. rolling summary：Gelab；
4. hybrid collapsed history：GUI-OWL 1.5；
5. structured folding/memory：MemGUI。

因此 collector 必须记录最终 provider request，而不能只读取某个统一 `history` 数组；BaseAgent 当前也没有统一 history interface。

---

## D-005 — Collection 与 evaluation 在代码、数据和 schema 上分离

**状态：Locked**

Collection：event-sourced、lossless、label-free、append-only。  
Evaluation：离线、可版本化、可删除重建。

**禁止：** 在 raw event 中写入 `misleading=true`、rubric verdict、strong/weak 结论等随研究定义变化的标签。

**理由：** 第一次错误分类或阈值若不合理，后续应只重跑 evaluator，不重跑 MobileWorld 任务。

---

## D-006 — 捕获“真实最终 request”，不是 pretty print 或事后重建

**状态：Locked**

每个 model call 要记录 adapter 完成 history pruning/flattening/folding 后，真正交给 provider 的完整 request object：

- message 顺序与 role；
- text、image、tool content parts 的原始边界；
- system/task/tool schemas/results；
- reasoning-related fields（若 provider/request 支持）；
- model 与所有非秘密参数；
- call role、step、attempt 和 request ID。

不能只保存拼接文本或 `pretty_print_messages()` 输出，因为它会丢失图片位置、role、message boundary 和结构化字段。

---

## D-007 — 捕获完整 response 和所有 attempt

**状态：Locked**

需要保留 provider raw response、normalized content、finish reason、usage、tool calls 和异常。所有 retries 都是独立 events；不能只保存最后一次解析成功的 response。

对 Seed 等 streaming call，要保存 raw chunks 及最终拼接结果。解析失败的输出也是潜在 history pollution/agent failure 证据，不能丢。

**安全边界：** 不记录 API key、authorization header 或其他 secret。

---

## D-008 — GUI transition 显式保存为 `S_t → A_t → S_{t+1}`

**状态：Locked**

MobileWorld 运行时已经拥有构造可观察 transition 的大部分信息，但当前 TrajLogger 把 `S_t/P_t/A_t` 写在 step `t`，通常把 `S_{t+1}` 作为下一 step screenshot 保存，未显式建立关联；最后一步 post-state 还可能丢失。

新 collector 必须在 action 执行后立刻写出显式 transition event：

```text
pre observation S_t
request I_t / response P_t
parsed action A_t
execution/tool/user result R_t
post observation S_{t+1}
```

terminal action 未执行环境时，明确记录 `transition_not_executed` 和原因，不伪造 post-state。

---

## D-009 — 图片使用 content-addressed blob，但保持可逆

**状态：Locked**

不要在 JSONL 中重复巨大 base64。提取 request 中实际使用的图片 bytes，以 SHA-256 做 blob ID，保存 MIME type、原始 bytes/hash、request 内位置，并在 event 中用 typed reference 替换。

**关键约束：** 必须保存模型实际收到的处理后图片，而不只是环境原始 screenshot。adapter 可能 resize、compress、删除旧图或使用不同窗口。不得为节省空间重新编码成近似图片。

---

## D-010 — 可选保存 adapter 内部状态，但明确不是模型输入

**状态：Locked for schema support；各 adapter coverage 可分阶段完成**

最终 request 足以证明模型看到了什么；但 Qwen/Gelab/GUI-OWL/MemGUI 的 summary/folding provenance 可能无法仅从 request 还原。collector schema 应支持 `adapter_state_snapshot` event，标明 `seen_by_model=false`。

示例：

- Seed：`history_responses`、history image refs；
- Qwen：`conclusions`；
- Gelab：last rolling summary；
- GUI-OWL：collapsed/raw window boundary；
- MemGUI：`state_summaries`、`latest_interaction`、`memory_state`。

内部 state 不得与 actual request 混在一个字段里。

---

## D-011 — 强弱分层使用三个基础轴，四级 severity 只是派生值

**状态：Locked**

基础字段：

```text
history_status
uptake_evidence
downstream_effect
```

派生严重度：

```text
WEAK_NOISE       = invalid/stale history injected + NO_OBSERVED_UPTAKE
POSSIBLE_MISLEAD = BEHAVIOR_CONSISTENT, but no explicit textual reliance
STRONG_MISLEAD   = EXPLICIT_USE
EXPLICIT_HARM    = EXPLICIT_USE + observable harmful downstream effect
```

这里贯彻用户的核心判断：旧错误 history 被注入已构成弱噪音；明确将它作为行动前提才算强噪音。

---

## D-012 — Seed pilot 同时保留结果账本、broad count 与 strict count

**状态：Locked**

结果文件口径：117 个正式 task 目录，116 条非空 trajectory，115 条有 numeric score；其中 46 个 `score=1`、69 个 `score=0`、2 个 `no_result`。已评分成功率是 `46/115=40.00%`。

聊天中曾按用户要求记录 headline `48/117≈41.03%`；这里的 48 是 `46 score=1 + 2 no_result`，只能作为历史账本兼容值，**不得写成 48 个确认成功**。

失败任务的 broad pilot count 是 `14/69≈20.29%`。它由 5 个 strict、8 个 current-state-confounded、1 个 provenance-only task 构成；相同 numerator 对非空 trajectory 是 `14/116`，对全部目录是 `14/117`。严格、低 current-state-confound 的固定语料下界另报 `5/116=4.31%`（这 5 个也都属于 69 个失败任务）。

`14/69` 是 broad preliminary observed count，不是严格 prevalence 或因果结论；`5/116` 才是当前保守 strict observational lower bound。两者都可能漏掉隐式 uptake，也都需要用新 raw prompt/transition 重新审计。必须写清分母与 adjudication tier；不得写成“14 个失败由错误 history 导致”。

自然轨迹还存在 `S_t` 与 `H_t` 的 state confound：当前 GUI 和错误 history 同时影响 `P_t/A_t`。行为一致不能隔离 history effect；因果主张必须等待未来固定 `S_t`、只改变 history 的 paired replay。

---

## D-013 — 自然 trajectory 中的语言是 exposure/propagation/association，不是 causation

**状态：Locked**

当前可报告：

- invalid history exposure；
- possible/explicit uptake；
- explicit uptake accompanied by harmful action/failure；
- failed-task prevalence。

只有未来固定 `S_t`、task、model、decoding，只改变 history 的 paired replay，才可更接近因果结论。

---

## D-014 — MobileWorld 优先，AndroidControl 暂不作为主数据源

**状态：Locked for current phase**

MobileWorld 能观察真实 actor 生成 history、真实 prompt exposure 和在线 GUI transition，适合验证问题是否自然发生。AndroidControl 缺少同等真实 actor reasoning/prompt history，更适合后期构造 synthetic misleading history 做外部泛化，不适合作为当前 motivation 主证据。

---

## D-015 — 先让 collector 支持全部 9 个 adapter，再决定实验规模

**状态：Locked for engineering; experimental scale remains open**

collector 的完成条件是 9 个内置 adapter 的 actor calls 都可记录，Planner 的 UIINS grounder 也可区分。服务器上先做每个 adapter 2–3 个短任务的 smoke runs 验证 capture，不要求立即跑 9 agents × 全任务集。

自然问题主调查先用 5–6 个代表 agent 覆盖五类 history representation，建议：

```text
seed_agent        # raw replay baseline
general_e2e       # second raw replay implementation
qwen3vl           # flat progress
gelab_agent       # rolling summary
gui_owl_1_5       # hybrid collapse
memgui            # structured folding
```

`mai_ui_agent`、`planner_executor`、`ui_venus_agent` 在标注体系稳定后扩展。具体 task/seed 数仍属于实验计划，不应硬编码进 collector。

---

## D-016 — Shadow/zero-intervention 是行为要求

**状态：Locked**

```text
audit_enabled=false → 与上游行为一致
audit_enabled=true  → 只增加观察与持久化，不修改 messages、action 或环境状态
```

collector 不应改变 object identity/ordering、图片编码、retry、timing-sensitive agent semantics 或异常传播。实现时对可变 request/state 使用只读序列化或深拷贝，不能原地替换生产对象。

---

## D-017 — Raw 数据不可变，derived 标签显式版本化

**状态：Locked**

与 `COLLECTOR_DESIGN.md` 统一的建议布局：

```text
mobileworld_audit_data/raw/runs/<run_id>/manifest.start.json
mobileworld_audit_data/raw/runs/<run_id>/run.events.jsonl
mobileworld_audit_data/raw/runs/<run_id>/tasks/<task_run_id>/events.jsonl
mobileworld_audit_data/raw/runs/<run_id>/blobs/sha256/...
mobileworld_audit_data/raw/runs/<run_id>/manifest.final.json
mobileworld_audit_data/derived/<evaluation>/<version>/<evaluation_run_id>/...
```

raw 只追加；若发现 collector bug，用新 run/schema 修复并在 manifest 标记，不原地改写旧证据。derived 可随时从 raw 重建。

---

## D-018 — 完整 transition 是可观察证据，不是逐步语义真值

**状态：Locked**

HTTP 200 只说明 transport 成功；截图变化只说明视觉状态发生变化。两者都不能自动证明“商品已加入购物车”或“任务子目标完成”。collector 记录原始证据；离线 evaluator 才核验 claim，并在证据不足时标 `UNVERIFIABLE`。

---

## D-019 — 单一 AgentSentinel monorepo 是服务器唯一代码来源

**状态：Locked for repository topology**

项目发布到 `https://github.com/pockyitachi/AgentSentinel.git`。MobileWorld
固定快照作为 `AgentSentinel/MobileWorld/` 下可直接修改的源码纳入该仓库，
上游 `Tongyi-MAI/MobileWorld` 仅作为只读来源与许可证归属，不是 push 目标。

服务器应 clone AgentSentinel，并且只在同一次 checkout 的 `MobileWorld/`
内实现 collector；不得改用 `/shared/linqiang/MobileWorld` 或任何其他既有
MobileWorld clone。运行数据仍写到 repo 外的显式 data root。

---

## 尚未锁定、留给后续实验设计的问题

以下内容不要在 collector 中写死：

1. 自然运行采样多少 tasks/seeds；
2. 每个 history family 的代表 agent 数；
3. claim extraction 使用规则、LLM judge 还是混合方案；
4. 人工标注员数量与一致性指标；
5. `TRUE_BUT_OFFTRACK` 如何结合未来 rubric 严格定义；
6. harmful effect 的时间窗口；
7. 何时从 shadow audit 进入 online Sentinel；
8. AndroidControl synthetic validation 的具体设计；
9. 是否保存 emulator checkpoint 以支持任意分支 replay。

这些决定变化不应要求重跑已正确、无损收集的 raw MobileWorld trajectories。
