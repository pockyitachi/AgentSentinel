# AgentSentinel MobileWorld Research Handoff

本目录保存 AgentSentinel 在 MobileWorld 上的权威研究边界、原始数据契约、实现决策、
验收记录和 G1 因果可行性协议。它不再只是“尚待实现的 collector 设计包”：
Collector 与 Epic 1 六模型调查已经完成，当前工作已经进入 G1。

## 当前状态

截至 2026-08-26：

| Workstream | 状态 | 说明 |
| --- | --- | --- |
| Runtime Audit Collector | 已实现并用于正式研究 | 默认关闭、passive、fail-open、event-sourced、append-only、label-free；保存应用层实际 SDK 参数和完整 transition |
| Epic 1：Motivation Investigation | **已完成** | 六模型各 117 tasks，共 702 个 model-task cases；完成 exact history reconstruction、outcome-blind MHR 与 local-harm 审核和 outcome-aware failure-link 审核 |
| ALE-319 / G1.1 | **已完成** | 冻结 CPU-only causal-replay protocol、schemas、pre-gold registry、controls、model/config manifest 与 locked analysis plan；没有生成 treatment response |
| ALE-320 / G1.2 | **下一项；在单独变更中准备** | 计划定义可移植的 History IR/Core、history-family codec、provider codec interface、protocol validator 和 derived sidecar contract |

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

## 下一阶段方向：ALE-320 / G1.2

G1 的下一目标是先验证 history-only intervention 的因果可行性，不是直接实现自动
Sentinel。计划中的 G1.2 聚焦可移植、CPU-only、evaluation-time transformation
contract：

- model-agnostic canonical History IR 与 Sentinel Core；
- 按 representation family 隔离的 extraction/render codec；
- provider codec interface 与 provider-call 前 protocol validator；
- raw request、Transformation Plan、rendered request、diff 与 provenance 的 derived sidecar；
- 对 curated transformation plan 做确定性应用。

预期边界不包含 claim truth inference、provider/model invocation、GPU、GUI action、
live prompt interception 或自动 runtime Sentinel，也不把 fixture conformance 声称为六个
production-ready adapters。

Collector raw layer 保持不可变、passive 和 label-free。所有 G1 label、plan、rendered
request、response 与统计都属于 repo 外的 versioned derived/replay layer。

本 README 分支只包含已合并的 G1.1 authority；G1.2 的 phase decision、AGENTS/STATUS
更新和 contract 正在另一项变更中准备。因此本节只记录 project direction，不授权实现。
在相应 authoritative files 合并前，必须遵守当前 checkout 中的 AGENTS.md、
DECISION_LOG.md 和 STATUS.md。

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
8. [SERVER_AGENT_INSTRUCTIONS.md](SERVER_AGENT_INSTRUCTIONS.md)。

Repo-root AGENTS 还要求完整读取 [STATUS.md](STATUS.md)。以下是研究结果与 G1 的补充
导航，不替代上述强制顺序：

- [OFFLINE_EVALUATION_DESIGN.md](OFFLINE_EVALUATION_DESIGN.md)；
- [六模型正式报告](../MobileWorld/docs/misleading_history_audit_report.md)；
- [G1_CAUSAL_REPLAY_PROTOCOL_V1.md](G1_CAUSAL_REPLAY_PROTOCOL_V1.md)；
- [G1_LOCKED_ANALYSIS_PLAN_V1.md](G1_LOCKED_ANALYSIS_PLAN_V1.md)；
- [g1/registry.lock.v1.json](g1/registry.lock.v1.json) 与 [schemas/g1/](schemas/g1/)。

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
├── g1/
│   └── frozen registry inputs, model/config manifest, publication lock
├── schemas/g1/
│   └── versioned G1.1 contracts
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
