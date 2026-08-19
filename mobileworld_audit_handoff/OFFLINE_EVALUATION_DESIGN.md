# Offline History Audit：版本化离线评测设计

## 0. 目的与边界

本 pipeline 消费 Runtime Audit Collector 生成的 immutable raw events，回答：

> 错误、失效或偏航的 previous-step 信息是否真实进入后续模型输入；模型是否表现出对它的使用；是否伴随可观察的有害后果？

评测与采集必须分开。任何 taxonomy、阈值、judge prompt 或人工标签变化都只能产生新的 derived version，不能修改 raw 目录，也不要求重新跑 MobileWorld。

当前是 motivation study，不是 Sentinel 在线系统：

- 标签全部离线产生；
- 不回写 agent prompt；
- 不宣称观察性关联等于因果；
- rubric 可以作为后续离线辅助字段，但第一版主要关注历史事实有效性与复用。

---

## 1. 输入、输出与目录隔离

### 1.1 输入

只读输入：

```text
<raw-root>/runs/<run_id>/manifest.start.json
<raw-root>/runs/<run_id>/manifest.final.json
<raw-root>/runs/<run_id>/run.events.jsonl
<raw-root>/runs/<run_id>/tasks/*/events.jsonl
<raw-root>/runs/<run_id>/blobs/sha256/**
<raw-root>/runs/<run_id>/integrity_report.json
```

运行任何评测前必须先通过 raw integrity validation。

### 1.2 推荐离线包

离线代码不要放进 MobileWorld 的 `src/mobile_world/runtime/audit/`。建议在仓库根目录创建独立工具包：

```text
history_audit/
├── README.md
├── pyproject.toml                 # 若需要独立环境
├── src/history_audit/
│   ├── io.py
│   ├── normalize.py
│   ├── reconstruct.py
│   ├── representations/
│   │   ├── raw_replay.py
│   │   ├── flat_progress.py
│   │   ├── rolling_summary.py
│   │   ├── hybrid_folding.py
│   │   └── memgui.py
│   ├── claims.py
│   ├── schemas.py
│   ├── label.py
│   ├── adjudicate.py
│   ├── metrics.py
│   └── cli.py
└── tests/
```

离线 pipeline 可以位于 AgentSentinel monorepo 内的独立目录；关键要求是运行时 collector 不 import 它，raw collection 与 derived evaluation 仍保持单向依赖。

### 1.3 Derived 输出

```text
derived/
└── <evaluation_name>/<evaluation_version>/<evaluation_run_id>/
    ├── manifest.json
    ├── normalized_decisions.parquet
    ├── history_exposures.parquet
    ├── claims.parquet
    ├── auto_labels.jsonl
    ├── human_labels.jsonl
    ├── adjudicated_labels.jsonl
    ├── metrics.json
    └── reports/
```

`evaluation_run_id` 不是简单的 `v1` 文件覆盖。manifest至少记录：

```text
raw run IDs + hashes
normalizer version
schema version
claim extractor version
verifier/judge model and exact prompt hash
human annotation guideline version
metric code commit
created_at
```

---

## 2. Pipeline 阶段

```text
Raw integrity check
  -> event normalization
  -> decision/transition reconstruction
  -> actual prompt history exposure extraction
  -> claim segmentation and provenance
  -> validity labeling
  -> uptake labeling
  -> downstream-effect labeling
  -> human adjudication
  -> metrics/report
```

每一层只读取前一层并写新 artifact。中间表必须可以删除并从 raw 重建。

### 2.1 Normalization

把 provider-specific raw event 标准化为：

```text
DecisionRecord
  run_id
  task_id
  task_run_id
  step_id
  task_instruction T
  pre_observation S_t
  actor_request I_t
  agent_prediction P_t
  provider_calls[]
  parsed_action A_t
  execution R_t
  post_observation S_{t+1}
  grounder_calls[]
  task_score/reason
  completeness flags
```

失败调用和 retry 不删除。需另外标识 `selected_actor_call`，即 adapter最终用于产生 A_t 的 response；不能假定同 step最后一个 provider attempt一定被使用。

