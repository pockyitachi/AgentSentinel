# AgentSentinel MobileWorld Research Handoff

本目录保存 AgentSentinel 在 MobileWorld 上的权威研究边界、原始数据契约、实现决策、
验收记录和 G1 因果可行性协议。它不再只是“尚待实现的 collector 设计包”：
Collector 与 Epic 1 六模型调查已经完成，当前工作已经进入 G1。

## 当前状态

截至 2026-08-28：

| Workstream | 状态 | 说明 |
| --- | --- | --- |
| Runtime Audit Collector | 已实现并用于正式研究 | 默认关闭、passive、fail-open、event-sourced、append-only、label-free；保存应用层实际 SDK 参数和完整 transition |
| Epic 1：Motivation Investigation | **已完成** | 六模型各 117 tasks，共 702 个 model-task cases；完成 exact history reconstruction、outcome-blind MHR 与 local-harm 审核和 outcome-aware failure-link 审核 |
| ALE-319 / G1.1 | **已完成** | 冻结 CPU-only causal-replay protocol、schemas、pre-gold registry、controls、model/config manifest 与 locked analysis plan；没有生成 treatment response |
| ALE-320 / G1.2 | **已完成** | 已冻结可移植 History IR/Core、codec/provider interface、protocol validator、sidecar schemas 与六 family fixture conformance |
| ALE-321 / G1.3 | **已完成；v1.1 已纠正** | CPU-only 正式发布 190 个 immutable capsules、0 exclusions（152 strict + 38 selected clean）；Amendment 1 增加显式 fail-closed authorization guards；另 38 reserve 只进 census；未调用模型/provider/GPU/GUI/replay |
| ALE-322 / G1.4 | **CPU/fake checkpoint 与 inert live-proof code preparation 已实现；live proof 延后** | commits `bf099a1a00f38edc33b6c5cbb1ab5d12d53bd18c`、`74b18c6bc0f4ce6c56c0e9b979cafec0b5298b6d` 已验证 runner/fake path 和 D-026 no-execution preparation；全部 readiness/authorization 与 safety fields 仍为 false，无 formal publication，story 仍未完成 |
| ALE-323 / G1.5 | **CPU History Codec checkpoint 已实现；live smoke 延后** | Qwen flat-progress 与 MAI raw-replay 的 exact extraction/render/diff/reversibility、arbitrary human-draft five-arm preview、secret-free CPU publication 和 G1.4 fail-closed interface 已验证；`live_ready=false`，10-call GPU backlog 未授权，story 仍未完成 |
| ALE-324 / G1.6 | **CPU/manual annotation workspace 已实现；人工标注与 formal export 待完成** | 私有 loopback-only 网页覆盖 190 个盲化 packet 的 action gold、transformation、consistency 与独立 adjudication；G1.5 preview/tokenizer 只读绑定，journal repo-external append-only；无 provider/GPU/replay/action |

当前 MobileWorld/ 是 AgentSentinel monorepo 中的实际实现与研究代码来源；上游
Tongyi-MAI/MobileWorld@0dcd098... 只用于 provenance，不是当前 push 目标。

## Epic 1：六模型调查（已完成）

Epic 1 在同一 canonical GUI-only 117-task suite 上分别审计六种 host-native history
representation。MHR 要求错误或 stale 的模型历史实际进入后续 actor request，并被后续
decision 明确复用。下表另列其中观察到局部 harmful effect 的 MHR cases；local harm 是
reuse chain 的属性，不是第二个 history event。

