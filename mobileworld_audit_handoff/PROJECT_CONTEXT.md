# MobileWorld History Audit：项目背景与研究口径

## 0. 这份文件的用途

这是一份可脱离原聊天记录阅读的服务器端 handoff。它说明当前研究到底要验证什么、哪些结论已经确定、哪些数字只是 preliminary observation，以及为什么现在要先实现 raw collector，而不是完整 Sentinel。

**当前阶段的一句话目标：**

> 在不改变 agent 行为的前提下，无损记录 MobileWorld agent 每次真实模型调用的完整输入/输出和对应 GUI transition，随后离线、可重复地衡量错误或失效 pre-step 被再次注入、被模型采用以及伴随下游伤害的频率，从而判断这个研究问题是否足够普遍和严重，能否支撑 proposal 的 motivation。

这不是当前阶段的目标：在线发现错误、过滤 prompt、生成 rubric、纠正 agent、提高成功率。这些属于未来 Sentinel 阶段。

---

## 1. 研究起点

项目关注 GUI agent 的 **task-local history**，不是 RAG、长期记忆或外部 experience knowledge。现代 GUI agent 经常把之前步骤的 reasoning、action、conclusion、summary 或 folded memory 放回下一步模型输入。问题在于：

1. 旧文字可能在生成时就错误，例如误认页面或虚假声称动作成功；
2. 旧文字可能曾经正确但当前已经过期；
3. 轨迹可能已经偏离目标，旧步骤仍持续强化偏航；
4. history 的文字通常比产生它时的旧 GUI 截图保存得更久；
5. summary/folding 还可能在重写历史时引入新错误或错配 action/result；
6. 后续模型可能将这些内容当成事实前提，继续采取错误动作或过早结束任务。

当前研究要先用真实运行数据回答：这类现象是否自然发生、发生多频繁、强度如何、是否集中在失败任务中，以及是否跨不同 history representation 存在。

---

## 2. 统一符号与严格时序

全项目统一使用以下符号，避免把模型输出、动作和环境状态混为一谈：

| 符号 | 含义 |
|---|---|
| `T` | task instruction |
| `S_t` | 第 `t` 次决策前 agent 看到的当前 GUI observation，至少包含 screenshot；还可能含 tool/user result |
| `H_t` | adapter 在第 `t` 次调用中实际渲染进模型输入的历史内容 |
| `I_t` | 第 `t` 次模型调用的完整 request：system/tool/task、`H_t`、`S_t`、参数等 |
| `P_t` | 第 `t` 次 `agent.predict()` 返回给 runner 的 exact prediction；它可能由一个或多个 provider raw responses 组装或转换而来 |
| `A_t` | 与 `P_t` 一同返回、交给 MobileWorld 环境执行的 parsed structure action |
| `R_t` | action transport/tool/user result；若没有则为空 |
| `S_{t+1}` | 执行 `A_t` 后、等待环境稳定后得到的下一 GUI observation |

因果可用时序是：

```text
T + H_t + S_t
      ↓ render
     I_t
      ↓ model
     P_t  (agent.predict return)
      ↓ parse
     A_t
      ↓ environment
 R_t + S_{t+1}
```

这里的 **pre-step** 指由某个更早步骤 `i < t` 产生、并且在 `I_t` 中实际再次暴露给模型的历史记录或其派生表示。adapter 内部保存过但没有进入 `I_t` 的内容不能算 exposure。Planner/grounder 等一个 decision 含多个物理模型调用时，raw provider responses 由各自的 `model_call_id/request_id` 表示；不要强行把它们都等同于单一 `P_t`。

在 `S_{t+1}` 出现前，不能用它判断 `P_t`；得到 `S_{t+1}` 后，才可以离线核验 `P_t` 中关于动作结果或 GUI 状态的 claim。这个限制同样适用于未来的在线 Sentinel。

---

## 3. 当前 motivation study 要回答的研究问题

### RQ1：错误 history 是否真实进入模型输入？

需要同时证明：

- 某条旧 history claim 已被 `S_i`、`A_i`、`R_i`、`S_{i+1}` 或后续状态证据判为错误、失效或偏航；
- 它在目标决策点 `t` 的真实 request `I_t` 中仍然存在。

