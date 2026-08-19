# Sentinel Proposal 中文演讲稿（15–20 分钟）

> 建议语速：正常、略慢，约 17–19 分钟。  
> 方括号中的内容是演讲提示，不需要念出来。英文缩写第一次出现时解释，后面直接使用即可。

---

## 0. 开场：我想解决什么问题（约 1 分钟）

大家好，今天我想介绍的 proposal 叫做 **Sentinel**。

先看一个很小的例子。用户要求 GUI agent 在 Amazon 买一个不超过 25 美元、评分至少 4.5 的 USB-C charger。Agent 实际误点进了一款数据线，但历史里却写着：“已经打开符合要求的 charger。”下一步模型如果继续相信这句话，就可能直接把错误商品加入购物车。

这里同时出现了两个问题：历史内容写错了，而且当前轨迹也走偏了。我们想问的是，在模型再次消费这些历史之前，能不能先检查哪些依据已经不可靠，哪些历史虽然真实、却属于一条已经失活的路径？

我们的范围很窄：不研究跨任务检索的 experience、外部知识库或者长期记忆，只研究**当前 GUI 任务、当前这一条运行轨迹中，被反复放回 prompt 的 previous steps，也就是 pre-steps**。

Sentinel 就是一个插在下一次模型调用之前的 runtime middleware。它不替 agent 点击，而是先决定：**哪些 pre-steps 还值得进入下一轮 prompt。**

---

## 1. 为什么当前 GUI 还不够（约 1.5 分钟）

大家可能会问：GUI agent 每一步不是都能看到当前截图吗？既然有当前截图，为什么还会被历史误导？

原因是模型并不会只根据截图从头推理。它通常会把截图和历史结合起来。例如，历史里写着“评分筛选已经成功”“目标商品已经加入购物车”，模型就很可能把这些当成已经完成的进展，只在当前页面上寻找下一件事。即使当前截图没有明确支持这些说法，模型也可能相信自己的历史，因为截图可能有遮挡、信息不完整，或者模型倾向于保持前后叙事一致。

这里至少有两种不同的问题。

第一种是**历史内容本身错了**。比如 agent 点击了筛选按钮，但点击没有生效；历史却写成“筛选成功”。或者它点击了 Add to Cart，但购物车仍然是空的；历史却写成“商品已经加入购物车”。

第二种是**历史是真的，但方向错了**。例如任务是买充电器，agent 却连续浏览了一个用户资料页或者笔记本支架页面。历史可以非常准确地记录这些动作，但这些真实记录会继续占据上下文，让模型围绕错误分支进行自洽推理。

这两类问题不能混成一个“历史好不好”的分数。前者需要事实过滤，后者需要路径相关性判断。这也是 Sentinel 采用双轴设计的原因。

---

## 2. 现有 GUI agents 怎么处理 pre-steps（约 1.5 分钟）

我们先调查了现有 GUI agents 如何保存和使用 pre-steps。当前审计表中有 26 个合并条目，并另外检查了 MobileWorld 固定版本中的 9 个注册 adapters。这里的数字不是 35 个独立系统，因为不同表之间有家族重叠。

从表示方式看，现有方法大致可以归为几类：有的直接回放旧的 Thought 和 Action；有的保留逐步自然语言总结；有的维护滚动摘要；还有的会把较早历史折叠成结构化记忆。图片窗口也不一样，有的只保留当前图，有的保留最近几张图，而文字历史通常更长。

现有系统也不是完全不检查过去。比如 Browser-Use 会根据当前页面评价上一步目标；Mobile-Agent 和 M3A 一类方法会比较动作前后的观察；Agent-S 会反思近期轨迹是否循环或者偏航。

所以我们的 claim 不是“以前没有人回看历史”。更准确的研究空白是：在我们冻结审计的这些系统中，还没有看到一个统一机制，能够在运行时把宿主原生 pre-steps 拆成可追踪的 claims，持续判断哪些内容已经失效或被反驳，同时用独立的任务 rubric 判断哪些真实历史属于偏航分支，并在下一次 model call 之前生成一份 clean active history。

---

## 3. Sentinel 的核心设计（约 1 分钟）

[如果有结构图，这里指向 middleware 所在位置]

Sentinel 位于宿主 agent 组装好 prompt 之后、真正调用模型之前。

宿主原本准备发送的是：system prompt、工具定义、用户任务、原始 pre-steps 和当前 GUI。Sentinel 拦截这次 request，但不会删除审计记录。它会把完整原始轨迹保存到一个 sidecar evidence store，然后为这一次调用生成一个派生的 `active history`。

