# MobileWorld Misleading History Reuse Audit

状态：最终审核结果，2026-08-25。当前纳入 MAI-UI-8B、
Qwen3-VL-8B、GELab-Zero-4B、UI-Venus-1.5-8B、GUI-Owl-1.5-8B-Instruct
与 MemGUI-8B-SFT 的 MHR、局部伤害和 MHR-to-final-failure 最终结果。
每个模型独立报告，不做模型间排名或优劣比较。

本报告研究自然轨迹中模型自己生成的错误历史是否会进入后续请求、被后续决策明确复用，
以及复用后是否伴随可见的局部损害。结果是观测性证据，不是随机干预或反事实实验；因此不把关联
表述为因果效应，也不估计删除历史后成功率会提高多少。

Figures 直接引用 Git 仓库外、哈希校验过的 canonical audit blobs；报告没有把原始采集
截图复制进仓库。

## Reader-facing definitions

正文只使用一个核心事件名称：

- **Misleading History Reuse (MHR)**：模型早先生成的错误或过时内容被搬进后续
  actor prompt，而且后续 prediction 明确引用、复述或依赖它。

对每个 MHR reuse instance，报告另行记录同一条审核链中是否观察到局部伤害，以及具体
伤害类型。局部伤害是 MHR 的一个属性，不是第二种事件；未观察到局部伤害也不等于已经
证明该 MHR 无害。这里的“观察到”描述自然轨迹中的现象，不表示已经证明历史复用是唯一
原因。

### Task、carried-forward error 与 reuse instance

- **Task / model–task case**：一个模型对一个 benchmark task 的一条完整运行轨迹。
  本报告每个模型都有117个 cases；合并六个模型时，同一个 benchmark task 会按模型形成
  6个独立 cases，而不是合并为一个。
- **Error Carried Forward from an Earlier Step**：模型在某一步生成的错误或过时内容，
  随后作为历史被搬进一个或多个后续 actor prompts。
- **Reviewed Reuse Instance**：一个 carried-forward error 与一个后续 target decision
  之间的审核连接：

  error generated at an earlier step → carried into a later prompt → reused by a later decision/action

一条 task trajectory 可以包含多个 reviewed reuse instances；同一个 error 也可能被搬进
多个后续 prompts。因此 instance 数可以大于117。报告以 task-level 结果为主，
instance-level 结果用于描述同一任务内部发生了多少次。

这里统计的是“某一步产生的一个具体 error”，不是错误类别：该 error 即使被搬进许多
后续 prompts，仍算一个 error；如果模型在另一个 step 重新生成相同文本，则算另一个
error。把不同 steps 的相同文本合并得到的 text family 也不是语义错误类别。

### MHR 的局部伤害类型

MHR 的局部伤害属性只使用以下五种既有下游效果；同一个 reuse instance 可以同时具有
多种效果：

- **UNNECESSARY_ACTION**
- **WRONG_ACTION**
- **REPEATED_ACTION**
- **PREMATURE_TERMINATION**
- **OFFTRACK_CONTINUATION**

**RECOVERED** 表示之后恢复，本身不是 harm；它只能与至少一种上述效果共同出现。

### MHR 与最终失败的两种路径

最终失败分析按 **failed task trajectory** 计数，而不是把同一任务中的多条 MHR 重复
计为多个失败。正文只使用两个互斥类别：

- **直接停止（direct stop）**：与最终失败保持审核连接的 MHR 在最后一个 decision 中
  被复用，模型随即显式输出 `finished` 或 `answer`，对应 chain 已被局部 MHR 审核标为
  **PREMATURE_TERMINATION** 并确认未恢复；任务结束后也没有新的恢复机会。
- **间接带偏（indirect derailment）**：MHR 没有在复用当步终止任务；轨迹此后仍继续，
  但相关错误导航、错误状态、被放弃的子目标或重复循环没有恢复，并与 evaluator 揭示的
  最终失败保持可追踪连接。

如果一个任务既有较早的间接带偏，又有最后一步的直接停止，正文优先计入直接停止，
所以两类不会重复相加。两条 GELab 轨迹在复用 MHR 的同一步因 `HOME` 被解析为
`unknown` 而由 runtime 终止；由于冻结审核没有确认 MHR 是该 parser termination 的
直接原因，而且没有后续轨迹可判断间接带偏，正文不把它们硬归入任何一类。
“直接/间接”描述的是自然轨迹中观察到的路径，不是已证明的反事实因果；没有“只纠正
或删除该历史后重跑同一状态”的配对实验。

## MAI-UI-8B

数据源：117-task curated set；117/117 task evidence coverage
完整。Dataset SHA-256：
1ffd746e21a15133c7124325b03ca94d7528a7aea5c4a5fda470fd9b8496a60d。

### Previous-history representation

MAI-UI 使用 **raw replay**。每条已接受的原始 assistant reply（包括其中的
`<thinking>` 与 `<tool_call>`）都会作为 assistant turn 累计回放，不做摘要或改写；
文字保留到任务结束，但图片窗口最多只保留最近3张 image messages（包含当前图）。
更早截图会消失，tool/ask-user result 会代替对应 observation 的截图。由于这些 reply
生成在动作执行前，它们记录的是 actor 当时的判断或意图，不是经过 post-state 验证的
执行结果。

### Task-level results

- 成功：**31/117**，SR = **26.50%**。
- 失败：**86/117**；no result：0。
- 全部117个 tasks 中，**7/117（5.98%）**出现 MHR；其中
  **7/117（5.98%）**观察到局部伤害。

| Outcome stratum | Tasks | Tasks with MHR | MHR tasks with observed local harm |
| --- | ---: | ---: | ---: |
| Failure | 86 | **7/86（8.14%）** | **7/86（8.14%）** |
| Success | 31 | **0/31（0%）** | **0/31（0%）** |

- 最终失败连接：直接停止 **0/86（0%）**；间接带偏 **5/86（5.81%）**。

### Reuse-instance results

- MHR：13 reviewed reuse instances，分布在7个 tasks。
- 其中观察到局部伤害：13 reviewed reuse instances，分布在7个 tasks。

| Observed effect | Affected MHR instances |
| --- | ---: |
| **UNNECESSARY_ACTION** | 3 |
| **WRONG_ACTION** | 5 |
| **REPEATED_ACTION** | 12 |
| **PREMATURE_TERMINATION** | 0 |
| **OFFTRACK_CONTINUATION** | 7 |
| **RECOVERED** | 1 |

效果允许重叠，所以该表不能纵向相加得到13。

### Prompt persistence

- 117 条轨迹共有 **4,156 个 actor prompts**，其中 **4,039 个**包含至少一条
  earlier-step history item；所有 earlier-step history items 在这些 prompts 中累计出现
  **91,607 次**。
- 13 个已确认的 errors 被搬进后续 actor prompts，累计精确出现 **309 次**，即全部
  earlier-step-history appearances 的 **309/91,607（0.34%）**。
- **13/13** errors 被搬进不止一个后续 prompt。
- 每个 error 被搬进后续请求数的中位数为 **28**，最长为 **36**。
- 观察到局部伤害的 MHR uptake 从 source step 到 target 的 lag 中位数为 **2 步**，
  最远为 **4 步**。

309 次只表示这些 errors 被带进了309个后续 prompt positions。其中正式确认的
MHR reuse instances 是 **13 次**，且这13次都有 observed harm；其余 appearances 不能
仅凭“出现在 prompt 中”判定为明确复用或损害。

### Real examples

