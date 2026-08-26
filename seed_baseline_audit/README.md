# MobileWorld `seed_baseline` 历史性 pre-step misleading 初步审计

审计日期：2026-08-15

> [!IMPORTANT]
> **Historical preliminary audit.** 本目录记录的是推动 Epic 1 立项的单模型
> `seed_baseline` 早期调查。Seed 不属于 canonical 六模型 Epic 1 evaluation；
> 本文的本地标签、分母、5/116 lower bound、15-chain 宽口径及 50-pair pilot
> 不得与正式 MHR 及 observed-local-harm 结果合并或直接比较。
>
> Epic 1 已完成六种 history representation、共 702 个 model-task cases 的正式审核。
> 当前定义和结果以
> [六模型正式报告](../MobileWorld/docs/misleading_history_audit_report.md) 为准，
> 最新阶段以 [Project Status](../mobileworld_audit_handoff/STATUS.md) 为准。本文后续
> “建议的下一步”保留为历史设计记录，不代表当前 roadmap。

## 结论先行

在当前 `seed_baseline` 日志中，**已经能确认存在“错误的 pre-step claim 被后续决策明确复用”的自然案例**。第一轮 6 例之后，我们扩展并红队复核了 20 个候选，又完成了 50 个概率抽样 immediate pairs 的单人 pilot。

按“直接反证、实际 prompt 暴露、target 明确采用、无错误 current-GUI 强化”的严格口径，当前固定语料至少有 **5/116 条非空轨迹（4.31%）**出现 observed invalid-history uptake。另有 state-confounded 链；与概率 pilot 去重后，宽口径共确认 15 条链、涉及 14 个 tasks，但这些不能隔离 history text 与已经写错的 GUI state。

这些数字仍不是总体发生率，也不能仅凭观察日志声称“删除该 pre-step 一定能挽救任务”。50-pair pilot 只审紧邻上一条、单标注且有效样本量很小；原始轨迹也没有 KEEP/DROP 的反事实分支。

因此，当时最准确的结论是：

> `seed_baseline` 中存在可复核的 invalid-history propagation；它值得成为 Sentinel 的首个研究对象。正式发生率研究应以 all-history target decision turn 为单位，对 500–600 turns 做概率抽样；因果部分应对 5 个低 state-confound 自然点做冻结状态的 history intervention replay。

当时的合并结论、证据分层和 proposal 表述见
[问题加固报告](problem_solidification_report.md)；20 例逐案红队见
[case_redteam.md](case_redteam.md)。

## 1. 数据范围

本审计当时只读取以下 repo-external source tree（未随 Git 发布）：

`/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline`

没有读取 `pilot_*`、`*_qverifier_*`、`*_gpt_*`、retry、smoke 或 task backup。117 个正式任务目录中的 run 对象也都不含 `reflections` 或 `reflection_stats` 字段。

| 项目 | 当前文件系统快照 |
|---|---:|
| 正式任务目录 | 117 |
| 非空 trajectory | 116 |
| 总 step 数 | 3,397 |
| 有可解析 score | 115 |
| Success / Failure | 46 / 69 |
| 正好 50 steps | 35，全部 score 0 |

两个缺失项应单独处理：`ThanksgivingPrepTask` 是初始化 HTTP 500 后的空 trajectory；`MastodonShareLocationTask` 有 6 steps，但没有 `result.txt`。二者都不能自动算作 failure。

当时 QR-MW 的实验说明和启动命令把该组 agent 标为
**Doubao Seed-2.0-Pro**，具体 endpoint 为
`doubao-seed-2-0-pro-260215`，不是 Seed Pro 1.0。那些 repo-external
启动材料未随本仓库发布；`traj.json` 本身也没有保存 `model_name`。

另一个需要保留在 provenance ledger 里的事实是：当前正式目录按 `result.txt` 得到 46 个 success，而仓库 `report.md` 写的是 baseline 48/117。本文没有为了对齐 aggregate 而引入 backup 或其他 run；本审计所有分母均以当前正式目录为准。发表前需要单独解释这 2 个 task 的差异。

## 2. 审计单位与判定口径

对于第 i 步：

- `S_i`：执行动作前、供 agent 决策的截图；
- `P_i`：该步模型生成的完整 prediction，包括 reasoning 和 tool call；
- `A_i`：解析并实际执行的动作；
- `S_(i+1)`：`A_i` 后、下一次决策前的截图；
- 对任意后续决策 t，`P_i` 都是一个可能暴露给模型的 pre-step。

