# G1.6 Decision Log

This additive log exists outside the byte-frozen G1.1/G1.2/G1.3 contract closure. The
historical `DECISION_LOG.md` and every frozen schema under `schemas/g1/`, `schemas/g1_2/`, and
`schemas/g1_3/` remain unchanged.

## D-029 — G1.6 仅授权 CPU/manual 私有标注工作区与封存，不授权 replay

**状态：Locked（owner 于 2026-08-27 明确授权 ALE-324 / G1.6）**

Owner 要求开始 ALE-324，并把 G1.6 中所有需要人工研究、判断和标注的步骤做成可点击网页，
提供明确选项供人工选择。该授权只允许离线、CPU-only、人工语义决策的 G1.6 工作：从 active
formal G1.3 v1.1 ReplayCapsule 的两个 curator channel 生成 hash-bound reviewer packets；在仅
本机 loopback 可访问的私有网页中完成 `ACTION_GOLD` 与 `TRANSFORMATION` 两条互相隔离的
双盲独立复核；对 metric-critical disagreement 进行第三方、identity-disjoint adjudication；
最后按既有 frozen G1.1 schemas 生成和验证 action-gold、Transformation Plan、review ledger、
arm plan、admission ledger 与 G1.6 curation/admission seal。真实证据、packet、草稿、review、
adjudication、receipts 和正式 G1.6 publication 全部写到 Git repo 外的受限、版本化、
append-only/content-addressed data root。

ALE-324 要求的“自然 action 是否 history-consistent 但与 GUI/task inconsistent”必须在前两条
curation channel 都不可逆 resolve 后，进入第三条 `CONSISTENCY_AUDIT` 描述性双盲工作台。它可看
source history、task、target-pre GUI 与历史自然 action，但不能看 accepted-action gold、
transformation 答案、post/later/outcome 或 replay response；结果需双审/冲突仲裁且覆盖 190 units，
但绝不能回流 gold、plan、admission、scoring 或 replay。

网页只是一层人工工作台，不是 semantic annotator。软件可以机械完成只读解引用、hash/坐标/
cut-off/role allowlist 校验、frozen Qwen span 高亮、用户所选 span 的双坐标换算、token/byte/字符
计数、target-only diff 与可逆映射预览、schema 校验、冲突检测、状态聚合和 content addressing；
软件不得自动选择 focal/oracle/sham span、生成或改写 correction、推断 accepted action、判断
claim 真伪、推荐 adjudication 结果、用自然 action 或 task outcome 预填答案，也不得调用任何
LLM/model/provider 代替人工。MAI 的 `G1_6_PENDING` focal span 必须由 transformation reviewer
在完整受保护 record 内人工选择，且不得触碰 `<tool_call>` 或 valid action bytes。

角色可见性必须由 server-side packet projection 和 API authorization 机械执行，不能只依赖
页面提示：

- `ACTION_GOLD` reviewer 只看 exact task instruction、target-pre GUI，以及 request cut-off 前
  允许的 non-history tool/ask-user evidence；不得看 history、misleading target、自然 target
  prediction/action、post-state、later evidence、outcome/evaluator/checker 或 replay response。
- `TRANSFORMATION` reviewer 可看 exact source history 与所需的 pre-cutoff evidence；不得看
  自然 target prediction/action、target result/post-state、later evidence、accepted-action set、
  outcome/evaluator/checker 或 replay response。
- `PRIMARY` 与 `SECONDARY` 在各自 finalize 前互相看不到 proposal。只有两份同 channel、
  same-unit、same-input 的 review 都不可逆 finalize 后，identity-disjoint `ADJUDICATOR` 才能看到
  两份 proposal；adjudicator 仍只能看到该 channel 原本允许的 source packet。
- action-gold 与 transformation reviewer identity sets 必须 disjoint；同 channel 的两位 reviewer
  及其 adjudicator 也必须两两不同。身份约束由 server 端验证，不能信任浏览器自报字段。

