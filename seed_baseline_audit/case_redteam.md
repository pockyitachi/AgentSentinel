# `seed_baseline` propagation cases：保守红队裁决

## 结论先行

对 `propagation_cases.jsonl` 的 20 个候选逐案检查后，原来的 `CONFIRMED` 标签不能直接沿用。按“直接反证 + 实际 prompt 暴露 + target 明确 uptake + 排除当前 GUI 状态混淆”的严格口径，当前可以守住的 Tier-1 是 **5 个 unique tasks**：

- `SBP-005` `MastodonAdjustTootsTask`
- `SBP-012` `CheckInvoiceTask2`
- `SBP-014` `PhotoManagementTask`
- `SBP-016` `MattermostDeadlineReconciliationTask`
- `SBP-017` `MattermostShiftCoverageTask`

因此，冻结 `seed_baseline` 语料上的保守 unique-task lower bound 是：

\[
5 / 116 = 4.31\%
\]

这里的分母是 116 条非空 baseline trajectories；若用全部 117 个正式任务目录作分母，则是 `5/117 = 4.27%`。这是“至少已经观察到多少个严格案例”的固定语料下界，不是随机抽样发生率，也不是 history intervention 的因果效应估计。

其余案例被分为：8 个 `CONFIRMED_WITH_STATE_CONFOUND`、1 个 `PROVENANCE_ERROR_ONLY`、2 个 `SELF_CORRECTED`、4 个 `CANDIDATE`。失败、50-step 截断、重复动作和 evaluator score=0 都没有被单独当作 misleading 证据。

## 裁决规则

- **A — direct contradiction**：旧 claim 有直接 GUI、文件内容或能定位该字段的 evaluator 反证。单纯“没在一张截图里看到”、最终失败或长轨迹不够。
- **B — exposed**：source pre-step 确实进入 target prompt。20/20 均能在 source 后一轮的 thread log 中看到对应 assistant message；Seed 同时保留所有历史 assistant text，只裁剪旧 image messages。
- **C — uptake**：target prediction 明确复述旧 claim，或明确把它作为下一动作的 premise。仅仅执行了一个与 claim 相容的动作不够。
- **State confound**：source action 已把错误值写进输入框、选中错误日期或改变页面，使 `S_target` 本身也能解释 target prediction。此时可以确认错误状态延续，但不能把延续唯一归因于历史文字。
- **Grounding evidence evicted at source**：逐案确认支持正确 task fact 的 GUI 在 `P_source` 时已不在最近三张 image observation 内，并检查中间没有重新展示同一事实。它不是按 step 差机械打标；例如 `SBP-005` 和 `SBP-013` 的直接反证是在 source 之后才出现。

分类语义：

- `STRICT_CONFIRMED`：A/B/C 均成立；关键 target 没有强化错误 premise 的 current-state confound。
- `CONFIRMED_WITH_STATE_CONFOUND`：A/B/C 基本成立，但错误值也已由 GUI 当前状态直接呈现；不进入 Tier-1 history lower bound。
- `PROVENANCE_ERROR_ONLY`：只能直接证明“来自某 source”是假的，不能直接证明值本身一定错误。
- `SELF_CORRECTED`：有错误 premise uptake，但在不可逆错误写入前被新观察纠正。
- `CANDIDATE`：A 或 C 仍不完整。

## 逐案 adjudication 表

`Evicted` 指 `grounding_evidence_evicted_at_source`；`Confound` 指 state confound。B 栏链接到 source assistant response 被序列化进下一轮 target prompt 的 thread-log 行；C 栏链接到 target raw prediction。

