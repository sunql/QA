# 变更：业务对象注册表（business_object）

- **日期**：2026-09-04
- **作者**：AI 助手（subagent-driven execution）
- **Phase**：Phase 4.4（L4 AI Ready — 语义层扩展；本期消费既有 Phase 3.1 entity_mapping / Phase 4.3 feature_definition / Phase 5.1 document_entity_relation）
- **状态**：**complete — ready for merge**
- **设计稿**：`docs/superpowers/specs/2026-09-04-business-object-registry-design.md`
- **实施计划**：`docs/superpowers/plans/2026-09-04-business-object-registry.md`
- **分支**：`feat/business-object-registry`（26 commits since main，base d01a4e1 → head bb9131a）
- **SDD ledger**：`.superpowers/sdd/2026-09-04-business-object-registry/progress.md`

## 1. 需求（验收标准）

- ✅ 6 行 `business_object` seed 幂等可查（`seedBusinessObjects()` ON CONFLICT DO NOTHING）
- ✅ 三表 `entity_type` 写入非法值 → 422（Pydantic 字面量拒绝 + DB FK 双层守卫）
- ✅ 删除被引用的业务对象 → 409（`BusinessObjectService.deleteObject` 三表引用检查）
- ✅ `graph_label` ≠ `header_class.class_name` → 422（`BusinessObjectGraphLabelMismatchError`）
- ✅ `document_entity_relation.entity_key` VARCHAR 迁移：已有行通过 `entity_mapping.enterprise_code` 回填；孤儿 DELETE
- ✅ Neo4j label 重命名脚本（旧 `Material/GoodsReceipt/NCR` → 新 `ItemMaster/Receipt/DETACH DELETE`）
- ✅ `/business-objects` 前端 CRUD 页端到端可点击（456 frontend tests pass, tsc 0 errors）
- ✅ 后端覆盖率 ≥ 80%（基线 93.21% 未退步）
- ✅ 双 agent 审查（code-reviewer + security-reviewer）：APPROVED after 2 fix rounds

## 2. 设计评审（决策表 + 多视角）

### 决策表（spec §3 摘要）

| 决策点 | 选项 | 选择 | 理由 |
|---|---|---|---|
| SSOT 形态 | (a) YAML 配置 (b) 新表 `business_object` (c) ontology_class 扩列 | (b) 新表 | 表形式更易支持 CRUD + 审计 + 运行时查询 |
| `code` 命名 | (a) `SUPP/MATL` 对齐采购域.md (b) 沿用 `SUPPLIER/MATERIAL` | (b) 沿用 | 避免 45+ 处已存行迁移 |
| Neo4j label | (a) 手写 dict (b) DB 派生 `class_name` | (b) DB 派生 | SSOT 唯一性；启动期一次性读 |
| 身份键 | (a) BIGINT 代理键 (b) VARCHAR 业务码 | (b) VARCHAR | 业务码更稳定，避免 hash 冲突 |
| IQC 本体类 | (a) 建类 (b) 不建 | (a) 建 | 结构性 0 行，spec §3.2 |
| NCR 本体类 | (a) 建类 (b) 不建 | (b) 不建 | user 拍板（spec §3.2） |
| `EntityType` 枚举 | (a) 保留 (b) 删 + Literal 类型 | (b) Literal | 类型安全 + 编译时验证 |

### 多视角

- **数据层**：FK 约束 + CHECK 约束 + 业务键回填 = 数据完整性
- **服务层**：Pydantic literal validation + 服务层守卫 + DB FK = 防御纵深
- **前端层**：CRUD 页 + i18n + 路由 + 菜单 = 用户自助管理
- **图谱层**：whitelist + 启动期 DB 派生 + rename script = 渐近迁移

## 3. 数据模型变更（4 个迁移）

### Alembic chain

