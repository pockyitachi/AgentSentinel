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

## D-026 — G1.4 live/GPU 只准备代码，不启动任何资源或调用

**状态：Locked（owner 于 2026-08-27 明确授权 inert/code-only preparation）**

GPU 当前没有可用空间。owner 明确要求先准备 G1.4 中未来 live/GPU proof 所需的代码，
等待 owner 通知后再考虑资源，并强调不得擅自启动。本条仅授权
`mobileworld.g1.exact-request-replay-live-preparation/contract-v1` 规定的静态模型配置绑定、
SDK call/paired-block/launch plan 的纯数据渲染、caller-injected response envelope 的纯投影、
注入式资源快照评估，以及相应 CPU-only schema/fixture/test/inspection 工作。Response 投影只
保留精确 SDK content 并派生 pinned host `str.strip()` parser input，不调用 provider 或 host
parser，也不构成 observed response。

该准备阶段不得调用或创建 provider client/client factory，不得打开 socket、发起网络请求、
启动 subprocess/tmux/Docker/service、探测或使用 GPU、加载或服务模型权重、运行 endpoint
health/seed canary、发送 provider request、生成 treatment response、执行 prefix/live replay、
restore backend，或执行任何 GUI/tool/action。现有
`OpenAICompatibleProviderCodec.send` 与 `execute_live_arm` 必须继续机械 fail-only；当前 CLI
不得获得可启用 transport 的 flag、environment variable 或 caller assertion。

Formal G1.3 v1.1 capsule 的 `execution_ready=false`、
`provider_invocation_allowed=false` 和
`treatment_response_generation_allowed=false` 保持不可变。所有 preparation/readiness aggregate
继续将 live transport validation、run-ready seal、provider invocation、treatment generation 和
formal replay readiness 标为 false。代码准备完成不等于 live 验收，也不改变 ALE-322 的
`IN_PROGRESS_LIVE_PROOF_DEFERRED` 状态。

本授权不包含 G1.5 live History Codec、G1.6 curation/gold/admission 或 G1.7 serving image、
seed/isolation/backend/scorer/restorer/run-ready/execution seals。未来只有在这些前置物存在、owner
另行明确授权 GPU/live 资源，并采用新的版本化 live-execution authority 后，才能把准备代码接入
真实 transport。即使未来获得该授权，ALE-322 仍绝不执行返回的 GUI action。