仅凭源码推测 agent “应该会保留历史”不够；仅凭 trajectory 中有一条错误输出也不够。必须有真实 prompt exposure。

### RQ2：模型对错误 history 的 uptake 有多强？

错误内容被注入本身已经是 noise，但不能把所有 exposure 都说成“模型被误导”。需要区分没有可见采用、行为上可能一致、明确引用。

### RQ3：错误 history uptake 是否伴随可观察伤害？

检查其后是否出现错误 action、重复 action、无意义绕路、过早结束、不可恢复偏航等。自然轨迹只能支持“伴随/传播”的观察性结论，不能自动证明严格因果。

### RQ4：问题是否跨 history representation 出现？

官方 MobileWorld 的 9 个内置 adapter 并非同一种 history 机制。研究必须覆盖 raw replay、flat progress、rolling summary、hybrid collapse 和 structured folding，而不能把 Seed 的实现泛化成全部 agent。

---

## 4. 错误内容与 uptake 必须分轴标注

不要用一个含混的 `misleading=true/false` 同时表达“历史错了”“模型用了”“产生伤害”。离线评测至少拆为三个基础字段：

```text
history_status     # 历史内容自身的有效性
uptake_evidence    # 当前模型输出是否采用了它
downstream_effect  # 此后是否出现可观察伤害
```

### 4.1 `history_status` 候选类别

以下是当前 working taxonomy，不属于 raw collector schema，可在 derived evaluation 中版本化修改：

| 类别 | 含义 |
|---|---|
| `SUPPORTED` | 可见证据支持该历史内容 |
| `FALSE_CLAIM` | 关于 GUI/任务事实的陈述从一开始就是错的 |
| `FALSE_SUCCESS` | 声称动作或子目标已经完成，但 post-state/result 不支持 |
| `STALE_STATE` | 当时可能正确，但到目标决策点已经失效 |
| `GUI_MISINTERPRETATION` | 错认页面、控件、选中状态、内容或导航结果 |
| `TRUE_BUT_OFFTRACK` | 陈述可能为真，但所属轨迹已经偏离 task/rubric 目标 |
| `SUMMARY_CORRUPTION` | flatten/summary/folding 后出现原始步骤没有或不支持的内容 |
| `RESULT_MISALIGNMENT` | action、tool/user result 或 step 在 history 中配错 |
| `UNVERIFIABLE` | 现有可观察证据不足，必须 abstain |

一条 exposure 可具有多个细分类，但应有一个明确的顶层可核验状态，例如 `SUPPORTED / REFUTED / STALE / OFFTRACK / UNVERIFIABLE`。

### 4.2 强弱分层

用户明确要求：**错误 history 注入本身就是弱噪音；明确采用错误 history 才是强噪音。** 推荐派生以下四级严重度：

| 派生级别 | `uptake_evidence` | 判定要求 |
|---|---|---|
| `WEAK_NOISE` | `NO_OBSERVED_UPTAKE` | 错误/失效 history 实际进入 `I_t`，但 `P_t/A_t` 没有可见采用证据 |
| `POSSIBLE_MISLEAD` | `BEHAVIOR_CONSISTENT` | `A_t` 或行为与错误 history 一致，但 `P_t` 没有明确把它作为前提；存在其他解释 |
| `STRONG_MISLEAD` | `EXPLICIT_USE` | `P_t` 明确引用、复述或依赖该错误 history 作为行动前提 |
| `EXPLICIT_HARM` | `EXPLICIT_USE` + harmful effect | 明确采用后又出现可观察的错误/重复 action、过早结束或其他伤害 |

推荐独立的 `downstream_effect` 值：

```text
NO_VISIBLE_HARM
UNNECESSARY_ACTION
WRONG_ACTION
REPEATED_ACTION
PREMATURE_TERMINATION
OFFTRACK_CONTINUATION
RECOVERED
UNKNOWN_EFFECT
```

为什么要分开：

