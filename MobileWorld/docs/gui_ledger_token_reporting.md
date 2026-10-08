# GUI Ledger：离线统计 token 与成功率

这个工具读取已经保存的 Collector 原始记录，对比 `OFF / INFORM / FULL`。
它不运行任务、不调用模型、不启动 GPU 或模拟器，也不修改原始审计。
没有保存 Collector 的旧实验，不能只靠最终 `traj.json` 补出同等完整的统计。

## 1. 怎样使用

从仓库根目录运行下面的命令。`/absolute/...` 都是需要替换的路径，
`--run` 指向某个具体 Collector run 根目录，即里面有 `tasks/` 的目录，
不是 trajectory 目录，也不是含多个 runs 的上级目录。
`OFF / INFORM / FULL` 在这里是报告标签，不会改变旧 run 的模式；是否实际启用某个
处理，要看原运行的配置记录。输出目录应放在原始审计目录之外。

```bash
MobileWorld/.venv/bin/python -m mobile_world.offline.gui_ledger_usage \
  --run OFF=/absolute/audit/off/run-id \
  --run INFORM=/absolute/audit/inform/run-id \
  --run FULL=/absolute/audit/full/run-id \
  --expected-tasks /absolute/tasks/gui117.txt \
  --baseline OFF \
  --output-dir /absolute/reports/gui-ledger-usage-01
```

任务列表每行一个非空任务名，例如：

```text
SetAlarmTask
TakeSelfieTask
```

正式的 117 项对比应传入实际的完整 117 任务列表，而不是上面的两行示例。
省略 `--expected-tasks` 时，工具只能从读取到的任务名推断观察到的集合；
这不能证明原定的 117 项全部运行过。
`--baseline` 默认是 `OFF`；若使用其他基线标签，要显式指定。未提供基线时仍能看到
各组统计，但没有相对该基线的降幅。

同一组分片可以重复使用标签：

```bash
MobileWorld/.venv/bin/python -m mobile_world.offline.gui_ledger_usage \
  --run OFF=/absolute/audit/off/shard-a-run \
  --run OFF=/absolute/audit/off/shard-b-run \
  --run FULL=/absolute/audit/full/shard-a-run \
  --run FULL=/absolute/audit/full/shard-b-run \
  --expected-tasks /absolute/tasks/gui117.txt
```

这些分片应覆盖互不重复的任务。同一标签下，同一任务出现在不同 run 根目录时，
成本仍全部计入，但成功率中的最终结果会标为有歧义；工具不会随意挑一个好结果。
这也不是合并多次相同 117 项重复实验的接口。

省略 `--output-dir` 时，只向标准输出打印 Markdown。
指定时，输出目录必须尚不存在，工具不会覆盖已有报告；生成：

- `report.md`：给人看的分组对比和统计限制。
- `report.json`：机器可读的统计结果。
- `tasks.csv`：每个实验组、每个任务一行，汇总它的所有 attempts；不是每个 attempt 一行。
  可以查看用量、最终分数、attempt 数和缺失用量的调用数。

## 2. 到底统计哪些 tokens

主口径是 **actor 在全部任务、全部已观察到的实际尝试中，服务已返回的 usage**。
成功和失败任务都纳入，不只算最后一次成功，也不只算成功任务。

| 指标 | 含义 |
| --- | --- |
| 输入 tokens | 服务返回的 `prompt_tokens`，按已观察到的调用累加 |
| 输出 tokens | 服务返回的 `completion_tokens`，单独累加 |
| 缓存输入 tokens | 服务明确提供的缓存输入计数；它已经包含在输入 tokens 中，不再额外相加 |
| 用量覆盖 | 有多少调用拿到了对应字段，有多少缺失；缺失不冒充 0 |
| actor 调用数 | 应用层可观察到的 SDK 调用次数；重试也计入 |
| GUI 动作数 | `execution_kind=gui` 的动作执行尝试；不混入询问用户、回答或 MCP |
| 成功率 | 在规定任务集合上，按下面的最终任务结果规则判定 |
| 每成功任务的输入开销 | 所有任务的累计输入 tokens ÷ 最终成功任务数；失败消耗也在分子中 |

这里不把“输入＋输出”与“输入 tokens”混为一个指标，也不把缓存量从输入总量中
扣掉后再声称少用了 tokens。缺失字段只提供已知部分之和，同时报告缺失覆盖，
不能将这个部分和当成完整成本。单独缺少缓存统计，不会抹掉已经完整提供的输入统计。
JSON 和 CSV 另有 `action_attempts`（全部执行尝试）与 `non_gui_action_attempts`，
可用于区分 GUI 操作和其他执行；这些都是尝试次数，不代表动作成功。

