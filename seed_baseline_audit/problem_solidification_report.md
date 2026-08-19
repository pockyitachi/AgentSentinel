# `seed_baseline` 中 pre-step misleading 问题的证据加固报告

审计日期：2026-08-15

## 结论先行

这轮分析只使用 MobileWorld 的 `seed_baseline`，没有混入 `pilot_baseline`、Q-Verifier、retry、GPT 或其他 run。当前证据已经足以支持一个比“agent 有时会犯错”更具体、也更可检验的经验命题：

> 在多个 Seed-2.0-Pro 自然轨迹中，模型曾经正确读取的任务事实或 GUI 状态，在支撑截图离开短视觉窗口后发生了文本漂移；这些错误 claim 继续作为原生 assistant pre-step 进入后续 request，并被后续决策明确复述或作为 premise，进而触发 GUI 操作。另有案例中，当前 GUI 已提供反证，旧 premise 仍被采用。

红队采用更严格的四项口径：**直接反证、实际进入 target prompt、target 明确采用、关键 target 没有一个已经写错的当前 GUI 可以独立解释该采用**。在这一口径下，确认了 **5/116 条非空轨迹，固定语料下界为 4.31%**。这五条不是“看到失败后倒推 history 有害”，而是在没有 current-state 强化的决策点上，旧的错误 premise 被明确复用并引发错误分支。

另有 8 个原账本案例和概率 pilot 新发现的 1 个 task，同样满足直接反证、prompt 暴露和 target uptake，但 source action 已把错误日期、金额、实体或 report 写进当前 GUI。把它们加入可得到 **15 条传播链、14/116 个 unique tasks（12.07%）**；这个宽口径只能称为 `confirmed propagation with state confound`，不能写成纯 history effect。

因此，当前最准确的结论是：自然 invalid-history uptake 已被确认，且至少 5 条轨迹具备低 state-confound 的在线拦截点；但 history-only 因果效应仍需冻结 GUI 状态后只改 prompt history 的 replay。

## 1. 冻结数据与模型配置

唯一数据源：

`/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline`

| 项目 | 数量 |
|---|---:|
| 正式任务目录 | 117 |
| 非空 trajectory | 116 |
| 有 score 的 trajectory | 115 |
| Success / Failure | 46 / 69 |
| 总 steps | 3,397 |
| 有后续决策的 source pre-steps | 3,281 |
| 全历史 source→target exposure pairs | 65,808 |

当前仓库运行配置对应 Doubao **Seed-2.0-Pro**，endpoint 为 `doubao-seed-2-0-pro-260215`。`traj.json` 没有自行保存 model name，因此论文归档时还应保存启动配置和 job metadata。

时序统一为：

```text
S_t  →  P_t  →  A_t  →  S_(t+1)
截图    模型输出   执行动作    动作后的下一观察
```

`S_t` 是执行 `A_t` 前的截图。任何“动作成功”判断都不能用 `S_t` 自证；应至少检查 `S_(t+1)`、executor result 或 backend/evaluator。

## 2. 为什么这一 history policy 会形成风险面

Seed adapter 会保留此前所有 assistant 的 `reasoning_content + content`，但 image observation 默认只保留最近 3 张。按实际 request builder 计算：

| 暴露口径 | 数量 |
|---|---:|
| immediate `P_(t-1) → decision_t` | 3,281 |
| all-history `(P_i → decision_t), i<t` | 65,808 |
| `lag≥4`，source 的前/后观察都已离开图像窗口 | 56,319 / 65,808（85.58%） |
| prompt 中至少含一条上述“文字仍在、视觉已退场”记录的 decision turn | 2,938 / 3,281（89.55%） |

这些数字不是错误率。它们说明的是：绝大多数旧文字被再次暴露时，模型已经不能同时看到生成或验证该文字的原始视觉证据。由此得到一个可检验的机制假设：

```text
正确读取事实
  → 支撑截图退出三图窗口
  → 文本记忆漂移或被重新编造
  → 错误 pre-step 继续进入 request
  → 后续决策把它当成当前 premise
```

## 3. 什么才算一条确认传播链

为了避免把所有失败都解释成 history 问题，本报告要求逐链满足以下条件：

1. **可审计 source claim**：`P_i` 中必须是事实、状态、结果或任务进展 claim；“我要点击”“我准备搜索”等 intent 不因动作失败自动变成错误历史。
2. **独立反证**：截图、文件内容、消息、地图、当前控件、link preview 或 evaluator/backend 能反驳该 claim。只有 score 0 不够。
3. **真实 prompt 暴露**：Seed request builder 的代码路径保留该 assistant text；强案例还用 `thread_*.log` 的 request dump 逐行确认 source message 确实进入 target request。
4. **target uptake**：后续 `P_t` 明确复述、接受或操作性依赖该 claim。仅仅做了相似动作但没有文本依据，最多是 candidate。
5. **行为后果**：target 沿该 premise 继续写入、发送、保存、终止或进入错误分支。

