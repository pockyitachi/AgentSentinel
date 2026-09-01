# Instructions for Coding Agents

本目录服务于 MobileWorld pre-step motivation audit。开始任何代码修改前，必须完整阅读：

1. `README.md`
2. `PROJECT_CONTEXT.md`
3. `DECISION_LOG.md`
4. `G1_4_DECISION_LOG.md`
5. `G1_5_DECISION_LOG.md`
6. `G1_6_DECISION_LOG.md`
7. `COLLECTOR_DESIGN.md`
8. `EVENT_CONTRACT_V1.md`
9. `IMPLEMENTATION_GUIDE.md`
10. `TEST_AND_ACCEPTANCE.md`
11. `SERVER_AGENT_INSTRUCTIONS.md`
12. `STATUS.md`
13. `G1_CAUSAL_REPLAY_PROTOCOL_V1.md`
14. `G1_LOCKED_ANALYSIS_PLAN_V1.md`
15. `G1_PORTABLE_SENTINEL_CONTRACT_V1.md`
16. `G1_REPLAY_CAPSULE_CONTRACT_V1.md`
17. `G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md`
18. `G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md`
19. `G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md`
20. `G1_4_NONFORMAL_LIVE_SMOKE_ENGINEERING_CLOSE_AMENDMENT_V1.md`
21. `schemas/g1_4/nonformal_live_smoke_manifest.v1.schema.json`
22. `g1_4/nonformal_live_smoke_manifest.v1.json`
23. `g1_4/nonformal_live_smoke_install_record.v1.json`
24. `G1_5_HISTORY_CODEC_CONTRACT_V1.md`
25. `G1_5_HISTORY_CODEC_CAPABILITIES_V1.md`
26. `G1_5_NONFORMAL_COMPATIBILITY_ENGINEERING_CLOSE_AMENDMENT_V1.md`
27. `G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md`
28. `G1_6_SOLO_FIRST_PASS_AMENDMENT_V1.md`
29. `G1_6_AI_ACTION_CANDIDATE_ASSISTANCE_AMENDMENT_V1.md`
30. `G1_6_AI_ACTION_CANDIDATE_PROMPT_V1.md`
31. `G1_6_AI_ONLY_ACTION_LABELS_AMENDMENT_V1.md`
32. `G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md`
33. `G1_SENTINEL_MVP_MIGRATION.md`
34. `g1/registry.lock.v1.json`
35. `schemas/g1_3/replay_capsule.v1_1.schema.json`
36. `schemas/g1_3/capsule_manifest.v1_1.schema.json`
37. `schemas/g1_3/capsule_integrity.v1_1.schema.json`
38. `schemas/g1_3/field_visibility.schema.json`
39. `schemas/g1_3/capsule_exclusion.schema.json`

历史 `replay_capsule.schema.json`、`capsule_manifest.schema.json` 与
`capsule_integrity.schema.json` 保持 byte-frozen v1；正式 G1 使用 Amendment 1 与三个
`v1_1` schema。

## 强制范围

最近完成 **ALE-321 / G1.3 的 CPU-only、offline、derived Replay Capsule
物化、验证与正式发布**。严格限定的 190 个冻结单元（152 个 strict-MHR candidates 加 38 个
selected clean controls）全部生成 capsule；另 38 个 reserve clean controls 只进入 census，
没有生成 capsule 或 exclusion。输入仅限冻结的 G1.1 registry、Collector v1 raw
events/blobs/integrity artifacts 和已冻结的只读定位引用；真实 capsule 已写入 repo 外的
content-addressed、write-once derived data root。
G1.3 Contract Amendment 1 已以兼容 v1.1 publication 修正三项显式 fail-closed
authorization/readiness guard；旧 v1 publication 保持不可变，仅作为历史版本，并已被
v1.1 publication supersede for formal G1 use。

ALE-322 / G1.4 的工程交付范围已按 D-035 收尾。历史 commit
`bf099a1a00f38edc33b6c5cbb1ab5d12d53bd18c` 已实现并验证 exact-request runner、
invariance/target-only diff guard、确定性 arm scheduling、idempotent
append-only attempt/resume 存储、blinded export、schema/CLI、in-process fake provider，以及仅通过
fake SDK client 测试的可注入 OpenAI-compatible Provider Codec。Formal v1.1 capsule 仍是只读
输入，且必须保持 `execution_ready=false`、`provider_invocation_allowed=false` 和
`treatment_response_generation_allowed=false`；本阶段对 formal capsule 的路径必须在任何
外部调用前 fail closed。当前精确工程状态是 `NONFORMAL_LIVE_SMOKE_PASSED`，formal replay
状态是 `DEFERRED_TO_G1_7_NOT_AUTHORIZED`；不存在活动的 G1.4 GPU/model、provider、replay、
treatment 或 action 权限。

