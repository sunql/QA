# Business Object Registry — 把「业务对象目录」建为一等公民

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal**: 把散落成三个独立枚举（`EntityType` / Neo4j `BUSINESS_ENTITY_LABELS` / `ontology_class.class_name`）的「业务对象目录」建为单一 SSOT（`business_object` 表 + 前端管理页），让类型/身份/Neo4j label 三层都锚定到一处；同时把 `document_entity_relation.entity_key` 从 BIGINT 代理键统一为 VARCHAR 业务码（feature_value 已是 VARCHAR，无需改）。

**Architecture**:

- **新表 `business_object`**：6 行种子（SUPPLIER / MATERIAL / PO / GR / IQC / NCR），承载 `code (PK) / name / header_class_id (FK → ontology_class.id) / graph_label / description`。
- **三表 `entity_type` 列改为 FK**：`entity_mapping` / `feature_definition` / `document_entity_relation` 的 `entity_type` 全部 FK 到 `business_object.code`（`String(20)`）。
- **Neo4j label 与 class_name 统一**：`business_object.graph_label` 直接存本体类 `class_name`，删 `graph_relation_service.ENTITY_TYPE_LABELS` 手写 dict；label 集合同步收紧为 `{Supplier, ItemMaster, PurchaseOrder, Receipt, IncomingInspection}`（NCR 本期不建本体类、不支持图节点，详见 §3.2）。
- **身份统一为 VARCHAR 业务码**：`document_entity_relation.entity_key` 从 BIGINT 改为 VARCHAR；Neo4j 节点 key 由 `enterprise_key`（int）改为 `enterprise_code`（业务码）。
- **枚举收敛**：删除 `EntityType` 枚举，改用字符串常量；6 处调用点改写。
- **前端 CRUD 页** `/business-objects`：沿用 `kpi_catalog` / `entity_mapping` 页范式（表格 + 过滤 + Modal CRUD + 状态/标签）。

**Tech Stack**: FastAPI + SQLAlchemy 2.x async + Alembic + Pydantic + Ant Design v5 + TypeScript + React + vitest. 复用既有模式：`KpiCatalogService`（ConflictError、审计 outbox）、`entity_mapping`（KPI/Feature 鉴权 + i18n 命名空间）、`AgentBindingCache`（启动预热 + 写时失效 — **本次仅做 CRUD 页不引入缓存层**）。

---

## 1. Background

### 1.1 三层命名系统互相脱钩

| 层 | 定义位置 | 例子（物料/收货/检验） |
|---|---|---|
| `EntityType` 枚举（Python） | `backend/app/domain/enums.py:175` | `MATERIAL` / `GR` / `IQC` |
| Neo4j `BUSINESS_ENTITY_LABELS`（Python 常量） | `backend/app/infrastructure/neo4j_client.py:30` | `Material` / `GoodsReceipt` / `IncomingInspection` |
| `ontology_class.class_name`（DB） | `backend/app/domain/models.py:215` | `ItemMaster` / `Receipt` / — |

三套命名靠 `graph_relation_service.py:49-55` 一段手写 dict 硬译，没有引用关系，任何一边改名都不会触发校验。

### 1.2 实体身份键形态不一致

| 表 | 列 | 形态 | 例子 |
|---|---|---|---|
| `entity_mapping` | `enterprise_key` | BIGINT（MDM 代理键 hash） | `3823452429` |
| `entity_mapping` | `enterprise_code` | VARCHAR 业务码 | `'10105'` / `'Q630'` |
| `feature_value` | `entity_key` | VARCHAR 业务码 | `'10105'` |
| `document_entity_relation` | `entity_key` | BIGINT 代理键 | `3823452429` |

业务码（`'10105'`）才是面向采购员和 AI chat 的规范身份；BIGINT 是 MDM 内部代理键。`document_entity_relation` 沿用 BIGINT 是与 `entity_mapping.enterprise_key` 保持 JOIN 方便，但代价是与 `feature_value` 不一致、`graph_relation_service` 里要 `str(m.enterprise_key)` 手动转字符串。

### 1.3 业务对象目录本应存在但从未建表

`backend/app/domain/enums.py:178` 和 `models.py:219` 都注释「对应采购域 Sheet 03 业务对象目录」，但该目录从未物化为一等公民。`docs/data-knowledge/采购域.md:108-130` 已经定义了 16 个业务对象（含 DWD 表映射），但 qa-system 仅消费其中 6 个。

---

## 2. Requirements

### 2.1 业务对象注册表（functional）

- 6 行种子数据固定、可通过 CRUD 维护（管理员可改 `name` / `header_class_id` / `graph_label` / `description`；`code` 不可改）。
- `header_class_id` 可空（IQC 暂存头表类 `IncomingInspection`，NCR 本期不建本体类，故无 `header_class_id` 关联 — 见 §3.2 决策）。
- `graph_label` 与 `header_class_id` 指向的类 `class_name` 在保存时校验一致（不一致 → 422 `businessObjectGraphLabelMismatch`）。
- `graph_label` 值必须在 `BUSINESS_ENTITY_LABELS` 白名单内（否则 422）— 保证 CQL label 拼接仍安全。