本报告把以下现象排除在 confirmed numerator 之外：

- 动作无效，但下一步正确识别并恢复；
- source 只有 intention，没有虚假的完成态 claim；
- 截图不变、重复点击、50-step 或最终失败本身；
- 当前屏幕看不到某件事，因此断言它从未发生；
- 模型自己说“我错了”，但没有独立证据确认它究竟哪里错。

## 4. 分层下界，而不是把所有案例混成一个数字

### Tier 1：直接反证 + 实际暴露 + 明确 uptake + 无错误当前状态强化

红队后最严格层共有 5 个 unique tasks：

| Task / chain | 独立反证 | 无 state-confound 的 target uptake | 错误走向 |
|---|---|---|---|
| `MastodonAdjustTootsTask` P32→P33 | 当前菜单命令是 `Bookmark`，反而否定“点它会取消” | P33 面对反向 GUI 仍采用旧 premise | 点击后实际重新 bookmark |
| `CheckInvoiceTask2` P19→P20 | S15/S16 已显示 invoice.pdf、客户邮箱、金额、日期和利率 | 当前只是空 compose GUI；P20 仍说资料不存在 | 重复 ask user，最终放弃可完成任务 |
| `PhotoManagementTask` P34→P39 | 相关月 calendar 只显示 Tokyo/Paris | Gallery/move dialog 没有 New York provenance，P39 仍沿用 | 创建 New York folder 并错误分类 |
| `MattermostDeadlineReconciliationTask` P16→P18 | channel 有四个 May deadlines，calendar 已有 matches | P18 已离开错误邮件草稿，仍只复述无关 2025-08-25 | 创建错误 missing event |
| `MattermostShiftCoverageTask` P26→P27 | May 4 明确有 `All Hands` | 当前 Mattermost GUI 不含 calendar verdict；P27 仍称无冲突 | 将有冲突请求转入 HR email 流程 |

固定语料保守下界是：

```text
5 / 116 non-empty trajectories = 4.31%
```

这不是抽样 prevalence，也不是 4.31% 的失败由 history 导致。它只表示：当前 116 条自然轨迹中，至少 5 条已经出现“错误旧 premise 在没有错误 current-state 强化时被明确采用”的可复核事件。

### Tier 2：传播成立，但 current GUI 也是替代解释

原 20 例账本中另有 8 个 `CONFIRMED_WITH_STATE_CONFOUND`：

- `CheckConferenceAndSendSmsTask1`
- `CheckSetMeetTimeTask`
- `CheckInterviewTimesTask`（只对缩窄后的 TechNova identity claim）
- `CheckConferenceAndSendSmsTask2`
- `CartInfoNotificationTask`
- `DownloadSendReceiptTask`
- `MattermostReadingGroupTask`（只对错误 arXiv link）
- `MattermostBudgetApprovalPipelineTask`

这些案例都能确认错误 claim、实际 prompt 暴露和 target uptake；但 source action 已把错误日期、实体、金额或链接写进表单，`S_target` 本身也能解释 target 为何继续。概率 pilot 新发现的 `MattermostProjectStatusReportTask` P45→P46 同属此层；`CheckSetMeetTimeTask` P22→P23 是同一 task 的第二条 state-confounded 链。

合并去重后，Tier 1 + Tier 2 是 **15 条传播链、14/116 个 unique tasks（12.07%）**。这个数字可以说明自然错误状态延续的规模下界，但不能被表述为 history text 的独立作用。

### Tier 3：边界样本，不进入 confirmed numerator

- `CheckConferenceLocationTask`：只确认“100 Main St 来自邮件”的 provenance 是假的，不能仅从邮件截图证明该地址值本身一定错误。
- `ScheduleLunchViaSmsTask`：source premise 与保存默认 1 PM 的行为一致，但 target 没有明确复述“未指定时间”。
- `TextArrivalTimeTask`：3h27 最快路线旁还有 3h53 备选，`about 3h40` 不构成无争议直接反证；source/target 数值还发生变化。
- `MastodonCalendarMultiMemosTask`：有限 feed 中没看到某 post，不能证明全局不存在。
- `MattermostTechnicalDebtTriageTask`：方向延续但精确数值继续突变，且未发送。

另有两个 `SELF_CORRECTED`（`CheckGithubInfoTask`、`ScheduleCoffeeTimeViaSmsTask`）。它们说明错误 history 并不必然导致错误写入：当前 GUI 足够清楚时，agent 有时会覆盖旧 premise。

## 5. 最贴近 Sentinel 的完整自然轨迹

### 例 1：`CheckInvoiceTask2`，已经正确读到的整组事实被改写为“不存在”