### 2.2 Exposure reconstruction

评测单位不是“agent内部理论上保存的 history”，而是 target actor request `I_t` 中实际出现的内容。

为每个 target step输出：

```text
HistoryExposure
  exposure_id
  target_decision_id
  target_step t
  request_message_index
  content_block_index
  text_span/image_ref
  representation_type
  candidate_source_step(s)
  provenance_confidence
  was_actually_in_request = true
```

必须保留 message role、顺序和图文相对位置。不能把完整 messages先 flatten成一个字符串再分析。

### 2.3 Claim segmentation

对 history text 切分成最小可核验 claim，例如：

```text
"I opened the item page and added it to the cart"
```

至少切为：

```text
claim 1: 已打开目标 item page
claim 2: 已把 item 加入购物车
```

动作意图、观察、事实断言、计划和成功宣称必须区分：

```text
OBSERVATION_CLAIM
ACTION_INTENT
ACTION_EXECUTION_CLAIM
SUCCESS_CLAIM
PLAN
SUMMARY_CLAIM
```

不要把“我要点击 Add to cart”自动标成“已经加入购物车”。这正是 Qwen/summary 类表示可能产生语义升级的地方。

---

## 3. 九个 agent 的 representation normalizer

基于官方 `0dcd098`，九个 adapter可按以下方式处理。

### 3.1 Raw replay family

```text
seed_agent
general_e2e
mai_ui_agent
planner_executor（只分析 actor history；grounder另存）
```

特点：旧模型 response作为 assistant messages重放；文字通常比对应旧截图保存更久。

Normalizer应：

- 通过 earlier raw actor responses做 exact/fuzzy source matching；
- 区分完整 response、被截断 response 和占位 screenshot；
- 记录 source screenshot是否仍在 target request；
- 不假定 `history_n=3` 意味着“三个旧步骤”：通常包含当前截图。

### 3.2 Flat progress family

```text
qwen3vl
ui_venus_agent
```

特点：多个旧步骤被拼成 `Task progress` 或 `Previous Actions`。

Normalizer应：

- 按显式 `Step N` 边界解析；
- 与 earlier responses/conclusions对齐；
- 对无法唯一映射的 span使用多个 `candidate_source_steps`；
- 不虚构精确 provenance。

### 3.3 Rolling summary

```text
gelab_agent
```

特点：下一步主要看到一个模型生成的 rolling summary，而不是原始对话。

重要事实：summary 与 action在同一次执行前模型调用中生成，因此不能默认视为经过 `S_{t+1}` 验证的事实。

Normalizer应：

- 对 summary做 claim级分析；
- 将 provenance标为 `model_generated_summary`；
- 将 earlier raw responses作为候选来源，而非确定逐字来源；
- 检测 summary新增、删除或语义升级的内容。

### 3.4 Hybrid collapsed history

```text
gui_owl_1_5
```

特点：较旧步骤折为文字，最近窗口可能保留 raw messages；默认 `history_n=1` 时通常只有当前截图。

Normalizer必须专门检查 action/result alignment。源码分析发现 collapsed history存在潜在 off-by-one：action `i` 的 result在 observation `i+1` 到来，却可能与错误 step配对，且最新结果可能遗漏。离线标签应基于 raw transition重新校准，而不是相信格式化后的 step编号。

### 3.5 Structured folding

```text
memgui
```

特点：folded summaries、latest interaction、UI memory共同进入 prompt。

Normalizer应：

- 分别保存三个 section；
- 比较 fold前 earlier responses与 fold后 claims；
- 标记 destructive folding造成的信息丢失或合并；
- 不把 actor自我反思等同于独立验证器 verdict。

---

## 4. 三个正交标签轴

错误历史“进入 prompt”本身已经是噪音；模型明确使用它是更强的传播证据。为避免混淆，至少使用三个独立轴，不要只产出一个笼统 `misleading=true`。

### 4.1 Axis A：History validity

建议枚举：

```text
SUPPORTED
REFUTED
STALE
OFFTRACK_TRUE
UNVERIFIABLE
NOT_A_FACTUAL_CLAIM
```

