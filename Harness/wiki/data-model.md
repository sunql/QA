# 数据模型

## 元数据库（PostgreSQL）

### llm_config
模型配置：`model_name`(唯一)、`provider`、`api_endpoint`、`api_key_encrypted`(Fernet 加密)、`cost_per_1k_input/output`、`max_input_tokens`、`weight`(0-100)、`cost_threshold`、`is_active`、时间戳。

### session_token_usage
会话 Token 流水：`session_id`、`model_config_id`(FK)、`model_name`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`cost`、`request_time`、`purpose`、`cost_source`。索引 `(session_id, request_time)` 与 `(model_config_id, request_time)`。

**`cost_source`（P1-2，migration 0126，2026-10-18 收尾）**：成本口径，取值为白名单 `measured`（token 由 provider 上报、单价来自 `llm_config`）、`estimated`（只有部分用量，如异常里提取的 token）、`unknown`（无法定价，如研究链路按名称查不到配置 ⇒ 成本是占位 0）。常量 SSOT 在 `app/services/token_usage_service.py`（`COST_SOURCE_*` / `COST_SOURCES`）。写入方：chat 主链路（默认 `measured`，retry-gen 为 `estimated`）、doc-QA、报告总结、研究 `LlmUsageRecorder`（无配置时 `unknown`）。**NULL = 0126 之前的早期行**，与 `unknown` 语义不同（后者是「当时确实无法定价」）。读侧 `GET /api/v1/sessions/{id}/usage/list` 以 `costSource` 暴露；前端 `UsagePage` 按白名单映射文案，未知值一律显示「未知」，**绝不把原始枚举拼进 i18n key**（缺 key 会回显 key 本身）。

### 后续表（Phase 2-3）
- `ontology_class`：本体类，`source_table` 映射物理表，支持 `parent_class_id` 继承，`version` + `valid_from/to` 版本。
  **实际列清单（2026-11-19 生产实测核对，与 ORM `models.py:236` 一致）**：
  `id, class_name, class_alias, description, source_table, parent_class_id, created_by,
  created_time, updated_time, version, valid_from, valid_to, object_type, object_owner`。
  ⚠️ **该表没有 `is_active` / `is_stub` / `datasource_id` 列**——写 SQL 前别想当然（2026-11-19 一次猜列翻车实录）。
  - **停用/恢复只能走软删除墓碑**：置 `valid_to = now()` 即从召回消失
    （`ontology_service.listClasses` 只返回 `valid_to IS NULL` 的行）；复活必须**原地**把
    `valid_to` 置回 NULL（id 不变 ⇒ Milvus 向量仍指向同一个 id，换行=换 class_id=白干）。
  - 🔴 **本体层无「类→数据源」绑定 ⇒ 召回不按会话数据源过滤**：`chat_recall.py:224` /
    `chat_service.py:996` 的 `listClasses` 全局取类，SQL 却在会话选定数据源上执行、
    值域采样也打在当前源上 ⇒ **登记在 A 源的物理表、在 B 源会话里被召回**，planner 一选就
    ORA-00942（每问还附赠 6 次值域采样空往返）。事故实例：2026-11-19 生产
    「公司目前有多少供应商」——`DIM_SUPPLIER`（THBI 真表）因向量排序洗牌掉出 topK，
    planner 被校验器引导到跨源幽灵类 `main_data_supplier` ⇒ 全链失败。
    **根治 = 召回入口按 `datasourceId` 过滤候选类**（批次待立）；止血 = 对 THBI 查不到的
    跨源类置 `valid_to` 墓碑。V0.3 计划已立案的「本体无类→源绑定」真空洞在现有多源问答的实例。
  **采购域 5 个类的物理表绑定（2026-11-18 校准，迁移 `0153`）**：`Supplier`→`BPSUPPLIER`、`ItemMaster`→`ITMMASTER`、`PurchaseOrder`→`PORDER`、`Receipt`→`PRECEIPT`（**ERP 业务表词表**）、`IncomingInspection`→**NULL**（来料检验无物理表）。
  ⚠️ 迁移 `0039`/`0040` 当年写的是 **DWD_\* 词表**（`DWD_SUPPLIER` 等）—— 那张表在系统的查询目标里**不存在**，且与本体 SSOT（`backend/seed_ontology.py` 的 `CLASSES`）**分叉** ⇒ NL2SQL 指向不存在的表 + 干净库上 `seed_ontology.seed()` 撞 `uq_ontology_class_name_version` 唯一约束。`0153` 已校准（**纯数据迁移，无 DDL** ⇒ `alembic check` / drift 不受影响）。
  **绑定词表的 SSOT = `seed_ontology.CLASSES`**；`seed_ontology._seedClasses` 按 **`source_table`** 查重复用 ⇒ 两个种子的 `source_table` 必须同款，否则干净库上 seed 直接崩。门禁：`backend/scripts/procurement_class_source_table_realdata.py`（干净库真跑迁移链 + 断言无 `DWD_*` 残留 + 断言 seed 复用不崩）。
  ⚠️ **DWD 族存在「两套并行命名」，同名 ≠ 同表（2026-11-18 源侧实测）**：`DWD_*` 类分两族 ——
  **有效族**（`_DTL` 命名，如 `DWD_PURCHASE_ORDER_DTL` 1,023,442 行）与
  **墓碑族**（`_LINE` 命名，如 `DWD_PURCHASE_ORDER_LINE` 927,648 行，`valid_to=2026-09-19`）——
  二者是**不同的物理表**，且 `_DTL` 族**缺** `PROMISED_RECEIPT_DATE`/`ORDER_QTY`/`LINE_AMOUNT_EXCL_TAX`/`MATERIAL_CODE`/`RECEIPT_DATE`/`RECEIPT_NO`。
  12 条采购 KPI 的公式引用的是**墓碑族** ⇒ 源绑定清单 = 34 有效候选 **＋ 7 个复活墓碑类 = 41 类**（[变更 SSOT](../changes/2026-11-18-procurement-m0-questionnaire-intake/summary.md)）。
  墓碑族的物理列数 = 本体属性数（抽样 `missing=0`）⇒ **复活无列级缺口**。
  另：`DWD_ARRIVAL_ORDER_DTL`（**81,754 行**，`ARR_QTY`/`QUALIFIED_QTY`/`INSPECT_DATE` 100% 填充）**就是来料检验的落地表** —— 与上面 `IncomingInspection→NULL`（ERP 业务表词表口径）不冲突，是两套词表。
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

**Milvus 3 集合的 `external_id` 回填**（`backend/scripts/backfill_milvus_external_id.py`）：

- 目标：把 `ontology_class_embeddings` / `ontology_property_embeddings` /
  `ontology_metric_embeddings` 里 `external_id == ""` 的行补成 `obj:{type}:{ontology_id}`，
  同时写 PG `id_mapping` 占位行（`ON CONFLICT (business_object, external_id) DO NOTHING`）。