- `WEAK_NOISE` 证明 context 中存在冲突或无效信息，但不能声称模型用过；
- `BEHAVIOR_CONSISTENT` 是弱证据，因为当前 GUI 本身也可能解释相同行为；
- `EXPLICIT_USE` 是自然轨迹中最强的 observed propagation 证据；
- 即使 `EXPLICIT_USE` 与失败同时出现，仍不要使用“它导致失败”的严格因果语言，除非以后做固定 `S_t`、固定模型、只改 history 的 paired replay。

---

## 5. 已有 Seed baseline 数字：保留原始账本与两层审计口径

此前在 Seed baseline trajectory 上完成过一轮人工/半人工 preliminary screening。这里必须同时保留**实际结果文件口径**和聊天中曾采用的**历史 headline 口径**，不能把二者混成一个成功率：

```text
正式 task 目录：117
非空 trajectory：116
有 numeric score：115
score = 1：46
score = 0：69
缺 result / 未评分：2

实际已评分成功率：46 / 115 = 40.00%
全部 117 目录中的 confirmed-success fraction：46 / 117 = 39.32%

历史 headline（仅为延续此前记录）：48 / 117 = 41.03%
其中 48 = 46 个 score=1 + 2 个未评分；它不是“48 个已确认成功”

失败任务中的 broad pilot count：14 / 69 = 20.29%
折算非空 trajectory：14 / 116 = 12.07%
折算全部目录：14 / 117 = 11.97%
严格、低 current-state confound 的 Tier-1：5 / 116 = 4.31%（亦为 5 / 69 个失败任务）
```

旧 Seed corpus 还有一组只描述**暴露机会**、不描述错误率的结构性统计：3,397 个 decision steps；3,281 个带至少一个 earlier prediction 的 target decisions；若把每个 `(source pre-step i, target t)` 计为一次 exposure，共 65,808 对。Seed 的实际 prompt builder 保留所有旧 assistant text、但最多保留含当前图在内的最近 3 张截图，因此其中 56,319 个 lag ≥ 4 exposures 的 source 前/后视觉证据均已离开当前图片窗口，2,938 个 target decisions 至少包含一条这种旧文字。它说明可审计表面积很大，**不代表这 56,319 条是错误或造成误导**。

早期自动扫描曾因重复动作、静止页面、revision cue、长轨迹等召回大量候选；这些信号只适合 candidate retrieval。随后对 20 条证据链红队后，分类为 5 strict、8 state-confounded、1 provenance-only、2 self-corrected、4 candidate。第一次“看起来很多”和最终 strict 只剩 5 并不矛盾：前者优化召回，后者要求直接反证、真实 exposure、明确 uptake，并排除当前 GUI 自身的替代解释。

必须同时保留以下限制：

1. `14/69` 是 69 个 score=0 trajectory 中，broad pilot ledger 观察到至少一条传播信号的 **14 个 unique tasks**，不是“14 个失败已经被证明由错误 history 导致”。其组成是 5 个 strict、8 个 `CONFIRMED_WITH_STATE_CONFOUND`、1 个 `PROVENANCE_ERROR_ONLY`；因此它不能直接作为严格 invalid-history prevalence；
2. `5/116` 才是当前旧语料上最保守的 strict observational lower bound：旧 claim 有直接反证、确实进入后续 prompt、target 明确采用，而且关键 target 没有呈现同一错误 premise 的 current-GUI reinforcement。它仍然只是 observed propagation，不是 intervention-based causation；
3. `14/116`、`14/117` 和 `14/69` 是同一个 broad numerator 面对不同问题的三个分母，均应保留分母名称，不能互换：分别回答非空 trajectory prevalence、全部目录占比、failed-task conditional prevalence；
4. `48/117` 是用户此前要求保留的历史 headline，但其 numerator 含 2 个未评分任务。所有论文表格和新分析必须将 `46 success / 69 failure / 2 no_result` 分列；
5. 当前旧日志虽然可以从源码和部分 thread dump 推断 exposure，却没有为每一步保存完整、未裁剪的真实 request，因此全部 case 都要由新 collector 重审；
6. 后续要分别重算 invalid exposure、possible uptake、explicit propagation、explicit harmful propagation，不能继续用一个 `14` 混合表达所有强度；
7. 这些数字只能作为 Seed baseline 的 pilot motivation，不能直接外推到全部 9 个 adapter。

