# Source Manifest｜来源准入清单

Source Manifest 用来回答四个问题：为什么允许读、谁对来源负责、它能支持多强的结论、变化应写给哪个 Agent。运行身份“有权限读取”不等于来源已经获得业务准入。

## 必填治理字段

任何实际启用的会议、妙记、上游文档或稳定 Base/Meegle 来源都必须配置：

| 字段 | 含义 |
|---|---|
| `admission_type` | 来源如何进入：主动投递、专用视图、受限扫描或明确批准 |
| `source_owner` | 对来源范围、质量和撤权负责的人 |
| `business_purpose` | 为什么该 Agent 需要读取它 |
| `authority_level` | 来源可以支持的最高权威等级 |
| `target_scope` | 只能路由到 `public` 或 `private` |
| `knowledge_mode` | `stable` 才进入沉淀；`dynamic` 保留查询时读取 |

## admission_type

| 值 | 使用场景 |
|---|---|
| `proactive_submission` | 人员提交明确 Token，或把文档放入约定的待沉淀共享目录 |
| `dedicated_view` | Base/Meegle 的已确认决策、已完成复盘、待沉淀等专用视图或 MQL |
| `bounded_scan` | 同时受目录、关键词、人员、项目和时间窗约束的增量发现 |
| `explicit_approval` | 业务负责人对单个来源给出可追溯批准 |

`test_fixture` 仅由本地测试自动使用，不能出现在真实来源配置中。

## authority_level

| 值 | 可支持的内容 |
|---|---|
| `formal_policy` | 正式制度和公司级规则 |
| `approved_decision` | 已确认会议决议或业务决策 |
| `business_owner` | 业务负责人确认的口径和方法 |
| `validated_experience` | 有案例结果支持的复盘经验 |
| `reference` | 仅供参考，不能自动覆盖更高等级知识 |

权威等级是类别规则，不是虚假的小数可信度。任何评分只能用于候选排序，不能越过正式制度、有效范围、版本替代和人工审核。

## 示例：上游文档

```yaml
documents:
  admission_type: proactive_submission
  source_owner: 品牌负责人
  business_purpose: 接收已确认的品牌规则和方法
  authority_level: business_owner
  document_tokens: []
  folder_tokens: [待沉淀共享目录Token]
  search_keywords: [品牌规则]
  target_scope: private
  knowledge_mode: stable
```

## 示例：稳定 Base 视图

```yaml
- base_token: base_token
  table_id: table_id
  view_id: 已确认决策视图
  admission_type: dedicated_view
  source_owner: 项目负责人
  business_purpose: 沉淀已确认决策
  authority_level: approved_decision
  target_scope: private
  knowledge_mode: stable
```

## 运行规则

1. 来源停用、负责人变化或授权撤回时，先更新清单，再运行同步。
2. 文档搜索必须同时有目录和关键词；会议搜索必须叠加参与人或所有者范围。
3. 稳定 Base 必须有专用 `view_id`；稳定 Meegle 必须有专用 `view_ids` 或 `mql`。
4. 目标知识目录和目标文档不得出现在上游来源中。
5. 当前代码把准入、负责人、业务目的和权威等级写入提案的“相关性理由”，供审核追溯；自动有效期、替代和冲突裁决仍属于后续语义治理阶段。
