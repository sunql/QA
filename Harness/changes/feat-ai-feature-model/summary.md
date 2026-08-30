# 变更：AI Feature Layer 定义 + 计算 + 存储（FeatureDefinition / FeatureValue）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 4（L4 AI Ready — Feature Layer；本 change 属 Phase 4.3）
- **状态**：done（实现 + 单测/集成测试全绿 + code-review/security-review 已复核修复）

## 1. 需求

建立 AI Feature Layer 的**数据层**（`feature_definition` + `feature_value` 双表 + 计算作业），把「LLM 每次实时聚合计算」转为「预计算特征、可复用、可追溯」。

**业务背景**：满足 AI-Ready 数据标准体系 §22-§23、采购域 Sheet 19、采购域 §十三（Supplier 360° 的前置）。当前 NL2SQL 对「供应商 3 月 OTD」类问题每次都实时 JOIN DWS/ADS 计算，慢且不可复用；Feature Layer 把 OTD/不良率/价格偏差等预计算成特征值，供 Phase 4.4 在线查询 API + Phase 5 Supplier 360° 消费。

**范围**：本 change 只做**定义 + 计算 + 存储**（数据层）。Phase 4.4（`feat-feature-store-api`）另做在线查询 API + NL2SQL prompt 注入（系统集成层），保持两个 change 边界清晰。

**验收标准**：
1. `feature_definition` + `feature_value` 双表落库（Alembic 0027），迁移在真实 PG 5433 应用成功
2. `FeatureDefinitionService` CRUD + REST 端点全通
3. `FeatureComputeService` 能按 `calculation_logic` 读业务库（只读护栏）→ 解析 → upsert 到 `feature_value`
4. `scripts/compute_features.py` 手动触发，幂等可重跑
5. `scripts/seed_features.py` 灌 5 条核心特征定义（OTD/不良率/价格偏差/风险/缺料）
6. 覆盖率 ≥ 80%，集成测试走真实 PG + 完整 API 链路

## 2. 设计评审

- **双表 vs 单表**：`feature_definition`（定义，一行一特征）+ `feature_value`（值，一行 = 特征 × 实体 × 有效期）。定义与值分离，可单独治理定义、批量重算值，符合标准体系 §22。
- **与 KPI 的关系**：`feature_definition` 不直接 FK 到 `kpi_catalog` / `ontology_metric`——Feature 是「预计算快照」，KPI 是「指标口径定义」，两者粒度不同（Feature 有 entity_key + valid_at，KPI 是全局口径）。如需关联留 `feature_alias` 自由文本标注，避免硬耦合（YAGNI，Phase 5 再按需补 FK）。
- **`entity_key` 用 VARCHAR(100) 而非 BIGINT**（**偏离计划**）：实际业务键是字符串（供应商编码 `Q630`/`B019`、物料编码 `RM-STEEL-001`），非代理键。用 VARCHAR 承载业务编码，避免引入代理键映射的额外复杂度。
- **`value` 双列 vs JSONB**：核心 5 特征全为数值 → `value DECIMAL(38,10)` + 可选 `value_text VARCHAR(500)` 逃生口；JSONB 暂不引入（YAGNI，未来非标特征再补）。
- **计算作业读业务库**：`FeatureComputeService.compute(feature, adapter)` 接受**注入的 adapter**（沿用 `business_db_pool.get_adapter`），`calculation_logic` 必须通过 `_assert_read_only` 只读校验（创建时 + 计算时双重校验），复用 SQL Guard 护栏。
- **幂等 upsert**：`unique (feature_id, entity_key, valid_at)` + `INSERT ... ON CONFLICT DO UPDATE`，重算同窗口覆盖旧值，不产生重复行。
- **status 状态机**：`FeatureStatus(str, Enum)` 三态 `DRAFT / ACTIVE / DEPRECATED`（与 KpiStatus 对齐）；计算只处理 `is_enabled=true` 且 `status=ACTIVE` 的定义。
- **API 路径**：`/api/v1/features`，挂 `app/main.py`（与 `kpi_catalog` / `entity_mapping` 同模式）。

## 3. 数据模型变更

新增 2 张表（Alembic `0027_ai_feature.py`，down_revision=`0026_entity_mapping_owner`）：