```
0037_agent_tool_config (base)
  └─ 0038_business_object (Task 1)
       └─ 0039_incoming_inspection_class (Task 2)
            └─ 0040_supplier_item_po_receipt (Task 2.5 inserted — 4 ontology classes)
                 └─ 0041_entity_type_fk (Task 8 — 3-table FK)
                      └─ 0042_doc_rel_key_varchar (Task 9 — entity_key VARCHAR)
```

### 迁移详情

**0038_business_object**（Task 1, commit 3cd8ef7）：
- 新表 `business_object` (code VARCHAR(20) PK, name, header_class_id FK→ontology_class.id RESTRICT, graph_label, description, created_by, created_time, updated_time)
- CHECK `code = UPPER(code)`
- INDEX `ix_business_object_header_class(header_class_id)`

**0039_incoming_inspection_class**（Task 2, commit 5083473）：
- INSERT ontology_class IncomingInspection (source_table=DWD_INCOMING_INSPECTION, object_type=Transaction, version=1)
- `created_time = now(), updated_time = now()` (brief bug fix)

**0040_supplier_item_po_receipt**（Task 2.5 inserted, commit d6b763c）：
- INSERT 4 ontology classes (Supplier/ItemMaster/PurchaseOrder/Receipt)
- Revision ID shortened from 46→31 chars for varchar(32) limit

**0041_entity_type_fk**（Task 8, commit fab7d5a）：
- 三表 `entity_type` FK → `business_object.code`，ondelete="RESTRICT"
- 防御性 DO$$ 预检：所有现有值必须在 business_object.code 白名单内

**0042_doc_rel_key_varchar**（Task 9, commit 4243dfb）：
- 新增临时列 `entity_key_new VARCHAR(100)`
- UPDATE JOIN：经 `entity_mapping(entity_type, enterprise_key → enterprise_code)` 回填
- DELETE 孤儿行（JOIN 未匹配的）
- DROP COLUMN + RENAME + SET NOT NULL
- CHECK `length(entity_key) > 0`
- 重建索引 `ix_doc_rel_entity`
- 幂等：`ADD COLUMN IF NOT EXISTS` + `DROP CONSTRAINT IF EXISTS`

## 4. 接口契约变更

### 后端 API

**新增 5 个端点**（Task 6, commit be6ef27）：

```
GET    /api/v1/business-objects          → list
GET    /api/v1/business-objects/{code}    → get
POST   /api/v1/business-objects          → create (409 重复, 422 校验)
PUT    /api/v1/business-objects/{code}    → update (404, 422)
DELETE /api/v1/business-objects/{code}    → delete (404, 409 引用)
```

**修改端点**（Task 7 merged, commits d693c67 + 0486163）：
- `entity_mapping.py`: query param `EntityType` → `BusinessObjectCode`
- `documents.py`: query param `EntityType` → `BusinessObjectCode`, `entity_key: int → str`
- `features.py`: 同步 (无文件改动，因已兼容)

### 类型契约

- 删除：`EntityType` Python 枚举（Task 4, commit f2a9b1c）
- 新增：`BusinessObjectCode = Literal["SUPPLIER","MATERIAL","PO","GR","IQC","NCR"]`
- 新增 DTOs：`BusinessObjectCreate`、`BusinessObjectUpdate`、`BusinessObjectRead`（CamelModel）

### Neo4j CQL 契约

- `BUSINESS_ENTITY_LABELS` 收紧为 `{Supplier, ItemMaster, PurchaseOrder, Receipt, IncomingInspection, Contract}`（5 类 + Contract）
- `BUSINESS_RELATION_TYPES` 移除 `GENERATED`（fix dispatch 16ac80e）
- 启动期 `_loadLabelMap(session)` 一次性读 `business_object.graph_label WHERE IS NOT NULL` 入内存 dict
- Neo4j 节点 key 从 `str(m.enterprise_key)` → `m.enterprise_code`

## 5. 实现要点（文件清单）

### 后端新增（13 个文件）