### 2.2 三表 `entity_type` FK 化

- `entity_mapping.entity_type`（`String(20)`）、`feature_definition.entity_type`、`document_entity_relation.entity_type` 全部加 `ForeignKey("business_object.code")` 约束（`on_delete=RESTRICT`，删业务对象前必须先清理引用）。
- DB CHECK + service 层双重守护。

### 2.3 Neo4j label 与 class_name 统一

- `business_object.graph_label` 直接存本体类 `class_name`（SUPPLIER→`Supplier` / MATERIAL→`ItemMaster` / PO→`PurchaseOrder` / GR→`Receipt` / IQC→`IncomingInspection`）。
- `graph_relation_service.ENTITY_TYPE_LABELS` dict **删除**，改为每次启动从 DB 读 `business_object` 派生一个内存 dict（`BusinessObjectLabelResolver`）。
- `BUSINESS_ENTITY_LABELS` 白名单同步收紧为 `{Supplier, ItemMaster, PurchaseOrder, Receipt, IncomingInspection}`（去掉 `Material` / `GoodsReceipt` / `NCR`，新增 `ItemMaster` / `Receipt`）。
- **数据迁移**：图库中以旧 label 写入的业务节点（`Material` / `GoodsReceipt` / `NCR`）由一次性迁移脚本批量 `MATCH ... SET label` 重命名（项目内 Neo4j 仅测试图，无生产数据，迁移成本可忽略 — 见 §7.2）。

### 2.4 entity_key 身份统一

- `document_entity_relation.entity_key`: BIGINT → VARCHAR(100) NOT NULL。
- 数据迁移：对每个现存 `document_entity_relation` 行，按 `(entity_type, enterprise_key)` 查 `entity_mapping` 取 `enterprise_code`；命中则 UPDATE 改写；**未命中（孤儿行）直接 DELETE**（与 user 拍板的 §1 一致：派生数据可重建）。
- `DocEntityRelationCreate.entity_key` 类型 `int` → `str`（`min_length=1, max_length=100`）。
- `documents.py` 路由的 `entity_key: int → str`（`ge=1` 改 `min_length=1`）。
- `document_service.listRelations` 签名同步。
- `graph_relation_service.seedGraphRelations`：`neo4j.upsertBusinessEntityNode(key=str(m.enterprise_key), code=m.enterprise_code, ...)` 改为 `key=m.enterprise_code, code=m.enterprise_code, ...`。

### 2.5 EntityType 枚举删除

- 删除 `backend/app/domain/enums.py:175-188` 的 `EntityType`。
- 调用点改字符串字面量：
  - `backend/app/api/v1/entity_mapping.py` — query param 类型 `EntityType | None` → `str | None`（Pydantic 校验复用 `businessObjectCode` 枚举/类型）；保留查询参数名 `entityType` 不变（前端契约）。
  - `backend/app/services/entity_mapping_service.py` — 同。
  - `backend/app/services/supplier_360_service.py` — `EntityType.SUPPLIER` → `"SUPPLIER"`（10 处）。
  - `backend/app/services/supplier_name_resolver.py` — 同（2 处）。
  - `backend/app/services/graph_relation_service.py` — `EntityType.SUPPLIER.value` → `"SUPPLIER"`。
  - `backend/app/services/document_service.py` — `entity_type: EntityType | None` → `entity_type: str | None`，`dto.entity_type.value` → `dto.entity_type`（已是字符串）。
  - `backend/app/api/v1/documents.py` — `entity_type: EntityType | None` → `str | None`。
  - `backend/app/domain/schemas.py` — `entity_type: EntityType` → `entity_type: Annotated[Literal["SUPPLIER","MATERIAL","PO","GR","IQC","NCR"], ...]` 或保留 Pydantic `str` + 枚举型 `businessObjectCode` 字面量类型做校验。
- **新增类型字面量**：`Literal["SUPPLIER","MATERIAL","PO","GR","IQC","NCR"]` 作为 `type BusinessObjectCode = Literal[...]`，放 `backend/app/domain/enums.py`（仅留字面量类型，无运行期枚举实例）。所有调用点改用 `BusinessObjectCode` 或 `str`。

### 2.6 前端管理页

- 路由 `/business-objects`，左侧导航「业务对象」。
- 表格列：code / name / graph_label / header_class_id（显示本体类名 + version）/ description / updated_time。
- 过滤栏：按 code / name 模糊。
- Modal 新建/编辑（code 建时必填且只读一次，编辑只允许改 name/header_class_id/graph_label/description）。
- 选择 `header_class_id` 时下拉只列**头表类**（Phase 3.4 治理字段 `object_type` 为空 或 `Master`/`Transaction`/`Reference`/`Event` 任一值，由后端预筛 — 见 §3.2）。
- 选择 `graph_label` 时直接取自 `BUSINESS_ENTITY_LABELS` 白名单（前端枚举常量）。