| Model | Previous-history representation | MHR cases | MHR cases with observed local harm |
| --- | --- | ---: | ---: |
| MAI-UI-8B | raw replay | 7/117 (5.98%) | 7/117 (5.98%) |
| Qwen3-VL-8B | flat task progress | 35/117 (29.91%) | 32/117 (27.35%) |
| GELab-Zero-4B | rolling summary | 33/117 (28.21%) | 29/117 (24.79%) |
| UI-Venus-1.5-8B | flat previous actions | 3/117 (2.56%) | 1/117 (0.85%) |
| GUI-Owl-1.5-8B-Instruct | hybrid-collapsed action history | 11/117 (9.40%) | 7/117 (5.98%) |
| MemGUI-8B-SFT | structured H/L/M folding | 27/117 (23.08%) | 18/117 (15.38%) |
| **Total** | six families | **116/702 (16.52%)** | **94/702 (13.39%)** |

六组数据合计 128 success、574 failure。116 个 strict-MHR cases 包含 272 条 reuse
chains，其中 94 个 cases / 239 条 chains 观察到局部 harm。对 108 个失败且含 MHR 的
cases 做 outcome-aware review 后，读者口径保留 10 个 explicit final-decision stop 与
48 个 earlier unrecovered derailment：合计 58/108 failed-MHR cases，也就是 58/574
全部失败 cases。

这些结果全部是 observational evidence，且 causal_claim_supported=false。它们不能证明
MHR 是失败的唯一原因、不能给模型排名，也不能估计删除或纠正 history 会提高多少成功率。
六种 representation 必须各用自己的 exact mapper 和语义口径。GUI-Owl 只使用 corrected
v3 的 11/117 与 7/117；已撤回的旧 v2 1/117 结果不得再引用。

完整定义、逐模型证据和限制见
[MobileWorld/docs/misleading_history_audit_report.md](../MobileWorld/docs/misleading_history_audit_report.md)。

## 最近完成阶段：ALE-321 / G1.3

G1.2 的可移植、CPU-only、evaluation-time transformation contract 已经完成并验收：

- model-agnostic canonical History IR 与 Sentinel Core；
- 按 representation family 隔离的 extraction/render codec；
- provider codec interface 与 provider-call 前 protocol validator；
- raw request、Transformation Plan、rendered request、diff 与 provenance 的 derived sidecar；
- 对 curated transformation plan 做确定性应用。

ALE-321 / G1.3 已在该契约与冻结 G1.1 registry 之上，将严格限定的 190 个目标（152 个
strict-MHR candidates 加 38 个 selected clean controls）全部物化并正式发布为 immutable、
self-validating Replay Capsules，0 个 exclusion。另 38 个 reserve clean controls 只进入
census，没有生成 capsule 或 exclusion。每个 capsule 冻结 exact pre-call request、
system/tools/task/history/current-observation 分区、目标 span、Blob 与环境 provenance、因果
cutoff，以及严格三选一的
`SERIALIZED_REQUEST_ONLY`、`EXACT_CHECKPOINT` 或 `DETERMINISTIC_PREFIX_REPLAY`
state-access descriptor，并把 captured response/action/result/post-state 严格隔离为 audit-only
reference。

本阶段没有执行 claim truth inference、provider/model invocation、GPU、GUI action/replay、
live prompt interception、intervention 选择/生成、treatment response 或自动 runtime Sentinel。
所有 190 个目标均通过完整性验证，无需稳定排除。

Contract Amendment 1 在不改变 protocol、人口或来源的前提下，新增
`provider_invocation_allowed=false` 与
`treatment_response_generation_allowed=false`，并保留原有
`execution_ready=false`。当前正式 v1.1 publication 的 manifest/content address 是
`8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402`；旧 v1
publication `c2af8b8393e2df2da21bedcc98614e60a08b8254dc03da373ce72d67fe7c76c5`
保持不可变和历史可识别，但已被 supersede for formal G1 use。

Collector raw layer 保持不可变、passive 和 label-free。所有 G1 label、plan、rendered
request、response 与统计都属于 repo 外的 versioned derived/replay layer。

D-023/D-024 只授权并记录上述 CPU-only G1.3 capsule 物化、修正与发布。

## 当前活动阶段：ALE-322 / G1.4（CPU/fake checkpoint 与 inert live-proof code preparation 已实现）