| 文件 | 责任 | Task |
|---|---|---|
| `alembic/versions/0038_business_object.py` | 新建 `business_object` 表 | 1 |
| `alembic/versions/0039_incoming_inspection_class.py` | INSERT IncomingInspection | 2 |
| `alembic/versions/0040_supplier_item_po_receipt.py` | INSERT 4 ontology classes | 2.5 |
| `alembic/versions/0041_entity_type_fk.py` | 3-table FK | 8 |
| `alembic/versions/0042_doc_rel_key_varchar.py` | entity_key VARCHAR migration | 9 |
| `scripts/seed_business_objects.py` | 6-row idempotent seed | 3 |
| `scripts/rename_neo4j_labels.py` | one-shot Neo4j rename | 13 |
| `services/business_object_service.py` | CRUD + guards | 5 |
| `api/v1/business_object.py` | 5 REST endpoints | 6 |
| `tests/unit/test_business_object_schemas.py` | Literal + DTO unit tests | 4 |
| `tests/integration/test_business_object_api.py` | CRUD integration tests | 6 |
| `tests/integration/test_seed_business_objects.py` | Seed idempotency tests | 3 |
| `tests/integration/test_business_object_entity_type_fk.py` | FK guard tests | 8 |
| `tests/integration/test_doc_rel_entity_key_migration.py` | VARCHAR migration tests | 9 |
| `tests/unit/test_rename_neo4j_labels.py` | Neo4j script unit tests | 13 |

### 后端改动（12+ 个文件）

| 文件 | 改动 | Task |
|---|---|---|
| `app/domain/enums.py` | 删 EntityType + 加 BusinessObjectCode Literal | 4 |
| `app/domain/models.py` | 3 entity_type columns 加 FK + entity_key VARCHAR + EntityMapping.__repr__ | 4, 8, 9, 14 |
| `app/domain/schemas.py` | BusinessObjectCreate/Update/Read + 9 处 EntityType→BusinessObjectCode + DocEntityRelationRead entity_type | 4, 14 |
| `app/domain/exceptions.py` | BusinessObjectGraphLabelMismatchError(ValidationError) | 4 |
| `app/services/messages_zh.py` | 4 MSG_BUSINESS_OBJECT_* | 4 |
| `app/services/business_object_service.py` | `session.add()` → `session.merge()` (Task 6 fix) | 6 |
| `app/services/entity_mapping_service.py` | EntityType → BusinessObjectCode | 7 |
| `app/services/supplier_360_service.py` | EntityType.SUPPLIER → "SUPPLIER" (10×) | 7 |
| `app/services/supplier_name_resolver.py` | 同 (2×) | 7 |
| `app/services/graph_relation_service.py` | 删 ENTITY_TYPE_LABELS + 加 _loadLabelMap + sheet16 edges | 10 |
| `app/services/document_service.py` | EntityType → BusinessObjectCode + entity_key: int→str | 7, 9 |
| `app/infrastructure/neo4j_client.py` | BUSINESS_ENTITY_LABELS 收紧 + 删 GENERATED | 10, 14 |
| `app/main.py` | 挂载 business_object router + status mapping | 6 |
| `app/tests/_testapp.py` | 测试 app 同步挂载 | 6 |
| `app/tests/conftest.py` | warmAgentCaches autouse 加 seedBusinessObjects() | 14 |
| 20+ test files | EntityType → string literal | 7.5 |

### 前端新增（3 个文件）

| 文件 | 责任 | Task |
|---|---|---|
| `frontend/src/types/businessObject.ts` | Literal union + DTOs | 11 |
| `frontend/src/api/businessObject.ts` | 5 functions | 11 |
| `frontend/src/pages/BusinessObjectPage.tsx` | CRUD page | 12 |
| `frontend/src/tests/businessObjectApi.test.ts` | API unit tests | 11 |
| `frontend/src/tests/BusinessObjectPage.test.tsx` | Page unit tests | 12 |

### 前端改动（4 个文件）