这里必须把四类现象分开：

1. **Invalid history**：`P_i` 中的事实、状态或 GUI 语义 claim 被可见证据直接反驳；
2. **Observed misleading propagation**：后续 `P_t` 明确复述或依赖该 invalid claim，并据此采取错误、重复或过早终止的行为；
3. **Invalid but recovered**：错误 claim 暴露过，但后续 observation 使 agent 在造成关键错误前纠正；
4. **Action failure control**：动作没有生效，下一步如实识别并恢复；若没有错误的完成态 claim，它不是 misleading history。

本报告中的 `confirmed misleading` 只表示第 2 类的观察证据链成立，**不等于反事实因果已经证明**。

## 3. 为什么这个 run 特别适合研究该问题

Seed adapter 的默认 `history_n=3`，见当前
[seed_agent.py](../MobileWorld/src/mobile_world/agents/implementations/seed_agent.py)。
构造下一次消息时，它重新加入历史 assistant response 的 `content` 和
`reasoning_content`，随后只裁掉超过最近 3 个的 image observation。

按这个实际 history policy 计算：

| 暴露分母 | 数量 |
|---|---:|
| 有后续决策的 source pre-step `P_i` | 3,281 |
| Immediate `P_(t-1) → decision_t` pair | 3,281 |
| 全历史暴露 pair `(P_i, decision_t), i<t` | 65,808 |
| `lag≥4`、源 step 前后截图均已不在 prompt 的暴露 | 56,319（85.58%） |
| 至少包含一个上述旧文本的 decision turn | 2,938 / 3,281（89.55%） |

这些数字不是错误率。它们说明的是一个很具体的风险面：**大部分历史文本暴露发生时，生成该文本的视觉证据已经不再和它一起出现在 prompt 中。** Sentinel 的 sidecar evidence store 因而不是附加功能，而是这个 run 上做长期 claim 审计的必要条件。

## 4. 全量自动候选扫描

扫描器只负责召回候选，不直接生成 misleading 标签。它使用明确错误/自纠语言、非例行动作重复、短动作 cycle、高相似 reasoning、相邻截图静止和失败终止等信号。

宽召回扫描在 95/116 条非空轨迹中写出 1,533 条**重叠候选行**。一条 source/evidence pair 可能被多个规则重复写入，所以这个数字既不是 unique claim 数，也不是发生率。

更保守的量化如下：

| Candidate signal | Success tasks / events | Failure tasks / events | 合计 |
|---|---:|---:|---:|
| 连续完全相同的非例行动作 | 5 / 5 | 30 / 86 | 35 / 91 |
| 连续点击同一近似目标 | 8 / 10 | 40 / 128 | 48 / 138 |
| 长度 2–4 的短动作块立即重复 | 2 / 2 | 11 / 51 | 13 / 53 |
| 高相似 reasoning pair | 6 / 8 | 21 / 60 | 27 / 68 |
| 明确承认 mistake/wrong page/confusion 等 | 20 / 71 steps | 57 / 472 steps | 77 tasks / 543 steps |

另外，34/116 个任务出现过完全相同的相邻 PNG，共 83 个 pair；排除 `ask_user` 和 `wait` 后剩 52 个 `action → exactly unchanged screenshot`。成功组为 1.11 次/100 steps，失败组为 1.65 次/100 steps，差异很弱。这反过来说明：**static GUI 不能自动等同于 misleading。**

35 个 50-step 任务全部失败，也不能把 50 steps 自动解释成 loop。例如 `MastodonMallPurchaseCommodityTask` 到第 50 步仍在一条较长但正常的下单链路上。

## 5. 人工确认的自然案例

第一轮人工复核定向检查了 9 个高置信候选：6 个形成干净的 invalid-claim → downstream-use 证据链，2 个属于较长程的 history pollution/off-track，1 个是 invalid-but-recovered 成功对照。这个 6/9 只是定向复核 yield，不能外推到全部 3,281 个 pre-step。

### 5.1 六个干净的 observed-propagation 案例

