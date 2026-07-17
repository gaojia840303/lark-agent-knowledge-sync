# Agent 同步配置

## 目录

- [最小配置](#最小配置)
- [字段说明](#字段说明)
- [数据源配置](#数据源配置)
- [运行模式](#运行模式)

## 最小配置

复制 [`agent-sync.example.yaml`](agent-sync.example.yaml) 到业务 Agent 项目，命名为 `agent-sync.yaml`。配置文件不得包含 Token、API Key、Cookie 或 `.env` 内容。

配置解析器支持标准 YAML 的常用子集；也支持 JSON（JSON 是合法 YAML）。

## 字段说明

### agent

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 全局唯一、小写短横线标识 |
| `name` | 是 | Agent 名称 |
| `owner` | 是 | 业务负责人 |

### knowledge

| 字段 | 必填 | 说明 |
|---|---|---|
| `wiki_space_id` | 是 | 同一飞书知识空间 ID |
| `public_folder_token` | 是 | 公共目录节点 Token，用于校验边界 |
| `private_folder_token` | 是 | Agent 专属目录节点 Token |
| `public_document_token` | 是 | 公共知识实际追加目标文档 Token |
| `private_document_token` | 是 | 专属知识实际追加目标文档 Token |

目录 Token 负责权限边界，文档 Token 负责确定性写入。MVP 不能只配置目录而没有目标文档。

目标文档是正式知识终点，不是采集来源。配置时必须保证：

- `public_document_token`、`private_document_token` 不出现在 `sources.documents.document_tokens`。
- `public_folder_token`、`private_folder_token` 不作为无边界的上游扫描目录。
- 需要比较冲突或检查幂等时，可以读取目标文档，但不能据此生成“目标文档变化提案”。

当前 V1 配置解析器尚未自动拒绝来源与目标 Token 重叠，MVP 配置审核必须人工检查；该校验应列入后续实现项。

### relevance

- `include_keywords`：至少命中一项才能入选；为空时允许白名单来源进入。
- `exclude_keywords`：命中任一项立即排除，优先级最高。
- `related_people`、`related_projects`：与包含词合并为相关性证据。
- `max_candidate_chars`：单条候选正文上限，默认 `12000`。

### review

- `storage: base`：使用飞书 Base 保存提案、运行和游标。
- `storage: local`：仅用于 MVP 测试；状态保存到配置文件同级 `.agent-sync/<agent-id>/`。
- `base_token`：管理 Base Token。
- `tables.proposals/runs/cursors`：三个管理表的 ID 或精确名称。
- `reviewers`：审核人说明；不作为权限替代。

### runtime

- `lark_cli_path`：可选；默认查 PATH，再查全局 `lark-shared` Skill。
- `meegle_cli_path`：可选；默认查 PATH。
- `lookback_days`：首次扫描回溯天数，默认 `7`。
- `timezone`：默认 `Asia/Shanghai`。

## 数据源配置

### meetings

```yaml
meetings:
  keywords: [品牌周会]
  participant_ids: [ou_xxx]
  minute_tokens: []
  target_scope: private
  knowledge_mode: stable
```

`vc +search` 至少需要关键词、参与者或时间条件。Skill 自动添加上次游标到当前日期的时间范围。

### documents（仅上游文档）

```yaml
documents:
  document_tokens: [docx_token]
  folder_tokens: [folder_token]
  search_keywords: [品牌六要素]
  target_scope: private
  knowledge_mode: stable
```

这里只配置目标知识库之外、可能产生待沉淀知识的上游文档。文件夹搜索必须同时配置 `search_keywords`；不执行无边界的全空间扫描，也不扫描公共或专属目标知识目录。

### bases

```yaml
bases:
  - base_token: base_token
    table_id: tbl_xxx
    view_id: vew_xxx
    target_scope: private
    knowledge_mode: dynamic
```

执行时先读取字段结构，再串行读取记录。默认 `dynamic`，不进入长期知识。

### meegle

```yaml
meegle:
  - project_key: PROJECT_KEY
    view_ids: [view_id]
    mql: []
    target_scope: private
    knowledge_mode: dynamic
```

默认 `dynamic`。若要从复盘或已确认决策中沉淀知识，应为专门的稳定视图或查询配置 `knowledge_mode: stable`，不要把日常状态表整体改成稳定知识。

### fixtures

`fixtures` 仅用于测试和验收，接收本地 JSON 文件路径。生产配置不得启用。

## 运行模式

- `scan`：采集上游来源并生成审核提案，不扫描目标知识库的普通编辑，也不直接写正式知识。
- `apply`：只应用已批准提案。
- `status`：查看最近运行、游标和提案数量。
- `bootstrap-base`：创建管理 Base 与三个数据表。
