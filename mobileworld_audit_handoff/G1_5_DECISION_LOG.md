# G1.5 Decision Log

本 additive log 的 D-028 最初位于 G1.1–G1.4 冻结 contract/source-bound 闭包之外；截至 D-028
落盘时，历史 `DECISION_LOG.md`、`G1_4_DECISION_LOG.md` 及已验收 G1.1–G1.4 文件保持
byte-unchanged。2026-08-31 的 D-035 后续以 additive engineering-close amendment 更新
`G1_4_DECISION_LOG.md` 与治理状态，但不改写冻结的 G1.1–G1.3 formal contracts 或 artifacts。

## D-028 — G1.5 分离 CPU History Codec checkpoint 与统一 GPU live-smoke batch

**状态：Locked（owner 于 2026-08-28 明确授权 ALE-323 / G1.5 的 CPU-only 阶段，并要求把
GPU 工作记录后汇入统一 GPU batch）**

在 ALE-320 / G1.2 portable core、ALE-321 / G1.3 v1.1 capsule publication 和
ALE-322 / G1.4 CPU runner checkpoint 已验收的前提下，允许只在 CPU 上实现 Qwen
flat-progress 与 MAI raw-replay 两个 History Codec。本授权覆盖：exact application-request
结构提取、外部注入且 immutable 的 curated span binding 校验、History IR 映射、五个 G1 arm
的纯渲染、target-only diff、可逆 source mapping、secret-free captured-shape fixtures、golden
diff、schema、共同 conformance tests、G1.4 invariance/preflight 的 fail-closed 集成，以及
additive contract/capability/fallback 文档。

CPU evidence 可发布为 repo 内 secret-free、content-addressed manifest/receipt 集合，供后续
repo-external gate receipt 只读绑定。该 publication 必须分别冻结两个 selected Codec 的
implementation/capability/source-fixture/conformance-receipt hashes，并共同冻结 accepted G1.2
History-IR schema、renderer 和 explicit no-tokenizer Unicode/UTF-8 coordinate binding；它本身不
是 formal G1 data、live proof、provider authorization 或 G1.6 seal。

2026-08-28 的 CPU-only 补充要求纳入同一 D-028：publication 还必须 hash-bind 一个纯只读
strict-five-arm / clean-Original+Sham preview API 及其输出 schema。该 API 只能把 exact G1.3 source records 与显式人工选择的
focal/oracle/sham/delimiter spans 转成 request-bound 坐标，机械计算人写 correction alternatives
的 pinned-tokenizer token counts/tie-break，并调用冻结 G1.2 renderer 产生 target-only diff 与可逆
mapping，同时显式投影 correction insertion anchors 与 summed-focal sham token match。浏览器输出不
新增 full-request/system/provider/image-payload 投影。G1.5 不加载或下载 tokenizer；token counter 必须由 caller 从本地 pinned artifact 注入，
special tokens 禁用，缺失时稳定返回 `PINNED_TOKENIZER_UNAVAILABLE`，不得估算、替代或信任人工
填写的 count。该补充不授权 G1.6 annotation、server 启动、provider、network、GPU 或 replay。

Codec 只解释 host-native syntax，不判断 claim 真伪、危害或该选择哪种 treatment。目标必须由
caller 提供 request-hash-bound 的精确 path、char/UTF-8 坐标、原文和 digest；缺失、漂移、重叠、
歧义或越过 protocol/external-result 边界时稳定阻断。两个 Codec 可声明
`scope=LIVE` 和五 arm 的 representation capability，但 CPU checkpoint 必须保持
`live_ready=false`。这表示 host syntax 的离线实现已存在，不表示 live transport、正式 replay
或 provider authorization 已通过。

本阶段必须复用已冻结 G1.2 的 IR/plan/render/pre-send contract 与 G1.4 的 invariance/runner
边界；Sentinel Core 不得出现 Qwen/MAI model ID、checkpoint 或 prompt-layout 分支。Formal G1.3
v1.1 capsule 继续只读且三项守卫必须保持 exact false：`execution_ready`、
`provider_invocation_allowed`、`treatment_response_generation_allowed`。G1.4 的
`OpenAICompatibleProviderCodec.send` 与 `execute_live_arm` 继续机械 fail-only。Unsupported arm
或 operation 不得作为 treatment 静默退化为 Original。