#### Example 1: nonexistent Android settings route

CheckGithubInfoTask, source step 45 → target step 46.

- **Error:** “Network & internet > General > Reset VPN or Network” is an available settings
  route.
- **Reuse:** the next prediction explicitly follows the same route assumption and presses Back.
- **Observed harm:** **UNNECESSARY_ACTION** and **OFFTRACK_CONTINUATION**; the trajectory
  later **RECOVERED**.

![Internet settings page without a General entry](/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_20260820_01/audit/raw/runs/01M0EZ6T4XF06CPS3Z69XXW4ZB/blobs/sha256/0a/0ac372444556353c30723da033c2770ea584895cb0b0a39b0542bb3e437f453c)

![Network and internet settings page without a General entry](/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_20260820_01/audit/raw/runs/01M0EZ6T4XF06CPS3Z69XXW4ZB/blobs/sha256/99/99672703245cbf5ccf8fde2c1220a319dfb6bf0f8d0ebb1d1bff1056eaf7e9c2)

*Left: the visible Internet page contains no “General” entry. Right: after the reused route
assumption and Back action, “General” is still absent.*

#### Example 2: share icon misread as an overflow menu

MastodonMultiInviteTask, source step 37 → target step 39.

- **Error:** the share icon is an overflow menu containing invite-link options.
- **Reuse:** the target prediction repeats that interpretation and clicks the same control.
- **Observed harm:** **WRONG_ACTION**, **REPEATED_ACTION**, and
  **OFFTRACK_CONTINUATION**.

![Mastodon profile with a visible share icon](/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_20260820_01/audit/raw/runs/01M0EZ6T4XF06CPS3Z69XXW4ZB/blobs/sha256/4b/4b666476a28377f87f70b5e19455a82f656d9108f9139515ef4125696f7e9b14)

![Android sharing-link sheet opened from the Mastodon share icon](/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_20260820_01/audit/raw/runs/01M0EZ6T4XF06CPS3Z69XXW4ZB/blobs/sha256/48/48a0241d7fb9bad6f2d64d168a605925394d9826009dc5182e53a81fd79b1b8f)

*Left: the selected control is visibly a share icon. Right: clicking it again opens the Android
“Sharing link” sheet, not invite-link settings.*

#### Example 3: ordinary Gallery viewer misread as a crop overlay

MastodonPostEditedPhotoTask, source step 20 → target step 22.

- **Error:** the ordinary Gallery viewer is a full-screen crop overlay.
- **Reuse:** the target prediction repeats the crop-overlay interpretation and clicks again.
- **Observed harm:** **WRONG_ACTION**, **REPEATED_ACTION**, and
  **OFFTRACK_CONTINUATION**.

![Gallery ordinary viewer displaying photo 2 of 7](/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_20260820_01/audit/raw/runs/01M0EZ6T4XF06CPS3Z69XXW4ZB/blobs/sha256/58/581ae2259521e3d0ba394db8dae7b752fa802e567fa25e8b5b3d5fb09465bdcf)

![Gallery ordinary viewer displaying photo 3 of 7](/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_20260820_01/audit/raw/runs/01M0EZ6T4XF06CPS3Z69XXW4ZB/blobs/sha256/bf/bfb715e6d039758dbf7ccc576eb3730ed8e2175c6024c042bd587522537f8c6c)

*Left: Gallery shows photo 2/7 with no crop controls. Right: the repeated click advances to
photo 3/7 instead of opening editing controls.*

## Qwen3-VL-8B

数据源：117-task curated set；117/117 task evidence coverage
完整。Dataset SHA-256：
266ab97b02fd6d479114a2f0db945dc9e66f17c4ba1f68a04b375ec3384a5cf2。

### Previous-history representation

Qwen3-VL 使用 **flat progress**。它不回放旧 raw replies、Thought、tool-call JSON 或旧
截图，而是把每个已接受步骤的 `Action:` sentence 累计扁平化为
`Task progress (You have done ...): Step N: ...`，并只发送当前截图；下一轮 observation
若带有 tool/ask-user result，该外部结果会追加到最近一条 Action 后。由于动作前生成的
句子被重新呈现在 “You have done” 之下，审计必须用实际 action 与 post-state 核验其中
隐含的完成或效果，而不能把 progress wording 当作真值。

### Task-level results

- 成功：**8/117**，SR = **6.84%**。
- 失败：**109/117**；no result：0。
- 全部117个 tasks 中，**35/117（29.91%）**出现 MHR；其中
  **32/117（27.35%）**观察到局部伤害。

| Outcome stratum | Tasks | Tasks with MHR | MHR tasks with observed local harm |
| --- | ---: | ---: | ---: |
| Failure | 109 | **35/109（32.11%）** | **32/109（29.36%）** |
| Success | 8 | **0/8（0%）** | **0/8（0%）** |

- 最终失败连接：直接停止 **8/109（7.34%）**；间接带偏
  **16/109（14.68%）**。

### Reuse-instance results

- MHR：139 reviewed reuse instances，分布在35个 tasks。
- 其中观察到局部伤害：131 reviewed reuse instances，分布在32个 tasks。

| Observed effect | Affected MHR instances |
| --- | ---: |
| **UNNECESSARY_ACTION** | 19 |
| **WRONG_ACTION** | 43 |
| **REPEATED_ACTION** | 105 |
| **PREMATURE_TERMINATION** | 9 |
| **OFFTRACK_CONTINUATION** | 23 |
| **RECOVERED** | 3 |

效果允许重叠，所以该表不能纵向相加得到131。

### Prompt persistence

- 117 条轨迹共有 **3,017 个 actor prompts**，其中 **2,900 个**包含至少一条
  earlier-step history item；所有 earlier-step history items 在这些 prompts 中累计出现
  **58,677 次**。
- 139 个 MHR reuse instances 去重后对应 **138 个 distinct errors carried forward**；
  其中一个 error 连接到两个正式审核 targets。
- 这些 errors 在所有后续 actor prompts 中累计精确出现 **2,495 次**，即全部
  earlier-step-history appearances 的 **2,495/58,677（4.25%）**。
- **127/138** errors 被搬进不止一个后续 prompt。
- 每个 error 被搬进后续请求数的中位数为 **16**，最长为 **45**。
- 观察到局部伤害的 MHR uptake lag 中位数为 **1 步（下一步）**，最远为 **2 步**。

2,495 次只表示 prompt exposure；正式确认的 MHR reuse instances 是 **139 次**，其中
**131 次**具有 observed harm。其余 appearances 不能仅凭“出现在 prompt 中”判定为
明确复用或损害。

### Real examples

#### Example 1: one-day reminder saved as one minute

MastodonCreateMemoTask, source step 29 → target step 30; followed by source step 30 → target
step 31.

- **Task requirement:** create the Urban Mobility event with a reminder **1 day before**.
- **Error:** “I confirmed the custom reminder of 1 day before the Urban Mobility lecture event.”
- **Visible contradiction:** the reminder unit is minutes, and the event form displays
  **1 minute before**.
- **Reuse and observed harm:** the error is present in the next prompt; the target treats the
  reminder as complete and saves the event. This was classified as **WRONG_ACTION** and
  **OFFTRACK_CONTINUATION**.
- **Following error:** “I saved the event ... with all the specified details and reminder.” The
  next decision reuses that false completion and terminates successfully; this was classified as
  **PREMATURE_TERMINATION**. The task received score 0.