还存在重要的 **state confound**：在自然 trajectory 中，当前模型同时看见当前 GUI `S_t` 和旧 history `H_t`。即使 `P_t/A_t` 与错误 history 一致，也可能是当前 GUI 状态本身诱导了相同行为。因此 `BEHAVIOR_CONSISTENT` 只能算 possible uptake；即使明确复述旧 history，也只能说 observed propagation。若要隔离 history 的因果作用，未来仍需固定 task、`S_t`、model 和 decoding，只改变 history 的 paired replay。

新的 collector 的首要价值之一，就是重新裁决这 14 个 broad cases：哪些旧 claim 确实进入 `I_t`、哪些可被直接判为错误或失效、哪些只是 state/provenance confound、哪些达到 `EXPLICIT_USE`，以及哪些伴随明确下游伤害。

---

## 6. 为什么 collection 与 evaluation 必须彻底分开

### 6.1 Collection 的职责

Collection 只忠实记录运行时事实：

- 最终、实际发送给 provider 的完整 request `I_t`；
- request 内 messages 的顺序、role、content part、图片位置、tool schema/result、参数；
- provider 返回的原始 response、所有 retry；对 stream 保存 chunks 和最终拼接结果；
- 当前 observation `S_t`；
- raw prediction `P_t` 和 parsed action `A_t`；
- action 后的 result `R_t` 与 post observation `S_{t+1}`；
- task score/reason；
- run config、agent/model、repository commit、环境和 schema 版本；
- 可选的 adapter render 前内部状态快照，用于追查 summary/folding provenance。

Collection 不应该知道什么是错误、强噪音、rubric 或 Sentinel verdict。

### 6.2 Evaluation 的职责

Evaluation 在离线 derived layer 中做：

- 重建哪些历史片段实际暴露在每个 `I_t`；
- claim extraction；
- history validity 标注；
- weak/possible/strong/harm 分层；
- 自动 verifier 与人工复核；
- 指标、表格和图；
- 将来改变 taxonomy、阈值或 rubric 后重新计算。

### 6.3 这样设计的直接收益

第一次分类规则不合理时，只需产生 `derived/schema_v2`，无需重新运行昂贵且受服务器/模拟器约束的任务。raw 层应是 append-only、immutable、label-free；derived 层可以删除并从 raw 重建。

推荐概念结构：

```text
Raw event collection (immutable)
       ↓
Normalization / exposure reconstruction
       ↓
Claim extraction
       ↓
Labels schema_v1 / schema_v2 / ...
       ↓
Metrics / tables / figures
```

“Lossless” 在这里的工程含义是：能够从保存的数据重建 provider 实际收到的 request 和 MobileWorld 当时可观察到的 transition。图片可从 JSON 中提取到 content-addressed blob，但必须保留原始 bytes、MIME type、hash 和在 request 中的位置；不能只保存 pretty print 或重新编码后的近似图。

---

## 7. 当前阶段明确不做的内容

为了防止服务器实现偏离目标，当前 collector PR **不得**包含：

- 在线 Sentinel classifier；
- `KEEP / DROP / REPLACE / ABSTAIN` 干预；
- rubric generator 或 rubric state tracking；
- 任何 prompt/history 修改；
- 自动纠错、replan 或 reflection hint；
- 用 evaluator label 反向影响 agent；
- 以成功率提升作为这一阶段的完成标准；
- 把 AndroidControl 改成主要实验平台。

未来 Sentinel 可能复用 collector 的 event schema/evidence ledger，但不能因此把 Sentinel 逻辑提前塞进 collection layer。

---

## 8. 当前运行环境和 handoff 状态