最终模型收到的内容可以概括为：

> 宿主原有的 system 和 tools，加上 task、clean active history、简短的 rubric state，以及 current GUI。

这里最关键的一点是：**Sentinel 的主要产出不是下一步动作，而是一个清理后的模型输入。** 原 agent 仍然负责看当前 GUI、选择动作和调用工具。Sentinel 只是决定哪些历史依据可以继续影响它。

---

## 4. Rubric 是什么时候生成、什么时候更新（约 2 分钟）

Sentinel 的 rubric 分成两部分：**Rubric Definition** 和 **Rubric State**。

任务刚开始时，Sentinel 根据用户 instruction 生成 Rubric v0。我们统一使用四类符号：

`H` 表示最终必须满足的 hard requirement；`S` 表示运行中可观察的 subgoal 或 checkpoint；`P` 表示可替代的完成 path；`C` 表示全程不能违反的 constraint。

例如任务是：在 Amazon 把一个价格不超过 25 美元、评分至少 4.5 的 USB-C wall charger 加入购物车，Prime 优先，不要结账。

那么 `H1` 是购物车最终含且仅含一件合格商品，`H2` 是不能进入 checkout。为了在加购前判断候选，Sentinel 会维护几个 `S`：`S1` 商品类型正确，`S2` 价格不超过 25 美元，`S3` 评分至少 4.5，`S4` 是在合格候选中优先 Prime。

路径可以有多条：`P1` 是搜索，`P2` 是分类浏览，`P3` 是从首页推荐进入。搜索不是硬要求，因为用户并没有规定一定要使用搜索框。

任务运行中，Sentinel 不会每一步重新发明一套 rubric。它主要更新的是 Rubric State，例如哪些要求是 pending、satisfied、violated 或 unknown，目前还有哪些 path 可行，以及当前 frontier，也就是下一个尚未完成的任务检查点是什么。

只有当运行中发现最初没有列出的合法路线时，Sentinel 才能增加一个 provisional，也就是临时的 soft path，并把 rubric 从 v0 更新到 v1。来自用户指令的 hard requirements 不能由 Sentinel 自己改写。

所以一句话总结是：**任务开始时生成 rubric definition，任务进行中更新 rubric state。**

---

## 5. Sentinel 每一步到底输入什么、输出什么（约 2 分钟）

每一步，Sentinel 接收四组主要信息。

第一组是用户任务和当前 rubric。第二组是宿主原本即将注入的 raw pre-steps。第三组是当前 GUI 和最近 transition，包括动作前后截图、UI tree、executor result 或 tool error。第四组是 sidecar 中积累的历史证据和以前的判定。

内部处理分三步。

第一步，Sentinel 暂时不看自然语言历史，只用当前 GUI、最近状态变化和 rubric 更新任务状态。这样可以减少错误历史反过来污染 rubric 判断。

第二步，它把每条 pre-step 拆成更小的 claims。例如“已经打开 VoltEdge，价格 18.99，评分 4.7，并且满足要求”，实际上包含商品身份、价格、评分和任务进展等多个 claim。Sentinel 用截图、UI tree 和执行结果分别验证它们。

第三步，history gate 为每条记录选择五种操作之一。

`KEEP` 表示内容可信，而且仍与 active path 相关，继续进入下一轮。

`DROP` 表示内容已被明确反驳，并且可以安全删除。

`REPLACE` 表示一条记录真假混合，或者完全删除会让 transition 断裂，因此只保留经过验证的最小事实。比如把“筛选成功”替换成“尝试点击筛选器，但结果状态没有变化”。

`ARCHIVE` 表示这是真实事件，但属于已经失活的偏航分支。原记录继续保存在 sidecar 中，却不再进入 active prompt。

最后是 `KEEP_UNCERTAIN`。如果证据不足，Sentinel 默认保守保留，并明确标成未验证，不能把“当前看不见”直接当成“过去没有发生”。

最终输出有两层。内部结构化输出包含 claim verdict、rubric state、gate decision、证据引用和日志；真正给原 agent 的，是过滤后的 active history，加上一段很短的 rubric state。Sentinel 不输出具体坐标，也不直接生成 GUI tool action。

---

## 6. 四种情况分别怎么处理（约 1.5 分钟）

[如果有 2×2 表，这里从左上开始讲]

双轴可以形成四种情况。