![Reminder dialog with value 1 and the minutes unit selected](/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_20260821_01/audit/raw/runs/01M0HAPBKPN5HJHFB6HQ3ME74M/blobs/sha256/48/48c9f51092533d9fd1e1af8237dacc89909519799a9b1c7814ba34e66a76d431)

![Calendar event form displaying 1 minute before](/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_20260821_01/audit/raw/runs/01M0HAPBKPN5HJHFB6HQ3ME74M/blobs/sha256/51/511e3fb171f24ca171229aa814f46e3158dc7abf865d25886951eac368510c11)

*The task requires one day. The GUI visibly contains one minute, but the carried-forward error
says one day; the next decision saves the event.*

#### Example 2: a 3–4 PM meeting saved as 1–2 PM on the wrong date

CheckSetMeetTimeTask, source step 10 → target step 12.

- **Task requirement:** Carl's email specifies **November 15 at 3 PM**; the one-hour Board
  Meeting should therefore be **3–4 PM on November 15**.
- **Error:** “I adjusted the end time of the calendar event to 2:00 PM.”
- **Visible contradiction:** at the reviewed source and target, the event form is still on
  October 16 with both times at **1:00 PM**.
- **Reuse and observed harm:** the exact error enters the target prompt; the target repeats the
  1-to-2-PM premise and clicks the time field again. This was classified as
  **REPEATED_ACTION** and **OFFTRACK_CONTINUATION**.
- **Trajectory outcome:** the same trajectory later saves **October 16, 1–2 PM**, terminates as
  success, and receives score 0 for an incorrect calendar event.

![Carl email specifying November 15 at 3 PM](/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_20260821_01/audit/raw/runs/01M0HAPBKPN5HJHFB6HQ3ME74M/blobs/sha256/d7/d7786fdfcfd999c1af48fea90b67785daf44a0909ecd05b75de275a470109e68)

![Board Meeting form still showing October 16 and 1 PM for both times](/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_20260821_01/audit/raw/runs/01M0HAPBKPN5HJHFB6HQ3ME74M/blobs/sha256/b7/b7e66c3dc10304117ee35e8d6aeb14dfc5b316728826cb279c4bcb297697e79b)

![Board Meeting immediately before saving as October 16 from 1 to 2 PM](/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_20260821_01/audit/raw/runs/01M0HAPBKPN5HJHFB6HQ3ME74M/blobs/sha256/48/488a97075555816213828ac5561026c88c844ddca0e3edf5c429d58964130198)

*The first two images show the required time and the contradictory state during the reviewed
reuse. The third records the later wrong event immediately before it is saved; it is trajectory
context rather than an additional causal attribution to one reuse instance.*

#### Example 3: unopened file answered as 5 instead of 29

CountFileLinesTask, source step 4 → target step 5.

- **Task requirement:** count the lines in file_1.txt; the benchmark answer is **29**.
- **Error:** “I opened file_1.txt to count the number of lines.”
- **Contradiction:** source pre, source post, target pre, and target post all show the same archive
  listing; no file contents were displayed.
- **Reuse and observed harm:** after receiving that error in its prompt, the target says it has
  reviewed the file, answers **5**, and ends the task. This was classified as **WRONG_ACTION**
  and **PREMATURE_TERMINATION**. The evaluator reports: “Incorrect answer: 5 (expected: 29).”

![Static archive listing showing file names but no file contents](/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_20260821_01/audit/raw/runs/01M0HAPBKPN5HJHFB6HQ3ME74M/blobs/sha256/fa/fac11ee4069c1cfdfab07e7efa25d1abf4c6c2dbdcd89db9d7e7bec732d02246)

*The same archive-listing screenshot appears before and after the claimed file-opening action and
at the target decision. The model never observed the contents of file_1.txt before answering 5.*

## GELab-Zero-4B

数据源：117-task curated set；117/117 task evidence coverage
完整。Dataset SHA-256：
aa7d7a7d1ff964097cf077fede57ed336257912d00c11cfbab424e4cebe706a8。

### Previous-history representation

GELab 使用 **rolling summary**。下一次请求只接收最近一个 parser-accepted response
中的 `summary`（需要时再附上 ask-user reply）和当前截图；新 summary 会替换旧 summary，
空或缺失 summary 则渲染为 `暂无历史操作`。旧 raw response、reasoning、action、summary
和截图都不回放。该 “after the operation” summary 与 action 在同一个 response 中、且在
环境执行前生成，所以仍是 actor-authored claim，不是独立 post-state verdict。

完整 reconstruction 保留了全部3,691次 summary exposure；outcome-blind retrieval 从中
生成并审核450个 candidate-chains，每个 task 最多4个。因此下面的 MHR 及其局部伤害
属性是统一严格 rubric 在该 reviewed set 上的结果，不把未审核的 prompt exposure 自动
标成 MHR。

### Task-level results

- 成功：**18/117**，SR = **15.38%**。
- 失败：**99/117**；no result：0。
- 全部117个 tasks 中，**33/117（28.21%）**出现 MHR；其中
  **29/117（24.79%）**观察到局部伤害。

| Outcome stratum | Tasks | Tasks with MHR | MHR tasks with observed local harm |
| --- | ---: | ---: | ---: |
| Failure | 99 | **30/99（30.30%）** | **26/99（26.26%）** |
| Success | 18 | **3/18（16.67%）** | **3/18（16.67%）** |

- 最终失败连接：直接停止 **0/99（0%）**；间接带偏
  **14/99（14.14%）**。另有2条 adapter/parser 边界轨迹不归入这两类。

### Reuse-instance results

- MHR：52 reviewed reuse instances，分布在33个 tasks。
- 其中观察到局部伤害：44 reviewed reuse instances，分布在29个 tasks。

| Observed effect | Affected MHR instances |
| --- | ---: |
| **UNNECESSARY_ACTION** | 6 |
| **WRONG_ACTION** | 22 |
| **REPEATED_ACTION** | 31 |
| **PREMATURE_TERMINATION** | 0 |
| **OFFTRACK_CONTINUATION** | 26 |
| **RECOVERED** | 2 |

效果允许重叠，所以该表不能纵向相加得到44。

### Prompt persistence

- 117 条轨迹共有 **3,808 个 actor prompts**，其中 **3,691 个**包含 earlier-step
  rolling summary；每个 history-bearing prompt 恰好只包含一个 summary，因此全部
  earlier-step-history appearances 也是 **3,691 次**。
- 52 个 MHR reuse instances 对应 **52 个 distinct errors carried forward**。
- 这些 errors 在后续 actor prompts 中累计精确出现 **52 次**，即全部
  earlier-step-history appearances 的 **52/3,691（1.41%）**。
- **0/52** errors 被搬进不止一个后续 prompt。
- 每个 error 被搬进后续请求数的中位数为 **1**，最长为 **1**。
- 44 个观察到局部伤害的 MHR uptake 全部发生在下一步：lag 中位数为 **1 步**，最远也为
  **1 步**。

这与 rolling-summary 机制一致：新的 summary 会替换旧 summary，所以一个固定
source step 的 exact error 只直接进入下一次请求。如果下一步又生成了语义相似的错误
summary，按本报告冻结的 provenance 定义，它是一个新的 earlier-step error，而不是把
两个 source steps 合并成一个 error。因此较短的 exact persistence 是 history
representation 的结构属性，不能解释成模型更安全。

### Real examples

#### Example 1: event start time mistaken for departure time

CheckDepartTimeTask, source step 3 → target step 4; task score 0.

- **Task requirement:** determine whether the CoolHacks email provides a departure time; if it
  does not, send Carl the specified fallback question.