并保存细分类：

```text
FALSE_CLAIM
FALSE_SUCCESS
GUI_MISINTERPRETATION
STALE_STATE
TRUE_BUT_OFFTRACK
SUMMARY_CORRUPTION
RESULT_MISALIGNMENT
```

定义：

- `SUPPORTED`：可见 evidence支持该 claim。
- `REFUTED`：可见 evidence与该 claim明确冲突。
- `STALE`：产生时可能正确，但在 target step已失效。
- `OFFTRACK_TRUE`：内容本身为真，但对任务目标/当前路径偏航。
- `UNVERIFIABLE`：现有截图、tool result和任务证据不足。
- `NOT_A_FACTUAL_CLAIM`：纯计划/意图，不能按真伪打标签。

Primary conservative metric只把 `REFUTED` 和规则明确的 `STALE` 计为 invalid；其他单独报告。

`ACTION_FAILURE_ONLY` 作为独立 control tag，而不是 invalid-history 类别：若 `A_i` 没有产生预期可见效果，但 `P_i` 只表达“我要点击/这应该打开页面”等 intent/prediction，没有虚假完成态 claim，而且后一步正确识别失败，则不能把 `P_i` 标成 misleading。相邻截图完全相同也最多是 `NO_VISIBLE_CHANGE` evidence，不能自动推出 action failed 或 history false。

### 4.2 Axis B：Observed uptake

建议枚举：

```text
NO_OBSERVED_UPTAKE
BEHAVIOR_CONSISTENT
EXPLICIT_USE
EXPLICIT_REJECTION
UNKNOWN
```

定义：

- `NO_OBSERVED_UPTAKE`：错误历史在 prompt中，但 P_t/A_t没有可观察使用证据。它仍是 noise。
- `BEHAVIOR_CONSISTENT`：当前行为与错误历史一致，但也可能由当前 GUI或合理检查解释，不能称为强传播。
- `EXPLICIT_USE`：P_t明确引用、复述、承接，或把错误历史作为行动前提。这是强噪音/强传播的核心定义。
- `EXPLICIT_REJECTION`：模型看到错误历史但主动否定、重新验证或纠正。
- `UNKNOWN`：模型输出不足以判断。

另外保存独立的 `state_confound`：

```text
NONE
CURRENT_GUI_REINFORCES_SAME_PREMISE
CURRENT_GUI_CONTRADICTS_PREMISE
UNKNOWN
```

即使 `P_t` 明确复述旧 history，如果 `S_t` 已经显示同一个错误草稿/错误日期，仍只能确认 observed propagation，不能把下一动作唯一归因于 history text。严格的 low-confound observational table 只纳入 `NONE` 或更强的 `CURRENT_GUI_CONTRADICTS_PREMISE`；其余单列。这正是旧 Seed broad `14/69` 与 strict `5/116` 口径不同的核心原因。

### 4.3 Axis C：Downstream effect

建议枚举：

```text
NO_VISIBLE_HARM
UNNECESSARY_ACTION
WRONG_ACTION
REPEATED_ACTION
PREMATURE_TERMINATION
RECOVERED
UNKNOWN_EFFECT
```

Effect标签需要基于 A_t、S_{t+1} 和任务目标证据，不能只看模型语言。

### 4.4 派生的报告层级

以下层级由三个轴派生，不作为人工唯一标签：

```text
WEAK_NOISE
  invalid history actually injected
  + NO_OBSERVED_UPTAKE（或 uptake unknown）

POSSIBLE_MISLEAD
  invalid history injected
  + BEHAVIOR_CONSISTENT

STRONG_MISLEAD
  invalid history injected
  + EXPLICIT_USE

EXPLICIT_HARM
  invalid history injected
  + EXPLICIT_USE
  + harmful downstream effect
```

这与项目讨论保持一致：旧错误历史只要进入输入就是噪音；明确使用才是强噪音。`EXPLICIT_HARM` 再额外要求可观察伤害。

---

## 5. Evidence 与时间合法性

对 source step `i` 的 claim，应分别检查：