[`G1_4_DECISION_LOG.md`](G1_4_DECISION_LOG.md) 中 D-025 允许先完成不需真实模型或外部运行时的
runner 工程：formal v1.1 capsule
只读加载与验证、exact-request preparation、target-only diff/invariance guard、预注册
arm schedule、idempotent append-only attempt/resume 存储、blinded-scoring export、schema/CLI、
in-process fake provider，以及只通过 fake SDK client 验证的 Provider Codec 边界。

D-026 与
[`G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md`](G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md)
在该 checkpoint 上只增加未来 live/GPU proof 所需的 inert、CPU-only 代码准备：静态冻结
model/config binding、no-send OpenAI SDK call 与 paired-block descriptor、caller-injected
response envelope 的纯投影、未执行的 vLLM launch plan、只消费 caller-injected snapshot 的
GPU capacity assessment，以及 no-send `prepare-live-code` CLI inspection。实现提交是
`74b18c6bc0f4ce6c56c0e9b979cafec0b5298b6d`。`live_code_prepared=true` 只表示代码准备状态，
不表示 resource、transport、run 或 formal replay readiness；全部 8 个 readiness/authorization
fields 与 9 个 safety fields 仍为 false，也没有 formal run publication。

Formal capsule 的 `execution_ready=false`、`provider_invocation_allowed=false` 和
`treatment_response_generation_allowed=false` 不变，不得被 wrapper、CLI、resume 或 test path
绕过。当前不授权任何真实 model/provider/network 调用、GPU、模型加载/服务、
GUI/tool/action、backend restore/prefix、live replay 或 treatment response。“约 90%”是
调度目标而非验收比例，不能用作完成度声明；ALE-322 的精确状态仍是
`IN_PROGRESS_LIVE_PROOF_DEFERRED`。G1.5 live History Codec、G1.6 curation/gold/admission、
G1.7 serving/seed/isolation/backend/scorer/restorer/run-ready/execution seals 与 owner 的 live/GPU
授权均未完成。无论后续是否运行 live proof，本 story 都不执行模型返回的 GUI action。

CPU/fake checkpoint 的实现提交是
`bf099a1a00f38edc33b6c5cbb1ab5d12d53bd18c`。它不是正式 replay publication，也不包含
任何 treatment response；精确回归、冻结输入与剩余 live gate 记录在 [STATUS.md](STATUS.md)。

## 当前活动阶段：ALE-323 / G1.5（CPU History Codec checkpoint）

[`G1_5_DECISION_LOG.md`](G1_5_DECISION_LOG.md) D-028 与
[`G1_5_HISTORY_CODEC_CONTRACT_V1.md`](G1_5_HISTORY_CODEC_CONTRACT_V1.md) 只授权 Qwen
flat-progress 与 MAI raw-replay 两个 History Codec 的 CPU-only 阶段。实现范围是 exact captured
request syntax extraction、request/hash-bound 的外部 curated span binding、复用冻结 G1.2 core 的
五 arm render/diff/reversibility、secret-free captured-shape fixtures/golden diff，以及复用 G1.4
invariance/runner 接口并在 provider encode/send 前阻断。