- **Error:** “I have successfully opened the 'CoolHacks' email and found the departure time.”
- **Visible contradiction:** the email only states that the hackathon takes place on
  **November 10 at 9:00 AM**. It does not provide a departure time.
- **Reuse and observed harm:** the next decision again says the departure time was found and
  proceeds toward messaging that information instead of taking the missing-information fallback.
  This was classified as **OFFTRACK_CONTINUATION**.

![CoolHacks email giving the event start time but no departure time](/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_20260821_01/audit/raw/runs/01M0JSPDHJ073675315FBW27BN/blobs/sha256/c5/c5e7a0c2f9747c2df92b2fe05fc39390c1cf877fbcc398f72e25c9d7fac12ab4)

![Inbox after the erroneous departure-time summary was carried forward](/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_20260821_01/audit/raw/runs/01M0JSPDHJ073675315FBW27BN/blobs/sha256/3f/3f1fe6cc1bb23867aa6d7d02df528af1610b79e5ac763f0029251abe90c87c28)

*The email supplies an event start time, not a departure time. The rolling summary converts it
into a found departure time, and the next decision continues from that error.*

#### Example 2: still on the login page after claiming login succeeded

ItemCheckoutTask, source step 9 → target step 10; task score 0.

- **Task requirement:** log in to Taodian and purchase an iPhone 15 Pro.
- **Error:** “目前已完成短信验证码登录，下一步是进入购物车页面，找到商品并下单。”
- **Visible contradiction:** the GUI is still on “用户登录”; the terms checkbox remains
  unchecked, so login has not completed.
- **Reuse and observed harm:** the next decision preserves the completed-login premise but clicks
  “同意协议并登录” again. The screen remains on the same login form. This was classified as
  **REPEATED_ACTION**.

![Taodian login page contradicting the completed-login summary](/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_20260821_01/audit/raw/runs/01M0JSPDHJ073675315FBW27BN/blobs/sha256/3b/3b87edd87c3451e1c45b8d987447f99dacf34461a73d0a60c2fec45c35065e9e)

![Same login page after the repeated login action](/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_20260821_01/audit/raw/runs/01M0JSPDHJ073675315FBW27BN/blobs/sha256/58/58dba5e31c47df71efd89de6147b7455aedc7c9938758a3df1a6b944db525680)

*Before and after the target action, the task is visibly still at login despite the carried-forward
claim that SMS login was complete.*

#### Example 3: a second click cancels an already completed favorite

MastodonConditionalFavoTask, source step 46 → target step 47; task score 1.

- **Task requirement:** favorite all `#dogs` posts unless they are already favorited or
  bookmarked.
- **Stale error:** “I have successfully favorited four posts. I am now proceeding to favorite the
  fifth post in the '#dogs' topic list.”
- **Visible contradiction:** before the target decision, the visible post already has a filled
  blue star and favorite count 1.
- **Reuse and observed harm:** the target treats the same visible post as the next unfinished item
  and clicks its star again. The star becomes empty and the count changes from 1 to 0. This was
  classified as **REPEATED_ACTION** and **WRONG_ACTION**.
- **Trajectory outcome:** the task eventually receives score 1. This is a direct example of an
  MHR with observed local harm coexisting with eventual task success.

![Post already favorited before the target decision](/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_20260821_01/audit/raw/runs/01M0JSPDHJ073675315FBW27BN/blobs/sha256/e0/e062a2dc7458e8dbaa7d1765c2794c029741a6f502fc2a879e2192e75d4c9a09)

![Same post unfavorited by the repeated click](/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_20260821_01/audit/raw/runs/01M0JSPDHJ073675315FBW27BN/blobs/sha256/c5/c5923eafa2118788fbdbdbf035fa5f3bae2cb7b06b14e14b596773e6d35c6b20)

*The carried-forward summary says the visible item still needs to be favorited. It is already
favorited; reusing that stale status immediately undoes the completed action.*

## UI-Venus-1.5-8B

数据源：117-task curated set；117/117 task evidence coverage
完整。Dataset SHA-256：
2e12cb879a2e66eda739e9fd25d9c2ddabd95f9e5b21b1c14c973d5017022c6d。

### Previous-history representation

UI-Venus 使用 **flat previous actions**。正式配置 `history_length=0` 明确表示无限历史：
每个 prior step 都累计渲染为零基的
`Step N: <think>...</think><action>...</action>`。`<conclusion>`、status、完整 raw
response 与所有旧截图都被排除，请求中只有当前 RGB screenshot；parse-failed response
也会成为 StepData 并可能继续出现。因此这些条目主要记录命令、意图与推理；准确保留
一个 action 并不能证明该 action 的效果已经发生，失败或偏航 action 也不能被误标为
错误视觉观察。

完整 reconstruction 保留全部91,058次 source-to-later-prompt appearances；共享
outcome-blind selector 从中审核264个 candidate-chains，覆盖71个 tasks，单 task 最多6个。
未进入 cards 的 prompt exposure 不会被自动标成 MHR。

### Task-level results

- 成功：**15/117**，SR = **12.82%**。
- 失败：**102/117**；no result：0。
- 全部117个 tasks 中，**3/117（2.56%）**出现 MHR；其中
  **1/117（0.85%）**观察到局部伤害。

| Outcome stratum | Tasks | Tasks with MHR | MHR tasks with observed local harm |
| --- | ---: | ---: | ---: |
| Failure | 102 | **3/102（2.94%）** | **1/102（0.98%）** |
| Success | 15 | **0/15（0%）** | **0/15（0%）** |

- 最终失败连接：直接停止 **0/102（0%）**；间接带偏
  **2/102（1.96%）**。

### Reuse-instance results

- MHR：3 reviewed reuse instances，分布在3个 tasks。
- 其中观察到局部伤害：1 reviewed reuse instance，分布在1个 task。
- 另外2个 MHR instances 未观察到局部伤害（**NO_VISIBLE_HARM**）。

| Observed effect | Affected MHR instances |
| --- | ---: |
| **UNNECESSARY_ACTION** | 0 |
| **WRONG_ACTION** | 1 |
| **REPEATED_ACTION** | 0 |
| **PREMATURE_TERMINATION** | 0 |
| **OFFTRACK_CONTINUATION** | 1 |
| **RECOVERED** | 0 |

效果允许重叠，所以该表不能纵向相加得到1。

### Prompt persistence

- 117 条轨迹共有 **4,112 个 actor prompts**，其中 **3,995 个**包含 earlier-step
  history；所有 source-step history entries 在后续 prompts 中累计出现 **91,058 次**。
- 3 个 MHR reuse instances 对应 **3 个 distinct errors carried forward**。
- 这些 errors 在所有后续 actor prompts 中累计精确出现 **41 次**，即全部
  earlier-step-history appearances 的 **41/91,058（0.05%）**。
- **3/3** errors 被搬进不止一个后续 prompt。
- 每个 error 被搬进后续请求数的中位数为 **9**，最长为 **26**。
- MHR uptake lag 中位数为 **2 步**、最远为 **3 步**；唯一观察到局部伤害的 MHR lag 为
  **3 步**。

41 次只表示这些 strict errors 的 prompt persistence；正式确认的 MHR reuse instances
仍是3次，其中1次具有 observed harm。

### Real examples

#### Example 1: wrong calendar event treated as successfully scheduled

ScheduleCoffeeTimeViaSmsTask, source step 9 → target step 10; task score 0.

- **Task requirement:** check a text-message invitation and, if available, schedule the matching
  coffee event and reply OK.
