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

## D-034 — 仅授权 GPU0 上 22-call 非正式、无秘密、非 case loopback smoke

**状态：Locked narrow owner authority（owner 于 2026-08-30 明确指定共享物理 GPU 0，并再次
要求不得停止、修改或干扰任何其他用户进程）**

本条只把 D-027 中 `GPU_LIVE_BACKLOG_RECORDED_NOT_AUTHORIZED` 的一个封闭工程 smoke 项改为
`AUTHORIZED_NON_FORMAL_SECRET_FREE_LOOPBACK_SMOKE_ONLY`。规范合同是
`G1_GPU_LIVE_SMOKE_CONTRACT_V1.md`；执行前必须由 owner UID、有效期和 out-of-band SHA-256
共同绑定 closed-shape `mobileworld.g1.gpu-live-smoke-authority/v1` 与 exact synthetic packet。
任何字段漂移、未知字段、secret-bearing 字段、hash/owner/time 不匹配都必须在 GPU probe、client、
socket、subprocess 或 model load 前以稳定 `GPU_SMOKE_*` code 阻断。
仅对这个 synthetic non-case smoke，D-027 中 formal replay 所需的 G1.6/G1.7 downstream seals
不作为输入门槛；D-034 不生成或翻转这些 seals，它们对未来任何 formal/case replay 仍是强制门禁。

授权资源固定为共享物理 GPU 0、UUID
`GPU-991ac45f-e9e9-1c25-590c-fb49ca752965`，并以该 UUID（不是易漂移的 ordinal 字符串）设置
`CUDA_VISIBLE_DEVICES`。API 只允许 `127.0.0.1:18007`；client 固定 MobileWorld venv 的
`openai==1.106.1`，server 固定 SkyRL venv 的 `openai==2.15.0`、`vllm==0.11.0`、
`torch==2.8.0+cu126`。本机 cache 可写这一现状仅能用于非正式 smoke：Qwen/MAI 每个 snapshot
必须在服务前后做完整树 inventory/hash 且 bytes 全等，同时明确
`formal_model_immutability_proven=false`、`toctou_free_model_binding_proven=false`。
authority 的 free-memory floor 固定为 64 GiB（`68719476736` bytes），必须在 Qwen 与 MAI 各自
启动前重新检查；低于阈值只可阻断，不得通过停止或修改其他用户进程释放显存。

调用矩阵在任何输出出现前固定为 22 次、零 SDK hidden retry、non-stream、失败不补跑：
G1.4 是 2 models × seeds `1729/2718/31415` × 2 fresh repeats = 12；G1.5 是 2 codecs ×
5 arms × seed `1729` = 10。严格先启动 Qwen，完成其 11 个 calls 后按 exact own-process guard
停服并证明进程、port、GPU allocation 释放，才可启动 MAI 的 11 个 calls。不得两模型共驻，
不得 warm-up generation、probe generation、额外 invocation 或 result-dependent call。

GPU 0 是共享资源，不要求或声称 exclusive lease。已有 foreign PID 只可进入最小 GPU process
snapshot，绝不是清理目标；为共享卡 invariance 与 PID reuse 防护，唯一允许的 foreign 身份读取是
当前 UID 与 `/proc/<pid>/stat` start time。不得 signal、renice、attach、改变 cgroup，亦不得读取
foreign `cmdline`、`exe`、`environ`、`fd`、`cwd`、`mem`、maps、stack 或其他 `/proc` 文件/链接。
只有同时匹配本 batch launch receipt 的 PID、UID、`/proc` start time、PGID、SID、
executable/command hash、model、GPU UUID 与 port 的进程才可被 stop；任一不匹配必须不发送信号
并 fail closed。port 已被占用时同样只报错，不得 kill listener 或换端口规避。

输入只能是 versioned、secret-free、synthetic、non-case packet；禁止 formal capsule、真实 task
或 190-unit replay。response/action/parser output 只作为 inert evidence 保存，绝不执行或反馈给后续
call。证据必须在 repo 外形成 exact-file-set content-addressed closure，覆盖 authority/packet、
环境、GPU/port/process snapshots、两模型 pre/post tree hash、两个 service lifecycle、22 个 call
request/response/usage/error/parser receipts、零 foreign-PID target/零 action/零 feedback/零
non-loopback/零 secret ledger 以及最终 manifest；失败证据不得改写成 PASS。

本条不翻转 formal G1.3 capsule 的 `execution_ready=false`、
`provider_invocation_allowed=false`、`treatment_response_generation_allowed=false`，不解锁 G1.4
formal send/replay、G1.6 gold/admission 或 G1.7。`OpenAICompatibleProviderCodec.send` 与
`execute_live_arm` 的 formal path 继续 fail-only；D-034 必须使用独立 smoke-only entrypoint。
external network、credential、真实外部 provider、backend restore/prefix/live replay、Docker/
emulator/MobileWorld action、generated action、treatment generation、response feedback、formal
publication 与任何他人进程操作继续禁止。任何扩大 GPU/UUID/model/endpoint/input/seed/repeat/
arm/call count/retry/process/network/evidence 范围都需要新的 owner 决策和 contract version。