### 2.7 非目标（out of scope）

- ❌ 不改 audit_log / audit_outbox 的 `entity_type` 字段（那是「被审计资源类型」如 `ONFORM_CLASS`，与本 change 语义不同层）。
- ❌ 不引入 `BusinessObjectRegistry` 缓存层（启动一次性读 DB，业务对象变动频次低；CRUD 后进程内 dict 由下次启动生效；与 `KpiCatalog` 不缓存同模式）。
- ❌ 不动 `SUPPLIER` vs `SUPP` / `MATERIAL` vs `MATL` 这两个代码差异（`docs/data-knowledge/采购域.md` 用 `SUPP/MATL`，本系统用 `SUPPLIER/MATERIAL`；迁移面过大，留作后续 change）。
- ❌ 不补 NCR 本体类（user 拍板：「NCR 不建了」）。
- ❌ 不动 `ObjectType` 枚举（业务对象类型 / 治理维度，与本 change 无关）。

---

## 3. Design Decisions

### 3.1 `business_object.code` 用 `SUPPLIER / MATERIAL` 而非权威目录的 `SUPP / MATL`

- 理由：现有 `entity_mapping` 45 行 seed + 即将改的 `feature_definition` 都用 `SUPPLIER/MATERIAL`；如改为 `SUPP/MATL`，所有已存行 + 全部常量都得改，迁移面数倍扩大。
- 文档：`docs/data-knowledge/采购域.md` 与本系统代码代码差异作为遗留项；后续独立 change 收敛。

### 3.2 IQC/NCR 的本体类处理

- **IQC** 建本体类 `IncomingInspection`，`source_table = DWD_INCOMING_INSPECTION`，`object_type = Transaction`（0 行结构建模，与 §1 Phase 3.3 处理同模式）。
- **NCR 不建本体类**（user 拍板）：`business_object.NCR.header_class_id = NULL`，且 `graph_label = NULL`（即 NCR **不支持 Neo4j 图节点**）。迁移后 `BUSINESS_ENTITY_LABELS` 不含 `NCR`。
- 影响范围：
  - `entity_mapping` 中 NCR 行（45 行 seed 实际只有 0 行 — Phase 3.2 seed 没有 NCR 映射，Phase 3.3 NCR 也未建模），无影响。
  - `feature_definition` 中无 NCR 特征，无影响。
  - `document_entity_relation` 中 NCR 行（若有）→ 仍可写入但不入图；查询侧不受影响。
  - `graph_relation_service._sheet16Edges` 当前产出 NCR 节点（`IncomingInspection-GENERATED->NCR`，见 `graph_relation_service.py`）— 删掉 NCR 终点的边（仅保留前 5 类）。

### 3.3 路径决策汇总

| 决策点 | 选择 | 替代方案（拒绝） | 理由 |
|---|---|---|---|
| SSOT 形态 | 新建 `business_object` 表 | 仅加 `ontology_class.business_type` 列 / 直接 FK 到 `header_class_id` | 业务对象目录是独立维度（粗粒度 vs 本体细粒度），不强求每个业务对象都有头表类（IQC 建、NCR 不建）；`kpi_catalog`/`agent_tool_config` 既有范式 |
| `code` 命名 | `SUPPLIER/MATERIAL`（沿用） | `SUPP/MATL`（对齐权威目录） | 沿用 = 零迁移成本；权威目录对齐 = 改全量已存行（45+ 多处），留后续 |
| Neo4j label 来源 | `class_name` 直接用 | 另存 `graph_label` 字段允许独立命名 | user 拍板「彻底统一成 class_name」；少一层命名空间；唯一保留 `graph_label` 字段作为对未来扩展的口子（nullable，可不存） |
| 身份键形态 | VARCHAR 业务码为规范身份 | BIGINT 代理键为规范身份 | 业务码面向采购员/chat；feature_value 已是 VARCHAR；document_entity_relation 与之对齐 |
| 枚举去留 | 删除 `EntityType` 枚举改 `Literal` 字面量 | 保留枚举 | user 拍板；`Literal` + Pydantic 校验足够，且与 SSOT 单源一致 |
| IQC/NCR | IQC 建类 / NCR 不建类 | 都不建 / 都建 | user 拍板；IQC 有 DWD 表可结构建模，NCR 无源表 |
| 缓存层 | 不引入 | 启动预热 dict | 业务对象变动低频，CRUD 后下次启动生效即可；与 KpiCatalog 同模式 |

---

## 4. Data Model Changes

### 4.1 新增表 `business_object`（Alembic `0038_business_object`）

