# 变更：KPI 业务目录治理（KpiCatalog）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 4（L4 AI Ready — 语义层扩展 + Feature Layer；本 change 属 Phase 4.1）
- **状态**：done

## 1. 需求

建立独立 `kpi_catalog` 表（与 `ontology_metric` 并存），承载 KPI 的**业务视角治理元数据**（VERSION / OWNER / UNIT / GRAIN / NUMERATOR / DENOMINATOR / STATUS），让治理方能维护指标语义、追踪版本与责任人。技术形态指标保留在 `ontology_metric` 供 NL2SQL 生成 SQL 使用；两表通过 `metric_id` 弱关联，业务 KPI 可先于技术 metric 存在。

**业务背景**：满足 AI-Ready 数据标准体系 §16-§17、采购域 §十三（指标治理 + Supplier 360° 视图前置）。本表是 Phase 4.2 seed、Phase 5 Supplier 360° 视图、Phase 6 Agent Registry 的输入。

## 2. 设计评审

- **两表并存 vs 拆分**：`ontology_metric` 保留为「技术形态」（formula/agg_function/target_class_id），新建 `kpi_catalog` 承载「业务治理」（version/owner/unit/grain/numerator/denominator/status）。两表通过 `kpi_catalog.metric_id` → `ontology_metric.id` 弱关联（nullable FK）。
- **不写 Neo4j / Milvus**：KPI 是治理层，不参与向量检索；保持 best-effort 不波及本体同步链路。
- **版本演进**：`revision_count` 在 service.updateKpi 中无条件 +1（PUT 即视为修订）；status=DRAFT 与 PUBLISHED 均允许更新；DEPRECATED 仅作归档标记，不强制锁。
- **status 状态机**：`KpiStatus(str, Enum)` 三态 `DRAFT / PUBLISHED / DEPRECATED`；service 层通过 `.value` 归一化为字符串写库；显式 `status: null` 被 service 层 ValidationError 拒绝（DB NOT NULL 约束兜底）。
- **重复 kpi_code**：`uq_kpi_catalog_code` 唯一约束；service 在 IntegrityError 时映射为 409 ConflictError。
- **API 路径**：`/api/v1/kpi-catalog`，挂在 `app/main.py`（与 `data_quality` / `entity_mapping` 同模式，避免 v1Router 双重叠加 prefix）。

## 3. 数据模型变更

新增表 `kpi_catalog`（Alembic `0023_kpi_catalog`）：
```
id BIGINT PK,
kpi_code VARCHAR(50) UNIQUE NOT NULL,         -- 业务编码
kpi_name VARCHAR(200) NOT NULL,               -- 业务名称
business_definition TEXT,                     -- 治理视角业务定义
formula TEXT,                                 -- 与 ontology_metric 同源，可独立维护
numerator TEXT,                               -- 分子公式/描述
denominator TEXT,                             -- 分母公式/描述
grain VARCHAR(100),                           -- 粒度（如「供应商+工厂+月」）
unit VARCHAR(50),                             -- 单位（%/天/元）
data_source TEXT,                             -- 数据来源说明
owner VARCHAR(100),                           -- 责任部门/人
version VARCHAR(20) NOT NULL DEFAULT 'v1.0',  -- 治理版本
revision_count INT NOT NULL DEFAULT 0,        -- 修订次数（PUT 自增）
status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',  -- DRAFT/PUBLISHED/DEPRECATED
metric_id BIGINT FK → ontology_metric.id NULLABLE,
created_by VARCHAR(50),
created_time, updated_time
```

**CheckConstraint**：`status IN ('DRAFT','PUBLISHED','DEPRECATED')`。
**索引**：`ix_kpi_catalog_status`、`ix_kpi_catalog_owner`、`uq_kpi_catalog_code`（unique）。

## 4. 接口契约变更

| Method | Path | 用途 | 鉴权 |
|---|---|---|---|
| GET | `/api/v1/kpi-catalog` | 列表（按 kpi_code 升序） | ✅ |
| GET | `/api/v1/kpi-catalog/{id}` | 详情 | ✅ |
| POST | `/api/v1/kpi-catalog` | 创建；重复 kpi_code 返回 409 | ✅ |
| PUT | `/api/v1/kpi-catalog/{id}` | 更新；revision_count +1 | ✅ |
| DELETE | `/api/v1/kpi-catalog/{id}` | 删除 | ✅ |

DTO（snake_case 字段 + `CamelModel`）：
- `KpiCatalogCreate` / `KpiCatalogUpdate` / `KpiCatalogRead`
- `KpiStatus` enum：`DRAFT / PUBLISHED / DEPRECATED`
- 所有 Text 字段加 `max_length` 上限（business_definition/formula ≤ 4000；numerator/denominator ≤ 1000；data_source ≤ 200），防止多 MB 字符串攻击