第一种，历史可靠，轨迹也 on-track。这时 Sentinel 只做 `KEEP`，不会为了显示自己存在而干预。

第二种，历史有错误，但轨迹仍然 on-track。比如评分筛选没有成功，但 agent 仍在正确的充电器结果页，而且合格商品仍然可见。这时只 `DROP` 或 `REPLACE` 错误记录，不要求整条任务回退。

第三种，历史是真的，但轨迹已经 off-track。比如历史准确写着“打开了笔记本支架页面”，但任务是购买充电器。Sentinel 不会把真事件改成假事件，而是把它 `ARCHIVE`。下一轮模型看不到这段偏航历史，只看到当前 GUI、仍未完成的要求和此前仍然相关的历史。

第四种，历史错误和轨迹偏航同时发生。比如历史声称打开了一个合格充电器，但当前 GUI 实际是价格超标、评分不足的数据线页面。这时既要 `DROP/REPLACE` 错误 claims，又要 `ARCHIVE` 失活分支。

如果某一轴无法判断，就只在那一轴弃权。历史真假不确定时，不做事实删除；路径不确定时，不做 archive。两轴不是必须一起行动。

---

## 7. 一个完整的运行例子（约 2.5 分钟）

下面我用刚才的 Amazon 任务，把 Sentinel 的运行过程串起来。

Step 0，agent 在 Amazon 首页，购物车为空。此时没有历史可以过滤。Sentinel 生成 rubric，并告诉模型：购物车要求还没有完成，目前可以通过搜索、分类浏览或者其他合法入口寻找候选。Agent 决定使用搜索。

Step 1，搜索结果页出现一个 VoltEdge charger，价格 18.99 美元，评分 4.7，而且是 Prime。历史写着“已经到达相关结果页”，这和 GUI transition 一致，所以 `KEEP`。

Step 2，agent 尝试点击评分筛选器。宿主历史却写成“评分筛选已经成功，所有结果都满足条件”。但 UI tree 显示筛选框仍然没有选中，页面状态也没有变化。Sentinel 因此把这条记录 `REPLACE` 成：“尝试点击评分筛选器，但结果没有变化。”

注意，这时任务并没有偏航。筛选只是一个可选步骤，而且合格的 VoltEdge 仍然可见。所以 Sentinel 不会命令 agent 重新搜索，也不会把整个路径判错。模型只是不再看到“筛选已经成功”这个错误前提。

Step 3，agent 点击坐标时误入了旁边的 sponsored laptop stand 页面。历史这次准确写着“打开了一个笔记本支架，这是一次误选”。这条历史是真的，但当前商品不能满足 `S1`，也就是商品类型检查。因此 Sentinel 把这条记录 `ARCHIVE`，不让它继续成为 active progress。原 agent 会根据当前支架页面和未完成的充电器要求，自行决定返回。

Step 4，agent 回到充电器结果页，这个真实且相关的 transition 被 `KEEP`。

Step 5，agent 又点错了商品。历史声称已经打开 VoltEdge，价格 18.99、评分 4.7、满足全部条件；但当前页面实际是 CablePro 数据线，价格 29.99、评分 4.3。这里历史错误和方向错误同时存在。Sentinel 会删除或替换关于商品身份、价格和评分的错误 claims，并把这个失活分支移出 active history。

最终给 agent 的输入里，不再有“我已经找到合格商品”这个错误依据，也不再有笔记本支架和数据线分支的历史噪声。模型只看到仍可信的搜索历史、H1 尚未完成、候选检查仍待满足，以及当前 GUI。之后 agent 可以重新打开正确商品、加入一件到购物车，并在购物车确认后停止，不进入 checkout。

这个例子说明，Sentinel 的 guide 不是一句“请点击返回”。它通过**改变模型当前能看到的历史依据**来引导决策，而具体动作仍由原 agent 完成。

---

## 8. 工程上如何作为插件实现（约 1.5 分钟）

Sentinel 包含五个主要部分。

第一是 sidecar evidence store，保存原始 pre/post screenshots、UI tree、动作、工具结果和原始 messages。第二是 model-call interceptor，在真正请求模型前取得宿主准备好的 prompt。第三是 host extractor，把不同格式的历史解析成 records 和 claims。第四是 rubric tracker 和 history verifier。第五是 renderer，根据 `KEEP/DROP/REPLACE/ARCHIVE` 生成协议合法的新 messages。

