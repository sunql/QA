# 数据模型

## 元数据库（PostgreSQL）

### llm_config
模型配置：`model_name`(唯一)、`provider`、`api_endpoint`、`api_key_encrypted`(Fernet 加密)、`cost_per_1k_input/output`、`max_input_tokens`、`weight`(0-100)、`cost_threshold`、`is_active`、时间戳。

### session_token_usage
会话 Token 流水：`session_id`、`model_config_id`(FK)、`model_name`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`cost`、`request_time`、`purpose`、`cost_source`。索引 `(session_id, request_time)` 与 `(model_config_id, request_time)`。

**`cost_source`（P1-2，migration 0126，2026-10-18 收尾）**：成本口径，取值为白名单 `measured`（token 由 provider 上报、单价来自 `llm_config`）、`estimated`（只有部分用量，如异常里提取的 token）、`unknown`（无法定价，如研究链路按名称查不到配置 ⇒ 成本是占位 0）。常量 SSOT 在 `app/services/token_usage_service.py`（`COST_SOURCE_*` / `COST_SOURCES`）。写入方：chat 主链路（默认 `measured`，retry-gen 为 `estimated`）、doc-QA、报告总结、研究 `LlmUsageRecorder`（无配置时 `unknown`）。**NULL = 0126 之前的早期行**，与 `unknown` 语义不同（后者是「当时确实无法定价」）。读侧 `GET /api/v1/sessions/{id}/usage/list` 以 `costSource` 暴露；前端 `UsagePage` 按白名单映射文案，未知值一律显示「未知」，**绝不把原始枚举拼进 i18n key**（缺 key 会回显 key 本身）。

### 后续表（Phase 2-3）
- `ontology_class`：本体类，`source_table` 映射物理表，支持 `parent_class_id` 继承，`version` + `valid_from/to` 版本。
- `ontology_property`：属性，`data_type`、`is_primary_key`、`is_foreign_key`、`ref_class_id`、`source_column`。
- `ontology_metric`：指标，**技术形态 SSOT**。现有列：`formula`(Text NN，逃生舱)、`agg_function`(varchar20 default 'SUM')、`target_class_id`(FK→ontology_class，单值，语义=role=BASE)、`dimension_defaults`(JSON，**零消费者**)。
  🔴 **该表在 NL2SQL 链路零消费者**（`nl2sql_service.py:34-35` 不 import、`nl2sql_prompts.py` 0 个 "etric"、`buildSchemaText` 只渲染类、`chat_recall` 用 `typeFilter="class"`）⇒ **补数据不改变任何一条 SQL**。
  扩展设计（`metric_type` / `numerator_measure` / `denominator_measure` / `aggregation_order` / `grain_keys` / `filter_expression` / `status` / `owner` / `execution_source`）见 [kpi-metric-technical-form](kpi-metric-technical-form.md)。
- `kpi_catalog`：KPI 业务目录，**治理 SSOT**。列：`kpi_code`(unique)、`kpi_name`、`business_definition`、`formula`、`numerator`、`denominator`、`grain`、`unit`、`data_source`(血缘文本)、`datasource_id`(FK→data_source，**RESTRICT**)、`owner`、`version`(default 'v1.0')、`revision_count`、`status`(`KpiStatus`: DRAFT/PUBLISHED/DEPRECATED)、`metric_id`(FK→ontology_metric，nullable)、`created_by`、`semantic_keywords`(`ARRAY(String(64))`)、`match_threshold`(`Numeric(3,2)` default 0.75)。
  通过 `metric_id` 弱关联 `ontology_metric`（KPI 可先于 metric 存在：业务定义先行、技术实现后置）。不进 Neo4j / Milvus（治理层不入向量检索）。
  ⚠️ dev 库现状：`semantic_keywords` **12/12 全空**、`datasource_id` **12/12 全 NULL**、挂靠 `metric_id` **0 条**。
- `ontology_metric_source`：**规划中**（未实施）。`ontology_metric` 的多源类 M:N 表 —— `metric_id`(FK→ontology_metric, CASCADE)、`class_id`(FK→ontology_class, RESTRICT)、`role`(NUMERATOR/DENOMINATOR/FILTER/BASE)、`UNIQUE(metric_id, class_id, role)`。
  必要性：9/12 KPI 的 `data_source` 含 `+`（多表；3 条单表：`KPI_PAYMENT_ON_TIME` / `KPI_SUPPLIER_DEFECT_RATE` / `KPI_SUPPLIER_FPY`）⇒ `target_class_id` 单值 FK 表达不了其中 9 条真实口径。
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

### id_mapping（统一 ID 映射中枢，A1 / M0-P0.1–P0.3）

**日期**：2026-10-31 · **变更**：[m0-id-mapping-a1](../changes/m0-id-mapping-a1/summary.md)
（前次：[id-mapping-dto-tighten](../changes/id-mapping-dto-tighten/summary.md)、
[id-mapping-admin-acl](../changes/id-mapping-admin-acl/summary.md)）

一行 = 一个业务对象在三个存储引擎中的 ID 映射。统一 ID 是三库对齐的唯一锚。

- 列：`unified_id`(PK, `String(128)`)、`business_object`(`String(32)`)、
  `external_id`(`String(128)`)、`pg_table`(`String(64)`)、`pg_id`(`String(128)`)、
  `neo4j_node_id`(`String(128)`)、`milvus_collection`(`String(64)`)、
  `milvus_id`(`String(128)`)、时间戳
