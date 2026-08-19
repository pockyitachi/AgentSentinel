# Sentinel：GUI Agent 运行时 Pre-step 误导监控与 Rubric 航向校验

> 版本：2026-08-13，补充现有系统表、I/O 契约与端到端示例  
> 研究范围：**当前 GUI 任务、当前单条执行轨迹中的 pre-steps**

## 摘要

当前 GUI agent 通常会把先前动作、模型自评、执行器结果、观察和任务进展等 **pre-steps** 重新注入下一次模型调用。这些历史是 agent 判断“我在哪里、已经做了什么、下一步该做什么”的主要依据，却也可能形成运行时误导：记录可能错误或已经失效；即使每条记录都是真的，一段偏航轨迹也可能持续强化错误方向，使 agent 在下一步继续沿用与任务无关的历史。

本文聚焦这一**单任务、单轨迹、下一步决策前**的问题，而不是跨任务检索的 experience、外部知识或长期记忆。我们提出 **Sentinel**：一个插在 GUI agent 与执行环境之间的 task-local plugin/middleware。任务开始前，Sentinel 根据任务指令生成一份允许多条合法路径、可随证据更新的 milestone rubric；运行中，它在每次下一步决策前执行两个正交检查：

1. **History misleadingness**：pre-step 中的结果、状态或进展 claim 是否被证据反驳、已经失效，因而会给当前一步错误依据；
2. **Rubric trajectory alignment**：当前 GUI 状态和累积轨迹是否仍位于至少一条可完成任务的 rubric 路径上，以及哪些真实 pre-steps 已属于失活分支、不再值得当前一步继续参考。

Sentinel 不替 agent 选择具体动作，也不修改模型权重。它是下一次模型调用之前的 **history gate**：先保留完整原始轨迹到 sidecar，再把宿主 pre-steps 转换成只含“仍可信且与当前任务相关”内容的 `active_history`。被证据反驳的记录从 active prompt 中删除或替换为经过验证的最小事实；真实但属于失活分支的记录从 active prompt 中移出、仅保留在审计日志。随后 Sentinel 把 `task + active_history + compact rubric state + current GUI` 交给原 agent。系统允许 `unknown` 和弃权，无法可靠判断的记录默认不删除。

本文的贡献是：**(1) 问题与测量**——将单轨迹 pre-steps 的运行时风险表示为 `history misleadingness × trajectory alignment` 双轴；**(2) 机制**——实现同时检查历史可靠性和任务走向、并在模型调用前生成 clean active history 的 GUI runtime middleware；**(3) 因果评测**——在相同 GUI 状态下分别构造错误历史、真实但无关的偏航历史和干净历史，测量它们对下一步决策及任务成功率的影响，并分解错误记录过滤、偏航分支过滤与 rubric 状态的作用。

---

## 1. 问题范围与经验前提

### 1.1 本文研究什么、不研究什么

本文只研究 GUI agent 在**当前任务、当前一次执行轨迹**中反复放进 prompt 的 pre-steps：

- 先前动作及其自然语言描述；
- 模型对动作效果的自评；
- executor/tool result 与错误；
- 先前截图、UI tree 或观察摘要；
- 任务进展、滚动摘要和折叠记忆。

本文不研究：

- 跨任务检索的成功/失败经验；
- demonstration 或示例轨迹；
- 外部知识库、RAG 文档或长期 episodic memory；
- 多 agent 之间传递的外部消息。

这些外部 experience 工作只说明“错误上下文会传播”，不是本文的研究对象或核心 baseline。

### 1.2 为什么 pre-steps 可能误导下一步

在第 $t$ 步，宿主把历史 $H_t=\{r_1,\ldots,r_{t-1}\}$ 注入下一次决策。风险至少有两类：

1. **内容误导**：历史声称“点击成功”“商品已加入购物车”或“里程碑已完成”，但执行证据不支持，或该状态后来已经失效；
2. **方向误导**：历史如实记录了 agent 连续访问用户资料页，但当前任务是购买商品；记录本身可以全是真的，却会让下一步继续围绕一条已经偏航的轨迹进行自洽推理。

因此，“pre-step 会不会误导当前 step”不能只等同于“历史是真是假”。它同时取决于：

- 历史内容是否可靠、当前仍适用；
- 历史属于哪条任务路径、对剩余任务是否仍相关；
- 当前 GUI 是否仍能由至少一条合法完成路径解释。

### 1.3 调研口径与可守住的空白

截至 2026-08-04，我们的内部调研整理了 **26 个合并审计条目（表格行）**；每行可能合并同一家族的多个版本，因此不能称为“26 个独立系统”或“26 个具体版本”。其中大部分条目检查了实现或 prompt 编排源码，少数仅依据论文/官方文档；精确的来源分母要在逐行 ledger 补齐后再报告。我们还审计了 MobileWorld 固定快照 `0dcd098` 中的 9 个注册 adapter。两组存在家族重叠，不能相加称为“35 个独立系统”。完整数字须在发布带 `family_id`、具体版本、source type、commit/date 和代码定位的冻结 ledger 后才能作为论文证据。

这些实现采用原始对话回放、逐步结论、滚动摘要和折叠记忆等不同形式保存轨迹。现有 GUI agent 也并非完全不检查历史：Browser-Use 会验证上一目标，Mobile-Agent 与 StepReflect 类方法检查最近 UI transition，Agent-S 会反思近期轨迹，TSR 会持续维护任务状态。

本文不主张“没有系统回看历史”。可检验的研究空白是：在限定的公开系统审计范围内，我们尚未发现一个方法同时：

1. 以多源 GUI 证据判断当前任务内原生 pre-steps 是否正在误导下一步；
2. 用独立、可更新的 milestone rubric 判断整条轨迹是否仍位于可行任务路径；
3. 区分“错误历史”和“真实但偏航的历史”；
4. 在下一次模型调用前，分别通过 `DROP/REPLACE` 处理错误记录，通过 `ARCHIVE` 处理真实但偏航的记录，生成可追溯的 clean active history。

Sentinel 针对的是这一组合，而不是任何单个组件的绝对首创。

### 1.4 现有 GUI agents 怎样处理 pre-steps

附录 A 按统一口径列出本次调研的 **26 个合并审计条目（表格行）**，附录 B 单独列出 MobileWorld 固定快照中的 **9 个注册 adapter**。两张表不相加：后者是 benchmark 中的宿主适配器，且与前者存在家族重叠。

表中不使用含混的“有/无历史核验”，而是分别记录：pre-step 的表示与窗口、写入时点、可用证据、已有检查的范围，以及 Sentinel 需要拦截的位置。这里的“未见记录级审计”特指：在冻结版本的默认路径中，未见一个独立、结构化、可持久化的机制，对任意旧记录维护证据状态，并据此为下一次 model call 构造过滤后的 active history；它不表示该 agent 完全不看当前截图、执行错误或近期轨迹。

### 1.5 `seed_baseline` 的初步自然轨迹证据

为先验证问题本身是否真实存在，我们只分析 MobileWorld `seed_baseline`，不混入 pilot、verifier、retry 或其他 agent 条件。该快照包含 116 条非空 Seed-2.0-Pro 轨迹、3,397 steps 和 3,281 个带历史的 decision turns。受审 Seed adapter 保留全部旧 assistant 文字，但默认只保留最近 3 张 image observations；65,808 个 all-history source→target exposures 中，56,319 个（85.58%）发生在 source 前/后视觉证据均已离开 prompt 之后。该比例是风险暴露面，不是错误率。

我们把自然案例按四项条件红队：旧 claim 有直接反证、source 确实进入 target request、target 明确采用该 premise、关键 target 没有一个已经写错的当前 GUI 可以独立解释该采用。按这一最严格口径，固定语料中确认 **5/116 条轨迹（4.31% 的已观察下界）**存在 invalid-history uptake。典型链包括：已读出的 invoice 与客户邮箱后来被反复称为不存在；May 4 的 `All Hands` 冲突在 19 步后被翻转为“无冲突”并进入 HR 邮件流程；以及当前菜单已经显示 `Bookmark`，agent 仍沿用旧 premise 把它误当成 remove 操作。这是固定语料中的 minimum known count，不是模型总体 prevalence，也不是 history-only 因果效应。

另有 8 个定向案例和概率 pilot 新发现的 1 个 task 满足直接反证、prompt 暴露和 target uptake，但 source action 已把错误日期、金额、实体或 report 写进当前 GUI。合并去重后，宽口径共确认 15 条传播链、涉及 14/116 个 tasks；这些只能报告为 `confirmed propagation with state confound`，不能用来隔离历史文字的独立作用。