| ID | A：直接反证 | B：进入 target prompt | C：target uptake | Evicted | Confound | 保守裁决 | 红队理由 |
|---|---|---|---|---|---|---|---|
| SBP-001 | 是；[S3](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask1/screenshots/CheckConferenceAndSendSmsTask1-0-3.png>) 为 `10/11–10/15` | [是，P10→P11](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask1/thread_139934151435392.log:669) | [是；P11 称“correct dates”](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask1/thread_139934151435392.log:685) | 是；正确日历之后未重现 | **是**；A10 已把错日期写进 S11 | `CONFIRMED_WITH_STATE_CONFOUND` | P11 也可直接读取当前短信草稿，不能隔离 history text 的作用。 |
| SBP-002 | 是；[S3](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckSetMeetTimeTask/screenshots/CheckSetMeetTimeTask-0-3.png>) 写明 Nov 15, 3 PM | [是，P6→P7](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckSetMeetTimeTask/thread_139934151435392.log:406) | [是；P7 接受 Nov 1 并继续建 event](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckSetMeetTimeTask/thread_139934151435392.log:422) | 是；S3 在 P6 时已离开三图窗口 | **是**；A6 已选择 Nov 1，S7 显示 Nov 1 | `CONFIRMED_WITH_STATE_CONFOUND` | 错误日期链清楚，但 target 当前日期页面本身是替代解释。 |
| SBP-003 | 是；[S4](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleLunchViaSmsTask/screenshots/ScheduleLunchViaSmsTask-0-4.png>) 明示 11 AM | [是，P16→P17](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleLunchViaSmsTask/thread_139934151435392.log:1125) | **部分**；[P17 只说保存/完成](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleLunchViaSmsTask/thread_139934151435392.log:1141)，没有复述“未指定时间” | 是；后续未重看 SMS | 部分；S17 可见默认 1 PM，但 A16 没有设置该时间 | `CANDIDATE` | 行为与 false premise 相容，但 C 不满足明确 premise uptake。 |
| SBP-004 | 是，但只对“TechNova 身份”可直接守住；原 JSON 把 P20 未逐字包含的日期/时间也捆进 source claim | [是，P20→P21](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInterviewTimesTask/thread_139934151435392.log:1344) | [是；后续继续用 TechNova/Nov 5/10 AM](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInterviewTimesTask/thread_139934151435392.log:1360) | 是；最后的邮件证据早于 P20 三图窗口 | **是**；A20 已把 TechNova 写入表单 | `CONFIRMED_WITH_STATE_CONFOUND` | 仅对缩窄后的 identity claim 裁决；原 bundled claim 不能整体称 strict。 |
| SBP-005 | 是；[S33](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonAdjustTootsTask/screenshots/MastodonAdjustTootsTask-0-33.png>) 菜单命令是 `Bookmark`，而后续已添加状态显示 `Remove bookmark` | [是，P32→P33](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonAdjustTootsTask/thread_139934151435392.log:2174) | [是；P33 明说“currently bookmarked”，把 Bookmark 当 remove](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonAdjustTootsTask/thread_139934151435392.log:2190) | **否/不适用**；直接反证由 A32 打开的当前菜单提供 | **否（反向）**；S33 实际反驳旧 premise | `STRICT_CONFIRMED` | 这是最干净的“历史 premise 压过当前 GUI 语义”案例；随后点击实际重新加 bookmark。 |
| SBP-006 | [S6](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceLocationTask/screenshots/CheckConferenceLocationTask-0-6.png>) 只能证明邮件没有地址、只写 hotel name；不能从该图直接证明 `100 Main St` 数值一定错误 | [是，P10→P11](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceLocationTask/thread_139934151435392.log:664) | [是；P18 再称“got from the email”并重输](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceLocationTask/thread_139934151435392.log:1138) | 是；邮件 S6 在 P10 已离窗 | P11 有搜索结果 confound；P18 输入前字段已清空 | `PROVENANCE_ERROR_ONLY` | [S16](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceLocationTask/screenshots/CheckConferenceLocationTask-0-16.png>) 显示路线目的地为 Arthur D Little Bldg，强烈可疑，但没有把正确 hotel address 明示出来；不把它升级为严格 value-error。 |
| SBP-007 | 是；[S2](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask2/screenshots/CheckConferenceAndSendSmsTask2-0-2.png>) 显示 October 4–10 | [是，P12→P13](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask2/thread_139934151435392.log:797) | [是；P13 称错误草稿正确](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask2/thread_139934151435392.log:813) | 是；日历远早于 P12 | **是**；A12 已写错日期 | `CONFIRMED_WITH_STATE_CONFOUND` | 与 SBP-001 相同，错误草稿是当前视觉替代解释。 |
| SBP-008 | **部分**；[S18](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/TextArrivalTimeTask/screenshots/TextArrivalTimeTask-0-18.png>) 最快路线是 3h27，但同时有 3h53 备选；“about 3h40”不是无争议的直接反证 | [是，P24→P25](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/TextArrivalTimeTask/thread_139934151435392.log:1584) | [是但数值漂移；P25 变成 3h45–4h](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/TextArrivalTimeTask/thread_139934151435392.log:1600) | 是；Maps 已离窗 | 否；A24 只聚焦空输入框 | `CANDIDATE` | A 不够硬，且 source/target 数值并非 exact reuse；不能用 score=0 补足局部反证。 |
| SBP-009 | 是；[S7](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CartInfoNotificationTask/screenshots/CartInfoNotificationTask-0-7.png>) 显示真实商品与订单号 | [是，P18→P19](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CartInfoNotificationTask/thread_139934151435392.log:1189) | [是；P19 称错误内容正确并发送](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CartInfoNotificationTask/thread_139934151435392.log:1205) | 是；订单详情早已离窗 | **是**；A18 已写入短信草稿 | `CONFIRMED_WITH_STATE_CONFOUND` | 可确认错误外部写入，但不能隔离 target 是读 history 还是读草稿。 |
| SBP-010 | **不足**；[S6](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonCalendarMultiMemosTask/screenshots/MastodonCalendarMultiMemosTask-0-6.png>) 和 S7 只显示两个 November posts；有限 feed absence 不能证明不存在 October/Sustainable Urban Development post | [是，P14→P15](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonCalendarMultiMemosTask/thread_139934151435392.log:940) | 部分；[P15 发明 location，但没有重证 title 来源](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonCalendarMultiMemosTask/thread_139934151435392.log:956) | 是；已看的 feed 在 P14 时离窗 | **是**；A14 已把 title 写进表单 | `CANDIDATE` | A 是“未见”而非直接反证；evaluator 的 Event not found 也没有局部证明 topic claim 为假。 |
| SBP-011 | 是；[S31](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/DownloadSendReceiptTask/screenshots/DownloadSendReceiptTask-0-31.png>) 清楚显示 total 5.08 | [是，P34→P35](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/DownloadSendReceiptTask/thread_139934151435392.log:2219) | [是；P35 接受并发送](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/DownloadSendReceiptTask/thread_139934151435392.log:2235) | 是；S31 恰已离开 P34 三图窗口 | **是**；A34 已把 `$149.99` 写进邮件 | `CONFIRMED_WITH_STATE_CONFOUND` | P36 的无 GUI 复述发生在发送之后；造成不可逆 send 的 P35 仍受草稿 confound。 |
| SBP-012 | 是；[S15](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInvoiceTask2/screenshots/CheckInvoiceTask2-0-15.png>) 有 invoice.pdf，[S16](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInvoiceTask2/screenshots/CheckInvoiceTask2-0-16.png>) 显示客户、金额、到期日和利息 | [是，P19→P20](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInvoiceTask2/thread_139934151435392.log:1301) | [是；P20 再称用户未提供 email/invoice 并继续 ask](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInvoiceTask2/thread_139934151435392.log:1315) | 是；PDF 在 P19 已离窗，之后未重开 | **否**；A19 ask-user 后仍是空 compose GUI，不显示“文件不存在” | `STRICT_CONFIRMED` | 直接证据、prompt 暴露、明确重复和不必要 ask 都成立。 |
| SBP-013 | 是，但仅 link 半项；[S8](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostReadingGroupTask/screenshots/MattermostReadingGroupTask-0-8.png>) preview 明示 galaxy paper；68.7 score 未独立验证 | [是，P6→P7](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostReadingGroupTask/thread_139934151435392.log:412) | [是；P7 认为草稿满足要求并发送](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostReadingGroupTask/thread_139934151435392.log:428) | **否/不适用**；反证 S8 是 send 后才出现 | **是**；A6 已把 link 写进当前草稿 | `CONFIRMED_WITH_STATE_CONFOUND` | 链接错误真实，但 target 可直接读草稿；后续自检发生在公开 post 已写入之后。 |
| SBP-014 | 是；[S9](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/PhotoManagementTask/screenshots/PhotoManagementTask-0-9.png>) 的相关一个月只见 Tokyo/Paris，无 New York；evaluator 只作结果 corroboration，不承担局部 A | [是，P34→P35](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/PhotoManagementTask/thread_139934151435392.log:2439) | [是；P39 明确以“burger from New York”为 premise 创建 folder](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/PhotoManagementTask/thread_139934151435392.log:2722) | 是；日历在 P34 早已离窗，之后未返回 | **否**；当前 Gallery/move dialog 没有 New York 文本或 provenance | `STRICT_CONFIRMED` | 旧 location claim 被长间隔明确复用并物化为 folder；失败本身未用于升级标签。 |
| SBP-015 | 是；[S37](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostBudgetApprovalPipelineTask/screenshots/MattermostBudgetApprovalPipelineTask-0-37.png>) 显示 Engineering `$85k / 50%` | [是，P48→P49](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostBudgetApprovalPipelineTask/thread_139934151435392.log:3311) | [是；P49 称错误表正确并发送](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostBudgetApprovalPipelineTask/thread_139934151435392.log:3327) | 是；公式截图已离窗；但正确 P47 文字仍在 prompt | **是**；A48 已写完整错误表 | `CONFIRMED_WITH_STATE_CONFOUND` | 这是“有正确历史文字仍 final-step drift”的反例，不宜把所有机制都归因于 screenshot eviction。 |
| SBP-016 | 是；[S5](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostDeadlineReconciliationTask/screenshots/MattermostDeadlineReconciliationTask-0-5.png>) 列出四个 2026 deadlines，[S8](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostDeadlineReconciliationTask/screenshots/MattermostDeadlineReconciliationTask-0-8.png>) 有 matching events | [是，P16→P17；P18 仍保留 P16](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostDeadlineReconciliationTask/thread_139934151435392.log:1190) | [是；P18 在邮件发送后的页面仍复述 2025-08-25，并转向建错 event](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostDeadlineReconciliationTask/thread_139934151435392.log:1222) | 是；channel/calendar 均已离窗 | **否（关键 P18）**；错误报告已由 A17 发送并离开当前 GUI | `STRICT_CONFIRMED` | P17 send 有草稿 confound，但 P18 的错误 event 分支是无当前事实支持的显式继续。 |
| SBP-017 | 是；[S7](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostShiftCoverageTask/screenshots/MattermostShiftCoverageTask-0-7.png>) 显示 May 4 All Hands；P25 也刚刚说应 deny | [是，P26→P27](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostShiftCoverageTask/thread_139934151435392.log:1727) | [是；P27 明说 All Hands 是 May 6、May 4 无冲突并进入 mail](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostShiftCoverageTask/thread_139934151435392.log:1743) | 是；日历已离窗 | **否**；A26 只 copy Mattermost request，当前 GUI 不含 conflict verdict | `STRICT_CONFIRMED` | 错误 branch premise 在无视觉强化时被明确重复，并最终导致发错 HR email。 |
| SBP-018 | 是；[S43](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckGithubInfoTask/screenshots/CheckGithubInfoTask-0-43.png>) 显示 745 stars/13 contributors | [是，P46→P47](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckGithubInfoTask/thread_139934151435392.log:3100) | [是；P47 按“没找到”重新开 Chrome](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckGithubInfoTask/thread_139934151435392.log:3116) | 是；S43 在 P46 刚离开三图窗口 | 否；当前是 home/email，不显示 stats | `SELF_CORRECTED` | 造成可观察的无谓 browser detour，但 S48 恢复正确值，尚未发送错误邮件。 |
| SBP-019 | 是；[S5](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleCoffeeTimeViaSmsTask/screenshots/ScheduleCoffeeTimeViaSmsTask-0-5.png>) 为 9:10 AM | [是，P8→P9](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleCoffeeTimeViaSmsTask/thread_139934151435392.log:571) | [是；P9 复述 3 PM premise](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleCoffeeTimeViaSmsTask/thread_139934151435392.log:587) | 是；SMS S5 在 P8 刚离窗 | 否；当前 home screen 无时间信息 | `SELF_CORRECTED` | A9 打开 Messages 在两种回复分支都可能需要，尚不能证明 action divergence；S10 在写字前纠正并最终成功。 |
| SBP-020 | 是；[S10](</Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostTechnicalDebtTriageTask/screenshots/MattermostTechnicalDebtTriageTask-0-10.png>) 显示 PaymentProcessor `47,880` | [是，P39→P40](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostTechnicalDebtTriageTask/thread_139934151435392.log:2597) | **部分**；[P40 保留 PaymentProcessing family/ranking，但数值又变为 15,912](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MattermostTechnicalDebtTriageTask/thread_139934151435392.log:2613) | 是；原公式早已离窗 | 否；A39 只回 home | `CANDIDATE` | downstream 是语义方向一致而非 exact premise reuse，且 SMS 未写入就返回复查。 |

