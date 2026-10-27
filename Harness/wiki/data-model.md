# 数据模型

## 元数据库（PostgreSQL）

### llm_config
模型配置：`model_name`(唯一)、`provider`、`api_endpoint`、`api_key_encrypted`(Fernet 加密)、`cost_per_1k_input/output`、`max_input_tokens`、`weight`(0-100)、`cost_threshold`、`is_active`、时间戳。

### session_token_usage
会话 Token 流水：`session_id`、`model_config_id`(FK)、`model_name`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`cost`、`request_time`、`purpose`。索引 `(session_id, request_time)` 与 `(model_config_id, request_time)`。

### 后续表（Phase 2-3）
- `ontology_class`：本体类，`source_table` 映射物理表，支持 `parent_class_id` 继承，`version` + `valid_from/to` 版本。
- `ontology_property`：属性，`data_type`、`is_primary_key`、`is_foreign_key`、`ref_class_id`、`source_column`。
- `ontology_metric`：指标，`formula`、`agg_function`、`target_class_id`、`dimension_defaults`(JSON)。
- `data_source`：数据源，`type`、`connection_url`、`encrypted_password`、`is_read_only`、`is_default`。
- `session_message`（Phase 5）：会话消息持久化，`session_id`、`role`(user/assistant)、`content`、`question`(assistant 回填)、`sql_generated`(assistant 回填)、时间戳。索引 `(session_id, created_time)`。用于 NL2SQL 前注入最近 5 轮上下文；服务端无记录时回退到客户端 `history` 字段。注入前按预算收口（H3，2026-09-26）：单条正文/历史 SQL 各限 500 字符，拼接后总预算 4000 字符，超预算丢最旧整块并记 `历史上下文按预算裁剪` —— 上下文会被注入 plan/SQL/answer 各阶段，不限长则长轮次下每轮 prompt 成本线性膨胀。**Migration 0051** 新增字段：`routing_layer`（VARCHAR(10)，L1/L2/L3/L4）、`latency_ms`（INTEGER，毫秒）、`token_cost_usd`（FLOAT，美元）。**Migration 0105** 新增字段：`chart_type`（VARCHAR(20)，NULL）、`chart_option`（JSONB，NULL）—— 让图表进最终报告（聊天回答 + 导出 PDF）并让**历史回放也能看到图**。两条必须在**唯一落库点**（`chat_context._storeSessionMessages`，归一函数在 `chat_chart_persist.py`）做的归一：`chart_type` 归一为裸字符串（`ChatResponse.chartType` 是 `ChartType | None`，pydantic 保留**枚举成员**，直接落 VARCHAR 会存成 `ChartType.BAR` 这种脏值）；`chart_option` 的 TABLE 负载 `rows` 截到 200 行并记 `truncated: true`（`QUERY_ROW_LIMIT` 默认 0 = 不限行，而表格 builder 返回**全量** data，不截则有把十万行塞进一行的风险）。**只截落库份**，实时响应仍发全量；`truncated` 必须在**读侧两侧**都披露（PDF 与前端回放）。

### entity_mapping（跨系统编码映射 SSOT）

**日期**：2026-09-30 · **变更**：[entity-mapping-sync-and-key-space](../changes/2026-09-30-entity-mapping-sync-and-key-space/summary.md)
（前次：[fix-entity-mapping-sync-bootstrap](../changes/fix-entity-mapping-sync-bootstrap/summary.md) 2026-09-16）

- 列：`id`、`entity_type`(FK → `business_object.code`，枚举 SUPPLIER/MATERIAL/PO/GR/IQC)、
  `enterprise_key`(BIGINT)、`enterprise_code`、`source_system`(枚举 ERP/SRM/QMS/MDM/PLM)、
  `source_key`、`source_code`、`match_rule`(枚举 MDM_MASTER/BUSINESS_KEY/MAPPING)、
  `effective_date`、`expiry_date`、`name`(业务名，仅 SUPPLIER/MATERIAL 同步脚本写入)、
  `owner`、时间戳
- 唯一索引：`uq_entity_mapping_entity_source` `(entity_type, enterprise_key, source_system)`
- 业务用法：把多个源系统的同物编码统一映射到平台级 `enterprise_key`，供 360° / 风险 /
  AutoComplete 等直接入口按平台级 key 查询

**enterprise_key 区间分配**（避免碰撞）：

派生唯一实现在 `app/domain/enterprise_key.py`（脚本与 `entity_mapping_service` 均 import，
不再各存一份）。`KEY_RANGE_SIZE = 2⁴⁸`。