两个 Codec 的 `scope=LIVE` 只声明 representation capability；`live_ready=false` 保持不变。
Formal G1.3 capsule 三项 authorization guards 仍为 false，G1.4 live entrypoints 仍机械禁用。
ALE-323 当前精确状态是 `CPU_CHECKPOINT_IMPLEMENTED_LIVE_SMOKE_DEFERRED`，不是完成。D-028
冻结的剩余验收为 2 codecs × 5 arms × 1 invocation = 10-call non-formal smoke matrix；它已记录到
统一 GPU backlog，但没有执行授权。能力和稳定 fallback 见
[`G1_5_HISTORY_CODEC_CAPABILITIES_V1.md`](G1_5_HISTORY_CODEC_CAPABILITIES_V1.md)，CPU conformance
requirements coverage 见
[`G1_5_HISTORY_CODEC_TEST_COVERAGE_V1.md`](G1_5_HISTORY_CODEC_TEST_COVERAGE_V1.md)。
`mobile_world.offline.g1_history_codecs` 另公开纯 CPU、只读的
`bind_human_record_spans`、`rank_correction_candidates`、`build_five_arm_preview` 与
`build_clean_control_preview`：调用者可把
exact G1.3 source record、显式人工 span/correction/oracle/sham/delimiter repair 绑定到任一已选
Codec，得到 strict 五 arm 或 clean Original/Sham rendered history、exact correction anchors、
summed-focal sham token match、target-only diff 与可逆 source mapping。序列化 preview 不含 full
request。该接口不局限于
fixture，但输入仍必须属于两个 Codec 的 admitted request grammar；它不推断 claim 或 correction。
Correction token count 只接受 caller-injected、locally pinned 且不加 special token 的 deterministic
counter；缺失时稳定阻断为 `PINNED_TOKENIZER_UNAVAILABLE`，禁止下载、替代 tokenizer 或人工填写
count。publication 同时绑定 frozen G1.1 model/config manifest 中 Qwen/MAI 两个 tokenizer record 的
canonical hashes 和 human-diff renderer dependency；caller 必须先逐项验证本地 artifact。输出闭合 schema 是
[`schemas/g1_5/history_codec_preview.schema.json`](schemas/g1_5/history_codec_preview.schema.json)。
后续 CPU gate 应只读绑定
[`g1_5/cpu_publication_manifest.v1.json`](g1_5/cpu_publication_manifest.v1.json) 的最终 file
SHA-256；manifest 内分别绑定两个 selected Codec、capability、fixture、checkpoint receipt，并共同
绑定 preview implementation/output schema、冻结 G1.2 History-IR schema/renderer 与 explicit
no-tokenizer Unicode/UTF-8 coordinate contract。
它不是 live capability 或 formal G1 publication。

## 当前活动阶段：ALE-324 / G1.6（CPU/manual annotation workspace checkpoint）

[`G1_6_DECISION_LOG.md`](G1_6_DECISION_LOG.md) D-029 与
[`G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md`](G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md)
授权把所有需要真人判断的 gold curation 选择放进一个私有、loopback-only 网页：ACTION_GOLD
只看 exact task/current GUI；TRANSFORMATION 只看 exact source history/pre-cutoff evidence 并由
人选择 focal/oracle/sham/correction/delimiter repair；CONSISTENCY_AUDIT 只在前两通道解决后做
描述性判断；material disagreement 必须由第三个 channel-bound identity 独立裁决。

工作台机械绑定 active G1.3 v1.1 publication、G1.5 CPU publication/preview、两个本地 pinned
tokenizer records、role-projected browser packet 和 append-only event chain。网页不接收 raw path、
完整 capsule、captured raw response、post/future/outcome 或另一通道 proposal；自然 normalized
action/parse outcome 只在前两 formal channels 已解决后的 CONSISTENCY_AUDIT 可见，在
ACTION_GOLD/TRANSFORMATION 始终不可见。preview 仅返回 assignment-scoped tokens、rendered
history、target-only diff 与 reversible mapping。
owner 启动步骤、9-role registry 形状和 completion boundary 见
[`G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md`](G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md)。

当前精确状态是 `IN_PROGRESS_HUMAN_CURATION_REQUIRED`：代码/workspace checkpoint 不是
formal gold publication。190 个单元仍需真实 PRIMARY/SECONDARY review 与必要 adjudication；
formal action bundle、Transformation Plan、admission/blinded catalog/seal 还必须在独立 exporter
门禁中完成。所有 execution/provider/treatment/GPU/replay/action flags 保持 false。

## 强制入口与补充导航

先读取 repo root 的 AGENTS.md 与本目录的 [AGENTS.md](AGENTS.md)，并以它们为工程授权。
本目录 AGENTS 当前要求在任何代码修改前完整、依序阅读：

