# MVP 运行说明

## 1. 先完成知识库绑定行为验证

正式配置同步来源前，先按 [知识库绑定行为验证](知识库绑定行为验证.md) 完成：

1. 已绑定文档内容修改测试。
2. 绑定目录新增文档测试。
3. 文档删除、移出目录或撤权测试。

记录每项测试的真实结果和首次生效时间。三项全部通过时，目标知识库内部变化不进入同步 Skill；任何一项失败时，先形成索引、绑定、缓存或权限问题，不使用知识提案流程补偿。

## 2. 完成采集范围与成本验证

按 [Agent 上游采集范围与成本验证](采集范围与成本验证.md) 为候选 Agent 建立来源清单和单次预算：

1. 优先配置“待沉淀”共享目录、知识候选 Base，以及 Base/Meegle 的专用业务视图。
2. 受限扫描只允许明确的会议系列、关键词、人员、项目和共享目录。
3. 不扫描公司全空间；个人文档默认排除，只有主动提交或明确批准时才能进入来源。
4. 文档搜索必须同时配置上游共享目录和关键词。
5. Base、Meegle 的稳定知识来源必须使用专用视图或 MQL，不整表、整项目读取。
6. 明确单次元数据、正文、候选、Token、提案和运行时间上限，超限内容顺延。

当前 V2 尚未全面实现元数据优先、总量和 Token 硬预算、真正的模型语义提炼、`dry-run` 与共享采集缓存。首个 MVP 只能使用隔离测试仓和严格小范围配置；不得据此开启公司级或多 Agent 自动扫描。

模型未启用时只能先完成来源范围准入，Token 字段填写“未启用”，不能按 `0 Token` 视为成本验证通过。Phase 1 接入模型后必须复测 Token 和提案质量，才能完成 MVP 验收。

## 3. 准备配置

复制根目录配置模板：

```powershell
Copy-Item .\agent-sync.example.yaml .\agent-sync.yaml
```

在 `agent-sync.yaml` 中补齐：

1. Agent ID、名称和负责人。
2. 公共、专属知识目录及目标文档 Token。
3. 主动投递入口、妙记范围、上游共享文档目录、Base 专用视图和 Meegle 专用视图或 MQL。
4. 相关人员、项目、包含词、排除词和审核人；搜索型来源不得把所有相关性规则留空。
5. 审核 Base 与三个管理表 ID。

`agent-sync.yaml` 已被 Git 忽略。不得在其中保存 API Key、访问令牌、Cookie 或其他凭证。

上游文档只能配置目标知识库之外的文档。`knowledge.public_document_token` 和 `knowledge.private_document_token` 不得再次出现在 `sources.documents.document_tokens` 中。

配置审核还必须确认：文档关键词搜索存在限定共享目录，稳定 Base/Meegle 来源存在专用视图或 MQL，未经授权的个人文档、目标知识目录和目标文档均不在来源中。

## 4. 初始化审核 Base

```powershell
python scripts/sync.py bootstrap-base --name "Agent 知识同步管理"
```

将返回的 Base Token 和三个表 ID 写入 `review` 配置。

## 5. 隔离环境运行

```powershell
python scripts/sync.py validate-config --config .\agent-sync.yaml
python scripts/sync.py scan --config .\agent-sync.yaml
python scripts/sync.py status --config .\agent-sync.yaml
python scripts/sync.py apply --config .\agent-sync.yaml --proposal-id "<proposal-id>"
```

首次验证优先使用 `review.storage: local` 或隔离测试 Base。当前版本没有正式 `dry-run`，`scan` 会写入提案和游标，因此不得直接对未完成准入审核的大范围真实数据运行。

MVP 阶段，`scan` 只生成待审核记录。审核人在 Base 将状态改为“已批准”后，才能执行 `apply`。运行后同时填写采集范围与成本验证表中尚未自动统计的正文量、候选量和 Token 指标。

## 6. 在线执行器

```powershell
$env:AGENT_SYNC_CONFIG_DIR = '<Agent 配置目录>'
$env:AGENT_SYNC_WEBHOOK_SECRET = '<至少 24 个字符的随机密钥>'
python scripts/server.py --host 127.0.0.1 --port 8765
```

执行器只读取服务器上的 Agent 配置，HTTP 请求不能覆盖数据源或写入目标。

如果飞书自动化无法调用 HTTPS，可以增加：

```text
--weekly --weekly-weekday 0 --weekly-time 09:00
```

该配置表示按服务器本地时间在每周一 09:00 运行全部 Agent 配置。只有 Phase 0、Phase 0.5 和单 Agent MVP 均通过后才能启用；日常发现更适合使用小范围的事件或日计划，每周用于集中审核和汇报，不应每周重读全部历史正文。

## 7. MVP 验收

1. 三项知识库绑定行为测试都有真实结果和生效时延记录。
2. 采集范围与成本验证有完整的来源清单、漏斗指标、预算和证据。
3. 公司全空间、未经授权的个人文档、目标知识目录和目标知识文档读取数为 0。
4. 文档搜索均限定共享目录，稳定 Base/Meegle 来源均使用专用视图或 MQL。
5. 不少于 10 条预期样例中至少发现 8 条，首轮“批准或仅需小修改”的提案不低于 60%。
6. 单次运行不超过已批准的正文、候选、Token、提案和时长预算；超限内容能够停止并安全顺延。
7. 相同内容第二次扫描不重复生成。
8. 动态 Meegle/Base 状态、未确认草稿和无关个人内容不进入长期知识。
9. 未批准或已拒绝提案不能写入正式知识。
10. 同一批准提案重复应用不会重复写入。