| entity_type | 区间 | 来源 |
|---|---|---|
| MATERIAL（demo） | 200001–200010 | `seed_entity_mapping.py` 测试 fixture（10 个 RM-STEEL-***） |
| PO（demo） | 300001–300003 | `seed_entity_mapping.py` 测试 fixture |
| GR（demo） | 400001 | 同上 |
| IQC（demo） | 500001 | 同上 |
| SUPPLIER（真实） | 1000000 – 1000000+2⁴⁸ | `stableKey(code, offset=SUPPLIER_KEY_OFFSET)` |
| MATERIAL（真实） | 1000000+2⁴⁸ – 1000000+2×2⁴⁸ | 同上 + `MATERIAL_KEY_OFFSET` |
| 其它未注册类型 | 1000000+2×2⁴⁸ 起 | 通用段（新增业务对象不必改该模块） |

> **2026-09-30 区间重划（不兼容）**：旧区间为 800000–4295767295 / 4295767296–8591534591
> （`KEY_RANGE_SIZE = 2³²`）。2³² 对 35 万条太小 —— MATERIAL 350922 条实测碰撞 **12 次**，
> `ON CONFLICT DO UPDATE SET name` 把 12 对编码静默折叠成 12 行（后者编码消失、
> 前者 name 被覆盖），rowcount 仍报满额。扩到 2⁴⁸ 后碰撞期望 14.3 → 2.2e-4。
> 键值因此全变，需清空 `entity_mapping` 后重跑同步（354422 行可由 THBI 确定性重放）。
> 无外部表引用该 BIGINT（`feature_value.entity_key` / `document_entity_relation.entity_key`
> 均为 VARCHAR 业务码）。

**维护契约**：

| 场景 | 工具 | 幂等 |
|---|---|---|
| 部署后初始化 demo 数据 | `seed_entity_mapping.py` | ✅ ON CONFLICT DO UPDATE 仅写 `expiry_date` |
| 从 THBI 数仓拉真实主数据 | `sync_entity_mapping_from_thbi.py`（`--dry-run` 预览，正式跑写入） | ✅ ON CONFLICT DO UPDATE SET name（重跑时刷新业务名） |
| 临时补缺 | AdminUI `/admin/entity-mappings` 手工 CRUD（`POST/PUT/DELETE /api/v1/entity-mappings`） | — |

**同步脚本调用契约**（2026-09-16 修复后）：

```bash
docker exec \
  -e DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata' \
  -e QUERY_TIMEOUT_SECONDS=600 \
  qa-backend python scripts/sync_entity_mapping_from_thbi.py [--dry-run]
```

- `QUERY_TIMEOUT_SECONDS` 必须 ≥ 600（DIM_IMATERIAL 35w 行 SELECT + fetchmany 全程 > 30s 默认值）
- **源表**：`THBI.DIM_SUPPLIER(BPSNUM_0/BPSNAM_0)`、`THBI.DIM_IMATERIAL(ITMREF_0/ITMDES1_0..3)`。
  2026-09-30 前写的 `DWD_SUPPLIER` / `DWD_MATERIAL` 在库里已不存在（实跑 ORA-00942），
  契约由 `test_sync_entity_mapping_from_thbi.py::TestSourceTables` 钉住。
- 脚本按 `(is_default=true, is_active=true)` 查数据源，**不硬编码 name** — 避免「重命名即失配」
- 拉取列名按小写键读取（adapter `execute_read_only` 统一下沉小写）
- 部署：脚本随镜像走，改脚本后需 `docker compose build backend && docker compose up -d backend`
  （镜像==代码；`deploy_backend.sh` 只是 Docker Hub 不可达时的临时替代）

**AutoComplete 按业务名搜索**（2026-09-30 补齐）：

- `searchMappings` 的 conds 增加 `EntityMapping.name.ilike(like)`（第三条）
  —— 此前只有 `enterprise_code` / `source_code`，业务名虽已入库却搜不到
  （实测 `name ilike '%浙江力航%'` 命中 1 行，原两条子句命中 0 行）。
  中缀 ILIKE 由 alembic 0086 的 GIN trigram 索引（partial: `entity_type='SUPPLIER'`）支撑。
- `createMapping` 补写 `name=dto.name`。DTO 一直声明该字段、bulk 路径也写，
  唯独单条 create 漏写 ⇒ 带 name 的 POST 返 201 却静默丢弃，`name` 列永远是空。
  由集成测试 `test_create_persists_business_name` 钉住。
