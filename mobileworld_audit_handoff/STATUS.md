# Project Status：GUI Agent Previous-Step Misleading Motivation Study

Last updated: 2026-08-26 (UTC)

## 1. Project objective

项目长期目标是研究一个面向 GUI agent 的运行时 Sentinel/middleware：在 agent做当前 GUI决策前，检查注入 prompt 的 previous-step 信息是否被后续GUI证据反驳、是否已失效、是否导致轨迹偏航，并在未来选择不注入、纠正或降低其权重。

核心例子：

```text
旧历史声称：已进入目标商品页 / 已加入购物车
当前GUI证据：实际在用户资料页 / 购物车为空
风险：该旧历史仍进入下一步prompt，模型可能以它为前提继续行动或提前结束
```

长期 Sentinel还可能根据 task在开始时生成 milestones/rubrics，并随可见证据更新 rubric state，用来判断 agent是否仍走在任务轨迹上。但这不是当前开发阶段。

## 2. Current stage and decision

当前只做 motivation validation：先确定 wrong/stale/off-track previous-step exposure是否真实、普遍、严重，是否值得支撑 proposal。

已确认的工程决策：

```text
Collection
  event-sourced
  lossless with respect to observable runtime data
  label-free
  immutable/append-only
  feature-gated

Evaluation
  fully offline
  versioned
  replaceable/re-runnable
  never rewrites raw
```

因此，第一次错误 taxonomy即使设计不合理，只需重跑离线 evaluator，不需要重新运行昂贵的 MobileWorld任务。

当前不实现 Sentinel、rubric、filter或在线LLM judge。

## 3. Canonical notation

```text
T       task instruction
S_t     当前决策前GUI observation
I_t     模型实际收到的完整request
P_t     agent.predict返回给runner的exact prediction；可由一个或多个provider response组装/转换
A_t     解析后的JSONAction
R_t     action执行的transport/tool/user evidence
S_{t+1} action后的GUI observation
H_t     I_t中实际注入的history部分
```

本交接包统一使用 `P_t` 表示模型输出，状态始终使用 `S_t`，action result使用 `R_t`；与 `PROJECT_CONTEXT.md` 一致。

## 4. Repository baseline

Mac当前官方clone位置曾为：

```text
/Users/apigo/Desktop/agent monitor/MobileWorld
```

服务器路径可以不同，以含 `src/mobile_world` 的仓库为准。

Frozen baseline：

```text
repository: Tongyi-MAI/MobileWorld
branch: main
commit: 0dcd0980eac64d76f498f93568a1ec0594b743c4
commit date: 2026-08-04
```

内置registered adapters（9）：

```text
qwen3vl
planner_executor
mai_ui_agent
general_e2e
seed_agent
gelab_agent
ui_venus_agent
gui_owl_1_5
memgui
```

动态 `.py` agent仍被框架支持。UIINS是 `planner_executor`内部grounder，不算第十个顶层agent。

## 5. Source audit findings

所有九个顶层agent都会把某种历史表示放入下一步模型输入，但不是同一实现。

| Family | Agents | Prompt history behavior |
|---|---|---|
| Raw chat replay | Seed, General E2E, MAI-UI, Planner-Executor | 旧response以assistant消息重放；旧文字通常保留，旧截图只留窗口 |
| Flat progress | Qwen3VL, UI-Venus | 旧步骤拼成Task progress/Previous Actions，通常只有当前截图 |
| Rolling summary | Gelab | 一个模型生成的rolling summary + 当前截图 |
| Hybrid collapsed | GUI-OWL 1.5 | 旧步骤折成Previous actions，recent窗口保留raw；默认通常仅当前截图 |
| Structured folding | MemGUI | folded summaries + latest interaction + UI memory + 当前截图 |

共同结构性风险：

> 历史文字、结论或summary通常比产生它们的旧GUI证据存活更久；模型可能继续看到旧判断，却看不到对应截图。

已发现的实现层风险，后续离线audit应专门检查：

- Qwen3VL可能把动作前生成的action text放入“You have done”式progress，导致intent语义升级为完成事实。
- UI-Venus默认 `history_length=0` 配合 `history[-0:]`，实际可能保留全部历史；parse-failed step也可能继续出现。
- Gelab summary与action同次、执行前生成，不是经过post-state验证的总结。
- GUI-OWL collapsed history存在action/result潜在off-by-one错配与latest result遗漏。
- MemGUI有next-screen-conditioned folding，但由actor自身管理，不等于独立evidence verdict，且folding可能破坏性丢失信息。

## 6. Existing data and preliminary observation

旧的本地QR-MW Seed baseline曾讨论：

```text
117 task directories in the historical ledger
historical headline SR: approximately 41.03%, often summarized as 48 / 117
artifact-level accounting currently available: 46 confirmed success / 69 failure / 2 no_result
broad screening: 14 of 69 failed tasks had at least one propagation signal
strict low-state-confound lower bound: 5 of 116 nonempty trajectories
```

`48/117` 只能保留为当时讨论采用的历史 headline/账本口径，不能与当前 artifact-level confirmed-success count混写。`14/69` 只应记录为 preliminary/manual pilot observation，定义尚未经过exact prompt exposure核实；不能表述为“14个失败由previous-step错误造成”。

新 collector要重新回答：

1. 被怀疑的旧文本是否真的存在于 target model request？
2. 它是 false、false-success、stale、off-track还是不可核验？
3. 只是注入噪音，还是当前输出明确使用了它？
4. 是否伴随错误action、重复action或premature termination？

## 7. Evaluation concept agreed with owner

错误历史只要实际进入 prompt，就是 noise。使用强度需要分层：

```text
NO_OBSERVED_UPTAKE
  已注入，但当前输出无可观察使用证据；仍是weak noise

BEHAVIOR_CONSISTENT
  当前行为与错误历史一致，但可能有其他解释；possible mislead

EXPLICIT_USE
  当前输出明确引用或以前提方式采用错误历史；strong mislead/strong noise
```

再独立标 downstream effect：

```text
NO_VISIBLE_HARM
UNNECESSARY_ACTION
WRONG_ACTION
REPEATED_ACTION
PREMATURE_TERMINATION
RECOVERED
UNKNOWN_EFFECT
```

详细taxonomy和metric在 `OFFLINE_EVALUATION_DESIGN.md`。

## 8. Why MobileWorld first, not AndroidControl

当前优先 MobileWorld，因为它能观察真实agent自然生成并重新注入的history，可回答：

- wrong previous-step是否自然发生；
- 是否真实进入下一步prompt；
- 是否在真实trajectory中被复用；
- 是否集中出现在失败task。

AndroidControl主要是成功demonstration，缺少真实actor reasoning和原始prompt history。它可在后期构造synthetic misleading history做外部泛化/控制实验，但不能替代当前motivation study。

## 9. Current code state

截至 2026-08-20，服务器上已在 AgentSentinel monorepo 的 `MobileWorld/` 中实现
Collector v1 的基础代码与 CPU-only deterministic fake tests：

```text
branch: codex/audit-lossless-collector-v1
starting AgentSentinel commit: 7fb9cc4c5265283b2dd2403d8160c0facae725f6
frozen MobileWorld upstream: 0dcd0980eac64d76f498f93568a1ec0594b743c4
implementation: MobileWorld/src/mobile_world/runtime/audit/
tests: MobileWorld/tests/runtime/audit/
```

已实现的基础能力包括：append-only run/task event streams、content-addressed
blob store、typed serializer/rehydration、model request/response/stream/retry 采集、UIINS
direct grounder 采集、runner transition/score/terminal 采集、GUI/MCP/user
execution evidence、credential guard、concurrency guard 和 integrity checker。全部开关
默认关闭；开关关闭时走原控制流。owner 于 2026-08-19 进一步澄清：真实
eval/runtime collector 只有 passive `fail_open_with_incomplete_marker`，不能由
collector error 中止、重试或改变agent/environment结果；完整性失败由运行后的
offline integrity checker和CPU fault-injection tests处理。

2026-08-20 已完成首个真实 MAI-UI-8B + MobileWorld 单任务 smoke：feature-off
与 Collector-on 都在 4 steps 得到 score 1.0；feature-off 没有产生 audit artifact，
Collector-on raw run 通过完整性、blob、凭据值和因果链接检查。CLI 也已用只读、
禁 optional-lock、带超时且 fail-open 的 Git status 自动记录 worktree dirty state；本次
manifest 正确记录 `git_dirty=true`，而非把当前未提交 Collector 代码误报为 unknown。

尚未完成的是 Seed、Planner+UIINS、MemGUI、其余 adapter 的真实 compatibility
smoke、schema v1 freeze 和 offline evaluator。在 raw schema freeze 前仍有两个保守
边界：`TrajLogger`/thread logger 构造失败发生在 task binding 之前；如果 audit
storage 在 bootstrap 前就完全不可写，fail-open 只能返回 in-memory degraded state，
无法在该不可写目标中留 durable marker。同步 blob 写入及 event write/flock 仍可能
增加时序开销，因此一次成功 paired smoke 不能被表述为物理零延迟证明。

## 10. Immediate roadmap

```text
1. Freeze source + deterministic fixtures
2. Implement schema/blob/recorder/serializer/integrity checker
3. Hook Base non-stream + streaming
4. Hook UIINS direct grounder
5. Hook runner transitions and score
6. Hook environment transport/MCP/user evidence
7. Validate feature-off parity, retries, images, concurrency
8. Server smoke on Seed, Planner, MemGUI
9. Nine-adapter small compatibility smoke
10. Freeze raw schema v1
11. Implement separate offline reconstruction/labeling pipeline
12. Run representative-agent motivation study
```

不要直接开始 `9 agents × all tasks`。九个agent先做2–3个短任务的logger compatibility smoke；研究采集应在schema和标注流程稳定后设计样本。

## 11. Server findings / deviations

服务器coding agent在这里追加，不覆盖旧记录：

```text
Date:
Commit:
Finding/deviation:
Evidence (file/symbol/test):
Decision:
Research-semantic impact:
Owner approval required: yes/no
```

```text
Date: 2026-08-19 UTC
Commit: working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: 开工时 monorepo clean；MobileWorld 无 collector 或 tests/runtime/audit。当前改动尚未 commit。
Evidence (file/symbol/test): git status/rev-parse/remote；UPSTREAM.md；agents/registry.py
Decision: 只在本 monorepo 的 MobileWorld/ 中实现，真实 raw root 强制位于 Git repo 外。
Research-semantic impact: 可明确分离 AgentSentinel commit 和 frozen MobileWorld upstream provenance。
Owner approval required: no
```

```text
Date: 2026-08-19 UTC
Commit: working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: 9 个 registry adapter 共用 Base OpenAI-compatible model boundary；UIINS 是唯一 agent-side direct grounder bypass。
Evidence (file/symbol/test): runtime/audit/model_io.py；agents/base.py；agents/grounding/uiins.py；tests/runtime/audit/
Decision: Base hook 覆盖 9 adapter，UIINS 单独 hook，并显式记录 Seed/其他 adapter 外层 retry correlation。
Research-semantic impact: raw data 可以区分 actor/grounder、provider retry 和 adapter retry，不需要 runtime label。
Owner approval required: no
```

```text
Date: 2026-08-19 UTC
Commit: working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: 本轮验证仅使用 CPU fake/tempdir；未进行真实 MobileWorld/model smoke。
Evidence (file/symbol/test): MobileWorld/tests/runtime/audit/；219 passed；Ruff/check/format 通过
Decision: 保留 Seed、Planner+UIINS、MemGUI 与 9-adapter compatibility smoke 为未完成，不 freeze raw schema。
Research-semantic impact: 当前只证明 collector 的 deterministic 工程行为，不证明真实 provider/environment 兼容性。
Owner approval required: no
```

```text
Date: 2026-08-19 UTC
Commit: working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: owner澄清真实runtime collector必须是纯被动、不可中断的fail-open；不应存在第二种可选择的runtime failure policy。
Evidence (file/symbol/test): runtime/audit/config.py、model_io.py、execution_io.py、runner_capture.py、recorder.py；agents/base.py 与六个 outer-retry adapter；core/runner.py；core/subcommands/eval.py；runtime/client.py；tests/runtime/audit/ 的 audit-off/audit-on 与逐故障注入对照；最终完整 CPU suite 328 passed，Ruff/check/format 与 git diff --check 通过。
Decision: runtime只保留固定的fail-open policy；bootstrap/start/finish/finalize/close的collector-only Exception/OSError均降级且不改变runner结果；正式RunRecorder只关闭JSONL event stream的per-event fsync，task/run finalization仍best-effort flush且no-throw；完整性失败改由运行后的offline integrity checker和fault-injection tests判定。
Research-semantic impact: 普通collector异常不会制造新的task retry、异常或轨迹分支；缺失证据仍通过capture_complete/missing_artifacts与离线完整性报告排除。当前blob写入及JSONL write/flock仍为同步I/O，因此不能宣称物理零延迟或对永久I/O hang作保证；真实smoke前必须把这项作为显式时序风险审查。
Owner approval required: no（owner已明确）
Resource note: 本次整改及验证未使用GPU、模型API、Docker、emulator或真实server smoke；Seed、Planner+UIINS、MemGUI和九adapter smoke仍未完成。
```