Authoritative UI state 由 repo 外 append-only hash-chain journal 和 write-once content objects
重建。每次 draft save 都产生新 immutable snapshot/event；finalized review 不得覆盖、删除或
回退，修正需新 workspace/version。浏览器 local storage、cookie 或内存状态只能保存非权威 UI
偏好，不得成为 annotation source of truth。工作区必须拒绝 symlink、path traversal、existing-
content mismatch 和跨 workspace/unit/channel 引用；敏感 evidence 不得上传、外发或写入 Git。

PRIMARY/SECONDARY 的相同输入由 reviewer-neutral `source_packet_sha256` 判断；reviewer/role/
assignment/peer refs 只进入不同的 assignment packet hash。身份使用 owner registry 中唯一 canonical
principal、一个 workspace-wide 32-byte key 与冻结 HMAC-SHA256 公式；manifest 固定 key commitment
和 canonical owner-registry SHA-256。浏览器自报 identity/alias/role 不具权威性。

所有 workspace/payload/event/source-packet digest 使用冻结 canonical JSON：UTF-8、key 排序、
compact separators、`ensure_ascii=false`、禁 NaN、hash subject 无尾换行；JSONL 只在 event bytes 后
额外写一个换行。每种 event kind/role/channel/payload 组合由 schema closed/fail-closed 校验。

本授权明确允许浏览器与 annotation server 之间的 `127.0.0.1`/`::1` loopback HTTP 通信，
但不允许 external bind、反向隧道、外部 hosting、遥测、CDN、remote font/script、DNS、HTTP(S)
外联或其他 external network。它不授权 provider client、model/token-generation、GPU probe/use、
模型权重加载或服务、除 owner 显式前台启动的单进程 loopback annotation server 之外的
subprocess/container/service launch（包括 reloader/child worker）、backend restore/prefix/live replay、
自然任务、MobileWorld/generated GUI/tool/action 执行、treatment response、runtime Sentinel 或
自动语义推断。人工在 annotation webpage 中点击选项是本条授权的标注输入，不是 generated
MobileWorld action。

Active G1.3 v1.1 capsules 和 publication 保持只读，原有
`execution_ready=false`、`provider_invocation_allowed=false` 与
`treatment_response_generation_allowed=false` 不得翻转或重解释。G1.6 seal 最多可在完整人工
审核与 validators 通过后设置 `curation_and_admission_sealed=true` 和
`admission_ready=true`；它必须继续保持 `execution_ready=false`、
`treatment_response_generation_allowed=false`、treatment response count 0，并将下一 gate
固定为 G1.7。网页可运行、packet 可浏览或部分人工 review 完成，都不等于 G1.6 seal 完成，
更不授权进入 GPU batch。

正式人工 finalization 与 G1.6 seal 还必须绑定 G1.5 CPU publication 中选定的 Qwen `flat_progress`
与 MAI `raw_replay` codec ID/version/implementation/capability/History-IR/renderer/tokenizer hashes；对
每份 accepted plan 完成 deterministic extraction、Original round-trip、各 applicable arm rendering、
target-only diff、non-target preservation 与可逆 byte/codepoint 映射。该 gate 只允许 CPU render，
不授权 provider/model/GPU/replay。还需输出描述性 disagreement statistics 与不含 arm/hypothesis 的
blinded-gold catalog；二者均不得变成 outcome 或 intervention rule。

`G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md` 是本条的 normative implementation contract。
`schemas/g1_6/` 只定义 annotation workspace、role-projected packet、assignment-scoped CPU preview、
intermediate proposal 和 append-only event envelopes；它们不得复制、放宽或替代 frozen
G1.1 formal output schemas。

## D-030 — 单人仅可执行隔离的非正式 first pass，不得伪装独立复核

**状态：Locked（owner 于 2026-08-28 明确确认“只有 1 人”并授权单人模式）**

Owner 当前只有一位真实 curator。不得通过多个用户名、别名、role 或 session 把同一个人伪装成
独立 `PRIMARY`、`SECONDARY` 或 `ADJUDICATOR`。D-029 的正式双盲独立性、第三方裁决和 frozen
formal output 要求保持不变。