```text
S_i       claim生成前界面
P_i       claim/intent原文
A_i       实际解析动作
R_i       transport/tool/user evidence
S_{i+1}   action后界面
S_t       target request当前界面
I_t       claim是否真实注入
P_t/A_t   当前模型是否使用
```

标签必须区分：

- “从产生时就是错的”；
- “产生时正确，后来变 stale”；
- “动作没有可见成功证据”；
- “有明确反证”。

缺少 evidence时使用 `UNVERIFIABLE`，不要强迫二分类。

“当前截图看不到旧事件”也不构成反证：`曾搜索过商品` 可以为真但已离屏；`购物车当前含商品` 才是可随状态改变的 persistent-state claim。标注必须保存 claim 的时间锚点与类型，不能把事件事实、当前状态、动作意图和后验自评分到同一个真假规则里。

当前 motivation study允许使用 `S_{i+1}` 判断 `P_i` 的 execution/success claim，因为这是离线评测；未来在线 Sentinel只能在 `S_{i+1}` 出现后、生成 A_{i+1} 前使用它。不得使用 `S_{t+1}` 去假装预测 t 时已有的 evidence。

---

## 6. 人工与自动标注流程

### 6.1 自动预标注

自动 verifier只能生成：

```text
proposed_label
confidence
evidence_refs
short_rationale
judge_prompt_version
```

不能覆盖人工标签。所有 judge request/response也应保存在 derived artifact中，以便更换 judge后重跑。

### 6.2 人工标注界面最小内容

每个 exposure展示：

1. task instruction；
2. source claim/span及其 message role；
3. `S_i` 与 `S_{i+1}`；
4. action/tool/user result；
5. target完整 request中的上下文位置；
6. `S_t`；
7. `P_t` 与 `A_t`；
8. 不显示 evaluator建议作为默认选项，避免 anchoring；
9. 所有图片均可回到 blob hash。

### 6.3 可靠性

正式报告前：

- 至少对研究样本做双人独立标注；
- 分别计算 validity、uptake、effect 的一致性；
- 冲突经 adjudication；
- 保存原始两个标签和最终标签；
- guideline改版产生新 annotation version，不覆盖旧版。

---

## 7. 指标与分母

不要只报告“14 cases”。至少同时报告 exposure-level 与 task-level 数字，并明确分母。

### 7.1 Exposure-level

```text
Invalid exposure rate
= invalid history exposures / verifiable history exposures

Explicit propagation rate
= invalid exposures with EXPLICIT_USE / invalid exposures

Possible uptake rate
= invalid exposures with BEHAVIOR_CONSISTENT / invalid exposures

Explicit harmful propagation rate
= invalid exposures with EXPLICIT_USE and harmful effect / invalid exposures

Persistence length
= target_step - source_step for each invalid exposure
```

同一个 source claim重复出现在多个后续 prompt时，每次是独立 exposure；同时另报 unique invalid records，防止长轨迹过度加权。

### 7.2 Task-level

```text
Task noise prevalence
= tasks with >=1 invalid exposure / audited tasks

Failed-task strong propagation prevalence
= failed tasks with >=1 invalid EXPLICIT_USE / failed audited tasks

Failed-task explicit-harm prevalence
= failed tasks with >=1 explicit harmful propagation / failed audited tasks
```

任务是否成功需明确定义阈值。官方 eval summary使用 `score > 0.99` 计成功；若研究选择 `score > 0`，必须在 metric manifest中写清，不可混用。

### 7.3 分层报告

至少按以下维度分层：

```text
agent_type
history representation family
task family/app
task success/failure
source claim type
source-target distance
whether source screenshot is still present in I_t
whether history is raw/flat/summary/folded
```

不同 agent通常绑定不同模型与 prompt，因此 agent间发生率属于观察性比较，不能直接归因于 history format。

### 7.4 当前 preliminary 数字

旧 Seed baseline讨论中暂记：

```text
117 formal task directories
116 nonempty trajectories
115 numeric results: 46 score=1 / 69 score=0
2 no_result
historical headline only: 48/117 = 46 score=1 + 2 no_result
broad pilot count: 14 / 69 failed tasks
strict low-state-confound lower bound: 5 / 116 nonempty trajectories
```