`G1_4_DECISION_LOG.md` D-026 曾只授权可能的 live/GPU proof 所需的 inert/code-only
准备：静态冻结模型绑定、纯 call/block/launch 与 caller-injected response 数据记录、仅注入式
容量评估、schema 和 CPU tests。它不授权 client、network、subprocess、GPU probe/use、模型
加载、provider send、replay 或 action。该准备与后续 D-034 smoke 权限均已消耗，不产生任何
新的 live entrypoint 权限。

`G1_5_DECISION_LOG.md` D-036 已按追加式 amendment 关闭 **ALE-323 / G1.5 的有界
engineering scope**。精确状态是
`CPU_CODEC_IMPLEMENTATION_COMPLETE_NONFORMAL_COMPATIBILITY_PASSED` /
`DEFERRED_TO_G1_7_NOT_AUTHORIZED`。已接受 Qwen flat-progress 与 MAI raw-replay 的纯 request
extraction/rendering、精确 curated-span binding、五 arm conformance、preview、secret-free CPU
publication、golden diff 与 G1.4 fail-closed integration。D-035 的十个调用只提供 non-formal
prompt/parser compatibility coverage，未执行正式 History Codec -> Provider Codec 路径，也不计入
D-028 formal matrix。两个 v1 Codec 均保持 `live_ready=false`；正式 matrix、完整 per-attempt
evidence、serving/isolation 与 live seals 全部转交 G1.7，且当前未获授权。不得把 engineering
close 冒充 formal live readiness、formal replay 或 G1.6 curation。

`G1_6_DECISION_LOG.md` D-029 与
`G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md` 授权 **ALE-324 / G1.6
CPU-only 人工标注工作台**：从 immutable G1.3 publication 生成双盲 role-projected packets，
使用本地 hash-verified tokenizer 与 G1.5 pure preview 做 renderer/diff/reversibility 检查，
并将 human-authored review/adjudication 写入 repo-external append-only journal。程序不得替人
判断 focal/oracle/sham/correction/action gold/consistency/adjudication；formal bundle、admission、
seal、replay 与 treatment generation 在独立门禁完成前继续 fail closed。

`G1_6_DECISION_LOG.md` D-030 与 `G1_6_SOLO_FIRST_PASS_AMENDMENT_V1.md` 仅额外授权
隔离的单人非正式 first pass。它必须使用独立 root/key/manifest/journal，按全局
ACTION_GOLD → TRANSFORMATION → CONSISTENCY_AUDIT 顺序锁定，并保持所有独立 review、
resolution、promotion、formal export、admission、replay 与 seal authority 为 false；不得原地
晋升或替代正式双盲 workspace。

`G1_6_DECISION_LOG.md` D-031 只再授权三路彼此隔离的 Codex stream 离线生成非权威
`ACTION_GOLD` 候选，供同一个 solo curator 逐项判断。候选不得成为 evidence/review/gold，
不得计入独立复核；网页只能读取冻结候选与写独立 candidate-decision journal，不能在线生成、
排序、投票、自动应用、保存、锁定或晋升候选。唯一窄幅例外是 D-032：同一 solo curator 逐条
三选一后，可另点一次明确的最终确认，由服务器在 decision-journal 锁内从冻结候选机械派生、
校验并写入一个既有 schema 的非正式 solo lock；该操作仍不得成为 formal review、晋升或发布。

`G1_6_DECISION_LOG.md` D-033 只再授权三个隔离 Codex research agents 把剩余 186 units 分成
三个固定 62-unit shard，逐图复核并发布独立的 `AI_ONLY_ACTION_LABELS` research dataset。每个
agent 只能 retain/reject 冻结 D-031 candidate 或排除本 unit，不得生成新 predicate，不得读取
human/peer/history/future material。该 publication 不是 human review/gold，不得写入或推进 solo/
formal journal，所有 review/export/admission/promotion/replay authority 必须保持 false。

必须：

- 保存模型真正收到的最终 request；
- 保存所有 application-visible API invocations/retries、错误、stream chunks/final assembly 和 raw/normalized response；SDK 内部透明 HTTP retries 不得伪造；
- 保存 `S_t, I_t, P_t, A_t, R_t, S_{t+1}`，其中 provider raw calls/attempts 独立可追踪；
- 使用稳定 ID、版本 manifest 和 content-addressed binary artifacts；
- 通过 feature flag 完全关闭；
- 关闭时保持原行为；开启时也不得修改传给模型的 request 或 agent action。
- 保持 Collector v1、G1.1 的 25-file contract/外部 registry 以及已验收 G1.2
  contract/package/schema 字节不变；