- **delete 可见性是硬约束**：Milvus 的 delete 走
  `Proxy → DML channel → DataNode → QueryNode` 三段异步管道，`flush()` 只持久化
  （Growing → Sealed → 对象存储），**不保证** QueryNode 已应用 delta binlog；
  collection 默认 Bounded（5s 容忍窗口）下 per-request
  `consistency_level="Strong"` 也**不能**立即看到 delete 后状态（2026-09-27 实测，
  见 [fix-milvus-delete-visibility](../changes/fix-milvus-delete-visibility/summary.md)）。
  ⇒ 脚本在 `insert + flush` 之后强制 `release + load`
  （`app/infrastructure/milvus_client.py::refreshCollection`），并**复扫校验**：
  旧行仍可见 ⇒ 抛错，**不返回可能撒谎的 `written`**。
- **定位必须按行自己的 auto-id**：同一 `ontology_id` 可能有多行（历史 delete 不可见
  留下的残留重复行），旧实现按 `ontology_id` 建索引来取「要重插的内容」⇒
  **取错内容**（delete 一直按行自身的 auto-id 打，**位置是对的**；错的是重插时
  取到了同 `ontology_id` 的另一行）⇒ 重插出内容相同的重复向量。
  ⇒ 现行实现按行自身 auto-id 索引，两处（delete 与取内容）用同一行。
- 代价：`release + load` 期间该集合处于**未加载**状态（约 2-3s）⇒ 该窗口内针对
  该集合的检索**拿不到正常结果**（可能直接报「集合未加载」）⇒ **必须在维护窗口执行**
  ⇒ 仅用于一次性维护脚本与集成测试的 delete-then-query 验证场景，**不用于在线业务路径**。

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

## WP-ONT-AI 增量：本体发现域（不可变快照 + 差异引擎）

**日期**：2026-11-16 · **变更**：[2026-11-16-ontology-discovery-ai](../changes/2026-11-16-ontology-discovery-ai/summary.md)
（批次 A1「发现底座」；**零 LLM、零外部授权**；迁移 `0146` / `0147`）

发现域**只写自己的两张表**，不写治理表、不写业务库（红线：AI 只产出候选，人工裁决，
机器发布）。与 `schema_cache` 的分工：`schema_cache` 是**当前态缓存**（内容变化即覆盖，
追求快），`metadata_snapshot` 是**证据**（只增不改、内容寻址、可被候选/剖析/提案引用）。

### discovery_source（授权目录，不新建连接）

| 列 | 类型 | 约束 / 默认 | 说明 |
|---|---|---|---|
| `id` | BIGINT PK | | |
| `name` | VARCHAR(100) | NOT NULL，**唯一** `uq_discovery_source_name` | 同一实例内唯一 |
| `datasource_id` | BIGINT | NOT NULL，FK → `data_source.id` **RESTRICT**（`fk_discovery_source_datasource`） | **只引用既有连接**（不出现第二套数据源真相源） |
| `owner` | VARCHAR(100) | NOT NULL | **业务责任人** |
| `access_policy` | JSONB | NOT NULL，默认 `'{}'::jsonb` | 访问策略，见下 |
| `status` | VARCHAR(20) | 默认 `'ACTIVE'`，CHECK `status IN ('ACTIVE','DISABLED')` | `DISABLED` = 撤销授权（采集被拒） |
| `note` | TEXT | 可空 | |
| `created_by` / `created_time` | | | |

索引：`ix_discovery_source_datasource(datasource_id)`。

**`access_policy` 的契约（刻意只承认已实现的键）**：

- `mode`：只允许 `READ_ONLY`（发现域只读）；
- `schema_owner`：**库模式属主**（Oracle schema / MySQL 库），内省时的目标命名空间；
  ⚠️ 与 `discovery_source.owner`（业务责任人）是两件事，刻意不同名；
- **未知键直接拒绝**（不静默忽略）：「我配了 schemas 白名单、以为生效了其实没生效」
  是最危险的失败模式；
- 策略 JSON ≤ 4096 字节（策略是配置不是数据载荷）。

### metadata_snapshot（不可变元数据快照，只增不改）

| 列 | 类型 | 约束 / 默认 | 说明 |
|---|---|---|---|
| `id` | BIGINT PK | | |
| `source_id` | BIGINT | NOT NULL，FK → `discovery_source.id` **RESTRICT**（`fk_metadata_snapshot_source`） | 快照证据不悬空 |
| `schema_hash` | VARCHAR(64) | NOT NULL，**源内唯一** `uq_metadata_snapshot_source_hash`，CHECK `schema_hash ~ '^[0-9a-f]{64}$'` | 内容寻址键 |
| `snapshot_json` | JSONB | NOT NULL，CHECK `jsonb_typeof(snapshot_json) = 'object'` | 规范化结构快照（只收结构，不收样本） |
| `table_count` / `column_count` | INTEGER | 默认 0，CHECK `>= 0` | 冗余计数 |
| `captured_at` | TIMESTAMPTZ | NOT NULL | 冻结时刻 |
| `created_by` / `created_time` | | | |

索引：`ix_metadata_snapshot_source(source_id)`。

- **刻意无 `updated_time`**：快照只增不改 ⇒ 没有「最后更新时间」的语义。
- **内容寻址**：`schema_hash` = 规范化 JSON 的 SHA-256，**与 Manifest 哈希同一把尺子**
  （复用 `app/domain/semantic_manifest_schema.manifestHash`）。同源同内容 ⇒ 复用既有行
  （幂等）；内容变化 ⇒ **新增**一行。
- **库级不可变**（迁移 `0147`）：`metadata_snapshot` 上挂 `BEFORE UPDATE OR DELETE` 行级
  触发器 `tr_metadata_snapshot_immutable` ⇒ 应用层纪律之外再加一道库层闸
  （误操作 / DBA 直连 / 将来新写的代码路径都绕不过）。**TRUNCATE 不受影响** ⇒ 测试夹具的
  「每测试清库」照常工作。

### 发现流水线六表（WP-ONT-AI A2 / G3，迁移 0166）

`discovery_run` → `discovery_profile` / `discovery_candidate` / `discovery_finding`
（run 的产物；FK `CASCADE`，run 删 ⇒ 证据一并消失）→ `discovery_proposal` →
`discovery_review`。要点：

- **`discovery_run` 带 LLM 计量三列**（`prompt_tokens` / `completion_tokens` / `cost`，
  CHECK 非负）—— 发现域一引入 LLM 就必须计量（守卫测试同时钉住）。
- **`discovery_candidate.evidence_refs` 不得为空**（`jsonb_array_length > 0`）：
  空证据的候选不可复核，而「证据完整率 100%」是硬红线。
- **`discovery_profile` 的 `(run_id, table_name, COALESCE(column_name,''))` 唯一**：
  列名为 NULL 表示表级指标，`COALESCE` 不可省（同 `semantic_release_check` 的坑）。
- **`discovery_proposal` 只持 `release_id` 指针**：`diff_json` 入库前必须过
  `compileManifest`；发布只调既有 `activate`（本域不写治理表）。
- **`discovery_review.approval_task_id` 可空 + `ON DELETE SET NULL`**：首期轻量审核，
  二期接 P5 审批时回填（非空时要求 `subject_type='ontology_proposal'`，服务层判）。