```text
Date: 2026-08-20 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: eval CLI 原先不提供 repository_dirty，导致任何真实 run 都会被保守标记 incomplete；现已仅在 audit enabled 时执行只读 Git status，并把 clean/dirty/unknown 显式传给 lifecycle。
Evidence (file/symbol/test): runtime/audit/lifecycle.py::detect_repository_dirty；core/subcommands/eval.py::_start_eval_audit；test_lifecycle.py；test_audit_cli.py；完整 CPU audit suite 338 passed，Ruff 与 git diff --check 通过。
Decision: 使用 `git --no-optional-locks status --porcelain=v1 -z --untracked-files=normal`，固定 repo cwd、`GIT_OPTIONAL_LOCKS=0`、5 秒 timeout；任意异常/nonzero/timeout 降为 unknown，feature-off 零调用且不写 Git metadata。
Research-semantic impact: raw manifest 可以区分 clean、dirty 和无法确认；provenance 检测失败仍不会阻止或改变 eval。
Owner approval required: no
```

```text
Date: 2026-08-20 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: 首个真实 MAI-UI-8B + MobileWorld Collector smoke 通过；本地 `mobile_world:reset` 镜像入口在无网络环境会因 `uv run` 尝试获取 hatchling 而无法自动启动 backend，但镜像内预装 `/app/service/.venv/bin/mobile-world` 可直接启动。Docker daemon 的 default network 没有给首次容器分配 endpoint，因此本轮用仅属于 smoke 的 bridge network 在创建时绑定 localhost 端口；未连接、检查内部状态或操作其他人的容器。
Evidence (file/symbol/test): model snapshot `Tongyi-MAI/MAI-UI-8B@e00a0097abb9cc621cac5172d8c4809f0839c94e`，vLLM 0.11.0，GPU 7，model endpoint 127.0.0.1:18007；environment image `mobile_world:reset@sha256:2038dc91b8a4288d7f66d8d843f970487903483cd81d76fd64be9c3ba658925c`；task OpenFlightModeTask；repo 外 data root `/shared/linqiang/mobileworld_smoke_data/mai_ui_8b_g7_20260820_01`；raw run `01M0EW5K68DAXX2Y8578DAGNW8`。
Decision: feature-off 与 Collector-on 使用相同业务参数（MAI-UI-8B、max-round 5、temperature 0 adapter、single task/concurrency、auto-retry 0、同一独立 emulator）。feature-off 4 steps/score 1.0 且 audit root 不存在；Collector-on 4 steps/score 1.0。完整性报告 valid=true、0 errors、0 warnings、28 events、4 model requests/responses、4 decisions/transitions、33 verified blobs、91 blob references、0 orphan；manifest capture_complete=true、missing_artifacts=[]、git_dirty=true。配置占位 API key 的 exact-value scan 覆盖 event/blob 且通过。
Research-semantic impact: 真实 MAI request 的历史 image 数为 1/2/3/3，四步 request/response 与 GUI transition 均可重建；paired run 的前三个 click 坐标完全一致，最终均为 answer 且 score 相同。step 3/4 的自然语言有差异，初始 screenshot hash 也因两次独立 reset/time 不同，因此这一次 smoke 只能证明 collector compatibility 和未观察到行为级回归，不能证明字节级确定性或严格因果 zero-intervention。
Owner approval required: no
Resource cleanup: 仅停止并删除本轮精确容器 `lq_mw_audit_g7_0`、专用网络 `lq_mw_audit_g7_net` 和本轮 vLLM exec session；结束后 GPU 7 为 0 MiB used，18007/6960/7060/7160/7260 均无 listener。raw data 与 integrity report 保留，未删除或修改其他进程/容器/emulator。
```

```text
Date: 2026-08-20 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: `gui_only` 只控制任务筛选，不能阻止任意 GUI agent 在普通任务中生成 `ask_user`；backend 原先会接受缺失/占位的 `USER_AGENT_*` 配置，直到执行该 action 才返回 HTTP 500。CLI 还会在当前目录存在 `.env` 时忽略显式 `--env-file`。
Evidence (file/symbol/test): runtime/user_agent_config.py；core/server.py::initialize_suite_family；core/api/env.py::launch_container；core/subcommands/env.py::_launch_containers；tests/runtime/test_user_agent_preflight.py；相关 CPU fake 11 passed，完整 CPU suite 349 passed，Ruff check 与 git diff --check 通过。
Decision: backend 在初始化 task registry 前对有效环境执行 secret-safe simulated-user preflight；CLI/API container launch 在任何 Docker 命令前要求并校验三项配置，且显式 `--env-file` 优先。错误只列配置键名/错误类别，不记录 key、endpoint 或 model 的值。未新增 LLM call，也未改 ask-user回答、agent prompt/action或 Collector 路径。
Research-semantic impact: 配置故障从 trajectory 中途的环境 HTTP 500 前移到正式 run 之前；合法配置下的 agent/runtime 数据路径不变。该 gate 属于运行前依赖检查，不是 Collector 对 agent run 的干预。
Owner approval required: no（owner 已明确要求修复并重新启动自有环境）
Resource note: 本轮只运行 CPU deterministic fake tests；未使用 GPU、模型 API、Docker、emulator，也未读写 raw collection data。
```

