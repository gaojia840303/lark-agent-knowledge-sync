# lark-agent-knowledge-sync

面向业务 Agent 的飞书知识增量同步 Skill。

它按照每个 Agent 的独立配置，从飞书会议与妙记、云文档、Base 和 Meegle 读取变化，完成增量检查、去重、相关性筛选和知识提案生成；审核通过后，再幂等写入该 Agent 的指定知识文档。

## 核心边界

- 这是供多个业务 Agent 复用的公共 Skill，不是独立的中央知识 Agent。
- 只维护“发现增量—审核—沉淀”链路，不重建 Agent 的问答检索能力。
- Meegle 和 Base 中的动态状态保留在原始事实源，默认不复制为长期知识。
- MVP 所有变更都进入人工审核，不直接修改正式知识。

## 工作流

```text
定时任务或人工命令
→ 加载指定 Agent 配置
→ 读取白名单数据源
→ 增量检查、去重和相关性判断
→ 生成知识变更提案
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

验证并运行：

```powershell
python scripts/sync.py validate-config --config .\agent-sync.yaml
python scripts/sync.py scan --config .\agent-sync.yaml
python scripts/sync.py status --config .\agent-sync.yaml
```

审核通过后应用单条提案：

```powershell
python scripts/sync.py apply --config .\agent-sync.yaml --proposal-id "<proposal-id>"
```

完整配置与 MVP 步骤见：

- [配置说明](references/config-schema.md)
- [MVP 运行说明](docs/MVP运行说明.md)
- [落地方案（修订版）](docs/Agent自动更新能力落地方案.md)
- [安全与审核规则](references/knowledge-policy.md)

## 命令与接口

本地命令：

```text
sync.py validate-config --config <agent-sync.yaml>
sync.py scan            --config <agent-sync.yaml>
sync.py apply           --config <agent-sync.yaml> --proposal-id <id>
sync.py status          --config <agent-sync.yaml>
sync.py bootstrap-base
```

在线执行器：

```text
POST /v1/agents/{agent_id}/sync
POST /v1/proposals/{proposal_id}/apply
GET  /v1/runs/{run_id}
GET  /v1/agents/{agent_id}/status
```

所有在线接口均需 Bearer 鉴权，且请求正文不能覆盖服务器上的 Agent 数据源或目标配置。

## 仓库结构

```text
├── SKILL.md                 # Codex Skill 入口和运行规则
├── agent-sync.example.yaml # Agent 配置模板
├── agents/                 # Skill 展示元数据
├── scripts/                # 同步核心与在线执行器
├── references/             # 配置、路由、安全和兼容说明
├── tests/                  # 标准库单元测试
└── docs/                   # 项目背景、落地方案和 MVP 指南
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
