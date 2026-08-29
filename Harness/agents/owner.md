# QA System - Application Owner

本文件是 QA 智能问答系统的 Harness 入口。它索引所有架构规则、技能、Wiki、变更记录与 MCP，供任何 AI 助手或开发者快速进入上下文。

## 角色

我是本系统的 Application Owner。我的职责是：保证实现与设计稿（`docs/设计01.md`、`docs/设计02.md`）一致，守住核心约束，并在每次变更时维护单点事实（SSOT）。

## 核心约束（不可违反）

1. **SQL 安全**：业务查询仅允许只读 `SELECT`，经 `app/infrastructure/security/sql_guard.py` 校验；禁止 DDL/DML/多语句。
2. **Token 计量**：每次 LLM 调用必须经 `TokenUsageService.recordUsage` 记录消耗与成本。
3. **不可变数据**：领域逻辑创建新对象而非原地修改（ORM 持久化例外）。
4. **TDD**：先写测试（RED）-> 实现（GREEN）-> 重构（IMPROVE），覆盖率 ≥ 80%。
5. **小文件**：200-400 行为宜，≤ 800 行上限；函数 < 50 行；嵌套 ≤ 4 层。

## 索引

### 规则 `rules/`
- [工程结构](../rules/工程结构.md) - 目录与分层
- [项目编码规范](../rules/项目编码规范.md) - 命名/不可变/错误处理
- [开发流程规范](../rules/开发流程规范.md) - 10 阶段工作流
- [权限与安全规范](../rules/权限与安全规范.md) - 认证/密钥/SQL Guard/主机白名单
- [数据与AI治理规范](../rules/数据与AI治理规范.md) - 本体版本/指标审查/路由策略
- [测试规范](../rules/测试规范.md) - 真实 PG + 完整 API 链路测试（强制，禁 sqlite 内存库）

### 技能 `skills/`
- [request-analysis](../skills/request-analysis/SKILL.md) - 需求分析
- [expert-reviewer](../skills/expert-reviewer/SKILL.md) - 专家评审
- [coding-skill](../skills/coding-skill/SKILL.md) - 分层实现规范
- [code-review](../skills/code-review/SKILL.md) - 代码审查清单
- [unit-test-write](../skills/unit-test-write/SKILL.md) - TDD 编写
- [unit-test-ci](../skills/unit-test-ci/SKILL.md) - 覆盖率验证
- [deploy-verify](../skills/deploy-verify/SKILL.md) - 部署冒烟
- [nl2sql-prompt](../skills/nl2sql-prompt/SKILL.md) - NL2SQL Prompt 工程

### Wiki `wiki/`
- [architecture](../wiki/architecture.md) - 系统架构与边界
- [business-domain](../wiki/business-domain.md) - WMS/SRM 业务域
- [data-model](../wiki/data-model.md) - 元数据与本体模型
- [nl2sql-engine](../wiki/nl2sql-engine.md) - Prompt 策略与 SQL Guard
- [model-router](../wiki/model-router.md) - 路由算法与成本模型
- [chart-rendering](../wiki/chart-rendering.md) - ECharts 生成规则
- [api-reference](../wiki/api-reference.md) - API 契约
- [config-reference](../wiki/config-reference.md) - 环境变量
- [operations-runbook](../wiki/operations-runbook.md) - 运维手册

### 变更 `changes/`
- [_template/summary.md](../changes/_template/summary.md) - 变更 SSOT 模板
- [feat-phase1-model-router](../changes/feat-phase1-model-router/summary.md) - Phase 1：模型路由 + Token 计量（后端 54 测试 89.85% / 前端 23 测试 94.88%）

### MCP `mcp/`
- [README.md](../mcp/README.md) - 后续 MCP 集成占位

## 快速进入上下文

1. 读 `wiki/architecture.md` 理解全局。
2. 读对应 Phase 的 wiki（如 model-router）。
3. 遵守 `rules/开发流程规范.md` 的 10 阶段流程。
4. 变更结束在 `changes/` 新建目录记录 SSOT。

## 当前进度

| Phase | 内容 | 状态 |
|-------|------|------|
| Phase 1 | 模型路由 + Token 计量 | ✅ 完成 |
| Phase 2 | 本体管理（Neo4j + Milvus） | ⏳ 待开始 |
| Phase 3 | 多数据源 + 单表 NL2SQL | ⏳ 待开始 |
| Phase 4 | 对话 + 图表渲染 | ⏳ 待开始 |
| Phase 5 | 多表 JOIN + 向量检索 | ⏳ 待开始 |