新增 `SOLO_FIRST_PASS` 仅用于保存非正式人工候选：同一真实 principal 按全局阶段顺序完成全部
190 条 `ACTION_GOLD`，再完成全部 190 条 `TRANSFORMATION`，最后才可查看并完成全部 190 条
preliminary `CONSISTENCY_AUDIT`。每条可反复保存 append-only 草稿，并可不可逆锁定本阶段结果；
后续阶段尚未开放时，packet、image、preview、draft 和 lock API 均必须 fail closed。Consistency
使用前两阶段所有 immutable lock 的 content-bound precursor checkpoint，不得将其称为 formal
channel resolution。

该模式使用独立的 owner-only registry、workspace manifest、assignment key 和
`solo-first-pass-events.jsonl`。每条记录固定 `review_tier=NON_FORMAL_SOLO_FIRST_PASS`、
`counts_as_independent_review=false`、`formal_resolution_eligible=false`、
`admission_eligible=false`、`promotion_allowed=false`、`replay_eligible=false` 和
`cross_channel_exposed=true`。它不得生成 `REVIEW_SUBMITTED`、adjudication、formal export、
admission 或 seal；正式 endpoint 在 solo session 下必须拒绝。

未来获得真实独立 reviewers 时，必须新建 formal workspace、registry 和 assignment key。Solo
journal 只保留为 non-authorizing precursor receipt，且在盲审完成前不得向 formal reviewers
展示；不存在把 solo lock 原地导入、晋升或改名为正式 review 的 API。

Owner 同时明确允许使用一个 detached `tmux` session 运行这一个单进程 annotation server，作为
D-029 前台包装要求的窄化运维例外。服务仍只能绑定 `127.0.0.1`，必须保持 `workers=1`、
`reload=false`、`proxy_headers=false`，不得启动外部网络、model/provider/GPU/replay/action 路径。

Owner 还明确要求通过其正常 SSH 登录连接做本地端口转发以打开该网页。这只授权 client 端
`127.0.0.1:8766` 到 server 端 `127.0.0.1:8766` 的单端口 `ssh -L`，并要求
`ExitOnForwardFailure=yes`。禁止 `-R`、`-D`、`GatewayPorts`、`0.0.0.0` client bind、共享代理、
外部 tunnel/hosting 或向第三方暴露端口；该转发不改变 annotation server 的 loopback-only
authority，也不授权任何应用层外联。

`G1_6_SOLO_FIRST_PASS_AMENDMENT_V1.md` 与两个 additive solo schemas 是本条的 normative
实现边界；D-029 与正式 schemas 不因本条放宽。

## D-031 — 三路 AI Action-Gold 候选只作单人辅助，不构成人工复核

**状态：Locked（owner 于 2026-08-28 明确授权 AI 先拟候选、唯一人类逐项复核）**

Owner 只授权三个彼此隔离的 Codex research-agent streams，对 active 190-unit population 的
`ACTION_GOLD` reviewer-neutral blind packet 离线拟定非权威 action-predicate 候选。本条仅在
这个候选 campaign 内窄幅取代 D-029 对 model-assisted action suggestion 与 target-pre GUI
inspection 的禁止；不授权仓库或 annotation server 调用 AI/provider API，不授权 actor model、
external network、GPU、weights、replay、MobileWorld action 或 treatment response。

每个 stream 只能看到相同冻结输入：exact task instruction、target-pre screenshot，以及 request
cutoff 前 allowlisted tool/ask-user evidence。禁止 history、misleading span、natural target output、
post/later/outcome/checker、transformation、human draft/decision 和 peer-agent output。三个 stream
不得互看、投票、排名、自动合并，且不得根据人类反馈 regenerate。输出不得保存 chain-of-thought，
只允许 evidence-linked concise rationale 与 uncertainty note。

AI output 不是 evidence、review、gold、resolution 或 adjudication，固定
`counts_as_independent_review=false`、`formal_resolution_eligible=false`、
`admission_eligible=false`、`replay_eligible=false`、`auto_apply_allowed=false` 和
`human_review_required=true`。AI 只可提出 action-predicate alternatives 或 `ABSTAIN`；不得替人
选择 `ACCEPT`、`EXCLUDE` 或 `NO_GOLD_CONSENSUS`。

