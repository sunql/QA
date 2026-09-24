# 系统架构

## 概述

智能问答系统：用户用自然语言提问，系统经多模型路由调用 LLM，将意图转为 SQL（NL2SQL），在可配置业务库上执行只读查询，并自动渲染 ECharts 图表。知识沉淀为本体（Ontology）。

NL2SQL 采用**4 层路由架构**（Phase 5）：L1 KPI 语义匹配 → L2 LLM 单 SQL → L3 CTE 链 → L4 LangGraph Agent Loop。详见 `wiki/nl2sql-engine.md#4-layer-routing-architecture-phase-5`。

## 分层

```
用户/前端 ── HTTP ──> API 层 (FastAPI)
                        │
            ┌───────────┼───────────┐
            ▼           ▼           ▼
       模型路由      本体服务     NL2SQL引擎
       (路由+计量)   (CRUD+图)    (Prompt+SQL)
            │           │           │
            ▼           ▼           ▼
       LLM 客户端    Neo4j+PG    SQL Guard + 数据源
       (OpenAI/Ollama)  Milvus    (动态引擎池)
                                    │
                                    ▼
                              业务数据库 (MySQL/PG)
```

## 核心组件

| 组件 | 位置 | 职责 |
|------|------|------|
| 模型路由 | `services/model_router_service.py` | 加权随机 + 成本阈值 + 会话亲和 + 预算降级 |
| Token 计量 | `services/token_usage_service.py` | 记录与聚合每次调用消耗 |
| LLM 客户端 | `infrastructure/llm/` | OpenAI/Azure/代理/Ollama 统一抽象 |
| Token 计数 | `infrastructure/token_counter/` | tiktoken + 启发式 |
| 本体服务 | `services/ontology_service.py`（Phase 2） | 类/属性/指标 CRUD + 图持久化 |
| NL2SQL | `services/nl2sql_service.py`（Phase 3） | ReAct 两阶段（计划→验证→SQL）+ REFINE 捷径 + SQL Guard |
| Agent Loop | `services/agent_loop.py`（Phase 5） | L4 LangGraph 迭代工具调用（5 NL2SQL tools） |
| Metric Pipeline | `services/metric_promotion_service.py`（Phase 5） | 冷指标自动晋升 DRAFT + 路由层计量聚合 |
| 图表渲染 | `services/chart_service.py`（Phase 4） | ECharts Option 生成 |
| 数据源 | `services/datasource_service.py`（Phase 3） | 动态引擎池 + 只读执行 |

## 数据流（对话出图，详见 `设计02.md` 15 步时序）

1. 前端 POST `/api/v1/chat`。
2. 意图识别（查询 vs 知识指令）。
3. 加载会话本体上下文。
4. 模型路由选模型（按 Token 估算成本）。
5. NL2SQL 生成 SQL（注入本体 schema），SQL Guard 校验。
6. 业务库执行只读查询。
7. 图表渲染生成 ECharts Option。
8. 记录 Token 消耗，返回回复 + 图表 + SQL。

## 部署

Docker Compose：PostgreSQL（元数据）+ Neo4j（本体图）+ Milvus（向量）+ MySQL（Demo 业务库）+ backend + frontend。详见 `docker/docker-compose.yml`。

## Phase 9 增量：数据质量评估报告（异步化）

**提交者**：Claude · **日期**：2026-09-15
**关联变更**：[feat-dq-scores-multiselect](../changes/feat-dq-scores-multiselect/summary.md) · [feat-dq-evaluation-report](../changes/feat-dq-evaluation-report/summary.md) · [feat-dq-evaluation-report-progress](../changes/feat-dq-evaluation-report-progress/summary.md)

### 新增组件

| 组件 | 位置 | 职责 |
|------|------|------|
| 评估报告 service | `services/evaluation_report_service.py` | `createReport` 仅写 PENDING 行；`runSnapshotJob` 自管 session 跑 snapshot；`getProgress` 读 JSONB 进度 |
| 评估报告 API | `api/v1/evaluation_report.py` | 5 个端点 + `GET /reports/{id}/progress`（必须注册在 `/{report_id}` 之前） |
| BackgroundTasks | FastAPI 依赖 | `createEvaluationReport` 加 `background_tasks.add_task(runSnapshotJob, report.id)` |
| 数据质量评分 service | `services/data_quality_score_service.py` | scope 字段：单 `datasource_id` + 多 `target_tables: list[str]` + 多 `rule_types: list[RuleType]`；空列表走全量 |

### 状态机

```
DRAFT ──┐
        │（创建向导提交）
        ▼
     PENDING ──── BackgroundTasks 立即拉起 ────┐
        │                                    ▼
        │                                RUNNING
        │                                    │
        │                            ┌───────┴───────┐
        │                            ▼               ▼
        │                       COMPLETED         FAILED
        │
        ▼（regenerate 重新生成）
     PUBLISHED（业务可见终态）
```

### 关键设计点

1. **进度 JSONB 列**：`evaluation_report.progress` 存 `{stage, completed, total, current_rule_id, current_rule_code, message, started_at, finished_at}`。每个进度更新独立 commit，不做 per-rule commit（避免 N 次 DB 写）。
2. **session 自管**：`runSnapshotJob` 内部用 `app.infrastructure.database.getSessionFactory()` 拿新 session，**不**复用请求 session（请求结束后请求 session 会 close）。
3. **catch-all 兜底**：snapshot 任何异常都写 `status=FAILED + progress.message`，绝不挂后台进程。
4. **前端轮询**：detail 页 useEffect 监听 status，PENDING/RUNNING 时 `setInterval(1000)`；COMPLETED/FAILED 时拉一次完整 report 后停。
5. **scope IN 组合**：空列表走 None 短路（避免 SQL `IN ()` 语法错）；AND 组合三条件；scope 命中 0 条规则 → 空响应、不写库。

### 风险门禁（参照 开发流程规范 §真实数据验证）

- 评估耗时受业务库 SQL 计划器影响大；snapshot 异步化后才能上 docker compose 冒烟。
- `progress` JSONB 内层键不被 Pydantic alias_generator 改写，前端先按 snake_case 读（Phase 4 强类型化时切 camelCase）。
- CheckConstraint 扩为 6 值（migration 0074）；降级路径已写明，alembic downgrade 即可回滚。