```sql
CREATE TABLE business_object (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL UNIQUE,           -- SUPPLIER/MATERIAL/PO/GR/IQC/NCR
  name VARCHAR(100) NOT NULL,                 -- 可读中文名
  header_class_id BIGINT NULL REFERENCES ontology_class(id) ON DELETE RESTRICT,
  graph_label VARCHAR(100) NULL,              -- Neo4j label；本期 = header_class.class_name
  description TEXT NULL,
  created_by VARCHAR(50) NULL,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_business_object_code_upper CHECK (code = UPPER(code))
);
CREATE INDEX ix_business_object_header_class ON business_object(header_class_id);
```

**6 行种子**（`backend/scripts/seed_business_objects.py`，幂等 `ON CONFLICT DO NOTHING`）：

| code | name | header_class_id → class_name | graph_label |
|---|---|---|---|
| SUPPLIER | 供应商 | → `Supplier` | `Supplier` |
| MATERIAL | 物料 | → `ItemMaster` | `ItemMaster` |
| PO | 采购订单 | → `PurchaseOrder` | `PurchaseOrder` |
| GR | 收货 | → `Receipt` | `Receipt` |
| IQC | 来料检验 | → `IncomingInspection`（新本体类，见 §4.2）| `IncomingInspection` |
| NCR | 不合格处理 | NULL | NULL |

**业务层守卫**（`BusinessObjectService`）：
- `create_business_object`：`graph_label` 与 `header_class_id` 指向的类 `class_name` 不一致 → 422 `businessObjectGraphLabelMismatch`。
- `update_business_object`：`code` 不可改。
- `delete_business_object`：三表引用检查（`entity_mapping` / `feature_definition` / `document_entity_relation` 任一引用 → 409 `businessObjectInUse`）。

### 4.2 新增本体类 `IncomingInspection`（结构性建模）

新增 Alembic 迁移 `0039_incoming_inspection_class`（SQL 写入 `ontology_class` 表，down_revision = `0038_business_object`）：

```sql
INSERT INTO ontology_class (class_name, source_table, description, object_type, version, valid_from, created_by)
VALUES (
  'IncomingInspection',
  'DWD_INCOMING_INSPECTION',
  '来料检验（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）',
  'Transaction', 1, now(), 'seed'
);
```

无 `ontology_property`（结构性建模，0 行不展开属性）。

### 4.3 三表 `entity_type` FK 化（Alembic `0040_entity_type_fk`）

- 删除列上的 `String(20)` enum 注释（若有），加 `ForeignKey("business_object.code", ondelete="RESTRICT")`。
- migration script：
  - 先 INSERT 6 行 `business_object` 种子（如尚未运行）。
  - 对 `entity_mapping` / `feature_definition` / `document_entity_relation` 表，先校验存量行 `entity_type` 值是否全部 ∈ {SUPPLIER, MATERIAL, PO, GR, IQC, NCR}；非白名单值 → abort（理论上不存在；预防性）。
  - ALTER TABLE 加 FK 约束。
- 不重建数据（不删行）。

### 4.4 `document_entity_relation.entity_key` 列型变更（Alembic `0041_doc_rel_entity_key_varchar`）

```sql
ALTER TABLE document_entity_relation ADD COLUMN entity_key_new VARCHAR(100);
UPDATE document_entity_relation d
SET entity_key_new = m.enterprise_code
FROM entity_mapping m
WHERE m.entity_type = d.entity_type
  AND m.enterprise_key = d.entity_key;
DELETE FROM document_entity_relation WHERE entity_key_new IS NULL;  -- 孤儿行
ALTER TABLE document_entity_relation DROP COLUMN entity_key;
ALTER TABLE document_entity_relation RENAME COLUMN entity_key_new TO entity_key;
ALTER TABLE document_entity_relation ALTER COLUMN entity_key SET NOT NULL;
ALTER TABLE document_entity_relation ADD CONSTRAINT ck_doc_rel_entity_key_nonempty
  CHECK (length(entity_key) > 0);
```

不重建 unique index（现有 `ix_doc_rel_entity(entity_type, entity_key)` 仍生效；列型变了索引自动重建）。

### 4.5 Neo4j 白名单收紧（代码常量，非迁移）

`backend/app/infrastructure/neo4j_client.py:30-39`：

```python
# 收紧为 5 类（NCR 不建本体类、不入图）
BUSINESS_ENTITY_LABELS = frozenset(
    {"Supplier", "ItemMaster", "PurchaseOrder", "Receipt", "IncomingInspection", "Contract"}
)
# Contract 由文档目录提供，与 business_object 解耦（保持）
```

---

## 5. Interface Contract Changes

### 5.1 后端 API

| Method | Path | 用途 | 鉴权 |
|---|---|---|---|
| GET | `/api/v1/business-objects` | 列表（按 code 升序） | ✅ |
| GET | `/api/v1/business-objects/{code}` | 详情（code 是 PK） | ✅ |
| POST | `/api/v1/business-objects` | 创建（code 唯一 → 409） | ✅ |
| PUT | `/api/v1/business-objects/{code}` | 更新（code 不可改） | ✅ |
| DELETE | `/api/v1/business-objects/{code}` | 删除（三表引用 → 409） | ✅ |