- **四眼不在库层**：`reviewer != proposer` 是跨表约束，CHECK 表达不了 ⇒ 服务层 409。
- **`discovery_evaluation`（0167）**：一次评测 = 数据集版本 + 可复算指标 + verdict
  （`PASS` / `FAIL` / **`INCONCLUSIVE`** 三态 —— 没有候选时是「没结论」，不是「质量差」）；
  `proposal_id` 可空（可只评一次运行），有引用时 RESTRICT。

### 迁移文件

- `alembic/versions/0146_discovery_snapshot.py` — 建 2 张表 + 2 个索引（纯加法）
  - **文件主干（stem）** = `0146_discovery_snapshot`（22 字符）；文件名含 `.py` = 26 字符
  - **revision id** = `0146`，**down_revision = "0145"**
- `alembic/versions/0147_snapshot_immutability.py` — 建不可变触发器 + 触发函数
- `alembic/versions/0167_discovery_evaluation.py` — 建评测表（纯加法）
- `alembic/versions/0166_discovery_pipeline.py` — 建流水线六表 + 12 个索引（纯加法；
  ORM `app/domain/discovery_pipeline_models.py`，双向钉住见
  `test_discovery_pipeline_models.py`）
  - **文件主干（stem）** = `0147_snapshot_immutability`（26 字符）；文件名含 `.py` = 29 字符
  - **revision id** = `0147`，**down_revision = "0146"**
  - `downgrade()` 用 `IF EXISTS` 幂等删触发器与函数
- **ORM ↔ 迁移逐字一致**：列注释与 CHECK 表达式由 `app/tests/unit/test_discovery_models.py`
  **双向字典比对**钉住（`alembic check` 只比约束名不比式，只补名 = 假绿）。

> 事实源：`backend/app/domain/discovery_models.py`、
> `backend/app/services/discovery_snapshot_service.py`。

## M1 增量：采购源绑定域（类 → 源表 / 属性 → 列）

**日期**：2026-11-18 · **变更**：[2026-11-18-m1-procurement-binding-tables](../changes/2026-11-18-m1-procurement-binding-tables/summary.md)（结构层）·
[2026-11-18-m1-procurement-binding-service](../changes/2026-11-18-m1-procurement-binding-service/summary.md)（行为层）· **迁移 `0154`**

设计来源 `docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md` §4；决策 **D2（两张表）** ·
**D-3（候选 ≠ 结论）**。绑定范围 **41 类**（G1 汇合实测：34 候选 + 7 复活墓碑类）。

本域**只写元数据库**，不写业务库；物理表的读取仍走既有 NL2SQL 通道。

### procurement_source_binding（一个本体类落到哪张物理表）

| 列 | 类型 | 约束 / 默认 | 说明 |
|---|---|---|---|
| `id` | BIGINT PK | | |
| `class_id` | BIGINT | NOT NULL，FK → `ontology_class.id` **RESTRICT** | **唯一键必须含 `class_id`** —— 实测 `DWD_SUPPLIER_PRICE_LIST` 与 `DWD_SUPPLIER_PRICE_LIST_DEL` 指向同一张表（同表双类） |
| `datasource_id` | BIGINT | NOT NULL，FK → `data_source.id` **RESTRICT** | |
| `schema_name` | VARCHAR(100) | NOT NULL | Oracle owner / PG schema / MySQL database |
| `source_table` | VARCHAR(100) | NOT NULL | 物理表名（**可 ≠ `class_name`**） |
| `binding_role` | VARCHAR(20) | NOT NULL，CHECK ∈ `BASE/NUMERATOR/DENOMINATOR/FILTER` | 复用 `MetricSourceRole` |
| `filter_expression` | TEXT | 可空 | 源表行级谓词（指标级 `filter_expression` 之外）。⚠️ 必须是**依赖行**的谓词：至少含一处「**裸列 ↔ 常量**」的直接比较，顶层只有 AND / NOT（`OR` / `CASE` 禁）⇒ 恒真谓词（`A = A` / `ABS(A) >= 0` / `CASE…ELSE 1 END = 1`）从语法上不可达 |
| `filter_allowlist` | VARCHAR(100)[] | 可空 | 过滤表达式允许引用的**列名白名单**（D2：独立列，**不进 JSON**）。⚠️ 只授权**列**；**函数**走系统内置的 `SAFE_SCALAR_FUNCTIONS`（**不可自报**），**结构词**（`SELECT`/`FROM`/`UNION`/**`TABLE`**/`LATERAL` …）一律禁。⚠️ **元素超长是静默截断**（PG `VARCHAR(100)[]`）⇒ 长度闸门必须住在校验层（共享常量 `MAX_ALLOWLIST_ITEM_LENGTH`），只卡 DTO 层不够 |
| `is_candidate` | BOOLEAN | NOT NULL，默认 `true` | TRUE = **未经确认**，不得进入任何读路径 / manifest |
| `confirmed_by` | VARCHAR(64) | 可空 | 确认人（**候选行为空**） |
| `confirmed_time` | TIMESTAMPTZ | 可空 | 确认时刻 |
| `valid_from` | TIMESTAMPTZ | NOT NULL，默认 `now()` | 生效起始 |
| `valid_to` | TIMESTAMPTZ | 可空，CHECK `valid_to IS NULL OR valid_to > valid_from` | **NULL = 当前生效**；失效是**软失效**（不删行 ⇒ 历史证据不丢） |
| `created_by` | VARCHAR(64) | 可空 | |

索引：`uq_procurement_source_binding_active(class_id, datasource_id, schema_name, source_table, binding_role)`
**部分唯一**（`WHERE valid_to IS NULL`）—— 同一键位只允许**一条生效**绑定，
失效后可再新建 ⇒ 软版本化。另有 `ix_procurement_source_binding_table(datasource_id, schema_name, source_table)`、
`ix_procurement_source_binding_class(class_id)`。

### procurement_property_binding（一个属性落到哪一列 + 复合键位）

| 列 | 类型 | 约束 / 默认 | 说明 |
|---|---|---|---|
| `id` | BIGINT PK | | |
| `source_binding_id` | BIGINT | NOT NULL，FK → `procurement_source_binding.id` **CASCADE** | 生命周期**从属**（源绑定删 ⇒ 子绑定删） |
| `property_id` | BIGINT | NOT NULL，FK → `ontology_property.id` **RESTRICT** | 证据不悬空 |
| `source_column` | VARCHAR(100) | NOT NULL | 物理列名（**允许 ≠ `ontology_property.source_column`**） |
| `key_ordinal` | SMALLINT | 可空，CHECK `>= 1` | 复合主键的**键位**（1..n）；**NULL = 非键列** |
| `transform_expression` | TEXT | 可空 | 列级变换（TRUNC / UPPER / 单位换算）；**为空 = 直读** |
| `is_candidate` / `confirmed_by` / `confirmed_time` / `valid_from` / `valid_to` | | 同上表 | 候选与有效期语义完全一致 |

