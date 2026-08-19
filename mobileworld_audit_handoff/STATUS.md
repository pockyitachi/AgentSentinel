# Project Status：GUI Agent Previous-Step Misleading Motivation Study

Last updated: 2026-08-18 (America/Toronto)

## 1. Project objective

项目长期目标是研究一个面向 GUI agent 的运行时 Sentinel/middleware：在 agent做当前 GUI决策前，检查注入 prompt 的 previous-step 信息是否被后续GUI证据反驳、是否已失效、是否导致轨迹偏航，并在未来选择不注入、纠正或降低其权重。

核心例子：

```text
旧历史声称：已进入目标商品页 / 已加入购物车
当前GUI证据：实际在用户资料页 / 购物车为空
风险：该旧历史仍进入下一步prompt，模型可能以它为前提继续行动或提前结束
```

长期 Sentinel还可能根据 task在开始时生成 milestones/rubrics，并随可见证据更新 rubric state，用来判断 agent是否仍走在任务轨迹上。但这不是当前开发阶段。

## 2. Current stage and decision

当前只做 motivation validation：先确定 wrong/stale/off-track previous-step exposure是否真实、普遍、严重，是否值得支撑 proposal。

已确认的工程决策：

```text
Collection
  event-sourced
  lossless with respect to observable runtime data
  label-free
  immutable/append-only
  feature-gated

Evaluation
  fully offline
  versioned
  replaceable/re-runnable
  never rewrites raw
```

因此，第一次错误 taxonomy即使设计不合理，只需重跑离线 evaluator，不需要重新运行昂贵的 MobileWorld任务。

当前不实现 Sentinel、rubric、filter或在线LLM judge。

## 3. Canonical notation

```text
T       task instruction
S_t     当前决策前GUI observation
I_t     模型实际收到的完整request
P_t     agent.predict返回给runner的exact prediction；可由一个或多个provider response组装/转换
A_t     解析后的JSONAction
R_t     action执行的transport/tool/user evidence
S_{t+1} action后的GUI observation
H_t     I_t中实际注入的history部分
```

本交接包统一使用 `P_t` 表示模型输出，状态始终使用 `S_t`，action result使用 `R_t`；与 `PROJECT_CONTEXT.md` 一致。

## 4. Repository baseline

Mac当前官方clone位置曾为：

```text
/Users/apigo/Desktop/agent monitor/MobileWorld
```

服务器路径可以不同，以含 `src/mobile_world` 的仓库为准。

Frozen baseline：

```text
repository: Tongyi-MAI/MobileWorld
branch: main
commit: 0dcd0980eac64d76f498f93568a1ec0594b743c4
commit date: 2026-08-04
```

内置registered adapters（9）：

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

动态 `.py` agent仍被框架支持。UIINS是 `planner_executor`内部grounder，不算第十个顶层agent。

## 5. Source audit findings

所有九个顶层agent都会把某种历史表示放入下一步模型输入，但不是同一实现。

| Family | Agents | Prompt history behavior |
|---|---|---|
| Raw chat replay | Seed, General E2E, MAI-UI, Planner-Executor | 旧response以assistant消息重放；旧文字通常保留，旧截图只留窗口 |
| Flat progress | Qwen3VL, UI-Venus | 旧步骤拼成Task progress/Previous Actions，通常只有当前截图 |
| Rolling summary | Gelab | 一个模型生成的rolling summary + 当前截图 |
| Hybrid collapsed | GUI-OWL 1.5 | 旧步骤折成Previous actions，recent窗口保留raw；默认通常仅当前截图 |
| Structured folding | MemGUI | folded summaries + latest interaction + UI memory + 当前截图 |

共同结构性风险：

> 历史文字、结论或summary通常比产生它们的旧GUI证据存活更久；模型可能继续看到旧判断，却看不到对应截图。

已发现的实现层风险，后续离线audit应专门检查：

- Qwen3VL可能把动作前生成的action text放入“You have done”式progress，导致intent语义升级为完成事实。
- UI-Venus默认 `history_length=0` 配合 `history[-0:]`，实际可能保留全部历史；parse-failed step也可能继续出现。
- Gelab summary与action同次、执行前生成，不是经过post-state验证的总结。
- GUI-OWL collapsed history存在action/result潜在off-by-one错配与latest result遗漏。
- MemGUI有next-screen-conditioned folding，但由actor自身管理，不等于独立evidence verdict，且folding可能破坏性丢失信息。

## 6. Existing data and preliminary observation

旧的本地QR-MW Seed baseline曾讨论：

```text
117 task directories in the historical ledger
historical headline SR: approximately 41.03%, often summarized as 48 / 117
artifact-level accounting currently available: 46 confirmed success / 69 failure / 2 no_result
broad screening: 14 of 69 failed tasks had at least one propagation signal
strict low-state-confound lower bound: 5 of 116 nonempty trajectories
```