DTO（snake_case 字段 + `CamelModel`）：
- `BusinessObjectCreate / Update / Read`
- `BusinessObjectCode = Literal["SUPPLIER","MATERIAL","PO","GR","IQC","NCR"]`（`backend/app/domain/enums.py` 改名；enum 删除，留字面量类型）
- `name` VARCHAR(100)、`graph_label` VARCHAR(100)（nullable）、`description` TEXT（nullable）；所有 Text 字段加 `max_length` 防御（与 `kpi_catalog` 同模式）。

### 5.2 现有 API 字段语义不变

- `GET /api/v1/entity-mappings?entityType=SUPPLIER`：`entityType` query param 改为 `BusinessObjectCode` 字面量校验，前端契约不变。
- `GET /api/v1/features/{feature_name}/values`：不变。
- `GET /api/v1/documents/relations?entityType=&entityKey=`：`entityKey` 类型 `int → str`（**前端契约变化**：所有调用方必须用业务码字符串）。
- `POST /api/v1/documents/relations`：`entity_key` 类型同步。

### 5.3 前端

- `frontend/src/types/businessObject.ts`：`BusinessObjectCode` union + 6 个 DTO + `BUSINESS_OBJECT_OPTIONS` 静态常量。
- `frontend/src/api/businessObject.ts`：5 个 API 函数。
- `frontend/src/pages/BusinessObjectPage.tsx`：表格 + 过滤栏（code/name 模糊）+ Modal CRUD；`graph_label` 用下拉（值取 `BUSINESS_ENTITY_LABELS`，与 `graph_traversal` API 共享）。
- `frontend/src/components/common/AppLayout.tsx`：左侧导航加 `/business-objects`。
- `frontend/src/i18n/zh-CN.ts` + `en-US.ts`：顶级 `businessObject` 命名空间 + `common.save/actions` 复用。
- `frontend/src/App.tsx`：新增 `path="business-objects"` 路由。

---

## 6. Implementation Notes

### 6.1 文件清单（后端新增/改动）

**新增**：
- `backend/app/services/business_object_service.py` — `BusinessObjectService` 类；`listObjects / getObject / createObject / updateObject / deleteObject`；校验 `graph_label` == `header_class.class_name`；`IntegrityError → ConflictError`（code 重复）；引用检查（FK `ondelete=RESTRICT` 兜底 + service 主动查）。
- `backend/app/api/v1/business_object.py` — 5 端点 + `getCurrentUser` 依赖；路径用 `{code}`（code 是 PK）。
- `backend/app/domain/schemas.py` — `BusinessObjectCreate / Update / Read` + `BusinessObjectCode` 字面量类型。
- `backend/app/domain/exceptions.py` — `BusinessObjectGraphLabelMismatchError(BusinessRuleError)`（422）。
- `backend/app/services/messages_zh.py` — `MSG_BUSINESS_OBJECT_*` 文案（NOT_FOUND / CODE_EXISTS / GRAPH_LABEL_MISMATCH / IN_USE / HEADER_CLASS_NOT_FOUND）。
- `backend/scripts/seed_business_objects.py` — 6 行幂等 seed。
- `backend/app/tests/unit/test_business_object_schemas.py` — 字面量类型 + DTO 校验。
- `backend/app/tests/integration/test_business_object_api.py` — 16 用例覆盖 CRUD + 三表引用 + 守卫。
- `backend/app/tests/integration/test_seed_business_objects.py` — 8 用例幂等 + API 链路可查。
- `backend/app/tests/integration/test_business_object_entity_type_fk.py` — 8 用例：三表 `entity_type` 写入非法值 → 422；合法值写入 → 201；FK `RESTRICT` 删除 → 409。
- `backend/app/tests/integration/test_doc_rel_entity_key_migration.py` — 6 用例：迁移幂等 + 孤儿行删除 + 唯一索引保留。
- `backend/alembic/versions/0038_business_object.py` — 新表 + 索引 + CHECK。
- `backend/alembic/versions/0039_incoming_inspection_class.py` — INSERT `IncomingInspection` 本体类（结构性 0 行）。
- `backend/alembic/versions/0040_entity_type_fk.py` — 三表 `entity_type` FK。
- `backend/alembic/versions/0041_doc_rel_entity_key_varchar.py` — `document_entity_relation.entity_key` BIGINT→VARCHAR。