- **Error:** “Since the event has been successfully scheduled ...” followed by `PressHome()`.
- **Visible contradiction:** the calendar contains Coffee Time on **October 16 at 1:00 PM**,
  rather than the invitation's **October 20 at 9:10 AM**.
- **Reuse:** the target carries forward the completed-scheduling premise and proceeds to notify
  the sender. Opening Messages is independently required by the task, so this instance has
  **NO_VISIBLE_HARM**, even though the history claim is refuted.

![Coffee Time event with the wrong date and time](/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0M2QKK2PRPEA3VXAKZ36W4B/blobs/sha256/e6/e6f8c33a66fdaadab3469ffe1f7a54f710007d45dfa35e8ce6b16b619d424fad)

![State after leaving the incorrectly scheduled event](/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0M2QKK2PRPEA3VXAKZ36W4B/blobs/sha256/b1/b1e1d8e3acc2da73a4345bf6661e57e51cb7256bd79b6676d81f75a90cd0c12a)

*The history upgrades an incorrectly configured calendar entry into a successful scheduling
claim. The next navigation is still independently appropriate, which is why this is MHR without
observed local harm.*

#### Example 2: upper-left close control called a bottom-left back arrow

MastodonMallPurchaseCommodityTask, source step 24 → target step 26; task score 0.

- **Error:** the image viewer has “a back arrow at the bottom left.”
- **Visible contradiction:** the screen instead shows a close control at the upper left.
- **Reuse:** the target repeats the same interpretation and click. The action still closes the
  viewer and returns to the feed, so the strict MHR instance has **NO_VISIBLE_HARM**.

![Image viewer with an upper-left close control](/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0M2QKK2PRPEA3VXAKZ36W4B/blobs/sha256/0c/0c8e47d65cd5db6d8c1eceece5f7590327e51008e4d38ebad6e8f175957e4c18)

![Feed after the repeated click closes the viewer](/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0M2QKK2PRPEA3VXAKZ36W4B/blobs/sha256/97/9798bd709dffa97373343f2830153d825056f27c4b3b548b4a4ca7a5f734c02c)

*The control description is visibly wrong, but its coordinates still perform the useful close
operation. This separates invalid history from observed harm.*

#### Example 3: stale compose-screen premise exits Mastodon

MastodonRemoveBookmarkTask, source step 20 → target step 23; task score 0.

- **Task requirement:** remove bookmarked posts carrying the `#pets` tag.
- **Stale error:** the carried history says the current screen is a new-post interface and that
  Back is needed to return to bookmarks.
- **Visible contradiction:** the target pre-state is already the Saved feed.
- **Reuse and observed harm:** the target explicitly relies on the stale premise and presses
  Back, leaving Mastodon for the Android home screen. This was classified as
  **WRONG_ACTION** and **OFFTRACK_CONTINUATION**.

![Saved feed already visible before the target decision](/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0M2QKK2PRPEA3VXAKZ36W4B/blobs/sha256/85/8571c7ffe5e11f3c2e37583a1d34dfb6dab00c7ae1d60b5ab21fb75348db736c)

![Android home screen after the stale Back action](/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0M2QKK2PRPEA3VXAKZ36W4B/blobs/sha256/9c/9cf59c0fe61c26e8a1057250d9706dd89915267589c92b5a1e6470429b476ffe)

*The source wording may once have described a different screen, but at the target it is stale.
Following it exits the relevant app and supplies the sole UI-Venus MHR case with observed local
harm.*

## GUI-Owl-1.5-8B-Instruct

数据源：117-task curated set；117/117 task evidence coverage 完整。Dataset SHA-256：
d21000e371f2153595f2b19fa295052533e8f37a155a4fc934f4313518d0e018。

### Previous-history representation

GUI-Owl 使用 **hybrid collapsed history**（冻结字段为 `hybrid_folding`）。正式配置
`history_n=1` 把当前 observation 也计算在窗口内，因此没有 prior raw message pair：
所有已接受步骤的短 `Action:` imperative 都累计折叠为
`StepN: <action text> Tool response: <external result>`，result 与下一 observation
（N+1）对齐，请求只携带当前截图。旧 thinking、完整 assistant response、tool-call JSON、
坐标与旧截图都不回放。

审计时把 actor-authored action text 与外部 result 分开：前者作为“已完成动作记录”，
同时核对 parsed action 和实际 `action_execution_started`；后者只是下一 observation
提供的 tool/ask-user evidence。一个被准确记录但本身偏航的动作是
`TRUE_BUT_OFFTRACK`，不是错误视觉观察；action text 与实际执行动作不一致时，才可能
形成 `RESULT_MISALIGNMENT`。这正是 GUI-Owl 与 summary/progress 型模型需要不同
representation mapper、但仍使用相同 MHR 与局部伤害指标的原因。

完整 reconstruction 保留全部89,313次 source-to-later-prompt appearances。
outcome-blind retrieval 生成474个 action-history candidates，覆盖全部117个 tasks；
其中29个是跨10个 tasks 的高置信 action-text/execution mismatch。普通任务候选预算为4，
但所有高置信 mismatch 都保留，因此一个 task 最多有16个 candidates。

### Task-level results

- 成功：**34/117**，SR = **29.06%**。
- 失败：**83/117**；no result：0。
- 全部117个 tasks 中，**11/117（9.40%）**出现 MHR；其中
  **7/117（5.98%）**观察到局部伤害。

| Outcome stratum | Tasks | Tasks with MHR | MHR tasks with observed local harm |
| --- | ---: | ---: | ---: |
| Failure | 83 | **9/83（10.84%）** | **7/83（8.43%）** |
| Success | 34 | **2/34（5.88%）** | **0/34（0%）** |

- 最终失败连接：直接停止 **0/83（0%）**；间接带偏 **4/83（4.82%）**。

### Reuse-instance results

- MHR：27 reviewed reuse instances，分布在11个 tasks。
- 其中观察到局部伤害：23 reviewed reuse instances，分布在7个 tasks。
- 另外4个 MHR instances 未观察到局部伤害（**NO_VISIBLE_HARM**）。

| Observed effect | Affected MHR instances |
| --- | ---: |
| **UNNECESSARY_ACTION** | 0 |
| **WRONG_ACTION** | 5 |
| **REPEATED_ACTION** | 23 |
| **PREMATURE_TERMINATION** | 0 |
| **OFFTRACK_CONTINUATION** | 4 |
| **RECOVERED** | 1 |

效果允许重叠，所以该表不能纵向相加得到23。一个 task
（AdjustFontIconMaximumTask）贡献15/27个 strict instances；因此 task-level prevalence
仍是主要统计量，instance count 用于描述 task 内部重复链条。

### Prompt persistence

- 117 条轨迹共有 **4,117 个 actor prompts**，其中 **4,000 个**包含 earlier-step
  history；所有 action-history entries 在后续 prompts 中累计出现 **89,313 次**。
- 27 个 MHR reuse instances 对应 **27 个 distinct source action entries**。
- 这些 strict errors 在所有后续 actor prompts 中累计精确出现 **551 次**，即全部
  earlier-step-history appearances 的 **551/89,313（0.62%）**。
- **25/27** errors 被搬进不止一个后续 prompt。
- 每个 error 被搬进后续请求数的中位数为 **22**，最长为 **47**。
- MHR uptake lag 中位数为 **1 步**、最远为 **3 步**；观察到局部伤害的 MHR lag 中位数同样为
  **1 步**、最远为 **3 步**。