```text
Date: 2026-08-20 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: 正式 GUI-117 run 使用的自有容器未挂载 simulated-user 配置；既有 Docker container 无法通过 restart 原地增加 bind mount，因此需要重建外层 container，而不是只重启 emulator。
Evidence (file/symbol/test): 旧自有 container `lq_mw_mai8b_gui117_g7_0`（ID ae6a67d0a144）及唯一 attached network `lq_mw_mai8b_gui117_g7_net` 已在变更前只读核验；新 container ID 6f3ec287476b，image sha256:2038dc91b8a4288d7f66d8d843f970487903483cd81d76fd64be9c3ba658925c，localhost ports 6970/7070/7170/7270，secret bind `/shared/linqiang/mobileworld_runtime_secrets/mai_ui_user_agent.env` -> `/app/service/.env` 为 read-only，源文件 owner linqiang/mode 600；container health healthy，ADB `emulator-5554 device`，仅一个 backend 监听 6800；MAI-UI vLLM 127.0.0.1:18007 health 仍通过。
Decision: 经 owner 明确授权，仅停止并自动移除上述精确旧 container；保留专用 network、GPU7 MAI-UI model server 和 repo 外 raw 数据。用同名、固定 image ID、owner/purpose labels 和只读最小 secret mount 创建 fresh container；不复用旧 emulator 临时状态。
Research-semantic impact: 后续 rerun 将从 fresh emulator 开始并具备 ask_user simulated-user 配置；本次只验证本机 backend/model health 和配置存在性，没有发起 task、模型 inference 或商业 API 请求，因此尚未证明 provider 网络/API 调用成功。
Owner approval required: no（owner 已明确要求修复机制并重新开 emulator）
Resource cleanup/recovery: 旧 `--rm` container 及其 emulator 临时状态已删除、不可从 Docker 恢复；raw run 与 trajectory 未修改。新 container、network 与 vLLM 当前保留运行。
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: simulated-user 配置本身有效，但该主机的 Docker bridge 容器没有外网转发；容器内真实 ask_user 在鉴权前即以 DNS/connection error 失败。
Evidence (file/symbol/test): 相同的最小 USER_AGENT_* 配置在宿主机调用 `tasks.utils.user_agent_answer_question` 成功并返回预期 2-character `OK`；容器内同一调用报 `Temporary failure in name resolution`，绕过 DNS 直连 OpenAI IP 仍 timeout；宿主机无 key 请求 OpenAI 返回预期 HTTP 401。`/etc/docker/daemon.json` 明确设置 `bridge=none`、`ip-forward=false`、`iptables=false`，而专用 network 为非-internal 172.19.0.0/16。
Decision: 不修改全局 Docker daemon、不重启 Docker、不通过 host network 暴露 MobileWorld 固定端口，也不绕过 sudo 修改共享主机 firewall。容器 restart/recreate/增加 DNS 均不能修复 NAT 缺失；若管理员为本容器/专用 bridge 增加 scoped egress，当前 container 无需重启。无管理员变更时，候选替代方案是单独评审一个仅供本容器使用的 host-network simulated-user sidecar + Unix socket transport。
Research-semantic impact: key、endpoint、model 已通过一次真实商业 API smoke；当前 formal 17-task rerun 仍不能开始，因为容器内 ask_user 会再次产生环境 HTTP 500。该阻塞与 Collector、MAI-UI GPU server 和 emulator health 无关。
Owner approval required: yes（任何共享主机 firewall 变更或新增 host-network sidecar 均需显式选择）
Resource note: 一次宿主机 ask_user completion 成功；失败的容器请求未到达 provider。诊断用临时 Docker containers 已自动删除，两个临时 ncat relay 均已停止；正式 container、emulator、network、vLLM 和 raw data 未改变。
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: runner-side broad credential heuristics incorrectly treated ordinary model-visible Mattermost text such as `password: user` as transport credentials. Two `MattermostCreateChannelTask` decisions therefore had `prediction_raw=null`, a redacted parsed action, and a factually incomplete task stream even though the authoritative model response blobs still contained the exact output.
Evidence (file/symbol/test): runtime/audit/runner_capture.py semantic-only sanitizer path; tests/runtime/audit/test_runner_capture.py two real Mattermost ask_user regressions; targeted 26 passed, full audit 340 passed, combined audit+offline 345 passed, Ruff/format/diff checks passed. Independent security review found no blocking regression. Formal verification run `01M0H60RRE02Y73CSH1XR3YFHJ`, task stream `01M0H60RT9T9EM6HEG4YRQ83S9`, preserves both credential-shaped predictions and all three A_t copies exactly; stream capture_complete=true, missing=[], collector errors=[], score=0 (`Channel not created`).
Decision: task/model/action/tool/user/evaluation semantic evidence excludes only exact configured secret values; task-independent metadata, transport endpoints/headers/configuration, and exception text retain the broad credential sanitizer. Live objects remain untouched. The shared ArtifactSerializer signed-URL policy was not relaxed in this patch because a correct change spans model_io and execution_io transport-query stripping; large/non-plain semantic signed URLs remain a separately documented limitation.
Research-semantic impact: ordinary task/model language is no longer silently rewritten, so P_t and A_t remain lossless for the observed Mattermost case. True configured secrets still never enter events/blobs and still make the affected semantic artifact explicitly incomplete.
Owner approval required: no (owner explicitly requested the repair)
Resource note: code verification was CPU-only. The formal two-task run used the already retained MAI-UI-8B vLLM; old raw runs were not modified.
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: `ThanksgivingPrepTask` initialization failed because parent image `mobile_world:reset@sha256:2038dc91b8a4288d7f66d8d843f970487903483cd81d76fd64be9c3ba658925c` called `reset_chrome(controller)` without importing `reset_chrome`. The backend traceback was `NameError: name 'reset_chrome' is not defined`; this was not an emulator, model, ask_user, or Collector failure.
Evidence (file/symbol/test): docker/patches/thanksgiving_reset_chrome/Dockerfile; parent task source SHA256 `ba4720794818f504fccaa5742d5d75a52e2733274053f0568e015b09fc64d694`; patched source SHA256 `7f6e049d9ee345fd031555bc25fad82707c3c42dc21583da9a4a28c510e74eb4`; derived image `sha256:4e594219474246e1c613a28861981f102df263bf07f0faf3b3a3d2efd2a07a85`. Formal verification task stream `01M0H62ZS4EM1H92JM420ZG66J` initialized normally, ran 50 complete S/P/A/S' transitions, tore down successfully, capture_complete=true, and scored 0 (`incorrect email`) rather than crashing.
Decision: build a no-network, byte-pinned derived image that changes exactly the existing system-helper import line; do not copy the newer repository task file over other parent-image source differences. Stop/auto-remove only the exact owner-labelled container `fbdaad77...`; recreate the same dedicated name, mwnet attachment, localhost 6970/7070/7170/7270 ports, and read-only USER_AGENT env from the immutable derived image. Preserve GPU7 MAI-UI/vLLM.
Research-semantic impact: Thanksgiving now yields a real Agent trajectory and official score instead of an environment-initialization no-result. A fresh emulator prevents smoke state from contaminating the replacement streams. Four CreateChannel ask_user calls were HTTP 200/nonempty, confirming the simulated-user path and container egress were functional in this environment.
Owner approval required: no (owner explicitly authorized repair and fresh emulator)
Resource cleanup/recovery: the replaced `--rm` container/emulator is not recoverable; the repaired container ID `11e2c638...`, its mwnet attachment, and the MAI-UI vLLM remain running. All old raw and trajectory roots remain unchanged.
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: the requested 117-task research dataset spans three immutable physical runs and must not be represented as a synthetic raw run. A zero-copy derived selection manifest is required to retain each source run/task identity and BlobRef resolution boundary.
Evidence (file/symbol/test): src/mobile_world/offline/curated_composite.py; scripts/build_curated_audit_composite.py; tests/offline/test_curated_composite.py (20 passed plus Ruff/format/diff checks and independent adversarial review). Formal dataset root `/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_curated117_20260821_01`; manifest SHA256 `1ffd746e21a15133c7124325b03ca94d7528a7aea5c4a5fda470fd9b8496a60d`; selection SHA256 `c3eb0c7de3a7f53afce46d8b2f64caf16d678fc0d0e851ee4375b53a37a26b33`.
Decision: publish `mobileworld.audit.curated-task-set/v1` with artifact_type=`derived_task_selection`, is_raw_run=false, selecting exactly one completed/capture-complete/zero-missing/zero-collector-error stream per canonical GUI-117 task. Selection is old100=100, rerun15=15, final2=2; no raw events or blobs are copied. Build/independent validate traversed 110,498 BlobRef occurrences and strongly SHA256-verified 21,864 unique source-local blobs (3,815,335,554 bytes); all checks passed.
Research-semantic impact: consumers obtain exactly 117 canonical task trajectories while preserving source provenance and excluding the 156 old crash attempts, the rerun CreateChannel incomplete stream, and the Thanksgiving init crash. The two report warnings describe only source-run-global status (old run crashed; rerun17 globally incomplete); every selected task satisfies the stricter per-stream eligibility predicate.
Owner approval required: no (owner explicitly requested the final 117-task dataset)
Trust boundary: source raw must remain immutable after validation; the zero-copy manifest intentionally does not create a cross-run lock. Hard-linked source leaves are accepted if their bytes/hash are valid. The builder supplements, but does not replace, the independently completed full raw integrity audits.
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: the curated MAI-UI GUI-117 data is suitable for an offline, outcome-blind motivation audit, but directly reviewing every raw event/blob or every structural exposure would be both unnecessarily expensive and statistically noisy. The deterministic scanner initially surfaced 2,989 broad candidates from 91,607 exact assistant-history exposures.
Evidence (file/symbol/test): src/mobile_world/offline/motivation_cards.py; src/mobile_world/offline/motivation_review.py; src/mobile_world/offline/motivation_prompt.py; scripts/run_motivation_codex_review.py; three matching offline test modules. Full offline suite 53 passed; Ruff check/format and git diff checks passed. A strong real-data preflight revalidated all transitive blob digests and reconstructed 117 tasks / 4,156 steps / 91,607 exact exposures. It produced 240 bounded formal candidates across 68 tasks (maximum five per task), canonical task_cards SHA256 `eccb7697204799355aa8281b4df039d701c4cdec143c47efaed64f0054305090`, outcome sidecar SHA256 `09d218ffe82167c09bbcafb1ff8cc1e3c30b67b1716dce06d44d06acef0fe994`, and reconstruction sidecar SHA256 `11a5bc0c8a004db39f721499b641a6f777c994b996e17e78bec01211c927a059`.
Decision: preserve every reconstructed S/I/P/A/R/S' step and exposure in a local sidecar, while formal review retains every high-precision textual/transition hit and at most four temporally spread pairs with at least two independent structural signals. Run outcome-blind Terra PASS1 over all 117 tasks, freeze it before opening the local formal outcome sidecar, then send all positive/uncertain tasks plus a seeded app×outcome-stratified 15% negative sample to independent Sol review; send only metric-critical disagreements to a separate Sol adjudicator identity. Compute final conservative motivation metrics only after review labels are frozen.
Research-semantic impact: reviewers receive compact cards and only card-local evidence screenshots; they do not receive the 4 GB raw corpus, task outcome/reason/score, selection reason, configured secrets, or another reviewer's labels (except the adjudicator, which receives both blind reviews). Structural signals remain retrieval facts, never runtime labels or causal claims. The source raw and curated manifest remain read-only.
Owner approval required: no (owner explicitly authorized sending the described blinded cards and relevant screenshots to OpenAI Codex after the external-processing risk was disclosed)
Execution: derived root `/shared/linqiang/mobileworld_audit_data/mai_ui_8b_gui117_g7_motivation_audit_v1_20260821_01`; primary model `gpt-5.6-terra`, secondary/adjudicator `gpt-5.6-sol`; Codex invocations are ephemeral, read-only, finite-retry, and batch artifacts are frozen with SHA receipts. The initial eight-task batch returned schema-valid but contract-invalid reviews in all three attempts, so the first tmux exited without freezing any review batch. Its failure receipt remains under `review/`. A one-task boundary test and a four-task test both passed; the formal run was restarted in detached tmux `mai8b_motivation_audit_v2` with batch size four and independent output `review_v2/`. The driver now records only a bounded, sanitized validation-error string on future invalid responses; it still discards subprocess stdout/stderr contents and hashes them in receipts.
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: the Qwen3-VL GUI-117 run completed all 117 tasks, but one ThanksgivingPrepTask model request was marked incomplete because the 5-byte local no-auth API-key sentinel was supplied in an uppercase CLI form and was incorrectly registered as a configured secret. Its bytes appeared once by chance only in a 1,294,892-character PNG base64 payload; the decoded/canonical PNG did not contain the sentinel. This was neither a real credential leak nor a Qwen provider failure.
Evidence (file/symbol/test): runtime/audit/secret_policy.py plus the shared normalization callers in context.py, lifecycle.py, model_io.py, runner_capture.py, and integrity.py; regression coverage in test_context.py, test_lifecycle.py, test_base_model_io.py, test_runner_capture.py, and test_integrity.py. Full audit suite 354 passed; Ruff check/format and git diff checks passed; independent security review found no blocker. The original affected stream was `01M0HZWRV2PFNH0A562PE8A81J`, collector error `01M0J03BJYQ02BRBY267ME7TBD`, at step 26.
Decision: treat only the exact MobileWorld local no-auth sentinel as non-secret, case-insensitively, at every configured-secret normalization boundary. Do not trim it, perform substring matching, ignore arbitrary short secrets, or exempt base64/data URLs. Transport API-key fields remain excluded from metadata, while every other non-empty credential remains fail-closed. D-020 records this policy.
Research-semantic impact: the independent replacement run `01M0J8N7SZPG34GGGPW9QRCDND` / task stream `01M0J8N7WPST76NT7E9Y9DBJ80` preserved 50/50 model request snapshots and terminals, 50 decisions/actions, capture_complete=true, missing=[], collector errors=[], and passed integrity with 0 errors, 0 warnings, and 0 orphan blobs. Its official score is 0 (`No email found` at max step), which is retained as genuine agent behavior rather than retried for a better label. An earlier isolated rerun1 used a base URL without `/v1`, produced one complete crashed evidence stream with three HTTP 404 terminals, and is not selected.
Derived dataset: `/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_curated117_20260821_01`, manifest SHA256 `266ab97b02fd6d479114a2f0db945dc9e66f17c4ba1f68a04b375ec3384a5cf2`. It selects qwen_original=116 and qwen_thanksgiving=1, strongly verifies 45,126 BlobRef occurrences and 14,573 source-local unique blobs / 2,255,267,859 bytes, and passes all curated validation checks. The sole warning is source-run-global incompleteness for the original run; every selected stream is completed/capture-complete/zero-missing/zero-collector-error.
Owner approval required: no (owner explicitly requested the repair)
Resource/provenance note: the original Qwen manifest.start/final/run.events and affected old task stream hashes remain exactly `8ae4a11b...`, `789432f7...`, `d488f2f0...`, and `16e4c7b1...`. The Qwen GPU7 vLLM and owner-labelled fresh MobileWorld container remain running; no other GPU process/container was changed.
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: the first formal motivation-review attempts exposed a contract mismatch rather than an evidence failure: Structured Outputs constrained JSON shape/enums, while deterministic post-validation enforced additional cross-field rules that prompt v1 did not state. Invalid retries reused identical prompts, four-task batches coupled unrelated tasks, and reviewer-visible NEGATIVE_AUDIT/PASS2 phases leaked the frozen primary routing class. The Codex read-only sandbox also did not by itself enforce filesystem-level outcome blindness.
Evidence (file/symbol/test): src/mobile_world/offline/motivation_prompt.py prompt v2; motivation_review.py::derive_task_screen_class; scripts/run_motivation_codex_review.py legacy seed loader, per-attempt feedback/provenance, singleton scheduling, neutral secondary phase, disabled reviewer tools, and bounded stage-failure continuation; tests/offline/test_motivation_codex_driver.py. Offline suite 63 passed, full MobileWorld suite 424 passed, Ruff check/format passed. Real cards dry-run revalidated 117 tasks, 960 image-reference records, an explicitly anchored legacy receipt SHA256 033fd7a17427272cb50c20dfe24fd287f3612516585fed1bae507db7c5500969, four imported tasks, and 113 pending tasks without opening outcomes or writing files.
Decision: prompt v2 states every validator-critical subtype/effect/coverage/screen/reference rule. Only invalid-response retries receive bounded `{code,path,message}` feedback; every actual prompt/model response has its own SHA provenance, rejected canonical responses are retained, and mechanically derived task_screen_class is normalized in code while the exact model JSON is preserved separately. New PASS1/PASS2/adjudication units are one task each; up to three isolated retry-exhausted tasks may be preserved/continued before a stage halts. Legacy c001-c004 are accepted only through an explicit receipt-SHA anchor, full current validation, versioned v1 case-hash reconstruction, safe batch-id grammar, and copied import receipt. All selected secondary tasks use reviewer-visible PASS2 regardless of routing reason. Codex is invoked with user config ignored and shell/unified exec/view-image/browser/apps/plugins/computer-use and other external-reading features explicitly disabled; raw filesystem paths are omitted from prompt image mappings, which use batch-global deduplicated attachment indices.
Research-semantic impact: PASS1 remains outcome-blind by construction and is frozen at exactly 117 tasks before the local outcome sidecar is opened. The secondary reviewer cannot infer primary class from its phase. Semantic effect labels are never auto-invented; only the deterministic task-level aggregation field is computed by code. The source raw, curated117 manifest, cards, reconstruction sidecar, and outcome sidecar remain unchanged. This is an observational screen and does not support causal claims.
Owner approval required: no (owner explicitly authorized the blinded Codex review and repair)
Execution: isolated boundary smoke `review_boundary_smoke_v3_c022` reviewed SendWaiverTask with four evidence mappings/two unique image attachments in one valid attempt under the disabled-tool configuration; response SHA256 96a0dcaf5200706fe633abf86c97af4d4108a060afc361f8c2d15804fcc713d2 and receipt SHA256 45ee1ea70c820a9b4ad8f4d28db3a31b54a24b21729237a1dcd4e5aa11f0b231. Formal detached tmux `mai8b_motivation_audit_v3` writes only derived artifacts under review_v3; the first pending singleton c005 completed and was atomically committed. No GPU, emulator, Docker, or raw collection process was changed by this analysis repair.
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: the first motivation-card builder normalized only MAI raw assistant-message replay. Applied unchanged to the Qwen3-VL curated117 set, it reconstructed all 3,017 decisions but silently reported zero history exposures/candidates even though Qwen places cumulative prior conclusions in the user message's `Task progress ... Step N:` flat-progress text.
Evidence (file/symbol/test): src/mobile_world/offline/motivation_cards.py Qwen adapter dispatch and exact flat-progress mapper; tests/offline/test_motivation_cards.py ordinal/quote/tool/ask-user/fail-closed regressions. A full read-only strong-hash reconstruction of Qwen curated manifest SHA256 `266ab97b02fd6d479114a2f0db945dc9e66f17c4ba1f68a04b375ec3384a5cf2` produced 117 tasks / 3,017 steps / 58,677 exact exposures / 2,900 history-bearing requests / 434 bounded formal candidates across 87 tasks. All 434 formal claims use representation_type=`flat_progress`; 48,946 long-lag exposures lack their source/post screenshot in the target request. Full offline suite 68 passed and Ruff check/format passed.
Decision: select normalization from explicit task/source adapter provenance. For `qwen3vl`, reproduce the exact runtime template, Action-conclusion extraction, quote/newline normalization, and tool-then-ask suffix rendering; map by `Step k` ordinal, store exact character offsets and hashes, and fail closed on provenance disagreement, malformed markers, delimiter collisions, or any byte mismatch. Candidate claim text excludes exogenous tool/user suffixes while target-request evidence preserves the complete exposed span. Preserve MAI `raw_replay` behavior and reject unsupported adapters rather than guessing.
Research-semantic impact: Qwen's lossy action conclusions are now measured as actual `flat_progress` exposure instead of being misclassified as no history. MAI/Qwen rates must remain stratified by representation and denominator; this observational comparison cannot establish model or history-format causality.
Owner approval required: no (owner explicitly requested the Qwen audit start)
Execution: outcome-blinded bundle `/shared/linqiang/mobileworld_audit_data/qwen3vl_8b_gui117_g7_motivation_audit_v1_20260821_01/cards`; manifest SHA256 `0adb760294dcf917875af13f1e44d3f3d843462da2d0b43b10ef11693b512351`; task-cards SHA256 `e3e422e02d612ffebb6478f08e674b4b2c2fafb8125c2310f0725f42f0ae2d67`; outcome sidecar SHA256 `8d4f1cadf536989054dcc015ff46127c2e5c591a90b5abefc1096f08a7cdcdb7`. Dry-run resolved 1,712 unique candidate images without opening outcomes. Detached tmux `qwen3vl8b_motivation_audit_v1` runs singleton outcome-blind Terra PASS1 under `review_v1/`, followed only after the frozen 117-task PASS1 by Sol review of positive/uncertain plus a seeded 15% negative audit and disagreement adjudication. The first task was atomically accepted on retry 2 after bounded feedback corrected an evidence-ID sort-order violation; receipt SHA256 `b7a1f68a27ea70232ea8d1c3e2601fac11a22a6258b6ae9281d8866b0fe5a1d6`. Raw, curated, and card inputs remain unchanged/read-only.
```

