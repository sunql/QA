# 变更：entity_mapping 模型 + Alembic 迁移 + CRUD + 前端页（Phase 3.1）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 3（L3 数据治理 — 跨系统编码映射）
- **状态**：done

## 1. 需求

建立跨系统实体编码映射表与 CRUD 能力，使 AI 应用能回答「供应商 100001 在 ERP 的编码是什么」「同一企业实体在 ERP/SRM/QMS 间的编码如何对应」等跨系统追溯问题（满足 AI-Ready 标准体系「命名与编码」「主数据与一致性」、采购域 §三、模板 Sheet 05）。本期仅承载映射模型 + 手工 CRUD，种子数据留 Phase 3.2，缺失业务对象建模留 Phase 3.3。

**验收标准**：
- `entity_mapping` 表承载 6 种实体类型（SUPPLIER / MATERIAL / PO / GR / IQC / NCR）× 5 种源系统（ERP / SRM / QMS / MDM / PLM）
- 同一（实体类型 + 企业代理键 + 源系统）不允许重复（DB 唯一约束 + service 层 ValidationError 兜底）
- 同一实体在不同源系统可有各自映射（仅约束「同实体类型 + 同源系统」唯一）
- 匹配规则三态：MDM_MASTER / BUSINESS_KEY / MAPPING（默认 MAPPING）
- 生效/失效日期可空，空表示长期有效
- REST 端点支持按 entityType / sourceSystem / enterpriseKey 过滤
- 硬删除：DELETE 置 204（映射为可重建的派生数据，无需软删）
- 前端「编码映射」页可增删改查

## 2. 设计评审

**已确认的关键决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 唯一约束策略 | DB `uq_entity_mapping_entity_source`（三列）+ service 层主动查重 | 与 data_lineage（Phase 2.1）同模式：PG 唯一索引做 race condition 兜底，service 层提前给出可读的 422 信息 |
| 删除方式 | 硬删除（DELETE 204） | 映射是「源系统 ↔ 企业侧」的配置性数据，可随时重建；与 data_lineage 的软删除（保留可视化历史）不同，映射无追溯需求 |
| 枚举字段存储 | VARCHAR + CheckConstraint（不建 enum type） | 与现有 DataLineage 模式一致；避免 PG enum type 后续 ALTER 成本 |
| `match_rule` 默认值 | `MAPPING`（server_default + ORM default） | 人工/规则映射是最常见的手工录入形态；MDM_MASTER / BUSINESS_KEY 为特定场景 |
| 迁移编号 | **`0021_entity_mapping`**（计划文档写 0020，因 0020 已被 `0020_data_lineage` 占用） | 迁移链唯一性要求 |
| `enterprise_key` 类型 | BIGINT（MDM 主键语义） | 与主数据统一代理键对齐；Pydantic `gt=0` 校验 |
| 实体类型范围 | 6 种（SUPPLIER / MATERIAL / PO / GR / IQC / NCR） | 对齐采购域 Sheet 03 业务对象目录；其余对象 Phase 3.3 补充 |

**多视角审视**：
- **后端视角**：service 层先查重再 INSERT，DB 唯一索引兜底并发；`updateMapping` 用 `model_dump(exclude_unset=True)` 只覆盖显式字段，不动未传字段
- **前端视角**：DTO camelCase 与后端 JSON 1:1 对齐；日期字段用带 `YYYY-MM-DD` 占位符的 Input + pattern 校验（不引第三方日期选择器，KISS）
- **安全视角**：所有 user input 走 Pydantic `max_length` + 枚举校验；service 层纯 ORM 无 SQL 拼接
- **不可变性视角**：create 构造新 ORM 对象；update 用 exclude_unset 局部更新，不整体替换

## 3. 数据模型变更

**新增表**：`entity_mapping`（Alembic `0021_entity_mapping`，down_revision = `0020_data_lineage`）