按原实验报告的 headline 记账口径，117 个任务由 48 个“成功或无结果”（46 个 `score=1` 加 2 个 `no_result`）与 69 个 `score=0` 失败组成。上述 14 个 task 全部属于这 69 个失败，因此当前可以报告：**14/69（20.29%）的失败轨迹至少出现一次经人工确认的 invalid pre-step uptake**。其中 5/69（7.25%）在关键 target 没有错误 current-GUI state 的强化，另外 9/69（13.04%）存在 state confound。`14/69` 是固定语料中已确认的观测下界，不表示这些失败已经被证明是由 pre-step 文本因果造成的。

我们还完成了一个 50 个 immediate pairs、25 个 tasks 的单标注概率 pilot：2 个 propagation、3 个 invalid-but-rejected/corrected、6 个 action-failure controls、35 个 no-invalid-source、4 个 unverifiable；两个 propagation 中有一个来自 scanner non-hit。该 pilot 只用于证明随机样本中也能找到现象、校准标签和设计正式样本，不能作为 all-history prevalence。正式研究将以 target decision turn 为单位，检查全部 `P_i, i<t`，抽样 500–600 turns、70–90 task clusters，并对至少 20% 做双人复标。

完整证据链、prompt request 行证、红队裁决与抽样设计见 [problem solidification report](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/problem_solidification_report.md>)。只有在冻结 GUI state、system prompt 和其他 messages 后，仅改变目标 history span 的配对 replay 中，本文才主张 pre-step 的因果影响。

---

## 2. 双轴问题定义

Sentinel 在 prompt 外维护证据账本 $E_{\le t}$，保存可取得的 pre/post screenshot、UI/无障碍树、动作、executor/tool result、时间戳和实际发送给 agent 的历史。每条记录被解析成带来源和时间语义的 claims。

### 2.1 轴一：History misleadingness

对 pre-step 中每个事实性、结果性或任务进展 claim，Sentinel 分别判断：

```text
epistemic_status ∈ {supported, refuted, unverifiable}
current_validity ∈ {active, invalidated, unknown, n/a}
```

- `epistemic_status`：该 claim 对它所声称的时间点而言是否受证据支持；
- `current_validity`：该 claim 现在是否仍可被当作当前状态使用。

例如：

- “第 3 步确实加入过购物车”可以是受支持的历史事件；后来购物车清空，并不会让该事件变成假话；
- “购物车现在有该商品”属于持续状态，清空后应标为 `supported + invalidated`；
- 当前屏幕没有显示某个过去事件，不足以反驳该事件；
- executor 的 `success` 可能只代表动作格式或调用无异常，不一定代表任务语义成功。

只有 claim 已被 `refuted`，或已经 `invalidated` 却仍作为当前任务进展暴露给 agent 时，Sentinel 才将其标为高 misleading risk。证据不足时输出 `unverifiable/unknown`，不能自动 `DROP/REPLACE`。

### 2.2 轴二：Rubric trajectory alignment

任务开始前，Sentinel 根据任务指令生成一个**可版本化的 AND–OR milestone graph**，而不是一条固定动作序列：

```text
Rubric = {
  hard_requirements,
  subgoals,
  alternative_paths,
  constraints,
  other_or_unknown_path,
  version
}
```

每个 milestone 包含：

```text
<milestone_id,
 kind,                  # hard_requirement / subgoal / constraint
 requiredness,          # terminal_required / derived_required / optional / preference
 predicate,
 instruction_span,
 prerequisites,         # AND / OR
 alternative_group,
 expected_apps_views,
 observability,         # gui / ui_tree / program / latent
 persistence,           # event / persistent / reversible
 confidence,
 status,                # pending / in_progress / satisfied / violated / unknown
 evidence_refs>
```

全文统一使用以下符号，实验条件另用 `Hist*` 和 `M*`，避免重名：

- `H*` = **Hard requirement**：任务最终必须满足的硬要求；
- `S*` = **Subgoal/checkpoint**：运行中可观察的子目标或检查点，包括由硬要求派生的候选核验、可选 waypoint 和 preference；
- `P*` = **Path**：由若干 `S*` 组成的一条可替代完成路径；
- `C*` = **Constraint**：全程不能违反的约束。
- `R*` = **Pre-step record**：宿主历史中的原生记录；它不属于 rubric，是 Sentinel 审计与 history-gate 操作的对象。

其中：

- **Hard requirement** 只能来自用户指令中的显式要求或确定性任务规范，必须引用原 instruction span；
- **Subgoal/checkpoint** 必须标明 `requiredness`：由硬要求派生的核验项需要满足；搜索、分类浏览等 optional waypoint 可以跳过；preference 只用于合法候选之间排序；
- **Alternative paths** 表示多条合法完成路线；
- **OTHER/unknown path** 为未枚举但可能合法的路线保留出口；
- rubric 可以增加或修订 soft branch，但运行中不能擅自修改用户明确给出的 hard requirements。

#### Amazon 示例

任务：“在 Amazon 把 1 个价格不超过 25 美元、评分至少 4.5 的 USB-C wall charger 加入购物车；Prime 优先，不要结账。”

- `H1`：购物车最终含且仅含 1 件合格 USB-C wall charger（hard terminal state）；
- `H2`：不进入 checkout（hard constraint-backed requirement）；
- `S1`：候选类型是 USB-C wall charger（derived-required checkpoint）；
- `S2`：候选价格不超过 25 美元（derived-required checkpoint）；
- `S3`：候选评分至少 4.5（derived-required checkpoint）；
- `S4`：合格候选中优先 Prime（preference）；
- `P1`：搜索路径；`P2`：分类浏览路径；`P3`：首页推荐路径；
- `C1`：不能把未通过 `S1–S3` 的候选当作 H1 已完成（constraint）。

这保留了“搜索 → 商品页 → 购物车”这一典型 rubric，但不会把搜索框误当成唯一合法路径。如果当前 GUI 跑到 user profile，且 profile 不属于登录、地址确认或其他可行分支，Sentinel 先标为 `deviating`；若连续多步仍未回到任何 viable frontier，才升级为 `off-track`。若 profile 是完成登录所必需，则仍可属于合法替代路径。

运行时，Sentinel 更新：

```text
milestone_status ∈ {pending, in_progress, satisfied, violated, unknown}
trajectory_alignment ∈ {on_track, deviating, off_track, unknown}
```

### 2.3 两个轴为什么不能合并

这张表不是四种 pre-step 数据格式，也不是四个错误等级。它是一个 **2×2 判断表**：横向问“当前任务方向对不对”，纵向问“当前仍会影响决策的历史依据可靠吗”。两问互不替代。

其中，History=`低` 只表示“没有发现尚未纠正、与当前决策相关的高置信矛盾”，不表示每句话都已被证明；Alignment=`off-track` 表示当前状态不在任何已知可行 frontier，或明显违反任务硬约束，但不表示任务已经无法恢复。证据不足时应输出 `unknown`，不能硬塞进四格。

| Case | History misleadingness | Trajectory alignment | 直观含义 | Sentinel 的具体处理 |
|---|---|---|---|---|
| A | 低 | on-track | 历史没有已知问题，当前也在推进任务 | `KEEP`：相关记录继续进入 `active_history` |
| B | 高 | on-track | 某条历史写错了，但 GUI 仍位于可行任务路径 | `DROP/REPLACE`：错误内容不进入下一轮；必要时以经过验证的最小事实替换 |
| C | 低 | off-track | 历史如实记录了 agent 走错方向 | `ARCHIVE`：原记录保留在 sidecar，但不进入下一轮 `active_history`；rubric state 只报告仍未完成要求/frontier |
| D | 高 | off-track | 错误历史与偏航同时存在 | `DROP/REPLACE + ARCHIVE`：清除错误依据并移出整个失活分支，再由原 agent 根据当前 GUI 决策 |

Rubric 只能判断轨迹相关性和任务方向，不能单独把历史事实标为假。两个轴**分别弃权**：History=`unknown` 时不执行事实 `DROP/REPLACE`；Alignment=`unknown` 时不执行 `ARCHIVE`。另一轴若证据充分，仍可独立操作。只有 D 类联合过滤要求两轴都达到各自阈值。`deviating` 是 `on_track` 与 `off_track` 之间的预警态，此时先保留记录并附内部 annotation，不立即从 active history 移除。这样系统才能区分：

> “历史说错了”与“历史没说错，但这段历史不值得当前一步继续沿用”。

---

## 3. Sentinel：Task-local Runtime Middleware

### 3.1 插件架构

Sentinel 位于宿主组装历史和下一次模型调用之间。它不修改 sidecar 中的原始轨迹，而是为本次调用生成一个派生的 active-history view：

```text
host raw pre-steps ──→ extractor / claim parser ──→ evidence + rubric audit
        │                                             │
        └────────────→ sidecar raw log                ↓
                                      history gate: KEEP / DROP / REPLACE / ARCHIVE
                                                    ↓
                                             clean active_history
                                                    ↓
task + active_history + compact rubric state + current GUI
                                                    ↓
                                      host agent next-step model call
```

系统包含：

