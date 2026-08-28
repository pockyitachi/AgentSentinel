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
20. `G1_5_HISTORY_CODEC_CONTRACT_V1.md`
21. `G1_5_HISTORY_CODEC_CAPABILITIES_V1.md`
22. `G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md`
23. `G1_6_ANNOTATION_WORKSPACE_RUNBOOK.md`
24. `G1_SENTINEL_MVP_MIGRATION.md`
25. `g1/registry.lock.v1.json`
26. `schemas/g1_3/replay_capsule.v1_1.schema.json`
27. `schemas/g1_3/capsule_manifest.v1_1.schema.json`
28. `schemas/g1_3/capsule_integrity.v1_1.schema.json`
29. `schemas/g1_3/field_visibility.schema.json`
30. `schemas/g1_3/capsule_exclusion.schema.json`

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

当前活动范围是 `G1_4_DECISION_LOG.md` 中 D-025 限定的
**ALE-322 / G1.4 CPU/fake checkpoint 后续阶段**。commit
`bf099a1a00f38edc33b6c5cbb1ab5d12d53bd18c` 已实现并验证 exact-request runner、
invariance/target-only diff guard、确定性 arm scheduling、idempotent
append-only attempt/resume 存储、blinded export、schema/CLI、in-process fake provider，以及仅通过
fake SDK client 测试的可注入 OpenAI-compatible Provider Codec。Formal v1.1 capsule 仍是只读
输入，且必须保持 `execution_ready=false`、`provider_invocation_allowed=false` 和
`treatment_response_generation_allowed=false`；本阶段对 formal capsule 的路径必须在任何
外部调用前 fail closed。“约 90%”只是排期目标，ALE-322 仍处于 in progress，
live/GPU proof 延后并需新授权。

`G1_4_DECISION_LOG.md` D-026 只额外授权未来 live/GPU proof 所需的 inert/code-only
准备：静态冻结模型绑定、纯 call/block/launch 与 caller-injected response 数据记录、仅注入式
容量评估、schema 和 CPU tests。它不授权 client、network、subprocess、GPU probe/use、模型
加载、provider send、replay 或 action；所有 live entrypoint 在 owner 新授权与 downstream seal
齐备前继续机械禁用。

`G1_5_DECISION_LOG.md` D-028 现只授权 **ALE-323 / G1.5 CPU-only History Codec
checkpoint**：Qwen flat-progress 与 MAI raw-replay 的纯 request extraction/rendering、外部注入的
精确 curated-span binding、五 arm conformance、secret-free fixture/schema/golden diff，以及与
G1.4 runner 的 fail-closed interface integration。两个 Codec 均须保持 `live_ready=false`；
2 codecs × 5 arms 的 10-call live smoke 仅进入统一 GPU backlog，当前不授权执行。不得把该
checkpoint 冒充 live proof、formal replay 或 G1.6 curation。

`G1_6_DECISION_LOG.md` D-029 与
`G1_GOLD_HISTORY_INTERVENTION_CONTRACT_V1.md` 授权 **ALE-324 / G1.6
CPU-only 人工标注工作台**：从 immutable G1.3 publication 生成双盲 role-projected packets，
使用本地 hash-verified tokenizer 与 G1.5 pure preview 做 renderer/diff/reversibility 检查，
并将 human-authored review/adjudication 写入 repo-external append-only journal。程序不得替人
判断 focal/oracle/sham/correction/action gold/consistency/adjudication；formal bundle、admission、
seal、replay 与 treatment generation 在独立门禁完成前继续 fail closed。

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
- 调用任何真实/外部 model 或 provider、发起外部网络请求、使用 GPU、加载/服务模型权重、
  执行 MobileWorld/generated GUI/tool/action、backend restore、prefix 或 live replay、生成
  treatment response、自动推断
  claim validity、自动选择/生成 intervention，或开始 G1.7+；只允许无网络的确定性
  fake-provider conformance、provider-free G1.5 CPU checkpoint 与 D-029 限定的人工 G1.6
  CPU workspace；
- D-029 唯一允许的 socket 是 owner 前台启动、单进程、仅绑定 loopback、无 remote asset、
  强制 same-origin/CSRF 的 annotation site；不得通过 tunnel/proxy/remote hosting 扩大它；
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

## 完成要求

只有通过 ALE-321 的 capsule 一一对应、rehydration/hash、exact-request、target resolution、
visibility/future-leakage、稳定 exclusion 和 deterministic double-build 验收，并在 `STATUS.md`
记录 commit、命令、测试结果、外部 publication 与已知限制后，才可宣称 G1.3 完成。
G1.4 CPU checkpoint 必须单独记录未完成的 live/GPU 验收项，不得将 ALE-322 标记为完成。
G1.5 CPU checkpoint 也必须记录精确 10-call live-smoke backlog，不得将 ALE-323 标记为完成。
G1.6 workspace checkpoint 不能冒充 gold publication；只有 190 单元双盲 review、必要 adjudication、
formal export/validation/admission/seal 全部完成后才可将 ALE-324 标记为完成。
任何真实 model/provider/external-network/GPU/MobileWorld-generated-GUI/action/live-replay 仍需
另行授权；D-029 owner-started loopback annotation site/human curation clicks 是唯一例外。不得把
凭据写入代码或本目录。