```
id (BIGINT PK),
entity_type (VARCHAR 20, SUPPLIER/MATERIAL/PO/GR/IQC/NCR),
enterprise_key (BIGINT, 企业统一代理键，MDM 主键),
enterprise_code (VARCHAR 100, 企业统一编码，如 SUP000001),
source_system (VARCHAR 20, ERP/SRM/QMS/MDM/PLM),
source_key (VARCHAR 100, 源系统原始 key),
source_code (VARCHAR 100, 源系统原始编码),
match_rule (VARCHAR 20, MDM_MASTER/BUSINESS_KEY/MAPPING, server_default=MAPPING),
effective_date (DATE, nullable), expiry_date (DATE, nullable),
created_time, updated_time (TimestampMixin)

UNIQUE: uq_entity_mapping_entity_source (entity_type, enterprise_key, source_system)
INDEX : ix_entity_mapping_enterprise_key (enterprise_key)
INDEX : ix_entity_mapping_source (entity_type, source_system, source_key)
CHECK : ck_entity_mapping_entity_type ∈ 6 值集合
CHECK : ck_entity_mapping_source_system ∈ 5 值集合
CHECK : ck_entity_mapping_match_rule ∈ 3 值集合
```

**迁移**：`backend/alembic/versions/0021_entity_mapping.py`。注：计划文档标注 0020，实际为 0021（0020 已被 data_lineage 占用），`down_revision` 指向 `0020_data_lineage`，已应用干净。

## 4. 接口契约变更

**HTTP 接口**（prefix `/api/v1/entity-mappings`）：
- `GET    /api/v1/entity-mappings?entityType=&sourceSystem=&enterpriseKey=` → `EntityMappingRead[]`
- `GET    /api/v1/entity-mappings/{mappingId}` → `EntityMappingRead`（不存在 → 404）
- `POST   /api/v1/entity-mappings` → 201 + `EntityMappingRead`（重复 → 422）
- `PUT    /api/v1/entity-mappings/{mappingId}` → 200 + `EntityMappingRead`（局部更新；不存在 → 404）
- `DELETE /api/v1/entity-mappings/{mappingId}` → 204（硬删除）

**后端 DTO**（`backend/app/domain/schemas.py`）：
- `EntityMappingCreate`：entity_type（枚举）/ enterprise_key（BIGINT gt=0）/ enterprise_code / source_system（枚举）/ source_key / source_code 必填；match_rule 默认 MAPPING；effective_date / expiry_date 可空
- `EntityMappingUpdate`：全部可选（None 视为不动），date 字段可置 None
- `EntityMappingRead`：全部字段 + id / created_time / updated_time

**前端类型**（`frontend/src/types/entityMapping.ts`）：
- `EntityType / SourceSystem / MatchRule` 字符串字面量联合（与后端枚举值 1:1）
- `EntityMappingBase / Create / Update / Read / ListFilter`（camelCase，与后端 JSON 对齐）

**前端 API**（`frontend/src/api/entityMapping.ts`）：`listMappings / getMapping / createMapping / updateMapping / deleteMapping`，BASE = `/entity-mappings`