本授权明确不包含：provider/client/client factory、credential、endpoint、DNS/network/socket、
subprocess/service/Docker/tmux、GPU probe/lease/use、模型权重加载或服务、health/seed canary、
provider send、live smoke、treatment response、formal replay、backend restore、deterministic
prefix、GUI/tool/action、claim validity inference、intervention curation 或 G1.6+。Fixtures 只能是
从真实 captured request 形状得到的 structure-preserving、secret-free surrogate；不得把 repo 外
formal capsule/request bytes 复制进 Git，也不得冒充 G1.6 adjudicated target 或正式 G1 数据。

CPU checkpoint 的精确状态为
`CPU_CHECKPOINT_IMPLEMENTED_LIVE_SMOKE_DEFERRED`。ALE-323 在下面的 live-smoke matrix 完成前
不得标记 complete；当前 queue 状态为 `GPU_LIVE_BACKLOG_RECORDED_NOT_AUTHORIZED`。

统一 GPU batch 中 G1.5 的最小验收矩阵冻结为 **2 codecs × 5 arms × 1 logical invocation =
10 logical provider invocations**：

| Codec | ORIGINAL | MASK | MASK_CORRECTION | ORACLE_CLEAN | SHAM_BENIGN_EDIT |
| --- | ---: | ---: | ---: | ---: | ---: |
| Qwen flat-progress | 1 | 1 | 1 | 1 | 1 |
| MAI raw-replay | 1 | 1 | 1 | 1 | 1 |

该矩阵只能使用 versioned、secret-free、non-case smoke packet；不是 190-unit formal capsule
replay。每次 invocation 必须保留最终 request/diff/mapping、provider envelope、usage/error、parser
classification 与相同 block seed，SDK hidden retry 必须为 0；response 要么 provider-accepted 且
host-parseable，要么产生明确的 provider/parser failure classification。任何返回 action 都只能作为
inert data 保存，绝不执行、回灌或改变 backend。运行前还必须具备新的 versioned live-execution
authority、owner 指定的 GPU/lease、通过的 G1.4 serving/seed/provider/parser/isolation seals、适用的
G1.6/G1.7 downstream seals，以及显式启用且 hash-bound 的 live Codec capability。任一前置项缺失
都必须在 client、model load 和 GPU 使用前阻断。矩阵调用数、输入或验收规则的任何扩展都需要
新的 owner 决策，不得在看到结果后补定。

## D-034 — Owner-authorized direct GPU 0 smoke boundary

**Status:** `LOCKED_NONFORMAL_DIRECT_SMOKE_ONLY`

Owner 于 2026-08-30 曾授权一次 non-formal 直接 GPU 0 smoke：固定
`CUDA_VISIBLE_DEVICES=0`，仅绑定 `127.0.0.1:18007`，按 Qwen 后 MAI
的顺序执行精确 22 个 secret-free synthetic calls；G1.5 的调用不得
增加该总数或引入额外重试。只允许清理本次 smoke 自己创建的
child/session；不得读取任何外来进程的 `/proc` 私有细节，不得向任何
外部进程发送 signal 或采取动作，不得执行任何返回 action。只读 GPU/
进程基线检查（包括收尾 `nvidia-smi`）仍允许。任一失败都立即结束且不得重试。旧 D-034 authority/shim/
formal-evidence 链已废弃，不得复用或作为运行 gate。