1. `S15`：Downloads 中可见 `invoice.pdf`；`S16/P16`：agent 正确读出客户邮箱、`$102,120`、Oct 4 due date 和 1.5%/month，并算出 `$104,417.70`。
2. 到 `P19`，这些截图刚好离开三图窗口。agent 突然声称“没有客户邮箱，也没有 invoice details”，然后 ask user；当前 GUI 只是空 email compose，不携带“文件不存在”这一事实。
3. `P19` 作为 assistant pre-step 确实进入 `P20` request；`P20` 再次明确说 Downloads 是空的、只能向用户索要资料。
4. 同一错误 absence premise 被 P21–P25 持续复述，最终 P26 放弃任务；它不是一次没有生效的点击，而是已知 task facts 在历史中被整体否定后持续控制后续决策。

这条链同时满足 direct evidence、prompt exposure、exact uptake 和低 state-confound，最适合做 history mask/correction replay。

### 例 2：`MattermostShiftCoverageTask`，正确事实在 19 步后翻转

1. `S7/P7`：calendar 明确显示 May 4 有 `All Hands`，agent 正确判断 Alex 的请求有冲突。
2. `P26`：在原图早已离开视觉窗口后，claim 翻转为“May 4 无冲突，可以 escalate”；`A26` 只复制请求文本。
3. `P27` 再次明确写“All Hands was May 6; May 4 has no conflict”，随后进入 Mail。
4. `P37` 发送 HR 邮件；到 `P45` 重新看到 calendar 后，agent 才恢复“May 4 有 All Hands”，但外部邮件已无法撤回。

这展示了 Sentinel 最需要处理的窗口：不是事后反思“刚才可能做错”，而是在 `P27` model call 前让旧的 no-conflict claim 不再作为 active premise。

## 6. Prompt 暴露不是推测

`traj.json` 只保存模型输出，单独看它不能证明旧输出后来进入了模型输入。因此我们另外检查了 `thread_*.log` 中的 request dump。

- 红队对 20 例扩展账本逐案检查后，20/20 都能在 source 后一轮的 `thread_*.log` request dump 中看到对应 assistant message；不同案例的分歧在反证、uptake 和 state confound，而不是“它是否真的进过 prompt”。
- 概率 pilot 的 `MattermostProjectStatusReport` 也确认 P45 在 [request line 3173](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostProjectStatusReportTask/thread_139934151435392.log:3173>)，随后 P46 在 [line 3190](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostProjectStatusReportTask/thread_139934151435392.log:3190>) 发送。

因此 prompt exposure 在这批候选中是原始 request-level 证据，而不是仅依据“代码理论上会保留历史”推断。

## 7. 50-pair 概率抽样 pilot 给出了什么

从 116 个 task 中按 outcome × trajectory length 两阶段抽取 25 个 task，再抽 50 个 immediate pairs；18 个来自 scanner hit，32 个来自 non-hit。全部完成一次人工复核：

| immediate-pair 类别 | 数量 |
|---|---:|
| confirmed propagation | 2 |
| invalid，但下一步拒绝/纠正 | 3 |
| action-failure control | 6 |
| 没发现 invalid source | 35 |
| unverifiable | 4 |

两个 propagation 是：

- `MattermostProjectStatusReportTask` P45→P46（scanner hit）；
- `CheckSetMeetTimeTask` P22→P23（scanner non-hit，同一 task 的第二条独立链）。

这个 pilot 支持三点：

1. 随机抽样也能找到 propagation，不只是定向挑失败案例；
2. scanner non-hit 中存在 positive，所以 candidate scanner 不能充当 ground truth；
3. 5 条确认 invalid source 中有 3 条被下一步拒绝/纠正，说明“检测到可能错误”不等于“一律 DROP 整条 step”。

窄口径的加权 descriptive 大约是 immediate propagation 3.9%，但当前 **不能把它写成正式 prevalence**：它只检查 `P_(t-1)`，会漏掉任意更老的 `P_i`；只有 25 个 task cluster；权重很不均，Kish effective sample size 约 13.2；而且只有单标注者。本报告只把它用于可行性、scanner 诊断和正式样本量规划。

## 8. 当前最重要的反例与混杂

### 8.1 多数自然案例不是 history-only

红队对 20 例的最终分类是：5 `STRICT_CONFIRMED`、8 `CONFIRMED_WITH_STATE_CONFOUND`、1 `PROVENANCE_ERROR_ONLY`、2 `SELF_CORRECTED`、4 `CANDIDATE`。

在 5 个 strict cases 中，4 个符合“grounding screenshot 离开三图窗口后，错误旧 premise 在无当前状态强化时被采用”；另 1 个更强，当前 GUI 已直接反驳旧 premise，agent 仍跟随 history。8 个 state-confounded cases 可以证明错误状态延续，却不能把 target action 唯一归因于 history text。论文必须单列 `state_confound`。