```text
Date: 2026-08-21 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: repeated live `init_state` restores under the container's software-rendered emulator could acknowledge snapshot load before ADB/screenshot stability. In three fresh GELab smoke containers the third logical task's first attempt then failed before any model request with `Device is not healthy`; task reordering moved the failure with position, excluding GELab and task-specific logic. The old recovery path could also restart underneath an active task attempt, leave timed-out ADB children alive, and lose emulator-generation diagnostics.
Evidence (file/symbol/test): runtime/controller.py, runtime/utils/helpers.py, tasks/base.py, core/server.py, runtime/utils/docker.py, docker/start_emulator.sh; tests/runtime/test_snapshot_recovery.py and test_emulator_restart.py. Full suite 467 passed; Ruff lint/format, `bash -n`, and git diff checks passed; independent combined review reported zero blockers. The no-model real-container gate completed 20/20 task-init/snapshot/decoded-PNG/tear-down cycles with one unchanged emulator generation/PID and zero bad screenshots or restarts.
Decision: require a bounded continuous-health and decoded-PNG barrier after snapshot restore; serialize device lifecycle operations; clear stale task state; perform at most one same-request verified recovery only for typed device failures; defer health recovery while a task is active so attempt boundaries remain factual; kill timed-out ADB/restart process groups; and record/verify exact emulator generations with bounded postconditions and append-only generation logs. Package only the six byte-pinned parent-image deltas in a network-disabled derived image rather than copying unrelated dirty-worktree source.
Research-semantic impact: the repaired three-task GELab smoke produced exactly three completed streams, zero crashes/retries, capture_complete=true, zero missing/collector errors, and integrity valid with zero errors/warnings. All 24 model calls terminated normally and parsed successfully; the INFO ask-user reply appeared with the rolling summary in the next request. ChromeSearch's score 0 at the 12-step smoke cap is genuine model behavior, not a collection failure.
Derived image/provenance: `mobile_world:snapshot-lifecycle-recovery-20260821@sha256:081fd323e3d9e3b9b52fdc67ea1a78fcd3d900465c082c6a8561c6593a0152fd`, parent/base `sha256:4e594219474246e1c613a28861981f102df263bf07f0faf3b3a3d2efd2a07a85`, patch id `snapshot-lifecycle-recovery-v1`. Build checked exact parent, patch, patch-applier, and post-image SHA256 values and ran with `--network=none`.
Execution: GELab formal GUI-117 run `01M0JSPDHJ073675315FBW27BN` started in detached tmux `gelabzero4b_gui117_g7_run` using a third fresh owner-labelled container, exact derived image digest, canonical 117-task order SHA256 `c87a1d579dfa6cfcb1a690bc967599fc320e1034b1e7fe33067c54de2843a9cf`, max-round 50, concurrency 1, and auto-retry 9. Data root is `/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_20260821_01`; initial tripwires show 117 tasks, attempt 1/10, and canonical first task. GPU7 is shared with other users and may reduce throughput; no other user's process was changed.
Owner approval required: no (owner explicitly requested the repair and GELab collection)
```

```text
Date: 2026-08-22 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: the GELab GUI-117 run completed cleanly, but its history cannot be reconstructed by either MAI raw-message replay or Qwen cumulative flat progress. GELab recursively exposes only the most recently parser-accepted `summary`; a parser exception retains an older summary, an accepted empty/missing summary resets the prompt to `暂无历史操作`, and a simulated-user reply is appended to the summary as external evidence rather than model-authored history.
Evidence (file/symbol/test): src/mobile_world/offline/motivation_cards.py GELab adapter dispatch, parser-state replay, exact rolling-summary mapper, provider edge-whitespace classification, and GELab-only bounded selector; tests/offline/test_motivation_cards.py runtime-parser parity, lag>1, sentinel, ask-user separation, provenance/template failure, whitespace-only provider, and deterministic tier/quartile selection regressions. Main-agent validation: full MobileWorld suite 481 passed; Ruff check/format and git diff checks passed; independent review reported zero blockers.
Decision: record each actual GELab history injection as representation_type=`rolling_summary`, with exact source decision, target request character span, hashes, and lag. Preserve the assistant summary separately from any appended ` 用户回复说：...` suffix. Retain all exact exposures in reconstruction. Define formal review retrieval as a GELab-only maximum of four candidates per task: critical transition/correction/provider signals first, then progress plus structural evidence, compound structural evidence, and progress-only; an overflowing tier is selected deterministically across target-time quartiles. This declared scanner policy is a conservative review subset/lower bound, not the exposure denominator; MAI/Qwen selection is unchanged.
Research-semantic impact: a strong real-data preflight over all 117 selected task streams reconstructed 3,808 decisions and 3,691 exact rolling-summary exposures (all observed lag=1), including 11 ask-user suffixes. The bounded formal set is 450 candidates across 116 tasks (per-task maximum/median 4), down from an impractical 2,725 unbounded hits; all 3,691 exposures remain in the sidecar. All 3,808 provider/decision differences were only trailing-edge whitespace and remain factually recorded as `edge_whitespace_only` without becoming false content-difference candidates. `EXACT` denotes source/span provenance only, never factual validity or causal influence.
Collection result: raw run `01M0JSPDHJ073675315FBW27BN` contains 117/117 results (18 score1, 99 score0), 117 completed streams, zero crash/retry/parse/collector anomalies, and integrity 0 errors/0 warnings/0 orphan blobs over 16,390 verified blobs (2.6628 GB). manifest.final SHA256 `0f0978a24f0e3a7020c851715064b3f09d11b61cac0c5a838d08255c7380b3d9`.
Derived outputs: zero-copy curated set `/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_curated117_20260822_01`, manifest SHA256 `aa7d7a7d1ff964097cf077fede57ed336257912d00c11cfbab424e4cebe706a8`; independent validation strongly checked 54,570 BlobRef occurrences and 16,390 unique source-local blobs / 2,662,805,591 bytes with all checks true and no warnings. Outcome-blinded bundle `/shared/linqiang/mobileworld_audit_data/gelab_zero_4b_gui117_g7_motivation_audit_v1_20260822_01/cards`: manifest SHA256 `05cf757b640368169c2b425a83b5419ef26b7bdfc93e8bf9bb174b3ccb0e4810`, task cards `533f316a1981d9c6d29df1655b46d4ac0fc54326c280ed2bcd97015659c93d3a`, reconstruction sidecar `501c7880d1ccf1aacf888e8a6d9557e2b8b798084763d5e7f8a147497f9c4d83`, isolated outcome sidecar `6110842fc8886f2d4bdd07357121ad41f8af14b31e8caba4c197629587bfefa2`. No external reviewer/API was invoked by this build.
Owner approval required: no (owner explicitly requested the GELab reconstruction implementation)
```

```text
Date: 2026-08-22 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: MobileWorld's UI-Venus adapter already used the UI-Venus-1.5 flat Previous Actions representation, but its parser rejected the official framework's bare-action fallback and retained double quotes in double-quoted text parameters. It also parsed the official PressRecent action and then crashed while converting it because MobileWorld has no recent-apps JSONAction.
Evidence (file/symbol/test): official inclusionAI/UI-Venus branch UI-Venus-1.5 commit 192a9247ad1129279ba1d6c263d4c9e7ecef3644 (Venus_framework/policy/ui_venus_policy.py and processor/uivenus_processor.py); official model revision inclusionAI/UI-Venus-1.5-8B@a06ff6c6f15a9eca210769dacc1603f73b4a500c; src/mobile_world/agents/implementations/ui_venus_agent.py; tests/agents/test_ui_venus_agent.py. The adapter+audit targeted set passed 360 tests, the full MobileWorld suite passed 487 tests, Ruff check/format and git diff checks passed, and independent review found no blocker.
Decision: preserve the existing MobileWorld prompt, generation parameters, current-image-only request, parse-failed history inclusion, and flat-history semantics. Make history_length=0 explicitly mean unbounded history without changing rendered bytes; accept one matching single/double quote pair and the official bare-action fallback; convert unsupported PressRecent to a factual UNKNOWN rather than turning a model output into a runtime crash. The component test rehydrates the collector's authoritative SDK-argument artifact and compares it directly with the fake provider's received kwargs.
Research-semantic impact: UI-Venus remains a distinct flat-history adapter: later requests contain Step N <think>/<action> entries and only the current screenshot. No prompt filtering, outcome label, history deletion, or intervention was introduced. Real-model/container smoke remains required before the formal 117-task run.
Owner approval required: no (owner explicitly requested UI-Venus preparation and formal collection)
Resource note: this preparation used CPU-only synthetic tests and read-only upstream/model metadata queries. It did not download weights, call the model, operate Docker/emulator, write raw data, or touch any GPU process.
```

```text
Date: 2026-08-22 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Execution: downloaded and pinned `inclusionAI/UI-Venus-1.5-8B` revision `a06ff6c6f15a9eca210769dacc1603f73b4a500c`. The snapshot contains exactly 13 resolved files. All four safetensors shards matched their official LFS byte counts and SHA256 values; 750 indexed tensors had exact key/shard coverage, valid contiguous headers, no duplicates, no broken links, and no incomplete or held download locks.
GPU handoff: after exact tmux, PID/PGID, model-path, served-name, localhost-port, and owner checks, only the linqiang-owned GELab vLLM tmux session was stopped. Its API/engine PIDs and port 18007 disappeared and about 24.8 GiB was released. The independent GELab Codex audit remained live, and no other user's GPU process was changed. UI-Venus now runs in tmux `uivenus15_8b_g7_server` from the immutable snapshot, served as `UI-Venus-1.5-8B` on `127.0.0.1:18007`, using vLLM 0.11.0, BF16, max model length 32768, one sequence, and GPU memory utilization 0.24.
Smoke result: real adapter format probe returned a valid `Finished` action. A second fresh, owner-labelled MobileWorld container then produced smoke run `01M0M22E7QCKNHY7JZSG2ZH342`: three completed task streams, zero crashes/retries/model-attempt failures/unknown parses/transition failures, capture_complete=true, no missing artifacts or collector errors, and integrity 0 errors/0 warnings/0 orphans over 190 verified blobs. All 21 requests had matching terminal responses. Step one had empty Previous Actions; every later request exactly contained all preceding `Step N: <think>...</think><action>...</action>` entries, no conclusion/status history, and exactly one image matching the current observation. OpenFlightMode and Chrome weather scored 1; the ask-user alarm task scored 0 because UI-Venus assumed a time instead of requesting it, which is model behavior rather than collection corruption.
Formal run: after destroying the smoke container, a third fresh formal container was created from `sha256:081fd323e3d9e3b9b52fdc67ea1a78fcd3d900465c082c6a8561c6593a0152fd` with owner/purpose labels, `mwnet`, localhost-only ports, and the simulated-user env mounted read-only. The filtered catalog is exactly 117 tasks with canonical order SHA256 `c87a1d579dfa6cfcb1a690bc967599fc320e1034b1e7fe33067c54de2843a9cf`. Formal run `01M0M2QKK2PRPEA3VXAKZ36W4B` completed at `2026-08-22T15:09:45.503547Z` in one outer attempt: 117/117 results, 15 score1 and 102 score0, exactly 117 completed streams, zero crash/retry, capture_complete=true, no missing artifacts, and no collector errors. The official read-only integrity check returned valid=true with 0 errors, 0 warnings, 24,871 events, 4,112 model requests/terminals/decisions, 20,881 verified blobs, and 0 orphan blobs; raw blobs total 4,003,493,486 bytes. Fingerprints: manifest.start `12e14e7b11dcc0459a96ac88ace4a9e80be7d2a26b268e9f09aceeb999b1a7d5`, manifest.final `06e1d8de4fc6404436f1c83ba6de40734699397a97c314df19dc55ee4e308fac`, run.events `89c71b216243119a13eafd953aff9a9502982880084da99b5b3fcb4f40b06160`. Data root is `/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_20260822_01`; the UI-Venus model service and formal container remain healthy and allocated for the next authorized step.
Owner approval required: no (owner explicitly requested the download, guarded GPU switch, deployment, smoke, and formal 117-task collection)
```

```text
Date: 2026-08-22 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: the UI-Venus formal run is complete and lossless, but its cumulative `Previous Actions` history is neither MAI assistant-message replay nor Qwen action-conclusion progress. UI-Venus re-injects every prior decision as a zero-based `Step N: <think>...</think><action>...</action>` entry; conclusion/status fields are excluded. Its current screenshot is converted RGBA-to-RGB and re-encoded, so encoded PNG BlobRef hashes differ even when the runtime pixel matrix is identical.
Evidence (file/symbol/test): src/mobile_world/offline/motivation_cards.py UI-Venus provenance dispatch, exact flat-previous-actions mapper, authoritative request-view/request-images cross-check, conversion-aware RGB pixel verification, and formal-candidate span mapping; src/mobile_world/offline/motivation_review.py adds only the accepted representation value `flat_previous_actions`; tests/offline/test_motivation_cards.py and test_motivation_review.py cover runtime extraction parity, bare/empty-action fallback, parse-failed history inclusion, ordinal/span checks, re-encoded-identical pixels, altered/historical images, request-view provenance, and the unchanged shared review contract. Full suite 503 passed, offline 98 passed, Ruff lint/format and git diff checks passed; independent implementation, scientific, and metric-invariance reviews reported zero blockers.
Decision: keep adapter-specific history reconstruction but reuse the exact same candidate schema, outcome-blind review rubric, MHR/MHR-OH definitions, downstream-effect labels, severity derivation, denominators, pass2 selection, and metrics. Record UI-Venus as representation_type=`flat_previous_actions` and mapping_status=`exact_ui_venus_flat_previous_actions`; retain all exact exposures in reconstruction and use the existing shared candidate selector without a UI-specific cap. Compare the unique current request image with S_t using decoded RGB pixels, never raw PNG hash equality. The review schema remains v1; compute_metrics does not read representation_type.
Research-semantic impact: all 117 tasks / 4,112 decisions reconstruct exactly, including 3,995 history-bearing requests, 3,995 unique exposed source steps, and 91,058 source-target history appearances (lag 1--49). The shared selector yields 264 outcome-blind candidates across 71 tasks (median 4, p95 5, maximum 6); reason occurrences are NEAR_DUPLICATE_REASONING 240, REPEATED_ACTION 237, STATIC_TRANSITION 122, PROGRESS_CLAIM 24, and LONG_LAG_IMAGE_ABSENT 13. All 4,112 authoritative request-image wrappers agree with the request-images record and current S_t RGB matrix. Provider/decision differences are 2,526 exact plus 1,586 edge-whitespace-only, with zero substantive differences.
Derived outputs: zero-copy curated set `/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_curated117_20260822_01`, manifest SHA256 `2e12cb879a2e66eda739e9fd25d9c2ddabd95f9e5b21b1c14c973d5017022c6d`, selection SHA256 `94ad68ecb3c7ca9e84a5e9c490e7e4edeecc5f97821d1dfa57c41e54ecbb5577`; independent validation strongly checked 61,649 BlobRef occurrences and 20,881 unique source-local blobs / 4,003,493,486 bytes with all checks true and no warnings. Outcome-blinded bundle `/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_motivation_audit_v1_20260822_01/cards`: manifest SHA256 `378c8cf8df21e24ce0d9ec7bc1d439f975d6d6232832974f484afe4308d8811b`, task cards `cf7797adfdab577cd44468380151de95afbb69faa422a2b693ad2ccf7e5a53cb`, reconstruction sidecar `eaf0e5a202a73996dc935c003e6d9db9e57175925ed613580b8c5c0f7e78aabe`, isolated outcome sidecar `aa4530853a3a030de8827d19670fffbaede22e82786290c405f269af417b4eb6`. Driver dry-run resolved 1,056 candidate images for 117 singleton PASS1 batches, wrote zero files, and did not open outcomes.
Owner approval required: no (owner explicitly requested UI-Venus-specific analysis preparation with unified metrics)
External-processing boundary: no Codex/API review was invoked and no UI-Venus evidence card or screenshot was sent externally. A separate explicit UI-Venus authorization is still required before starting review_v1.
```

