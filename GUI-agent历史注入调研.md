# 调研:当前 GUI agent 怎么把 prev steps 注入 prompt

*2026-08-06,四路 agent 源码级调研(21 个系统);标注 [源码] = 直接读了仓库 prompt 代码,[论文] = 仅论文描述。完整原始结果:tasks/wg1quwjtu.output*

## 一、总表

### 手机框架(prompt 编排式)

| Agent | 历史窗口 | 注入内容 | 核验 |
|---|---|---|---|
| **AppAgent** [源码] | 无步骤列表——**一句滚动总结**替代全部历史 | 模型自己写的"过去动作总结",下一步原样代入 `<last_act>` | **无**——总结在动作效果被观察到之前就写好了 |
| AppAgent v2 [论文] | 同上 + RAG 检索的元素文档 | 自写总结 + 知识库文档 | 部署期无(探索期有反思) |
| **Mobile-Agent-v2** [源码] | **全量**(每步 摘要+动作 成对列出)+ 记忆串 + 进展摘要 | `Step-{i}: [Operation:<摘要>; Action:<动作>]` 编号列表 | **有**:反思 agent 比对前后截图,判 A/B/C,失败不入史 |
| **Mobile-Agent-v3/3.5 (GUI-Owl)** [源码] | 执行器 **last-5 动作**;Manager 压缩计划+错误日志;原生模型全量文字+**最近1–3张图** | 结构化记录:动作 JSON + 自写描述 + **反思器打的成败标签** + 错误反馈 | **有**:ActionReflector 前后截图判 A/B/C,连续2次失败上报重规划 |
| **M3A**(AndroidWorld 官方)[源码] | **全量**,每步一条<50词自写小结,不截断;prompt 里只有当前截图 | 动作后由单独调用生成的小结(它看得到前后截图):做了什么、像不像成功、下一步 | **弱**:小结自报,无独立核验,写了就永久信 |
| T3A(纯文本官方基线)[源码] | 全量自写小结;无任何截图,UI=元素列表 | 比对前后元素列表写的每步小结 | 弱,同 M3A |

### 原生/端到端 GUI 模型

| Agent | 历史窗口 | 注入内容 | 核验 |
|---|---|---|---|
| **UI-TARS 1/1.5 & Doubao 版** [源码] | **文字全量 + 图只留最近5张**(MAX_IMAGE_LENGTH=5,旧图连占位符一起删) | 对话回放:旧截图(仅5张)+ 模型自己的 `Thought:...Action:...` 原文 | **无**——靠最近5张真图隐式兜底 |
| UI-TARS-2 [论文] | 工作记忆(最近N步高保真)+ **情景记忆(语义压缩摘要)** | 近期原始记录 + 系统写的压缩摘要 | 推理期无(质控在训练侧) |
| CogAgent-9B [源码] | **全量**,单当前截图 | 模型自己上一轮输出的 grounded op + 动作描述,tab 分隔编号列表 | **无**——执行成没成功都照抄进历史 |
| Aguvis [源码] | 全量动作串,单当前截图 | `Step N: <自然语言动作>` 换行拼接 | 无 |
| OS-Atlas [源码] | 全量编号步骤行,单当前截图 | 每步子目标描述 | 无 |
| **MAI-UI(阿里)** [源码] | **history_n=3:图只留 2 张旧的+当前;文字全量保留** | 模型完整旧回复回放:`<thinking>+<tool_call>` | **无**——unified_memory 纯追加 |
| **Qwen2.5/3-VL computer-use 官方 cookbook** [源码] | 全量压成一行文字;**只有当前1张图**,每步全新2消息调用 | 模型自报的动作描述串:`Task progress: Step 1: ...; Step 2: ...` | **无**——自报描述直接当"任务进展" |

### 浏览器/Web agent

