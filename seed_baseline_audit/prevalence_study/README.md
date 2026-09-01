# `seed_baseline` historical prevalence pilot：概率抽样与旧版主研究设计

审计日期：2026-08-15

> [!IMPORTANT]
> **Historical sampling pilot.** 这项 50-pair study 只用于测试
> `seed_baseline` 上的抽样与标注机制，不是 canonical Epic 1 prevalence study。
> 本文建议的 500–600-turn 设计没有按原方案执行，后文仅作为历史 planning record。
>
> 已完成的 Epic 1 evidence base 是六种 history representation、共 702 个
> model-task cases 的 outcome-blind audit，以及后续 outcome-aware observational
> failure-link review。当前定义和结果请使用
> [六模型正式报告](<../../motivation study/misleading_history_audit_report.md>)，
> 当前项目阶段请使用
> [Project Status](../../mobileworld_audit_handoff/STATUS.md)。

## 结论

这轮只读取 `traj_logs/seed_baseline`。我在当时的原始环境完成了一个可复现的
两阶段概率抽样 pilot：从 116 条非空轨迹中按
`success/failure/missing × trajectory length` 分层抽取 25 个 task，再在 task
内分别抽 scanner hit 与 non-hit，共人工复核 50 个
`P_(t-1) → P_t` immediate pair。由于 repo-external trajectory tree 未随 Git
发布，这里的“可复现”不表示当前 clone 可以独立重跑。

50 条全部完成单人复核，没有把空白模板当成标注结果；证据不足的 4 条保留为 `UNVERIFIABLE`。

| immediate-pair pilot 分类 | 数量 |
|---|---:|
| source 确认 invalid，且被下一步有害复用 | 2 |
| source 确认 invalid，但下一步拒绝/纠正或变异 | 3 |
| action failure / recovery control | 6 |
| 没有发现 invalid source premise | 35 |
| 证据不足 | 4 |
| 合计 | 50 |

抽样中有 18 个 scanner hit、32 个 non-hit。两个确认的 immediate propagation 分别来自一个 hit 和一个 non-hit。只合并 legacy 六案例账本与这次新证据时，8 个确认的 immediate pair 中有 7 个是当前 scanner non-hit。这不能用来估计 scanner recall，但足以证明：**只审 scanner hit 会系统性漏掉研究对象，scanner 只能是抽样分配变量，不能当标签。**

## 两个新确认案例

1. `MattermostProjectStatusReportTask`, `P45 → P46`（scanner hit）

   `S5/S7/S9` 中的真实项目包括 `Authentication Module`、`Payment Integration`、`Dashboard UI`、`API Gateway Setup`、`Performance Testing`、`Security Audit`。`P45` 却生成了包含 `User profile API`、`Order processing pipeline` 等不存在项目的报告；`P46` 不再检查原频道，直接接受正文并发送邮件。这是直接的 `invalid pre-step → downstream harmful reuse`。

2. `CheckSetMeetTimeTask`, `P22 → P23`（scanner non-hit）

   `S3` 的 Carl 邮件明确写着 `November 15 at 3 PM`。`P22` 却把 `11 AM → 12 PM` 当成正确的一小时会议，`P23` 接受这个 premise 并确认 `12 PM`，继续配置错误事件。这也是 immediate propagation；而且它和既有的 `P6 → P7–9` 是同一 task 中第二个独立的错误历史记录/目标决策链。

## legacy ledger + 本 pilot 能给出的局部 lower bound

下面数字只合并 `output/manual_review.json` 中 legacy 的 6 条确认链与本次两个新 immediate case，并做去重。它们是这一份**局部账本**的确定性观察下界，不是置信区间，也不是任何更新/扩展案例账本的最终总数；发表前必须与更广的案例 ledger 统一 schema 后重新去重计算。

| 口径 | 已确认 / 分母 | lower bound |
|---|---:|---:|
| 目标 decision turn `t` 明确依赖任意 invalid history | 23 / 3,281 | 0.701% |
| immediate `P_(t-1) → P_t` pair | 8 / 3,281 | 0.244% |
| all-history source-target exposure `(P_i,P_t)` | 23 / 65,808 | 0.035% |
| 至少有一条确认链的 task | 7 / 116 | 6.03% |

这里最贴合 Sentinel 问题的主口径是第一行：**decision turn 是否依赖了 active history 中任意一条 invalid claim**。65,808 是 source-target exposure 数，用于研究 lag 和具体污染边；它不应该替代 3,281 个 decision turn 作为第一主分母。

## 为什么 50-pair pilot 不能当总体发生率

这次 pilot 的 row 只检查紧邻的 `P_(t-1)`。但 Seed 在 `P_t` 前保留所有更早 assistant text，因此真正的风险集合是 `{P_i | i<t}`。一个 immediate pair 被标为“no invalid source”，并不代表 `P_t` 没有依赖 `P_(t-4)`、`P_(t-20)` 等旧错误。

脚本仍计算了设计权重，窄口径的 immediate propagation 得到约 3.9% 的 HT/Hájek 描述值；**这个数字只用于检查抽样和权重管道，不能写成 overall pre-step propagation prevalence**。原因有三点：

- 研究单位过窄，会漏掉 long-range reuse；
- 只有 25 个 task cluster；
- 大多数 `task × scanner-domain` 只抽到一个 pair，二阶段方差无法估计，Kish effective sample size 只有约 13.2。