- 对每个 capsule 重新解析并校验 exact pre-call request、blob、target span、因果 cutoff、
  visibility channel 和全部 hash；缺失、歧义或完整性失败必须稳定排除，不能修补；
- 每个 capsule 的 state-access descriptor 必须且只能是 `SERIALIZED_REQUEST_ONLY`、
  `EXACT_CHECKPOINT` 或 `DETERMINISTIC_PREFIX_REPLAY` 之一；G1.3 只记录并验证 descriptor，
  不执行 restore 或 prefix；
- 将 captured response/action/result/post-state 限定为 sealed audit-only reference，绝不能
  进入 runtime-visible、treatment-renderer、`ACTION_GOLD` 或 `TRANSFORMATION` 输入；
- 保证从同一不可变源重复构建得到 byte-identical manifest 与语义相同的 capsule。

禁止：

- 实现 Sentinel 判断器；
- 生成或更新 rubric；
- 给 raw event 写 `wrong/misleading/KEEP/DROP/REPLACE` 标签；
- 过滤、重排、修补或压缩 agent messages；
- 用 HTTP 200、截图变化或 task failure 自动等同动作语义失败；
- 把 API key、Authorization header 或其他 secrets 写入日志；
- 为了匹配本设计而破坏服务器已有用户修改或强制 reset 工作树。
- 调用 target actor model 或任何 project provider/client、发起外部网络请求、使用 GPU、
  加载/服务 project model weights、执行 MobileWorld/generated GUI/tool/action、backend
  restore、prefix 或 live replay、生成 treatment response、自动决定 claim validity、自动选择
  formal intervention，或开始 G1.7+；只允许无网络的确定性 fake-provider conformance、
  provider-free G1.5 CPU checkpoint、D-029 限定的人工 G1.6 CPU workspace，以及 D-031 已明确
  授权的三路离线 Codex 候选 campaign，以及隔离的 D-033 AI-only research publication。
  D-031 是唯一 candidate-suggestion 例外；输出仍不可信且
  必须逐项人审，annotation website 自身不得调用 Codex 或任何 model/provider；
- D-033 是唯一额外的 AI-only semantic-labeling 例外，只能产生与 human journals 隔离、不可晋升、
  不具 formal authority 的 186-unit research publication；
- D-029 唯一允许的 server socket 是 owner 启动、单进程、仅绑定 loopback、无 remote asset、
  强制 same-origin/CSRF 的 annotation site；D-030 只额外允许 owner 的单端口 SSH local forward
  从 client `127.0.0.1:8766` 到 server `127.0.0.1:8766`，禁止 reverse/dynamic forwarding、
  wildcard bind、shared proxy 或 remote hosting；
- annotation browser 内的人类点击/表单输入是获准的 curation 输入，不得转成或执行为
  MobileWorld action；
- 将 G1 label、capsule metadata、visibility classification 或 transformation decision 写回
  raw Collector event。

## 数据原则

Raw collection 是不可变事实层；所有 claim extraction、错误分类、weak/strong uptake 和统计均属于可版本化的 offline derived layer。若实现选择与文档冲突，先更新设计并说明理由，不要静默偏离。

G1.3 的 capsule 也是 derived layer：它只冻结已记录的因果单元与可见性边界，不得把
captured natural action 当作 replay 必须复现的结果，也不得将 future/post-action evidence
提升为模型可见输入。

`EVENT_CONTRACT_V1.md` 是 event 名称、字段和终态规则的唯一权威；“完整 request”指 MobileWorld 在 SDK invocation 前传入的 application-layer arguments，不声称是 SDK 内部最终 HTTP wire body。

Owner 公开证据例外（2026-09-01）：尽管真实 collection/capsule/replay 数据默认必须留在
repo 外，owner 已明确批准只把 Epic 1 正式报告所引用的精确 39 张 content-addressed PNG
原始截图放入 `motivation study/report_assets/screenshots/`，并把唯一固定 PDF
`motivation study/misleading_history_audit_report_20260825.pdf` 放入公开 Git。该例外不覆盖
raw cards、requests、trajectories、model responses、reviewer text、receipts、logs、replay
数据或任何其他 Collector blob。仓库是公开的；这些精确 bytes 进入 commit 后可能长期留在
Git history、fork 和 cache，即使以后删除也不能保证完全收回。截图中的手机号、验证码、姓名、
邮箱等均按 benchmark 的 synthetic/demo fixture 内容发布；其中还包含第三方 app UI、商标与
图片，相关第三方再分发权没有逐项独立核实。截图中任何看似医学或健康的文字都只是静态研究
证据，不构成医学建议或项目背书。该文档发布例外不产生 model/provider/network/GPU/replay/
treatment/GUI/tool/action 权限。