唯一人类必须逐 atomic candidate 明确选择 `ADOPT_TO_FORM`、
`ADOPT_WITH_EDITS_TO_FORM`、`USE_AS_SUPPLEMENT` 或 `IGNORE`。不存在默认、bulk accept、majority、
best、consensus winner 或自动 merge。每次决定前还必须由人类勾选“已亲自核对 task、screenshot
与 cited visible evidence”，网页不得替人自动断言。采用只把候选追加复制到浏览器内存的 dirty
form，不折叠等价项，并将全部 predicate 人工确认与 closed-world/completeness 确认重置为 false；
它不得 autosave、finalize 或 lock。只有人类随后另点现有 solo draft/lock 按钮才可写入 solo
journal。

Candidate campaign、objects、receipts 与 human candidate-decision journal 必须位于第三个独立的
repo-external root，且不得嵌套或写入 formal/solo roots。网页只有已冻结候选的 read endpoint 与独立
decision append endpoint；禁止任何 `/generate`、`/regenerate`、`/rank`、`/merge` 或
`/accept-all` endpoint。Exposure 必须同时绑定 workspace-scoped identity 与跨 workspace 可重算
但不泄露 access secret 的 owner-registry principal commitment；formal registry/store 必须消费该
exposure set 并在认证或权威操作前拒绝匹配 principal。看到候选的 principal 记录为
`AI_ASSISTED_SOLO_CURATOR`，以后不得担任 formal G1.6 Primary、Secondary 或 Adjudicator；solo
结果仍不可晋升或迁移为 formal review。

`G1_6_AI_ACTION_CANDIDATE_ASSISTANCE_AMENDMENT_V1.md`、
`G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md` 与 `schemas/g1_6_ai/` 是本条的 normative additive
边界。Campaign 必须诚实记录 `ai_semantic_suggestion_performed=true`，以及 task/GUI evidence
进入 Codex agent context 的新数据披露；所有 target-actor/provider/project-GPU/replay/action/
treatment authority 继续为 false。

Owner 随后于 2026-08-28 再次明确授权继续 ALE-324/G1.6 D-031、更新两份 `AGENTS.md`、
本地提交实现、把通过验收的 sealed campaign 部署到 repo-external root，并关闭/替换旧版 tmux
进程后重启同一个 loopback annotation website。该运维确认不授权 GitHub push、formal G1.6
completion/export/seal、GPU、真实 provider、external network、replay 或 MobileWorld action；旧
solo workspace/registry/key 与已持久化记录必须保留，新进程只能在同一 workspace 上升级。

## D-032 — Solo Action-Gold 候选改为三选一与无填表确认

**状态：Locked（owner 于 2026-08-29 明确要求“只直接选择，不用填写”）**

Owner 明确要求把 D-031 的单人候选网页进一步简化为每条 atomic candidate 只显示三个主要
选择：`最优（直接用）`、`正确（也可用）`、`错误（不用）`。这三个中文质量标签只是
`SOLO_FIRST_PASS` 的人机交互投影，分别写入既有 append-only decision 值
`ADOPT_TO_FORM`、`USE_AS_SUPPLEMENT` 与 `IGNORE`；它们不改变 frozen candidate bytes 或
decision schema。`最优` 不产生 formal rank、winner、vote 或 Agent 优先级；`最优` 与 `正确`
在最终 accepted set 中都只是合理的一步动作。

本条在 solo 简易界面内窄幅取代 D-031 的四按钮与独立 checkbox 呈现：每个三选一按钮必须
明确写明“点击即表示本人已核对 task、target-pre screenshot、可见 evidence 与候选动作”，该
显式点击本身承载原四项 human attestation；页面不得默认、批量或代点。技术 JSON、evidence ID、
备注与数字坐标默认隐藏且不要求填写。点击或聚焦候选后，网页必须按 Agent A/B/C 明确标色，
把 POINT 的所有合法区域与中心标记、DRAG 的起点/终点与方向持续叠加在 target-pre 截图上；
无坐标动作必须明确说明没有固定点击位置。overlay 只是视觉解释，不是 evidence 或执行坐标。