### Real examples

#### Example 1: history says click, but the source action was wait

DownloadSendReceiptTask, source step 5 → target step 6; task score 0.

- **History record:** “Click on the email from William with the subject 'Reimbursement
  Information' to open it.”
- **Execution mismatch:** the source prediction's tool call and the parsed/executed source action
  are both `wait`, not click.
- **Reuse:** after the incorrect completed-action record enters the next prompt, the target repeats
  it and actually clicks the email. This is **RESULT_MISALIGNMENT** with
  **NO_VISIBLE_HARM** at that local step because opening the relevant email is useful.

![Inbox before the source wait action](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/6b/6b715b4f7d1a0bedcccf70c3fbed864ba90f7659cdb76bf2ab7f030ce26b49a4)

![Same inbox before the target follows the incorrect click record](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/69/6997aee30634be87eed5999cbb3c142ae04913e3f5f71b61ad10e7c518abae51)

![Email opened by the target click](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/34/3419212da12abeaddc46014cac804534f73f525c9938b8aab9b1a28d744a1a9c)

*The folded text claims that click already happened, while the exact source action was wait. The
next decision explicitly adopts the action record and performs the click.*

#### Example 2: “turn it off” first turns the toggle on, then is repeated to recover

CheckSetMeetTimeTask, source step 10 → target step 11; task score 0.

- **History record:** “Click on the 'All-day' toggle switch to turn it off.”
- **Visible contradiction:** the source pre-state shows the toggle already off; the source click
  turns it on.
- **Reuse and observed harm:** the target repeats the same “turn it off” action and clicks again,
  restoring the toggle to off. This was classified as **WRONG_ACTION** and
  **REPEATED_ACTION**, followed by **RECOVERED**.

![All-day toggle already off before the source click](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/9b/9b5f8502a1a2e96f4ff200f22fb6ab2f87f105dd93758f73785ede8256227a93)

![All-day toggle on after the source action](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/dc/dc5f5ed41d1988569dd9182cbdc569cf5b8bf81708677a24c7c771f5fa76815c)

![All-day toggle off again after the repeated target click](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/9e/9e2dae21903b73db3bd6bdb8df40757c920c1751149911f80e5c06292c23e926)

*The textual purpose is false at the source: the click turns an already-off toggle on. Repeating
the same carried record happens to undo the error.*

#### Example 3: repeated swipe moves away from Chinese Simplified

MastodonChangeLanguageTask, source step 40 → target step 41; task score 0.

- **History record:** “Scroll down to find Chinese Simplified in the list.”
- **Action/result mismatch:** the actual drag moves the language list toward earlier alphabetic
  entries rather than toward Chinese Simplified.
- **Reuse and observed harm:** the target explicitly repeats the same directional drag and moves
  farther away. This was classified as **WRONG_ACTION**, **REPEATED_ACTION**, and
  **OFFTRACK_CONTINUATION**.

![Language list before the source drag](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/c0/c04b6aab85be517175111fc6e287aa541becde56d3e3bffa8dedbe2f18d78263)

![Language list after the source drag and before reuse](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/32/322e44ef70005a92e5f95d118debe99269808d52deb98112cbf120de07f72800)

![Language list after the repeated target drag](/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01/audit/raw/runs/01M0NK5E8YZM21NA5WZ7Q3A9MD/blobs/sha256/1a/1ac773389eddb8b4c1bcbc527472f523ef48e901e63d76a2db614ad27b17756c)

*The audit checks the recorded action's direction and purpose against the exact executed drag,
rather than treating the imperative sentence as a generic visual observation.*

## MemGUI-8B-SFT

数据源：117-task curated set；117/117 task evidence coverage
完整。Dataset SHA-256：
cfc82e42b3a726c42213e7b6985ce2fc0391f471c98d7cd2119ecb880e828588。

### Previous-history representation

MemGUI 使用 **structured folding**，每个请求只携带当前 screenshot 与三层 actor-managed
text state：

- **H — Folded Action History**：模型把一个 step 或连续 span 压成 summary；新 fold
  会删除所有与它重叠的旧 span summary，再插入新版本。
- **L — Recent Step Record**：只保留最近一个已接受步骤的 model-authored
  UI Observation、Action Intent 和 runtime-derived action summary；下一步会覆盖它。
  普通 L action summary 通常只保留 action type，不保留坐标、输入文字或外部执行结果
  `R_t`。
- **M — Folded UI State**：按 `memory_add`、`memory_update`、`memory_delete` 维护的持久
  UI memory，直到模型显式修改或删除。

旧 raw assistant reply、thinking 与旧截图都不回放；H/L/M 也只有在整条 response 通过
parse、action conversion 与 memory precondition 后才原子提交。因此审计按 H、L、M
entry-version 分层重建 provenance：它们是 self-authored state，不是已验证事实；fold
的破坏性替换和 L 对执行结果的省略都必须在判断 validity 与 harm 时保留。

完整 reconstruction 含 **5,219 个 unique history entry versions**（H=2,545，L=2,662，
M=12）及 **27,660 次**后续 prompt appearances（H=24,736，L=2,662，M=262）。
outcome-blind selector 审核302个 candidates，覆盖92个 tasks，单 task 最多4个；所有12个
M versions 与117个 span-H candidates 都被显式纳入，避免只审普通短文本而忽略 MemGUI
特有的 structured state。

### Task-level results

- 成功：**22/117**，SR = **18.80%**。
- 失败：**95/117**；no result：0。
- 全部117个 tasks 中，**27/117（23.08%）**出现 MHR；其中
  **18/117（15.38%）**观察到局部伤害。

| Outcome stratum | Tasks | Tasks with MHR | MHR tasks with observed local harm |
| --- | ---: | ---: | ---: |
| Failure | 95 | **24/95（25.26%）** | **17/95（17.89%）** |
| Success | 22 | **3/22（13.64%）** | **1/22（4.55%）** |

- 最终失败连接：直接停止 **2/95（2.11%）**；间接带偏
  **7/95（7.37%）**。

### Reuse-instance results

- MHR：38 reviewed reuse instances，分布在27个 tasks。
- 其中观察到局部伤害：27 reviewed reuse instances，分布在18个 tasks。
- 另外11个 MHR instances 未观察到局部伤害（**NO_VISIBLE_HARM**）。

| Observed effect | Affected MHR instances |
| --- | ---: |
| **UNNECESSARY_ACTION** | 4 |
| **WRONG_ACTION** | 10 |
| **REPEATED_ACTION** | 18 |
| **PREMATURE_TERMINATION** | 4 |
| **OFFTRACK_CONTINUATION** | 11 |
| **RECOVERED** | 1 |

效果允许重叠，所以该表不能纵向相加得到27。

全部12个 selected M versions 的最终 validity 为10个 `SUPPORTED`、1个
`OFFTRACK_TRUE`、1个 `UNVERIFIABLE`，没有 `REFUTED`/`STALE`，因此本次 strict MHR
不是由 M 驱动。117个 selected span-H candidates 中，14个满足 MHR，其中10个观察到
局部伤害；全部 strict instances 的 claim type 为17个
`SUMMARY_CLAIM`、19个 `OBSERVATION_CLAIM` 与2个 `SUCCESS_CLAIM`，表明 H 与 L
都贡献了 strict findings。

### Prompt persistence

- 117 条轨迹共有 **2,779 个 actor prompts**，其中 **2,662 个**包含 earlier-step
  structured history；H/L/M entries 在后续 prompts 中累计出现 **27,660 次**。