## 5. 实现要点（已落地）

**后端新增**：
- `app/services/kpi_catalog_service.py`：`KpiCatalogService` 类；`listKpis / getKpi / createKpi / updateKpi / deleteKpi`；`updateKpi` 自增 revision_count；`IntegrityError → ConflictError`；`status: null → ValidationError`
- `app/api/v1/kpi_catalog.py`：5 端点（list/get/create/update/delete）；统一 `_user: CurrentUser = Depends(getCurrentUser)` 鉴权
- `app/tests/unit/test_kpi_catalog_schemas.py`：10 用例覆盖 enum + DTO
- `app/tests/integration/test_kpi_catalog_api.py`：16 用例覆盖完整 API + DB 约束（含 status=null 拒绝 + DB CHECK 约束 + revision_count 自增）
- `alembic/versions/0023_kpi_catalog.py`：迁移（down_revision=`0022_ontology_class_governance`）

**后端改动**：
- `app/domain/enums.py`：新增 `KpiStatus(str, Enum)` 三态
- `app/domain/models.py`：新增 `KpiCatalog` ORM（含 2 列唯一约束 + 3 索引 + CheckConstraint）
- `app/domain/schemas.py`：新增 `KpiCatalogCreate/Update/Read`；所有 Text 字段加 `max_length`
- `app/domain/exceptions.py`：新增 `ConflictError(DomainError)`
- `app/services/messages_zh.py`：新增 `MSG_KPI_CATALOG_NOT_FOUND` / `MSG_KPI_CATALOG_DUPLICATE_CODE` / `MSG_KPI_CATALOG_STATUS_NULL`
- `app/main.py`：挂载 `kpi_catalog.router` 到 `/api/v1/kpi-catalog`；`_statusFor` 新增 `ConflictError → 409`
- `app/tests/_testapp.py`：测试 app 同步挂载 + `ConflictError → 409`

**前端新增**：
- `src/types/kpiCatalog.ts`：`KpiStatus` union + `KPI_STATUS_OPTIONS` + 3 个 DTO
- `src/api/kpiCatalog.ts`：5 个 API 函数（axios）
- `src/pages/KpiCatalogPage.tsx`：表格 + 过滤栏（kpiCode/kpiName/owner/status）+ Modal CRUD 表单；状态 Tag 配色（DRAFT=default/PUBLISHED=green/DEPRECATED=orange）
- `src/tests/KpiCatalogPage.test.tsx`：4 用例覆盖渲染 + 状态 Tag + 新建流

**前端改动**：
- `src/App.tsx`：新增 `path="kpi-catalog"` 路由
- `src/components/common/AppLayout.tsx`：左侧导航加 `/kpi-catalog`
- `src/i18n/zh-CN.ts` + `en-US.ts`：新增 `appLayout.menu.kpiCatalog` + 顶级 `kpiCatalog` 命名空间（columns/labels/placeholders/deleteConfirm/newButton/createTitle/editTitle/title）+ `enums.kpiStatus`（DRAFT/PUBLISHED/DEPRECATED 三态中文/英文）
  - **修正 i18n 结构**：kpiCatalog 块最初误嵌入 `forms.localImport.preview` 下方，经调试 `forms` 顶层未发现该 key，最终定位在 `localImport` 子级。结构修正为顶级键，与 `forms/localImport/datasource/enums` 平级，符合 page 内 `t("kpiCatalog.*")` 引用路径

**复用既有模式**：
- ORM：`TimestampMixin` + `BigIntPk` + `BigIntFk` + UniqueConstraint 命名（沿用 `feat-entity-mapping-model`）
- 路由：APIRouter + `getDb` 依赖 + `Depends(getCurrentUser)`（沿用 `feat-data-quality-rule-model`）
- Pydantic：`CamelModel` + `alias_generator=to_camel` + snake_case 字段名 + Text 字段 `max_length`（沿用 `feat-data-quality-rule-model`）
- 异常：`DomainError` + 子类 `NotFoundError`/`ConflictError`/`ValidationError` 三态 → 404/409/400
- 前端：`httpClient`（axios 拦截器统一信封）+ Antd Table/Modal/Form + `FilterBar` 复用 + `useTranslation`
- 测试：unit 10 + integration 16 覆盖 service+API+DB 约束

## 6. 测试

