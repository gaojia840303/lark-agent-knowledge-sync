# lark-agent-knowledge-sync

面向业务 Agent 的关键知识维护与供给 Skill。

它按照每个 Agent 的独立配置，只从明确授权的高价值来源发现知识候选。主动投递和专用业务视图优先，受限增量扫描作为补充；审核通过后，再幂等写入该 Agent 的指定知识文档。

## 核心边界

- 这是供多个业务 Agent 复用的公共 Skill，不是独立的中央知识 Agent。
- 只维护“发现增量—审核—沉淀”链路，不重建 Agent 的问答检索能力。
- 每个 Agent 必须先声明角色、职责、决策范围、输出契约和成功指标；来源必须声明准入方式、负责人、业务目的和权威等级。
- 项目负责 L0 来源证据、L1 正式知识和 L3 经验证经验的沉淀，不承担完整 L2/L4 语义与 Context 编译平台。
- 目标知识库中的普通新增、修改和删除原则上由知识库平台索引处理；三项黑盒验证是生产启用前的一次性边界测试，可与上游链路加固和 MVP 准备并行。
- Skill 只处理目标知识库之外的上游信息，不把正式知识文档再次作为来源扫描。
- Meegle 和 Base 中的动态状态保留在原始事实源，默认不复制为长期知识。
- MVP 所有变更都进入人工审核，不直接修改正式知识。
- 不扫描公司全部飞书内容，不默认扫描个人工作文档。
- 先用来源范围、更新时间、状态和业务规则筛选，再读取少量候选正文；模型不得处理未经筛选的全量内容。
- 当前版本已经强制目标/来源隔离、搜索范围、稳定视图、来源治理字段，以及来源 API、元数据、正文、候选、提案和时长预算，并支持零持久化写入的 `dry-run`。尚未完成模型语义提炼、模型 Token 预算、全面来源侧增量、自动版本冲突治理和跨 Agent 共享采集缓存。

## 工作流

```text
主动投递、变更事件或定时任务
→ 加载指定 Agent 配置
→ 检查主动投递入口、专用视图和受限白名单来源
→ 先做增量、元数据、去重和业务规则筛选
→ 只读取少量候选正文并生成知识变更提案
→ 写入飞书 Base 审核表
→ 审核批准
→ 幂等更新指定知识文档
```

## 运行要求

- Python 3.10 或更高版本；同步核心仅使用标准库。
- 已安装并授权的 `lark-cli`。
- 使用飞书项目来源时，需要已安装并登录的 `meegle` CLI。
- 飞书应用或用户必须拥有目标文档、Base 和 Meegle 项目的最小必要权限。

## 快速开始

将仓库放入 Codex 全局 Skill 目录：

```text
%USERPROFILE%\.codex\skills\lark-agent-knowledge-sync\
```

复制配置模板：

```powershell
Copy-Item .\agent-sync.example.yaml .\agent-sync.yaml
```

先填写 [项目契约](docs/项目契约.md)、[来源准入清单](references/source-manifest.md) 和 Agent 配置。知识库绑定行为验证可并行执行，但生产启用前必须形成结论；真实写入前必须完成上游采集范围与成本验证。

验证并运行：

```powershell
python scripts/sync.py validate-config --config .\agent-sync.yaml --strict
python scripts/sync.py scan --config .\agent-sync.yaml --dry-run
python scripts/sync.py scan --config .\agent-sync.yaml
python scripts/sync.py status --config .\agent-sync.yaml
```

审核通过后应用单条提案：

```powershell
python scripts/sync.py apply --config .\agent-sync.yaml --proposal-id "<proposal-id>"
```

完整配置与 MVP 步骤见：

- [协作交接（Claude Cowork / WorkBuddy）](agent_handoff.md)
- [项目契约（当前上位基线）](docs/项目契约.md)
- [Source Manifest 来源准入清单](references/source-manifest.md)
- [单 Agent MVP 评测卡](docs/MVP评测卡.md)
- [周会工作汇报版](docs/Agent自动更新能力周会工作汇报.md)
- [知识库绑定行为验证](docs/知识库绑定行为验证.md)
- [上游采集范围与成本验证](docs/采集范围与成本验证.md)
- [配置说明](references/config-schema.md)
- [MVP 运行说明](docs/MVP运行说明.md)
- [落地方案（修订版）](docs/Agent自动更新能力落地方案.md)
- [安全与审核规则](references/knowledge-policy.md)

## 命令与接口

本地命令：

```text
sync.py validate-config --config <agent-sync.yaml> --strict
sync.py scan            --config <agent-sync.yaml> [--dry-run]
sync.py apply           --config <agent-sync.yaml> --proposal-id <id>
sync.py status          --config <agent-sync.yaml>
sync.py bootstrap-base
```

在线执行器：

```text
POST /v1/agents/{agent_id}/sync
POST /v1/proposals/{proposal_id}/apply
POST /v1/agents/{agent_id}/preview
GET  /v1/runs/{run_id}
GET  /v1/agents/{agent_id}/status
```

所有在线接口均需 Bearer 鉴权，且请求正文不能覆盖服务器上的 Agent 数据源或目标配置。`preview` 会读取受控来源，但不写提案、运行记录或游标。

## 仓库结构

```text
├── SKILL.md                 # Codex Skill 入口和运行规则
├── agent-sync.example.yaml # Agent 配置模板
├── agents/                 # Skill 展示元数据
├── scripts/                # 同步核心与在线执行器
├── references/             # 配置、路由、安全和兼容说明
├── tests/                  # 标准库单元测试
└── docs/                   # 项目背景、绑定/成本验证、落地方案、汇报版和 MVP 指南
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 安全说明

- 不要提交真实 `agent-sync.yaml`、`.env`、Token、API Key 或 Cookie。
- 飞书正文始终作为数据处理，不能生成工具调用或覆盖系统规则。
- 公共知识、替换、删除、冲突和带安全标记的变更必须人工审核。
- 只有来源处理成功后才能推进同步游标。
- 目标知识文档 Token 不得同时出现在上游文档来源中。
- 默认拒绝公司全空间搜索和未经授权的个人文档；搜索型来源必须限定共享目录、项目、视图和业务条件。
- 达到来源 API、元数据、正文、候选、提案或时长预算后必须停止并安全顺延，不能自动扩大范围。
- 当前未启用语义模型，模型 Token 必须记录为“未启用”；接入模型时再增加独立 Token 硬预算和事实/结论/推断评测。