- 本地 Mac 不能实际运行完整 MobileWorld Android/容器实验；代码实现与 smoke tests 需要在服务器完成。
- 研究代码基准固定为官方 MobileWorld `main` commit：`0dcd0980eac64d76f498f93568a1ec0594b743c4`（2026-08-04）。
- 这个快照有 9 个内置 registered adapters；详细代码审计见 `MOBILEWORLD_CODE_AUDIT.md`。
- 当前应先实现 event-sourced、lossless、label-free collector，再在服务器做少量 smoke runs 验证 9 个 adapter 的 request/transition 均能被捕获。
- collector 稳定以后，主自然轨迹采集先选 5–6 个代表 agent 覆盖五类 history representation：建议 `seed_agent`、`general_e2e`、`qwen3vl`、`gelab_agent`、`gui_owl_1_5`、`memgui`；`mai_ui_agent`、`planner_executor`、`ui_venus_agent` 在标注体系稳定后扩展。这里的 5–6 不是 collector 支持范围，collector 仍须支持全部 9 个。
- 主采集之后才运行独立 offline evaluation pipeline；AndroidControl 只作为后期 synthetic misleading-history 的外部验证，不承担当前自然发生率证据。

---

## 9. 当前研究可以安全声称什么

在新实验完成前，最稳妥的表述是：

> MobileWorld 的内置 GUI agents 使用多种 task-history representation。源码显示历史文字、action conclusion 或 summary 经常在与其原始 GUI 证据不同的保留窗口中继续进入后续决策；旧 Seed broad screening 在 `14/69` 个失败任务中记录到至少一条 pre-step propagation signal，其中严格、低 current-state-confound 的固定语料下界为 `5/116` 个非空 trajectories。由于旧日志未无损保存每次真实 request 和显式 post-state transition，下一步先构建零干预 audit collector，对问题的 exposure、uptake 与 harm 进行可复核测量。

当前不应声称：

- “所有 agent 都和 Seed 一样保存历史”；
- “14 个失败都是 prev step 导致的”；
- “只要旧 history 在 prompt 中，模型就被误导”；
- “MobileWorld 已经证明 Sentinel 能提高成功率”；
- “现有系统完全不利用后续截图”——MemGUI 有 actor-managed folding，但不等于独立 verifier。

---

## 10. 未来 Sentinel 的已讨论形态（不是当前 deliverable）

如果 motivation study 证明问题足够 solid，未来 Sentinel 才会在 `S_t` 到达后、构建下一步 `I_t` 前运行。用户对它的核心预期不是只输出一段解释，而是：**若某个 pre-step claim 已有充分直接证据证明无效，就不再把该无效内容注入当前 actor prompt**。实现上应以 claim/span 为粒度，避免一条 record 同时含正确和错误信息时整条误删；证据不足则保留并 abstain。

未来机制有两个正交输出轴：

1. **History validity**：旧 claim 对其时间点是否 `supported/refuted/unverifiable`，以及现在是否仍 active；这一路可产生 `KEEP / DROP(MASK) / REPLACE(CORRECT) / ABSTAIN`。
2. **Trajectory alignment**：真实 history 也可能忠实记录了一条偏航路线；这一路只判断相对 task milestone 是 `on-track/deviating/off-track/unknown`，不能用 rubric 单独把事实 claim 判假。

Rubric 的统一口径是：任务开始时根据 `T` 生成一份**版本化、允许多条合法路径的 milestone template**；运行中通常更新的是每个 milestone 的 `pending/in_progress/satisfied/violated/unknown` 状态，而不是每一步重新生成一套目标。若发现 rubric 本身错误或任务出现新约束，应显式产生新版本并保留 provenance，不能静默改写旧 rubric。当前 collector 不生成、保存或执行 rubric verdict。

这也解释了它与一般 reflection 的区别：已有 GUI agent 可以检查最近一次 transition 或反思一段有界轨迹，但常由 actor 自己生成一次性自评。未来 Sentinel 设想的是旁路 evidence ledger、可追溯的 record/claim 级状态、对较早 history 的持续复核，以及明确影响后续 prompt 的 gate。不能因此声称现有 agent “完全不回看历史”；本项目要通过调研和实验验证的是这组能力的组合差异。

未来还需配套 fixed-state paired replay，固定 task、`S_t`、model 和 decoding，只改变 history，才能测试删除错误 history 是否因果性改善下一步 action。当前 handoff 只为这条未来路线打下可信数据基础。