1. 本 README；
2. [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)；
3. [DECISION_LOG.md](DECISION_LOG.md)；
4. [G1_4_DECISION_LOG.md](G1_4_DECISION_LOG.md)；
5. [G1_5_DECISION_LOG.md](G1_5_DECISION_LOG.md)；
6. [G1_6_DECISION_LOG.md](G1_6_DECISION_LOG.md)；
7. [COLLECTOR_DESIGN.md](COLLECTOR_DESIGN.md)；
8. [EVENT_CONTRACT_V1.md](EVENT_CONTRACT_V1.md)；
9. [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md)；
10. [TEST_AND_ACCEPTANCE.md](TEST_AND_ACCEPTANCE.md)；
11. [SERVER_AGENT_INSTRUCTIONS.md](SERVER_AGENT_INSTRUCTIONS.md)；
12. [STATUS.md](STATUS.md)；
13. [G1_CAUSAL_REPLAY_PROTOCOL_V1.md](G1_CAUSAL_REPLAY_PROTOCOL_V1.md)；
14. [G1_LOCKED_ANALYSIS_PLAN_V1.md](G1_LOCKED_ANALYSIS_PLAN_V1.md)；
15. [G1_PORTABLE_SENTINEL_CONTRACT_V1.md](G1_PORTABLE_SENTINEL_CONTRACT_V1.md)；
16. [G1_REPLAY_CAPSULE_CONTRACT_V1.md](G1_REPLAY_CAPSULE_CONTRACT_V1.md)；
17. [G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md](G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md)；
18. [G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md](G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md)；
19. [G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md](G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md)；
20. [G1_5_HISTORY_CODEC_CONTRACT_V1.md](G1_5_HISTORY_CODEC_CONTRACT_V1.md)；
21. [G1_5_HISTORY_CODEC_CAPABILITIES_V1.md](G1_5_HISTORY_CODEC_CAPABILITIES_V1.md)；
22. [G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md](G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md)；
23. [G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md](G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md)；
24. [G1_SENTINEL_MVP_MIGRATION.md](G1_SENTINEL_MVP_MIGRATION.md)；
25. [g1/registry.lock.v1.json](g1/registry.lock.v1.json)；
26. [schemas/g1_3/replay_capsule.v1_1.schema.json](schemas/g1_3/replay_capsule.v1_1.schema.json)；
27. [schemas/g1_3/capsule_manifest.v1_1.schema.json](schemas/g1_3/capsule_manifest.v1_1.schema.json)；
28. [schemas/g1_3/capsule_integrity.v1_1.schema.json](schemas/g1_3/capsule_integrity.v1_1.schema.json)；
29. [schemas/g1_3/field_visibility.schema.json](schemas/g1_3/field_visibility.schema.json)；
30. [schemas/g1_3/capsule_exclusion.schema.json](schemas/g1_3/capsule_exclusion.schema.json)。

三个无 `_v1_1` 后缀的 capsule/manifest/integrity schema 是 byte-frozen 历史 v1，
不得作为当前 formal G1 contract 使用。

以下是补充研究结果导航，不替代上述强制顺序：

- [OFFLINE_EVALUATION_DESIGN.md](OFFLINE_EVALUATION_DESIGN.md)；
- [六模型正式报告](../MobileWorld/docs/misleading_history_audit_report.md)；
- [schemas/g1/](schemas/g1/) 中的 G1.1 machine contracts；
- [schemas/g1_2/](schemas/g1_2/) 中的 G1.2 machine contracts；
- [schemas/g1_4/](schemas/g1_4/) 中的 9 个 CPU/fake runner schemas 与 6 个 additive inert-preparation schemas。
- [schemas/g1_5/](schemas/g1_5/) 中的 captured-shape fixture、CPU checkpoint/publication、
  five-arm preview 与 host-coordinate binding schemas。