```text
Date: 2026-08-22 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Authorization/execution: the owner explicitly authorized sending the frozen UI-Venus evidence cards and selected screenshots to OpenAI Codex. Outcome-blind review `review_v1` was started in background tmux session `uivenus15_8b_motivation_audit_v1` with gpt-5.6-terra for singleton PASS1, gpt-5.6-sol for PASS2 and the seeded 15% negative audit, and gpt-5.6-sol for material-disagreement adjudication. The first launch intentionally did not use `--resume`.
Input tripwires: cards manifest SHA256 `378c8cf8df21e24ce0d9ec7bc1d439f975d6d6232832974f484afe4308d8811b`; task cards `cf7797adfdab577cd44468380151de95afbb69faa422a2b693ad2ccf7e5a53cb`; reconstruction sidecar `eaf0e5a202a73996dc935c003e6d9db9e57175925ed613580b8c5c0f7e78aabe`; isolated outcome sidecar `aa4530853a3a030de8827d19670fffbaede22e82786290c405f269af417b4eb6`. The output root and log were absent before launch, and no conflicting UI-Venus review process or tmux session existed.
Initial health: the tmux pane and Python driver remained alive, and the first four singleton PASS1 batches (`c001`--`c004`) each produced an accepted, hash-bound receipt on attempt 1. No frozen PASS1, outcome selection, PASS2, or adjudication artifacts existed at this checkpoint; therefore outcomes had not yet been opened. The review continues unattended in the background at `/shared/linqiang/mobileworld_audit_data/ui_venus_1_5_8b_gui117_g7_motivation_audit_v1_20260822_01/review_v1`.
Owner approval required: no (explicit authorization was given in this thread)
```

```text
Date: 2026-08-22 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: the official MobileWorld GUI-Owl-1.5 adapter's collapsed-history path associated action i with result observation i instead of i+1. With the default history_n=1 this shifted every MCP/ask-user result to the next action and omitted the latest completed result. Its 999-normalized endpoint also scaled to width/height, one pixel outside the valid screenshot boundary. Prompt-advertised key/Menu, other unsupported mobile actions, and hallucinated non-registered MCP tool names could escape parsing and then crash conversion or environment execution. The shared mutable runtime_conf default was also consumed with pop().
Evidence (file/symbol/test): src/mobile_world/agents/implementations/gui_owl_1_5.py; tests/agents/test_gui_owl_1_5_agent.py. Golden tests cover history_n=1 tool and ask-user results, history_n=3 collapsed-plus-raw pairing, current-only default images, parser structure, coordinate endpoints/bounding boxes/swipes, conversion retry and state atomicity, reset/config isolation, registry resolution, registered/unregistered MCP dispatch, and exact collector rehydration/correlation across a malformed-response retry. The GUI-Owl plus shared outer-retry set passed 97 tests; the full MobileWorld suite passed 526 tests. Both changed files pass Ruff and git diff --check. Repository-wide Ruff still reports 277 unrelated pre-existing findings outside this patch.
Decision: retain the MobileWorld experiment defaults history_n=1 and SCALE_FACTOR=999 and do not change the official prompt or add a tap alias. Read each collapsed result from observation i+1; build requests without mutating history; commit only a fully parsed, converted, JSONAction-valid turn; retry parse/conversion-invalid outputs at most five times with identical messages. Clamp every point to [0,width-1]/[0,height-1]. Return factual UNKNOWN with an explanatory text for unsupported mobile actions, an unregistered tool, or exhausted output validation, avoiding the existing global ENV_FAIL enum mismatch without expanding this patch into shared action-model semantics. Copy runtime_conf and require integral history_n>=1.
Research-semantic impact: GUI-Owl remains the intended hybrid-collapsed stratum: at the formal default, all prior Action conclusions and their correctly aligned external results are text-collapsed and only the current screenshot is sent. No history filtering, runtime label, outcome signal, rubric, or Sentinel intervention was added. Parser/conversion retries remain losslessly visible at the provider boundary while only accepted outputs enter later model history.
Owner approval required: no (owner explicitly requested GUI-Owl preparation before download/deployment/collection)
Resource note: implementation and verification were CPU-only. No model weight, GPU process, tmux service, Docker container, emulator, or raw collection data was read or changed. A real-model format/action smoke remains required before the formal 117-task run.
```

```text
Date: 2026-08-22 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Execution: downloaded and pinned `mPLUG/GUI-Owl-1.5-8B-Instruct` revision `06d5faecff74840bab2be2425e9c42667a5d04fc`. The first shard-4 download had the official byte count and a structurally valid safetensors header/index but failed SHA256 because it contained a 67,088,384-byte sparse hole. That file was preserved under `/shared/huggingface/quarantine/`, only shard 4 was re-downloaded with Xet disabled, and the repaired snapshot then passed exact SHA256 checks for all four shards, 16/16 snapshot entries, 750 tensor/index mappings, JSON parsing, and `metadata.total_size=17,534,247,392`; no incomplete file or held lock remained.
GPU handoff: UI-Venus vLLM had already shut down normally and port 18007 was free. The remaining UI-Venus Codex audit is CPU-only and was preserved. GUI-Owl now runs in tmux `guiowl15_8b_g7_server` from the immutable snapshot, served as `GUI-Owl-1.5-8B-Instruct` on `127.0.0.1:18007`, with vLLM 0.11.0, BF16, max model length 32768, one sequence, and GPU memory utilization 0.24. Only linqiang-owned resources were changed; the two existing taoz GPU7 processes remained alive and unchanged.
Smoke result: a real adapter format probe returned a valid first-attempt `Action` plus `mobile_use` tool call. A second fresh owner-labelled MobileWorld container produced smoke run `01M0NJDJFJTTT4ZFRAG0V2ETX1`: three completed streams, zero crashes/retries/UNKNOWN parses/model or transition failures, capture_complete=true, no missing artifacts or collector errors, and integrity 0 errors/0 warnings/0 orphan blobs over 213 verified blobs. All 27 requests correlated exactly with their response, decision, and action; each request contained only system plus user, one current screenshot, and the exact default history_n=1 collapsed history. All 127 prior-step lines reconstructed exactly, including the alarm simulated-user reply appearing with the correct preceding action from the next request onward. Scores were 1/0/1; the Chrome zero was a CAPTCHA/max-step model outcome rather than collection corruption.
Formal run: after destroying the smoke container, a third fresh formal container was created from `sha256:081fd323e3d9e3b9b52fdc67ea1a78fcd3d900465c082c6a8561c6593a0152fd`, with exact owner/purpose labels, `mwnet`, localhost-only ports, and the simulated-user environment mounted read-only. Its GUI-only catalog is exactly 117 tasks with canonical order SHA256 `c87a1d579dfa6cfcb1a690bc967599fc320e1034b1e7fe33067c54de2843a9cf`. Formal run `01M0NK5E8YZM21NA5WZ7Q3A9MD` started in background tmux `guiowl15_8b_gui117_g7` with max-round 50, concurrency 1, auto-retry 9, and exact `/v1` model endpoint. Initial tripwires show 117 tasks, attempt 1/10, the canonical first task, correct image/model/adapter roots, MCP/user-interaction disabled, and stream chunks enabled. Data root: `/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_20260822_01`.
Owner approval required: no (owner explicitly requested the download, guarded GPU switch, deployment, smoke, and formal 117-task collection)
```

```text
Date: 2026-08-22 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
Finding/deviation: MemGUI does not replay raw messages, cumulative actions, or a single rolling summary. Its official live adapter builds each request from three actor-managed structures plus the current screenshot: H_t folded action summaries, L_t the most recent accepted step record, and M_t persistent UI memory. Older screenshots and raw assistant responses are absent. The live adapter also omitted the environment result r_t from L_t, destructively replaced every old folded span that overlapped a new span, could partially mutate folding or memory before a later validation failure, fabricated a missing fold from the current intent, scaled coordinate 1000 outside the image, and returned an invalid ENV_FAIL JSONAction after exhausted parsing.
Evidence (file/symbol/test): official `lgy0404/MemGUI-8B-SFT@7c167054fe55512a923f6bdea873e99361d588b0`, `kwai/MemGUI-Agent@321734eaf9788c6a802f8f11e62651702d14af28`, MobileWorld adapter pin `83e7b8fc75ebb6c4a098254a42999db5f4172666`, and MemGUI-Bench adapter pin `747a9d7c32f0057a4ff46d9e8ae85de264ffc9a9`; src/mobile_world/agents/implementations/memgui_agent.py and tests/agents/test_memgui_agent.py. Final source/test SHA256 values are `7c480fe9ff56d1f61a26f727cb08981e5eae7a6d05599cbf61f18eeffd85038f` and `15e90f101f69b446e7cc22275412420cf0d13e2967ea82e2c43a899b73180ff9`. The MemGUI plus shared outer-retry set passed 114 tests, the full MobileWorld suite passed 566 tests, Ruff check/format and git diff checks passed, and an independent final review reported zero blockers. A real Collector retry probe also confirmed identical messages across rejected/accepted attempts, complete lossless call correlation and rehydration, one current image per request, and only the accepted response entering history.
Decision: preserve official structured-folding semantics, including one-based fold ranges satisfying 1 <= start <= end <= current_step, non-forced tag order, and destructive replacement of any overlapping old folded span. Require a valid fold from step two onward instead of fabricating one. Parse, validate, and stage the fold, memory mutation, action conversion, and JSONAction before committing any adapter state. Duplicate memory_add and missing memory_update/delete IDs are contract violations that retry with identical messages; this is intentionally stricter enforcement than the official live adapter. Unsupported actions/buttons/tools, invalid memory operations, and invalid coordinates participate in the same three-attempt validation loop; exhaustion returns a factual legal UNKNOWN. Clamp valid normalized coordinates to the current image bounds. Preserve MobileWorld's deterministic request temperature 0.0 and do not import the checkpoint's sampling defaults into the adapter request.
Research-semantic impact: H_t and M_t are actor-authored compressions, not independently verified state. Partial-overlap folding can discard an uncovered part of an older summary; L_t omits the external execution result and simplifies ordinary actions without coordinates or text; memory operations consume a trajectory step. These limitations must remain visible in the later adapter-specific reconstruction and report. No prompt filtering, outcome label, online rubric, runtime correction, or Sentinel intervention was introduced.
Owner approval required: no (owner explicitly requested MemGUI code preparation, model download, deployment preparation, and the later formal collection)
Resource note: code preparation and tests were CPU-only and did not touch GPU, Docker, emulator, or raw collection data. The active GUI-Owl formal run and all other users' GPU processes were left unchanged.
```

```text
Date: 2026-08-22 UTC
Execution: downloaded and pinned public checkpoint `lgy0404/MemGUI-8B-SFT` revision `7c167054fe55512a923f6bdea873e99361d588b0` into `/shared/huggingface/hub/models--lgy0404--MemGUI-8B-SFT/snapshots/7c167054fe55512a923f6bdea873e99361d588b0`. The snapshot contains exactly 20 resolved files and no incomplete download. All four safetensors shards match their official byte counts and SHA256 values: `babc088f36ff0cf5d2bc14c07a0bb8617e6f188fea6fba56a983c61eb4f6cadf`, `65824065b0e803ccb74148c35652f1903d1f011ec94b8249f969ea5c017e1dbf`, `b04188b0c088a0c929f88a94597caa1c30a4bb74ebd65a0a149a7c145c160a0f`, and `d4f0acfebaa3c6dcfbbbea9bcfd28bb5356cdef341eedcb9fa09154b123733bd`. All non-LFS files match their fixed-revision Git blob IDs; 11 JSON files parse without duplicate keys; 750 tensors have exact index/shard coverage and contiguous, non-sparse payloads; the index metadata total and reconstructed tensor payload total both equal 17,534,247,392 bytes. No download lock is held.
Deployment boundary: this phase downloaded and verified weights only. It did not stop GUI-Owl, bind port 18007, allocate MemGUI on GPU7, operate Docker, or touch any other user's process. MemGUI deployment remains gated on the active GUI-Owl 117-task run completing cleanly, followed by an exact owner/PID/model/port guard that stops only the linqiang-owned GUI-Owl vLLM. A real model-format probe and a fresh-container Collector smoke must pass before a separate fresh formal 117-task run.
Owner approval required: no (owner explicitly requested the fixed model download and subsequent guarded deployment)
```

