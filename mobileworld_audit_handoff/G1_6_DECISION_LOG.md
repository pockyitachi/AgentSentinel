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