索引：`uq_procurement_property_binding_active(source_binding_id, property_id)`（部分唯一）·
`uq_procurement_property_binding_key_ordinal(source_binding_id, key_ordinal)`（部分唯一，`key_ordinal IS NOT NULL`）。

### 状态机（行为层，M1-1.1）

```
        创建（写入口）              确认（人显式触发）        失效（软失效）
   ────────────────────▶  候选  ────────────────────▶  已确认  ────────────────────▶  已失效
   is_candidate = TRUE                                   valid_to = <now>
```

- **新建一律落候选**（D-3）；确认必须由人显式触发，**确认人取当前登录用户**（不接受客户端传入）。
- **候选 ⇔ 无确认人**由库层 CHECK 钉死（`ck_*_candidate` / `ck_*_confirmed`），不靠文档措辞。
- **确认前置**：源绑定至少有一条属性绑定（`FILTER` 角色例外 —— 过滤源不承载列映射）；
  且复合键序号必须 **1..n 连续**（库层只保证不重复，**连续性由服务层补**）。
- **软失效级联**：源绑定失效 ⇒ 其属性绑定**一并失效** ⇒ 不会出现「父已过期、子还生效」的孤儿映射。
  ⚠️ 失效时刻取 `max(now, valid_from + 1s)` —— `now()` 是事务开始时刻，与同事务新建行的
  `valid_from` **完全相同**，直接写会撞 `valid_to > valid_from` 的 CHECK。
- **过期后可同键位再建**（部分唯一索引只约束生效行）⇒ 这是「改绑定」而非「改行」的机制。

### 读视图（HTTP 层，两档鉴权）

| 视图 | 过滤条件 | 鉴权 |
|---|---|---|
| `GET /api/v1/procurement-bindings` | 全量（含候选、含已失效） | **admin** |
| `GET /api/v1/procurement-bindings/{id}` | 单条详情（含属性绑定） | **admin** |
| `GET /api/v1/procurement-bindings/effective` | `is_candidate = FALSE AND valid_to IS NULL` | **登录即可** |
| `GET /api/v1/procurement-bindings/effective/properties` | 自身已确认且生效 **且父已确认且生效** | **登录即可** |

⇒ **D-3 在 HTTP 面的落法**：候选是未经确认的治理草案，普通用户看到会当成结论 ⇒ 全量视图限 admin；
只有「已确认且当前生效」的行对所有登录用户可见。

**状态机（确认 = 版本冻结点）**：`候选态（父未确认）──▶ 确认态（父已确认，子集合冻结）──▶ 失效态（只读）`。
⇒ 父已确认时追加 / 确认 / 失效子绑定一律 **409**；父已失效时改子绑定 **422**。
要改绑必须走「失效现役行 → 建新候选 → 重新确认」。
**锁序固定为 父 → 子**（`SELECT ... FOR UPDATE` + `populate_existing`）⇒ 不死锁、不 TOCTOU。

**生效属性映射的两条父状态谓词可观测性不同**（变异反证实测）：
「父必须已确认」✅ 可观测（子可先于父被确认）；「父必须未失效」⚪ **冗余防御**
（父失效时子由级联失效一并作废 ⇒ 删掉该谓词行为不变）。

> 事实源：`backend/app/domain/procurement_binding_models.py`（ORM）、
> `backend/app/services/procurement_binding_validation.py`（纯校验）、
> `backend/app/services/procurement_binding_service.py`（编排）、
> `backend/app/api/v1/procurement_bindings.py`（10 条路由）。

## M1 增量：发布校验结论表（`semantic_release_check`）

**日期**：2026-11-18 · **变更**：[2026-11-18-m1-semantic-release-check](../changes/2026-11-18-m1-semantic-release-check/summary.md)· **迁移 `0155`**

回答一个问题：**「这次发布到底查了什么、每项结论如何」**。
闸门结论原先只以 `blockers` / `warnings` 两个字符串数组出现在写接口的响应体里 ——
**响应一关就没了** ⇒ 无法回答「上周那次被拦下的发布，拦在哪一条、后来怎么处理的」。

### semantic_release_check（一个发布版本的逐项校验结论）

| 列 | 类型 | 约束 / 默认 | 说明 |
|---|---|---|---|
| `id` | BIGINT PK | | |
| `release_id` | BIGINT | NOT NULL，FK → `semantic_release.id` **CASCADE** | 发布被删 ⇒ 校验证据一并消失（不留孤儿证据） |
| `check_code` | VARCHAR(64) | NOT NULL，CHECK 非空 | 校验项编码（机器可读，如 `METRIC_SOURCE_PRESENT`） |
| `check_item` | VARCHAR(200) | NOT NULL，CHECK 非空 | 校验项名称（中文，展示用） |
| `severity` | VARCHAR(10) | NOT NULL，CHECK ∈ `ERROR/WARNING` | **该校验项失败时的**严重级别：`ERROR` = 阻断发布；`WARNING` = 治理债 |
| `result` | VARCHAR(10) | NOT NULL，CHECK ∈ `PASS/FAIL` | **本次**的结论 |
| `target_type` | VARCHAR(32) | 可空 | 被校验对象类型（`RELEASE` / `METRIC` / …） |
| `target_key` | VARCHAR(200) | 可空 | 被校验对象标识（指标名等；**NULL = 发布级校验项**） |
| `detail` | TEXT | NOT NULL | 结论详情（人类可读） |
| `checked_time` | TIMESTAMPTZ | NOT NULL，默认 `now()` | 校验发生时刻（**覆盖写 ⇒ 每次评估刷新**） |
| `checked_by` | VARCHAR(64) | 可空 | 触发本次校验的操作人 |
| `created_time` / `updated_time` | TIMESTAMPTZ | NOT NULL，默认 `now()` | `TimestampMixin` |

索引：`ix_semantic_release_check_release(release_id)` ·
`uq_semantic_release_check_unique(release_id, check_code, COALESCE(target_key, ''))` **唯一**。
⚠️ **`COALESCE` 不可省**：PostgreSQL 的 NULL 互不相等，直接建唯一索引会让
`target_key IS NULL` 的**发布级**校验项（快照完整性 / Manifest 契约）无限重复落库。

### 两个字段为什么正交（而不是合成一列枚举）

- `severity` 回答「**若这条失败会有多严重**」；
- `result` 回答「**这次结论如何**」。

合成一列（`PASS/WARN/ERROR/FAIL`）后就无法表达「本次通过、但若失败就是 ERROR」的风险面；
分两列则「全 PASS 的报告」也能直接告诉读者每项的风险面。

### 落表口径（行为层，M1-1.2）

- **逐项落表，`PASS` 也落**：只落失败项会让「通过」与「没跑」不可区分 ⇒ 评审看到空表。
- **覆盖写（replace-all）**：一个 release 的「当前校验结论」只有一份；历史由 `audit_log` 承担。
- **失败也要落表**：校验失败 ⇒ **先提交 check 行再抛原异常**（`getDb` 在异常时 rollback，
  否则证据随事务消失）⇒ 「不得激活」才可证（M1-a DoD 第 3 条）。
