# Seed baseline：错误 pre-step 确实进入后续 prompt 的日志证据

范围仅限 `/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline`。本页的目的
不是新增 occurrence label，而是补上一个关键 provenance 问题：`traj.json` 只保存模型
输出，不能单独证明某条输出后来真的进入了模型输入；正式目录中的 `thread_*.log` 则
包含 `pretty_print_messages`，可以直接检查下一轮 request 的 message array。

## 五个 replay point 的 prompt 暴露

| Task | 错误 source output | 后续 request 中的同一 `reasoning_content` | 解释 |
|---|---|---|---|
| `CheckSetMeetTimeTask` | [P6 原始输出](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckSetMeetTimeTask/thread_139934151435392.log:357) | [下一轮 request](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckSetMeetTimeTask/thread_139934151435392.log:406)；[再下一轮仍在](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckSetMeetTimeTask/thread_139934151435392.log:455) | “Nov 1, 10–11” 并非只写入日志，而是作为旧 assistant reasoning 继续暴露 |
| `ScheduleLunchViaSmsTask` | [P16 原始输出](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleLunchViaSmsTask/thread_139934151435392.log:1073) | [P17 request](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleLunchViaSmsTask/thread_139934151435392.log:1125)；[P18 request](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/ScheduleLunchViaSmsTask/thread_139934151435392.log:1178) | “没有指定时间 / default 1 PM” 在保存与完成判断前均仍在 prompt |
| `CheckConferenceAndSendSmsTask1` | [P10 原始输出](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask1/thread_139934151435392.log:621) | [P11 request](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask1/thread_139934151435392.log:668)；[P12 request](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckConferenceAndSendSmsTask1/thread_139934151435392.log:718) | 错误 May 日期的 reasoning 与 tool-call payload 都继续暴露 |
| `CheckInterviewTimesTask` | [P20 原始输出](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInterviewTimesTask/thread_139934151435392.log:1296) | [P21 request](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInterviewTimesTask/thread_139934151435392.log:1343)；[后续仍复述](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/CheckInterviewTimesTask/thread_139934151435392.log:1622) | 虚构的 TechNova 实体进入输入并持续到后续时间设置 |
| `MastodonAdjustTootsTask` | [P32 原始错误段](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonAdjustTootsTask/thread_139934151435392.log:2117) | [P33 request](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonAdjustTootsTask/thread_139934151435392.log:2174)；[P34 request](/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline/MastodonAdjustTootsTask/thread_139934151435392.log:2224) | bookmark/favorite 的错误状态解释在当前菜单出现后仍继续暴露 |

这五条只能证明 **exposure**。要标 `observed propagation`，还必须另外检查下一步
prediction 是否明确接受该 premise，以及 action 是否沿它继续。要声称 **causal
misleading**，还必须在冻结 GUI state 的 replay 中只改变 history。

## 结构性机制

当前 `seed_agent` 会重新加入全部历史 assistant `content/reasoning_content`，但只保留最近
3 个 image observation。因此在 action `t` 之前，通常是：

```text
text:  P1 ... P(t-1)
image: S(t-2), S(t-1), S(t)
```

全 corpus 有 65,808 个 `(P_i, decision_t)` 文字暴露，其中 56,319 个（85.58%）发生
在 `P_i` 相关的前/后截图都已离开 3 图窗口之后。这个比例不是错误率；它刻画的是
“文字仍在、原证据已不在”的风险面。

## 最重要的混杂

部分 source action 会把错误值同时写进环境：例如输入错误短信日期、选择错误日历日期、
输入虚构公司名。此后 target decision 同时看到错误 history 和被错误 action 改变的当前
GUI。因此自然日志可证明“错误 premise 形成了 text+state 的自我强化链”，不能单独证明
只删 history 就会恢复。

更干净的 history-only opportunity 是 `ScheduleLunchViaSmsTask`：P16 的动作只是输入正确
标题，但 reasoning 丢失了 11 AM 事实；P17 才根据这一 premise 保存默认 1 PM。另一个
近似干净案例是 `MastodonAdjustTootsTask`：P32 的动作只打开菜单，S33 反而给出
`Bookmark` 这一反证，但 P33 仍沿旧解释操作。