- 38 个 MHR reuse instances 对应 **38 个 distinct exact entry versions**：H=19、L=19、
  M=0。
- 这些 strict entry versions 在后续 prompts 中累计精确出现 **153 次**，即全部
  history appearances 的 **153/27,660（0.55%）**；其中 H=134、L=19。
- **10/38** strict entries 被搬进不止一个后续 prompt。
- 每个 strict entry 被搬进后续请求数的中位数为 **1**，最长为 **31**。
- MHR 的 uptake 全部发生在下一步；其中观察到局部伤害的 MHR 也全部发生在下一步：
  lag 中位数与最大值均为 **1 步**。

这里的 identity 是 reconstruction 中的 `history_entry_id`，不是仅按文本、fold range、
source step 或 memory ID 去重。一个 L error 后来被新的 H fold 重新表述时，两者是两个
有独立来源的 exact entry versions。

### Real examples

#### Example 1: recent-step record says an inactive search bar is active

SearchItemAndCheckoutTask, L entry, source step 5 → target step 6; task score 1.

- **L claim:** the UI Observation says the search bar is active and the Action Intent is to type
  “临时纹身”.
- **Visible contradiction:** there is no cursor or keyboard, and the source `input_text` leaves
  the GUI unchanged.
- **Reuse and observed harm:** the target again says the search bar is active and repeats the
  same `input_text`. This was classified as **REPEATED_ACTION**. The trajectory later succeeds,
  showing that MHR with observed local harm can coexist with final task success.

![Unchanged shopping screen with no active search cursor or keyboard](/shared/linqiang/mobileworld_audit_data/memgui_8b_sft_gui117_g4_20260823_01/audit/raw/runs/01M0P576JF73HFWD85PEKNP35V/blobs/sha256/81/81a201f44049f6e82a58042f6b28fda7a6aa86afe49b651b96637b20236d4008)

*The same pixels occur before and after both reviewed actions. L carries the model's active-field
observation forward even though the external result is not represented in L.*

#### Example 2: folded history treats a static download icon as an active transfer

DownloadSendReceiptTask, H entry, source step 19 → target step 20; task score 0.

- **H claim:** “[Steps 6-18] Initiated and waited for the download of 'receipt.jpg' ... The
  download is still in progress.”
- **Visible contradiction:** the static attachment/download icon does not establish that a
  transfer remains in progress.
- **Reuse and observed harm:** the target adopts the “still in progress” premise and then leaves
  the attachment page without verifying or completing the download. This was classified as
  **PREMATURE_TERMINATION**.

![Static receipt attachment before reuse](/shared/linqiang/mobileworld_audit_data/memgui_8b_sft_gui117_g4_20260823_01/audit/raw/runs/01M0P576JF73HFWD85PEKNP35V/blobs/sha256/fd/fdaabb534afce9395b31388feb15802dc69bdd96af22e07b5e9eec7ca94923dc)

![State after leaving the attachment page](/shared/linqiang/mobileworld_audit_data/memgui_8b_sft_gui117_g4_20260823_01/audit/raw/runs/01M0P576JF73HFWD85PEKNP35V/blobs/sha256/1c/1cf26b4aa71a64f7106ace46addff56e76fedb9a1eeba559dc4b1d01dfe53186)

*The folded H summary compresses many steps into an unverified transfer-status claim. The next
decision relies on that claim rather than checking an external result.*

#### Example 3: folded cleanup summary contradicts itself and the folder

LocalFileManagementTask, H entry, source step 7 → target step 8; task score 0.

- **H claim:** the summary lists two ZIP files from 2023 but also claims that no files older than
  one year remain after scrolling.
- **Contradiction:** the trajectory contains no delete action, and the folder still visibly
  contains an old 2024 ZIP relative to the task date.
- **Reuse and observed harm:** the target trusts the completed-cleanup premise, leaves Files, and
  opens Mattermost. This was classified as **OFFTRACK_CONTINUATION**.

![Downloads folder before the contradictory fold](/shared/linqiang/mobileworld_audit_data/memgui_8b_sft_gui117_g4_20260823_01/audit/raw/runs/01M0P576JF73HFWD85PEKNP35V/blobs/sha256/2e/2efa6fde4ead30492a53062d25398abb60838adbbd6bb3a3dce32b23011f23f1)

![Downloads folder still containing old files at the target](/shared/linqiang/mobileworld_audit_data/memgui_8b_sft_gui117_g4_20260823_01/audit/raw/runs/01M0P576JF73HFWD85PEKNP35V/blobs/sha256/b9/b974c0a9967757c8d0d84b62b967822fce37574704ea0fd904d6e670fd9868ea)

![Mattermost after the target leaves Files](/shared/linqiang/mobileworld_audit_data/memgui_8b_sft_gui117_g4_20260823_01/audit/raw/runs/01M0P576JF73HFWD85PEKNP35V/blobs/sha256/d0/d000280b9f632bf7d20fad0bf3fb278da6a4ff6a0aac2fac4ba3cd7d122fbc47)

*Structured folding can preserve a compact contradiction after the detailed source steps and
old screenshots have disappeared. The target treats that compressed state as complete.*

## Motivation：MHR 是值得解决的任务失败风险点

六个模型各执行117个 benchmark tasks，共得到 **702个 model–task cases**：其中
**128个成功、574个失败**。以下全部以完整 case 为单位；一个 case 即使包含多条 MHR
reuse instances，也只计一次。

| Outcome | Model–task cases | Cases with MHR | MHR cases with observed local harm |
| --- | ---: | ---: | ---: |
| Success | **128** | **8/128（6.25%）** | **4/128（3.13%）** |
| Failure | **574** | **108/574（18.82%）** | **90/574（15.68%）** |
| **Total** | **702** | **116/702（16.52%）** | **94/702（13.39%）** |

成功的128个 cases 中有8个出现 MHR，其中4个仍出现局部 observed harm：分别涉及重复
动作、错误动作或持续偏航，其中1个后来恢复。它们最终成功，说明 MHR 与局部 harm
并不必然让任务失败。

失败的574个 cases 中有108个出现 MHR，接近全部失败的五分之一；其中
**90/108（83.33%）**已经在至少一条严格 MHR 复用链中观察到局部伤害：错误 history
实际进入后续 prompt、被后续决策明确复用，并在同一审核链上观察到局部 harm。按受影响
的失败 case 去重：

| 严格 MHR 复用链中观察到的局部 effect | 受影响的失败 cases |
| --- | ---: |
| 重复动作（REPEATED_ACTION） | **65** |
| 错误动作（WRONG_ACTION） | **50** |
| 持续偏航（OFFTRACK_CONTINUATION） | **41** |
| 不必要动作（UNNECESSARY_ACTION） | **12** |
| 过早终止（PREMATURE_TERMINATION） | **12** |

同一个 case 可以同时具有多种 effect，因此上表各行不能相加；在局部伤害标注中，6个
cases 至少有一条 chain 后来被标记为 **RECOVERED**。这不等于整个任务的所有错误都已
恢复，也不会撤销此前已经发生的局部伤害。上表不是任务中所有普通错误的统计；每个计数
都绑定到一条经过审核、并观察到相应局部伤害的 MHR 复用链。

最终失败连接审核再问一个更接近终局的问题：MHR 所维持或引出的错误路径是否持续未恢复，
并能否与 evaluator 揭示的最终失败保持可追踪连接。108个“失败且含 MHR”的 cases 中：