**`feature_definition`**：
```
id BIGINT PK,
feature_name VARCHAR(100) UNIQUE NOT NULL,    -- 如 SUPPLIER_OTD_3M
feature_alias VARCHAR(200),                    -- 中文别名，如 供应商3月准时交付率
feature_definition TEXT,                       -- 业务定义/描述
entity_type VARCHAR(20) NOT NULL,              -- SUPPLIER / MATERIAL / PO
calculation_logic TEXT NOT NULL,               -- 计算 SQL（读业务库的单条 SELECT）
window_size VARCHAR(20),                       -- 3M / YTD / 12M
refresh_frequency VARCHAR(20),                 -- DAILY / WEEKLY / MONTHLY
unit VARCHAR(50),                              -- % / 天 / 元
owner VARCHAR(100),
version VARCHAR(20) NOT NULL DEFAULT 'v1.0',
status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',   -- DRAFT/ACTIVE/DEPRECATED
is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
datasource_id BIGINT FK → data_source.id NOT NULL,  -- 计算读哪个业务库
created_by VARCHAR(50),
created_time, updated_time
```

**`feature_value`**：
```
id BIGINT PK,
feature_id BIGINT FK → feature_definition.id NOT NULL,
entity_key VARCHAR(100) NOT NULL,              -- 实体业务编码（供应商/物料编码）
value DECIMAL(38,10) NULLABLE,                 -- 数值型特征值
value_text VARCHAR(500) NULLABLE,              -- 文本型特征值（逃生口）
valid_at DATE NOT NULL,                        -- 特征有效期（月末/快照日）
computed_at TIMESTAMP NOT NULL,                -- 计算时间
unique (feature_id, entity_key, valid_at)
```

**CheckConstraint**：`feature_definition.status IN ('DRAFT','ACTIVE','DEPRECATED')`；`feature_value` 至少 `value` 或 `value_text` 一者非空。
**索引**：`uq_feature_definition_name`（unique）、`ix_feature_definition_status`、`ix_feature_value_feature_id`、`uq_feature_value_dedup`（feature_id, entity_key, valid_at unique）。

## 4. 接口契约变更

| Method | Path | 用途 | 鉴权 |
|---|---|---|---|
| GET | `/api/v1/features` | 定义列表（按 feature_name 升序） | ✅ |
| GET | `/api/v1/features/{id}` | 定义详情 | ✅ |
| POST | `/api/v1/features` | 创建；重复 feature_name 返回 409；calculation_logic 非只读返回 400 | ✅ |
| PUT | `/api/v1/features/{id}` | 更新 | ✅ |
| DELETE | `/api/v1/features/{id}` | 删除（级联删 feature_value） | ✅ |
| POST | `/api/v1/features/{id}/compute` | 触发单特征计算 | ✅ |
| POST | `/api/v1/features/compute-batch` | 触发所有 enabled 特征计算 | ✅ |
| GET | `/api/v1/features/{id}/values` | 特征值列表（分页，验证用） | ✅ |

DTO（snake_case 字段 + `CamelModel`）：
- `FeatureDefinitionCreate` / `FeatureDefinitionUpdate` / `FeatureDefinitionRead`
- `FeatureValueRead`（feature_id / entity_key / value / valueText / validAt / computedAt）
- `FeatureStatus` enum：`DRAFT / ACTIVE / DEPRECATED`
- 所有 Text 字段加 `max_length`（feature_definition/calculation_logic ≤ 8000）

## 5. 实现要点

**后端新增**：
- `app/domain/models.py`：`FeatureDefinition` + `FeatureValue` ORM
- `app/domain/enums.py`：`FeatureStatus(str, Enum)`、`FeatureEntityType`、`FeatureRefreshFrequency`
- `app/domain/schemas.py`：DTO + `max_length` 上限
- `app/domain/exceptions.py`：复用 `NotFoundError` / `ConflictError` / `SqlSafetyError`
- `app/services/feature_definition_service.py`：CRUD（`listFeatures / getFeature / createFeature / updateFeature / deleteFeature`；`IntegrityError → ConflictError`；创建/更新时校验 `calculation_logic` 只读）；`owner = actor.departments[0]`、`created_by = actor.userId` 服务端派生，update/delete 走 `AclService.assertCanModify`
- `app/services/feature_compute_service.py`：`computeFeature(feature, adapter)` —— `execute_read_only(calculation_logic)`（try/except → `DataSourceError`）→ 行数上限 10000 → 解析行（列宽校验）→ upsert `feature_value`；`computeAllEnabled()`
- `app/api/v1/features.py`：8 端点 + `Depends(getCurrentUser)`
- `app/main.py`：挂载 `features.router`
- `scripts/compute_features.py`：手动触发入口（`computeAllEnabled`）
- `scripts/seed_features.py`：5 条核心特征定义（幂等）

