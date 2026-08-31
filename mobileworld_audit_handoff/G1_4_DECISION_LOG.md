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

## D-027 — G1.4 GPU 工作进入统一 GPU batch，先完成其他 ticket 的 CPU 工作

**状态：Locked scheduling decision（owner 于 2026-08-27 要求先记录、后续另行授权）**

owner 要求当前不启动 G1.4 的任何 GPU/live 工作。团队应先完成其他 ticket 中能够在 CPU 上
完成的工作，再把各 ticket 剩余的 GPU 事项汇总为一个独立 GPU batch，统一复核资源、顺序、
依赖和授权。该排期不选择或预约 GPU，不允许后台监控后自动抢占资源，也不把其他 ticket 的
授权扩展到 ALE-322；每个 queue item 在执行前仍需独立确认其 scope、输入、输出和 owner 授权。
新的 versioned live-execution authority 还必须在运行前冻结 live-proof 请求矩阵、调用数量和验收
向量；这些内容不能在看到结果后补定。

G1.4 在未来 GPU batch 中的待办项固定为：

1. **资源与隔离 admission**：由 owner 指定可用 H200、使用窗口和顺序；验证稳定的空闲显存、
   GPU UUID/owner/lease、端口、同机进程、隔离和清理方案。不得把单次低利用率或瞬时空闲显存
   当作 admission。容量必须满足版本化 assessment/seal；当前口头估算不能替代正式资源证据。
2. **前置 seal 检查**：在任何 client、model load 或 service start 前，确认适用的 G1.5 live
   Qwen/MAI History Codec、G1.6 curated plan/gold/admission，以及 G1.7 serving-image/config、
   model revision、seed support、backend dependency、isolation、scorer key、run-ready 和
   execution-authorization seal 均存在且相互 hash-bound。任何一项缺失都必须在 GPU 使用前阻断。
3. **两模型顺序 serving proof**：严格按冻结 model/config manifest，先后而非并驻地验证
   `qwen3vl_8b` 与 `mai_ui_8b`。两者当前都绑定 `127.0.0.1:18007`，因此前一服务必须按
   exact owner/PID/model/port guard 完整退出并证明资源释放后，才能启动后一服务；不得停止或
   修改其他用户进程，也不得静默改变 vLLM、BF16、32K context、single-sequence、seed、endpoint
   或其他冻结配置。现有 inert launch plan 不是 serving receipt，必须由真实的版本化 serving
   evidence 取代后才能通过该项。
4. **非正式 synthetic canary**：先使用非 case、非 formal-capsule 输入验证 endpoint、冻结的
   replay seeds `1729`、`2718`、`31415`、SDK hidden retries=0、non-stream policy，以及同一 paired
   block 只增加同一个 seed。Canary 失败必须在任何 treatment response 前阻断并要求版本化修正，
   不得退化为 unseeded invocation。Canary 只生成版本化 live-proof evidence，不是 treatment
   response。
5. **Live Provider Codec fidelity**：分别为 Qwen/MAI 证明真实 SDK 调用保留 model、messages、
   roles/order、system/task、tools、全部 image bytes/current screenshot、sampling settings、unknown
   kwargs 和 exact final application request；完整保存 response envelope、usage、latency、error、
   retry/attempt 与 raw bytes，且不得继承 SDK 隐藏重试。
6. **Host parser equivalence**：以 target-pre screenshot 尺寸和冻结 parser binding 验证 Qwen 与
   MAI 的 host parser 输出不变，包括各自已锁定的坐标转换语义。Parser 只产生 inert action data；
   ALE-322 永不执行、回灌或用该 action 改变 GUI/backend。
7. **Backend 与 fresh-repeat isolation**：在 live 环境重新证明 `backend_dependency=NONE`，并证明
   fresh invocation 之间没有 session、KV-cache 或其他 carry-over。若发现 external-state dependency，
   对应 case 必须阻断，直到适用 restorer 另行封存和授权。
8. **证据封存与资源释放**：写入版本化、content-addressed、可复核的 serving/seed/codec/parser/
   isolation receipts；失败也保留精确证据。结束时只按 exact guard 停止本 batch 自己启动的服务，
   验证端口、进程和 GPU allocation 已释放，不触碰其他用户任务。

完整 190-unit formal replay、treatment-response generation、正式 run publication、backend
restore/prefix replay 和任何 GUI/tool/action 不因本排期获得授权。它们只有在全部下游 contract、
seal 和新的 versioned live-execution authority 齐备并经 owner 再次明确授权后才能另行评估。