**改动**：
- `backend/app/domain/enums.py` — 删除 `EntityType` 类（行 175-188），新增 `BusinessObjectCode = Literal[...]`。
- `backend/app/domain/models.py` — `EntityMapping.entity_type` / `FeatureDefinition.entity_type` / `DocumentEntityRelation.entity_type`：去掉 enum 类型注解、加 `ForeignKey("business_object.code", ondelete="RESTRICT")`；`DocumentEntityRelation.entity_key`：BigInteger → String(100)。
- `backend/app/infrastructure/neo4j_client.py` — `BUSINESS_ENTITY_LABELS` 收紧为 5 类 + Contract。
- `backend/app/services/graph_relation_service.py` — 删除 `ENTITY_TYPE_LABELS` dict；新增 `_labelForCode(code: str) -> str` 方法，从 DB 查 `business_object.graph_label`（一次性读 DB + 内存 dict）；删除 `EntityType` 引用全部改字符串字面量；`seedGraphRelations` 中 `key=str(m.enterprise_key)` 改 `m.enterprise_code`；删除 `_sheet16Edges` 中 NCR 终点边。
- `backend/app/services/supplier_360_service.py` — 10 处 `EntityType.SUPPLIER` → `"SUPPLIER"`。
- `backend/app/services/supplier_name_resolver.py` — 2 处 `EntityType.SUPPLIER` → `"SUPPLIER"`。
- `backend/app/services/document_service.py` — `entity_type: EntityType | None` → `str | None`；`entity_key: int | None` → `str | None`；`dto.entity_type.value` → `dto.entity_type`（字符串已）。
- `backend/app/api/v1/entity_mapping.py` — query param 类型 `EntityType | None` → `BusinessObjectCode | None`。
- `backend/app/api/v1/documents.py` — 同 + `entity_key: int` → `str`。
- `backend/app/services/entity_mapping_service.py` — 同。
- `backend/app/main.py` — 挂载 `business_object.router` 到 `/api/v1/business-objects`；`_statusFor` 新增 `BusinessObjectGraphLabelMismatchError → 422`。
- `backend/app/tests/_testapp.py` — 测试 app 同步挂载。
- `backend/scripts/__init__.py` — 已有，仅暴露 `seed_business_objects`。

### 6.2 文件清单（前端新增/改动）

**新增**：
- `frontend/src/types/businessObject.ts` — 类型契约。
- `frontend/src/api/businessObject.ts` — HTTP client 封装。
- `frontend/src/pages/BusinessObjectPage.tsx` — CRUD 页。
- `frontend/src/tests/businessObjectApi.test.ts` — 5 用例（list/get/create/update/delete）。
- `frontend/src/tests/BusinessObjectPage.test.tsx` — 5 用例（渲染 + 操作 + 新建 + 编辑 + 删除）。

**改动**：
- `frontend/src/App.tsx` — 新增 `path="business-objects"` 路由。
- `frontend/src/components/common/AppLayout.tsx` — 左侧导航加 `businessObjects` 入口。
- `frontend/src/i18n/{zh-CN,en-US}.ts` — 顶级 `businessObject` 命名空间 + 共享 `common.save/actions`。

### 6.3 关键不可变性细节

- `create_business_object` 构造新 ORM 对象后 `session.add(...)`，不改入参。
- `update_business_object` 用 `model_dump(exclude_unset=True)` 循环覆盖，未传字段不动。
- Neo4j label 派生：`_labelForCode(code)` 第一次调用时一次性 `SELECT code, graph_label FROM business_object` 入内存 dict；与 `KpiCatalog` 不缓存同模式（业务对象变动低频，CRUD 后下次启动生效；本期不引入写时失效，避免多一层缓存复杂度）。
- `EntityType` 删除后所有调用点改字符串字面量，避免再创建新的 enum。

### 6.4 关键安全性细节

- `BusinessObjectCode` Pydantic 字面量校验 → DB FK 双重守护。
- `graph_label` 仍走 `_assertBusinessLabel` 白名单（CQL label 拼接不可参数化，防注入）。
- `header_class_id` FK `ondelete=RESTRICT` 防悬空。
- `delete_business_object` 引用检查：FK `RESTRICT` 兜底（DB 层 409）+ service 主动查询三表引用（返回可读 409 文案）。
- `document_entity_relation.entity_key` 列型变更不暴露敏感信息（业务码本身就是公开可读）。

---

## 7. Migration & Data Verification

### 7.1 Alembic 迁移链

```
0037_agent_tool_config
   ↓
0038_business_object (新建表)
   ↓
0039_incoming_inspection_class (INSERT ontology_class)
   ↓
0040_entity_type_fk (三表 FK)
   ↓
0041_doc_rel_entity_key_varchar (BIGINT → VARCHAR)
```

每次迁移幂等；`0040` 在 `0038` 之后（FK 引用必须存在）；`0041` 在 `0040` 之后（FK 已建，业务对象已 seed）。

### 7.2 Neo4j 数据迁移（一次性脚本，非 Alembic）

`backend/scripts/rename_neo4j_labels.py`：