**2026-08-31 outcome/amendment:** GPU 0 attempt 因当前 vLLM 不支持
`--swap-space`，在模型加载前以 0/22 次调用安全失败；该 attempt 已结束且
不得重试。Owner 现仅授权一次 GPU 4 替代 attempt：固定
`CUDA_VISIBLE_DEVICES=4`，仍仅绑定 `127.0.0.1:18007`，按 Qwen 后 MAI
的顺序执行精确 22 个 secret-free synthetic calls；G1.5 的调用不得增加该
总数或引入额外重试。只能清理本 attempt 自己创建的 child/session；
不得向 `taoz` 或任何外部进程发送 signal、修改、停止或采取任何动作。
任一失败都立即结束且不得重试。旧 authority/shim/formal-evidence 链仍禁止复用。

**2026-08-31 GPU 4 outcome/next-fix boundary:** 该 attempt 已进入 Qwen 模型
加载/PROFILE，随后因 bundled Triton `ptxas` 的 mode 为 `0644` 而触发
`EACCES`，以 0/22 次调用安全失败。MAI 未启动；`taoz` PID 217927、其
基线/显存与 loopback port 均保持或恢复至基线。该 attempt 已结束且
不得重试。下一步只授权修改 smoke child-process environment，使用 system
CUDA tool paths；不得 `chmod` 或以其他方式修改共享 venv。

## D-035 — G1.5 compatibility coverage only

最终 GPU4 run 为 D-028 的十个 arm 各提供了一次 non-formal compatibility observation，且属于
Qwen→MAI 精确 22-call engineering smoke 的一部分。它没有保留 D-028 formal close 所需的全部
request/diff/mapping、Provider Codec、usage/error/attempt、authority 与 downstream seal，也不
改变既有 CPU publication 的 `live_ready=false`。因此 ALE-323 保持未完成；不得把 G1.4 的
`NONFORMAL_LIVE_SMOKE_PASSED` 状态传播为 G1.5 completion。任何后续 live/formal 工作均未获授权。

## D-036 — G1.5 engineering close 与 formal live readiness 分离

**状态：Locked（owner 于 2026-08-31 要求修订 G1.5 验收口径）**

Owner 接受
`G1_5_NONFORMAL_COMPATIBILITY_ENGINEERING_CLOSE_AMENDMENT_V1.md` 的追加式、有界收尾：

- `engineering_close_status=CPU_CODEC_IMPLEMENTATION_COMPLETE_NONFORMAL_COMPATIBILITY_PASSED`；
- `formal_live_ready_status=DEFERRED_TO_G1_7_NOT_AUTHORIZED`。

D-028 的 CPU contract、Codec bytes、capability、publication、schema、receipt 与 formal 十调用
矩阵均不改写。ALE-323 的 engineering close 接受已完成的 Qwen flat-progress 与 MAI raw-replay
Codec、五 arm 纯渲染、target-only diff、可逆 mapping、preview、content-bound CPU publication、
fail-closed runner integration 和 28 项 Codec regressions。D-035 的十个 arm-shaped calls 只作为
production prompt/parser 的 non-formal syntactic compatibility coverage；它们未调用正式
History Codec -> Provider Codec 路径，绝不计入 D-028 formal matrix。

D-035 sealed manifest 中历史字段 `claims.g15_complete=false` 保持 immutable；它在 D-036 前的
D-028/formal-completion taxonomy 下仍正确。D-036 不回写或重解释该 evidence，只新增一个后续、
独立的 bounded engineering-close axis。

ALE-323 因此可按修订后的 engineering scope 关闭。两个 Codec 继续保持
`live_ready=false`。正式十调用矩阵、formal Provider Codec、完整 request/diff/mapping/
provider/usage/latency/error/attempt/retry receipts、SDK hidden-retry proof、serving equivalence、
`backend_dependency=NONE`、fresh session/KV isolation 及 live-admission/run-ready/execution/formal-
replay seals 全部转交 ALE-325 / G1.7。G1.7 必须以独立 hash-bound seal 覆盖 readiness，不得修改
v1 capability bytes；其未来失败也不重开 ALE-323，除非 Codec 语义 bytes 发生版本化变更。

本决策不授权任何新的 model/provider/network/GPU/replay/treatment/backend/GUI/tool/action。
任何 G1.7 live 工作仍需新的 owner 明确授权。