在 GPU batch 获得该新授权和验收通过前，G1.4 backlog 的精确记录状态是
`GPU_LIVE_BACKLOG_RECORDED_NOT_AUTHORIZED`，queue item 是 `QUEUED_NOT_AUTHORIZED`；ALE-322
继续保持 `IN_PROGRESS_LIVE_PROOF_DEFERRED`，现有
`OpenAICompatibleProviderCodec.send` 与 `execute_live_arm` 继续机械 fail-only。

## D-034 — Owner-authorized direct GPU 0 smoke boundary

**Status:** `LOCKED_NONFORMAL_DIRECT_SMOKE_ONLY`

Owner 于 2026-08-30 曾授权一次 non-formal 直接 GPU 0 smoke：固定
`CUDA_VISIBLE_DEVICES=0`，仅绑定 `127.0.0.1:18007`，按 Qwen 后 MAI
的顺序执行精确 22 个 secret-free synthetic calls。只允许清理本次
smoke 自己创建的 child/session；不得读取任何外来进程的 `/proc`
私有细节，不得向任何外部进程发送 signal 或采取动作，不得执行任何
返回 action。只读 GPU/进程基线检查（包括收尾 `nvidia-smi`）仍允许。任一
失败都立即结束且不得重试。旧 D-034
authority/shim/formal-evidence 链已废弃，不得复用或作为运行 gate。

**2026-08-31 outcome/amendment:** GPU 0 attempt 因当前 vLLM 不支持
`--swap-space`，在模型加载前以 0/22 次调用安全失败；该 attempt 已结束且
不得重试。Owner 现仅授权一次 GPU 4 替代 attempt：固定
`CUDA_VISIBLE_DEVICES=4`，仍仅绑定 `127.0.0.1:18007`，按 Qwen 后 MAI
的顺序执行精确 22 个 secret-free synthetic calls。只能清理本 attempt 自己
创建的 child/session；不得向 `taoz` 或任何外部进程发送 signal、修改、
停止或采取任何动作。任一失败都立即结束且不得重试。旧
authority/shim/formal-evidence 链仍禁止复用。

**2026-08-31 GPU 4 outcome/next-fix boundary:** 该 attempt 已进入 Qwen 模型
加载/PROFILE，随后因 bundled Triton `ptxas` 的 mode 为 `0644` 而触发
`EACCES`，以 0/22 次调用安全失败。MAI 未启动；`taoz` PID 217927、其
基线/显存与 loopback port 均保持或恢复至基线。该 attempt 已结束且
不得重试。下一步只授权修改 smoke child-process environment，使用 system
CUDA tool paths；不得 `chmod` 或以其他方式修改共享 venv。

## D-035 — Post-hoc non-formal G1.4 engineering close

**状态：`NONFORMAL_LIVE_SMOKE_PASSED` / formal replay
`DEFERRED_TO_G1_7_NOT_AUTHORIZED`（owner 于 2026-08-31 接受）**

Owner 在审阅最终 GPU4 结果后，接受该 22/22 compatibility smoke 作为 ALE-322 的有界工程
交付收尾。该决定是 post-hoc scope amendment，不满足 D-027 的预注册 formal proof 条件，也不
把 direct HTTP、observed vLLM 0.19.1 runtime、共享 GPU4 或事后 source/runtime binding 追认为
formal Provider Codec、冻结 serving environment、backend/session isolation、treatment 或 replay
evidence。

实现及验证面固定于 commit `86d54efce0c3f36c4a5df86c8ff146fd9b7fa25a`。外部只读
content-addressed evidence manifest SHA-256 为
`f70cee09e4870f3b0ab8dcd0d187efacd49362731c976b0872b4243600305179`；安装收据 SHA-256 为
`272f03d16f988f8e9e9cb3a36146583f7545e62daf2716b0518c2155f97a7064`。历史 D-034 链最后存在于
commit `60835447601cacbfae4c806b464b3247d555aeac`，并在 commit
`83e6cb847e62594f75ce4f6b47b3bae3337203d6` 删除；不得复活或复用。

formal Provider Codec、完整 attempt/usage/latency/error receipts、SDK hidden-retry fidelity、
fresh invocation/session/KV isolation、backend dependency、run-ready/execution seals 与正式 replay
全部移交未来 G1.7 另行考虑，本决定不授权它们。GPU/model authority 已用尽；不得再运行 GPU、
模型、replay 或 action。ALE-323/G1.5 仍未完成。