- 仍缺：`EntityMappingUpdate` 无 `name` 字段（改名前只能走 bulk 或重建）。

**待办（不在本 fix 范围）**：

- 周期性同步任务（launchd / scheduler）
- Prometheus 指标 + Alertmanager 行数告警

## 本体图（Neo4j）

```
(:Class {id, name, alias, description, sourceTable})
(:Property {id, name, alias, dataType, sourceColumn})
(:Metric {id, name, alias, formula, aggFunction})

(:Class)-[:HAS_PROPERTY]->(:Property)
(:Property)-[:REFERENCES]->(:Class)
(:Metric)-[:DERIVED_FROM]->(:Class)
```

## 向量库（Milvus）

- `ontology_embeddings`：类/属性/指标名称与描述的向量，用于语义检索。
- `query_embeddings`：历史查询向量（Phase 5），推荐相似问法。

## 跨库主键兼容

ORM 主键用 `BigInteger().with_variant(Integer, "sqlite")`：生产 PostgreSQL 用 BIGINT 自增，测试 SQLite 用 INTEGER 自增（SQLite 仅 INTEGER PRIMARY KEY 自增）。

## Phase 9 增量：数据质量评估报告表

**日期**：2026-09-15 · **变更**：[feat-dq-evaluation-report](../changes/feat-dq-evaluation-report/summary.md) · [feat-dq-evaluation-report-progress](../changes/feat-dq-evaluation-report-progress/summary.md)

### evaluation_report

```
id                 BIGINT PK
name               VARCHAR(255)
datasource_id      BIGINT FK → data_source.id
class_ids          JSONB                -- [int, ...] 业务对象 id 数组
time_window_start  TIMESTAMPTZ
time_window_end    TIMESTAMPTZ
status             VARCHAR(20) CHECK IN ('DRAFT','PUBLISHED','PENDING','RUNNING','COMPLETED','FAILED')
snapshot           JSONB                -- 评估结果（KPI/维度/规则明细/违规样本）
progress           JSONB                -- 异步进度 {stage,completed,total,current_rule_id,current_rule_code,message,started_at,finished_at}
created_at         TIMESTAMPTZ
updated_at         TIMESTAMPTZ

索引：
  ix_evaluation_report_status_running  (status) WHERE status = 'RUNNING'   -- 部分索引，加速 dashboard 查询
  ix_evaluation_report_datasource_id   (datasource_id)
```

**CheckConstraint**：6 个状态值（DRAFT/PUBLISHED 是业务可见性；PENDING/RUNNING/COMPLETED/FAILED 是评估态）。

**进度 JSONB 结构**（前端按 snake_case 读，Pydantic alias_generator 不递归）：

```json
{
  "stage": "RUNNING",
  "completed": 5,
  "total": 12,
  "current_rule_id": 42,
  "current_rule_code": "PO_NOT_NULL_QTY",
  "message": null,
  "started_at": "2026-09-15T10:30:00Z",
  "finished_at": null
}
```

### evaluation_report_rule（关联表）

```
id                  BIGINT PK
report_id           BIGINT FK → evaluation_report.id (ON DELETE CASCADE)
rule_id             BIGINT FK → data_quality_rule.id
class_id            BIGINT FK → ontology_class.id
snapshot_status     VARCHAR(20)          -- 该规则在 snapshot 里的 PASS/FAIL/ERROR
snapshot_pass_rate  FLOAT
snapshot_message    TEXT
```

### 迁移文件

- `alembic/versions/0069_dq_evaluation_report.py` — 初次建表（status 只有 DRAFT/PUBLISHED）
- `alembic/versions/0074_dq_eval_report_progress.py` — 扩 CheckConstraint + 新增 progress 列 + ix_evaluation_report_status_running

## Phase 环节一 增量：研究会话证据契约表

**日期**：2026-10-13 · **变更**：[feat-research-evidence-contract](../changes/feat-research-evidence-contract/summary.md)
· **Wiki**：[[research_evidence_contract]]

### research_run_state（本轮问题的版本化快照）