## Tier-1 为什么只剩 5 个

| Tier-1 case | 无 current-state confound 的决定性 target | 观察到的错误走向 |
|---|---|---|
| SBP-005 | P33 面对反向 GUI 命令仍采用旧 bookmark premise | 点击 `Bookmark`，实际重新添加 |
| SBP-012 | P20 面对空 compose GUI 再次宣称 invoice/email 不存在 | 重复 ask-user，最终放弃已可完成的任务 |
| SBP-014 | P39 的 Gallery dialog 不含 New York 依据 | 创建 New York folder 并错误分类 |
| SBP-016 | P18 已离开错误邮件草稿，当前 GUI 不含 2025-08-25 依据 | 转向创建不相关的 missing event |
| SBP-017 | P27 当前 Mattermost GUI 不含 calendar conflict verdict | 把有冲突的 Alex request 转入 HR email 流程 |

这五个案例仍然只是**观察性 propagation**：它们证明“直接被反驳的旧 claim 在 prompt 中暴露，随后在没有当前 GUI 强化时被 target 明确采用并引发错误分支”。只有固定同一 GUI state、对历史做 `Original / Drop / Replace` 配对 replay 后，才能主张删除该 pre-step 对下一动作有因果影响。

## 对 fact-drift-after-eviction 机制的影响