- **10个直接停止**：MHR 在最后一个 decision 中被复用，模型随即显式输出 `finished`
  或 `answer`；对应 chain 被审核为 **PREMATURE_TERMINATION** 且未恢复。
- **48个间接带偏**：MHR 被复用后任务仍继续，但错误路径未恢复，并与最终失败保持
  可追踪连接。

两类按 case 互斥，合计 **58/108（53.70%）**；换成六模型全部失败 cases 作分母，就是
**58/574（10.10%）**。其中直接停止为 **10/574（1.74%）**，间接带偏为
**48/574（8.36%）**。

| Model | Failed cases | 直接停止 | 间接带偏 |
| --- | ---: | ---: | ---: |
| MAI-UI | 86 | 0 | 5 |
| Qwen3-VL | 109 | 8 | 16 |
| GELab-Zero | 99 | 0 | 14 |
| UI-Venus | 102 | 0 | 2 |
| GUI-Owl | 83 | 0 | 4 |
| MemGUI | 95 | 2 | 7 |
| **Total** | **574** | **10** | **48** |

局部 harm 与最终失败连接是两个重叠但不同的统计轴，不能相加。另有2条 GELab
adapter/parser 边界轨迹单列；其余48个失败且含 MHR 的 cases 没有建立足够可靠的
MHR-to-final-failure 连接。把2个边界 cases 与这48个 cases 合看，其中仍有35个发生过
局部 harm，不能解释为“无害”。

因此，当前最直接的 motivation 是：**18.82%的失败 cases 含 MHR；在这些 cases 中，
83.33%已经出现局部伤害，53.70%呈现未恢复且与最终失败相连的路径。** 这使 MHR 成为
GUI agent 中具体、反复出现、值得纳入专门修复候选的 failure mode 之一。由于没有“只
纠正或删除 history 后从同一状态重跑”的配对反事实，这仍是观测性证据，不能写成已经
证明这些失败由 MHR 导致，更不能声称它是唯一原因或删除 history 后成功率必然提升。
候选检索还有按任务设置的审核预算，因此未进入 cards 的 history exposures 也不能解释为
已经审核过的 negative cases。

## What the current evidence supports

六份数据均完成 outcome-blind MHR 正式审核；随后对六模型合计的116个 strict-MHR
model–task cases（不是每个模型116个）及其272条 chains 完成最终失败连接审核。两套结果
都保持 `causal_claim_supported=false`。这116个 cases 共含272条 MHR chains；其中94个
cases、239条 chains 观察到局部伤害。
当前证据支持：

- 错误历史确实进入后续 actor prompts，并在 MHR instances 中被明确复用；
- 伴随局部伤害的 MHR instances 同时具有来源明确的错误历史复用和可见的局部 harm；
- 模型生成的 errors 可以被持续搬进许多轮 prompt，而正式确认的 harmful reuse
  通常很快发生；rolling-summary 机制也可能把每个 exact source error 只暴露给下一步；
- 不同 history representation 需要不同的 exact mapper 与 claim semantics：raw replay、
  flat progress、rolling summary、flat previous actions、collapsed action records 以及
  structured H/L/M 不能互相套用同一个文本 heuristic；
- outcome strata 必须按数据集分别解释：MAI-UI-8B、Qwen3-VL-8B 与 UI-Venus-1.5-8B
  的 successful strata 中没有 MHR；GELab-Zero-4B 有3/18个 successful tasks 出现
  MHR，且3个都观察到局部伤害；GUI-Owl 有2/34个 successful tasks 出现 MHR，但都未
  观察到局部伤害；MemGUI 有3/22个 successful tasks 出现 MHR，其中1个观察到局部伤害。
  因而错误历史甚至局部 observed harm 都不等于最终任务必然失败；
- GUI-Owl 的短 imperative history 必须按 executed action record 审核；MemGUI 的
  structured M 候选本次未产生 strict MHR，而 strict findings 来自 H 与 L。这些是
  representation-specific findings，不是模型优劣排名；
- 574个失败 model–task cases 中，**10/574（1.74%）**表现为 MHR 在终止步被复用后
  任务直接停止，**48/574（8.36%）**表现为 MHR 复用后，后续轨迹持续带偏且未恢复；
  两类互斥，合计
  **58/574（10.10%）**。另有2条 adapter/parser 边界轨迹不强行归入任一类。

当前证据不支持：

- MHR（包括观察到局部伤害的 MHR）必然导致最终任务失败；
- 把10、48或58写成已经证明“由 MHR 导致”的任务数；它们都缺少纠正/删除历史后的
  配对反事实，而且许多轨迹还存在其他足以导致失败的问题；
- 所有 later-prompt appearances 都形成 reuse 或 harm；
- 删除、压缩或纠正历史后，SR 会提高某个数值；
- text family 数量等于语义错误类别数量；
- 把8个 successful controls 当作匹配反事实对照，或把其余48个含 MHR 的失败任务
  强行归入直接停止 / 间接带偏任一侧；
- 不同模型的候选检索预算、history persistence 和 representation 不同，因此本报告的
  task rates 不能脱离各 section 的 evidence scope 直接解释为受控的模型排名。

## Methods appendix: final definitions

最终结果使用以下固定映射：

| Reader-facing term | Frozen internal field |
| --- | --- |
| MHR | strict_explicit_use |
| MHR 的局部伤害属性 | strict_harm |
| Reviewed reuse instance | candidate-chain |
| Direct stop | final audit supports an unrecovered link to failure; reuse occurs in the last decision, which explicitly returns `finished` or `answer` and is labeled `PREMATURE_TERMINATION` |
| Indirect derailment | final audit supports an unrecovered link to failure; the task has no direct stop and contains a later trajectory after an earlier reuse target |

内部 MHR 门槛还要求 evidence coverage 完整、error 实际进入请求、来源为
**EXACT** 或
**HIGH**、history validity 为 **REFUTED** 或 **STALE**，并且 state confound 为 **NONE**
或 **CURRENT_GUI_CONTRADICTS_PREMISE**。MHR 的局部伤害属性在此基础上记录是否至少
含一种既有 harmful effect。

最终失败连接统计先取审核支持“与失败保持连接”且对应 chain 的
`recovery_status=NOT_RECOVERED` 的任务集合，再从 cards 中机械检查
`reuse_target_step` 与 `task_ended.termination`：最后一个 decision 复用错误历史并显式
输出 `finished` 或 `answer`，对应 chain 含 `PREMATURE_TERMINATION`，且之后没有恢复机会
的归为 direct；没有 direct、但较早复用之后存在未恢复且与最终失败相连路径的归为
indirect。若两者同时存在，direct 优先。
该二次映射按 task 去重，不是新的模型裁决字段。两条 GELab 轨迹虽在复用当步被 runtime
终止，但停止的近端机制是 `HOME` 被 adapter 解析为 `unknown`；冻结审核未确认 MHR 直接
触发该 parser termination，且没有后续 suffix，因此将其保留为边界案例，不归入两类。

审核顺序保持 outcome separation：MHR 判断及其局部伤害属性先在不知道 task outcome、
score 与 evaluator reason 的条件下完成；恢复状态、路径连续性与 competing defects 固定
后，最终失败审核才读取 outcome/evaluator 检查终局连接，而且不能改写既有 MHR 判断或
局部伤害标注。所有最终 causal count 字段保持 null。

后续增加模型时，应复用完全相同的 MHR、局部伤害属性、task、reuse-instance、
carried-forward-error、prompt-persistence 和 lag 定义，只新增模型 section，不修改既有
口径。
