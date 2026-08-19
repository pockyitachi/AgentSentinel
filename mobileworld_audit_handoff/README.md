# MobileWorld Pre-step Audit Collector — Server Handoff

本目录是 AgentSentinel monorepo 中的设计与续作包。服务器端只需 clone 整个 AgentSentinel 仓库，不需要读取原始聊天记录，也不应依赖服务器上其他 MobileWorld checkout，即可理解当前研究、实现第一阶段 collector，并生成后续可重复评测的数据。

## 当前唯一工程目标

在官方 MobileWorld 中实现一个可关闭、零干预、event-sourced、lossless、label-free 的 runtime audit collector，完整保存：

1. 每次模型调用真正收到的最终 request（包括 role、文本、tool schema、图片、模型参数和 retry/stream 信息）；
2. 每次模型调用的原始 response 与规范化 response；
3. 完整 decision/transition：`S_t → I_t → P_t → A_t → R_t → S_{t+1}`，以及 MCP/tool result、用户回复、执行异常和任务终局分数；
4. 足够的版本、ID、时间和 artifact provenance，使离线评测可以反复更换错误定义而无需重跑任务。

Collector 只收集事实，不判断 history 是否错误，不生成 rubric，不修改 prompt，也不实现 Sentinel 的 `KEEP/DROP/REPLACE`。

## 权威代码基线

- Repository: `https://github.com/Tongyi-MAI/MobileWorld.git`
- Branch: `main`
- Frozen design baseline: `0dcd0980eac64d76f498f93568a1ec0594b743c4`
- Commit date: `2026-08-04`
- 内置 registered adapters: 9

服务器开始实现前必须记录实际 checkout commit。若服务器代码不是上述 commit，不要强制 reset 或覆盖服务器已有修改；先做差异审计，再更新本包引用的行号和实现计划。

## 建议阅读顺序

1. [`AGENTS.md`](AGENTS.md) — coding agent 的强制范围与行为规则；
2. [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) — 研究动机、已有证据和当前阶段；
3. [`DECISION_LOG.md`](DECISION_LOG.md) — 已确定且不应擅自推翻的设计决策；
4. [`MOBILEWORLD_CODE_AUDIT.md`](MOBILEWORLD_CODE_AUDIT.md) — 9 个 adapter 与实际 history 注入方式；
5. [`COLLECTOR_DESIGN.md`](COLLECTOR_DESIGN.md) — collector 总体架构；
6. [`EVENT_CONTRACT_V1.md`](EVENT_CONTRACT_V1.md) — raw event 与 artifact 的 V1 契约；
7. [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md) — 服务器端逐阶段改码说明；
8. [`TEST_AND_ACCEPTANCE.md`](TEST_AND_ACCEPTANCE.md) — 测试矩阵和 Definition of Done；
9. [`OFFLINE_EVALUATION_DESIGN.md`](OFFLINE_EVALUATION_DESIGN.md) — 与收集层隔离的版本化分析；
10. [`SERVER_AGENT_INSTRUCTIONS.md`](SERVER_AGENT_INSTRUCTIONS.md) — 可直接交给服务器 coding agent 的续作任务；
11. [`STATUS.md`](STATUS.md) — 当前完成度和下一动作。
12. [`examples/sample_events.jsonl`](examples/sample_events.jsonl) — 仅用于说明 event shape 的无标签示例；
13. [`UPLOAD_CHECKLIST.md`](UPLOAD_CHECKLIST.md) — 上传与服务器 bootstrap 核对表。

## 目录内容

```text
mobileworld_audit_handoff/
├── README.md
├── AGENTS.md
├── PROJECT_CONTEXT.md
├── DECISION_LOG.md
├── MOBILEWORLD_CODE_AUDIT.md
├── COLLECTOR_DESIGN.md
├── EVENT_CONTRACT_V1.md
├── IMPLEMENTATION_GUIDE.md
├── OFFLINE_EVALUATION_DESIGN.md
├── TEST_AND_ACCEPTANCE.md
├── SERVER_AGENT_INSTRUCTIONS.md
├── STATUS.md
├── UPLOAD_CHECKLIST.md
└── examples/
    └── sample_events.jsonl
```

## 服务器端的预期摆放方式

本项目采用单一 monorepo；服务器必须使用同一次 clone 中的 `MobileWorld/`：

```text
AgentSentinel/
├── MobileWorld/
└── mobileworld_audit_handoff/
```

不要改用服务器上已有的其他 MobileWorld clone。实现代码进入本 monorepo 的 `MobileWorld/`；设计、决策和评测定义保留在本 handoff 目录。不要把运行生成的 raw audit logs 提交进 AgentSentinel Git 仓库。

## 本包完成时的状态

本包只交付设计和交接材料。Mac 环境不具备运行 MobileWorld 的条件，因此 collector 实现、单元测试、服务器 smoke run 和真实模型采集尚未执行。任何服务器端结果必须在 [`STATUS.md`](STATUS.md) 中追加记录。