| Task | Source → target | 被反驳的 pre-step | 后续如何复用 | 结果 |
|---|---|---|---|---|
| `CheckConferenceAndSendSmsTask1` | s10 → s11–12 | s10 把 Paris 日期写成 `05/20/2025,05/27/2025`；repo-external S3 明确是 Oct 11–15 | s11 称其为 “correct dates” 并发送；s12 再次复述 | score 0，正确日期短信不存在 |
| `CheckSetMeetTimeTask` | s6 → s7–9 | repo-external S3 邮件是 Nov 15, 3 PM；s6 却改成 Nov 1, 10–11 AM | s7 接受 Nov 1，s8–9 进入错误日期的创建流程；之后日期继续漂移 | score 0，calendar event 错误 |
| `ScheduleLunchViaSmsTask` | s16 → s17–18 | repo-external S4 写明 tomorrow 11 AM/约1小时；s16 却说“没有指定时间”，表单仍是默认 1 PM | s17 直接保存；s18 把 1 PM 事件当作完成 | score 0，缺少 11–12 事件 |
| `CheckInterviewTimesTask` | s20 → s21–28 | 三封邮件分别是 Amazon/Google/Meta；s20 凭空生成 TechNova、Nov 5、10–11 | 后续输入 TechNova，并配置和保存整条虚构事件 | score 0，calendar events 错误 |
| `MastodonAdjustTootsTask` | s32 → s33–34 | s32 混淆 favorite 与 bookmark；repo-external S33 菜单写 `Bookmark`，说明当前已未收藏 | s33 却说点击会取消收藏，实际重新收藏；s34 又声称已移除 | score 0，两个 status 仍 bookmarked |
| `CheckConferenceLocationTask` | s10 → s11、s18–20 | repo-external S6 邮件只写 Harvard Square Hotel；s10 凭空归因为 100 Main St；task evaluator 的目标地址是 [110 Mt Auburn St](../MobileWorld/src/mobile_world/tasks/definitions/gmail/check_conference_location.py) | s11 选择错误地点；s18 再次称地址“from the email”；最终计算错误路线 | score 0；同时漏做 SMS milestone |

最适合讲 proposal 的单个例子是 `CheckSetMeetTimeTask`：证据、错误 claim 和下一步 GUI 行为最短、最清楚。

原始证据是：邮件画面写着 **November 15 at 3 PM**。agent 在 s3 其实读对了；但到 s6，旧图已离开短视觉窗口，prediction 突然说“邮件是 November 1, 10–11 AM”，并点击 Nov 1。下一步 s7 没有重新查邮件，而是接受“现在已经在 Nov 1”并继续创建事件。Sentinel 若保留 S3 的 evidence ledger，就可以在 s6 输出：

```json
{
  "source_step": 6,
  "claim": "email_date_time = Nov 1, 10:00-11:00",
  "verdict": "REFUTED",
  "evidence": "S3: November 15 at 3 PM",
  "prompt_operation": "MASK_AND_CORRECT"
}
```

它不是泛泛提醒“再想想”，而是在下一次 model call 前把错误 span 从 active history 中屏蔽，并把带证据的更正放回同一位置或紧邻位置。

### 5.2 一个成功恢复对照

`ScheduleCoffeeTimeViaSmsTask` 的 s8 把 9:10 AM 邀请错误改成 3 PM，s9 又按
3 PM/PTO 的 premise 继续；但 repo-external S10 重新展示原短信后，agent 在 s10
恢复到 9:10 AM，并结合 S8 calendar 的 9–11 AM 冲突作出正确回复，最终 score 1。

这个例子很重要：invalid pre-step 不等于必然失败，也不等于应该删除整条 record。新的强证据已经纠正错误时，Sentinel 应把旧 claim 标为 `SUPERSEDED/REFUTED`，保留该 step 中仍正确的动作和实体信息，而不是无条件删除整轮。

### 5.3 应排除的动作失败对照

`SetAlarmTask`、`CheckEventTimeTask` 和 `AdjustFontIconMaximumTask` 都出现过误点或 drag 未生效，但下一步根据新截图识别并恢复。如果源 `P_i` 只表达“我将点击/拖动”，而没有声称“已经成功”，这类样本应标为 `ACTION_FAILURE_ONLY`，不进入 misleading history 分子。

## 6. 历史设计推论

本节是 Epic 1 和正式 G1 contract 之前的方案草图，不代表已经实现的 runtime 行为，
也不是当前 G1.2 的权威 operation contract。旧的 MASK/CORRECT/LOW_RELEVANCE
术语仅用于保留设计演进；当前 curated transformation 语义与 phase boundary 必须以
[handoff decisions](../mobileworld_audit_handoff/DECISION_LOG.md) 为准。

