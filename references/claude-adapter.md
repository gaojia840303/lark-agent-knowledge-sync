# Claude 兼容适配

不复制、不改写公共同步逻辑。Claude 运行环境只做一层命令适配：

1. 将全局 Skill 目录以只读依赖方式提供给执行环境。
2. 传入与 Codex 完全相同的 `agent-sync.yaml`，并保证正式目标知识文档没有同时出现在上游来源中。
3. 调用 `scripts/sync.py scan|apply|status`，不在 Claude 提示词中重写采集、路由、审核和幂等规则。
4. 飞书和 Meegle 凭证由运行环境提供，不写入配置、Prompt 或知识文档。
5. Claude 产生的语义提炼结果仍只能作为“Agent 推断”字段进入审核，不能绕过 Base 审核状态。

适配层的验收标准是：同一配置和同一来源在 Codex 与 Claude 环境产生相同的来源 ID、内容哈希、目标范围和提案 ID。
