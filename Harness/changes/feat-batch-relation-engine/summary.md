# 变更：feat-batch-relation-engine

- **日期**：2026-09-09
- **Phase**：本体治理（通用「批量关系引擎」：PG join/语义关系 + Neo4j 入图的一次性批建，含清单模板）
- **状态**：✅ engine implemented + 全量清单推导完成（本 summary 沉淀推导规则）；45 条语义关系**待用户经系统执行**

## 1. 需求

回答一个问题：**已经进入系统的类，如何批量建立关系？**

v1 语义关系（`ontology_relation` 表 + SemanticRelation Tab + `POST /relations/backfill`，见 feat-semantic-relations）
只能**单条建**。本 change 提供一个**相对通用的批量能力**，一次可任选/多选 3 种关系：

1. **本体入图（syncGraph）**：PG `ontology_class/property` 全量 upsert 为 Neo4j 节点 + `HAS_PROPERTY`/`REFERENCES` 边（幂等，让后续边可见）。
2. **物理关联 join（inferJoins / 清单）**：写 `ontology_join`（列级）+ `(:Class)-[:JOIN]->(:Class)`。
3. **语义关系（清单）**：写 `ontology_relation` + `(:Class)-[:{REL}]->(:Class)`。

对**已存在**的关系可选 **skip / overwrite**。关系内容来源 = 系统按共享列推断（可选）+ 用户上传的清单（JSON/CSV）。

**2026-09-09 应用任务（本 summary §9 推导规则 + §10 执行）**：把 **DIM(5)+DWD(27)+DWS(5)+ADS(2)=39 个业务类**的语义关系全量补齐——
现有 `ontology_relation` 仅 1 条（`DWD_BOM SUPPLIES DWD_MATERIAL`），经推导提出 **45 条待建语义关系**，
覆盖 37/39 类。用户将通过系统批量关系引擎完成最终关联（本 change 不代写 prod）。

## 2. 设计评审

| 决策点 | 选择 | 理由 |
|---|---|---|
| 服务归属 | 新 `app/services/ontology_batch_service.py`（587 行） | `ontology_service.py` 已超 800 上限；复用 `makeJoinKey`/`_entityToDict`/`_logNeo4jFailure`/AuditService，不逐条调 `createJoin`（否则每行各自 commit + 重复抛 422） |
| 批量写入 | **内存预取已存在集合 + 逐条判 create/skip/overwrite + 单次 commit** | 不用 `on_conflict_*`（隐藏 insert-vs-update，audit 无从下手）；逐行 CREATE/UPDATE audit |
| 覆盖语义 | join 身份=`join_key`（源/目标/列不变，覆盖 `join_type/relation_type/description`）；relation 身份=(源,目标,relation_type) 三元组（仅覆盖 description；改 relation_type = 新关系） | 键是身份，改键即删除重建语义 |
| 本体入图 | 新 `neo4j_client.syncOntologyNodes`：`UNWIND` 批量 MERGE 节点+边 | 幂等；Neo4j 失败 fail-open（PG 为准） |
| 共享列推断 | Pass A 复用 `SAGE_X3_REFERENCE_MAP`（列名 upper 命中→目标表）；Pass B 通用共享列（≥2 类含同列 + 至少一方 `is_primary_key`，黑名单过滤） | 安全优先，不产生无主键样板列乱连 |
| 模板格式 | JSON（与 schema 严格校验）+ CSV（Excel 友好，服务端解析、按类名反解 id）；模板文件 + 规则文档 | 覆盖「系统跑」与「人工喂」两种用法 |
| 端点 | 4 个：执行 / preview(只读) / 模板下载 / parse-csv(上传回填) | preview 不写库，跑前看计数 |
| 迁移 | **无新表/新列**（复用 `ontology_join` + `ontology_relation`），无 Alembic 迁移 | `ontology_relation` 表本身来自前置 feat-semantic-relations（0049） |
| 类解析 | 按 `class_name` 或 `source_table` 尾段（忽略大小写）；歧义/缺失 → 该行 `BatchRowError`（index=行号），不 fail-fast | 批量容忍坏行，其余继续 |

## 3. 数据模型变更（qa_metadata PG / Neo4j）

**PG：无结构变更。** 复用两表（写新增行，无 DDL）：

- `ontology_join`：`join_key` 唯一，(source_columns/target_columns) JSON，join_type/relation_type/description。
- `ontology_relation`：唯一 `(source_class_id,target_class_id,relation_type)`；relation_type ∈ `ClassRelationType` 6 值。

