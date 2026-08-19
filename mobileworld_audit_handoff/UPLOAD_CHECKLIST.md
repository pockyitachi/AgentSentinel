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

Raw audit 数据应写到显式配置的外部路径，例如：

```text
<server-data-root>/mobileworld_audit_data/raw/runs/<run_id>/
```

不要写入：

- AgentSentinel source tree（包括 `MobileWorld/`）；
- Git tracked tests fixtures（除最小脱敏单元测试 fixture）；
- handoff 文档目录；
- 共享临时目录中无法追踪生命周期的位置。