- **闸门口径零漂移**：`checksToGateResult(evaluatePublishChecks(m)) == evaluatePublishGate(m)`。
- **闸门与落表同源**：校验项集合**只算一次**，闸门结论与落表行都从它派生
  （否则「闸门算一遍、落表再算一遍」会静默错位）。
- **失败尝试也留痕（`audit_log`）**：check 表是覆盖写的 ⇒ 上一轮的 FAIL 行会被下一轮删掉。
  唯一写 `audit_log` 的地方是**成功**的状态迁移 ⇒ 被闸门拦下的那次尝试在两边都没痕迹，
  「那次被哪一条拦下的」就再也查不到 —— **而这正是这张表要回答的问题**。
  ⇒ 失败路径补一条审计：`action="UPDATE"`（审计动作是**封闭词表**，扩词要改 ORM CHECK + 占迁移号），
  `after` 里带 `blocked=true` / `phase` / `blockerCount` / `blockers` / `errorCount` / `checkCount`。
- **上限从契约推导，截断必须显式标记**：上限 = `2 + 3 × MAX_MANIFEST_METRICS`
  （`MAX_MANIFEST_METRICS` = Manifest 契约 `metrics` 数组的 `maxItems`，从
  `semantic_manifest_schema` 导出）。⚠️ 写死一个「看起来更宽」的数会出事：
  契约允许的**合法** manifest 也会被截断，而截断丢的是**尾部**校验项 ——
  尾部往往正是最后一条指标的 ERROR 结论行。
  ⇒ 超限时保留上限-1 条 + 追加一条 `CHECKS_TRUNCATED`（`severity=ERROR` / `result=FAIL`）合成行：
  表里查得到「报告被截断」，闸门把它当**阻断项**（fail-closed），且闸门与落表用**同一个集合**。
  ✅ **M1-1.3 批次 B 已重推**为
  `6 + 3 × MAX_MANIFEST_METRICS + 2 × MAX_MANIFEST_SOURCE_BINDINGS`（= **10006**）——
  契约上限变了而公式没变 ⇒ 合法 manifest 被静默截断（同一类缺陷，第三犯的机会）。
  ⇒ 上限**从契约常量推导**（代码里不再写死数字），单测钉死公式本身。

### 读接口分页口径

`GET /api/v1/semantic/releases/{releaseId}/checks` 支持 `limit`（1~200，默认 50）/ `offset`（≥0）。
⚠️ `rows` 是**当前页**，而 `total` 与 `summary` 覆盖**全部行** ——
否则读者翻到第二页、看到「这一页没有 ERROR」就会误判「整体通过」。

校验项目录（新增校验项只加常量，不改表结构）：

| code | 目标类型 | severity | 判据 |
|---|---|---|---|
| `SNAPSHOT_INTEGRITY` | RELEASE | ERROR | 快照规范化 / 体量 / 哈希一致 |
| `MANIFEST_SCHEMA` | RELEASE | ERROR | Manifest 满足其 `schemaVersion` 对应的契约（v1 / v2） |
| `METRIC_SOURCE_PRESENT` | METRIC | WARNING | 指标有源类引用 |
| `METRIC_NUMERATOR_LANDING` | METRIC | WARNING/ERROR | 指标分子度量有落点类 |
| `METRIC_DENOMINATOR_LANDING` | METRIC | WARNING/ERROR | 指标分母度量有落点类（仅 RATIO/DIFFERENCE） |
| `CHECKS_TRUNCATED` | RELEASE | ERROR | 结论条数超上限（报告不完整 ⇒ 阻断） |
| `DOMAIN_SLICE_EMPTY` | RELEASE | ERROR | 域切片为空（不能发布空采购快照） |
| `DOMAIN_SLICE_CROSS_DOMAIN` | RELEASE | WARNING | 为满足引用契约补齐了跨域依赖类 |
| `SOURCE_BINDING_CANDIDATE` | RELEASE | WARNING | 仍有未确认（候选）绑定（D-3 治理债） |
| `SOURCE_BINDING_JOIN_MISSING` | BINDING | **ERROR** | 绑定所属类在**整个本体**里零 JOIN 边（孤岛） |
| `SOURCE_BINDING_PROPERTY_UNBOUND` | BINDING | WARNING | 类已绑源表，但有属性没有 `sourceColumn` 绑定 |
| `CLS-001` | CLASS | ERROR | 类名唯一 / objectType 合法 / **父子继承无环** |
| `CLS-002` | BUSINESS_OBJECT | ERROR | 头类存在 / 主域存在且**启用** |
| `REL-001` | RELEASE | ERROR | JOIN 两端类存在 + 活库语义关系类型白名单 / 端点墓碑 |
| `AGT-001` | RELEASE | WARNING | Agent 域/层在启用词表 + 绑定工具已注册且启用（活库） |
| `GRF-001` | RELEASE | WARNING | 图 label 在 Neo4j 业务实体白名单 + 入图对象头类有主键 |
| `MET-002` | KPI | WARNING | 已发布 KPI 绑定启用中数据源 + 关联指标已发布 |

（M1-1.3 批次 B 已把「采购源绑定缺 JOIN」等采购域校验项接进**同一张表**；
G2 把设计稿 §5.5 的 6 个校验码接进同一张表 —— 判据在
`app/services/semantic_release_design_checks.py`，活库事实在
`app/services/semantic_release_context_facts.py`。`CLS/REL/GRF/AGT` 每码**一条汇总行**，
`MET-002` **逐 KPI**；`None` = 没查过 ⇒ 该行不出现。）

> 事实源：`backend/app/domain/semantic_release_check_models.py`（ORM）、
> `backend/app/services/semantic_release_check_types.py`（类型与常量 · **叶子模块**）、
> `backend/app/services/semantic_release_checks.py`（发布级 + 指标级纯评估）、
> `backend/app/services/semantic_release_domain_checks.py`（域级 + 绑定级纯评估）、
> `backend/app/services/semantic_release_check_service.py`（落表 / 回读）。
> 真机验证：`backend/scripts/semantic_release_check_realdata.py`。
>
> ⚠️ **为什么拆三个模块**：域级模块要 import `PublishCheck`，而 checks 模块又要 import
> 域级模块 ⇒ 类型必须住**零业务依赖的叶子模块**，否则形成结构上的 import 环
> （与 `semantic_manifest_version` 同一类问题，见下文）。

## M1 增量：Manifest 契约 v2（采购源绑定入档）

**日期**：2026-11-19 · **变更**：[2026-11-19-m1-procurement-domain-release](../changes/2026-11-19-m1-procurement-domain-release/summary.md)（批次 A）· **迁移 `0156`**

回答一个问题：**「发布的快照里，采购源绑定到底是怎么落的」**。
M1-1.1 把绑定做成了两张治理表（类 → 物理表 / 属性 → 列），
但发布快照是**运行期唯一真相** —— 绑定不入档 ⇒ 快照不自包含、
「已发布的采购语义」无法离线复算。

### v2 契约：新增 `sourceBindings` 分区