**Neo4j（qa-neo4j）**：新增 `syncOntologyNodes(classes, properties)` → 节点
`(:Class {id, name, layer, source_table})` + `(:Property {id, name})`，边 `(:Class)-[:HAS_PROPERTY]->(:Property)`
+ `(:Property)-[:REFERENCES]->(:Class)`（ref_class_id 非空时）。幂等 MERGE。

## 4. 接口契约变更

4 个端点（均需登录态 `getCurrentUser`，audit 归属当前 user）：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/v1/ontology/batch` | POST | 执行：`BatchRelationRequest` → `BatchRelationResult` |
| `/api/v1/ontology/batch/preview` | POST | 只读预览：推断候选 + 冲突预判计数，**不写库**（内部 `_execute(write=False, actor="preview")`） |
| `/api/v1/ontology/batch/template?kind=joins|relations` | GET | 下载 CSV 模板（带 BOM，Excel 友好） |
| `/api/v1/ontology/batch/parse-csv` | POST | multipart 上传 CSV（`kind` 为 `Form("relations")`）→ 按类名反解 id + 校验 → 返回可执行 manifest JSON；分块读取 + 硬上限超限 422；超长/非法字段逐行记 `BatchRowError` 不 500 |

**`BatchRelationRequest`**：`syncGraph=false, inferJoins=false, applyManifest=false, onConflict="skip", manifest: RelationManifest|null`；
validator 要求**至少一个动作**为真、`applyManifest` 时 manifest 非空（否则 422）。
**`RelationManifest`**：`joins: [OntologyJoinCreate]`, `relations: [OntologyRelationCreate]`。
**`OntologyJoinCreate`**：relation_type max20 默认 `"business"`；**`OntologyRelationCreate`**：relation_type 必填 max30，service 侧再按 `ClassRelationType` 严格校验。

**CSV 表头契约**：

- relations：`sourceClassName,targetClassName,relationType,description`
- joins：`sourceClassName,sourceColumns,targetClassName,targetColumns,joinType,relationType,description`（多列 `;` 分隔；joinType 缺省 INNER、relationType 缺省 foreign_key）

## 5. 实现要点

- `app/services/ontology_batch_service.py`（新，587 行）：
  - 推断（95-214）：Pass A = 列名 upper ∈ `SAGE_X3_REFERENCE_MAP` → X3 命名约定连目标表；Pass B = 通用共享列（∉ 黑名单、∉ X3 map）且 ≥1 属性 `is_primary_key` → 非 PK 源 → PK 目标（FK→PK），跳过 PK-as-source；无 PK → 无候选。`makeJoinKey` 去重 + 跳自环。
  - `runBatch`：`_syncGraph` → `_applyJoins` → `_applyRelations`（各自 create/update + 逐行 audit + Neo4j `linkClassJoin`/`linkClassRelation`，每次 `_logNeo4jFailure`）→ 单次 `session.commit()`。
  - `SHARED_COLUMN_BLACKLIST`：ETL_LOAD_TS/CREATION_DATE/CREDAT_0/UPDUSR_0/CREUSR_0/UPDDATTIM_0/CREDATTIM_0/CRE_DATE/UPD_DATE 等样板列（可扩充）。
- `app/infrastructure/neo4j_client.py`（713 行）：`syncOntologyNodes`(383) 批量 MERGE；`linkClassRelation`(359)/`linkClassJoin`(333)。
- `app/api/v1/ontology.py`：4 路由（`/batch` 632、`/batch/preview` 654、`/batch/template` 667、`/batch/parse-csv` 684）+ CSV 模板常量。
- `app/domain/schemas.py`：`InferredJoin`(1024)/`RelationManifest`(1037)/`BatchRowError`(1046)/`BatchCounts`(1053)/`GraphSyncResult`(1062)/`BatchRelationRequest`(1071)/`BatchRelationResult`(1093)。
- `app/domain/enums.py`：`ClassRelationType` 6 值（驱动 Neo4j + 前端）。
- 前端：`components/ontology/BatchRelationModal.tsx`（三 Checkbox + conflict Radio + JSON/CSV Segmented + 预览/执行）、`api/ontology.ts`（4 函数）、`pages/OntologyPage.tsx` 头部「批量关系」按钮、i18n。

## 6. 测试

- 后端集成 `app/tests/integration/test_ontology_batch_api.py`（真实 PG `qa_metadata_test` + 完整 API 链路）：
  空 payload 422 / apply_manifest 无 manifest 422 / 非法 on_conflict 422 / X3 + 共享列推断落库 / manifest skip（join、relation）/ overwrite 更新 + audit before-after / 类名缺失记行错继续 / syncGraph 节点计数 / preview 不写库 / 重复清单行 preview 计数对齐 + execute 只写一次 / 模板下载表头 / parse-csv 反解 / 未知类行错 / 超限 422 / 超长字段记行错。
- 前端：`BatchRelationModal.test.tsx`（渲染/勾选/预览/执行/CSV 上传回填/成功计数）+ ontologyApi 4 函数断言 + OntologyPage 按钮。
- 覆盖率：变更文件 funcs ≥80%。

## 7. 安全审查

- 写操作**逐行落 audit**（`ONTOLOGY_JOIN`/`ONTOLOGY_RELATION` CREATE/UPDATE），归属当前登录 user，actor 全链路透传。
- 类/枚举/字段全部**白名单严格校验**；未知类名/relation_type → 行级错误，不注入、不 500。
- parse-csv 分块读取 + **硬上限**（超限立即 422），防大包内存耗尽；超长字段截断记行错。
- SQL 只读约束不变：推断/预览只 SELECT，无写。
- Neo4j/Milvus 类外部依赖失败一律 **fail-open**（`_logNeo4jFailure`），PG 为准、不阻断。

## 8. 部署验证（2026-09-09 实测）

```text
PG qa_metadata：  67 类 / 3629 属性 / 279 join（全 relation_type=foreign_key） / 1 语义关系
Neo4j qa-neo4j：  Class 67 + Property 3629 节点全量在场
                  HAS_PROPERTY 3629 / JOIN 200（类对，← 279 列级 join 聚并） / SUPPLIES 1
