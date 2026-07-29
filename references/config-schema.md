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
| `role` | 是 | Agent 在业务流程中的角色 |
| `responsibilities` | 是 | 至少一项持续职责 |
| `decision_scope` | 是 | Agent 可以判断和不能越界的范围 |
| `output_contract` | 是 | 可验收的固定输出 |
| `success_metrics` | 是 | 至少一项业务或质量指标 |

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

当前解析器会拒绝目标文档出现在 `document_tokens`、目标目录出现在 `folder_tokens`，并拒绝公共/专属目标使用同一个 Token。

### relevance

- `include_keywords`：至少命中一项才能入选；为空时允许白名单来源进入。
- `exclude_keywords`：命中任一项立即排除，优先级最高。
- `related_people`、`related_projects`：与包含词合并为相关性证据。
- `max_candidate_chars`：单条候选正文上限，默认 `12000`。

解析器会拒绝搜索型会议和文档来源把 `include_keywords`、`related_people`、`related_projects` 全部留空。只有主动提交的明确 Token，或已经经过业务负责人批准的专用 Base/Meegle 视图，才允许把来源本身作为相关性证据。

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
- `max_metadata_items`：单次允许进入处理的元数据条数，默认 `100`。
- `max_fulltext_items`：单次正文条数，默认 `20`。
- `max_fulltext_chars`：单次正文总字符，默认 `60000`。
- `max_candidates`：进入提案判断的候选数，默认 `10`。
- `max_proposals`：单次新提案上限，默认 `5`。
- `max_runtime_seconds`：单次运行时长，默认 `600`。
- `max_source_api_calls`：上游来源 API 调用次数，默认 `100`；审核存储自身调用不计入该字段。

`lookback_days` 只用于会议和妙记的时间范围，不是调度周期，也不代表文档、Base 和 Meegle 已经完成来源侧增量。日常运行使用短时间窗；一次性历史补录必须使用独立配置和单独批准的预算。

## 数据源配置


### 来源治理公共字段

实际启用的会议、妙记、上游文档和稳定 Base/Meegle 来源必须填写：

- `admission_type`：`proactive_submission`、`dedicated_view`、`bounded_scan` 或 `explicit_approval`。
- `source_owner`：来源范围、质量和撤权负责人。
- `business_purpose`：该 Agent 读取来源的业务目的。
- `authority_level`：`formal_policy`、`approved_decision`、`business_owner`、`validated_experience` 或 `reference`。

完整定义见 [Source Manifest](source-manifest.md)。运行时会把四项信息附加到提案的“相关性理由”，但当前不会自动裁决有效期、替代或冲突。
### meetings

```yaml
meetings:
  keywords: [品牌周会]
  participant_ids: [ou_xxx]
  minute_tokens: []
  target_scope: private
  admission_type: bounded_scan
  source_owner: 品牌负责人
  business_purpose: 发现已确认的品牌决策
  authority_level: approved_decision
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

以下是当前已实现能力与后续目标能力的区分：

| 能力 | 当前实现 | 后续要求 |
|---|---|---|
| 来源范围 | 强制目标/来源隔离、文档目录、会议人员范围、稳定专用视图、搜索相关性和来源治理字段 | 接入真实业务授权台账和撤权事件 |
| 增量 | 会议/妙记使用时间游标；所有来源使用批次哈希避免重复提案 | Docs、Base、Meegle 在来源侧按更新时间或事件取增量 |
| 正文读取 | 部分来源先取正文或分页数据，再做筛选和批次哈希 | 元数据与规则通过后才取正文，长文只取摘要或变化段落 |
| 相关性 | 字符串匹配关键词、人员和项目 | 少量候选进入语义模型判断 |
| 知识提炼 | 截取来源内容生成候选，`Agent推断` 为空 | 模型生成事实、提炼结论和推断分离的提案 |
| 成本限制 | 强制来源 API、元数据、正文、总字符、候选、提案和时长预算 | 模型接入后增加输入/输出 Token 硬预算 |
| 预演 | `scan --dry-run` 读取受控来源但不写提案、运行和游标 | 增加按来源的完整漏斗与延期队列 |
| 多 Agent 复用 | 各 Agent 独立采集 | 相同来源采集一次、缓存一次、分别路由 |

以下 `runtime` 字段已经由同步核心执行；模型尚未启用，因此 `model_budget` 仍是后续目标，不能填写为已执行：

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
  max_source_api_calls: 100
```

首个 MVP 在代码补齐前，按 [采集范围与成本验证](../docs/采集范围与成本验证.md) 使用隔离、小范围配置并人工记录这些指标；不得直接开启公司级或多 Agent 自动运行。

## 运行模式

- `scan`：采集上游来源并生成审核提案，不扫描目标知识库的普通编辑，也不直接写正式知识。当前不是 `dry-run`，会写提案和游标。
- `apply`：只应用已批准提案。
- `status`：查看最近运行、游标和提案数量。
- `bootstrap-base`：创建管理 Base 与三个数据表。