### 8.2 失败不是证据，成功也不排除传播

定向找到的 strict cases 都出现在失败轨迹中，但审计过采样了失败与长轨迹，且没有反事实分支，不能从这里推导 outcome effect。另一方面，`ScheduleCoffeeTimeViaSmsTask` 中错误时间曾被下一步短暂采用，之后因原短信重新出现而修正，最终 score 1。传播、恢复和最终任务成败必须分开报告。

### 8.3 当前 GUI 不是所有 claim 的 oracle

当前页面看不到旧消息、文件或离屏状态，不等于旧 claim 为假。只有直接证据足够时才可 `REFUTED`；其余必须是 `UNVERIFIABLE`。这也是 Tier 3 不进入头条数字的原因。

## 9. 下一步怎样把“存在性”升级成 prevalence 和因果

### 9.1 正式发生率研究

主单位改为 **target decision turn `t`**，而不是 immediate pair。对每个抽中的 turn，标注者同时看到：

```text
task goal
+ current S_t
+ 所有 prior text P_1 ... P_(t-1)
+ 对应的 sidecar evidence ledger
```

主标签：

```text
Y_t = 1
iff P_t 明确依赖 active history 中某个被直接证据反驳的 claim，
并据此产生错误、无必要重复、过早完成或偏离 rubric 的 GUI 决策。
```

建议 500–600 个 target turns、70–90 个 task clusters；至少 20% 双人独立复标，并对所有 positive/uncertain 做双标。主 prevalence sample 与 enriched scanner-hit taxonomy sample 分开。使用设计加权 Hájek estimator 和分层两阶段 Rao–Wu cluster bootstrap。

### 9.2 冻结状态的 history intervention

首批优先选择低 state-confound 的自然点：

1. `MastodonAdjustTootsTask` P32→P33
2. `CheckInvoiceTask2` P19→P20
3. `PhotoManagementTask` P34→P35/P39
4. `MattermostDeadlineReconciliationTask` P16→P18
5. `MattermostShiftCoverageTask` P26→P27

`TextArrivalTimeTask` 可保留为边界/negative-control replay：它没有 state confound，但 3h27 最快路线旁还有 3h53 备选，适合检验 Sentinel 是否会对近似值差异过度干预。

冻结 task、GUI state、system prompt、tools 和其他 messages，只改变目标 history span：

```text
Original
Mask invalid span
Mask + evidence correction
Oracle-clean history
```

比较下一动作、rubric alignment、完成率和错误干预率。只有这个实验才能把“observed propagation”升级为“该 pre-step 对下一决策具有因果影响”。

## 10. Proposal 中建议使用的表述

可守住的版本：

> 在 MobileWorld `seed_baseline` 的 116 条非空 Seed-2.0-Pro 轨迹中，我们发现了一个可复核的运行时风险面：历史 assistant 文字长期保留，而其支撑视觉只保留最近三张。按“直接反证、实际 prompt 暴露、target 明确采用、无错误当前 GUI 强化”的严格口径，固定语料中至少有 5 条轨迹出现 observed invalid-history uptake；若加入当前 GUI 已被 source action 写错的 state-confounded 链，则共确认 15 条链、涉及 14 个 tasks。50-pair 概率 pilot 还在 scanner non-hit 中发现传播。该结果证明自然问题存在，但不把观察关联写成 history-only 因果；后续将以 all-history target-turn 抽样估计发生率，并在冻结 GUI 状态下只改变 history 验证因果作用。

暂时不要写：

- “4.31% 是该模型的真实发生率”；
- “这些 pre-steps 导致了所有对应 task 失败”；
- “删除旧消息一定能恢复任务”；
- “14/116 个宽口径 tasks 都证明了纯 history-only effect”；
- “scanner 已经能自动检测该问题”。

## 11. 可复核产物

- [扩展 20 例证据账本](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/propagation_cases.jsonl>)
- [逐例传播复核报告](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/propagation_review_summary.md>)
- [20 例保守红队裁决](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/case_redteam.md>)
- [机器可读红队标签与去重下界](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/redteam_adjudication.json>)
- [真实 prompt 注入证据](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/prompt_exposure_evidence.md>)
- [概率抽样 pilot 报告](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/prevalence_study/README.md>)
- [50 条实际标注](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/prevalence_study/pilot_annotations_reviewed.csv>)
- [机器可读 pilot 结果](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/prevalence_study/pilot_results.json>)
- [全历史 target-turn bundles](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/prevalence_study/pilot_turn_context_all_history.jsonl>)
- [保守自动信号统计](</Users/apigo/Desktop/agent monitor/seed_baseline_audit/output/conservative_metrics.json>)

所有分析产物都位于 proposal 工作区；QR-MW 原始轨迹未被修改。