| 文件 | 改动 | Task |
|---|---|---|
| `frontend/src/App.tsx` | 新增 `/business-objects` 路由 | 12 |
| `frontend/src/components/common/fallbackNav.ts` | 新增 businessObjects 菜单项 | 12 |
| `frontend/src/i18n/zh-CN.ts` | 新增 `businessObject` 命名空间 + `appLayout.menu.businessObjects` | 12 |
| `frontend/src/i18n/en-US.ts` | 同 | 12 |

### 脚本改动（4 个文件，fix dispatch）

| 文件 | 改动 |
|---|---|
| `scripts/seed_entity_mapping.py` | EntityType → string literal |
| `scripts/seed_features.py` | 同 |
| `scripts/sync_entity_mapping_from_thbi.py` | 同 |
| `scripts/seed_graph_relations.py` | 同 |

## 6. 测试（76+ 用例 + 覆盖率）

### 后端测试

| 测试文件 | 用例数 | 结果 |
|---|---|---|
| `test_business_object_schemas.py` | 9 | PASS |
| `test_business_object_api.py` | 7 | PASS |
| `test_seed_business_objects.py` | 4 | PASS |
| `test_business_object_entity_type_fk.py` | 4 | PASS |
| `test_doc_rel_entity_key_migration.py` | 4 | PASS |
| `test_entity_mapping_api.py` | 15 | PASS（fix 后） |
| `test_feature_definition_api.py` | 19 | PASS（fix 后） |
| `test_document_catalog_api.py` | 12 | PASS（fix 后） |
| `test_graph_relation_service.py` | 16 | PASS |
| `test_agent_tool_layer_contract.py` | 3 | PASS |
| `test_rename_neo4j_labels.py` | 6 | PASS |
| `test_sync_entity_mapping_from_thbi.py` | 15 | PASS（fix 后） |
| `test_graph_relations_integration.py` | 3 PASS / 4 FAIL | pre-existing data assertion |

**后端回归**：2250+ tests passing / 4 pre-existing failures (test_graph_relations_integration 数据断言不匹配实际 seed 行数 — root cause: commit 073b535 "feat(supplier)" 把 synthetic 45 rows 改成 real 20 rows，test 断言未跟上)

**覆盖率**：≥ 80% (基线 93.21%，新代码 100% 行覆盖)

### 前端测试

| 测试文件 | 用例数 | 结果 |
|---|---|---|
| `businessObjectApi.test.ts` | 5 | PASS |
| `BusinessObjectPage.test.tsx` | 2 | PASS |

**前端回归**：457 passing / 1 pre-existing error in `AgentRegistryPage.test.tsx`（unrelated unhandled rejection in jsdom）
**tsc --noEmit**：0 errors

## 7. 安全审查（双 agent APPROVED）

### Security-reviewer verdict: APPROVE_MERGE

| Check | Status |
|---|---|
| No hardcoded secrets/credentials | PASS |
| SQL injection prevention | PASS（all parameterized queries） |
| XSS prevention | PASS（React default escaping） |
| Path traversal | PASS |
| CSRF protection | PASS（FastAPI default） |
| Auth/authz on new endpoints | PASS（all 5 endpoints require auth） |
| Input validation | PASS（BusinessObjectCode Literal + max_length） |
| FK constraints enforced | PASS（ondelete="RESTRICT"） |
| Delete reference checks | PASS（service-level ConflictError before DB） |
| CQL injection prevention | PASS（labels from controlled constants） |
| Migration safety | PASS（IF NOT EXISTS + DO$$ pre-check） |

### Code-reviewer verdict: APPROVED after fix rounds

- Round 1 (reviewer 1): NEEDS_FIXES (1 CRITICAL + 3 IMPORTANT + 2 MINOR)
- Fix dispatch: 9 commits addressed all
- Round 2 (re-reviewer): NEEDS_FIXES (1 residual unit test EntityType reference)
- Final fix: 1 commit addressed
- **Final**: APPROVED

