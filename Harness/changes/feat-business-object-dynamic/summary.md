# feat-business-object-dynamic

> 日期：2026-09-04 | 状态：done | Spec: Harness/changes/feat-business-object-dynamic/summary.md
> Plan: Harness/changes/feat-business-object-dynamic/summary.md

## 目标

把硬编码 `BusinessObjectCode` Literal 改为 DB 驱动动态校验。6 个 seeded codes
（SUPPLIER / MATERIAL / PO / GR / IQC / NCR）存在 `business_object` 表；
新增 code 通过 admin UI / API 立即生效，无需发版。

## 实现

### Registry 模块
- `backend/app/business_object_registry.py`（新）：模块级单例
  - `warmUp(session)`：启动预热，全量加载
  - `reloadOne(session, code)`：写时失效（service create/update/delete 末尾调用）
  - `isValid(code) -> bool`：BeforeValidator 校验
  - `getAll() -> list[str]`：所有有效 code
  - `invalidate()`：清空缓存
  - `asyncio.Lock` 防并发 reload 竞态；pre-warmUp 调用抛 `RuntimeError`

### Pydantic 类型策略（双模式）

| 类型 | 用途 | 校验方式 |
|------|------|----------|
| `BusinessObjectCodeType` | 引用已有 code（Path/Query/related entity_type 字段） | `BeforeValidator(registry.isValid)` |
| `BusinessObjectCodeNew` | create payload 新 code（仅 `BusinessObjectCreate.code`） | `BeforeValidator(_validateBusinessObjectCodeFormat)`，format-only |

- `BusinessObjectCodeType = Annotated[str, BeforeValidator(_validateBusinessObjectCode)]`
  — 12 处引用点（Path/Query 参数、EntityMapping/FeatureDefinition/DocumentEntityRelation.entity_type）
- `BusinessObjectCodeNew = Annotated[str, BeforeValidator(_validateBusinessObjectCodeNew)]`
  — 仅 `BusinessObjectCreate.code`；format: uppercase + alphanumeric + underscore + max 20 chars
- `enums.py` 的 `BusinessObjectCode` Literal 加 `DEPRECATED` 注释（保留供静态类型检查）

### DTO 迁移（13 schema 字段 + 6 API Path/Query）
- `BusinessObjectCreate.code` → `BusinessObjectCodeNew`
- `BusinessObjectUpdate.code` → `BusinessObjectCodeNew`（可选）
- `BusinessObjectRead.code` → `BusinessObjectCodeType`
- `EntityMappingCreate.entity_type` → `BusinessObjectCodeType`
- `EntityMappingUpdate.entity_type` → `BusinessObjectCodeType | None`
- `EntityMappingRead.entity_type` → `BusinessObjectCodeType`
- `FeatureDefinitionCreate.entity_type` → `BusinessObjectCodeType`
- `FeatureDefinitionUpdate.entity_type` → `BusinessObjectCodeType | None`
- `FeatureDefinitionRead.entity_type` → `BusinessObjectCodeType`
- `DocumentEntityRelationCreate.entity_type` → `BusinessObjectCodeType`
- `DocumentEntityRelationUpdate.entity_type` → `BusinessObjectCodeType | None`
- `DocumentEntityRelationRead.entity_type` → `BusinessObjectCodeType`
- `FeatureRuleCreate.entity_type` → `BusinessObjectCodeType`
- `FeatureRuleUpdate.entity_type` → `BusinessObjectCodeType | None`
- `FeatureRuleRead.entity_type` → `BusinessObjectCodeType`
- API Path/Query：`/business-objects/{code}` / `/entity-mappings?entity_type=` / `/documents?entity_type=` 等 6 处

### Service 集成
- `business_object_service.py`：create/update/delete 末尾调
  `businessObjectRegistry.reloadOne(session, code)`
- `entity_mapping_service.py` + `document_service.py`：参数类型从
  `BusinessObjectCode | None` 改为 `str | None`（解除对 Literal 的隐式依赖）

### Lifespan
- `main.py` lifespan 新增 `businessObjectRegistry.warmUp(session)`

### 测试 conftest
- `tests/integration/conftest.py`：autouse warmUp fixture
- `tests/unit/conftest.py`：autouse warmUp fixture（commit af79573 修复）

### 前端
- `types/businessObject.ts`：删除 `BUSINESS_OBJECT_OPTIONS` 硬编码；
  类型从 `Literal['SUPPLIER' | ...]` 改为 `string`
- `types/entityMapping.ts` + `types/feature.ts`：删除硬编码副本
- `pages/BusinessObjectPage.tsx`：`code` Select 改用 `listBusinessObjects()` API 拉取

## 验证

### 集成测试（真实 PG，TRUNCATE 隔离）
- `test_business_object_registry.py`：6 用例（warmUp/reload_one/get_all/
  is_valid/raises RuntimeError pre-warmUp）

### 后端覆盖率
- `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test uv run pytest app/tests/ --cov=app --cov-fail-under=80`
- 92.38% coverage；27 pre-existing failures 与本变更无关

### 前端
- `cd frontend && npx vitest run src/tests/BusinessObjectPage.test.tsx ...`
- tsc clean；26/26 targeted tests pass

## 关键设计决策

- **create payload paradox**：`BusinessObjectCodeNew` 不调 registry，只校验 format。
  因为新 code 不在 registry 中——registry 在 service create 末尾才 reloadOne
- **path/query param 语义**：引用不存在的 code → 404 + "不存在"（不是 422），
  语义与 Resource not found 一致
- **双重 BeforeValidator**：同一 field（`entity_type`）在 Read 用 BusinessObjectCodeType
 （registry 校验），在 Update/Create 用 BusinessObjectCodeNew（format-only）
- **保留 Literal 枚举**：`enums.py` 的 `BusinessObjectCode` 不删除，仅加 DEPRECATED
  注释；静态类型检查仍可用，运行时由 BeforeValidator 覆盖

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| R1 unit tests 未 warmUp | af79573 在 unit conftest 加 autouse warmUp |
| R2 create payload paradox | 78bd7f0 引入 BusinessObjectCodeNew（format-only） |
| R3 path/query param 语义 | 404 for non-existent code + clear "不存在" message |

## 关联变更

- 前置：Base model + CRUD service 已存在（business_object 表已建）
- 关联：`[[qa-system-agent-tool-binding]]` / `[[qa-system-agent-tool-config-db]]` /
  `[[qa-system-feature-rule-config]]` — 同一 registry 模式
- 前端：`FEATURE_ENTITY_TYPES` in `feature.ts` pending deprecation
  （FeatureCatalogPage still uses it; no /entity-types API yet）

## Out of Scope

- `FEATURE_ENTITY_TYPES` 前端 deprecation（FeatureCatalogPage 仍在用，尚无 /entity-types API）
- 27 个 pre-existing unit test failures（FeatureRuleRegistry needs warmUp in unit conftest；
  DocEntityRelationService int/str entity_key type bug）