**前端页面**（`frontend/src/pages/EntityMappingPage.tsx`）：11 列表格 + 顶部实体类型筛选 + 新建/编辑 Modal + Popconfirm 删除

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/domain/enums.py` | 新增 `EntityType`（6 值）/ `SourceSystem`（5 值）/ `MatchRule`（3 值） |
| `backend/app/domain/models.py` | 新增 `EntityMapping` ORM（`BigIntPk` + `TimestampMixin` + UniqueConstraint + 2 Index + 3 CheckConstraint） |
| `backend/app/domain/schemas.py` | 新增 3 个 CamelModel DTO + 10 条 schema 描述常量 |
| `backend/app/domain/error_messages.py` | 10 条 `MSG_SCHEMA_ENTITY_MAPPING_*` 描述常量 |
| `backend/app/services/messages_zh.py` | 2 条 `MSG_ENTITY_MAPPING_*` 错误信息（NOT_FOUND / EXISTS） |
| `backend/app/services/entity_mapping_service.py` | 新文件：`listMappings`（过滤）/ `getMapping`（404）/ `createMapping`（查重→422）/ `updateMapping`（exclude_unset 局部更新）/ `deleteMapping`（硬删）+ `entityMappingToRead` |
| `backend/app/api/v1/entity_mapping.py` | 新文件：5 个 REST 端点 + Query alias 过滤参数 + 依赖注入 |
| `backend/app/main.py` | 注册 `entity_mapping.router` 到 `/api/v1/entity-mappings` |
| `backend/app/tests/_testapp.py` | 测试 app 同步挂载 entity_mapping 路由（与 main.py 平行，缺失会导致集成测试 404） |
| `backend/alembic/versions/0021_entity_mapping.py` | 新增迁移（建表 + 唯一索引 + 2 普通索引 + 3 CheckConstraint） |
| `frontend/src/types/entityMapping.ts` | 新文件：DTO 类型契约 |
| `frontend/src/api/entityMapping.ts` | 新文件：HTTP client 封装 |
| `frontend/src/pages/EntityMappingPage.tsx` | 新文件：CRUD 页（EntityType/SourceSystem/MatchRule 常量 + matchRule 语义色 Tag） |
| `frontend/src/App.tsx` | 新增 `<Route path="entity-mapping">` |
| `frontend/src/components/common/AppLayout.tsx` | 左侧导航新增「编码映射」入口 |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | 新增 `entityMapping` 命名空间 + `common.save/actions`（补全缺失 key，连带修复 DataQualityPage 同款问题） |

**关键不可变性细节**：
- `createMapping` 构造新 ORM 对象后 `session.add(...)`，不修改任何入参
- `updateMapping` 用 `model_dump(exclude_unset=True, by_alias=False)` 循环覆盖显式字段，未传字段保持原值
- `entityMappingToRead` 用 `EntityMappingRead.model_validate(m, from_attributes=True)`，JSON 走 CamelModel 别名

**前端已知坑（antd Select + jsdom）**：
- antd v5 Modal 内 Select 会额外渲染一个不可点击的 a11y 复制层（`div[role=option]`），真实可点击选项是 `.ant-select-item-option`（带 `title` 属性）
- 测试统一用 `findByTitle("值")` 点选选项（唯一命中可点击元素），避免 `findByText` 双匹配 / `findByRole("option")` 命中不可点击复制层

## 6. 测试

**后端集成测试**（真实 PG 5433，`backend/app/tests/integration/test_entity_mapping_api.py`，15 测试）：
- `test_list_empty`：GET 空数组
- `test_create_get_update_roundtrip`：CRUD 全链路
- `test_delete_returns_204_and_gone`：删除后 GET → 404
- `test_list_filter_by_enterprise_key`：按 enterpriseKey 过滤
- `test_list_filter_by_entity_type_and_source_system`：组合过滤
- `test_duplicate_unique_constraint_returns_422`：同实体 + 同源系统重复 → 422
- `test_same_entity_different_source_system_is_allowed`：不同源系统允许并存
- `test_get_not_found_returns_404`：不存在 → 404
- `test_invalid_enum_returns_422`：非法枚举 → 422
- `test_list_respects_limit_offset`：分页 limit 截断 + offset 跳过（审查 H2 修复）
- `test_update_null_for_non_nullable_ignored`：sourceCode 传 null 视为不动
- `test_update_null_clears_expiry_date`：expiryDate 传 null 清除
- `test_update_empty_string_rejected_422`：非空列空串 → 422（审查 L1 修复）
- `test_create_inverted_date_range_422`：生效 > 失效 → 422（审查 M1 修复）
- `test_create_enterprise_key_overflow_422`：enterprise_key 超 BIGINT → 422（审查 L4 修复）
- **小计**：15/15 PASS

**后端单元测试**（`backend/app/tests/unit/test_entity_mapping_service.py`，20 测试）：
- `listMappings`：空 / 全量 / 默认分页（3）
- `getMapping`：命中 / 404（2）
- `createMapping`：全字段 / 日期倒置 422 / 查重命中 422 / **commit IntegrityError → rollback + 422**（4）
- `updateMapping`：局部覆盖 / **非空列传 None 不动** / **日期列传 None 清除** / 更新后日期倒置 422 / 404（5）
- `deleteMapping`：删除 + commit / 404（2）
- `entityMappingToRead`：snake_case roundtrip（1）
- DTO 校验：必填 / enterprise_key 上限 / update 空串（3）

**前端单元测试**（12 测试）：
- `frontend/src/tests/entityMappingApi.test.ts`：6 测试（list 带参/无参、get、create camelCase payload、update 局部更新、delete）
- `frontend/src/tests/EntityMappingPage.test.tsx`：6 测试（渲染表格、操作列按钮、新建提交 createMapping、编辑提交 updateMapping、删除确认、筛选重新拉取）

**测试结果**：
- 后端全量：**1306 passed**，覆盖率 **93.21%**（≥80% 门禁）
- `entity_mapping_service.py`：100%；`entity_mapping.py` router：100%
- 前端全量：**319 passed**；`npx tsc --noEmit` 通过

## 7. 安全审查

**触发场景**：HTTP 入口 + ORM 写入 + service 层校验（按 code-review.md 安全审查清单，本次由 code-reviewer / security-reviewer 双 agent 并行审查）。

**关键风险**：
- 实体类型/源系统/匹配规则来自用户输入 → 枚举注入风险
  - 解决：Pydantic enum 校验（非法值 → 422），DB 侧 CheckConstraint 双保险
- 字符串字段长度 → Pydantic `max_length` 约束（VARCHAR 100 / 20 与 DB 列对齐）
- SQL 注入 → service 层纯 SQLAlchemy ORM 标准操作，无 raw SQL / f-string
- 错误信息泄漏 → NOT_FOUND / EXISTS 只含 id 与业务键，不暴露 DB 细节

**审查结果**：见 §9.3（code-reviewer / security-reviewer 双 agent 均 APPROVED，无 CRITICAL/HIGH 遗留）。

## 8. 部署验证

```bash
cd backend