`48/117` 只能保留为当时讨论采用的历史 headline/账本口径，不能与当前 artifact-level confirmed-success count混写。`14/69` 只应记录为 preliminary/manual pilot observation，定义尚未经过exact prompt exposure核实；不能表述为“14个失败由previous-step错误造成”。

新 collector要重新回答：

1. 被怀疑的旧文本是否真的存在于 target model request？
2. 它是 false、false-success、stale、off-track还是不可核验？
3. 只是注入噪音，还是当前输出明确使用了它？
4. 是否伴随错误action、重复action或premature termination？

## 7. Evaluation concept agreed with owner

错误历史只要实际进入 prompt，就是 noise。使用强度需要分层：

```text
NO_OBSERVED_UPTAKE
  已注入，但当前输出无可观察使用证据；仍是weak noise

BEHAVIOR_CONSISTENT
  当前行为与错误历史一致，但可能有其他解释；possible mislead

EXPLICIT_USE
  当前输出明确引用或以前提方式采用错误历史；strong mislead/strong noise
```

再独立标 downstream effect：

```text
NO_VISIBLE_HARM
UNNECESSARY_ACTION
WRONG_ACTION
REPEATED_ACTION
PREMATURE_TERMINATION
RECOVERED
UNKNOWN_EFFECT
```

详细taxonomy和metric在 `OFFLINE_EVALUATION_DESIGN.md`。

## 8. Why MobileWorld first, not AndroidControl

当前优先 MobileWorld，因为它能观察真实agent自然生成并重新注入的history，可回答：

- wrong previous-step是否自然发生；
- 是否真实进入下一步prompt；
- 是否在真实trajectory中被复用；
- 是否集中出现在失败task。

AndroidControl主要是成功demonstration，缺少真实actor reasoning和原始prompt history。它可在后期构造synthetic misleading history做外部泛化/控制实验，但不能替代当前motivation study。

## 9. Current code state

截至此交接包生成：

```text
Mac环境无法运行MobileWorld benchmark。
未在官方MobileWorld clone中实现collector。
未修改MobileWorld源码。
未实现offline evaluator。
本交接包只包含design/instructions/test plan/status。
```

服务器端下一步：按 `SERVER_AGENT_INSTRUCTIONS.md` 和 `IMPLEMENTATION_GUIDE.md` 实现 collector。

## 10. Immediate roadmap

```text
1. Freeze source + deterministic fixtures
2. Implement schema/blob/recorder/serializer/integrity checker
3. Hook Base non-stream + streaming
4. Hook UIINS direct grounder
5. Hook runner transitions and score
6. Hook environment transport/MCP/user evidence
7. Validate feature-off parity, retries, images, concurrency
8. Server smoke on Seed, Planner, MemGUI
9. Nine-adapter small compatibility smoke
10. Freeze raw schema v1
11. Implement separate offline reconstruction/labeling pipeline
12. Run representative-agent motivation study
```

不要直接开始 `9 agents × all tasks`。九个agent先做2–3个短任务的logger compatibility smoke；研究采集应在schema和标注流程稳定后设计样本。

## 11. Server findings / deviations

服务器coding agent在这里追加，不覆盖旧记录：

```text
Date:
Commit:
Finding/deviation:
Evidence (file/symbol/test):
Decision:
Research-semantic impact:
Owner approval required: yes/no
```

## 12. Implementation checklist

- [ ] Server repo path identified
- [ ] Commit and dirty state recorded
- [ ] Nine-agent registry revalidated
- [ ] Feature-off baseline fixtures saved
- [ ] Raw schema v1 implemented
- [ ] Blob store and reconstruction test passed
- [ ] Base non-stream capture passed
- [ ] Streaming chunk capture passed
- [ ] Retry/error capture passed
- [ ] UIINS grounder capture passed
- [ ] Runner transition capture passed
- [ ] Last-step post-state test passed
- [ ] GUI/MCP/user execution evidence passed
- [ ] Task score/reason capture passed
- [ ] Concurrency/context isolation passed
- [ ] Credential scan passed
- [ ] Integrity checker passed
- [ ] Seed server smoke passed
- [ ] Planner+UIINS server smoke passed
- [ ] MemGUI server smoke passed
- [ ] Nine-agent compatibility smoke passed
- [ ] Raw schema v1 frozen
- [ ] Offline pipeline started only after collector DoD

## 13. Open questions that do not block Collector v1

- 正式motivation sample使用5–6个representative agents还是全9个的成本分配。
- 人工标注规模、双标比例和adjudicator人选。
- Primary success threshold采用官方 `score > 0.99` 还是旧分析的 `score > 0`；必须在metric manifest固定。
- 是否后续增加emulator checkpoint以支持真正counterfactual branch replay。
- 未来 Sentinel 的 rubric template 已约定在任务开始时由 task 生成并版本化；运行中更新 milestone state，不是每步静默重生成 rubric。允许何时显式发布 rubric 新版本仍留给后续实验；不属于本次 collector 实现。

这些问题不能被用来拖延label-free raw collection，因为raw schema的目的就是让后续定义可改变。
