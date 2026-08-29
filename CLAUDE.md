# QA System - 项目入口

本文件为 AI 助手的项目入口，指向 Harness 架构治理系统。

## Harness 架构管理系统

所有架构规则、技能、Wiki、变更记录位于 `Harness/`：

- **入口 Agent**：`Harness/agents/owner.md` — 索引所有规则/技能/Wiki
- **规则**：`Harness/rules/` — 工程结构、编码规范、开发流程、权限安全、数据与AI治理
- **技能**：`Harness/skills/` — 需求分析、编码、代码审查、单测、NL2SQL Prompt 等
- **Wiki**：`Harness/wiki/` — 架构、数据模型、NL2SQL 引擎、模型路由、图表渲染等
- **变更**：`Harness/changes/` — 每个特性的单点事实（SSOT）记录
- **MCP**：`Harness/mcp/` — 后续 MCP 集成占位

## 核心约束

1. **不可变数据**：始终创建新对象，禁止原地修改。
2. **SQL 安全**：业务查询仅允许只读 SELECT，经 SQL Guard 校验。
3. **Token 计量**：每次 LLM 调用必须记录 Token 消耗与成本。
4. **TDD**：先写测试（RED）-> 实现（GREEN）-> 重构（IMPROVE），覆盖率 ≥ 80%。
5. **小文件**：200-400 行为宜，不超过 800 行；函数 < 50 行；嵌套 ≤ 4 层。
6. **显式错误处理**：每个层级显式处理错误，UI 层给友好提示，服务端记录详细上下文。
7. **真实数据库测试**：后端测试必须用真实 PostgreSQL + 完整 API 链路 + 真实数据库对象（见 `Harness/rules/测试规范.md`），禁止 sqlite 内存库 + 直接调用 service。

## 编码约定

- 函数/变量：`camelCase`
- 类型/组件：`PascalCase`
- 常量：`UPPER_SNAKE_CASE`
- 自定义 hooks：`use` 前缀
- ORM/Pydantic 字段名：`snake_case`（与 DB 列及 JSON 契约一致，刻意偏离）
- Python 项目因此偏离 PEP 8，以与前端及用户全局规则保持一致

## 开发流程

参考 `Harness/rules/开发流程规范.md` 的 10 阶段工作流。每次写完代码立即用 `code-reviewer` / `security-reviewer` 审查。
