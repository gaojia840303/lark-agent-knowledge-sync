# MVP 运行说明

## 1. 先完成知识库绑定行为验证

正式配置同步来源前，先按 [知识库绑定行为验证](知识库绑定行为验证.md) 完成：

1. 已绑定文档内容修改测试。
2. 绑定目录新增文档测试。
3. 文档删除、移出目录或撤权测试。

记录每项测试的真实结果和首次生效时间。三项全部通过时，目标知识库内部变化不进入同步 Skill；任何一项失败时，先形成索引、绑定、缓存或权限问题，不使用知识提案流程补偿。

## 2. 准备配置

复制根目录配置模板：

```powershell
Copy-Item .\agent-sync.example.yaml .\agent-sync.yaml
```

在 `agent-sync.yaml` 中补齐：

1. Agent ID、名称和负责人。
2. 公共、专属知识目录及目标文档 Token。
3. 妙记关键词、上游文档范围、Base 表/视图和 Meegle 项目/视图。
4. 相关人员、项目、包含词、排除词和审核人。
5. 审核 Base 与三个管理表 ID。

`agent-sync.yaml` 已被 Git 忽略。不得在其中保存 API Key、访问令牌、Cookie 或其他凭证。

上游文档只能配置目标知识库之外的文档。`knowledge.public_document_token` 和 `knowledge.private_document_token` 不得再次出现在 `sources.documents.document_tokens` 中。

## 3. 初始化审核 Base

```powershell
python scripts/sync.py bootstrap-base --name "Agent 知识同步管理"
```

将返回的 Base Token 和三个表 ID 写入 `review` 配置。

## 4. 本地运行

```powershell
python scripts/sync.py validate-config --config .\agent-sync.yaml
python scripts/sync.py scan --config .\agent-sync.yaml
python scripts/sync.py status --config .\agent-sync.yaml
python scripts/sync.py apply --config .\agent-sync.yaml --proposal-id "<proposal-id>"
```

MVP 阶段，`scan` 只生成待审核记录。审核人在 Base 将状态改为“已批准”后，才能执行 `apply`。

## 5. 在线执行器

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

该配置表示按服务器本地时间在每周一 09:00 运行全部 Agent 配置。

## 6. MVP 验收

1. 三项知识库绑定行为测试都有真实结果和生效时延记录。
2. 目标知识文档没有被配置为上游文档来源。
3. 首次扫描生成符合预期的审核提案。
4. 相同内容第二次扫描不重复生成。
5. 动态 Meegle/Base 状态不进入长期知识。
6. 未批准或已拒绝提案不能写入正式知识。
7. 同一批准提案重复应用不会重复写入。
