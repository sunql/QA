# 系统架构

## 概述

智能问答系统：用户用自然语言提问，系统经多模型路由调用 LLM，将意图转为 SQL（NL2SQL），在可配置业务库上执行只读查询，并自动渲染 ECharts 图表。知识沉淀为本体（Ontology）。

NL2SQL 的**实际路由**（Phase 6.4）有三条路径：**L1** KPI 语义匹配 →（未命中）**L2** LLM 单 SQL；
多步子问题由独立的 `_executeMultiStep` 承担（同样记 `routing_layer="L2"`）；**L4** Agent Loop
由 `IntentType.AGENT_RUN` 意图**单独触发**（**不是** L2 失败后自动升级），实现在
`app/services/agent_runtime_service.py`（纯 Python async while loop，非 LangGraph）。
⚠️ 设计里的「**L3 CTE 链**」作为独立层**从未在生产接线**，已于 **2026-09-27 删除**
（仅保留 `prior_cte` 片段能力）。详见 `wiki/nl2sql-engine.md`（4 层路由章节）。

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
| Agent Loop | `app/services/agent_runtime_service.py:593`（Phase 6.4） | L4 纯 Python async while loop 工具调用（5 NL2SQL tools），非 LangGraph |
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

## Schema 漂移治理：ORM ↔ Alembic ↔ PostgreSQL 三方对齐

**提交者**：Claude · **日期**：2026-11-02
**关联变更**：[fix-schema-ssot-alignment](../changes/fix-schema-ssot-alignment/summary.md)

### 1. 三方对齐模型：意图源 / 变更历史 / 事实

- `Base.metadata`（ORM 声明）= 唯一的**意图源** —— 「这个表应该长什么样」；
- Alembic 迁移链 = **变更历史** —— 意图是怎么一步步落到 DB 上的；
- 真实 PostgreSQL = **事实** —— 现在实际长什么样。

drift 工具（`app/infrastructure/schema_drift.py`）比对「ORM 声明」与「真实 DB」；
Alembic 负责把 ORM 声明翻译成 DB 上的 DDL。三者任一不一致都会出问题，且**方向不同**：

- **ORM 漏声明** ⇒ drift 报 `extra_index` / `extra_column`（warning，不阻断）；
  更危险的是 `alembic autogenerate` 会**反过来提议 DROP**这些对象。
- **DB 漏建** ⇒ drift 报 `missing_index` / `missing_column`（**blocking**，容器起不来）。

⇒ 「ORM 漏声明」在启动期只是噪音，在迁移期却是**数据丢失级**风险。
这是漂移治理里最需要盯住的非对称性。

### 2. 本次对齐做了什么

4 处补齐，全部在 **ORM 侧**（零 DDL、零新迁移、零生产写）：

1. **模块注册** —— 4 个模型模块（`mcp_models` / `multi_step_models` / `research_models` /
   `wiki_graph_insight`）没进 `Base.metadata`，独立 CLI 看不到它们 ⇒ 被误报 `extra_table`，
   而 `autogenerate` 会提议 **DROP 13 张活表**；
2. **漏列** —— `knowledge_claim.confidence_level`（迁移 `0100` 已建，ORM 未声明）；
3. **33 个索引** —— 32 个 `Index` + 1 个 `UniqueConstraint` 补进 `__table_args__`
   （列序 / unique / method / partial 谓词均对着 `pg_indexes` 实测抄）；
4. **1 处谓词** —— `ix_evaluation_report_status_running` 的 ORM 谓词由
   `status IN ('PENDING','RUNNING')` 改回迁移 `0074` 与两个真实库的
   `status = 'RUNNING'`。

结果：**drift 可见的漂移清零** —— 全新迁移库上 `--strict` 实测 **exit 0**；
`autogenerate` 的「DROP 活表」地雷被拆掉。

⚠️ **但这不是「无差异」**。用 alembic **1.19.1** 只读跑 `alembic check` 实测（base `7d28d08a`
→ 本批后 HEAD）：

| 类别 | 本批前（base） | 本批后（HEAD；ORM 侧自 `c618e060` 起未再变动） |
|---|---|---|
| `removed table` | 14（13 张未注册活表 + `po_lines_sandbox`） | **0**（**全新迁移库口径**；5434 共享库上 = **1** —— `po_lines_sandbox`，测试自建的残留） |
| `removed index` | 64 | **6** |
| `removed column` | 1（`knowledge_claim.confidence_level`） | **0** |
| `removed check constraint` | 20 | **20（本批未收敛 —— 见下方 §3）** |

`alembic check` 在全新迁移库上仍报 **56 条**残余差异（本批明确不覆盖，需 DDL + 数据迁移
评估）。详见变更记录
[`Harness/changes/fix-schema-ssot-alignment/summary.md`](../changes/fix-schema-ssot-alignment/summary.md) §8.6。

### 3. drift 工具的盲区（重要）

drift 工具**只比**：表集合、列名、索引名 + 有序列元组。
**不比**：列类型、列 comment、索引 unique 标志、**索引谓词**、opclass、排序方向、
**CHECK 约束**。