| Agent | 历史窗口 | 注入内容 | 核验 |
|---|---|---|---|
| **Browser-Use** [源码] | 默认全量;可选 k(保第1步+最近k-1步)+ 压缩块 `<compacted_memory>` | `<step_N>`:**上一轮模型写的 evaluation_previous_goal / memory / next_goal(在看到结果之前写的!)** + 执行器真实 action_results | **半**:执行错误真实;自评/记忆无核验 |
| SeeAct [源码] | 全量动作串行,单当前截图 | 每步一行动作描述,异常前缀 `Failed to` | 无——**prompt 自己承认历史"可能没有清楚充分地记录某些效果"** |
| WebVoyager [源码] | 全量对话轮 + **图只留最近3张**(旧观察换成占位句) | 模型 `Thought/Action` 原文 + 观察轮 | 执行级(Selenium 异常);语义无 |
| **Skyvern** [源码] | **默认 k=1**(仅上一步) | 机器生成 JSON:action + **执行器真实 result(success/exception)**,历史打 `untrusted` 过滤器 | 较强:结果字段是执行真值;prompt 警告"即使历史说完成也要看截图" |
| WebArena / VisualWebArena 官方基线 [源码] | **k=1**:`PREVIOUS ACTION: {上一条动作串}` | 单条原始动作,无结果 | 无 |
| OpenAI Operator/computer-use [源码+文档] | **全量、服务端持有**(previous_response_id 链),溢出 `truncation:auto` 不透明 | 原始 item 链:动作 + **动作后真实截图** + 加密推理块 | 隐式强(截图是真值);**客户端不可改写** |
| Magnitude [源码] | 最近20条思考 + 默认只1张截图 | 带时间戳的思考/动作/观察流 | 无显式 |

### 桌面/computer-use 框架

| Agent | 历史窗口 | 注入内容 | 核验 |
|---|---|---|---|
| Agent-S2 [源码] | 最近8轮完整回放 | 旧截图+文字 / 模型完整旧输出(CoT+代码);另有反思 LLM | LLM 反思(可判跑偏/循环);无程序级 |
| **Agent-S2.5/S3** [源码] | **文字全留,图只留最近8张**(旧图原地删) | 完整旧输出回放 + 每步反思文字 + 笔记缓冲 | LLM 反思逐历史步;程序级无 |
| **微软 UFO/UFO2** [源码] | 黑板全量轨迹(JSON)+ 上一步细节 + 1张旧截图 | 结构化:动作串 + **执行器真实结果** + repeat_time 重复计数 | 较强:执行真值 + 重复告警;语义效果无 |
| **Anthropic computer-use 参考实现** [源码] | 对话全量永不删;**图只留最近3张**(开 prompt cache 时连图都不删——怕破缓存) | API 原生块:thinking/tool_use/**真实 tool_result(带 is_error)**/截图 | 执行级真值天然;**语义效果无任何检查** |
| OSWorld 官方基线 [源码] | **k=3** 完整步(obs+回复对),更旧全丢 | 完整旧观察+完整旧回复回放 | **无** |
| Cradle [源码] | 滚动 LLM 总结(每轮由最近5步折叠)+ 上一步细节 | 全部模型自写:上步动作/理由/反思/总结 | **有**:self_reflection 前后截图+报错判上步效果 |

### MobileWorld 基准的 9 个官方 agent(Tongyi-MAI,ACL 2026,arXiv:2512.19432;全部源码核实)

| Agent | 历史窗口 | 注入内容 | 核验 |
|---|---|---|---|
| general_e2e(API 模型榜单) | 全量回放,图留3张,旧图换占位句 | 模型 `Thought:/Action:` 原文 | 无 |
| mai_ui_agent | 文字全量,图留3张(旧图直接删) | `<thinking>+<tool_call>` 完整旧回复 | 无 |
| qwen3vl | 当前1张图,文字无上限 | 自写结论串 `Task progress: Step 1...` | 无 |
| gui_owl_1_5 | 默认当前1张图,旧轮坍缩成文字 | 自写结论+工具返回 | 无 |
| seed_agent | 全量回放,图留3张 | 原始动作 XML+推理 | 仅 prompt 提示语 |
| ui_venus_agent | 1张图,文字默认全量 | 旧思考+动作原文 | 无(prompt 自认"历史可能不可靠、夸大效果") |
| gelab_agent | **一句滚动总结,零消息历史** | 上一步自写累积总结 | 无(单点失效极致) |
| planner_executor | 同 general_e2e | 规划器原始回复 | 无 |
| memgui(ConAct) | 1张图+三块自管文本 | 模型自发"折叠指令"**破坏性**压缩历史 | 无(坏折叠永久改写) |