**后端单测（10 用例，全部 PASS）**：
`app/tests/unit/test_kpi_catalog_schemas.py`：
- `test_has_three_values` enum 三态存在
- `test_value_strings_usable_in_db` enum 值与 DB 字符串一致
- `test_create_required_fields` kpi_code/name 必填
- `test_create_rejects_oversized_code` kpi_code > 50 字符被拒绝
- `test_create_rejects_empty_name` 空字符串被拒绝
- `test_update_partial_fields` 局部更新，未传字段保持
- `test_update_invalid_status_rejected` 非法 status 字符串被 422
- `test_update_clears_field_with_explicit_none` 显式 null 清空字段
- `test_read_dto_carries_all_fields` Read DTO 18 字段齐全
- `test_model_dump_json_by_alias_camelcase` 输出 camelCase JSON

**后端集成测试（16 用例，全部 PASS，真实 PG 5433 + 完整 API 链路）**：
`app/tests/integration/test_kpi_catalog_api.py`：
- `test_table_and_columns_after_migration` 迁移后表/列存在性
- `test_create_kpi_minimal_fields` 最小字段创建
- `test_create_kpi_full_governance_fields` 13 字段全填
- `test_create_kpi_rejects_invalid_status` 422
- `test_create_kpi_rejects_duplicate_code` 409 ConflictError
- `test_get_kpi_by_id` 详情 200
- `test_list_kpis` 列表按 kpi_code 升序
- `test_update_increments_revision_count` DRAFT 自增
- `test_update_published_increments_revision_count` PUBLISHED 也可改
- `test_update_clears_field_with_explicit_null` 清空字段
- `test_deprecate_status` → DEPRECATED 200
- `test_delete_kpi` 删除 204，后续 GET 404
- `test_metric_id_optional_fk` metric_id 可空
- `test_update_status_null_rejected` 显式 null 被拒（400 或 422）
- `test_db_persists_revision_count` DB 实存自增
- `test_db_status_enum_constraint` DB CHECK 约束阻挡非法 status

**前端测试（4 用例，全部 PASS）**：
`frontend/src/tests/KpiCatalogPage.test.tsx`：
- `渲染标题与表格，并加载 KPI 列表`：新建按钮 + 表格数据 + 状态 Tag 中文「已发布
- `渲染操作列按钮`：编辑/删除按钮可见
- `点击新建并提交调用 createKpi（camelCase payload）`：完整 CRUD 流
- `列表渲染状态 Tag 颜色：DRAFT/PUBLISHED/DEPRECATED`：3 状态渲染

**覆盖率**：
- 后端全量：**1374 passed**（基线 1348 + Phase 4.1 新增 25 + status=null 1 = 1374）
- `app/services/kpi_catalog_service.py`：所有分支命中
- `app/api/v1/kpi_catalog.py`：所有端点命中
- 前端全量：**325 passed**（基线 321 + Phase 4.1 新增 4 = 325），`npx tsc --noEmit` 0 错误

## 7. 审查

**Code Reviewer（code-reviewer）**：
- 报告 2 CRITICAL + 2 HIGH + 2 LOW
- CRITICAL #1「status=None 会写 NULL」**确认有效**：DB NOT NULL 约束；service 层已新增 `if "status" in updates and updates["status"] is None: raise ValidationError(...)` 守护，并加 `MSG_KPI_CATALOG_STATUS_NULL` 文案 + 集成测试 `test_update_status_null_rejected`
- CRITICAL #2「test_db_status_enum_constraint 隔离性」**驳回**：测试已 PASS（`test_db_status_enum_constraint PASSED [100%]`），DB CHECK 约束实际生效；审查者误判
- HIGH #1「created_by 被丢弃」**驳回**：service 行 62 已正确 `created_by=dto.created_by` 传递；审查者误读代码
- HIGH #2「user context 未捕获」**接受但降级**：与现有 `entity_mapping.py` 同模式（项目级「先鉴权后审计」暂未启用），保持一致
- LOW #1「singleton vs factory」**接受但暂不修改**：项目内既有 entity_mapping 用 factory、kpi_catalog 用 singleton 的混合模式均通过测试；不破坏现有惯例
- LOW #2「kpiCode on update」**接受但暂不修改**：API 层 `exclude_unset=True` 仍接受该字段，符合前端契约

**Security Reviewer（security-reviewer）**：
- 报告 0 CRITICAL + 2 HIGH + 2 MEDIUM + 1 LOW
- HIGH #1「GET endpoints 无鉴权」**已修复**：所有 5 个端点（含 GET）均加 `_user: CurrentUser = Depends(getCurrentUser)` 依赖
- HIGH #2「Text 字段无 max_length」**已修复**：business_definition/formula ≤ 4000；numerator/denominator ≤ 1000；data_source ≤ 200；其他 String 字段已有 ≤ 50/100 限制
- MEDIUM #3「无 status 变更授权策略」**接受但暂不修改**：与现有 entity_mapping / ontology_class 同模式（项目级 owner-based ACL 暂未启用），保持一致；下次「审计体系」阶段统一引入
- MEDIUM #4「CurrentUser 是 stub」**接受但属项目级问题**：超出本 change 范围，跟踪至全局架构治理计划
- LOW #5「duplicate code 在 409 中回显」**接受**：REST 标准做法，kpi_code 非敏感