而 **alembic 1.19.1 全包不比较索引谓词** —— `postgresql_where` 在整个 alembic 包里出现
**0** 次，`alembic/ddl/impl.py` 里 `"where"` 出现 **0** 次。
（实测环境 = 项目 venv / `uv.lock` 锁定的 `alembic-1.19.1`；`pyproject.toml` 只写
`alembic>=1.13.0` ⇒ **升级 alembic 后需重新复核本结论**。）
⇒ 谓词分歧**永远不会被 `alembic check` / autogenerate / drift 报出来**，只能靠专门测试钉住
（本批新增 `test_running_status_index_predicate_matches_migration`）。

⇒ **「drift 全绿」≠「ORM 与 DB 语义全等」。** 判据要分层看：

| 层 | 谁负责 | 覆盖 |
|---|---|---|
| 表 / 列 / 索引的有序列集合 | drift 工具 | 启动期 blocking 门禁 |
| 列类型 / comment / 约束形状 / 排序方向 | `alembic check`（autogenerate diff） | 迁移级复核，**drift 看不见** |
| **CHECK 约束** | `alembic check`（autogenerate diff） | 迁移级复核，**drift 检测不到 CHECK 约束的缺失** |
| 索引谓词 / opclass | **无自动工具**，只能专用测试 | 靠 `test_orm_schema_ssot.py` |

⚠️ **CHECK 约束是当前最危险的一颗雷**（2026-11-02 实测）：DB 侧有 **48 个命名 CHECK 约束**
（`pg_constraint.contype='c'`），**ORM 侧已声明 28 个**（分布在 **13 张表**：
`agent_orchestration_binding` / `_profile` / `_run` / `_step` / `_usage` 共 17 个，
`agent_tool_config`、`audit_log`、`data_quality_score`、`evaluation_report`、
`evaluation_report_schedule`、`mcp_call_log`（3 个）、`permission_grant`、
`wiki_ontology_link`（`chk_link_type` / `chk_link_weight`）），
**缺 20 个**（48 − 28 = 20）⇒ `alembic check` 报出的 **20 条 `removed check constraint`**
与「ORM 未声明的那 20 个」集合**完全一致**（已实测比对）。
而 **drift 工具根本不比较 CHECK 约束** ⇒ **补没补 CHECK 约束，drift 都看不出来**
（drift 检测不到 CHECK 约束的缺失）⇒
**drift `--strict` 依然 exit 0，没有任何告警**。一旦有人跑
`alembic revision --autogenerate`，autogenerate 会生成 `op.drop_constraint('ck_*')`
**把数据校验静默删掉**（`ck_feature_value_not_both_null`、`ck_agent_status`、
`ck_entity_mapping_entity_type`、`ck_kpi_catalog_status`、`chk_link_granularity` 等 20 个）。
⇒ **在把缺的 CHECK 约束补进 ORM（或显式决定废弃它们）之前，禁止跑
`alembic revision --autogenerate`。**（补的时候**不要重复添加已声明的那 28 个**。）

另注：drift 工具的 `[alembic]` 检查**只比版本号**（DB 的 `alembic_version` vs 代码里的 head），
**不是** autogenerate 差异检查 —— 所以「`--strict` exit 0」与「`alembic check` 仍 FAILED」
可以同时成立，两者测的不是一回事。

### 4. 后续纪律

1. **模型与迁移同 PR**：新增 / 修改 ORM 模型必须在**同一个 PR** 里带 Alembic 迁移
   （见 [`Harness/rules/工程结构.md`](../rules/工程结构.md) 的「变更流程」段），
   否则启动期校验直接阻断；ORM 与迁移不同步是本次全部漂移的根。
2. **补声明要照抄实测定义**：ORM 侧补索引声明时，**列序、unique、method（btree/gin）、
   partial 谓词**都要对着 `pg_indexes` 实测抄，不要凭印象写 —— 抄错的代价是
   autogenerate 提议重建索引，或把「局部唯一」写成「全局唯一」。
3. **只读巡检用门禁脚本**：结构一致性用 `backend/scripts/fix_schema_ssot_realdata.py`
   （**只读**、可重复跑、退出码 0/1/2）复验，不要用 `Base.metadata.create_all` 建表来「对齐」。
4. **autogenerate 前先看 `alembic check`**：`alembic revision --autogenerate` 会把
   「ORM 未声明的 DB 对象」直接变成 `drop_*` 操作 —— 目前 DB 侧 48 个命名 CHECK 约束里
   **ORM 缺 20 个**（已声明 28 个，见 §3 的 ⚠️）⇒ **补齐前禁止跑 autogenerate**。
5. **alembic 升级后复核盲区结论**：`alembic>=1.13.0` 是开放区间，autogenerate 的比较逻辑
   会随版本变 ⇒ 「alembic 不比较索引谓词」这类结论是**版本相关**的，升级后需重新实测
   （当前实测版本 **1.19.1**）。
