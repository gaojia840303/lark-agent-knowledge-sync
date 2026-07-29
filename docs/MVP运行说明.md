# MVP 运行说明

## 1. 并行完成知识库绑定边界测试

按 [知识库绑定行为验证](知识库绑定行为验证.md) 限时完成以下三项测试；它可与配置、来源清单和样例准备并行，但生产启用前必须有结论：

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

当前代码已强制来源 API、元数据、正文、候选、提案和时长预算，并支持 `dry-run`。尚未实现真正的模型语义提炼、模型 Token 预算、全面来源侧增量、自动版本冲突治理与共享采集缓存。首个 MVP 仍只能使用隔离测试仓和严格小范围配置。

模型未启用时先完成 Phase 0.5A 的来源范围、非模型预算和安全验证，Token 字段填写“未启用”。语义模型接入后再完成 Phase 0.5B 的 Token、事实/结论/推断和提案质量验证。

## 3. 准备配置

复制根目录配置模板：

```powershell
Copy-Item .\agent-sync.example.yaml .\agent-sync.yaml
```

在 `agent-sync.yaml` 中补齐：

1. Agent ID、名称、负责人、角色、职责、决策范围、输出契约和成功指标。
2. 公共、专属知识目录及目标文档 Token。
3. 主动投递入口、妙记范围、上游共享文档目录、Base 专用视图和 Meegle 专用视图或 MQL。
4. 每类实际来源的准入方式、来源负责人、业务目的和权威等级。
5. 相关人员、项目、包含词、排除词和审核人；搜索型来源不得把所有相关性规则留空。
6. 来源 API、元数据、正文、候选、提案和运行时长预算。
7. 审核 Base 与三个管理表 ID。

`agent-sync.yaml` 已被 Git 忽略。不得在其中保存 API Key、访问令牌、Cookie 或其他凭证。

上游文档只能配置目标知识库之外的文档。`knowledge.public_document_token` 和 `knowledge.private_document_token` 不得再次出现在 `sources.documents.document_tokens` 中。

运行 `validate-config --strict` 会检查 Agent 契约、来源治理字段、文档目录、稳定视图和目标/来源重叠；人工仍须确认来源授权真实有效，个人文档准入有证据，业务目的和权威等级正确。

## 4. 初始化审核 Base

```powershell
python scripts/sync.py bootstrap-base --name "Agent 知识同步管理"
```

将返回的 Base Token 和三个表 ID 写入 `review` 配置。

## 5. 隔离环境运行

```powershell
python scripts/sync.py validate-config --config .\agent-sync.yaml --strict
python scripts/sync.py scan --config .\agent-sync.yaml --dry-run
python scripts/sync.py scan --config .\agent-sync.yaml
python scripts/sync.py status --config .\agent-sync.yaml
python scripts/sync.py apply --config .\agent-sync.yaml --proposal-id "<proposal-id>"
```

首次先运行 `scan --dry-run`：它读取配置允许的受控来源，但不写提案、运行记录或游标。预演范围和预算通过后，再使用 `review.storage: local` 或隔离测试 Base 运行正式 `scan`。

MVP 阶段，`scan` 只生成待审核记录。审核人在 Base 将状态改为“已批准”后，才能执行 `apply`。运行结果自动记录非模型漏斗；模型未启用时 Token 记录为“未启用”。

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

该配置表示按服务器本地时间在每周一 09:00 运行全部 Agent 配置。只有知识库边界结论、Phase 0.5 和单 Agent MVP 均通过后才能启用。调度周期与 `lookback_days` 是两个独立参数；日常发现更适合小范围事件或日计划，每周用于集中审核和汇报。

## 7. MVP 验收

完整样例、失败分类、业务结果反馈和放行规则见 [单 Agent MVP 评测卡](MVP评测卡.md)。

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
