# QA System - Application Owner

本文件是 QA 智能问答系统的 Harness 入口。它索引所有架构规则、技能、Wiki、变更记录与 MCP，供任何 AI 助手或开发者快速进入上下文。

## 角色

我是本系统的 Application Owner。我的职责是：保证实现与设计稿（`docs/设计01.md`、`docs/设计02.md`）一致，守住核心约束，并在每次变更时维护单点事实（SSOT）。

## 核心约束（不可违反）

1. **SQL 安全**：业务查询仅允许只读 `SELECT`，经 `app/infrastructure/business_db_pool.py` 校验（不在 `app/infrastructure/security/sql_guard.py` —— 该文件不存在）；禁止 DDL/DML/多语句。
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
- [部署与访问规范](../rules/部署与访问规范.md) - 前端同源相对路径（禁编译期绝对地址）/端口绑定/CORS/发布入口枚举
- [数据与AI治理规范](../rules/数据与AI治理规范.md) - 本体版本/指标审查/路由策略
- [测试规范](../rules/测试规范.md) - 真实 PG + 完整 API 链路测试（强制，禁 sqlite 内存库）
- [数据库环境使用规范](../rules/数据库环境使用规范.md) - 表结构/新增表需求直接用 prod qa_metadata（先备份 `<表名>_<YYYYMMDD>`），仅测试走 qa_metadata_test

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
- [architecture](../wiki/architecture.md) - 系统架构与边界（含 Phase 9 DQ 报告异步化）
- [business-domain](../wiki/business-domain.md) - WMS/SRM 业务域
- [data-model](../wiki/data-model.md) - 元数据与本体模型（entity_mapping SSOT）
- [nl2sql-engine](../wiki/nl2sql-engine.md) - Prompt 策略 + SQL Guard（4 层路由）
- [model-router](../wiki/model-router.md) - 路由算法与成本模型
- [agent-loop](../wiki/agent-loop.md) - L4 Agent Loop（纯 Python async while loop，非 LangGraph）
- [chart-rendering](../wiki/chart-rendering.md) - ECharts 生成规则
- [metric-pipeline](../wiki/metric-pipeline.md) - 复杂指标管线（CTE 派生 / 冷指标晋升）
- [chat-service-assessment](../wiki/chat-service-assessment.md) - Chat 服务 SSOT 评估（§2.4 LOW / §15 残差 SSOT）
- [frontend](../wiki/frontend.md) - 前端架构（Vite + React + SSE）
- [frontend-theme](../wiki/frontend-theme.md) - 前端主题与 ECharts token 双轨
- [supplier-receipt-workflow](../wiki/supplier-receipt-workflow.md) - 供应商收货工作流
- [audit-log-system](../wiki/audit-log-system.md) - 审计日志系统
- [ai-roadmap](../wiki/ai-roadmap.md) - AI 路线图
- [wiki-ontology-link](../wiki/wiki-ontology-link.md) - wiki ↔ ontology 链接管理 + NL2SQL 业务规则注入
- [api-reference](../wiki/api-reference.md) - API 契约
- [config-reference](../wiki/config-reference.md) - 环境变量
- [operations-runbook](../wiki/operations-runbook.md) - 运维手册（部署 / 监控 / 备份 / 恢复）