这里有一个现实限制：不同 GUI agents 的历史结构并不统一。MobileWorld 虽然统一了 runner 生命周期，但没有统一的 get-history 或 set-history 接口。有些系统还有 tool-call 和 tool-result 必须相邻、服务端管理历史、prompt cache 等约束。

所以我们不会声称“一次实现，九个宿主零适配”。统一的是 Sentinel 的审计协议和输出语义；每个历史表示族仍需要 extractor 和 renderer。

如果某个宿主不能安全删除消息，我们有两种选择：要么保留协议外壳，用经过验证的最小内容做 `REPLACE`；要么把它标成 annotation-only fallback。论文主实验里的 filtering 条件必须证明错误语义真的没有进入实际 model request，不能只在 prompt 末尾加一句“请忽略上文”，然后把它称为过滤。

---

## 9. 我们准备怎样评测（约 2.5 分钟）

评测需要回答两个问题：第一，错误或者偏航的 pre-steps 是否真的会改变下一步决策；第二，Sentinel 的过滤是否能改善行为，而且不会因为误删好历史伤害干净任务。

为了做因果比较，我们会尽量从同一个 GUI checkpoint 分叉。也就是说，当前页面和环境状态相同，只改变模型看到的历史。

历史条件包括一个干净对照和三类扰动：`Hist0` 是 clean history；`Hist1` 是真实但无关的 detour history；`Hist2` 是错误或过时的 progress；`Hist3` 是前两种混合。

Monitor 方面，最核心的是一个二乘二设计。第一维是要不要过滤被证据反驳的历史，第二维是要不要 archive rubric 判定为失活分支的历史。这样我们可以分别测出事实过滤的效果、路径过滤的效果，以及两者联合时是否互补。

另外，我们会单独比较“只做 clean history”和“clean history 加可见 rubric state”。这个比较很重要，因为它能回答：收益到底来自删除误导历史，还是来自多给模型一段任务提示。

主要行为指标包括下一步错误动作率、任务成功率、恢复到可行路径需要多少步，以及额外 token、调用和延迟。安全指标包括 false-drop，也就是错误删除本来相关的历史；false-archive，也就是把合法路径误判成偏航；replacement 是否引入了新的错误；以及 active history 是否仍保留完成下一步所需的关键 transition 和 tool result。

第一阶段是四周最小可行研究原型。我们先集中在 1 到 2 个历史格式明确、能够构造 active-history view 的宿主，使用 40 到 60 个 GUI-only tasks 做 pilot。这个 pilot 用来验证机制链路和估计效应，不会被包装成已经对 5 个百分点差异有充分统计功效的正式结论。

---

## 10. 预期贡献、边界与结束（约 1 分钟）

最后总结一下，这个工作的贡献有三点。

第一，我们把 GUI agent 的单轨迹历史风险拆成两个不同问题：历史事实是否可靠，以及真实历史是否仍属于当前可行路径。

第二，我们实现一个真正位于 model call 之前的 history gate。它保存完整 provenance，但只把经过筛选的 active history 交给原 agent。

第三，我们用状态一致的配对分支，分别测量错误历史、真实偏航历史以及两种过滤机制的因果影响。

我们不会主张 rubric 是事实真值，也不会主张所有已有 agent 都不检查历史。Sentinel 的定位更具体：它把已有的 GUI 证据、任务里程碑和宿主原生 pre-steps 接到同一个运行时过滤接口上。

如果用一句话概括：

> Sentinel 是一个 GUI agent 的 pre-prompt history gate。它在每次模型决策前，用 GUI 和执行证据移除错误 pre-steps，用动态 rubric 移出真实但偏航的 pre-steps，再把 clean active history、简短任务状态和当前 GUI 交给原 agent 自己决定下一步。

谢谢大家。

---

## 临场速记版：忘词时只看这六句

1. 我们研究的是**同一个 GUI task、同一条 trajectory 中被反复注入的 pre-steps**，不是外部 experience。
2. 风险有两种：**历史说错了**，或者**历史说得没错但轨迹走偏了**。
3. Rubric 在任务开始时生成 definition，运行中更新 state；`H/S/P/C` 分别是硬要求、检查点、路径和约束。
4. Sentinel 的产出不是点击动作，而是 `task + clean active history + rubric state + current GUI`。
5. History gate 的五个操作是 `KEEP / DROP / REPLACE / ARCHIVE / KEEP_UNCERTAIN`。
6. 实验在同一 GUI 状态下只改变历史，分别测事实过滤、偏航分支过滤和两者联合的效果。