1. **Sidecar evidence store**：保存不会因 prompt 裁剪而丢失的截图、UI tree、动作和工具结果；
2. **Model-call interceptor**：在宿主发出下一次模型请求前取得原始 messages；
3. **Host extractor/renderer**：解析不同宿主的历史格式，根据 gate decision 构造不含被过滤内容的 active-history view，并保持 tool-call/result、图文块和角色顺序合法；
4. **Rubric generator/tracker**：生成多路径 rubric，实时维护 milestone 和 viable frontier；
5. **History monitor**：判断 pre-step claim 的证据状态、当前有效性和路径相关性。

MobileWorld 的统一 runner 不提供统一 `get_history/set_history`，所以工程承诺是“统一监控协议 + 每宿主 extractor/renderer”，不是“实现一次、九宿主零适配”。原始轨迹始终完整保存在 sidecar；不同宿主只改变本轮送给模型的派生 view。Agent 的权重、动作空间和任务 evaluator 保持不变。

### 3.2 Rubric 生成与安全校验

任务开始前：

1. 从 instruction 抽取实体、显式终态、必须满足的约束和禁止条件；
2. 独立生成最多 3 条差异化候选路径；
3. 合并成不超过 12 个节点的 AND–OR 图；
4. 检查每个 hard node 是否引用原 instruction span，是否引入新实体，是否把 soft step 错写成必要条件；
5. 保留 `OTHER/unknown` 路径；
6. 冻结 rubric v0，后续每次修订保留版本、理由和证据。

Benchmark 的人工 gold rubric 只用于评测和 oracle，不输入正常 Sentinel。

### 3.3 每一步的运行循环

Sentinel 使用“先看 GUI，再解释历史”的两阶段流程，降低污染历史反过来影响航向判断的风险。

#### A. GUI–rubric grounding

输入当前 GUI、UI tree、rubric 和最近状态变化，暂不输入自然语言 pre-steps。输出：

- 当前可能对应的 rubric nodes；
- 已满足、未满足或已失效的 hard requirements；
- 当前所有 viable paths 及其 frontier；
- `on_track / deviating / off_track / unknown`；
- 证据引用和置信度。

若多个路径都能解释当前 GUI，保留路径集合，不强选唯一方案。

#### B. History–path linking

再把结构化 history claims 与阶段 A 的结果结合，判断每条 pre-step：

```text
path_membership ∈ {active_path, inactive_branch, path_independent, unknown}
task_relevance ∈ {high, low, unknown}
```

历史文本不能单独把 milestone 判为已完成；它只能帮助解释动作意图、来源分支和当前相关性。

#### C. 偏航升级规则

满足以下任一条件时进入 `deviating` 候选：

1. 当前 GUI 明确违反 instruction-backed hard constraint；
2. 当前状态高置信匹配已经失活或互斥的分支；
3. 连续 $k$ 步没有推进任何 viable frontier，且动作持续引用 inactive branch；
4. 历史声称 milestone 已完成，但 GUI 或 sidecar 证据明确表明未完成或已失效。

若当前 GUI/程序状态已经以高置信直接违反 instruction-backed hard constraint，或当前 active candidate 明确不满足由 `H*` 派生的必要 `S*` checkpoint，可立即判为 `off_track`；其他偏航信号升级为 `off_track` 或触发高强度干预前，要求：

- 连续两次观察支持；
- 超过 calibration 阈值；
- 当前不是 loading、弹窗遮挡、跨应用过渡或合法回退；
- 至少仍存在一个可行 frontier；
- `OTHER` 路径不能合理解释当前状态。

证据不足时输出 `unknown`。

### 3.4 从原始 pre-steps 到模型输入：Sentinel 到底产出什么

Sentinel 有两个调用时点。任务开始时，它从 instruction 生成 rubric v0；每一步运行时，它拦截宿主**即将发送给模型**的 request，生成一个新的 request。核心关系是：

```text
ModelRequest_t = HostSystemAndTools + Task + ActiveHistory_t + RubricState_t + CurrentGUI_t

ActiveHistory_t =
  KEEP(records)
  + REPLACE(records with verified minimal facts)
  + KEEP_UNCERTAIN(records)
  - DROP(refuted/invalid records)
  - ARCHIVE(true but inactive-branch records)
```

因此，Sentinel 的主要部署产物不是一段“建议下一步点击哪里”的文字，而是 **`active_history` 和由它组装出的下一次 model request**。宿主原有 system policy、tool schema 和采样参数原样保留；完整原始历史仍在 sidecar 中，便于审计和回放；被 `DROP/ARCHIVE` 的内容不再出现在给 agent 的 active prompt 中。上式描述完整部署条件；消融实验可以隐藏 `RubricState`，但不能偷偷恢复已过滤的原始历史。

#### 3.4.1 与实现字段一一对应的调用流程

下面是后续插件实现采用的函数边界。它不是另一套独立算法：每个变量都对应本节后面的输入/输出字段。

```python
def intercept_next_model_call(host_request, episode):
    # 1. 当前 GUI 先独立更新 rubric；不让可能被污染的历史决定任务真值。
    rubric_state = episode.rubric_tracker.update(
        task=episode.task_instruction,
        current_gui=episode.current_gui,
        last_transition=episode.last_transition,
    )

    # 2. 从宿主原始 messages 中抽取会进入下一轮的 pre-step records/claims。
    records = episode.adapter.extract_presteps(host_request.messages)
    claims = episode.claim_parser.parse(records)

    # 3. 用 screenshot、UI tree、executor result 等证据检查历史内容。
    claim_verdicts = episode.history_monitor.verify(
        claims=claims,
        evidence_store=episode.sidecar,
        current_gui=episode.current_gui,
    )

    # 4. Rubric 只判断相关性；证据 verifier 只判断事实可靠性。
    decisions = episode.history_gate.decide(
        records=records,
        claim_verdicts=claim_verdicts,
        rubric_state=rubric_state,
    )

    # 5. 只渲染 KEEP / REPLACE / KEEP_UNCERTAIN；DROP / ARCHIVE 不进入 active prompt。
    active_history = episode.adapter.render_active_history(
        records=records,
        decisions=decisions,
        preserve_protocol=True,  # 保持 tool-call/result、role 和图文块结构合法
    )

    # 6. 原 agent 只看到清理后的历史、简短任务状态和当前 GUI，并自行选动作。
    request_envelope = episode.adapter.extract_non_history_envelope(
        host_request  # 仅保留 system policy、tool schemas、采样参数等非历史部分
    )
    model_request = episode.adapter.compose_model_request(
        request_envelope=request_envelope,
        task=episode.task_instruction,
        active_history=active_history,
        rubric_state=rubric_state.for_agent(),
        current_gui=episode.current_gui,
    )

    # 7. 原始历史、证据、判定和最终 request 全部写入 sidecar，便于复现。
    episode.sidecar.save_gate_result(
        raw_messages=host_request.messages,
        records=records,
        claim_verdicts=claim_verdicts,
        rubric_state=rubric_state,
        decisions=decisions,
        model_request=model_request,
    )
    return model_request
```

| 代码变量 | 实际内容 | 是否给原 agent 看 |
|---|---|---|
| `host_request.messages` | 宿主原本准备注入的全部 pre-steps | 否；先被 Sentinel 拦截 |
| `rubric_state` | H/S/P/C 的当前状态、可行路径和 frontier | 只给压缩后的任务状态 |
| `claim_verdicts` | 每个历史 claim 的 supported/refuted/unverifiable | 否；属于内部审计结果 |
| `decisions` | 每条记录的 KEEP/DROP/REPLACE/ARCHIVE/KEEP_UNCERTAIN | 否；属于 history gate 控制信号 |
| `active_history` | 过滤后仍允许进入下一轮的历史 view | 是 |
| `model_request` | `host system/tools + task + active_history + rubric state + current GUI` | 是；这才是 Sentinel 交给模型的最终产物 |

**当前状态。** 工作区存在一个实验性 `sentinel_mvp` 接口草稿，用来探索 claim span、
evidence 和 gate operation 的序列化方式；它不属于本阶段的实证结论。当前 replay
fixture 仍有语义级过滤、完整 request envelope、因果时序和 oracle 边界需要修正，
不能据此声称“无效 pre-step 已经可靠地不再进入真实下一轮请求”。本阶段只完成
`seed_baseline` 的自然案例审计、prompt 暴露核验和概率抽样 pilot；实现工作在完成
all-history 标注与冻结状态 replay 设计后再继续。下面的接口与伪代码是待验证设计契约，
不是已经达到的系统能力。

#### 3.4.2 运行时输入