```text
Date: 2026-08-23 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
GPU4 deployment: the owner explicitly chose a separate GPU4 stack so the still-running GUI-Owl collection on GPU7 would not be interleaved. MemGUI is served from immutable checkpoint revision `7c167054fe55512a923f6bdea873e99361d588b0` as `MemGUI-8B-SFT` on `127.0.0.1:18004` in tmux `memgui8b_sft_g4_server`, using vLLM 0.11.0, BF16, max model length 32768, one sequence, and GPU memory utilization 0.24. The MemGUI EngineCore PID is `213881`; the pre-existing ziqiang GPU4 process PID `35708` was not changed. The GUI-Owl GPU7 model, runner, Docker container, model/environment ports, and raw run were left running and unchanged.
Smoke result: an independently named, owner-labelled, no-GPU-device Docker container used localhost ports `6940/7040/7140/7240` and produced run `01M0P4DHSV5XBXAV2JJ3DEXPBG`. All three task streams completed with capture_complete=true, no missing artifacts or collector errors. The read-only integrity check returned valid=true with 0 errors, 0 warnings, 175 events, 28 requests/terminals/decisions, 223 verified blobs, and 0 orphans. All 28 requests used temperature 0.0, system-plus-user messages, exactly one current screenshot matching S_t, and exact MemGUI H/L/M reconstruction; all accepted provider texts matched P_t. There were no provider failures, parser retries, UNKNOWN/ENV_FAIL decisions, or state-correlation failures. Twenty-five step-two-or-later folds were valid (24 step-level and one span-level), including one real destructive-overlap replacement. H was nonempty in 22 requests and L in 25; this smoke did not produce a nonempty M, whose state transitions remain covered by the final adapter tests. Scores 1/0/0 were model outcomes, not collection failures. The smoke manifest recorded CLI scale_factor 999, but MemGUI's fixed internal 1000-coordinate mapping made this behaviorally inert; the formal run corrects the manifest setting to 1000.
Formal run: the exact smoke container was stopped and auto-removed, and a second fresh container `lq_mw_memgui8b_sft_gui117_g4_0` was created from `sha256:081fd323e3d9e3b9b52fdc67ea1a78fcd3d900465c082c6a8561c6593a0152fd`, with owner/purpose labels, `mwnet`, the same MemGUI-only localhost ports, the simulated-user environment mounted read-only, and DeviceRequests=null. Its GUI-only catalog is exactly 117 tasks with canonical order SHA256 `c87a1d579dfa6cfcb1a690bc967599fc320e1034b1e7fe33067c54de2843a9cf`. Formal run `01M0P576JF73HFWD85PEKNP35V` started in background tmux `memgui8b_sft_gui117_g4` with max-round 50, concurrency 1, auto-retry 9, exact `/v1` model endpoint, scale_factor 1000, MCP/user-interaction disabled, and stream chunks enabled. Initial tripwires show 117 tasks, attempt 1/10, the canonical first task, exact model/image/data roots, and a first-attempt accepted step-one MemGUI response at temperature 0.0. Data root: `/shared/linqiang/mobileworld_audit_data/memgui_8b_sft_gui117_g4_20260823_01`.
Owner approval required: no (owner explicitly authorized the isolated GPU4 Docker/model deployment and formal 117-task collection while preserving GPU7)
```

```text
Date: 2026-08-23 UTC
Commit: dirty working tree based on 7fb9cc4c5265283b2dd2403d8160c0facae725f6
GUI-Owl formal completion: run `01M0NK5E8YZM21NA5WZ7Q3A9MD` ended naturally with 117/117 result files, 34 score1 and 83 score0. The immutable raw run contains 118 task streams because canonical task 70 `MastodonShareLocationTask` had one capture-complete crashed attempt at step 12 after a transient emulator-unhealthy transition and then one completed whole-task retry. All 118 task streams remain capture-complete with empty missing-artifact and collector-error lists. The official read-only integrity check returned valid=true with 0 errors, 0 warnings, 25,007 events, 4,129 model requests/terminal responses/decisions, 21,181 verified blobs, and 0 orphan blobs. Fingerprints: manifest.start `4148fa13e55e92468ceda207441c171cb4bb72b0d2ca3812e50bfe43e9e60e16`, manifest.final `c0be51097b955c9f5a230ec3b3aa22eb70832179fa62375b6e558cd998bdad8c`, run.events `8c8397967e4675ce90af09fd35597c63c20b25068df47b11966b5ae57801ed7d`.
Finding/deviation: the retry reused one GUI-Owl agent instance, so task70 attempt2 inherited the 12 accepted collapsed-history entries from attempt1. Raw capture is lossless, but selecting only that completed retry would create a cross-attempt history stratum. The original raw run was not changed. A fresh task70-only run `01M0PH151N6976HNPFPHTC9NGC`, task stream `01M0PH1536FDM3TQJ10867GYZY`, was collected in a new emulator/agent with auto-retry disabled. It completed 50/50 steps in one attempt, with no model/adapter retry, UNKNOWN action, transition failure, missing artifact, or collector error. Step one had zero prior history and every later request contained exactly Step1 through Step(i-1), with one current screenshot. Its integrity check returned 0 errors, 0 warnings, and 0 orphans. Fingerprints: manifest.start `d38b084f87bb0113b5233bbd005c8605ee50a4e34299d9e7d302d4b61b05f622`, manifest.final `118d84518d58377ec7e44d894b474f43e68a925b57c13902fb8a6a15f97e3d1f`, run.events `31e6b6206ba51c8b47676f38d8a1521d085b881a08e35d06075cb37127842815`, task events `6c6a69ce912b3364ddfc7b6e5967a48141cfbf782176e64c8d085406c6d81562`.
Derived selection: `src/mobile_world/offline/curated_composite.py` now supports an explicit, repeatable, fail-closed task-source pin while retaining the prior exactly-one-eligible-stream rule for every unpinned task. The zero-copy curated set `/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_curated117_20260823_01` selects 116 original formal streams plus only the clean task70 stream. Its manifest SHA256 is `d21000e371f2153595f2b19fa295052533e8f37a155a4fc934f4313518d0e018`; independent validation returned valid=true, no warnings, 117 tasks, one exact pin, 62,009 BlobRef occurrences, 21,155 unique source-local blobs / 3,555,961,043 bytes, and all selected stream/blob SHA256 checks true. Neither old task70 stream is selected.
Offline reconstruction: GUI-Owl uses `representation_type=hybrid_folding` and `mapping_status=exact_gui_owl_collapsed_history_n1`. The adapter-specific mapper exactly rebuilds every cumulative `StepN` conclusion, aligns its external Tool response with observation N+1, and proves that each request contains only the current screenshot by decoded RGB equality. The shared card schema, review labels, MHR/MHR-OH rules, severity derivation, pass2 selection, and metrics were not changed. Real full-data validation reconstructed 117 tasks, 4,117 decision steps, 4,000 history-bearing requests / unique exposed step-level entries, and 89,313 exact source-to-later-prompt appearances. The deterministic outcome-blind retrieval subset contains 181 candidates across 57 tasks, capped at four candidates per task; complete exposure records remain in reconstruction. These counts are retrieval/exposure facts, not misleading or harmful labels.
Derived outputs: outcome-blind cards at `/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_motivation_audit_v1_20260823_01/cards`; manifest SHA256 `d55b4175af0142ea4df8a1980c5cdd1be4002b0b7bb969f3eb9056354d8a4e45`; task cards `63e3458958f0dfc5e890cfcf3513343b93958701a72beb7089b69784703035dd`; reconstruction refs `035dc198ea9996e8f74a8722a9d67863086f1bf9114f5ff340e146336b6c3570`; isolated outcomes sidecar `f19c432cf6a13581bc6836ed8812a43f918efcee9d8ef1397879ae111cf6ba66`. Driver dry-run resolved 724 candidate images and 117 singleton PASS1 batches, wrote zero files, did not invoke Codex, and returned outcomes_opened=false. Combined verification passed 587 full-suite tests and 119 offline tests; targeted Ruff, format, and diff checks passed; independent review reported no blocker.
External-processing boundary: the GUI-Owl review launch was not executed because the owner has not yet explicitly authorized sending these GUI-Owl evidence cards and selected screenshots to OpenAI Codex. No review directory, tmux log, or GUI-Owl review session exists, and no GUI-Owl evidence was sent externally. A destination-specific authorization is required before first launch; that launch must not use `--resume`.
Owner approval required: yes, only for the external OpenAI Codex evidence-card/screenshot transfer and review launch. Local raw processing, clean rerun, zero-copy selection, cards construction, and dry-run were explicitly requested and are complete.
```

```text
Date: 2026-08-23 UTC
Authorization/execution: the owner explicitly authorized sending the frozen GUI-Owl-1.5-8B-Instruct outcome-blind evidence cards and selected screenshots to OpenAI Codex. Outcome-blind review `review_v1` was started in background tmux session `guiowl15_8b_motivation_audit_v1` with gpt-5.6-terra for singleton PASS1, gpt-5.6-sol for PASS2 and the seeded 15% negative audit, and gpt-5.6-sol for material-disagreement adjudication. The first launch intentionally did not use `--resume`.
Input tripwires: curated manifest SHA256 `d21000e371f2153595f2b19fa295052533e8f37a155a4fc934f4313518d0e018`; cards manifest `d55b4175af0142ea4df8a1980c5cdd1be4002b0b7bb969f3eb9056354d8a4e45`; task cards `63e3458958f0dfc5e890cfcf3513343b93958701a72beb7089b69784703035dd`; reconstruction refs `035dc198ea9996e8f74a8722a9d67863086f1bf9114f5ff340e146336b6c3570`; isolated outcomes sidecar `f19c432cf6a13581bc6836ed8812a43f918efcee9d8ef1397879ae111cf6ba66`. The pre-launch dry-run returned 117 tasks, 117 singleton PASS1 batches, 724 resolved candidate images, zero files written, and outcomes_opened=false.
Initial health: the tmux pane and Python driver are alive, and six singleton PASS1 receipts (`c001`--`c006`) are accepted. The first c001 response violated the lexicographically sorted unique evidence_ref_ids invariant; it was preserved under rejected/ and accepted on attempt two after hashed validation feedback. The remaining five initial batches passed on attempt one. All seven Codex invocations returned code zero, with no retry exhaustion. No frozen PASS1, outcome selection, PASS2, adjudication, or final artifacts exist at this checkpoint; therefore outcomes have not been opened. The review continues unattended at `/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_motivation_audit_v1_20260823_01/review_v1`.
Owner approval required: no (explicit GUI-Owl external-processing authorization was given in this thread)
```

```text
Date: 2026-08-23 UTC
Resource release: after the MemGUI formal collection completed, the owner requested releasing every linqiang GPU4/GPU7 allocation. Exact tmux pane, PID/UID/PGID/SID, model snapshot, served name, and localhost listener guards identified only `memgui8b_sft_g4_server` as a remaining linqiang GPU service. A Ctrl-C was sent only to that pane. MemGUI API/resource-tracker/EngineCore PIDs `213288/213878/213881` exited, port 18004 closed, and the tmux session disappeared. GPU4 now contains only ziqiang PID `35708`; it remained alive and unchanged. GPU7 already had no linqiang compute allocation, so no GPU7 process was signalled; taoz PIDs `229261` and `140001` remained alive and port 18007 remained closed. The CPU-only MemGUI formal container `lq_mw_memgui8b_sft_gui117_g4_0` was deliberately preserved because DeviceRequests is null, and the CPU-only GUI-Owl Codex review tmux remained alive. No broad pkill, GPU-wide clear, shared tmux-server stop, Docker stop, or other-user action was performed.
Owner approval required: no (owner explicitly requested releasing our GPU4 and GPU7 usage)
```

```text
Date: 2026-08-23 UTC
GUI-Owl review completion: outcome-blind `review_v1` ended normally. PASS1 accepted 117/117 tasks in 124 Codex calls; seven schema/validation-invalid first responses were preserved and all recovered on attempt two, with zero retry exhaustion. PASS1 froze before outcomes were opened. PASS2 accepted 25/25 tasks in one call each: eight positive/uncertain reviews plus a seeded 17-task negative audit. Ten material disagreements were adjudicated in 10/10 successful calls. Outcome fields were never supplied to a reviewer.
Final result: 113 NEGATIVE, 3 POSITIVE, and 1 UNCERTAIN over 117 tasks / 181 reviewed candidates. The shared metrics record four strict-explicit candidate chains and four strict-harm chains, representing one unique MHR task and the same one unique MHR-OH task, all in the 83-task failure stratum; the 34-task success stratum contains zero MHR/MHR-OH tasks. Motivation strength is `MODERATE_OBSERVATIONAL`; `causal_claim_supported=false`. Final summary/reviews/metrics SHA256 values are `fe6a2d2def68f970bc9a4564289749b7259141bca38619d386685d42c3ac0724`, `33816f1d0821c796330f484f1a017d9bc2830bd7d962fb6ad22bec2a8fbe60ec`, and `54618fe0ac4b0226a3a0ce15468f4f83cc0735568185938e213b5e7b34710fe3`. PASS1 and secondary review SHA256 values are `da1d4f419b0a438a98a500b1c3fa81a2d1d4829ce71af4e6f11821dfe7824121` and `dff69c44dfb4bfe6193ee8425744cec276a0207d4107b6d3d9434eeffa4f65d6`. The tmux session and all reviewer processes exited naturally after final artifact publication.
Owner approval required: no (the explicitly authorized GUI-Owl review is complete)
```