## 8. 部署验证

### Migration apply 顺序

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  uv run alembic upgrade head
# 0037 → 0038 → 0039 → 0040 → 0041 → 0042
```

### Seed 顺序

```bash
DATABASE_URL=... uv run python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from scripts.seed_business_objects import seedBusinessObjects

async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.begin() as conn:
        await seedBusinessObjects(conn)
    await engine.dispose()

asyncio.run(main())
"
```

### Neo4j rename（手动 one-shot）

```bash
# 仅在生产 Neo4j 有旧 label 时执行
cd backend
uv run python scripts/rename_neo4j_labels.py
# 输出: {Material: 0, GoodsReceipt: 0, NCR_deleted: 0}  # 重跑后
# 或: {Material: 10, GoodsReceipt: 5, NCR_deleted: 3}  # 首次运行
```

## 9. 真实数据验证（PG 5433 + Neo4j）

### PG 5433 (qa_metadata_test)

```sql
-- 验证 business_object 表
SELECT code, name, header_class_id, graph_label FROM business_object ORDER BY code;
-- Expected 6 rows: SUPPLIER, MATERIAL, PO, GR, IQC, NCR
-- NCR 的 graph_label 是 NULL

-- 验证 FK
SELECT conrelid::regclass, conname FROM pg_constraint
WHERE contype = 'f'
  AND pg_get_constraintdef(oid) LIKE '%business_object%';
-- Expected: entity_mapping_fk_entity_type, feature_definition_fk_entity_type, document_entity_relation_fk_entity_type

-- 验证 entity_key VARCHAR
SELECT data_type, character_maximum_length
FROM information_schema.columns
WHERE table_name = 'document_entity_relation' AND column_name = 'entity_key';
-- Expected: character varying, 100

-- 验证 ontology_class 完整性
SELECT count(*) FROM ontology_class WHERE class_name IN
  ('Supplier', 'ItemMaster', 'PurchaseOrder', 'Receipt', 'IncomingInspection');
-- Expected: 5
```

### Neo4j 验证

```cypher
// 验证新 labels
MATCH (n) WHERE any(l IN labels(n) WHERE l IN ['Supplier', 'ItemMaster', 'PurchaseOrder', 'Receipt', 'IncomingInspection'])
RETURN labels(n), count(*);

// 验证无旧 labels
MATCH (n:Material) RETURN count(n);  // Expected: 0
MATCH (n:GoodsReceipt) RETURN count(n);  // Expected: 0
MATCH (n:NCR) RETURN count(n);  // Expected: 0
```

## 10. 关联

- **Spec**：`docs/superpowers/specs/2026-09-04-business-object-registry-design.md`
- **实施计划**：`docs/superpowers/plans/2026-09-04-business-object-registry.md`
- **SDD ledger**：`.superpowers/sdd/2026-09-04-business-object-registry/progress.md`
- **任务 reports**：`.superpowers/sdd/2026-09-04-business-object-registry/task-{1..14}-report.md`
- **Review packages**：`.superpowers/sdd/2026-09-04-business-object-registry/review-*.diff`
- **范式**：`Harness/changes/feat-kpi-catalog-governance/summary.md`（CRUD 注册表 + 审计 outbox）
- **前置**：`Harness/changes/feat-entity-mapping-model/summary.md`、`feat-feature-store-api/summary.md`、`feat-document-catalog-model/summary.md`
- **规则**：`Harness/rules/开发流程规范.md`

## 已知问题（post-merge follow-up）

1. **test_graph_relations_integration.py 4 个 pre-existing failures**：test 数据断言 45 rows vs 实际 20 rows。Root cause: commit 073b535 "feat(supplier)" 改了 seed 但没改 test。**Track**：Harness ticket / TODO。

2. **`AgentRegistryPage.test.tsx` 1 个 pre-existing error**：jsdom unhandled rejection in form submit。**Track**：Harness issue。