- [schemas/g1_6/](schemas/g1_6/) 中的 workspace、event、review proposal、role-projected packet
  与 assignment-scoped browser preview schemas。

其中 Collector 文档保留其设计时状态与验收原则；完成记录以 STATUS.md 为准，工程授权
以 AGENTS.md、DECISION_LOG.md、G1_4_DECISION_LOG.md、G1_5_DECISION_LOG.md 与
G1_6_DECISION_LOG.md 为准。本 README
不能覆盖它们。

## 目录职责

~~~text
mobileworld_audit_handoff/
├── AGENTS.md, SERVER_AGENT_INSTRUCTIONS.md
│   └── agent 工作范围与服务器操作规则
├── PROJECT_CONTEXT.md, DECISION_LOG.md, G1_4_DECISION_LOG.md,
│   G1_5_DECISION_LOG.md, G1_6_DECISION_LOG.md, STATUS.md
│   └── 研究语义、locked decisions 与 append-only execution record
├── COLLECTOR_DESIGN.md, EVENT_CONTRACT_V1.md
├── IMPLEMENTATION_GUIDE.md, TEST_AND_ACCEPTANCE.md
│   └── Collector v1 设计、raw contract、实现和验收
├── MOBILEWORLD_CODE_AUDIT.md, OFFLINE_EVALUATION_DESIGN.md
│   └── history representation 与 derived audit 设计
├── G1_CAUSAL_REPLAY_PROTOCOL_V1.md
├── G1_LOCKED_ANALYSIS_PLAN_V1.md
├── G1_PORTABLE_SENTINEL_CONTRACT_V1.md
├── G1_SENTINEL_MVP_MIGRATION.md
├── G1_REPLAY_CAPSULE_CONTRACT_V1.md
├── G1_REPLAY_CAPSULE_CONTRACT_V1_AMENDMENT_1.md
├── G1_EXACT_REQUEST_REPLAY_RUNNER_CONTRACT_V1.md
├── G1_EXACT_REQUEST_REPLAY_LIVE_PREPARATION_CONTRACT_V1.md
├── G1_5_HISTORY_CODEC_CONTRACT_V1.md
├── G1_5_HISTORY_CODEC_CAPABILITIES_V1.md
├── G1_5_HISTORY_CODEC_TEST_COVERAGE_V1.md
├── G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md
├── G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md
├── g1/
│   └── frozen registry inputs, model/config manifest, publication lock
├── schemas/g1/
│   └── versioned G1.1 contracts
├── schemas/g1_2/
│   └── versioned G1.2 portable-contract schemas
├── schemas/g1_3/
│   └── five historical v1 contracts plus three active v1.1 corrected schemas
├── schemas/g1_4/
│   └── nine CPU/fake runner schemas plus six additive inert-preparation schemas
├── schemas/g1_5/
│   └── captured-shape fixture, provider-free CPU checkpoint/publication/preview, and coordinate schemas
├── schemas/g1_6/
│   └── annotation workspace/event/proposal, role-projected packet, and browser-preview schemas
├── g1_5/
│   └── content-addressed CPU manifest, two conformance receipts, preview binding, and no-tokenizer binding
└── examples/
    └── label-free raw event example
~~~

## Monorepo、数据与 provenance

服务器应只使用同一次 AgentSentinel clone 中的 MobileWorld/，并保留用户现有修改；
不得切换到其他 MobileWorld checkout，也不得为了对齐上游而 reset 本仓库。

~~~text
AgentSentinel/
├── MobileWorld/
└── mobileworld_audit_handoff/
~~~

真实 raw collection、derived audit、screenshots、review receipts、replay capsules 和 model
responses 都必须写在 Git 工作树之外的受限、versioned data root。Git 只保存代码、schema、
协议、报告、manifest/hash 和非秘密引用。Raw evidence append-only；任何 derived 输出不得
回写或“修复”既有 raw bytes。