```yaml
SentinelStepInput:
  task_instruction: string
  rubric: {version, hard_requirements, subgoals, alternative_paths, current_frontiers}
  host_request:
    adapter_id: string
    messages: [message_or_content_block]     # 未经过滤的宿主 request
  observation:
    current_gui: {screenshot_ref, ui_tree_ref, app, view, state_hash, timestamp}
    last_transition: {pre_gui_ref, action, executor_result, post_gui_ref} | null
  sidecar:
    prior_claims: [...]
    evidence_refs: [...]
    prior_gate_decisions: [...]
  host_capabilities:
    {filter_history_view, replace_content, preserve_protocol_shell}
  runtime_budget:
    {max_verifier_calls, max_audited_claims, max_output_tokens}
```

正常部署输入不包含未来轨迹、benchmark 最终 checker、人工 gold rubric 或只在评测中可见的 oracle。任务开始时另有一次 init call，输入 task/host/environment，输出 rubric v0 和 monitor configuration。

#### 3.4.3 一个具体的 prompt 前后对照

宿主原本准备发送：

```text
Task: Add one qualifying USB-C wall charger to the cart. Do not checkout.

Raw history:
R1: Searched for USB-C wall chargers.
R2: Successfully applied the 4.5+ rating filter.
R3: Opened a laptop stand product page.

Current GUI: [未筛选的 charger 搜索结果页；filter checkbox=false]
```

Sentinel 的内部判定是：

```json
[
  {"record_id": "R1", "operation": "KEEP", "reason": "supported and active-path"},
  {"record_id": "R2", "operation": "REPLACE", "reason": "filter click had no effect",
   "replacement": "The attempted rating-filter click did not change the result state."},
  {"record_id": "R3", "operation": "ARCHIVE", "reason": "true event from inactive detour"}
]
```

原 agent 最终收到：

```text
Task: Add one qualifying USB-C wall charger to the cart. Do not checkout.

Active history:
- Searched for USB-C wall chargers.
- Verified state: the attempted rating-filter click did not change the results.

Rubric state:
- H1 cart requirement: pending.
- H2 no-checkout requirement: satisfied so far.
- S1/S2/S3 candidate checks: unresolved.
- Viable path: P1 search path.
- Current frontier: verify a qualifying candidate.

Current GUI: [未筛选的 charger 搜索结果页；filter checkbox=false]
```

这里 `R2` 的错误说法没有进入 prompt，`R3` 的真实偏航记录也没有进入 active history。Rubric state 只告诉模型“哪些要求仍未完成、当前哪些路径仍可行”，不输出坐标或具体 tool action；最终点击仍由原 agent 根据当前 GUI 决定。

### 3.5 Sentinel 的结构化输出

```yaml
SentinelStepOutput:
  claim_verdicts:
    - {claim_id, source_record_id, epistemic_status, current_validity,
       confidence, evidence_refs, rationale}

  rubric_state:
    {status, viable_paths, current_frontiers, satisfied_requirements,
     unresolved_requirements, violated_requirements, confidence, evidence_refs}

  history_decisions:
    - record_id: string
      claim_ids: [string]
      source_spans: [[start, end]]
      operation: KEEP | DROP | REPLACE | ARCHIVE | KEEP_UNCERTAIN
      replacement_text: string | null
      reason: string
      reversible: true

  active_history: [message_or_content_block]
  rubric_state_for_agent: string
  model_request:
    {messages: [message_or_content_block], tools: [...], model_config: {...}}
  telemetry:
    {records_considered, verifier_calls, abstained, latency_ms,
     raw_history_tokens, active_history_tokens}
```

操作语义如下：

- `KEEP`：事实受支持且仍属于 active path，原样进入下一轮；
- `DROP`：记录或原子 claim 已被明确反驳，且删除不会破坏必要上下文；
- `REPLACE`：一条记录真假混合、或完全删除会丢失必要 transition；只注入经过证据支持的最小替代文本；
- `ARCHIVE`：记录是真实事件，但属于已经失活的偏航分支；只存 sidecar，不进入 active prompt；
- `KEEP_UNCERTAIN`：证据不足，保守保留；若仍注入，必须显式标成 `UNVERIFIED`，不能伪装成已验证事实。

如果宿主的 tool-call/result adjacency、服务端历史或缓存协议不允许安全删除，adapter 必须保留协议外壳并以 `REPLACE` 清空/替换误导语义，或者将该宿主标为 annotation-only fallback。论文主实验只把能够构造真实 active-history view 的宿主计入 filtering 条件，不能把“在末尾追加一条请忽略”冒充为已经删除原记录。

输出遵守以下不变量：

1. Rubric 不能单独把历史 claim 判为 `refuted`；事实 verdict 必须有独立 GUI/执行证据；
2. 当前 GUI 看不到过去事件，不足以反驳该事件；
3. `off_track` 不等于历史失真；真实偏航记录只能 `ARCHIVE`，不能改写成假事件；
4. 用户指令产生的 hard requirements 在运行中不可改写；新路线只能先作为 provisional path；
5. `unverifiable` 默认 `KEEP_UNCERTAIN`，不得执行事实 `DROP/REPLACE`；航向 `unknown` 时不得 `ARCHIVE`；
6. 所有 DROP/REPLACE/ARCHIVE 都保留原记录、证据和可逆映射在 sidecar；
7. Sentinel 不输出 GUI tool action；最终动作仍由宿主 agent 决定。

### 3.6 四种情况怎样改变下一轮 prompt

- **A（Low + On-track）— KEEP**：有效且相关的 pre-steps 继续进入 `active_history`；只附最短 rubric state。
- **B（High + On-track）— DROP/REPLACE**：错误内容不再进入下一轮；若需要维持因果连续性，替换成“点击未生效”等已验证事实。当前路径仍保留。
- **C（Low + Off-track）— ARCHIVE**：真实偏航记录保存在 sidecar，但从 `active_history` 移出；下一轮只看到偏航前仍相关的历史、未完成要求和当前 GUI。
- **D（High + Off-track）— DROP/REPLACE + ARCHIVE**：移除错误 claim，并把整个失活分支移出 active history；不给 agent 指定点击，只让它基于清理后的上下文重新决策。

若状态仅为 `deviating`，先 `KEEP` 并等待更多观察；若当前 GUI 已高置信违反 hard constraint 或必要 `S*` checkpoint，可按 3.3 的单步例外直接过滤。`Shadow` 条件只计算 verdict，不改变 prompt。

### 3.7 完整模拟轨迹：Amazon 购物任务

以下是**机制演示，不是实验结果**。任务是：

> 在 Amazon 模拟商城中，把 1 个价格不超过 25 美元、评分至少 4.5 的 USB-C wall charger 加入购物车。Prime 商品优先；不要结账。

Sentinel 从指令生成：`H1` 最终购物车含且仅含 1 件合格 USB-C wall charger；`H2` 不进入 checkout。为了在加购前判断候选，tracker 维护四个 `S` checkpoints：`S1` 商品类型是 USB-C wall charger、`S2` 价格 ≤$25、`S3` 评分 ≥4.5、`S4` 合格候选中优先 Prime。`S1–S3` 是由 H1 派生的必要核验，`S4` 是 preference。`P1` 搜索、`P2` 分类浏览和 `P3/OTHER` 推荐入口是可替代路径，不是硬要求。演示环境固定三个商品：合格的 `VoltEdge Mini 20W Charger`（$18.99、4.7、Prime）、不相关的 `FoldPro Laptop Stand`（$22.99、4.8、Prime）和不合格的 `CablePro Cable`（$29.99、4.3）。

下表每行只显示该步新写入或当前最关键的 pre-step。Sidecar 始终保留全部原记录；真实下一轮 prompt 只携带经 gate 处理后的 `active_history`。