- 唯一约束：`uq_id_mapping_bo_ext (business_object, external_id)`
- 格式：`unified_id = "obj:{business_object}:{external_id}"`，由
  `app/domain/id_mapping.py` 的 `buildUnifiedId` / `parseUnifiedId` 派生与反解。
  分段去空白 + NFC 归一；长度上限与列宽同源（32 / 128 / 128）；拒绝 `:` 与
  Unicode `Cc`/`Cf`/`Cs`/`Zl`/`Zp`（NUL、零宽、软连字符、双向覆盖、代理项、行分隔符）。

**谁写哪一列**：

| 写入方 | 可写的列 |
|---|---|
| `POST /id-mappings`（admin） | `business_object` / `external_id`（其余由服务端派生或留 NULL）；传指针 → 422 |
| `IdMappingService.register(..., pg_table=, pg_id=)` | 同上 + `pg_table` / `pg_id`（ontology 同步、回填脚本） |
| `PUT /id-mappings/{id}`（admin，运维改数） | 五个跨库指针列（partial） |
| A2 / A3 回填 | `neo4j_node_id` / `milvus_collection` / `milvus_id` |

**回填脚本**（`backend/scripts/backfill_id_mapping.py`）：

- 对每个 `ontology_class` 行写 `obj:CLASS:{class_id}`，`pg_table="ontology_class"`、
  `pg_id=str(class_id)`；`business_object` 用 `CLASS`（不按类名建命名空间）。
- conflict 路径 `ON CONFLICT (unified_id) DO UPDATE`，**只更新** `pg_table`/`pg_id`；
  重跑不抹 A2/A3 的 `neo4j_*`/`milvus_*`。
- **单事务**：全部批次一次提交，中途失败不留「前几批已写」的半状态。
- 动手前体检（任何写入之前）：`unified_id` 与自身两段不一致 ⇒ `CorruptMappingError`
  点名报错（否则会撞 `uq_id_mapping_bo_ext` 并在写入中途 IntegrityError）；
  `business_object` 只差大小写（`class` / `Class`）⇒ `CaseVariantMappingError`，
  指出 `reconcile_milvus_id_mapping.py` 的小写占位行与合并指引。
- 默认**预演**，`--apply` 才写；目标库必须命中 `scripts/kpi_metric_db_guard.py` 的
  deny-by-default 白名单（`qa_metadata` / `qa_metadata_test*`），且 `APP_ENV` 是生产
  或库名为 `qa_metadata` 时 `--apply` 还需 `--allow-prod`。
- 门禁 `backend/scripts/id_mapping_realdata.py`：只读模式断言**集合关系**——
  派生/反解一致、无大小写漂移、每个存活类都有 `CLASS` 映射（覆盖）、每个
  `CLASS` 映射都指向存在的类（无悬空）。**不是**「总行数 == 存活类数」：表里
  还有 `PROPERTY` / `METRIC` 行与软删墓碑，计数口径在真实库上恒假，且数相等
  也抓不到换集。大小写漂移必须在只读阶段挂掉：canonical 行满足覆盖、小写行
  自身自洽时，拖到 `--apply` 才拒等于白跑一轮上线窗口。只读模式可上生产。
- `--full` 追加「重跑存活 + 幂等」：哨兵 UPDATE、重跑回填与幂等回填都在
  **未提交事务**里，断言后 `rollback` ⇒ 全程无 COMMIT，哨兵从不落盘、并发
  读不到、进程被杀也不留假坐标。代价是所有被回填行持行锁到回滚，类数很大时
  选窗口执行。目标判定为生产时需 `--allow-prod`。

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

索引：`ix_agent_orchestration_step_run_step`（`run_id`,`step_no`）——**非唯一**索引。

约束：`uq_agent_orchestration_step_run_step`（`run_id` + `step_no` **唯一**——同一 run 内
step 序号不重复）、`ck_agent_orchestration_step_kind`（`kind IN ('PLAN','TOOL')`）、
`ck_agent_orchestration_step_status`（`status IN ('RUNNING','SUCCEEDED','FAILED','SKIPPED')`）、
`ck_agent_orchestration_step_action`（`action IN ('CALL_TOOL','FINISH','ASK_USER')`）、
`ck_agent_orchestration_step_counters`（`step_no >= 0` 且 `iteration_no >= 0` 且
`prompt_tokens >= 0` 且 `completion_tokens >= 0` 且 `cost >= 0`）。
外键只有一条：`run_id → agent_orchestration_run.id`（`ON DELETE CASCADE`，已在上方字段表列出）。
完整清单见 `backend/alembic/versions/0135_agent_orchestration.py:162-177`。

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

- `alembic/versions/0135_agent_orchestration.py` — 建 5 张表 + 5 个索引
  - **文件主干（stem）** = `0135_agent_orchestration`（**24 字符**）；文件名含 `.py` = **27 字符**
  - **revision id** = `0135`（**4 字符**），`down_revision = "0134"`
    （线性接在 `0134_data_layer_rule` 之后）
  - `alembic_version.version_num` 是 `varchar(32)` ⇒ **三个值（4 / 24 / 27 字符）都在约束内**
  - **downgrade 会 DROP 这 5 张表** ⇒ 回滚口径是「停 Worker + 关 flag」，**不做 downgrade**（见灰度手册 §5）

**已知缺口（不阻塞上线，但要记账）**：

- `(run_id, iteration_no, kind)` **没有** DB 级唯一索引（靠应用层保证幂等）；
- `agent_orchestration_usage` **没有** `run_id` 索引（用量台账按 run 聚合时会全表扫）。