```text
Date: 2026-08-23 UTC
Superseding correction: the preceding GUI-Owl review result is retracted and must not be reported or combined with other model metrics. The generic claim-typing heuristic recognized only a narrow English action vocabulary, so Chinese imperatives and English forms such as Drag/Ask/Uncheck were frozen as OBSERVATION_CLAIM instead of action-history records. The validator then prevented reviewers from returning NOT_A_FACTUAL_CLAIM for those frozen types. All ten REFUTED chains in the old review came from this typing error, including the four chains that produced its sole strict MHR/MHR-OH task. The old `review_v1`, its tmux log, and the invalid cards were permanently removed after exact-path guards; raw formal data, clean task70 raw data, and the curated117 manifest were not changed.
Corrected semantics: GUI-Owl collapsed history is audited as completed action records, not as generic state observations. Pure completed-action imperatives are ACTION_EXECUTION_CLAIM; explicitly prospective text is ACTION_INTENT; an independently retrieved explicit completion/progress assertion remains SUCCESS_CLAIM. Action-record validity is grounded in the exact Action conclusion, parsed A_i, and matching action_execution_started copy. An accurate but off-task action is TRUE_BUT_OFFTRACK/OFFTRACK_TRUE, not a false history claim; a text-versus-executed-action mismatch is an outcome-blind retrieval signal and may be REFUTED/RESULT_MISALIGNMENT after review. The separately aligned next-observation tool/ask-user result remains external evidence and is never merged into the actor-authored claim. Shared review labels, MHR/MHR-OH formulas, severity, outcome blinding, and pass2/adjudication policies are unchanged.
Implementation/verification: motivation_cards.py SHA256 `d3b085331ef3dc604ada57cac1f4c24489a2a3ed83d3dfdc0339c83dadc316f4`; motivation_prompt.py `fcd06f53ffeb38883776f34514d0891d12e1558f9fabce8cd45180b4d73fc8a9`; motivation_review.py `08731658b234a9543d0f7973afd61931fcddac5850eac6d040e7a6b017ed1244`; review driver `4176c71eaef8dd5a25ce6b5967b425d3479362420d161bcb3470ed9fc8ae6ffd`. Fresh reviews use prompt v3; compatible v2/legacy-v1 receipt paths remain validated. Targeted tests passed 178, offline tests 207, full MobileWorld tests 675; Ruff, format, and diff checks passed; independent review reported no P1/P2 blocker.
Corrected cards: the canonical cards path now contains 117 tasks, 4,117 steps, 4,000 history-bearing requests, all 89,313 exact cumulative history appearances, and 474 outcome-blind review candidates covering all 117 tasks. All selected claims are ACTION_EXECUTION_CLAIM. Twenty-nine high-confidence action-text/execution mismatches across ten tasks are retained as retrieval candidates, not preassigned MHR or harm labels. Corrected manifest SHA256 `3c17deac3fb35b7573f86671b107baa06ee8f9ae94af544b7a2fb548fbe81808`; task cards `78db3fe06dcdda4eb2bb5d6cdfb330b3aac21e32c60a60b6a5806eb816f30366`; reconstruction refs `10ca3c29a44e2abff8426584038f67d8ad2f1d0cf927e152270a2cb8e1dd65ac`; isolated outcomes sidecar `f19c432cf6a13581bc6836ed8812a43f918efcee9d8ef1397879ae111cf6ba66`. The corrected dry-run returned 117 singleton PASS1 batches, 1,892 resolved candidate images, zero files written, and outcomes_opened=false. A fresh empty-root v3 review is required; none of the deleted v2 labels or receipts may be resumed or seeded.
Owner approval required: no (the owner explicitly requested deletion of the invalid review, correction, and fresh re-audit under the prior destination-specific OpenAI Codex authorization)
```

```text
Date: 2026-08-23 UTC
Fresh GUI-Owl v3 re-audit launch: after the corrected-card dry-run passed and the old review/cards were absent, a new empty-root review was launched in background tmux `guiowl15_8b_motivation_audit_v1`. The command does not contain `--resume` or any seed directory. The run manifest SHA256 is `76bbdea92ab1b9c0312afd76558b7d6cad8d16b37166ef08bfbc5a6cf8f6dbd5`; it binds the corrected task-cards SHA256 `78db3fe06dcdda4eb2bb5d6cdfb330b3aac21e32c60a60b6a5806eb816f30366`, task_count=117, batch_size=1, prompt version `mobileworld.audit.motivation-codex-prompt/v3`, gpt-5.6-terra PASS1, gpt-5.6-sol PASS2/adjudication, seeded-negative rate 0.15, and max_attempts=3.
External-processing guard: Codex is invoked with `exec --ephemeral -s read-only --ignore-user-config --strict-config`, and the manifest disables apps, browser/computer use, shell/unified exec, image generation, view_image, plugins, multi-agent, workspace dependencies, and the other declared reviewer features. Reviewer payload is limited to one outcome-blinded task card and its digest-bound selected screenshots; reconstruction/raw streams and outcomes are not reviewer inputs.
Initial health: the first two singleton PASS1 batches were accepted on attempt one with return code zero and no validation error. The first receipt SHA256 is `6c3966215dea8c3862f1a3997f0eca05c28aa1e7cd75ec9d52c35d2e9057789e`; it records prompt v3, accepted_attempt=1, and matching normalized/model-response SHA256 `857d9dca0fceb4135d306636ee9e7da2e536556d535faeeb00f11d3fbc1b665c`. PASS1 freeze, outcome selection, PASS2, adjudication, and final artifacts are all absent at this checkpoint, so the outcomes sidecar has not reached its sole open point. The corrected review continues unattended at `/shared/linqiang/mobileworld_audit_data/gui_owl_1_5_8b_gui117_g7_motivation_audit_v1_20260823_01/review_v1`.
Owner approval required: no (the owner explicitly authorized the corrected GUI-Owl re-audit and prior destination-specific evidence transfer)
```

```text
Date: 2026-08-23 UTC
Corrected GUI-Owl v3 review completion: the fresh review ended normally and supersedes every deleted v2 label and metric. PASS1 covered 117/117 tasks; PASS2 independently reviewed 61 tasks (all 51 primary-positive tasks plus a fixed-seed 10-task negative audit); all 58 material disagreements were adjudicated. Outcome fields were opened only after PASS1 freeze and were never supplied to reviewers. Final screen counts are 69 NEGATIVE, 48 POSITIVE, and 0 UNCERTAIN over 474 reviewed action-history candidates. The final summary/reviews/metrics SHA256 values are `da886033ab3fdf4d4d7c53092f6669fac72b126d17e3d692358a825496580fac`, `435a180b0694486f841e4aac67a4690819af56e9f6b98bb494479928435f7923`, and `f7ce46030a7948ba1f179f7245de8f6f9c73e6f70df380a1eb412aa441002f88`.
Corrected strict result: 39 primary-invalid action-history chains occur across 18 tasks, while 98 accurate-but-off-track action records occur across 33 tasks. Twenty-seven chains across 11 tasks satisfy the exact-provenance, actually-injected, EXPLICIT_USE, low-confound MHR gate; 23 chains across 7 tasks additionally have an observed harmful effect and therefore satisfy MHR-OH. Task rates are 11/117=9.40% MHR and 7/117=5.98% MHR-OH. In the 83-task failure stratum, 9 tasks are MHR and 7 MHR-OH; in the 34-task success stratum, 2 are MHR and 0 MHR-OH. Strict harmful effects overlap: REPEATED_ACTION 23, WRONG_ACTION 5, OFFTRACK_CONTINUATION 4, and RECOVERED 1. Motivation strength is STRONG_OBSERVATIONAL and causal_claim_supported=false.
Action-alignment result: of the 29 outcome-blind ACTION_EXECUTION_MISMATCH retrieval candidates, 25 are finally REFUTED and 4 SUPPORTED; 18 satisfy strict MHR and 16 satisfy strict MHR-OH. Other strict findings arise from false embedded action effects or directional/purpose assertions even when the low-level action copy itself matches. One task, AdjustFontIconMaximumTask, contributes 15 of the 27 strict instances because all high-confidence mismatches are deliberately retained beyond the ordinary four-card task budget; task-level prevalence therefore remains the primary cross-task statistic. These results audit completed action-history records rather than misclassified observation claims and must be used instead of the retracted v2 result.
Owner approval required: no (the owner explicitly authorized the corrected GUI-Owl re-audit)
```

```text
Date: 2026-08-23 UTC
MemGUI review completion: the structured-H/L/M outcome-blind audit completed normally over all 117 task cards and 302 reviewed candidates, with no incomplete task. PASS1 covered 117/117 tasks; PASS2 independently reviewed 62 tasks (all 52 positive/uncertain screens plus a fixed-seed 10-task negative audit); all 56 material disagreements were adjudicated. Outcome fields were opened only after PASS1 freeze and were never supplied to any reviewer. Final screen counts are 69 NEGATIVE, 46 POSITIVE, and 2 UNCERTAIN. The final summary/reviews/metrics SHA256 values are `c6b3788ac50c607313a0e540af9ffa018e548b01a93e5c74a2064d82ae630b86`, `eb4f3f36f8bf61028d9da699aafa9e31d1a96b397dd43ca4099e341e96939a67`, and `f36bac25179780488647ce3f93b8098bc183f124ac9a465b66bd643e1dc8251e`.
Strict result: 77 primary-invalid candidate chains (72 REFUTED and 5 STALE) occur across 45 tasks. Thirty-eight chains across 27 tasks satisfy the exact-provenance, actually-injected, EXPLICIT_USE, low-confound MHR gate; 27 of those chains across 18 tasks also have an observed harmful effect and therefore satisfy MHR-OH. Task rates are 27/117=23.08% MHR and 18/117=15.38% MHR-OH. In the 95-task failure stratum, 24 tasks are MHR and 17 MHR-OH; in the 22-task success stratum, 3 are MHR and 1 MHR-OH. Harm effects overlap: REPEATED_ACTION 18, OFFTRACK_CONTINUATION 11, WRONG_ACTION 10, PREMATURE_TERMINATION 4, UNNECESSARY_ACTION 4, and RECOVERED 1. Motivation strength is STRONG_OBSERVATIONAL and causal_claim_supported=false.
Representation result: all 12 selected structured-memory versions were reviewed (10 SUPPORTED, 1 OFFTRACK_TRUE, 1 UNVERIFIABLE; zero REFUTED/STALE and zero strict MHR), so confirmed strict findings are not driven by M. Of 117 selected span-fold H candidates, 25 are primary-invalid, 14 satisfy strict MHR, and 10 satisfy strict MHR-OH. Across all strict instances, claim types are 17 SUMMARY_CLAIM, 19 OBSERVATION_CLAIM, and 2 SUCCESS_CLAIM, showing that both folded H summaries and recent-step L observations contribute. These are observational natural-trajectory results, not causal estimates, and the 302-card bounded review set remains a conservative subset while all 27,660 exact H/L/M appearances remain in reconstruction.
Owner approval required: no (the owner explicitly authorized the MemGUI evidence transfer and background review)
```

```text
Date: 2026-08-24 UTC
Six-model report consolidation complete: `MobileWorld/docs/misleading_history_audit_report.md` now contains independent sections for MAI-UI-8B, Qwen3-VL-8B, GELab-Zero-4B, UI-Venus-1.5-8B, corrected GUI-Owl-1.5-8B-Instruct v3, and MemGUI-8B-SFT. Each model section has an explicit previous-history representation subsection plus task-level, reuse-instance, prompt-persistence, and real-example evidence. The six frozen representation families are raw_replay, flat_progress, rolling_summary, flat_previous_actions, hybrid_folding (reader-facing hybrid collapsed), and structured_folding. The report uses only the corrected GUI-Owl action-record v3 metrics; the retracted v2 1/117 result remains excluded.
Validation: the Markdown has exactly six model headings and six instances of each standard subsection. All 39 canonical screenshot references exist and their content SHA256 equals the extensionless path basename. The rendered PDF completed two XeLaTeX passes, has 29 pages, no LaTeX error or overfull box, and passed full-page raster inspection. Markdown SHA256 is `068117d23c4f8f1340f3c23c6796652bdd3df81741fb13cef5d983af469085bf`. The published PDF is `/shared/linqiang/mobileworld_audit_data/reports/misleading_history_audit_report_20260824.pdf`, 16,099,969 bytes, mode 0600, SHA256 `a399ceebd33e9aefa1a28b2b27b2ea878bb7712fbe165497898da20eaf1e1776`; the earlier dated three-model PDF was preserved unchanged.
Owner approval required: no (the owner explicitly requested that all six models and their previous-history handling be added to the existing report)
```