| Step | 宿主写入的 pre-step / 当前 GUI | 双轴判断 | Sentinel 处理 | 对下一步的影响 |
|---|---|---|---|---|
| 0 | 无历史；Amazon 首页、空购物车 | **A：Low + On-track** | 状态块列出 H1 未完成、H2 当前满足，S1–S4 待核验；frontier 为“发现合格候选” | Agent 搜索 `usb c wall charger` |
| 1 | `R1: 已到达相关结果页`；GUI 显示 VoltEdge $18.99/4.7/Prime | **A：Low + On-track** | R1 有搜索 transition 和结果页支持；保持 P1 搜索路径 | Agent 尝试点 `4 Stars & Up` |
| 2 | `R2: 已成功应用评分筛选，所有结果均合格`；实际 checkbox 仍未选中、页面未变化 | **B：High + On-track** | `REPLACE R2` 为“筛选点击未改变结果”；原错误句不进入 active prompt。筛选只是 optional step，P1 仍可行 | Agent 仍可直接核验 VoltEdge，无需重新搜索 |
| 3 | 误点 sponsored card；`R3: 打开了 FoldPro 支架，这是误选`；GUI 确为支架页 | **C：Low + Off-track** | R3 是真事件，不改写 sidecar；但当前商品不满足 S1，因此 `ARCHIVE R3`，它不进入下一轮 active history | Agent 只看到未完成要求、当前错误 GUI 和可行 frontier，不会继续把支架当进度 |
| 4 | `R4: 已返回 charger 结果页`；GUI 再次显示 VoltEdge | **A：Low + On-track** | R4 受 transition 支持并 `KEEP`；R3 仍只在 sidecar | Agent 重新定位 VoltEdge |
| 5 | 宿主误写 `R5: 已打开 VoltEdge，$18.99/4.7/Prime，满足全部商品要求`；GUI 实际为 CablePro，$29.99/4.3，购物车为空 | **D：High + Off-track** | `DROP/REPLACE R5` 的错误商品 claims，并 `ARCHIVE` 当前失活分支；active prompt 只保留此前相关历史，H1 仍未完成 | Agent 不会基于“已经合格”继续加错商品，而会根据清理后的上下文重新决策 |
| 6 | `R6: 已离开不合格 CablePro，尚未加购`；GUI 为结果页 | **A：Low + On-track** | R6 受支持；用当前 UI element 而非旧坐标定位 VoltEdge | Agent 打开正确商品 |
| 7 | `R7: 已打开 VoltEdge charger`；GUI 显示 $18.99、4.7、Prime 和 Add to Cart | **A：Low + On-track** | S1–S4 的候选证据齐全；H1 仍待加购验证；frontier 为“仅加购 1 件，不结账” | Agent 点击 Add to Cart 一次 |
| 8 | `R8: 已加入 1 件 VoltEdge`；mini-cart 显示正确名称、Qty 1、$18.99，未进入 checkout | **A：Low + Terminal** | H1/H2 满足，且 S1–S3 与购物车商品建立同一性链接；S4 preference 也满足；输出 `TERMINAL_SUCCESS` | Agent 停止，不点 checkout |

四个关键输出块分别是：

```text
# Case A — Low + On-track
History: R1 supported.
Rubric: ON_TRACK via search path.
Current frontier: verify the visible qualifying candidate.
Action on history: KEEP.

# Case B — High + On-track
History correction: R2 is REFUTED; the rating filter remains unchecked.
Rubric: ON_TRACK; filtering is optional and a qualifying candidate is visible.
Gate operation: REPLACE R2 with the verified no-state-change fact.
Active prompt: the original false sentence is absent.

# Case C — Low + Off-track
History: R3 supported; it truthfully records the laptop-stand detour.
Rubric: OFF_TRACK; the current product fails candidate checkpoint S1.
Gate operation: ARCHIVE R3; keep provenance only in sidecar.
Active prompt: R3 is absent; unresolved requirements/frontier remain visible.

# Case D — High + Off-track
History correction: R5 is REFUTED by product identity, price and rating.
Rubric: OFF_TRACK; S1/S2/S3 fail on this branch and terminal H1 remains unresolved.
Gate operation: DROP/REPLACE R5 + ARCHIVE the inactive branch.
Active prompt: only verified, still-relevant history and current rubric state remain.
```

这个 trajectory 展示了 Sentinel 的边界：它没有规定“下一步必须点击哪个坐标”，而是在 model call 前删除/替换错误依据、移出偏航历史，再把 clean active history、rubric state 和当前 GUI 交还给原 agent 决策。

---

## 4. 研究问题与贡献

### 4.1 研究问题

- **RQ1：Pre-step harm**——在 GUI 状态相同的情况下，仅改变 pre-step 内容或当前相关性，会不会改变下一步动作并损害任务成功？
- **RQ2：History gate**——Sentinel 能否识别已被反驳、已失效或错误声称任务进展的 pre-steps，并在允许弃权的前提下安全 `DROP/REPLACE`？
- **RQ3：Rubric-guided filtering**——动态 milestone rubric 能否识别真实但偏航的轨迹，将失活分支从 active prompt 中 `ARCHIVE`，并减少历史自强化？
- **RQ4：Joint value**——事实过滤与 rubric 路径过滤是否互补，联合 clean-history gate 是否优于任一单轴？
- **RQ5：Plugin generality**——同一双轴协议能否通过显式适配层覆盖不同 GUI 历史表示？

### 4.2 贡献定位

1. **问题与测量**：将当前任务内 pre-steps 的运行时风险操作化为 `history misleadingness × rubric trajectory alignment`，区分错误/失效历史、真实但偏航历史及两者共存；
2. **机制**：实现允许弃权的 task-local history gate，以多源 GUI 证据决定 `KEEP/DROP/REPLACE`，以多路径 rubric 决定是否 `ARCHIVE` 真实偏航记录，并为下一次调用构造可追溯的 `active_history`；
3. **因果评测**：在状态一致的 GUI 分支中，分别评估 invalid-record filtering、inactive-branch filtering、联合 clean-history gate 和 oracle 上界，同时报告误删、错误替换、下一步行为、任务成功率和运行成本。

---

## 5. 相关工作与差异

### 5.1 非本文核心：外部 experience 与长期记忆

跨任务 experience-following、poisoned memory 和 memory bank 研究表明，错误外部经验会传播到后续决策。但它们的干预单元是跨 episode 的检索记忆；本文研究的是**同一个 GUI episode 内，宿主自动累积的 pre-steps 对紧接着的下一步决策的影响**。因此这些工作只作邻域动机，不作为主要问题定义。

### 5.2 直接近邻