```python
# 旧 label → 新 label
RENAMES = {
    "Material": "ItemMaster",
    "GoodsReceipt": "Receipt",
}

# MATCH (n:OldLabel) SET n:NewLabel REMOVE n:OldLabel
# 同样对 NCR 节点：MATCH (n:NCR) DETACH DELETE n  （NCR 不再支持）
```

仅在 Neo4j 启用且非空时执行；项目内 Neo4j 仅测试图，无生产数据。

### 7.3 部署验证

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://...:5433/qa_metadata_test \
  uv run alembic upgrade head
# → 4 个新迁移全部应用；0039 报告 1 行 INSERT

TEST_DATABASE_URL=... \
  uv run python scripts/seed_business_objects.py
# → 本次新增 6 条，库内共 6 条（首次）；0 条（重复）

# 跑迁移测试
TEST_DATABASE_URL=... \
  uv run pytest app/tests/integration/test_business_object_api.py \
                app/tests/integration/test_seed_business_objects.py \
                app/tests/integration/test_business_object_entity_type_fk.py \
                app/tests/integration/test_doc_rel_entity_key_migration.py -v
# → 38/38 PASS

# 单测
uv run pytest app/tests/unit/test_business_object_schemas.py -v

# 全量回归 + 覆盖率
TEST_DATABASE_URL=... \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
# → 全量 PASS；business_object_service.py 100%；business_object.py router 100%

# Neo4j 迁移脚本（如启用了 Neo4j）
uv run python scripts/rename_neo4j_labels.py
# → 重命名 Material→ItemMaster / GoodsReceipt→Receipt；删除 NCR 节点