```
id               UUID PK
session_id       UUID FK → research_session.id (ON DELETE CASCADE)
turn_id          UUID FK → research_turn.id    (ON DELETE CASCADE)
parent_run_id    UUID FK → research_run_state.id (ON DELETE SET NULL)   -- 版本链
current_question TEXT NOT NULL DEFAULT ''
mode             VARCHAR(20) NOT NULL
scope_version    INTEGER NOT NULL DEFAULT 1
datasource_id    INTEGER
schema_version   INTEGER NOT NULL DEFAULT 1
model_id         INTEGER
state_payload    JSONB NOT NULL DEFAULT '{}'::jsonb
created_at / updated_at TIMESTAMPTZ

索引：ix_research_run_state_session_id (session_id)
      ix_research_run_state_turn_id    (turn_id)
被引用：research_session.active_run_id            FK (ON DELETE SET NULL)
        research_step_result.run_state_id         FK (ON DELETE CASCADE)
        research_run_state.parent_run_id          FK (自引用, ON DELETE SET NULL)
```

**承重语义**：续跑**新建**一行（新 UUID）而不是原地改 —— 所以「同一 turn 的最新版本」不是某一行的属性，
只能按 `created_at` 序取；`turn_id` 在版本化时**被复制**（不变）⇒ 按 `turn_id` 能覆盖一个 turn 的
**所有**版本。只按单个 `run_state_id` 读会漏掉早期版本的行。

### research_step_result（每步真实结果）

```
id               UUID PK
run_state_id     UUID FK → research_run_state.id (ON DELETE CASCADE)
step_index       INTEGER NOT NULL
step_type        VARCHAR(50) NOT NULL
sql_text         TEXT
raw_result_ref   JSONB        -- {"rows": [...], "rowCount": n, "schemaVersion": 1}
evidence_summary JSONB        -- EvidenceSummary（数值标量串化）
status           VARCHAR(20) NOT NULL   -- success / empty / failed
error            TEXT
created_at       TIMESTAMPTZ

索引：ix_research_step_result_run_state_id (run_state_id)
```

**没有 `turn_id` 列**（有意）：挂 `run_state_id`，要按 turn 读必须
`JOIN research_run_state ON run_state_id WHERE research_run_state.turn_id = :t`。

### research_session 增列

```
active_run_id    UUID FK → research_run_state.id (ON DELETE SET NULL)  -- 会话级执行权互斥
idempotency_key  VARCHAR            -- 重复触发去重（原子条件 UPDATE 认领）
```

### 迁移文件

- `alembic/versions/b1ea6cb32588_run_state.py` — `research_run_state` 建表
- `alembic/versions/0118_research_active_run.py` — `research_session.active_run_id` + FK + 索引
- `alembic/versions/0119_research_step_result.py` — `research_step_result` 建表

生产库 head：`0119`（2026-10-13 部署冒烟实测）。

> 文件名长度约束：≤ 32 字符（见 [[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/alembic-version-filename-32-char-limit]]）。
> 本批 0118 / b1ea6cb32588 初版超限（35 / 38），2026-10-13 改短（27 / 25）。

---

## Phase 环节二 增量：研究平台级表