- [When History Lies](https://arxiv.org/abs/2608.06057) 通过 Original/Polluted/Oracle State 配对视图研究非 GUI 工具调用中的历史诱导决策翻转，是因果设计近邻，但不处理视觉轨迹或部署期航向 monitor；
- [GUI-RobustEval](https://arxiv.org/abs/2605.29447) 测量 GUI agent 在错误策略前缀后的恢复，但环境状态和历史同时变化，不能单独识别 pre-step 文本的影响；
- [StepReflect](https://arxiv.org/abs/2608.05587) 用前后截图验证最近一次 UI transition 是否符合宿主给出的 expectation，但明确不判断 expectation 本身是否正确，也不检查任意旧 pre-step 或整条任务路径；
- [TSR](https://arxiv.org/abs/2607.00502) 持续维护结构化任务状态，但不诊断累积 pre-steps 是否正在误导下一步，也不分别处理错误历史和真实偏航历史；
- [HalluClear](https://arxiv.org/abs/2604.17284) 与 MIRAGE-Bench 主要诊断当前输出中的 hallucination 或不忠实，而不是宿主历史和任务航向的双轴运行时反馈。

本文的新颖性限定为上述组合，不声称“首次使用 rubric”“首次做 GUI reflection”或“首次发现历史会误导 agent”。

---

## 6. 评测设计

> **MVP 是什么？** MVP 通常是 *Minimum Viable Product* 的缩写。本文借用这个说法指“**最小可行研究原型**”：四周内只实现足以检验核心链路的一小版 Sentinel，而不是完整产品或论文最终系统。它只承诺 1–2 个具有显式 extractor/renderer 的宿主、可复现的 active-history filtering 和小规模 pilot；通过闸门后才扩展更多宿主、任务与正式统计功效。

### 6.1 场地与宿主

**四周 MVP**：从 MobileWorld 原始论文定义的 GUI-only 任务中分层选择 40–60 题，覆盖单应用/跨应用和短/长轨迹；固定 task-ID 清单。暂不加入 user-interaction 和 MCP。

- 主宿主：`qwen3vl`，代表平铺式 task-progress 历史；
- 复现宿主：`general_e2e`，代表对话回放历史；
- Paper-core 再加入 `gelab` 的滚动摘要和一个明确带反思的 AndroidWorld 宿主。

MobileWorld 需要 Linux/WSL2、KVM 和 privileged Docker；主跑使用远程 Linux/KVM。所有条件冻结 commit、容器/APK、模型 endpoint/version、temperature、task 参数、seed、最大步数和初始状态。

### 6.2 构造一个对照与三类历史扰动

在同一 GUI checkpoint 上构造：

- **Hist0 Clean/Sham**：只含当前有效、与 viable path 一致的历史；
- **Hist1 True-but-irrelevant detour**：实际执行一段可逆偏航并返回相同 checkpoint；历史事件都是真的，但属于失活分支、与剩余任务低相关；
- **Hist2 False/stale progress**：GUI 状态与对照相同，但历史错误声称某 milestone 已完成，或继续把已失效状态写成当前进展；
- **Hist3 Mixed**：同时包含 Hist1 和 Hist2，用于联合干预测试。

优先使用同一中途 emulator/backend checkpoint 分叉；若基础设施不能完整克隆 backend，则用确定性 prefix replay，并报告状态 mismatch。所有历史文本匹配长度、位置、语气和格式。注入点必须从所有合格步骤中预先随机抽样，不能只从失败轨迹事后选择。

### 6.3 Monitor 条件

- **M0 No monitor**：宿主原样运行；
- **Mplacebo Matched monitor**：匹配调用、token、位置和延迟，但不给目标 claim/rubric 的有效信息；
- **M1 Static rubric**：任务开始时注入 rubric，运行中不更新；
- **M2 Dynamic status**：实时更新 milestone/frontier，但不处理历史；
- **M3 Alignment-only Filter**：rubric 只在内部判定路径；将 inactive-branch records `ARCHIVE`，不给 agent 显示额外 frontier block；
- **M4 History-only Filter**：不提供 rubric state/path filtering，只对被证据反驳或失效的 records 执行 `DROP/REPLACE`；
- **M5 Joint Clean History**：M3 与 M4 的双轴联合过滤；给 agent 匹配长度的中性状态块；
- **M6 Joint + Rubric State**：在 M5 的 clean history 上再显示已满足/未完成要求、viable paths 和 frontier，但不输出具体 GUI action；
- **M7 Oracle**：人工多路径 rubric、金标 frontier 与金标历史处理，用作上界。

所有在线条件尽可能匹配 verifier 调用数、提示长度和墙钟延迟；M0 保持真正的 no-monitor baseline。Shadow 检测默认对 sidecar 日志离线重放，不改变 agent 时序。

### 6.4 预注册核心比较

MVP 不跑完整笛卡尔积，只完成能够回答核心问题的比较：

1. **错误 pre-step 的危害**：`Hist2+M0` vs `Hist0+M0`；
2. **真实偏航历史的危害**：`Hist1+M0` vs `Hist0+M0`；
3. **动态 rubric 的价值**：`M2` vs `M1`，分别在 Hist0/Hist1 下比较；
4. **偏航分支过滤的增量**：`M3` vs `Mplacebo` under Hist1，同时以 Hist0 检查误删；
5. **错误记录过滤的独立价值**：`M4` vs `Mplacebo` under Hist2，同时以 Hist0 检查安全性；
6. **双轴联合价值**：在 Hist3 上运行 `invalid-record filter on/off × inactive-branch filter on/off` 的 2×2 factorial（Mplacebo/M3/M4/M5），估计两轴主效应与 interaction；
7. **可见 rubric state 的额外价值**：`M6` vs `M5` under Hist1/Hist3；
8. **错误 rubric 安全性**：multi-path rubric vs single-path rubric，在已知替代成功路径上的 false-deviation rate。

每项 monitor 对比均加入 matched placebo 或等价的调用/token 控制，避免把“多一次模型思考”误当作 rubric 或历史处理的作用。

### 6.5 指标

**Rubric 质量：**

- explicit hard-requirement recall；
- invented hard-requirement rate；
- alternative-path coverage；
- observable-node coverage；
- rubric revision precision。

**实时航向：**

- frontier accuracy；
- deviation precision/recall 与 false-deviation rate；
- 合法替代路径上的 viable-path recall；
- `unknown`/abstention rate 与检出延迟；
- 从偏航到重新进入 viable frontier 的恢复步数。

**历史处理：**

- refuted/invalidated claim 检测 P/R；
- inactive-branch `ARCHIVE` P/R；
- relevant-history false-archive rate；
- invalid-record DROP/REPLACE precision、benign-record false-drop rate 和 filtering coverage；
- active-history fidelity：应保留的关键 transition/tool result 是否仍完整可用。

**最终行为与成本：**

- 下一步错误动作率；
- 任务成功率 SR；
- 无进展或浪费步数；
- oracle-gap recovery；
- 每步额外调用、token、延迟、缓存命中和每救回一题成本。

### 6.6 金标与标注边界

金标不要求穷举全部合法 GUI 路线。标注者只需判断：

- instruction 中的显式 hard requirements；
- 当前状态是否与至少一条已知合法路径一致；
- 是否明确违反 hard constraint；
- 某段历史是否仍有助于剩余要求；
- 无法排除未枚举路径时必须标 `unknown`。

正常 Sentinel 只接收任务指令和部署期可用 GUI 证据。人工 rubric、benchmark checker、数据库和 callback 只用于标注与 M7 Oracle，不能输入正常条件。

### 6.7 统计口径

任务实例是主实验单位；同一任务的 steps、claims 和 seeds 是簇内重复观测。条件按 `task × seed × initial checkpoint` 配对，报告任务级百分点差和 95% CI，使用 paired task-cluster bootstrap，并以 mixed-effects model 作敏感性分析。

40–60 题 × 3 seeds 是 pilot，用于估计效果方向、不一致率和系统方差，不宣称对 5pp SR 差异有充分功效。正式 paper-core 根据 pilot discordance 重新做功效分析；不再使用“每条记录 n=8 + FDR”打有害标签。

---

## 7. 四周最小可行研究（先坐实问题，再实现插件）

### Week 1：All-history 自然发生率

- 冻结 `seed_baseline` 语料、时序定义、claim codebook 和 state-confound 字段；
- 以 target decision turn 为单位构造 500–600 条概率样本，每条展开全部 `P_i, i<t`；
- 至少 20% 双人独立复标，并对全部 positive/uncertain 双标；
- 分开报告 strict propagation、state-confounded propagation、self-correction 和 action-failure control。

**G0：**样本、权重和 prompt-exposure provenance 可完全复现；主要标签一致性达到预注册门槛；报告带 task-cluster CI 的 prevalence，而不是 candidate yield。若严格 positive 只剩不可复核个案，则停止通用问题主张。

### Week 2：冻结 GUI 状态的 history 因果 replay

- 先复现 5 个低 state-confound 自然 decision points；
- 冻结 task、GUI state、system/tools 和其他 messages，只比较 `Original / Mask / Mask+Correction / Oracle-clean`；
- 验证状态 hash、request envelope、图片和随机参数一致，再扩展到 30–50 个 branch points；
- 先测 next-action/rubric alignment，再决定是否值得跑完整 task completion。

**G1：**至少一个预注册的 history intervention 在状态一致条件下稳定改变下一动作，并且方向与人工 gold 一致。若 `Mask/Correction` 与 `Original` 没有可重复差异，则不直接进入 middleware 论文主张，保留测量/恢复分析。

### Week 3：Shadow Sentinel（不改变 actor prompt）

- 只在 Seed 主宿主接入 model-call interceptor 和 sidecar evidence ledger；
- 实现 claim extraction、证据状态与 abstention，但不向 actor 注入 correction；
- 在冻结的 dev/calibration/test 上测 claim coverage、false-refute 和 latency；
- rubric 只生成 instruction-grounded AND–OR 结构并做 shadow status tracking。

**G2：**部署期证据可裁决率、false-refute、rubric invented-requirement 和合法替代路径 false-deviation 均通过预注册门槛；否则继续 shadow，不启用 DROP/REPLACE/ARCHIVE。

### Week 4：受限 history gate 与 rubric 增量

- 仅对 direct-evidence、低风险 claim 开启 `KEEP/DROP/REPLACE/KEEP_UNCERTAIN`；
- `ARCHIVE/LOW_RELEVANCE` 先保持 annotation-only，不破坏宿主私有历史；
- 用 2×2 factorial 分开 history correction 与 rubric relevance 的贡献；
- 第二宿主只做接口可移植性 sanity check，不宣称九宿主零适配。

**G3：**false-drop、错误 replacement 和 clean-task regression 低于预注册风险阈值；history-only correction、rubric-only guidance 与 joint 条件能被分别识别。样本不足时只报告 pilot 区间，不宣称安全界或 5pp 效应已被正式证明。

---

## 8. 资源、时间线与范围控制

四周 MVP 需要 1 台 Linux/KVM 主机、1–2 个宿主、40–60 个 GUI-only 任务、1–3 seeds，以及约 80–150 小时双人标注。API/算力粗略为 USD 2k–10k，取决于模型单价和 verifier 缓存率。

MVP 通过后，paper-core 再扩展到：

- MobileWorld 固定 GUI-only task-ID 集；
- `gelab` 滚动摘要与摘要幻觉；
- AndroidWorld 中明确带反思的宿主；
- 迟显失败、rubric 动态修订和 0/1/3 历史剂量；
- TSR、StepReflect-only、逐步 critic 和 matched monitor baselines；
- 足量配对任务的正式功效实验。

ICLR 2027 摘要截止 2026-09-18 AoE，全文截止 2026-09-25 AoE。9/10 前只根据实际通过的闸门冻结论文范围；不得把四周 pilot 写成充分功效的 confirmatory 结果。

---

## 9. 论文核心表述

### 一句话版本

> Sentinel 是一个位于 GUI agent 下一次 model call 之前的 history gate：它用 GUI/执行证据 `DROP/REPLACE` 错误或失效的 pre-steps，用动态 milestone rubric `ARCHIVE` 真实但属于偏航分支的 pre-steps，再把 `task + clean active history + compact rubric state + current GUI` 交给原 agent 自行决定下一步动作。

### 明确不使用的表述

- “本文研究跨任务 experience 或外部记忆”；
- “当前屏幕看不到就说明历史为假”；
- “rubric 是历史真值证据”；
- “搜索框或商品页是所有购物任务的唯一合法路径”；
- “偏航历史必然是假历史”；
- “没有已有 GUI agent 回看历史”；
- “一次接入即可零适配覆盖九宿主”；
- “运行时风险分数本身已经证明某条历史造成失败”。

部署期 Sentinel 报告的是 misleading/deviation risk；只有在状态一致、只改变历史或监控反馈的配对实验中，论文才主张因果影响。

---

## 附录 A：26 个 GUI-agent 合并审计条目怎样处理单轨迹 pre-steps

本表只讨论**当前任务、当前一次执行轨迹中会进入后续 prompt 的 pre-steps**，不讨论跨任务 experience 或外部知识库。`[源码]` 表示至少检查过该合并条目所指的一个冻结实现或 prompt 编排路径，不代表标题中的每个版本都已逐一核验；`[论文]` 表示当前条目只依据论文描述；`[官方文档]` 表示依据产品/API 文档。窗口均指受审仓库、commit 和配置中的行为，不能外推为同一家族所有版本和部署设置。终稿 ledger 必须为每行补具体版本、source URL、commit/date、config 与 `file:line`；在此之前，表中近似窗口只用于工程选型。

### A.1 移动端 prompt 编排框架（6 个条目）

| Agent / family | Pre-step 表示与窗口 | 写入时点 / 证据性质 | 已有检查的准确范围 | Sentinel 接入点 |
|---|---|---|---|---|
| AppAgent `[源码]` | 一句滚动动作总结，以 `<last_act>` 进入下一轮；不保留逐步列表 | 当轮动作生成阶段写出，主要是意图或预期；下一轮另有当前观察 | 默认部署循环中未见独立效果核验或旧记录修复 | 拦截 `<last_act>`/summary；原值存 sidecar，给下一轮生成 clean replacement summary |
| AppAgent v2 `[论文]` | 滚动摘要，加检索到的元素知识文档；具体默认窗口待源码 ledger | 模型摘要与外部检索文档混合，后者不是当前轨迹结果证据 | 论文中的探索期反思不等于部署期持续记录核验 | 摘要槽和检索文档必须分源；目前只作论文级适配设计 |
| Mobile-Agent-v2 `[源码]` | 近似全量 `Step-i: Operation summary + Action`、memory 和 progress | 执行后用前后截图做 A/B/C ActionReflector | 有最近 transition 核验；未见任意旧记录的持久化重审/修复 | 拦截历史列表和 A/B/C 输出，复用前后截图证据 |
| Mobile-Agent-v3/3.5（GUI-Owl）`[源码]` | executor 近期动作、manager plan/error，以及较长模型文字；常见入口约保留最近 1–3 张图，executor 通常看最近 5 动作 | ActionReflector 执行后比较前后截图；outcome/error 会进入历史 | 有最近 transition 核验，连续失败可重规划；没有任意旧 claim 的持久化审计 | 分层读取 action/outcome/error 与 manager plan；计划不能当作结果真值 |
| M3A（AndroidWorld）`[源码]` | 顺序累积的短自然语言 summary；决策通常使用当前截图 | 动作执行后，以第二次模型调用读取 before/after 截图、UI elements、动作和理由再生成 summary | 有最近 transition 的同一 actor 自评；不是独立 verifier，也不复查任意旧 summary | 拦截 summary list，建立 `source step → pre/post evidence → claim` 索引 |
| T3A（AndroidWorld）`[源码]` | 与 M3A 类似的逐步 summary；文字近似全量 | 动作后结合前后 UI 元素/无障碍状态生成小结 | 有动作后自总结；未见持久化旧记录审计 | 拦截 summary list，以 UI-tree diff 为主要证据 |

### A.2 原生/端到端 GUI 模型（7 个条目）

| Agent / family | Pre-step 表示与窗口 | 写入时点 / 证据性质 | 已有检查的准确范围 | Sentinel 接入点 |
|---|---|---|---|---|
| UI-TARS 1/1.5 与 Doubao 版 `[源码]` | 旧轮 `Thought + Action` 回放；文字较长保留，受审实现最多保留最近约 5 张历史图 | 旧回复在动作生成时写出；后续截图是观察证据，但未显式绑定为 outcome | 未见独立 record-level verifier；模型可借近期图隐式复看 | 拦截 messages，构造保持角色、图文块和前后观察对齐的派生 active view |
| UI-TARS-2 `[论文]` | 近期高保真 working memory + 远期语义压缩 episodic memory | 远期内容是压缩记忆，不是天然真值；默认窗口待源码确认 | 推理期原生旧记录审计尚未由源码确认 | 分接 working/episodic memory；目前只作 paper-level 适用性讨论 |
| CogAgent-9B `[源码]` | 编号 grounded operation 与动作描述列表；文字近似全量、当前图 1 张 | 记录模型选定的操作，不等于动作语义成功 | 未见显式效果或旧记录核验 | 拦截 action-history 字符串，另接 runner 的 post-state |
| Aguvis `[源码]` | `Step N: <动作>` 串；通常全量文字、当前截图 | 动作选择时写入，缺独立 outcome 字段 | 未见显式效果核验 | 拦截动作串；由 sidecar 补 pre/post observation 与 executor result |
| OS-Atlas `[源码]` | 编号历史步骤或子目标；文字较长、当前截图为主 | 多为动作/子目标文本，含计划性质 | 未见显式效果核验 | 拦截 history formatter；将 plan 与 completed event 分型 |
| MAI-UI `[源码]` | `<thinking> + <tool_call>` 旧回复回放；长文字，`history_n=3` 时约保留含当前图在内的 3 张图 | 旧回复多为动作前输出；后续截图未显式绑定成 outcome claim | 未见独立效果或旧记录核验 | 拦截 unified messages；旧图裁剪前存入 sidecar |
| Qwen2.5/3-VL computer-use cookbook `[源码]` | `Task progress: Step 1…` 扁平文字；逐步累积、通常仅当前图 | 普通动作描述多在执行前生成；本行不把 MobileWorld adapter 的 MCP/user-result 补写行为外推到所有 cookbook | 未见独立 record-level verifier | 拦截 progress formatter；区分 `intent / executor result / observation`；MobileWorld 特有行为见附录 B |

### A.3 浏览器/Web agents（7 个条目）

| Agent / family | Pre-step 表示与窗口 | 写入时点 / 证据性质 | 已有检查的准确范围 | Sentinel 接入点 |
|---|---|---|---|---|
| Browser-Use `[源码]` | `<step_N>` 含 `evaluation_previous_goal`、`memory`、`next_goal`、executor `action_results`；历史长度与压缩策略取决于受审版本/配置 | `evaluation_previous_goal` 在上一动作执行、当前页面可见后生成；`next_goal` 面向下一动作，`action_results` 来自执行器 | Prompt 明确要求用当前 screenshot/browser state 验证上一步；是同一 actor 自评，不为任意旧记录保存真值状态或修复 | 拦截 history item；复用 action result/current state，补持久 claim 状态而非重复声称“首次核验” |
| SeeAct `[源码]` | 线性动作历史，异常可写 `Failed to`；决策用当前截图 | 动作文本与执行异常进入后续 prompt | Prompt 要求结合当前截图逐一分析此前动作及效果；属多步 prompt-level 复读，无独立 verdict、证据引用或持久勘误 | 拦截 action-history formatter，新增持久 claim/evidence 状态 |
| WebVoyager `[源码]` | 旧 `Thought/Action` 与 observation 对话；文字较长，通常只留最近约 3 张图 | 动作文本在执行前生成；后续 observation/Selenium 异常是执行后证据 | 有异常通道，未见语义效果的持久化旧记录审计 | 拦截 messages；sidecar 保存被占位符替换的图并对齐 action-observation |
| Skyvern `[源码]` | 结构化 action JSON + result/exception；默认主要使用最近 1 步、当前 screenshot/DOM | result/exception 来自执行层，证据强度高于模型自报 | 有执行级检查；prompt 还要求用当前页面核对历史，但未见任意旧 claim 修复 | 拦截结构化 history JSON，按字段 provenance 使用 executor/DOM |
| WebArena / VisualWebArena 官方基线 `[源码]` | 单条 `PREVIOUS ACTION`；通常 `k=1`、当前 observation | 保存上一动作选择，不含独立语义结果 | 未见历史效果核验 | 拦截 previous-action 模板；以 post-state 区分 action 与 outcome |
| OpenAI Responses API computer-use `[官方文档]` | 官方循环示例可用 `previous_response_id` 延续 response，并由客户端返回 `computer_call_output` screenshot | computer call 来自模型；动作执行后的 screenshot/tool output 由客户端回传 | 有真实执行回传，但不等于对任意旧语义 claim 的显式审计/修复 | Filtering 实验需客户端自管 input items 并构造 active view；仅能用服务端链时归为 annotation-only fallback |
| Magnitude `[源码]` | 带时间戳的思考、动作与观察流；受审默认约最近 20 条思考、短图窗 | 混合模型输出与观察事件 | 未见独立旧记录核验 | 拦截 trajectory/event formatter，按时间戳和字段来源对齐证据 |

### A.4 桌面/computer-use 框架（6 个条目）

| Agent / family | Pre-step 表示与窗口 | 写入时点 / 证据性质 | 已有检查的准确范围 | Sentinel 接入点 |
|---|---|---|---|---|
| Agent-S2 `[源码]` | 近期完整对话/轨迹 + 独立 reflector；默认轨迹上限约 8 轮且可配置 | Reflector 在上一动作后、下一动作前读取当前图和累积轨迹 | 有 bounded-trajectory reflection，可判断循环/偏航；不是只看上一步，也没有 per-record 真值状态/旧记录修复 | 同时接 worker messages 与 reflector 输出，避免重复包装已有轨迹反思 |
| Agent-S2.5/S3 `[源码]` | 旧输出、逐轮 reflection 与 notes；长上下文路径可保留较长文字、最近最多约 8 张图，其他后端会删旧完整轮 | Reflector 读取一段有界轨迹与近期图像 | 有近期轨迹/整体方向反思；未形成持久逐 claim verdict 和旧 turn 修复 | 同时拦截 worker/reflector message state；新增时态 claim 与 sidecar evidence |
| Microsoft UFO/UFO2 `[源码]` | blackboard/trajectory JSON、上一动作细节、executor result 和重复计数 | executor result/error 是执行证据；plan/reasoning 仍是模型文本 | 有执行级结果与重复告警，未见旧语义 claim 的持久复核 | 拦截 blackboard JSON，保留字段 provenance |
| Anthropic computer-use 参考实现 `[源码]` | API 原生 thinking、tool_use、tool_result、截图块；文字较长，常只留最近约 3 张图，cache 配置会改变策略 | `tool_result/is_error` 是执行层回传；任务语义是否达成仍需观察 | 有执行错误证据，未见通用语义 outcome verifier | 由客户端构造协议合法的派生 messages；若过滤会破坏 tool-use/result 配对则执行最小 `REPLACE` |
| OSWorld 官方基线 `[源码]` | 最近 `k` 个 observation-response 对；受审默认 `k=3` | 混合真实 observation 与模型旧回复，更旧轮会裁掉 | 未见显式历史核验 | 拦截 rolling trajectory buffer；sidecar 保存被裁证据 |
| Cradle `[源码]` | 由近期轨迹折叠的滚动 summary + 上一动作细节/反思；summary 更新常看最近约 5 步 | self-reflection 在动作后比较前后状态；summary 是模型聚合记录 | 有最近 transition 反思；未见对既有 summary 中任意旧 claim 的持续复核 | 同时接 summary 与 reflection；用 summary-span provenance 保留来源 |

**计数说明：**A.1–A.4 共 26 个**合并审计条目/表格行**（6+7+7+6），不是 26 个独立系统或 26 个具体版本。若按标题中合并的具体版本拆分，数量须另行重算；若论文使用“21 个系统家族”，冻结 ledger 必须新增 `family_id`、合并理由、source type、commit/date、默认配置和 `file:line`，不能让读者自行猜测版本合并方式。

---

## 附录 B：MobileWorld 固定快照的 9 个注册 adapter

下表针对 MobileWorld commit `0dcd098`。它统一的是 agent 加载与 `initialize/predict/done/reset` 生命周期，并没有统一 `get_history/set_history` 或 history schema；因此 Sentinel 统一的是协议与 sidecar，读取和渲染仍需 per-host adapter。

| MobileWorld adapter | Pre-step 表示与窗口 | 写入时点 / 可用证据 | 已有检查的准确范围 | Sentinel 接入点 |
|---|---|---|---|---|
| `general_e2e` | 对话回放；较长文字、最近约 3 张图，旧图换占位 | 混合模型 `Thought/Action`、后续 screenshot、tool/user result | 未见独立、持久化的 record-level verifier/repairer；并非没有执行后观察 | messages 与图像裁剪函数；sidecar 保存被替换图 |
| `mai_ui_agent` | `<thinking>+<tool_call>` 回放；较长文字、约 3 张近期图，较旧含图 user message 会删除 | 旧回复多是动作前模型输出；下一轮截图是后验观察 | 未见独立历史核验/修复组件 | unified messages；删旧图前建立证据索引 |
| `qwen3vl` | 当前 1 张图 + 无显式上限的 `Task progress` 文字串 | 动作描述通常在执行前写；MCP/user return 存在时可补入上一步 progress | 未见独立 record-level verifier | progress serializer；按字段区分 intent/result/observation |
| `gui_owl_1_5` | 当前图为主；旧轮折叠为文字 conclusion；该 commit 默认 `history_n=1` | 折叠文本可能混合模型 conclusion 与工具返回 | 未见独立 record-level verifier；工具返回也不自动证明任务语义成功 | `_format_previous_steps` 等 formatter；先用回归测试确认 action-result 步号对齐 |
| `seed_agent` | 原始回复/动作 XML 回放；通常约 3 张近期图 | 模型动作与后续 screenshot 同时存在 | Prompt 有避免重复/静止屏幕提示，未见持久旧记录审计 | message history 与 image pruning |
| `ui_venus_agent` | 当前 1 张图 + 旧 thought/action；该 commit 默认 `history_length=0`，其 Python `[-0:]` 切片实际保留全量文字，可能是切片语义副作用而非有意设计 | thought/action 是模型文本；`status=success/failed` 主要反映生成/解析流程，不是动作效果真值 | Prompt 提醒历史可能不可靠，未见独立 verifier/repairer | history slice/formatter；先固定并测试真实窗口语义 |
| `gelab_agent` | 一句累积 rolling summary，几乎不留原始消息；当前图 | summary 随模型响应生成，是聚合的自管任务状态 | 未见独立效果核验；summary 是单点聚合风险 | summary state；以 summary span 为 provenance，从 sidecar 恢复原步证据 |
| `planner_executor` | 类似 `general_e2e` 的 planning/execution 对话回放，近期图约 3 张 | 混合 planner 回复、post observation、tool/user result | 未见独立记录级 verifier/repairer | planner/executor messages 分源；计划不能当完成事件 |
| `memgui` | 当前图 + 三块自管文本；folding directive 会破坏性压缩/替换旧内容 | folding 通常在下一轮、已看到新截图后生成；仍是模型聚合记忆 | 未见独立旧记录效果核验，错误折叠可能持久化 | 折叠前保存来源 span；从 sidecar 生成 clean active view，不尝试反向展开或覆写私有折叠状态 |

### B.1 表格给出的总体结论

这些系统并不是“所有历史都只含动作前自述”，也不是“从不回看历史”。Pre-steps 实际混合了意图、后验自评、执行器结果、错误和观察；已有系统可以验证最近 transition 或反思近期轨迹。**仅在附录 A 的 26 个合并审计条目与附录 B 的 9 个 adapters 这一冻结样本中**，我们尚未发现一个同时完成以下工作的统一机制：把当前任务内任意旧 pre-step 拆成有来源的时态 claims，维护持久证据状态，用独立 task rubric 区分“事实错误”和“真实但偏航”，再在下一次 model call 前分别 `DROP/REPLACE` 错误内容、`ARCHIVE` 失活分支并生成 clean active history。

关键一手锚点：

- [MobileWorld 9 项 registry](https://github.com/Tongyi-MAI/MobileWorld/blob/0dcd0980eac64d76f498f93568a1ec0594b743c4/src/mobile_world/agents/registry.py#L24-L52)
- [MobileWorld BaseAgent 接口](https://github.com/Tongyi-MAI/MobileWorld/blob/0dcd0980eac64d76f498f93568a1ec0594b743c4/src/mobile_world/agents/base.py#L15-L54)
- [Browser-Use 上一步验证 prompt](https://github.com/browser-use/browser-use/blob/32601887cfbc9f4f1e3cad3e2b678e56aeaeaae4/browser_use/agent/system_prompts/system_prompt.md#L159-L187)
- [SeeAct 历史分析 prompt](https://github.com/OSU-NLP-Group/SeeAct/blob/2434627b196b33d1b0668a418188dd2f348883bb/seeact_package/seeact/data_utils/prompts.py)
- [Agent-S 轨迹反思 prompt](https://github.com/simular-ai/Agent-S/blob/bffdb59c60cbbb38c3a190b2e91da12039e4063c/gui_agents/s3/memory/procedural_memory.py#L119-L143)
- [AndroidWorld M3A 实现](https://github.com/google-research/android_world/blob/3e50888527ef9f29b9157ecd537e408008bb1c85/android_world/agents/m3a.py)
- [OpenAI Responses API computer-use 指南](https://developers.openai.com/api/docs/guides/tools-computer-use)

StepReflect、TSR 等属于直接近邻机制而不是上述 26 个宿主条目，故在正文 5.2 单列，避免和“可运行 agent/adapter”计数混在一起。
