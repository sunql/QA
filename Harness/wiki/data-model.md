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
- `session_message`（Phase 5）：会话消息持久化，`session_id`、`role`(user/assistant)、`content`、`question`(assistant 回填)、`sql_generated`(assistant 回填)、时间戳。索引 `(session_id, created_time)`。用于 NL2SQL 前注入最近 5 轮上下文；服务端无记录时回退到客户端 `history` 字段。**Migration 0051** 新增字段：`routing_layer`（VARCHAR(10)，L1/L2/L3/L4）、`latency_ms`（INTEGER，毫秒）、`token_cost_usd`（FLOAT，美元）。

### entity_mapping（跨系统编码映射 SSOT）

**日期**：2026-09-16 · **变更**：[fix-entity-mapping-sync-bootstrap](../changes/fix-entity-mapping-sync-bootstrap/summary.md)

- 列：`id`、`entity_type`(FK → `business_object.code`，枚举 SUPPLIER/MATERIAL/PO/GR/IQC)、
  `enterprise_key`(BIGINT)、`enterprise_code`、`source_system`(枚举 ERP/SRM/QMS/MDM/PLM)、
  `source_key`、`source_code`、`match_rule`(枚举 MDM_MASTER/BUSINESS_KEY/MAPPING)、
  `effective_date`、`expiry_date`、`name`(业务名，仅 SUPPLIER/MATERIAL 同步脚本写入)、
  `owner`、时间戳
- 唯一索引：`uq_entity_mapping_entity_source` `(entity_type, enterprise_key, source_system)`
- 业务用法：把多个源系统的同物编码统一映射到平台级 `enterprise_key`，供 360° / 风险 /
  AutoComplete 等直接入口按平台级 key 查询

**enterprise_key 区间分配**（避免碰撞）：

| entity_type | 区间 | 来源 |
|---|---|---|
| MATERIAL（demo） | 200001–200010 | `seed_entity_mapping.py` 测试 fixture（10 个 RM-STEEL-***） |
| PO（demo） | 300001–300003 | `seed_entity_mapping.py` 测试 fixture |
| GR（demo） | 400001 | 同上 |
| IQC（demo） | 500001 | 同上 |
| SUPPLIER（真实） | 800000–4295767295 | `sync_entity_mapping_from_thbi.py` SHA-256 前 8 字节 + offset 800000 |
| MATERIAL（真实） | 4295767296–8591534591 | 同上 + MATERIAL offset |

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

- `QUERY_TIMEOUT_SECONDS` 必须 ≥ 600（DWD_MATERIAL 35w 行 SELECT + fetchmany 全程 > 30s 默认值）
- 脚本按 `(is_default=true, is_active=true)` 查数据源，**不硬编码 name** — 避免「重命名即失配」
- 拉取列名按小写键读取（adapter `execute_read_only` 统一下沉小写）
- 部署：仅需 `docker cp` 单文件到 `/app/scripts/`，无需重启 uvicorn

**待办（不在本 fix 范围）**：

- `entity_mapping_service.searchMappings` 增加 `EntityMapping.name.ilike(like)` 子句，使 AutoComplete 支持中文名搜索
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

> 文件名长度约束：≤ 32 字符（见 [[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/alembic-version-filename-32-char-limit]]）。
