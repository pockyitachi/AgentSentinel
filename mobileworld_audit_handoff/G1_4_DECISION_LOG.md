# G1.4 Decision Log

This additive log exists outside the frozen G1.3 builder-contract closure. The historical
`DECISION_LOG.md` is byte-bound by the active G1.3 publication and therefore remains unchanged.

## D-025 — G1.4 分阶段授权：先完成 CPU-only replay harness，live/GPU proof 延后

**状态：Locked（owner 于 2026-08-27 明确授权 ALE-322 / G1.4 的 CPU-only 实现阶段）**

G1.2 和经 Amendment 1 修正的 G1.3 已完成，因此允许实现 ALE-322 中不需要
真实模型或外部运行时的 CPU-only 部分：只读加载和验证 formal v1.1
ReplayCapsule，接受但绝不生成已给定的 curated
`deployment_prediction=false` Transformation Plan，构建 exact-request preparation、
target-only diff/invariance guard、state-access admission、预注册 arm-order schedule、
idempotent append-only attempt/resume 存储、blinded-scoring export、版本化 schema/CLI，以及
确定性 in-process fake provider 与 Provider Codec 一致性测试。测试只能使用合成或
经脱敏的 test-only plan/authorization fixtures，不得冒充 G1.6 curation 或真实
scientific invocation。

Formal G1.3 capsule 中的 `execution_ready=false`、
`provider_invocation_allowed=false` 和
`treatment_response_generation_allowed=false` 仍是强制事实。G1.4 CPU harness
必须保留并机械执行这三个守卫，不得通过 wrapper、CLI flag、resume 状态、
test fixture 或 Provider Codec 翻转或绕过。确定性 fake 是无网络、无模型的
conformance double，不是 provider invocation 或 treatment response。

本授权明确不包含：任何真实/外部 model 或 provider 调用、网络请求、credential 使用、
模型权重加载或服务、GPU 使用、Qwen/MAI live endpoint proof、treatment-response
generation、GUI/tool/action 执行、backend restore、deterministic prefix replay、自然任务或
live replay、正式 run publication、G1.5+ 工作、自动语义推断或 runtime Sentinel。
即使后续获得 live/GPU 授权，ALE-322 也绝不执行返回的 GUI action。

Owner 所说“约 90% CPU 工作”是分阶段调度目标，不是验收指标或科学完成度。
ALE-322 在 CPU 实现和 fake conformance 通过后仍必须标记为
**IN_PROGRESS_LIVE_PROOF_DEFERRED**；只有 owner 审核 GPU 资源并单独授权 live 路径，
且剩余 live Provider Codec 验收证据通过后，才能判断该 story 是否完成。