## 完成要求

只有通过 ALE-321 的 capsule 一一对应、rehydration/hash、exact-request、target resolution、
visibility/future-leakage、稳定 exclusion 和 deterministic double-build 验收，并在 `STATUS.md`
记录 commit、命令、测试结果、外部 publication 与已知限制后，才可宣称 G1.3 完成。
ALE-322 的精确工程交付状态是 `NONFORMAL_LIVE_SMOKE_PASSED`；不得称为 formal live
proof、formal replay ready 或 G1.7 ready；其精确 formal-replay 状态是
`DEFERRED_TO_G1_7_NOT_AUTHORIZED`。ALE-323 只按 D-036 的有界 engineering scope 关闭；两个
v1 Codec 仍为 `live_ready=false`，G1.7 formal readiness 仍未授权。
G1.6 workspace checkpoint 不能冒充 gold publication；只有 190 单元双盲 review、必要 adjudication、
formal export/validation/admission/seal 全部完成后才可将 ALE-324 标记为完成。
任何 target-actor/project-provider/external-network/GPU/MobileWorld-generated-GUI/action/
live-replay 仍需另行授权；D-029 owner-started loopback annotation site/human curation clicks
与 D-031 已冻结的离线候选 campaign 是现有窄幅例外。不得把凭据写入代码或本目录。

Owner 直接 smoke 边界（2026-08-30）：尽管上文保留了历史 deferred/backlog
描述，owner 曾额外授权一次 non-formal 直接 GPU 0 smoke：固定
`CUDA_VISIBLE_DEVICES=0`，仅绑定 `127.0.0.1:18007`，按 Qwen 后 MAI
的顺序执行精确 22 个 secret-free synthetic calls。清理只能针对本次
smoke 自己创建的 child 和 session；不得读取任何外来进程的 `/proc`
私有细节，不得向任何外部进程发送 signal 或采取动作，不得执行任何
返回 action。只读 GPU/进程基线检查（包括收尾 `nvidia-smi`）仍允许。任一
失败都立即结束且不得重试。旧 D-034
authority/shim/formal-evidence 链已废弃，不得复用或作为 gate。

GPU 0 结果与替代授权（2026-08-31）：GPU 0 attempt 因当前 vLLM 不支持
`--swap-space`，在模型加载前以 0/22 次调用安全失败；该 attempt 已结束且
不得重试。Owner 现仅授权一次 GPU 4 替代 attempt：固定
`CUDA_VISIBLE_DEVICES=4`，仍仅绑定 `127.0.0.1:18007`，按 Qwen 后 MAI
的顺序执行精确 22 个 secret-free synthetic calls。只能清理本 attempt 自己
创建的 child/session；不得向 `taoz` 或任何外部进程发送 signal、修改、
停止或采取任何动作。任一失败都立即结束且不得重试。旧
authority/shim/formal-evidence 链仍禁止复用。

GPU 4 结果与下一步窄修边界（2026-08-31）：该 attempt 已进入 Qwen 模型
加载/PROFILE，随后因 bundled Triton `ptxas` 的 mode 为 `0644` 而触发
`EACCES`，以 0/22 次调用安全失败。MAI 未启动；`taoz` PID 217927、其
基线/显存与 loopback port 均保持或恢复至基线。该 attempt 已结束且
不得重试。下一步只授权修改 smoke child-process environment，使用 system
CUDA tool paths；不得 `chmod` 或以其他方式修改共享 venv。

GPU 4 最终结果与工程收尾（2026-08-31）：system-CUDA tool-path 修复及 production
prompt/parser 修正后，唯一获准 smoke 完成 Qwen 11/11，完整停止己方服务后再完成
MAI 11/11，共精确 22/22 个 HTTP-200 且 host-parser 成功的调用；retry=0，生成 action
执行数=0。三个原始 artifact 已按 D-035 封存为只读 content-addressed bundle。该结果只支持
`NONFORMAL_LIVE_SMOKE_PASSED` 工程交付，不构成 formal Provider Codec、serving environment、
isolation、treatment 或 replay proof；这些事项仅留待未来 G1.7 另行考虑且当前未获授权。
本次 GPU/model 权限已消耗完毕，不再授权任何 GPU/model attempt。
