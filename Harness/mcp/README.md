# MCP 集成（占位）

本目录预留后续 MCP（Model Context Protocol）集成：

## 计划
- **Context7 MCP**：在编码时拉取第三方库最新文档，避免 API 幻觉。
- **数据库 MCP**：暴露业务库 schema 给 LLM，辅助 NL2SQL 上下文构建（受 SQL Guard 约束）。
- **Neo4j MCP**：本体图查询工具。

## 约束
任何 MCP 工具暴露给 LLM 的查询能力仍须遵守 `权限与安全规范.md`：
- 业务库查询只读、经 SQL Guard。
- 不泄露凭据。
- 审计日志。