**修复后复查**：后端 1374/1374 PASS；前端 325/325 PASS；`tsc --noEmit` 0 错误。

## 8. 部署与验证

**迁移路径**：
```bash
cd backend
TEST_DATABASE_URL=... .venv/bin/alembic upgrade head
# 期望：0023_kpi_catalog applied; kpi_catalog 表 + 3 索引 + CheckConstraint 创建
```

**API 冒烟**：
```bash
curl -X POST http://localhost:8000/api/v1/kpi-catalog \
  -H 'Content-Type: application/json' \
  -d '{"kpiCode":"KPI_OTD","kpiName":"准时交付率"}'
# 期望：201 + {"id":1,"kpiCode":"KPI_OTD","status":"DRAFT",...}

curl http://localhost:8000/api/v1/kpi-catalog
# 期望：200 + [{...KPI_OTD...}]

curl -X POST .../kpi-catalog -d '{"kpiCode":"KPI_OTD",...}'
# 期望：409 + {"message":"KPI 编码「KPI_OTD」已存在"}

curl -X PUT .../kpi-catalog/1 -d '{"status":"PUBLISHED"}'
# 期望：200 + {"status":"PUBLISHED","revisionCount":1,...}

curl -X PUT .../kpi-catalog/1 -d '{"status":null}'
# 期望：400 或 422
```

**前端冒烟**：
- 访问 `http://localhost:5173/kpi-catalog`
- 左侧菜单显示「KPI 目录」
- 点击「新建 KPI」→ 模态弹出 → 填写 kpiCode + kpiName → 确定 → 列表新增一行
- 编辑/删除按钮可用；状态 Tag 中文显示（DRAFT=灰/PUBLISHED=绿/DEPRECATED=橙）

**集成链路**：
- KPI Catalog 不直接接入 NL2SQL 主链路（治理层）
- Phase 4.2 seed 脚本将填充 12 条种子（OTD/质量/价格/财务）
- Phase 5 Supplier 360° 视图将消费 `kpi_catalog.owner` + `status`

## 9. 真实数据验证（Harness 门禁）

本次 change 未涉及真实业务数据（KPI 表为空种子状态），种子填充属于 Phase 4.2 范围。
本次 change 的真实数据验证集中在：
- 真实 PostgreSQL 5433：所有集成测试命中真库真表，迁移脚本执行成功
- Alembic 版本链：`0023_kpi_catalog` 在 `0022_ontology_class_governance` 之上，向下兼容
- Frontend 路由：`/kpi-catalog` 与左侧菜单联动，端到端可点击进入
- i18n 修复：调试发现 `kpiCatalog` 块误嵌入 `localImport` 子级，修正为顶级键后双语页面均能正确渲染翻译

## 10. 决策与遗留

**已落地的决策**：
1. 新建独立 `kpi_catalog` 表（与 `ontology_metric` 并存）— 两表职责清晰，互不耦合
2. `revision_count` 在 service 自增（非 DTO）— 客户端无法绕过
3. 不写 Neo4j / Milvus — KPI 治理层不入向量检索
4. status 三态用 enum + DB CheckConstraint 双重保险
5. 所有 Text 字段加 `max_length` — 防御性输入校验
6. 所有 5 端点统一鉴权 — 与现有项目惯例对齐

**遗留（下次迭代）**：
- Owner-based ACL / 角色权限（MEDIUM #3）— 等待「审计体系」专项
- KPI 与 ontology_metric 双向同步 — 当前是单向 metric_id 弱关联；如果 ontology_metric 删除，kpi_catalog.metric_id 需手动清理
- 历史版本快照 — 当前 revision_count 只 +1，不存历史快照；如需「按版本回放」需新增 kpi_catalog_history 表
- KPI 与 Feature Layer 关联（Phase 4.3 计划）— kpi_catalog.metric_id 已留好对接位

**相关 Change**：
- 前置：`feat-data-quality-rule-model` / `feat-entity-mapping-model` / `feat-ontology-governance-fields`（提供 governance 字段范式）
- 后继：Phase 4.2 `feat-kpi-catalog-seed`（12 条种子）；Phase 5 Supplier 360° 视图消费 kpi_catalog