```text
Date: 2026-08-24 UTC
Six-model report direct-terminal proxy update: the reader-facing definitions now freeze a conservative `direct-terminal failure-linked proxy`, requiring strict MHR-OH, PREMATURE_TERMINATION, target equal to the final decision, an actual finished/answer action, evaluator-reason alignment, score 0, and no RECOVERED label. This proxy is explicitly observational, excludes earlier unrecovered divergence and max-step exhaustion, and is not a counterfactual causal fraction. Per-model failed-task results are MAI-UI 0/86, Qwen3-VL 9/109=8.26%, GELab 0/99, UI-Venus 0/102, corrected GUI-Owl v3 0/83, and MemGUI 2/95=2.11%; pooled across 574 failed model-task trajectories the proxy is 11/574=1.92%. The nine Qwen and two MemGUI task names are recorded in their model sections. The conclusion now also states explicitly that neither MHR nor MHR-OH necessarily causes final task failure.
Validation/publication: the report still has 39 canonical screenshot references and all content hashes match their path basenames. Two-pass XeLaTeX rendering produced 29 pages with no LaTeX error or overfull box; all 29 rasterized pages were visually checked. Updated Markdown SHA256 is `a67248d07b62503a3a4df6cd126ad506de5c1cf8534b53ade48c637d6bd5d248`; the updated mode-0600 PDF at `/shared/linqiang/mobileworld_audit_data/reports/misleading_history_audit_report_20260824.pdf` is 16,105,309 bytes with SHA256 `b8a0e8256372cda539b35df96818ea16af6f9263d7e0179be114ad1f6cdbd4da`.
Owner approval required: no (the owner explicitly requested that this per-model 1.92% direct-terminal proxy be recorded in the report)
```

```text
Date: 2026-08-24 UTC
Six-model failure-link Phase A launch: the owner authorized sending the broader outcome-aware review evidence to OpenAI Codex. The workflow is deliberately split: Phase A reviews all 116 frozen strict-MHR tasks / 272 strict chains (239 MHR-OH and 33 non-OH) without opening outcome, score, evaluator, or Phase-B data; Phase B may start only in a separate process after the complete primary/secondary/material-adjudication Phase-A freeze is hash-verified. Eight successful control tasks remain in the Phase-A population and will remain in Phase B. All reported attribution remains observational and `causal_claim_supported=false`.
Provider preflight correction: an initial isolated `_01` launch failed on its first unit because the Phase-A Structured Output schema used const/enum leaves without explicit scalar `type`, producing provider `invalid_json_schema`. That driver was stopped, no outcome was opened, and `_01` is retained as immutable failure evidence and must never be resumed. Both A/B schemas were corrected, recursively linted, and accepted by real synthetic `gpt-5.6-terra` provider smokes before relaunch; the provider-smoke responses also passed the formal local validators. Final core SHA256 is `c4ca3140aea1744d8e7d4e40b1561815d22b4f53a112db532701abc3387c252e`; Phase-A review-schema SHA256 is `057c448fa0499425d7035170893386560be8aa9d7f86738e3d09bfd3393781ec`. Focused tests passed 35 and the full MobileWorld suite passed 710; independent core and driver reviews both returned GO with no P1/P2.
Fresh formal launch: outcome-blind Phase A now runs in tmux `six_model_failure_link_phase_a_v1`, output root `/shared/linqiang/mobileworld_failure_link_audit_data/six_model_failure_link_audit_v1_20260824_02/phase_a`, with no `--resume`. Frozen run/input/card/schema SHA256 values are `1ace1569aa16cbb70900d953b71634119f71d883f745efafb1d0945ac755ec36`, `cb3221f0cea82b42dedb31c82e2860b9c57ba6a2970d626ad0b1df36706d3582`, `632e321ae683e9d0129443e525564710166374dc6bab3b0bc71ff85b3a6ff362`, and `057c448fa0499425d7035170893386560be8aa9d7f86738e3d09bfd3393781ec`. Codex uses Terra/Sol/Sol, singleton reviews, three attempts, 1800-second timeout, `--ephemeral`, read-only sandbox, ignored user config/rules, strict config, and 23 disabled external capabilities. The first formal unit, `gelab_zero_4b/CheckGithubInfoTask`, was accepted on attempt one with return code zero; its 38 images, card, prompt, schema, response, and receipt bindings independently rehashed and validated. The audit continues in the background; Phase A freeze, Phase B, outcome opening, and final failure-link rates do not yet exist.
Owner approval required: no (the owner explicitly authorized this destination-specific Phase A/B failure-link evidence transfer and background review)
```

```text
Date: 2026-08-25 UTC
Epic 1 completion: the six-model observational audit and the outcome-aware failure-link review completed and supersede the preceding launch-only checkpoint. Across 702 model-task cases, 128 succeeded and 574 failed. Strict MHR was identified in 116 cases / 272 chains: 8 successful cases and 108 failed cases. Ninety-four of the 116 MHR cases (239 chains) had an observed local-harm attribute; local effect types overlap and are not themselves final-failure attribution.
Outcome-aware review: the final immutable artifacts are under `/shared/linqiang/mobileworld_failure_link_audit_data/six_model_failure_link_audit_v1_20260824_03`. Phase A remained outcome-blind over all 116 cases / 272 chains; its resolution manifest and final-review SHA256 values are `865bf9ab51bf46f8882deb06ea9dd35cb2a963d2c810898ef2cb37ff90555bb8` and `d3f02da3f83eb4ec5b67f24de7de0d6f1b15c14bb7fb0638608643e96e0f1154`. Phase B then opened only the digest-bound terminal/evaluator evidence; its resolution manifest, final-review set, and metrics SHA256 values are `bb0d1c3e8a81d7a805a39438d62c71d131873ba717a1e833e689ea371edffe18`, `c69fe469fb0380f4900fcd7d17138bf73a5e74a37ede0444553d07ffe83db54f`, and `f5b4400ce6c7d6955e3aa67271c5303d4adb46df9434afb9a652cc5bddfce536`. The internal observational failure-link set contains 60/108 failed MHR cases; `causal_claim_supported=false` throughout.
Reader-facing result: the report applies a stricter, task-level, mutually exclusive presentation to those reviewed paths: 10 failed cases show an explicit final-decision direct stop, and 48 show an earlier unrecovered indirect derailment, for 58/108 failed MHR cases (53.70%) and 58/574 total failures (10.10%). Two GELab parser/runtime boundary cases are kept separate rather than forced into either category. These counts are observational links, not proof that MHR alone caused failure or that removing history would improve task success.
Final publication: `MobileWorld/docs/misleading_history_audit_report.md` SHA256 is `a057b3dda627cf9ff6c474b0cd4bc0df198b13c5fa50fe9229b94df6e9edbbab`. The self-contained PDF remains repo-external at `/shared/linqiang/mobileworld_audit_data/reports/misleading_history_audit_report_20260825.pdf`, 16,129,136 bytes, SHA256 `2b714a8641d34d607b1876aec39d77789c32220f3a9ef78631c2f195309366a8`; the Git report source intentionally keeps content-addressed external screenshot references rather than copying raw screenshots into the repository.
Owner approval required: no (this records completion of the explicitly authorized Epic 1 audit and report)
```

```text
Date: 2026-08-26 UTC
Phase authorization: the owner stated that Epic 1 is complete and explicitly requested implementation of ALE-319 / G1.1, the first story of Epic 2. This authorizes only offline, derived, state/request-frozen causal replay whose first endpoint is the original actor's next structured action. Qwen3-VL flat-progress is the primary host and MAI-UI raw replay is the replication host.
Locked boundary: Collector v1 remains immutable, passive, zero-intervention, and label-free. G1 artifacts may read frozen raw and derived evidence but may not alter raw events, natural prompts, actor actions, GUI/backend state, or collector behavior. Automatic claim verification, online rubric tracking, runtime interception/filtering/correction, action execution, full-task branching, and treatment-response-driven case selection remain out of scope. Every G1.1 record is deployment_prediction=false, and no treatment model response is generated in this story.
Decision record: D-021 in DECISION_LOG.md. G1.2 and later execution remain blocked until the versioned protocol, schemas, deterministic pre-treatment registry/ledger, model/config manifest, locked analysis plan, input hashes, and dry validation report are frozen. Independently curated transformations and accepted next-action sets remain the downstream G1.6 admission gate; G1.1 must not fabricate them from the observed natural action.
Owner approval required: no (this entry records the owner's explicit phase authorization)
```

```text
Date: 2026-08-26 UTC
ALE-319 / G1.1 completion: the CPU-only, immutable pre-gold causal-replay protocol and candidate registry are frozen and formally published. D-021 preserves Collector v1 as immutable, passive, zero-intervention, and label-free. Qwen3-VL flat-progress remains the primary host and MAI-UI raw replay remains the replication host. The protocol, locked analysis plan, model/config manifest, 19 G1 schemas, deterministic builder/CLI/tests, source configuration, registry, ledger, arm catalog, and dry-validation contract are hash-bound before any treatment response exists.
Formal external publication: `/shared/linqiang/mobileworld_causal_replay_data/g1_1/registry/sha256/dd3dad4f94c66dce6999d3cc2743cd75c37688788754e95b27531cfd00d733f4`. The full directory basename is the SHA-256 of its 9,911-byte `registry_manifest.json`. It contains exactly six regular files, no symlink or subdirectory, mode 0555 with files mode 0444, owner/group `linqiang:linqiang` (uid/gid 1035), and 3,103,332 total bytes. The six-file aggregate SHA-256 is `dbec86f012b1cb9a11f94123cb302a62ffc6a04a33422121d190f28edf793bc6`; the separately named 25-file registry-contract aggregate is `f1e23239896eb7f6487e337ec391df73d19c84fababecae996c0a2e752f156d8`.
Repository publication lock: `mobileworld_audit_handoff/g1/registry.lock.v1.json`, 6,384 bytes, SHA-256 `1e038ffe604acf0eae2af1e45ec0e856e2f105353b0c5a1dbea0da9b15657944`. It pins the external absolute root, all six file hashes and byte counts, ownership/modes, source config SHA-256 `c8235705c575e134c11bc00896f31ec95243af4ffd2ffd47a3e6ecf64ce5cb59`, protocol SHA-256 `07fd0b64ea68f514e5369033a5f0b2f0c191c95fdb1cfef1b7e6ad4d81badd26`, locked analysis-plan SHA-256 `ecb98d1dfc1d28e496d9d7320054ee19cc222df5b9d7ee00105073dfc786e79b`, and model/config-manifest SHA-256 `7ba840b1b7c7f4539ec9b967a5b4029c3a0e3217f6bb8bc1e9eb7d04687c6c5f`.
Frozen census: 152 strict-MHR pre-gold candidate cases across 42 source-distinct tasks; 76 clean controls split into 38 selected and 38 reserve; 674 ledger records; 0 INCLUDED cases and 0 treatment responses. MAI contributes 13 strict cases / 7 tasks, 23 controls with 8 selected across 7 tasks, and 240 ledger records. Qwen contributes 139 strict cases / 35 tasks, 53 controls with 30 selected across 30 tasks, and 434 ledger records. Every source case remains `CANDIDATE_FROZEN`; only G1.6 may append an INCLUDED or EXCLUDED admission decision, and it may not rewrite the source registry.
Validation: independent CPU builds are byte-identical; the formal external bytes equal the frozen snapshot; CLI readback rebuilt all artifacts from the immutable sources and returned valid=true. All 25 dry-validation checks are true, emitted schemas validate, and unresolved source references, unresolved capsule references, pre-gold future-leakage cases, pre-gold future-leakage controls, treatment responses, and obsolete-model-manifest matches are all zero. The final MobileWorld suite passed 771 tests. Publication uses exact-root validation and write-once installation; concurrent destination replacement cannot authorize cleanup of another directory or unknown child.
Scope and safety: this completion is ALE-319 / G1.1 only. Gold materialization, admission, execution readiness, run readiness, treatment-response generation, provider invocation, generated-action execution, model service use, replay execution, and GPU use are all false. No G1.2+ implementation or execution is included, and Collector/raw data were not mutated. D-021 and the publication lock remain working-tree artifacts until committed and merged, so G1.2 and later stories remain blocked on that authoritative merge. Any future GPU model service or replay requires separate explicit owner approval.
Owner approval required: no for this destination-specific, CPU-only G1.1 publication (the owner explicitly requested ALE-319 implementation); yes for any future GPU model service or replay.
```

## 12. Implementation checklist

- [x] Server repo path identified
- [x] Commit and dirty state recorded
- [x] Nine-agent registry revalidated
- [x] Feature-off baseline fixtures saved
- [x] Raw schema v1 implemented
- [x] Blob store and reconstruction test passed
- [x] Base non-stream capture passed
- [x] Streaming chunk capture passed
- [x] Retry/error capture passed
- [x] UIINS grounder capture passed
- [x] Runner transition capture passed
- [x] Last-step post-state test passed
- [x] GUI/MCP/user execution evidence passed
- [x] Task score/reason capture passed
- [x] Concurrency/context isolation passed
- [x] Credential scan passed
- [x] Integrity checker passed
- [x] MAI-UI-8B server smoke passed
- [ ] Seed server smoke passed
- [ ] Planner+UIINS server smoke passed
- [x] MemGUI server smoke passed
- [ ] Nine-agent compatibility smoke passed
- [ ] Raw schema v1 frozen
- [ ] Offline pipeline started only after collector DoD

## 13. Open questions that do not block Collector v1

- 正式motivation sample使用5–6个representative agents还是全9个的成本分配。
- 人工标注规模、双标比例和adjudicator人选。
- Primary success threshold采用官方 `score > 0.99` 还是旧分析的 `score > 0`；必须在metric manifest固定。
- 是否后续增加emulator checkpoint以支持真正counterfactual branch replay。
- 未来 Sentinel 的 rubric template 已约定在任务开始时由 task 生成并版本化；运行中更新 milestone state，不是每步静默重生成 rubric。允许何时显式发布 rubric 新版本仍留给后续实验；不属于本次 collector 实现。

这些问题不能被用来拖延label-free raw collection，因为raw schema的目的就是让后续定义可改变。
