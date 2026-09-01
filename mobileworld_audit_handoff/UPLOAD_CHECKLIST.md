# GitHub and Server Bootstrap Checklist

## 首次发布前（Mac）

- [ ] 将整个 AgentSentinel monorepo 推送到项目自己的 GitHub remote。
- [ ] 确认 `MobileWorld/` 是新仓库直接跟踪的源码，不是指向别人仓库的空 gitlink。
- [ ] 确认 `examples/sample_events.jsonl` 存在且每行都是独立 JSON。
- [ ] 不上传 API keys、`.env`、真实用户数据或 provider credentials。
- [ ] 若一同上传数据样本，单独标记来源、许可和是否含敏感信息。

## 服务器接收后

- [ ] 使用 `git clone --recurse-submodules <AgentSentinel URL>` 获取整个 monorepo。
- [ ] 只使用该 clone 内的 `AgentSentinel/MobileWorld/`，不使用服务器已有的其他 MobileWorld checkout。
- [ ] 在 AgentSentinel 顶层执行并记录：`git status --short`、`git rev-parse HEAD`、`git remote -v`。
- [ ] 对照根目录 `UPSTREAM.md` 中的 frozen MobileWorld source commit。
- [ ] 不自动清理 dirty worktree；先判断已有修改归属。
- [ ] 创建独立开发分支，例如 `audit-collector-v1`。
- [ ] 阅读 `AGENTS.md` 指定的全部文件。
- [ ] 按 `IMPLEMENTATION_GUIDE.md` 分阶段提交，而不是一次性大改。
- [ ] 先运行 mocked/unit tests，再申请真实模型和 emulator smoke run。
- [ ] 将实际测试与结果追加到 `STATUS.md`。

## 上传/运行产物边界

### Epic 1 owner 公开证据例外（2026-09-01）

- [ ] 只允许 `motivation study/report_assets/screenshots/` 中、正式报告实际引用的精确 39 张
  content-addressed PNG，以及唯一固定文件
  `motivation study/misleading_history_audit_report_20260825.pdf`。
- [ ] 校验 39 张图逐个 hash、总数和总字节数，并由
  `motivation study/report_assets/screenshot_manifest.v1.json` 绑定；Markdown 必须使用仓库内
  相对路径。
- [ ] 确认仓库是公开的，并接受这些 bytes 进入 Git history、fork 与 cache 后无法保证完全收回。
- [ ] 将可见手机号、验证码、姓名、邮箱等标为 synthetic/demo benchmark fixture 内容；不得把它们
  描述为真实账号或可用凭据。
- [ ] 记录第三方 app UI、商标与图片的再分发权未逐项独立核实；本次收录是研究证据发布，
  不是对第三方内容的额外许可。
- [ ] 明确截图中任何医学或健康表述都只是静态 benchmark 内容，不是医学建议或项目背书。
- [ ] 不把该例外扩展到 raw cards、requests、trajectories、model responses、reviewer text、
  receipts、logs、replay 数据或其他 Collector blob。

### Epic 1 owner 最终 failure-link 原始归档例外（2026-09-01）

- [ ] 只允许精确目录
  `motivation study/failure_link_audit_raw/six_model_failure_link_audit_v1_20260824_03/`；已删除的
  `_02` 不在授权中。
- [ ] 校验精确 2,842 个常规文件 / 119,555,475 bytes，以及按相对路径 C locale 排序的
  inventory SHA-256
  `a97f9d4541c339d3cb6782bf499eed61ade9bfe68270419b7a62f500f4aa944a`。
- [ ] 明确归档包含 raw cards/requests、model responses、reviewer text/rationales、
  operational receipts、logs 和 machine-local paths；发布前审查未找到可确认的在用 secret。
- [ ] 确认这是 owner 知情的公开与永久性发布；即使以后删除，bytes 仍可能留在
  Git history、fork、mirror 和 cache。
- [ ] 不修改 v1/v2 publication locks；它们继续记录历史 safe/report-publication
  scope，不将这份 raw archive 追加进旧 lock。
- [ ] 不将这项例外扩展到其他 audit/collection/capsule/replay 数据，也不因发布获得
  model/provider/network/GPU/replay/treatment/GUI/tool/action 权限。

除上述精确 allowlist 外，Raw audit 数据应写到显式配置的外部路径，例如：

```text
<server-data-root>/mobileworld_audit_data/raw/runs/<run_id>/
```

不要写入：

- AgentSentinel source tree（包括 `MobileWorld/`）；
- Git tracked tests fixtures（除最小脱敏单元测试 fixture）；
- handoff 文档目录；
- 共享临时目录中无法追踪生命周期的位置。