用户最初的直觉是对的：Sentinel 最终要 guide agent 的主要方式，确实是**在下一次 model call 前决定哪些 pre-step 内容还能进入 prompt**。但从这些真实轨迹看，操作粒度不能只到整个 step。

一个 `P_i` 往往同时包含：正确的页面身份、错误的日期、合理的下一目标和具体动作。如果整轮 DROP，可能连有用信息也一起删除。因此 MVP 更稳妥的输出是 claim/span 级 prompt view：

| Sentinel verdict | Prompt renderer 的行为 |
|---|---|
| `KEEP` | 原样保留有证据支持的 span |
| `MASK` | 不把已确认错误且会误导当前决策的 span 注入下一次 prompt |
| `CORRECT` | 用带 evidence reference 的更正替代或紧邻错误 span |
| `LOW_RELEVANCE` | 内容可能为真，但属于已偏离 rubric 的支线；降低其 active-context 权重 |
| `ABSTAIN` | 证据不足，不删不改 |

运行时真正交给 actor 的产物不是一个抽象风险分数，而是：

```text
filtered_history_for_next_prompt
+ evidence-grounded corrections
+ current rubric milestone state
```

以 `CheckSetMeetTimeTask` 为例，原始历史中的：

```text
The email said November 1, 10:00–11:00 AM.
```

不会再进入 actor 的 active prompt；它会被渲染为类似：

```text
[CORRECTION for step 6]
The date/time claim “November 1, 10:00–11:00 AM” is refuted.
Evidence S3: the email says November 15 at 3:00 PM.
Use November 15 at 3:00 PM for the calendar milestone.
```

这比 reflection 的区别也因此很清楚：reflection 通常生成一段新的建议；Sentinel 维护的是**哪一个旧 claim 仍可作为下一步 premise**，并实际控制下一轮看到的 history view。

## 7. 当前不能声称什么

1. 不能把 1,533 candidate rows 称为 1,533 次 misleading；
2. 不能用 6 个确认案例除以 9 个定向案例来报告自然发生率；
3. 不能因 task 失败、到 50 steps、重复点击或截图不变就标 misleading；
4. 不能从观察日志直接推出“删除历史会提高成功率”；
5. 不能只检查 scanner hit。若不审查 non-hit，就只能报告确认案例下界和 scanner precision。

## 8. 历史上建议的下一步实验

以下方案没有按本文写法执行，保留它们只是为了记录 Epic 1 之前的研究设计演进。
当前 G1 protocol 与 admission gate 以 handoff 目录中的 locked 文档为准。

### 8.1 先估计自然发生率

主单位改为 3,281 个带历史的 target decision turns；每个抽中 turn 都要检查全部 `P_i, i<t`，不能只审 `P_(t-1)`。建议按 task、success/failure、trajectory 长度和 scanner hit/non-hit 两阶段抽样 500–600 turns，覆盖约 70–90 个 task clusters；至少 20% 双人复标。每个 turn 标：

`content_status × downstream_use × error_type × KEEP/MASK/CORRECT/ABSTAIN`

这样才能报告 invalid-history prevalence、observed propagation prevalence 和 scanner recall/precision。若只审高置信候选，只能报告 observed lower bound。

### 8.2 再做冻结状态的因果 replay

首批可用 5 个干净 decision point：

1. `MastodonAdjustTootsTask` s32 → s33
2. `CheckInvoiceTask2` s19 → s20
3. `PhotoManagementTask` s34 → s35/s39
4. `MattermostDeadlineReconciliationTask` s16 → s18
5. `MattermostShiftCoverageTask` s26 → s27

对每个点冻结 task、GUI state、system prompt 和所有其他 messages，只改变目标 history span：

- `Original`
- `Mask invalid span`
- `Mask + evidence correction`
- `Oracle history`

比较下一动作是否回到 rubric、后续 completion、误修和额外 token/latency。多 seed 重复之后，才能把“观察到传播”升级为“该 pre-step 对决策具有因果影响”。

## 9. 审计产物

- [全量扫描脚本](scan_seed_baseline.py)
- [保守 repetition/static/revision 量化脚本](quantify_baseline_signals.py)
- [数据与候选摘要](output/summary.json)
- [保守量化完整结果](output/conservative_metrics.json)
- [全部 step ledger](output/steps.csv)
- [自动候选 ledger](output/candidates.csv)
- [人工案例账本](output/manual_review.json)

以上 derived artifacts 现保留在本目录；repo-external QR-MW 原始日志未被修改，
也没有随 Git 发布。
