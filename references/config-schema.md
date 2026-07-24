# Agent 同步配置

## 目录

- [最小配置](#最小配置)
- [字段说明](#字段说明)
- [数据源配置](#数据源配置)
- [当前实现边界](#当前实现边界)
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

当前 V2 配置解析器尚未自动拒绝来源与目标 Token 重叠，MVP 配置审核必须人工检查；该校验应列入后续实现项。

### relevance

- `include_keywords`：至少命中一项才能入选；为空时允许白名单来源进入。
- `exclude_keywords`：命中任一项立即排除，优先级最高。
- `related_people`、`related_projects`：与包含词合并为相关性证据。
- `max_candidate_chars`：单条候选正文上限，默认 `12000`。

生产准入规则比当前解析器更严格：搜索型会议和文档来源不得将 `include_keywords`、`related_people`、`related_projects` 全部留空。只有主动提交的明确 Token，或已经经过业务负责人批准的专用 Base/Meegle 视图，才允许把来源本身作为相关性证据。

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

`lookback_days` 只用于会议和妙记的时间范围，不代表文档、Base 和 Meegle 已经完成来源侧增量。日常运行使用短时间窗；一次性历史补录必须使用独立配置和单独批准的预算。

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

`vc +search` 至少需要业务关键词，并叠加参与者、所有者或固定妙记范围。Skill 自动添加上次游标到当前日期的时间范围。目标架构应先判断标题、人员、项目和摘要，再按需读取完整纪要或逐字稿；当前 V2 仍会为搜索结果读取纪要内容。

### documents（仅上游文档）

```yaml
documents:
  document_tokens: [docx_token]
  folder_tokens: [folder_token]
  search_keywords: [品牌六要素]
  target_scope: private
  knowledge_mode: stable
```

这里只配置目标知识库之外、可能产生待沉淀知识的上游文档。个人文档默认不配置；只有主动提交、进入指定共享目录或业务负责人明确批准时才允许使用明确 Token。

文档搜索必须同时配置非空 `folder_tokens` 和 `search_keywords`，不允许只填关键词搜索账号全部可见空间，也不扫描公共或专属目标知识目录。当前 V2 代码尚未强制非空目录，MVP 前必须人工审核；代码级拒绝属于待实现项。

### bases

```yaml
bases:
  - base_token: base_token
    table_id: tbl_xxx
    view_id: vew_xxx
    target_scope: private
    knowledge_mode: dynamic
```

执行时先读取字段结构，再串行读取记录。默认 `dynamic`，应由 Agent 使用时实时查询，不放入周期知识扫描；只有确有受控运行摘要需求时才配置窄视图。若配置 `knowledge_mode: stable`，必须同时提供“待沉淀”“已确认决策”或“已完成复盘”等专用 `view_id`，不能把业务整表改成稳定知识来源。

### meegle

```yaml
meegle:
  - project_key: PROJECT_KEY
    view_ids: [view_id]
    mql: []
    target_scope: private
    knowledge_mode: dynamic
```

默认 `dynamic`，应由 Agent 使用时实时查询，不放入周期知识扫描。若要从复盘或已确认决策中沉淀知识，应为专门的稳定 `view_ids` 或 `mql` 配置 `knowledge_mode: stable`，不要读取整项目或把日常状态整体改成稳定知识。

### fixtures

`fixtures` 仅用于测试和验收，接收本地 JSON 文件路径。生产配置不得启用。

## 当前实现边界

以下是 V2 已实现能力与目标能力的区分：

| 能力 | 当前 V2 | MVP / 生产要求 |
|---|---|---|
| 来源范围 | 支持显式 Token、关键词、目录、表、视图、项目和 MQL | 强制共享目录、专用视图、个人文档默认拒绝和搜索型相关性条件 |
| 增量 | 会议/妙记使用时间游标；所有来源使用批次哈希避免重复提案 | Docs、Base、Meegle 在来源侧按更新时间或事件取增量 |
| 正文读取 | 部分来源先取正文或分页数据，再做筛选和批次哈希 | 元数据与规则通过后才取正文，长文只取摘要或变化段落 |
| 相关性 | 字符串匹配关键词、人员和项目 | 少量候选进入语义模型判断 |
| 知识提炼 | 截取来源内容生成候选，`Agent推断` 为空 | 模型生成事实、提炼结论和推断分离的提案 |
| 成本限制 | 单条 `max_candidate_chars`；Base/Meegle 支持来源级 `max_pages` | 单次元数据、正文、总字符、候选、Token、提案、时长和 API 次数硬预算 |
| 预演 | 无正式 `dry-run` | 支持只统计不写提案和游标的预演模式 |
| 多 Agent 复用 | 各 Agent 独立采集 | 相同来源采集一次、缓存一次、分别路由 |

计划中的预算字段不得在当前版本中被理解为已经生效。目标结构如下，需在同步核心实现并测试后才能用于生产：

```yaml
runtime:
  max_metadata_items: 100
  max_fulltext_items: 20
  max_fulltext_chars: 60000
  max_candidates: 10
  max_proposals: 5
  max_runtime_seconds: 600

model_budget:
  max_input_tokens: 15000
  max_output_tokens: 3000
```

首个 MVP 在代码补齐前，按 [采集范围与成本验证](../docs/采集范围与成本验证.md) 使用隔离、小范围配置并人工记录这些指标；不得直接开启公司级或多 Agent 自动运行。

## 运行模式

- `scan`：采集上游来源并生成审核提案，不扫描目标知识库的普通编辑，也不直接写正式知识。当前不是 `dry-run`，会写提案和游标。
- `apply`：只应用已批准提案。
- `status`：查看最近运行、游标和提案数量。
- `bootstrap-base`：创建管理 Base 与三个数据表。