所有 atomic candidates 仍必须逐项选择，A/B/C 保持等权，不得 majority、consensus、自动选择、
生成、regenerate、deduplicate 或 merge。三选一只写 candidate decision journal，不写 solo
annotation journal。所有候选决定完成且至少一条为 `最优` 或 `正确` 后，页面可提供一个单独、
不可逆、措辞完整的“确认并锁定本任务”按钮。该按钮的显式点击同时确认：保留候选的 exact
字段/区域/evidence/rationale 均已由本人核对；保留项构成当前截图下完整 closed-world reasonable
one-step set。D-032 简易路径同时窄幅取代 D-031 的“点击后立即 deep-copy 至 dirty form”：三选一
阶段只写 candidate journal，不物化 annotation form；只有单独最终确认才物化保留项，人工编辑
fallback 仍沿用原 dirty-form 规则。网页最终只发送 closed `ai_simple_lock=true` 请求；服务器在
candidate-decision journal 的 shared lock 内重读每条最新决定、从冻结 stable candidate bytes
机械构造并完整校验既有 schema-valid `ACCEPT` solo payload，将保留 predicate 的
`human_selected`、closed-world 与 completeness 设置为 true，并持锁直到 solo journal durable
append 完成。decision supersession 使用同一把锁的 exclusive 模式，因此另一标签页不能在核验与
append 之间改变 retained set。这仍是
`NON_FORMAL_SOLO_FIRST_PASS`，不计独立 review、不可晋升、不可 formal export/admit/seal/replay。

若全部候选为 `错误`/`ABSTAIN`、存在旧版 `ADOPT_WITH_EDITS_TO_FORM`、candidate material
duplicate、校验失败、候选缺失或人类认为位置/字段不准确，简易锁定必须 fail closed，并只允许
打开保留的高级人工编辑器；不得自动推断 `EXCLUDE`、删除旧 journal 事件、修复坐标、合并候选
或编造 gold。候选选择和最终锁定仍是两个独立动作。网页服务继续只允许 owner-authorized
single-process loopback/tmux；GPU、target actor/provider、external network、replay、
MobileWorld/generated action 与 treatment response 仍全部禁止。

## D-033 — 剩余 186 条只发布隔离的 AI-only Action labels，不冒充人工标注

**状态：Locked（owner 于 2026-08-30 明确要求“按 AI-only 完成剩余 186 条”）**

Owner 已亲自锁定前 4 条 `ACTION_GOLD`，并明确要求由 AI-only 方式完成其余 186 条。该授权
只允许三个彼此隔离的 Codex research agents 各自复核一个 62-unit shard；每个 agent 只能读取
冻结的 D-031 blind packet、target-pre screenshot 与同 unit 的 A/B/C frozen candidates，不得读取
history、natural action、post/later/outcome、transformation、人类 journal/decision、peer batch output、
registry 或 secret。它们只能保留/拒绝现有候选或声明没有可靠候选，不得生成、修补、执行动作或
保存 chain-of-thought。

这 186 条必须发布到第四个、repo-external、content-addressed 的独立
`AI_ONLY_ACTION_LABELS` root。它们不是 human annotation、independent review、gold、resolution、
adjudication、formal export 或 admission；不得写入/导入 solo 或 formal journal，不得打开
Transformation，也不得使 ALE-324 完成。Manifest 必须绑定 active G1.3、完整 sealed D-031
campaign、三个 batch draft hash，以及 solo journal 中恰好 4 个已锁 Action unit 的不可变 prefix；
publication 本身不得复制 reviewer identity、assignment identity、secret 或人类 proposal 文本。

`G1_6_AI_ONLY_ACTION_LABELS_AMENDMENT_V1.md` 与 `schemas/g1_6_ai_only/` 是本条的 normative
additive boundary。所有 authority flag 固定 false，唯一诚实的语义披露是
`ai_semantic_labeling_performed=true`。仍禁止 GPU、target actor/project provider、external
network、model weights、backend restore、replay、MobileWorld/generated action 与 treatment
response。