### 变更 `changes/`
- [_template/summary.md](../changes/_template/summary.md) - 变更 SSOT 模板
- **近 30 天热点（按 SSOT 性质）**：
  - [chore-magic-number-governance](../changes/chore-magic-number-governance/summary.md) - 魔数治理 Phase 2 15/15（0087 easy + 0088 medium + 0089 hard）
  - [chore-chat-service-file-split](../changes/chore-chat-service-file-split/summary.md) - chat_service.py mixin 分解（4916→~1100 行基类 + 7 mixin + 1 helper）
  - [chore-nl2sql-service-file-split](../changes/chore-nl2sql-service-file-split/summary.md) - nl2sql_service.py 8 文件门面拆分
  - [chore-chat-15-residuals-assessment](../changes/chore-chat-15-residuals-assessment/summary.md) - §15 残差 4 项评估（保留 / 挂账 / 关闭）
  - [chore-chat-15-tail-three-items](../changes/chore-chat-15-tail-three-items/summary.md) - KPI 索引 SSOT + 空关键词语义 + 失败路径 LLM 用量
  - [fix-sql-guard-db-side-readonly](../changes/fix-sql-guard-db-side-readonly/summary.md) - 库侧只读兜底（PG/MySQL/Oracle 三库）
  - [fix-plan-scope-gate-b](../changes/fix-plan-scope-gate-b/summary.md) - 半空计划闸门方案 B
  - [fix-oracle-alter-session-best-effort](../changes/fix-oracle-alter-session-best-effort/summary.md) - Oracle ALTER SESSION best-effort 降级
  - **历史里程碑**：完整目录见 `Harness/changes/`（173 个变更，每变更配 `summary.md` SSOT）

### MCP `mcp/`
- [README.md](../mcp/README.md) - 后续 MCP 集成占位

## 快速进入上下文

1. 读 `wiki/architecture.md` 理解全局。
2. 读对应 Phase 的 wiki（如 model-router）。
3. 遵守 `rules/开发流程规范.md` 的 10 阶段流程。
4. 变更结束在 `changes/` 新建目录记录 SSOT。

## 当前进度

### 已完成阶段（Phase 1-9 + 6.x 增量）

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 模型路由 + Token 计量 | ✅ 完成 |
| Phase 2 | 本体管理（Neo4j + Milvus） | ✅ 完成 |
| Phase 3 | 多数据源 + 单表 NL2SQL | ✅ 完成 |
| Phase 4 | 对话 + 图表渲染（Vite + React + SSE） | ✅ 完成 |
| Phase 5 | 多表 JOIN + 向量检索 | ✅ 完成 |
| Phase 5.x | 复杂指标管线（CTE 派生 / 冷指标晋升 / 路由层聚合） | ✅ 完成 |
| Phase 6.x | NL2SQL 深化：REFINE 捷径 / 半空计划闸门（方案 A+B）/ LLM 计量盲区 H1-H9 / 库侧只读兜底 | ✅ 完成 |
| Phase 6.4 | L4 Agent Loop（纯 Python async while loop，非 LangGraph） | ✅ 完成 |
| Phase 7 | 本体语义关联 / 质量报告（wiki 知识层 M1-M8） | ✅ 完成 |
| Phase 8 | RBAC identity + ontology ACL 越权洞修复 + 数据备份 | ✅ 完成 |
| Phase 9 | 数据质量评估报告（异步化 / scope 字段 / CheckConstraint 6 值） | ✅ 完成 |

### 当前工作热点（2026-09-25 → 至今）

| 主题 | 范围 | 状态 |
|---|---|---|
| §2.4 LOW 文件拆分 | nl2sql 8 文件 + chat_service mixin 分解 | ✅ 完成（chat-service-assessment.md §2.4 全 ✅） |
| §2.4 LOW 魔数治理 Phase 2 | 15 项迁 `system_config`（0087/0088/0089） | ✅ 完成（15/15） |
| §15 残差 | 7 项主批 + 4 项残差评估 | ✅ 完成（评估 4 项：保留 prior_cte / 挂账 M9 / 关闭 logger.warning / 关闭 Milvus round-trip） |
| 文档-代码漂移清扫 | §2.5 + 三拨 doc-drift-cleanup（b/c 系列） | ✅ 完成（7 行 ✅） |
| 配置重复字段治理 | `config.py` Settings 9 组重复 + `created_time` 怪胎 | ✅ 完成（2026-09-27） |

**完整 SSOT**：`Harness/wiki/chat-service-assessment.md`（§2.4 + §15）+ `Harness/changes/`（每个变更单点 SSOT）。
