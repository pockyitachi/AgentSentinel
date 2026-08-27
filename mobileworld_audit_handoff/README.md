# AgentSentinel MobileWorld Research Handoff

本目录保存 AgentSentinel 在 MobileWorld 上的权威研究边界、原始数据契约、实现决策、
验收记录和 G1 因果可行性协议。它不再只是“尚待实现的 collector 设计包”：
Collector 与 Epic 1 六模型调查已经完成，当前工作已经进入 G1。

## 当前状态

截至 2026-08-27：

| Workstream | 状态 | 说明 |
| --- | --- | --- |
| Runtime Audit Collector | 已实现并用于正式研究 | 默认关闭、passive、fail-open、event-sourced、append-only、label-free；保存应用层实际 SDK 参数和完整 transition |
| Epic 1：Motivation Investigation | **已完成** | 六模型各 117 tasks，共 702 个 model-task cases；完成 exact history reconstruction、outcome-blind MHR 与 local-harm 审核和 outcome-aware failure-link 审核 |
| ALE-319 / G1.1 | **已完成** | 冻结 CPU-only causal-replay protocol、schemas、pre-gold registry、controls、model/config manifest 与 locked analysis plan；没有生成 treatment response |
| ALE-320 / G1.2 | **已完成** | 已冻结可移植 History IR/Core、codec/provider interface、protocol validator、sidecar schemas 与六 family fixture conformance |
| ALE-321 / G1.3 | **已完成** | CPU-only 正式发布 190 个 immutable capsules、0 exclusions（152 strict + 38 selected clean）；另 38 reserve 只进 census；未调用模型/provider/GPU/GUI/replay |

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

Collector raw layer 保持不可变、passive 和 label-free。所有 G1 label、plan、rendered
request、response 与统计都属于 repo 外的 versioned derived/replay layer。

D-023 与当前 AGENTS/STATUS 只授权并记录上述 CPU-only G1.3 capsule 物化与发布；
ALE-322 / G1.4 及以后仍未启动、未获授权，任何 model/provider/GPU/GUI/action/replay 仍需
新的 story 与 owner 明确授权。

## 强制入口与补充导航

先读取 repo root 的 AGENTS.md 与本目录的 [AGENTS.md](AGENTS.md)，并以它们为工程授权。
本目录 AGENTS 当前要求在任何代码修改前完整、依序阅读：

1. 本 README；
2. [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)；
3. [DECISION_LOG.md](DECISION_LOG.md)；
4. [COLLECTOR_DESIGN.md](COLLECTOR_DESIGN.md)；
5. [EVENT_CONTRACT_V1.md](EVENT_CONTRACT_V1.md)；
6. [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md)；
7. [TEST_AND_ACCEPTANCE.md](TEST_AND_ACCEPTANCE.md)；
8. [SERVER_AGENT_INSTRUCTIONS.md](SERVER_AGENT_INSTRUCTIONS.md)；
9. [STATUS.md](STATUS.md)；
10. [G1_CAUSAL_REPLAY_PROTOCOL_V1.md](G1_CAUSAL_REPLAY_PROTOCOL_V1.md)；
11. [G1_LOCKED_ANALYSIS_PLAN_V1.md](G1_LOCKED_ANALYSIS_PLAN_V1.md)；
12. [G1_PORTABLE_SENTINEL_CONTRACT_V1.md](G1_PORTABLE_SENTINEL_CONTRACT_V1.md)；
13. [G1_REPLAY_CAPSULE_CONTRACT_V1.md](G1_REPLAY_CAPSULE_CONTRACT_V1.md)；
14. [G1_SENTINEL_MVP_MIGRATION.md](G1_SENTINEL_MVP_MIGRATION.md)；
15. [g1/registry.lock.v1.json](g1/registry.lock.v1.json)；
16. [schemas/g1_3/replay_capsule.schema.json](schemas/g1_3/replay_capsule.schema.json)；
17. [schemas/g1_3/field_visibility.schema.json](schemas/g1_3/field_visibility.schema.json)；
18. [schemas/g1_3/capsule_exclusion.schema.json](schemas/g1_3/capsule_exclusion.schema.json)；
19. [schemas/g1_3/capsule_manifest.schema.json](schemas/g1_3/capsule_manifest.schema.json)；
20. [schemas/g1_3/capsule_integrity.schema.json](schemas/g1_3/capsule_integrity.schema.json)。

以下是补充研究结果导航，不替代上述强制顺序：

- [OFFLINE_EVALUATION_DESIGN.md](OFFLINE_EVALUATION_DESIGN.md)；
- [六模型正式报告](../MobileWorld/docs/misleading_history_audit_report.md)；
- [schemas/g1/](schemas/g1/) 中的 G1.1 machine contracts；
- [schemas/g1_2/](schemas/g1_2/) 中的 G1.2 machine contracts。

其中 Collector 文档保留其设计时状态与验收原则；完成记录以 STATUS.md 为准，工程授权
以 AGENTS.md 与 DECISION_LOG.md 为准。本 README 不能覆盖它们。

## 目录职责

~~~text
mobileworld_audit_handoff/
├── AGENTS.md, SERVER_AGENT_INSTRUCTIONS.md
│   └── agent 工作范围与服务器操作规则
├── PROJECT_CONTEXT.md, DECISION_LOG.md, STATUS.md
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
├── g1/
│   └── frozen registry inputs, model/config manifest, publication lock
├── schemas/g1/
│   └── versioned G1.1 contracts
├── schemas/g1_2/
│   └── versioned G1.2 portable-contract schemas
├── schemas/g1_3/
│   └── five versioned G1.3 capsule/publication contracts
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