因此本 pilot 不报告正式 CI，也不据此作总体率结论。

## 历史上建议的下一版：以 target decision turn 为主单位

总体仍是同样的 3,281 个带历史 decision turn，但每一行改为：

```text
task goal
+ target turn t 的 current observation S_t
+ 所有 active prior text P_1 ... P_(t-1)
+ sidecar evidence ledger S_i / S_(i+1)
+ scanner allocation flags（仅用于抽样）
```

主标签定义：

```text
Y_t = 1
iff P_t 明确复述、接受或操作性依赖某个被直接证据反驳的 P_i claim，
并由此产生错误、无必要重复、过早完成或偏离 rubric 的 GUI 决策。
```

标注时必须拆成两个判断，不能把“history 里有错”直接等同于“当前被误导”：

1. `history claim status`：`SUPPORTED / REFUTED / UNVERIFIABLE`；
2. `target use`：`HARMFUL_REUSE / CORRECTIVE_REJECTION / NO_USE / UNCLEAR`。

最终 turn label 至少包括：

- `CONFIRMED_PROPAGATION`
- `INVALID_PRESENT_BUT_NOT_USED`
- `INVALID_REJECTED_OR_RECOVERED`
- `ACTION_FAILURE_ONLY`
- `NO_INVALID_RELEVANT_HISTORY`
- `UNVERIFIABLE`

本目录的 `pilot_turn_context_all_history.jsonl` 已经把相同 50 个 target turn 扩展为完整 prior-text bundle，可作为下一轮标注器和界面输入；**它目前还没有完成 all-history adjudication**。

## 历史正式抽样建议

以下设计没有按本文写法执行，不能视为当前 sampling protocol 或尚待完成的 Epic 1
工作项。它保留用于解释早期方法选择如何演进为后来的六模型 evidence-card audit。

采用两阶段分层 cluster sample：

1. 第一阶段以 task 为 cluster，在 `result_status × length` 六个 scored strata 内做 SRSWOR；缺 result 的 1 条非空轨迹单列描述；
2. 第二阶段在每个选中 task 内抽 target decision turn，并同时保留 scanner hit/non-hit；
3. 每个 `task × scanner-domain` 至少抽 2 条，建议 3 条，才能估计二阶段方差；
4. 对每个 target turn 展开所有 `P_i, i<t`，并记录最终依赖的 source step 和 lag；
5. 设计权重为 task inclusion probability 与 task 内 turn inclusion probability 的乘积倒数。

建议主样本为 **500–600 个 target decision turn，约 70–90 个 task cluster**。如果真实率约 1–4%、design effect 约 1.5，这个量级只能给出较粗的约 ±1–2.5 percentage-point 95% precision，但已经足以把“存在性”推进到可报告 prevalence。至少 20% 双人独立复标，即 500 条主标时增加约 100 条 second annotation；合计约 600 次标注判断。若希望在 4% 附近做到约 ±1 percentage point，考虑 finite-population correction 后仍需约 1,300 个 turn，成本明显更高。

如果目标还包括 scanner recall/precision 或细分错误类型，不应依赖主样本中偶然出现的少量 positives。可以另外增加一个 enriched scanner-hit set 做 taxonomy/precision，但它必须与 prevalence probability sample 分开报告。

## CI 方法

正式研究使用设计加权 Hájek estimator：

```text
p_hat = Σ(w_j y_j) / Σ(w_j)
```

95% CI 使用 **stratified two-stage Rao–Wu rescaled bootstrap**：在每个 outcome × length stratum 内重抽 task cluster，再在 task 内重抽 scanner-domain turn；建议至少 2,000 replicates，并保留 finite-population correction。由于主样本每个二阶段 domain 至少有 2–3 条，task 内方差才可识别。

同时报告：

- raw numerator/denominator；
- weighted estimate 与 cluster CI；
- task-level incidence；
- lag 分布（1、2–3、4–10、>10）；
- hit/non-hit 分层结果；
- 双标 raw agreement、positive agreement，以及适合稀有标签的 agreement statistic。

## 产物

- `build_sampling_frame.py`：只读 `seed_baseline`，生成 task/pair frame、固定随机种子样本与完整历史 bundle；
- `task_frame.csv`：116 条非空轨迹；
- `immediate_pair_frame.csv`：3,281 条 immediate frame；
- `pilot_sample.csv`：50 条带设计权重的随机样本；
- `pilot_manual_labels.json`：实际单人复核结果；
- `pilot_annotations_reviewed.csv`：合并后的 50 条完整标注；
- `pilot_results.json`：计数、窄口径权重诊断和确认 lower bounds；
- `pilot_turn_context_all_history.jsonl`：下一轮 target-turn/all-history 标注输入；
- `analyze_pilot.py`：验证样本和标签一一对应，并重算所有数字。

原始环境中的 provenance commands（当前 clone 缺少它们所引用的 repo-external
trajectory tree，不能独立执行）：

```bash
python3 seed_baseline_audit/prevalence_study/build_sampling_frame.py
python3 seed_baseline_audit/prevalence_study/analyze_pilot.py
```

以上 derived outputs 现保留在本目录；repo-external QR-MW trajectories 未被修改，
也没有随 Git 发布。