实际 actor 请求里的系统提示、任务、历史、截图和本轮 Inform 都已交给同一次模型调用。
因此 Inform、已经进入历史的 Nudge 等文本，其开销已经包含在服务返回的输入用量里，
不能再按提示长度加一次。工具不根据字符数猜 token，也不虚构文本与图片的拆分。
多模态图片的计量方式依赖具体模型服务，报告保留“服务报告的用量”这一边界。

## 3. 重试、任务结果和缺失如何处理

**逐调用统计，不相加任务累计快照。** 用 run、task attempt、request 的身份区分
应用层可观察到的 SDK 调用。解析重试、请求重试和整任务重试已经消耗的 usage 都要算；
返回内容最终解析失败，不会让那次模型消耗消失。失败事件如果保留了 response usage，
也计入一次；没有 usage 则保留缺失记录。流式调用使用该次终结记录的 usage，
不把每个 chunk 的累计计数再相加。

`task_ended.token_usage` 和 `traj.json` 里的数是累计快照，不是逐调用增量。
它们可能在预测失败时尚未更新，也可能跨复用同一个 agent 的整任务重试继续累计，
因此这个工具不拿它们作为相加的数据源。

**成功率只选有明确唯一性的最终 attempt。** 同一 run、同一任务内，使用最高且唯一的
`whole_task_attempt_index` 对应结果；要求这些 attempt 索引本身没有缺失或重复，
按官方阈值 `score > 0.99` 判成功。
最新 attempt 缺少分数时，不回退到更早一次的好分数；同一任务跨多个 run 出现，
也不按时间或成功与否擅自挑选。所有实际尝试的成本仍保留。

**缺失和不可比需要显式报告。** 输入或输出用量不完整、Collector 采集不完整、任务集合
不一致、最终分数不完整或配置不可比时，不自动给出“节省了百分之多少”的结论。
如果没有传入预期任务列表，工具可以在各组一致的观察集合上给出描述性差值，但会标为
`cohort_source=observed_union`，不能据此声称完成了原定的 117 项评测。
actor 模型及其他运行条件应一致；
Ledger 模式、UI 树等实验处理差异要明确披露，不能偷偷当作完全同条件。
配置核对也只能覆盖审计中实际记录的内容，不代表后台环境或服务状态被证明完全一致。

自动对比要求每组只有一种明确的配置：`agent_type`、`model_name`、`suite_family`、
`max_round` 必须齐全；实际请求也应只有一种模型及采样参数组合，并与基线一致。
其他已记录的重试、工具、缩放、等待、超时等配置也参与对比。
Ledger/UI 树处理参数单独披露，不作为必须与 OFF 相同的条件。

`report.json` 中，`known_input_tokens` 是已知部分之和；完整的 `input_tokens`
在缺少输入用量时为 `null`。`issues` 描述统计问题，`comparison_issues` 描述不能给出
相对降幅的原因。可对比时，`input_reduction_vs_baseline = 1 - 本组输入 / 基线输入`；
正数表示减少，负数表示增加。没有成功任务或统计不完整时，不生成“每成功任务”的有限比值。

## 4. 怎样模仿论文的 token 对比

Ledger 原论文的 Table 4（PDF 第 7 页）在完整的 500 个任务上统计输入用量，
成功和失败都包含在内。我们可以采用同样的核心口径：固定任务集合，统计所有尝试的
输入 tokens，并同时给出成功率。[Ledger 原论文](https://arxiv.org/pdf/2608.00808#page=7)

我们的集合是 117 个 GUI 任务，不能拿绝对数直接与论文的 500 个 coding 任务比较。
最重要的是在同一 117 项内部，对照 OFF 与 INFORM/FULL：

1. 输入 tokens 有没有减少？
2. 成功率有没有同时保持或提高？
3. 减少是不是只是因为更早报错或提前失败？
4. 重试次数、缺失 usage、配置差异是否足以影响对比？

只比较两组各自成功任务的平均 tokens 会换掉样本集合，容易形成选择偏差。
即使统计完整，单次实验的差值仍是描述性结果，不自动证明 Ledger 导致了节省。
尤其 UI 树额外提供了观察信息时，不能把信息来源差异全部归因于跟踪或提醒逻辑。

## 5. 不包括什么

- SDK 内部透明重试目前不可见；这里不是服务端所有隐藏尝试的账单。
- 现有模拟用户直接调用其自己的模型，usage 不在这条 actor Collector 采集链中。
  报告不把它伪装成已统计的全系统成本，也不把模拟用户调用数当作 actor tokens。
- 不计算美元费用，不推算 GPU 成本，不补造图片 token 或缺失用量。
- 采集完整性检查依据 Collector 完成标记和任务流清单；这不是重新执行整个加密哈希完整性审计。
- 不增加任何运行时 hook 或模型调用；分析旧数据不等于重新执行评测。

实现入口为 `mobile_world.offline.gui_ledger_usage`。统计依据是实际调用的原始审计，
不是 State Tracker 的状态条数，也不是 Inform 文本字符数。