```jsonc
"sourceBindings": [{
  "className": "DWD_PURCHASE_ORDER_DTL",   // 必填（按**名字**，不按 id）
  "datasourceName": "THBI",                  // 可空
  "schemaName": "ERP",                       // 可空
  "sourceTable": "PORDER",                   // 必填
  "bindingRole": "BASE",                     // BASE/NUMERATOR/DENOMINATOR/FILTER
  "filterExpression": null,
  "filterAllowlist": [],
  "properties": [{
    "propertyName": "PO_NO",
    "sourceColumn": "PO_NO",
    "keyOrdinal": 1,                          // 可空（复合键序号）
    "transformExpression": null
  }]
}]
```

**为什么单独分区而不是折进 `classes[].sourceTable`**：绑定是「类 →（数据源, schema, 表）」的
**一对多**映射，还带过滤/变换表达式、键序号与逐属性映射 ⇒ 折进 v1 字段会**丢掉真信息**，
而失真的快照比没有快照更危险。

### 版本分发（升版的连锁面）

`MANIFEST_SCHEMAS = {v1, v2}`；`MANIFEST_SCHEMA_V2` 由 `_deriveV2(v1)` **派生**
（复制粘贴 ⇒ 两版契约悄悄漂移）。`validateManifest` **按 `manifest.schemaVersion` 取 Schema**：

- **未知版本直接拒绝**（不放行、不猜）；
- **v1 快照继续按 v1 校验**（历史快照不许被新契约判死）；
- v2 追加**引用闸**：绑定 `className` 必须存在；绑定 `propertyName` 必须属于**该类**
  （空属性类也必须校验 ⇒ 用 `allowed is not None` 判定，不能用 `and allowed` ——
  后者让「空属性集合」被当成「不检查」）；同一绑定 `keyOrdinal` 不重复；
  同一 `(className, datasourceName, schemaName, sourceTable, bindingRole)` 不重复；
  同一绑定内 `propertyName` 不重复（库里属性名无唯一约束 ⇒ 自己挡）。

⚠️ **版本字面量必须冻结**（`app/domain/semantic_manifest_version.py`，零 import 的叶子模块）：

- 注册表的**键**与每版 Schema 的 `schemaVersion` const 都用**版本字面量**
  （`SCHEMA_VERSION_V1` / `SCHEMA_VERSION_V2`），**不得**用「当前版本」——
  否则升到 v3 时 v2 会从注册表里消失、v2 契约被改写成 v3 ⇒ 已发布的 v2 快照
  当场变成「未知版本」⇒ 无法校验 / 激活 / 回放；
- `_assertCurrentVersionRegistered()` 在 **import 期**就炸（升版漏登记不必等到发版才发现）；
- 单测钉死「注册表里每一版都自述自己的版本」；
- 该叶子模块同时**断开 ORM 的 import 环**：`app/domain/models.py` → 子模型 →
  `semantic_manifest_schema` → `app.domain.schemas` → services → `app.domain.models`
  （今天只靠导入顺序侥幸成立）。实测 `import app.domain.models` 不再连带加载
  `app.domain.schemas` / `jsonschema` / 三个 service registry。

⚠️ **「哪一版含绑定分区」是版本配置**（`MANIFEST_SCHEMA_SOURCE_BINDING_VERSIONS`），
不是版本字符串比较：用「版本 ≠ v1」判断 ⇒ 未来任何不带该分区的版本会走进绑定引用闸 ⇒
读一个不存在的键 ⇒ KeyError ⇒ 500（而不是一条可读的契约错误）。

⚠️ **升版是连锁面，不是改一行常量**，五处必须一起处置：

| 面 | 处置 |
|---|---|
| `semantic_release.schema_version` 的 `server_default` + DB 默认值 | 迁移 **0156** 改 `'v2'`；ORM 改用 `server_default=SCHEMA_VERSION`（drift 守卫靠单测钉住，`alembic check` 只比名不比式） |
| 库里既有 v1 ACTIVE 快照 | `semantic_schema_adapter.schemaVersionMismatch` 只认当前版本 ⇒ **降级直读治理表**（既有设计：宁可慢一点也不能少给事实）⇒ **发版顺序见 SSOT §9.1**：先切代码 → 建一份 v2 发布并 activate；中间窗口期是**降级直读**（功能正确、性能回退） |
| 发布校验项文案 | PASS detail 写**实际版本**（`Manifest 满足 {version} 契约`） |
| `semantic_diff_service.MANIFEST_SECTIONS` | 增 `sourceBindings` ⇒ 自动落进 `UNCOVERED_SECTIONS`（API 契约显式披露「未覆盖」） |
| 夹具 | 版本取 `SCHEMA_VERSION` 动态值；夹具的版本必须与行 `schema_version` 一致（不一致 ⇒ 被判漂移） |

### 什么进快照、什么不进（字节确定性铁律）

| 进 | 不进 | 理由 |
|---|---|---|
| `className` / `datasourceName` / `schemaName` / `sourceTable` / `bindingRole` | 自增 `id` | 同一治理内容必须编译出同样字节（`measure.class_id` / `kpis.datasourceId` 已各犯过一次） |
| `filterExpression` / `filterAllowlist` | `validFrom` / `validTo` | 时间戳进快照 ⇒ 两次编译字节不同 ⇒ M1-a DoD 第 2 条被破坏 |
| 每个属性绑定的 `propertyName` / `sourceColumn` / `keyOrdinal` / `transformExpression` | `is_candidate = TRUE` 的**候选**绑定 | **D-3**：候选不是结论，不得被读路径消费；manifest 就是读路径的数据源 |
| — | `createdBy` / `confirmedBy` / `confirmedTime` | 实例相关残留列，与「换环境必须得到同样字节」冲突 |

⇒ 编译口径：**只收 `is_candidate = FALSE` 且 `valid_to IS NULL` 的绑定**；
墓碑类（`valid_to` 非空）及其属性整体**丢弃**（丢弃不是报错 —— 墓碑类上的绑定是「历史」，
报错会把一次正常发布挡死）。

### 编译期发布不变量（不只是「写入时校验过」）

| 不变量 | 判据 | 失败形态 |
|---|---|---|
| **候选 fail-closed** | `is_candidate` 必须**显式为假**（`is False`，不是 `not row.is_candidate` —— 后者在 `None` 时 fail-open） | 未知候选态 ⇒ 不进快照 |
| **生效时刻** | `valid_from` 不得晚于此刻（未来才生效的绑定**还没生效**） | 不进快照 |
| **表达式安全** | 过滤表达式复用 `validateFilterExpression(expr, allowlist)`、列级变换复用 `validateTransformExpression(expr, sourceColumn)`（都是纯函数） | 抛 `SourceBindingCompileError` ⇒ **422**（不是 500） |
| **属性归属** | 属性必须属于**绑定所属的类**（库侧无此约束，只有写路径在挡） | 抛错并**定位到绑定 id** |

⇒ 表达式安全从「入口校验」升级为**发布不变量**：绕过写路径入库的行
（运维脚本 / 数据修复 / 未来的批量导入）不会把越权表达式带进快照。

⚠️ **抛错而不是悄悄丢弃**：这类行一旦进快照，读路径就会拿着越权表达式去查库；
而「悄悄跳过」会让问题不可见（没有校验行可指认是哪一条绑定）。
批次 B 补绑定级校验项后，失败会**落表成 check 行**并定位到具体绑定。