# 应用迁移
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run alembic upgrade head
# → Running upgrade 0020_data_lineage -> 0021_entity_mapping

# 跑集成测试
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_entity_mapping_api.py -v
# → 15/15 PASS

# 单测
uv run pytest app/tests/unit/test_entity_mapping_service.py -v
# → 20/20 PASS

# 全量回归 + 覆盖率
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
# → 1306 passed, TOTAL 93.21%

cd ../frontend
npx vitest run
# → 319 passed
npx tsc --noEmit
# → 通过
```

## 9. 真实数据验证（Harness 门禁）

集成测试全程走真实 PG 5433 + 真实 FastAPI + 真实 ORM + 完整 HTTP 链路；无 sqlite 内存库，无 mock session。`_ensureSchema` 在测试进程内执行 `alembic upgrade head` 应用迁移 0021，每测试 `TRUNCATE` 隔离。

### 9.1 验证结果（2026-08-30）

```
test_list_empty PASSED
test_create_get_update_roundtrip PASSED
test_delete_returns_204_and_gone PASSED
test_list_filter_by_enterprise_key PASSED
test_list_filter_by_entity_type_and_source_system PASSED
test_duplicate_unique_constraint_returns_422 PASSED
test_same_entity_different_source_system_is_allowed PASSED
test_get_not_found_returns_404 PASSED
test_invalid_enum_returns_422 PASSED
test_list_respects_limit_offset PASSED
test_update_null_for_non_nullable_ignored PASSED
test_update_null_clears_expiry_date PASSED
test_update_empty_string_rejected_422 PASSED
test_create_inverted_date_range_422 PASSED
test_create_enterprise_key_overflow_422 PASSED
========================= 15 passed =========================
```

| 检查项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| GET /api/v1/entity-mappings 空列表 | `[]` | `[]` | ✅ |
| POST 创建 → 201 + 完整 JSON | 201 | 201 | ✅ |
| GET /{id} roundtrip 字段一致 | 与 POST 返回一致 | 一致 | ✅ |
| POST 重复（同实体+同源系统）→ 422 | 422 | 422 | ✅ |
| POST 同实体不同源系统 | 201 允许 | 201 | ✅ |
| POST 非法枚举 → 422 | 422 | 422 | ✅ |
| GET /{id} 不存在 → 404 | 404 | 404 | ✅ |
| DELETE /{id} → 204 + 再 GET → 404 | 204 / 404 | 204 / 404 | ✅ |
| GET /?enterpriseKey=X 过滤 | 仅返回对应映射 | 1 条 | ✅ |
| GET /?entityType=&sourceSystem= 组合过滤 | 组合条件 | 1 条 | ✅ |
| GET /?limit=2&offset=0 → 2 条；offset=2 → 剩 1 条 | 分页截断 | 2 / 1 | ✅ |
| PUT sourceCode=null | 保留原值 V000001 | V000001 | ✅ |
| PUT expiryDate=null | 清除日期 | null | ✅ |
| PUT sourceCode="" → 422 | 422 | 422 | ✅ |
| POST 生效日期 > 失效日期 → 422 | 422 | 422 | ✅ |
| POST enterprise_key=2^63 → 422 | 422 | 422 | ✅ |

### 9.2 数据契约 Roundtrip 一致性

后端 `entityMappingToRead` 使用 `EntityMappingRead.model_validate(m, from_attributes=True)`，JSON 输出走 Pydantic 默认 `by_alias=True`（CamelModel + alias_generator=to_camel），前端 TypeScript 类型字段命名与后端 JSON 完全一致（snake_case ORM ↔ camelCase JSON）。`Decimal / date / datetime / Enum` 全部走标准 Pydantic 序列化路径。

### 9.3 代码审查结果

**审查方式**：code-reviewer + security-reviewer 双 agent 并行审查（覆盖 §7 全部安全触发场景）。两轮：首轮发现 → 修复 → 复核 APPROVED。

**首轮发现与修复对照**（按严重度合并重叠项）：

| 级别 | 编号 | 发现 | 修复 |
|---|---|---|---|
| HIGH | H1 | router 未挂 `getCurrentUser` 认证依赖，所有端点匿名可访问 | `entity_mapping.py` 改为 `APIRouter(dependencies=[Depends(getCurrentUser)])`，与 peer router 对齐 |
| HIGH | H2 | 列表端点无分页，数据量增长后单次响应可拖垮服务 | `listEntityMappings` 增加 `limit`（默认 200，1-1000）/ `offset`，透传 service 层 `.limit().offset()` |
| MEDIUM | M1 | 并发 race：两请求同时越过查重，败者 commit 撞唯一索引 → 裸 500 | `createMapping` 包 `try/except IntegrityError → rollback + ValidationError`（复用 `_commitOrConflict` 模式） |
| MEDIUM | M-A | Update DTO 允许空串（如 `sourceCode=""`），绕过长度语义 | `schemas.py` 三个非空列 Update 字段加 `min_length=1`；service 层 `_NON_NULL_UPDATE_FIELDS` 拒绝置 None |
| LOW | L4 | `enterprise_key` 无上界，可写入超 BIGINT 值 | `Create` DTO 加 `le=2**63 - 1` 上限 |
| LOW | L-B | 前端空日期字符串（`""`）提交 → 后端 422（date 拒绝空串） | `normalizeDates`：空串 → `null`（创建存 NULL / 编辑清除日期），前后端契约对齐 |
| LOW | L-C | `form.validateFields()` 在 try/catch 外，校验失败会抛未捕获异常 | 两个 submit handler 均改为 `try { values = normalizeDates(await form.validateFields()) } catch { return }` 早退 |

**复核结论**：

- **security-reviewer**：APPROVED。H1/H2 已修复；无 CRITICAL/HIGH 遗留。L4 企业代理键上限、M1 并发 race 转 422 均验证通过。
- **code-reviewer**：APPROVED。M-A/M-B（=M1 合并项）已修复；L-B/L-C 前端契约对齐完成；无 CRITICAL/HIGH 遗留。

**回归验证**：修复后全量后端 1306 passed（93.21%）、前端 319 passed、tsc 通过；新增集成测试 6 条 + 单测 20 条锁定上述修复行为（见 §6）。**门禁：通过，允许 commit。**

## 10. 关联

- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 3.1
- 模板：`Harness/changes/feat-data-lineage-model/summary.md`（Phase 2.1，CRUD 模型 SSOT 模板）
- 下一阶段：`feat-entity-mapping-seed`（Phase 3.2，25 条供应商/物料跨系统映射种子）
- 规则：`Harness/rules/开发流程规范.md`（10 阶段工作流）