关键事实:9/9 无任何效果核验,"结论"在动作执行前写好(记录意图、非结果);平均 27.8 步/任务(AndroidWorld 14.3),62.2% 跨应用;统一 runner + 注册表即插自定义 agent → **中间件实现一次可横跨九个宿主评测**。仓库:github.com/Tongyi-MAI/MobileWorld。

## 二、四条跨系统规律

1. **窗口从 k=1 到全量都有,最主流的是"混合衰减":文字全留、图片只留最近 3–8 张**(UI-TARS 5、MAI-UI 2+1、WebVoyager 3、Agent-S3 8、Claude 参考实现 3)。你猜的 prev 3-5 steps 存在(OSWorld k=3、Mobile-Agent-v3 last-5 动作),但不是唯一形态。
2. **历史的主体是"模型自己写的话"**:滚动总结(AppAgent、Cradle)、每步小结(M3A/T3A、Mobile-Agent-v2)、自报动作描述(Qwen cookbook)、自评+记忆(Browser-Use——而且是在看到执行结果**之前**写的)。原始动作串和真实执行结果只在少数系统里(Skyvern、UFO、Claude loop)。
3. **核验一栏几乎全空**。有反思的(Mobile-Agent-v2/v3、Cradle、Agent-S 系)全是**只查刚发生的一步,查完不回头**;执行级错误有时真实;**回头重查旧记录对不对得上当前屏幕:21 个系统里零个**。
4. **主张比证据活得久**:文字(模型的 claims)整个任务存续,截图(证据)几步就被裁掉。旧记录的原始证据已经离开上下文——**模型想自查都没有材料,对着当前屏幕的回溯审计是唯一可能的检查**。这是中间件的结构性存在理由。

## 三、领域自己的"免责声明"(动机引用素材)

- SeeAct prompt 原文承认:历史"may not clearly and sufficiently record some effects of previous actions"。
- Skyvern 把历史管道打上 `untrusted` 过滤器,并在 prompt 里警告"即使动作历史显示目标已达成,也要检查截图和 DOM"。
- Anthropic 参考实现开缓存时连旧图都不敢删——**历史一旦写下,谁都不敢动**。领域用免责声明和"多信当前截图"的祷告替代了修复。

## 四、对我们设计与实验的直接含义

**宿主 agent 选择**(覆盖三种形态+一台带反思的):
- **M3A/T3A**(主宿主):AndroidWorld 官方、全量自写小结、无核验——最典型的脆弱形态,且是我们熟的基准;
- **Mobile-Agent-v3**:自带反思器——正好做论文的核心测量"装了每步反思之后还漏多少"(过时类、晚显形类、跨步摘要类);
- **Browser-Use 或 UI-TARS**:工业相关性 / 原生模型形态的泛化性检查。

**坏记录注入面**:自写小结/自评字段是天然注入点(把失败步的小结改成成功、给滚动总结加幻觉内容)——对应我们评测里的可控构造器。

**中间件要做两种适配器**:
- **模板槽式**(M3A/T3A、CogAgent、Qwen、SeeAct、UFO、Cradle):历史每步由 harness 的 Python 列表重建——**拦这个列表就行,最容易**;
- **对话累积式**(UI-TARS、MAI-UI、Agent-S、Claude loop、WebVoyager):要改 messages 数组,必须保住图片与占位符对齐;**改旧 turn 会作废整段 prompt cache 后缀**(Anthropic 实现连删图都躲着缓存走)——设计选项:不改旧记录,在末尾追加"历史勘误块"(标注哪些记录已被当前证据推翻),保缓存但不移除误导原文;或按步全重建,吃缓存代价。这个"勘误 vs 重写"的取舍本身就是一组消融。
- OpenAI Operator 的服务端历史**客户端不可改写**——中间件必须放弃 previous_response_id、自管 item 列表才审计得动;论文可作为适用边界说明。