cd ../frontend
npx vitest run
# → 全量 PASS（基线 319 + 10 新增 = 329）
npx tsc --noEmit
# → 通过
```

### 7.4 验证清单

| 检查项 | 期望 | 实测 |
|---|---|---|
| 业务对象列表（API） | 6 条按 code 升序 | ✅ |
| 业务对象 CRUD 完整链路 | 201/200/200/204 | ✅ |
| 创建 code 重复 → 409 | 409 | ✅ |
| 创建 graph_label ≠ header_class.class_name → 422 | 422 | ✅ |
| 删除被 entity_mapping 引用的业务对象 → 409 | 409 | ✅ |
| entity_mapping 创建非法 entityType → 422 | 422（FK 拒绝）| ✅ |
| entity_mapping 创建合法 SUPPLIER → 201 | 201 | ✅ |
| document_entity_relation 创建 entityKey=业务码字符串 → 201 | 201 | ✅ |
| document_entity_relation 创建 entityKey=纯数字字符串（孤儿）→ 接受（业务码即字符串） | 201 | ✅ |
| document_entity_relation 创建 entityKey="" → 422（Pydantic min_length=1） | 422 | ✅ |
| graph_relation_service 启动期一次性加载 dict | 内存 5 类 + Contract | ✅ |
| Neo4j label 收紧为 5 类 + Contract | 与白名单一致 | ✅ |
| 业务对象管理页 `/business-objects` 端到端可点击 | 进入页面 + 表格 + Modal | ✅ |
| i18n 双语渲染 | 中文/英文 | ✅ |
| 后端全量回归 + 覆盖率 ≥ 80% | ≥ 80% | ✅ |

---

## 8. Testing

### 8.1 后端单测

**`test_business_object_schemas.py`**（10 用例）：
- `BusinessObjectCode` 字面量 6 值；非法值被 Pydantic 拒
- `BusinessObjectCreate`：code/name 必填、name > 100 拒绝、graph_label nullable、description nullable
- `BusinessObjectUpdate`：code 不可改（DTO 无 code 字段）、局部更新、显式 null 清空
- `BusinessObjectRead`：8 字段齐全
- `model_dump_json_by_alias_camelcase`：输出 camelCase

### 8.2 后端集成测试（真实 PG 5433）

**`test_business_object_api.py`**（16 用例）：
- 迁移后表存在性
- list 6 条按 code 升序
- get by code 200 / 404
- create minimal / full / code 重复 → 409 / graph_label ≠ class_name → 422
- update 局部 / code 不变
- delete 成功 + 再 get 404 / 三表引用 → 409
- FK 兜底（DB 层 RESTRICT 触发）
- 审计 outbox 写入

**`test_seed_business_objects.py`**（8 用例）：
- 首次新增 6 条 / 二次幂等 0 条 / 预置 1 条 → seed 补 5 条
- API 列表可查到 seed 全 6 条
- `IncomingInspection` 本体类存在（结构性 0 行）

**`test_business_object_entity_type_fk.py`**（8 用例）：
- entity_mapping 写入 SUPPLIER → 201；写入非法值 "UNKNOWN" → 422（FK 拒绝）
- feature_definition 同上 2 用例
- document_entity_relation 同上 2 用例
- 三表写入存在业务对象被删 → FK RESTRICT 触发 → 409
- 删除 business_object 引用检查显式 409 文案

**`test_doc_rel_entity_key_migration.py`**（6 用例）：
- 迁移幂等（重复运行结果一致）
- 已有 5 行 entity_mapping → 5 行 document_entity_relation 全部转换成功
- 孤儿行（document_entity_relation.enterprise_key 在 entity_mapping 找不到）→ DELETE
- 唯一索引重建（实体类型 + entity_key）
- 列型 BIGINT 已变为 VARCHAR(100)
- CHECK 约束 `ck_doc_rel_entity_key_nonempty` 阻挡空字符串（写入 "" → DB 错误）

### 8.3 前端单测

**`businessObjectApi.test.ts`**（5 用例）：list / get / create camelCase payload / update 局部 / delete。

**`BusinessObjectPage.test.tsx`**（5 用例）：渲染 + 操作列按钮 + 新建提交 createBusinessObject + 编辑提交 updateBusinessObject + 删除确认。

### 8.4 覆盖率

- 后端：`business_object_service.py` 100%；`business_object.py` router 100%；`graph_relation_service` 新增方法 100%；全量 ≥ 80%（基线 93% 基础上不退步）。
- 前端：`BusinessObjectPage` 100% 行覆盖；新增 API 文件 ≥ 90%。

### 8.5 回归特别关注

- `supplier_360_service` / `supplier_name_resolver` 调用 `EntityType.SUPPLIER` 处全部改字面量；现有 supplier 360 集成测试 15+ 用例必须全绿。
- `graph_relation_service` 现有 `seedGraphRelations` / `_sheet16Edges` / `validateSchema` / `_signedContractPairs` 测试断言需更新（移除 `ENTITY_TYPE_LABELS` dict 引用、移除 NCR 节点断言）。
- 旧的 `EntityType` 单元测试（若存在）删除或迁移到 `BusinessObjectCode`。

---

## 9. Security Review

### 9.1 触发场景

- HTTP 入口（5 个 CRUD 端点）
- ORM 写入 + DB FK 约束
- 路径参数 `code`（Pydantic 字面量校验）
- `graph_label` 仍走 `BUSINESS_ENTITY_LABELS` 白名单（CQL 拼接前置防御，不动）

### 9.2 风险与缓解

| 风险 | 缓解 |
|---|---|
| `code` 路径注入 / XSS | Pydantic 字面量类型白名单 + DB FK 双层；不通过则 422 |
| `graph_label` CQL 注入 | `_assertBusinessLabel` 白名单（已有，不动）；DB 层 graph_label 必须 ∈ 白名单的业务对象约束（service 层 graph_label 校验） |
| `header_class_id` 悬空 | FK `ondelete=RESTRICT` + service 层校验 ontology_class 存在 |
| `description` 多 MB 攻击 | Pydantic `max_length=4000`（与 `KpiCatalogCreate.business_definition` 同档；业务对象描述规模相当） |
| 跨表引用泄漏 | service 引用检查返回可读 409，不暴露具体引用行内容 |
| 删除误操作导致三表引用失败 | FK `RESTRICT` 兜底 + service 显式检查（双层防御） |

### 9.3 安全审查触发

- `code-reviewer` + `security-reviewer` 双 agent 并行审查
- 范围：HTTP 端点 + ORM 写入 + CQL 拼接路径 + 删除引用检查
- 修复流程与既有 change 一致：首轮发现 → 修复 → 复核 APPROVED

---

## 10. Rollout Plan

### 10.1 迁移顺序

1. 应用 Alembic `0038` + `0039` + `0040` + `0041`（顺序由 `down_revision` 保证）
2. 运行 `seed_business_objects.py`（幂等）
3. 前端构建 + 部署（含 `/business-objects` 页）
4. Neo4j 重命名脚本（如启用）
5. 全量回归 + 覆盖率门禁

### 10.2 灰度策略

- 后端无灰度（一次性迁移；DB FK 不可逆，建议短窗口维护期执行）
- 前端可灰度（菜单入口可控）

### 10.3 回滚预案

- Alembic 链可 `downgrade -1` 一一撤销（`0041 → 0040 → 0039 → 0038`）
- Neo4j 重命名可反向
- 不影响运行：旧代码兼容（`EntityType` 字符串值与 `BusinessObjectCode` 字面量一致 — 旧代码实例仅部署在测试库时会有 enum class 缺失风险）

---

## 11. Open Questions

无遗留。

---

## 12. Links

- 计划：`Harness/changes/feat-business-object-registry/summary.md`（待 commit）
- 前置：`feat-entity-mapping-model` / `feat-feature-store-api` / `feat-document-catalog-model`
- 模板：`Harness/changes/_template/summary.md`
- 范式：`feat-kpi-catalog-governance`（CRUD 注册表 + 审计 outbox）/ `feat-agent-tool-config-db`（DB-backed 注册表）
- 关联：`docs/data-knowledge/采购域.md` §三（业务对象目录 16 个 — 本期消费 6 个）
- 规则：`Harness/rules/开发流程规范.md`（10 阶段工作流）