**日期**：2026-10-13 · **变更**：[feat-research-platform](../changes/feat-research-platform/summary.md)
· **Wiki**：[[research_platform_capabilities]]
· **前置**：[§环节一](#phase-环节一-增量研究会话证据契约表)

### research_event_outbox（事件台账，SSE 断线回放）

```
seq         BIGSERIAL PK                    -- 单调游标（弃 created_at：同刻并列会漏/重）
session_id  UUID NOT NULL
event       VARCHAR(50) NOT NULL
payload     JSONB NOT NULL DEFAULT '{}'::jsonb   -- 先过 research_json 再落库（Decimal→str）
created_at  TIMESTAMPTZ NOT NULL

索引：ix_research_event_outbox_session_id (session_id)
```

**承重语义**：**先落库、再广播**（写用**独立短会话** ⇒ 事件活过状态机 rollback）。
**刻意无 `run_state_id`**（emit 绑定早于 run_state 创建，回放按会话）、**无 UUID event_id**（冗余）。
无 TTL / 无会话删除级联（**遗留**：孤儿行留存，见变更记录 §7）。

### research_task（任务台账，认领/回收）

```
id            UUID PK
session_id    UUID FK → research_session.id (ON DELETE CASCADE)
turn_id       UUID FK → research_turn.id    (ON DELETE CASCADE)
kind          VARCHAR(20) NOT NULL
status        VARCHAR(20) NOT NULL          -- pending / leased / done / failed / orphaned
attempt       INTEGER NOT NULL DEFAULT 0
max_attempts  INTEGER NOT NULL DEFAULT 1
worker_id     VARCHAR(64)
lease_until   TIMESTAMPTZ
payload       JSONB NOT NULL DEFAULT '{}'::jsonb   -- 含相位游标（崩溃时知道跑到哪个相位）
error         TEXT
created_at / updated_at TIMESTAMPTZ

索引：ix_research_task_session_id (session_id)
      ix_research_task_turn_id    (turn_id)
      ix_research_task_lease_until (lease_until)
```

**承重语义**：认领是**原子条件 UPDATE**（无 TOCTOU）；启动期 **reaper** 把 `leased` 孤儿翻 `orphaned`
并**释放会话 `active_run_id`**。**边界**：只回收 `leased`；`pending` 与「启动时 `awaiting_user` 且持权」
是合法态（见 memory `qa-system-research-task-reaper-scope`）。

### 迁移文件

- `alembic/versions/0120_research_event_outbox.py` — `research_event_outbox` 建表（29 字符）
- `alembic/versions/0121_research_task.py` — `research_task` 建表（21 字符）

迁移链：`0117 → b1ea6cb32588_run_state → 0118 → 0119 → 0120 → 0121`（**非**线性 0117→0118；
`0118` 的 `down_revision` 是 hash 命名的 `b1ea6cb32588`）。生产库 head：`0121`（2026-10-13 部署冒烟实测）。

## Phase 6.6 增量：通用多工具 Agent 编排

**日期**：2026-10-28 · **变更**：[feat-agent-orchestration](../changes/feat-agent-orchestration/summary.md)
· **Wiki**：[[agent-runtime]]

**只新增，不改既有表**（设计稿 §2.2 的非回归底线）。既有 6 张 Agent 相关表
（`agent_definition` / `agent_access_policy` / `agent_tool_config` / `agent_schedule` /
`agent_run_log` / `session_token_usage`）**零改动**——编排配置与运行记录全部落在
下面 5 张新表里。

### agent_orchestration_profile（一个 Agent 一份编排配置）

```
id                     BIGINT PK
agent_id               BIGINT FK → agent_definition.id (ON DELETE CASCADE)  -- UNIQUE
instructions           TEXT NOT NULL         -- 编排指令（进 LLM 前过 prompt sanitizer）
enabled                BOOLEAN NOT NULL DEFAULT false    -- 新建一律停用，需显式 toggle
default_max_iterations INTEGER NOT NULL DEFAULT 20
default_max_tool_calls INTEGER NOT NULL DEFAULT 30
default_max_cost_usd   NUMERIC(16,8) NOT NULL DEFAULT 5
default_timeout_seconds INTEGER NOT NULL DEFAULT 900
max_observation_chars  INTEGER NOT NULL DEFAULT 50000
revision               INTEGER NOT NULL DEFAULT 1        -- 乐观锁
created_time / updated_time TIMESTAMPTZ
```

约束：`uq_agent_orchestration_profile_agent`（一个 Agent 只一份）、
`ck_agent_orchestration_profile_limits`（各项上限 > 0 / 成本 ≥ 0）、
`ck_agent_orchestration_profile_revision`（`revision >= 1`）。

### agent_orchestration_binding（profile ↔ 工具绑定）

```
id            BIGINT PK
profile_id    BIGINT FK → agent_orchestration_profile.id (ON DELETE CASCADE)
tool_name     VARCHAR(64) NOT NULL       -- 指向 agent_tool_config.name
enabled       BOOLEAN NOT NULL DEFAULT true
sort_order    INTEGER NOT NULL DEFAULT 0
```

约束：`uq_agent_orchestration_binding_tool`（`profile_id` + `tool_name` 唯一）、
`ck_agent_orchestration_binding_sort_order`（`sort_order >= 0`）。
**表里不存 handler_ref** —— handler 一律从 `agent_tool_config` 现读，杜绝动态 handler 注入。

### agent_orchestration_run（一次编排运行）

```
run_id                  VARCHAR(36) NOT NULL   -- UNIQUE，外部标识
agent_id / agent_code   BIGINT / VARCHAR(64)   -- agent_id 可空（Agent 被删后历史仍在）
profile_revision        INTEGER NOT NULL       -- 快照用的 profile 版本
instructions_snapshot   TEXT NOT NULL          -- 指令快照
tool_bindings_snapshot  JSONB NOT NULL         -- 工具绑定快照（执行时按它约束）
max_observation_chars   INTEGER NOT NULL
question                TEXT NOT NULL
created_by              VARCHAR(64) NOT NULL
model_config_id         BIGINT NOT NULL
status                  VARCHAR(32) NOT NULL DEFAULT 'QUEUED'
max_iterations / max_tool_calls INTEGER NOT NULL
max_cost_usd            NUMERIC(16,8) NOT NULL
timeout_seconds         INTEGER NOT NULL
iteration_count / tool_call_count / prompt_tokens / completion_tokens / total_tokens INTEGER NOT NULL DEFAULT 0
cost                    NUMERIC(20,8) NOT NULL DEFAULT 0
cost_source             VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'   -- MEASURED/ESTIMATED/UNKNOWN/MIXED
cost_complete           BOOLEAN NOT NULL DEFAULT false
final_answer            TEXT                   -- 可空
pause_code              VARCHAR(64)            -- 可空：暂停原因
pause_detail            JSONB                  -- 可空
error_code / error_message                       -- 可空
cancel_requested        BOOLEAN NOT NULL DEFAULT false
lease_owner / lease_until                        -- Worker 租约（可空）
revision                INTEGER NOT NULL DEFAULT 1
queued_at / started_at / finished_at TIMESTAMPTZ
created_time / updated_time TIMESTAMPTZ
```

索引：`uq_agent_orchestration_run_run_id`（`run_id` 唯一）、
`ix_agent_orchestration_run_status_id`（`status`,`id`——Worker 扫描队列用）、
`ix_agent_orchestration_run_creator_time`（`created_by`,`created_time`——「我的运行」列表）、
`ix_agent_orchestration_run_agent_time`（`agent_id`,`created_time`）。

### agent_orchestration_step（运行的轨迹）

```
id            BIGINT PK
run_id        BIGINT FK → agent_orchestration_run.id (ON DELETE CASCADE)
step_no       INTEGER NOT NULL        -- 全局递增序号
iteration_no  INTEGER NOT NULL        -- 第几轮
kind          VARCHAR(32) NOT NULL    -- PLAN / TOOL
status        VARCHAR(32) NOT NULL DEFAULT 'RUNNING'   -- RUNNING/SUCCEEDED/FAILED/SKIPPED
action        VARCHAR(32)             -- 可空：CALL_TOOL / FINISH / ASK_USER
plan_summary / answer TEXT            -- 可空
tool_name     VARCHAR(64)             -- 可空
input_json / output_json JSONB        -- 可空
error_code / error_message            -- 可空
prompt_tokens / completion_tokens INTEGER NOT NULL DEFAULT 0
cost          NUMERIC(20,8) NOT NULL DEFAULT 0
started_at    TIMESTAMPTZ NOT NULL
finished_at   TIMESTAMPTZ             -- 可空
```

索引：`ix_agent_orchestration_step_run_step`（`run_id`,`step_no`）。

### agent_orchestration_usage（计量台账）

```
id              BIGINT PK
run_id          BIGINT FK → agent_orchestration_run.id (ON DELETE CASCADE)
step_id         BIGINT FK → agent_orchestration_step.id (ON DELETE SET NULL)  -- 可空
caller_type     VARCHAR(32) NOT NULL    -- PLANNER / TOOL
model_config_id BIGINT NOT NULL
model_name      VARCHAR(50) NOT NULL
prompt_tokens / completion_tokens INTEGER NOT NULL DEFAULT 0
cached_tokens   INTEGER                 -- 可空
cost            NUMERIC(20,8) NOT NULL DEFAULT 0
cost_source     VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'
call_status     VARCHAR(32) NOT NULL    -- SUCCEEDED / OUTPUT_INVALID / FAILED
request_time    TIMESTAMPTZ NOT NULL
```

### 迁移文件

- `alembic/versions/0135_agent_orchestration.py` — 建 5 张表 + 5 个索引（revision id
  `0135_agent_orchestration` = **24 字符**，文件名含 `.py` = **27 字符**；
  `alembic_version.version_num` 是 `varchar(32)`，≤ 32 约束内）
  - revision `0135`，`down_revision = "0134"`（线性接在 `0134_data_layer_rule` 之后）
  - **downgrade 会 DROP 这 5 张表** ⇒ 回滚口径是「停 Worker + 关 flag」，**不做 downgrade**（见灰度手册 §5）

**已知缺口（不阻塞上线，但要记账）**：

- `(run_id, iteration_no, kind)` **没有** DB 级唯一索引（靠应用层保证幂等）；
- `agent_orchestration_usage` **没有** `run_id` 索引（用量台账按 run 聚合时会全表扫）。
