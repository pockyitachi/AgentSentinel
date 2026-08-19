# Instructions for Coding Agents

本目录服务于 MobileWorld pre-step motivation audit。开始任何代码修改前，必须完整阅读：

1. `README.md`
2. `PROJECT_CONTEXT.md`
3. `DECISION_LOG.md`
4. `COLLECTOR_DESIGN.md`
5. `EVENT_CONTRACT_V1.md`
6. `IMPLEMENTATION_GUIDE.md`
7. `TEST_AND_ACCEPTANCE.md`
8. `SERVER_AGENT_INSTRUCTIONS.md`

## 强制范围

当前只实现 **event-sourced、lossless、label-free audit collector**。

必须：

- 保存模型真正收到的最终 request；
- 保存所有 application-visible API invocations/retries、错误、stream chunks/final assembly 和 raw/normalized response；SDK 内部透明 HTTP retries 不得伪造；
- 保存 `S_t, I_t, P_t, A_t, R_t, S_{t+1}`，其中 provider raw calls/attempts 独立可追踪；
- 使用稳定 ID、版本 manifest 和 content-addressed binary artifacts；
- 通过 feature flag 完全关闭；
- 关闭时保持原行为；开启时也不得修改传给模型的 request 或 agent action。

禁止：

- 实现 Sentinel 判断器；
- 生成或更新 rubric；
- 给 raw event 写 `wrong/misleading/KEEP/DROP/REPLACE` 标签；
- 过滤、重排、修补或压缩 agent messages；
- 用 HTTP 200、截图变化或 task failure 自动等同动作语义失败；
- 把 API key、Authorization header 或其他 secrets 写入日志；
- 为了匹配本设计而破坏服务器已有用户修改或强制 reset 工作树。

## 数据原则

Raw collection 是不可变事实层；所有 claim extraction、错误分类、weak/strong uptake 和统计均属于可版本化的 offline derived layer。若实现选择与文档冲突，先更新设计并说明理由，不要静默偏离。

`EVENT_CONTRACT_V1.md` 是 event 名称、字段和终态规则的唯一权威；“完整 request”指 MobileWorld 在 SDK invocation 前传入的 application-layer arguments，不声称是 SDK 内部最终 HTTP wire body。

## 完成要求

只有通过 `TEST_AND_ACCEPTANCE.md` 的 P0 验收，并在 `STATUS.md` 记录 commit、命令、测试结果和已知限制后，才可宣称 collector 完成。真实模型 smoke run 需要服务器凭据和运行授权，不得把凭据写入代码或本目录。