⚠️ **同一类可有多条 BASE 绑定**（多落点是合法设计）⇒ 契约不收紧，
但**读路径接线时必须显式消歧**（否则「这条类指哪张表」是未定义行为）。

### 契约上限（从契约导出，不写死）

`MAX_MANIFEST_SOURCE_BINDINGS = 2000`、`MAX_MANIFEST_BINDING_PROPERTIES = 500`
（v2 Schema 的 `maxItems`）⇒ 发布校验项条数上限要**从它们推导**（批次 B 重推为 10006）。

> 事实源：`backend/app/domain/semantic_manifest_schema.py`（契约 + 版本分发）、
> `backend/app/domain/semantic_manifest_version.py`（版本字面量 · 叶子模块）、
> `backend/app/services/semantic_compiler_bindings.py`（绑定分区编译 + 发布不变量）、
> `backend/app/services/semantic_compiler.py`（接线）、
> `backend/alembic/versions/0156_semantic_manifest_v2.py`（迁移）。

## M1 增量：采购域发布编排（域切片 + 回滚）

**日期**：2026-11-19 · **变更**：[2026-11-19-m1-procurement-domain-release](../changes/2026-11-19-m1-procurement-domain-release/summary.md)（批次 B）· **无新表**（复用 `semantic_release` / `semantic_release_check`）

回答两个问题：**「采购域发布出去的那份快照里到底有什么」** 与
**「发错了怎么退回去」**。

### 域发布 = 存**切片**，不是全量

`domain_code` 非空时，`POST /api/v1/semantic/releases` 编译全量后**按域切片**再落库：

```
业务对象挂域 → 类（+父类闭包）→ JOIN → 指标 → KPI
      ↓ 引用闭合补齐（四条来源取**联合不动点**）
本域指标引用到的类
  + 选中类的 properties[].refClassName（**属性引用**）
  + 有绑定类的一跳 JOIN 邻居
  + 以上补进来的类的父类闭包
```

⚠️ **补齐是幂等的**：对切片再切一次得到**同一份** ⇒ 发布校验项可以只依赖
**已存快照**复算（否则「跨域补齐了什么」在 validate 阶段就复算不出来 ⇒ 证据不可复算）。

⚠️ **四条来源必须取一个「联合不动点」**（代码评审 HIGH）：它们互相喂新类 ——
新类又有自己的属性引用、自己的绑定、自己的父类。**只做 JOIN 邻居闭包 + 父类闭包
两个独立闭包是不够的**：带绑定的**父类**会被判成孤岛 ⇒ 合法发布被 ERROR 阻断。
⇒ 实现成一个迭代到稳定的 `_closure`（度量引用 / 属性引用 / JOIN 邻居 / 父类闭包
交替迭代）。

⚠️ **属性引用（`properties[].refClassName`）也必须闭**：v2 引用契约要求属性的
`refClassName` 必须存在于切片里 ⇒ 不闭 ⇒ **该域永远发不出去**（表引用型字典是
常见建模，不是假想场景）。

⚠️ **孤岛判据取全本体口径**：切片阶段把有绑定类的一跳 JOIN 邻居拉进来 ⇒ 两个口径等价 ⇒
「只连跨域边」的类不会被误判成孤岛。

### 回滚 = **新建副本式**（D-11）

`POST /api/v1/semantic/releases/{releaseId}/rollback`，body `{version, note?}`。

| 形态 | 结论 |
|---|---|
| **A. 新建副本式**（采纳） | 以历史版本为源新建 DRAFT → VALIDATED → ACTIVE；同作用域旧 ACTIVE 在**同一事务**里归档；副本 `manifest_hash` 与源**逐字相同** |
| B. 状态回退式（ARCHIVED → ACTIVE） | 历史行被「复活」；与 `ck_semantic_release_status` / 状态机冲突 |

⇒ 副本直接**复制源行的正文与哈希** ⇒ 回滚的语义是「回到那个内容」，
不是「重新编译一次」（重编译会带上治理表的最新变化）。

⚠️ **回滚源不得为 DRAFT**（草稿从未发布过 ⇒ 没有「回到」的对象）⇒ 409。

### 候选绑定：只出**一条**发布级汇总行

候选绑定**不进快照**（D-3）⇒ 数量**没有契约上限** ⇒ 若按候选逐条出校验项，
条数上界就无从推导 ⇒ 候选只出**一条发布级汇总行**（WARNING，detail 带总数 + 前 5 个类名）。

⚠️ **没查过候选数就不出这一行**（`candidateBindingCount: int | None`）：
写一句「没有候选绑定」是**没依据的结论**（比缺一行更坏）。

### 绑定级校验项的定位键

`target_key` = `类名@数据源.schema.表#角色`（**五元组**）。

⚠️ **必须带角色与数据源**：一个类可以绑到同一张表的多个角色（BASE / NUMERATOR …），
也可以在**两个数据源**里各绑一次；少一个维度 ⇒ 两行撞
`uq_semantic_release_check_unique` ⇒ **整次发布 500**。
⚠️ 列宽 `String(200)`：超宽时改用「类名摘要(64) + 完整身份 sha256[:16]」，**不截断**
（截断会让两个不同的绑定撞成同一个键）。

### diff 视图纳入绑定分区（批次 B）

`sourceBindings` 从 `UNCOVERED_SECTIONS` 移到 `COVERED_SECTIONS`，字段级覆盖：
`filterExpression`（BREAKING）/ `filterAllowlist`（BREAKING）/
`properties[].sourceColumn`（BREAKING）/ `properties[].transformExpression`（BREAKING）/
`properties[].keyOrdinal`（COSMETIC）。

⚠️ diff 侧的绑定身份同样必须带 `datasourceName`（少一个维度 ⇒ 两行被挤成一条 ⇒ **变更被吞**）。

⚠️ **披露面随之扩大**：`GET /api/v1/ontology-discovery/diff` 的 `coveredFields`
多出 `sourceBindings` 分区（字段级：绑定自身字段 + `properties[].*`）。
这是**对外契约的扩大**（多披露比少披露安全）⇒ 断言「披露分区集合相等」的用例必须同步。

⚠️ **diff 两侧必须同口径**（代码评审 HIGH）：基线 = 该域的 ACTIVE 行（**切片**），
候选 = 全域编译结果 ⇒ **两侧不可比**（差异报告在说谎）。⇒ 候选侧按**同一个域**
切一次；`scope` 如实披露 `DOMAIN`（域作用域）/ `GLOBAL`（全局作用域）。

⚠️ **绑定键的渲染器只有一份**：校验侧与差异侧共用
`semantic_release_check_types.bindingTargetKey()` ⇒ 差异报告里的一个绑定名，
能在 check 表里查到**同一行**（各写一份 ⇒ 迟早漂移 ⇒ 查不到）。

### 版本号在**作用域内**唯一（迁移 0157）