- 20 个候选中，有 18 个在 source 生成时，最后一张支持正确 task fact 的截图已离开 Seed 的三图窗口；`SBP-005` 和 `SBP-013` 的反证是在 source 之后才出现。这个比例来自定向候选，不能报告为总体发生率。
- 在 5 个 Tier-1 中，4 个（SBP-012/014/016/017）符合“grounding screenshot evicted → old text remains → fact drift/uptake”；SBP-005 是不同机制：当前 GUI 已给出反证，agent 仍接受旧 premise。
- screenshot eviction 不是充分条件。SBP-015 在 P48 前一轮 P47 仍有正确的 `$85k / 50%` assistant text，却在最终写表时漂移；这提示 monitor 不能只检查截图是否离窗，还要核对 active claims 的证据状态。

## 固定语料统计边界

- **严格、无 state confound 的错误分支：5/116（4.31%）**。
- `SELF_CORRECTED` 的两例不并入上述 lower bound；它们用于衡量 Sentinel 的潜在误删风险和自然恢复能力。
- 8 个 `CONFIRMED_WITH_STATE_CONFOUND` 可以用于构造 replay 样本，但不能在 observational table 中被表述为“pre-step text 导致 target action”。
- `SBP-006` 只进入 provenance 计数；除非取得明确的正确 hotel address 或可定位该字段的 evaluator ground truth，否则不能把 `100 Main St` 的 value error 写成 confirmed。
- `SBP-008`、`SBP-010`、`SBP-020` 以及 C 不充分的 `SBP-003` 保留为 candidate，不进入任何 confirmed numerator。