**5 条核心特征（seed）**：
| feature_name | entity_type | window | calculation_logic 数据源 |
|---|---|---|---|
| SUPPLIER_OTD_3M | SUPPLIER | 3M | DWS_SUPPLIER_DELIVERY_MONTHLY 近 3 月 OTD 均值 |
| SUPPLIER_DEFECT_RATE_3M | SUPPLIER | 3M | DWS 近 3 月 reject_rate 均值 |
| SUPPLIER_PRICE_VARIANCE_3M | SUPPLIER | 3M | DWS 价格偏差率 |
| SUPPLIER_RISK_SCORE | SUPPLIER | 12M | OTD/不良率/价格偏差加权合成 |
| MATERIAL_SHORTAGE_RISK | MATERIAL | 12M | DWS 物料缺货风险 |

**复用既有模式**：ORM（`BigIntPk`/`BigIntFk` + `TimestampMixin`）、路由（`APIRouter` + `getDb` + `Depends(getCurrentUser)`）、只读护栏（`business_db_pool._assert_read_only`）、异常三态（404/409/400）。

## 6. 测试

**单测**：
- `test_feature_definition_schemas.py`：enum 三态 + DTO 字段校验 + max_length + status 非法值拒绝 + `owner`/`created_by` 不在 Create DTO + `datasource_id ≤ 0` 拒绝
- `test_feature_compute_service.py`：注入 fake adapter → 断言 parse 行 → upsert `feature_value`；空结果集；value/value_text 双空拒写；calculation_logic 含写操作被 `SqlSafetyError` 拒；**执行失败转 `DataSourceError`；>10000 行超限拒写；超长 entity_key/value_text 拒写**
- `test_datasource_pool.py`（`TestAssertReadOnly`）：新增 10 例深度扫描用例——`WITH x AS (DELETE...)` / `SELECT INTO` / `INTO OUTFILE` / `FOR UPDATE` / `FOR SHARE` / `nextval()` / `pg_read_file()` 均拒；字符串字面量含 `DELETE FROM`、引号标识符 `"DELETE"` 不误杀

**集成测试（真实 PG 5433 + 完整 API 链路）**：
- `test_feature_definition_api.py`：CRUD + 重复 feature_name 409 + datasource_id 无效 422 + status 非法 422 + 迁移列存在性 + 级联删除
- `test_feature_compute_api.py`：create 定义 → `POST /compute`（fake adapter）→ `GET /values` 断言值落库 + 重算幂等（同窗口覆盖不重复）
- `test_seed_features.py`：seed 幂等

**前端**：`FeatureCatalogPage.test.tsx`（4 例，vitest）：渲染列表/操作列、create 提交 camelCase payload（无 owner/createdBy）、compute 调用。

**覆盖率**：feature 模块 89%（`feature_compute_service` 92% / `feature_definition_service` 88% / `business_db_pool` 88%），104 用例全绿；`npx tsc --noEmit` 干净。

## 7. 安全审查（已完成）

security-reviewer 首轮发现以下问题，已全部修复并补单测：