| 作用域 | 唯一索引 | 键 |
|---|---|---|
| 全局（`domain_code IS NULL`） | `uq_semantic_release_global_version` | `(version)` |
| 域（`domain_code IS NOT NULL`） | `uq_semantic_release_domain_version`（**迁移 0157**） | `(version, domain_code)` |

⇒ **不同域可以复用同一个版本号**（版本号标识「哪一次发布」，不是「哪一套治理内容」）；
**同一域内不行**。

⚠️ **服务层查重不够**：查重是「先查后写」⇒ 两个并发请求可以同时查到「没有重复」
然后同时写入 ⇒ **库侧唯一索引**才是兜底（全局作用域早就是「查重 + 索引」双保险，
域作用域本批补齐成同款）。

⚠️ **建索引前先探测存量重复**（迁移 0157 里的 `_assertNoDuplicateDomainVersions`）：
同域重复版本号意味着「哪一次发布」无法标识 ⇒ **报错、不自动修**（不删行、不改版本号）
⇒ 必须人工裁决。静默挑一条留下 = 伪造发布历史。

> 迁移号：**0156**（manifest v2 默认值）→ **0157**（域作用域版本号唯一索引）
> → **0158**（M1-1.4 采购本体对象核对与补齐，见下节）。
> A4-0（审批面放开 `instance_id`）按「串行占号、不许插队」规则取下一个空号 **0159**。

### 采购本体对象：墓碑类必须**原地复活**（迁移 0158，M1-1.4）

V0.3 §3.2 的 10 个采购本体对象逐条核对后，补齐动作落 **0158**（数据迁移 + 一张**迁移专用日志表**）：

| 动作 | 对象 | 内容 |
|---|---|---|
| 复活 7 个墓碑类（清 `valid_to`） | PR / PO / GR / INVOICE / PAY | `DWD_PURCHASE_REQUISITION_LINE`、`DWD_REQUISITION_ORDER_LINK`、`DWD_PURCHASE_ORDER_LINE`、`DWD_GOODS_RECEIPT_LINE`、`DWD_PURCHASE_INVOICE`、`DWD_PURCHASE_INVOICE_LINE`、`DWD_SUPPLIER_PAYMENT` |
| 新增 4 个 BO | PR / QUOT / POL / PAY | `business_object` 4 行，`primary_domain_id='PROCUREMENT'` |
| 改指 1 个 BO 的头类 | INVOICE | `DWD_AP_INVOICE_DTL`（46 行文件抽取旁表）→ `DWD_PURCHASE_INVOICE`（38,323 行） |
| 明确不建 | CONTRACT / SUPPLIER_PERFORMANCE / RFQ | 见核对表 §4（CONTRACT 承载面 = 文档目录，**实测 0 份**） |

⚠️ **为什么必须复活而不是新建 v2 行**：`ontology_metric_source` 用 **`class_id`** 引用类；
编译器遇到**墓碑类**的源引用会**整条丢弃**（`semantic_compiler_sections.groupMetricSources`）
⇒ 指标只剩一半口径 ⇒ **静默口径漂移**（OTD 只剩分母、三单匹配只剩 FILTER），
且指标归不了属 ⇒ 被域切片 **fail-closed 排除**。
新建 v2 行会**换 `class_id`** ⇒ 指标源仍指向旧 id ⇒ 换汤不换药。**原地复活保持 `class_id`**。

**实测（开发库 5433）**：

| 项 | 补齐前 | 补齐后 |
|---|---|---|
| 编译后保留的指标源 | **3 / 18**（丢 15） | **18 / 18**（丢 0） |
| 采购域切片 类 / 指标 / KPI | **6 / 1 / 1** | **10 / 9 / 9** |

⚠️ **迁移是自包含的**（不 import `app.*`）⇒ 与决策目录
`app/domain/procurement_object_catalog.py` 的一致性由**单测里的漂移守卫**钉住
（`app/tests/unit/test_procurement_object_catalog.py`，含变异反证）。
⚠️ **在 Lineage A 环境（测试库 5434）整批 no-op**：所有锚点是 Lineage B 类名（`DWD_*` / `DIM_*`）⇒
找不到类 ⇒ 什么都不改（不报错、不产生半截状态）。
⚠️ **变更日志表 `procurement_object_reconciliation_journal`**：升级时逐条记录
「实际复活了哪个 class id / 原墓碑时间戳」「插了哪几个 BO code」「改指前的头类 id 与 graph_label」。
没有它，降级只能按「类名 + 当前状态」反推 ⇒ 会把**升级前就活跃**的同名类一并墓碑化
（误改不属于本迁移的数据），且原 `graph_label` / 时间戳只能硬编码猜回去。
降级末尾删表 ⇒ 降级后库形状与本迁移无关（实测 `downgrade 0157` 后原时间戳 / 原 graph_label 全部精确还原）。
⚠️ **降级删 BO 用 `pg_constraint` 动态枚举**所有引用 `business_object` 的外键
（不写死表名；当前 4 张：`business_object_domain` / `document_entity_relation` /
`entity_mapping` / `feature_definition`）⇒ 任一引用存在就保留该行，不让降级撞 RESTRICT。
⚠️ **`business_object` 的主键是 `code`，没有 `id` 列** —— 迁移里写 `RETURNING id` 会直接报
`UndefinedColumnError`（评审后改写时踩到）。
⚠️ **复活不按 `version` 过滤**：墓碑行可能不是 v1；按版本锚定会静默 0 行 = 类仍是墓碑。

> 事实源：`backend/app/domain/procurement_object_catalog.py`、
> `backend/alembic/versions/0158_procurement_object_reconciliation.py`、
> 核对表 `docs/update/1118/M1/M1-1.4-采购本体对象核对表.md`、
> 证据 `docs/update/1118/M1/evidence/procurement-domain-slice-{before,after}.txt`。

### 编排层 / 支撑层的文件切分

`semantic_release_service.py`（编排层）与 `semantic_release_support.py`（支撑层）分开：

| 层 | 职责 | 住哪 |
|---|---|---|
| 编排层 | 编译 → 切片 → 落库 → 状态迁移 → 审计 | `semantic_release_service.py` |
| 支撑层 | 输入校验 / 校验项评估 / 审计留痕 / 撞库翻译 | `semantic_release_support.py` |

⚠️ **分层方向决定谁住哪**：编排层 import 支撑层 ⇒ 支撑层**不能**反向 import 编排层 ⇒
`ReleaseEvaluation` 必须住支撑层；而状态机判定 `isTransitionAllowed` **留在编排层**
（支撑层不需要它）⇒ 依赖单向成立。

> 事实源：`backend/app/services/semantic_domain_slice.py`（发布切片）、
> `backend/app/services/knowledge/domain_slice.py`（转发层，保持旧 import 路径）、
> `backend/app/services/semantic_release_domain_checks.py`（域级 + 绑定级校验项）、
> `backend/app/services/procurement_binding_candidates.py`（活库候选汇总）、
> `backend/app/services/semantic_release_service.py`（编排层：切片接线 + `rollback()`）、
> `backend/app/services/semantic_release_support.py`（支撑层）、
> `backend/app/services/semantic_diff_service.py`（绑定分区 diff）。