```

- 语义清单 parse-csv 校验：relations 45 行 / **0 错误**；joins 279 行 / **0 错误**。
- 批量引擎 preview（只读）：manifest 45 → **created 45 / skipped 0 / errors 0**（预测，未写库）。
- 批量引擎幂等：skip 策略下重放同清单全记 skipped，不产生重复。
- 冲突语义 engine 已由集成测试证明（test_apply_manifest_skip_on_existing / overwrite + audit）。
- 注：engine 代码 + 本清单均位于工作树**未提交**（git status 可见 `?? ontology_batch_service.py` 等），未 commit/push（遵用户约束）。

## 9. 推导规则（2026-09-09，业务层 39 类语义关系全量）

### 9.1 两层关系分开对待

| 层 | 现状 | 性质 | 处置 |
|---|---|---|---|
| 物理 join | **279 条已在库** | 共享列事实：任两表含同名列（`SUPPLIER_CODE`/`MATERIAL_CODE`/`FACILITY_CODE`/`CURRENCY_CODE`/`YEAR_MONTH`/单据号…）就有一条 | **不执行**（重放=全 skip）；仅留 `docs/batch-relation-engine/joins.csv` 作还原/审计快照 |
| 语义关系 | **仅 1 条** | 业务意图（结构/流转/归属），**刻意不复刻 join 机械噪音**（带供应商列的单据都连到全部 DWS 月度表） | **45 条待建 = 本 change 唯一待执行清单** |

### 9.2 覆盖边界

**只对 39 个业务类** = DIM(5) + DWD(27) + DWS(5) + ADS(2)。排除 ODS(27)（DWD 的 1:1 镜像贴源，仅 ETL 血缘有意义）与 ETL 工具表。

### 9.3 枚举方向读法（方向 = `源类 -(type)-> 目标类`，GENERATED 读「源(汇总) 由 目标(明细) 生成」）

| 类型 | 读法 | 本清单数 | 例 |
|---|---|---|---|
| `CONTAINS` | 单据头含行 / 主体含子记录 | 10 | `DWD_PURCHASE_ORDER → DWD_PURCHASE_ORDER_LINE`（键 PO_NO） |
| `GENERATES` | 上游单据驱动下游产生 | 9 | `DWD_PURCHASE_ORDER → DWD_ARRIVAL_NOTICE`（ASN 行经 PO_NO 溯源） |
| `GENERATED` | 源=汇总/派生，由目标明细聚合 | 12 | `DWS_SUPPLIER_QUALITY_MONTHLY → DWD_GOODS_RECEIPT_LINE`（REJECTED_QTY/REJECT_RATE） |
| `SUPPLIES` | 供应商供应物料 | 1 | `DWD_SUPPLIER → DWD_MATERIAL` |
| `INSPECTED_BY` | 主体被其质量档案评估 | 1 | `DWD_SUPPLIER → DWS_SUPPLIER_QUALITY_MONTHLY` |
| `RELATED_TO` | 层间同体 / 主数据归属 / 关联实体 | 12 | `DIM_SUPPLIER ↔ DWD_SUPPLIER`、`DWD_BUSINESS_PARTNER → DWD_SUPPLIER` |

### 9.4 推导方法（可审计）

> 依据 = **真实列** + **既有 join 连通性**。每条 description 记录证据列（PO_NO/REJECTED_QTY/PARTNER_CODE…），
> 不凭印象臆造。方向/归属为**提案**，执行前可逐条改 CSV 那行。

1. **单据结构 CONTAINS（10）**：header→line 家族，键=真实单据号列。采购订单/收货单/到货通知/报价单/采购发票/供应商付款单 = 头→行；`BOM → BOM_DETAIL`（键 MATERIAL_CODE）；`SUPPLIER_PRICE_LIST_HEADER → SUPPLIER_PRICE_LIST`（键 PRICE_LIST_CODE/PRICE_LIST_RECORD）；后补 2 条结构边：`DWD_PURCHASE_REQUISITION_LINE → DWD_REQUISITION_ORDER_LINK`、`SUPPLIER_PRICE_LIST_HEADER → SUPPLIER_PRICE_LIST_CONFIG`。
2. **采购价值链 GENERATES（9）**（各跳均有真实溯源列）：
   `请购行 →(经 DWD_REQUISITION_ORDER_LINK 桥接)→ 采购行`；`请购行 → 报价应询`；`报价 → 采购订单`（QUOTATION_NO）；
   `采购单 → 到货通知`、`采购行 → ASN 行`（PO_NO/PO_LINE_NO）；`到货通知 → 收货`、`ASN 行 → 收货行`；`收货行 →(三单匹配)→ 发票行`（RECEIPT_NO/PO_NO）；`发票 → 付款`（LINKED_INVOICE_NO）。
3. **汇总 GENERATED（12）**：各 DWS 月度 ← **最贴近明细**——
   采购月度 ← 采购行（ORDER_QTY/ORDER_AMOUNT/ORDER_LINE_COUNT）；到货月度 ← 收货行（RECEIVED_QTY/ON_TIME_RATE，含 ASN 计划日期比对）；质量月度 ← 收货行 + ASN 行（REJECTED_QTY/REJECT_RATE）；付款月度 ← 付款**单**（PAYMENT_AMOUNT/PAYMENT_COUNT）；物料价格月度 ← 采购行实际价（AVG/MIN/MAX_NET_UNIT_PRICE）。
   ADS：`SUPPLIER_360` ← 4 个 DWS 月度（ORDER_AMOUNT_12M/OTD_RATE_12M/REJECT_RATE_12M/PAYMENT_AMOUNT_12M）；`SUPPLIER_ORDER_DETAIL` ← 采购行 + 收货行（补交付/质量列）。
4. **主数据 RELATED_TO（12）**：DIM↔DWD 层间同体（MATERIAL/SUPPLIER/FACILITY）；`BUSINESS_PARTNER` ↔ 供应商/客户/承运商（PARTNER_CODE）；`CUSTOMER.SUPPLIER_CODE` → 兼供货供应商；物料 ↔ 工厂视图（MATERIAL_CODE+FACILITY_CODE）/工艺路线/MATERIAL 作 BOM 主料；工厂含其物料-工厂视图；`SUPPLIER_360` 主体为供应商（SUPPLIER_KEY）。
5. **仅 2 类无语义边**：`DIM_DATE`、`DIM_CURRENCY` —— 叶子维度，只被物理 join 引用，无业务语义边（如实记录，不硬凑）。

### 9.5 产物（数据齐备，用户经系统执行）

| 文件 | 内容 | 性质 |
|---|---|---|
| `docs/batch-relation-engine/to-apply/relations.csv` | 45 条（推荐通道） | 执行输入 |
| `docs/batch-relation-engine/to-apply/manifest.example.json` | 同 45（真实 id） | 执行输入（弹窗粘贴） |
| `docs/batch-relation-engine/to-apply/run-sheet.md` | 执行步骤 + 预期 | 指引 |
| `docs/batch-relation-engine/joins.csv` | 279 物理快照 | 还原/审计，勿执行 |

预期结果：PG `ontology_relation` 1→**46**；Neo4j 新增 45 边（CONTAINS 10 + GENERATES 9 + GENERATED 12 + SUPPLIES 1 + INSPECTED_BY 1 + RELATED_TO 12）；audit 45 条 CREATE。节点已在图中，**无需先跑本体入图**（除非全新环境）。

## 10. 关联

- 引擎执行细则：`docs/batch-relation-engine/README.md`
- 执行清单（用户侧跑引擎）：`docs/batch-relation-engine/to-apply/run-sheet.md`
- 前置：`Harness/changes/feat-semantic-relations/summary.md`（ontology_relation 表 + 单条语义关系 v1）
- 前置：`Harness/changes/feat-ontology-neo4j-sync/summary.md`（本体入图起点）
- 数据模型/API 契约：`Harness/wiki/data-model.md`、`Harness/wiki/api-reference.md`
- Wiki 术语：`Harness/wiki/nl2sql-engine.md`（层间血缘读法）