| 级别 | 问题 | 修复 |
|---|---|---|
| **C1 CRITICAL** | `_assert_read_only` 只看首 token，可被 `WITH x AS (DELETE ...)` / `SELECT ... INTO` / `FOR UPDATE` / `nextval()` 绕过 | 新增 `_assertNoHiddenWrites` 深度扫描：flatten 语句，拦截 `Keyword.DML/DDL` 命中 `_FORBIDDEN_VERBS`、`Keyword INTO`、`Keyword SHARE`、`Name` 命中 `_FORBIDDEN_FUNCTIONS`（nextval/setval/pg_read_file 等）；字面量/注释跳过 |
| **M1** | `FeatureDefinitionCreate` 含 `created_by` 字段，client 可声明 | 从 DTO 移除，服务端由 `actor.userId` 派生 |
| **H2** | 计算路径无行数上限，误配 SQL 可全表倾泻 | `_MAX_FEATURE_VALUE_ROWS = 10000` 上限，超限抛 ValidationError |
| **L2** | `_parseRow` 不校验列宽，超长 `entity_key`/`value_text` 会在 DB 层 DataError | 校验 `entity_key ≤ 100`、`value_text ≤ 500`，超限抛 ValidationError |
| **L4** | `datasource_id` 无 `gt=0`，0/负数可穿透 | Create/Update DTO 加 `gt=0` |
| **MEDIUM（code-review）** | `adapter.execute_read_only` 异常未包裹，底层驱动异常外泄 | try/except → log WARN + 转 `DataSourceError`（不泄漏连接细节） |

**其余安全要点**：
- **calculation_logic SQL 注入**：创建/更新时 `_assert_read_only` 校验 + 计算时 `execute_read_only` 二次校验（现含深度扫描），双重护栏。
- **datasource_id 越权**：FK 约束 + service 校验存在性；不暴露密码（复用 DataSource 读 DTO 不回密文）。
- **owner-based ACL**：`owner = actor.departments[0]` 派生，update/delete 走 `AclService.assertCanModify`（非 owner + 非 admin → 403）。

**遗留（跨 change，非本 change 范围）**：H1「stub auth fail-open」——测试/开发环境的 `getCurrentUser` stub 允许未鉴权调用，属 Phase 4.5 governance hardening 的跨切面议题，随 ACL 扩展与 audit-history-api 一并处理。

## 8. 部署验证（计划）

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  .venv/bin/alembic upgrade head   # 期望 0027_ai_feature applied
curl /openapi.json | jq '.paths | keys' | grep features  # 确认路由存在
curl -X POST localhost:8000/api/v1/features -H 'Content-Type: application/json' \
  -d '{"featureName":"SUPPLIER_OTD_3M","entityType":"SUPPLIER","calculationLogic":"SELECT ...","datasourceId":4}'
# 期望 201
```

## 9. 真实数据验证（Harness 门禁，计划）

- 用 THBI（datasource id=4）真实跑 5 条核心特征的计算 SQL，断言 `feature_value` 落库且值合理（如 SUPPLIER_OTD_3M 落在 [0,1]）
- 贴关键 SQL + 落库行数 + 首 5 行值到本 SSOT §9
- 若真实计算暴露方言/命名差异（如 Oracle 大写列、日期函数），修复 + 补单测

## 10. 决策与遗留

**已定决策**：
1. 4.3（数据层）与 4.4（在线查询 + NL2SQL 注入）拆两个 change
2. `entity_key` 用 VARCHAR 业务编码（偏离计划的 BIGINT 代理键）
3. 核心 5 特征全数值 → `value DECIMAL(38,10)` + `value_text` 逃生口，不引 JSONB
4. 计算作业 adapter 注入，双重重校验只读
5. 幂等 upsert（unique + ON CONFLICT）
6. SQL 只读护栏深度扫描（`_assertNoHiddenWrites`），拦截数据修改 CTE / SELECT INTO / FOR UPDATE / 危险函数（C1）
7. `created_by` 服务端派生（`actor.userId`），不入 Create DTO（M1）
8. 计算路径行数上限 10000 + 列宽校验 + 执行异常转 `DataSourceError`（H2 / L2 / MEDIUM）

**遗留（后续 change）**：
- Phase 4.4 `feat-feature-store-api`：在线查询 API + NL2SQL prompt 注入
- Feature 与 KPI Catalog 关联 FK（Phase 5 按需）
- 调度器（一次性脚本 + 手动触发，符合「暂不做调度」决策）
- 特征版本快照（当前只覆盖当前值，无历史）
- H1「stub auth fail-open」：跨切面，随 Phase 4.5 ACL 扩展 / audit-history-api 一并处理（见 §7）