`48/117` 不能写成 48 个 confirmed success。`14/69` 是由 5 strict、8 state-confounded、1 provenance-only task 组成的 broad motivation pilot count；`5/116` 才是当前最保守的 strict observational lower bound。它们都不是因果结论。新 raw collector必须重新核实：

- 对应错误文本是否实际进入 target prompt；
- history本身是否可证伪；
- 是 weak noise、possible uptake还是 explicit use；
- 是否有明确 downstream harm。

在完成前不能把 `14/69` 写成“由错误历史导致失败”。

---

### 7.5 自然发生率的抽样原则

自然发生率不能只人工审核 scanner 命中的候选；那样最多报告 scanner precision 或 observed lower bound。主抽样单位建议是 **target decision turn `t`**：对抽中的 `I_t` 审查其中所有可追踪的 `i<t` history exposures，再从 exposure 层汇总。只抽 immediate pair `(t-1,t)` 会漏掉长期保留的旧 pre-steps。

Collector 稳定后的第一轮正式标注可先以约 500–600 个 target turns、覆盖约 70–90 个 task-run clusters 作为可行性预算起点，并至少 20% 双人独立标注；这不是预注册样本量，需根据新数据的 prevalence、cluster correlation 和成本重新计算。抽样至少按以下维度分层：

```text
agent / history representation family
task score outcome（success/failure/no_result分开）
trajectory length
source-target distance / source image是否仍在request
scanner hit 与 scanner non-hit
task/app family
```

统计时以 `task_run_id` 为 cluster 做 bootstrap 或其他 cluster-aware interval，同时报告 task-weighted 和 exposure/step-weighted 结果，避免 50-step 失败轨迹支配结论。自动 scanner 只负责提高召回和分层抽样效率，不能自动把重复动作、静止截图、长轨迹或最终失败升级为 misleading label。

旧 Seed 数据上曾人工复核 50 个 immediate pairs；它只证明标注流程可行，不是总体发生率估计。新 raw 数据应同时保留 hit 与 non-hit 的随机样本，才允许做加权 prevalence。

---

## 8. 推荐 CLI 契约

实现后建议支持：

```bash
# 1. 校验 raw
uv run python -m history_audit.cli validate-raw \
  --run /path/to/audit_run

# 2. 标准化与重建 exposure
uv run python -m history_audit.cli reconstruct \
  --run /path/to/audit_run \
  --output /path/to/derived/<evaluation_run_id>

# 3. 自动预标注（可选）
uv run python -m history_audit.cli auto-label \
  --derived /path/to/derived/<evaluation_run_id> \
  --schema history-audit/1.0.0 \
  --judge-config judge_config.yaml

# 4. 导出人工标注包
uv run python -m history_audit.cli export-annotation \
  --derived /path/to/derived/<evaluation_run_id>

# 5. 合并/裁决并计算指标
uv run python -m history_audit.cli metrics \
  --derived /path/to/derived/<evaluation_run_id> \
  --labels adjudicated_labels.jsonl
```

命令和模块名可按实现调整，但阶段边界与不可变性不可改变。

---

## 9. 离线 pipeline Definition of Done

1. 对同一 raw run，固定版本和配置可生成 byte-stable或语义稳定的 derived结果。
2. 删除 derived后可以仅从 raw完整重建。
3. 修改 taxonomy/judge prompt只生成新 evaluation run，不触碰 raw。
4. 九种 adapter表示都能输出标准 `HistoryExposure`；不确定 provenance被显式标记。
5. validity、uptake、effect 是独立字段。
6. “错误注入即噪音，explicit use才是强噪音”可由字段无歧义派生。
7. 所有指标同时保存 numerator、denominator和过滤条件。
8. 每个标签可回链到 exact request span、raw response、action和 screenshot blobs。
9. 不用未来状态冒充 agent决策时可用信息。
10. 报告明确区分自然轨迹证据、相关性和未来反事实 replay。
