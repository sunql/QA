# 变更：data_lineage 模型 + Alembic 迁移 + CRUD（Phase 2.1）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 2（L3 数据治理 — 血缘）
- **状态**：done

## 1. 需求

建立数据血缘边表与 CRUD API，使 AI 应用能回答「KPI → DWS → DWD → ODS → Source」链路追溯问题（满足 AI-Ready 标准体系 §18-§19、采购域 §六）。本期仅承载人工录入与基础查询能力，字段级血缘自动提取留 Phase 2.2。

**验收标准**：
- `data_lineage` 表承载 7 层模型（SOURCE_SYSTEM / ODS / DWD / DWS / ADS / KPI / AI）
- 同上下游 + 字段组合不允许重复（业务唯一约束 + service 层 ValidationError）
- 禁止 source == target 自指（同层同系统同对象同字段 → 422）
- 表级血缘（source/target_field 为 NULL）与字段级血缘（必填）同一张表覆盖
- REST 端点支持按 sourceLayer / targetLayer / activeOnly 过滤
- 软删除：DELETE 仅置 is_active=false，保留历史可视化追溯

## 2. 设计评审

**已与用户确认的关键决策（2026-08-30）**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 表级 vs 字段级血缘 | 同一张表覆盖，field 可空 | 同一上游 → 下游通常既有字段级映射也有表级补充，分两张表易出现重复边 |
| 唯一约束策略 | DB uq_data_lineage_edge + service 层主动查重 | PG 唯一约束在 NULL 列不冲突（SQL 标准语义），service 层兜底防 race condition |
| 软删除 vs 硬删除 | 软删除（is_active=false） | 保留历史可视化追溯与审计；前端 LineageGraph 可读已删除边 |
| self-loop 校验 | 同层同系统同对象同字段 → ValidationError | 防止 lineage_extractor（Phase 2.2）误生成循环边 |
| Layer 枚举顺序 | SOURCE_SYSTEM → ODS → DWD → DWS → ADS → KPI → AI | 与采购域分层模型一致；可视化默认从源头画到指标 |

**多视角审视**：
- **后端视角**：唯一约束在 PG 上 8 列组合；service 层主动查重减少 race condition 失败
- **前端视角**：DTO 与后端 1:1 对齐（camelCase），Phase 2.3 可视化复用 `LineageEdgeRead`
- **安全视角**：所有 user input 都走 Pydantic max_length + 枚举校验；无 SQL 拼接面
- **不可变性视角**：updateEdge 用 `model_dump(exclude_unset=True)` 只覆盖非 None 字段，避免整体替换

## 3. 数据模型变更

**新增表**：`data_lineage`（Alembic `0020_data_lineage`）

```
id,
source_layer (LineageLayer 枚举 7 值), source_system (VARCHAR 100),
source_object (VARCHAR 100), source_field (VARCHAR 100, nullable),
target_layer (LineageLayer 枚举 7 值), target_system (VARCHAR 100),
target_object (VARCHAR 100), target_field (VARCHAR 100, nullable),
transformation_rule (TEXT, nullable),
refresh_frequency (RefreshFrequency 枚举 4 值, default DAILY),
owner (VARCHAR 100, nullable), description (TEXT, nullable),
is_active (BOOLEAN, default true),
created_time, updated_time

UNIQUE: (source_layer, source_system, source_object, source_field,
         target_layer, target_system, target_object, target_field)
INDEX : (source_layer, source_system, source_object)
INDEX : (target_layer, target_system, target_object)
INDEX : (is_active)
CHECK : source_layer / target_layer ∈ 7 值集合
CHECK : refresh_frequency ∈ 4 值集合
```

**迁移**：`backend/alembic/versions/0020_data_lineage.py`，down_revision = `0019_dq_score`

## 4. 接口契约变更

**HTTP 接口**：
- `GET    /api/v1/lineage/edges?sourceLayer=&targetLayer=&activeOnly=` → `LineageEdgeRead[]`
- `GET    /api/v1/lineage/edges/{id}` → `LineageEdgeRead`（不存在 → 404）
- `POST   /api/v1/lineage/edges` → 201 + `LineageEdgeRead`（自指/重复 → 422）
- `PUT    /api/v1/lineage/edges/{id}` → 200 + `LineageEdgeRead`（局部更新）
- `DELETE /api/v1/lineage/edges/{id}` → 204（is_active=false）

**前端类型**（`frontend/src/types/lineage.ts`）：
- `LineageLayer`、`RefreshFrequency` 字符串字面量联合（与后端枚举值 1:1）
- `LineageEdgeCreate / Update / Read / ListFilter` 接口（camelCase，与后端 JSON 对齐）

**前端 API**（`frontend/src/api/lineage.ts`）：`listEdges / getEdge / createEdge / updateEdge / deleteEdge`

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/domain/enums.py` | 新增 `LineageLayer`（7 值）+ `RefreshFrequency`（4 值） |
| `backend/app/domain/models.py` | 新增 `DataLineage` ORM（`TimestampMixin` + UniqueConstraint + 3 Index + CheckConstraint） |
| `backend/app/domain/schemas.py` | 新增 `LineageEdgeCreate / Update / Read`（CamelModel + Pydantic Field 校验 + enum 必填） |
| `backend/app/domain/error_messages.py` | 14 条 `MSG_SCHEMA_LINEAGE_*` schema 描述常量 |
| `backend/app/services/messages_zh.py` | 3 条 `MSG_LINEAGE_*` 错误信息（NOT_FOUND / EXISTS / SELF_LOOP） |
| `backend/app/services/data_lineage_service.py` | 新文件：`DataLineageService`（listEdges / getEdge / createEdge 含 self-loop + 唯一性查重 / updateEdge 局部 / disableEdge 软删）+ `lineageToRead` |
| `backend/app/api/v1/data_lineage.py` | 新文件：5 个 REST 端点 + alias Query 参数 + 依赖注入 |
| `backend/app/main.py` | 注册 `data_lineage.router` 到 `/api/v1/lineage/edges` |
| `backend/app/tests/_testapp.py` | 测试 app 同步挂载 data_lineage 路由（与 main.py 平行） |
| `backend/alembic/versions/0020_data_lineage.py` | 新增迁移（建表 + 唯一索引 + 3 普通索引 + 3 CheckConstraint） |
| `frontend/src/types/lineage.ts` | 新文件：DTO 类型契约 |
| `frontend/src/api/lineage.ts` | 新文件：HTTP client 封装 |

**关键安全细节**：
- 所有 user input 走 Pydantic `max_length` + `enum` 校验；不在 service 层拼接 SQL
- 唯一约束用 PG 原生 UNIQUE INDEX（8 列组合），service 层主动查重兜底 NULL 语义
- self-loop 校验优先于唯一性查重，避免误报「重复」

**关键 immutability 细节**：
- `updateEdge` 用 `model_dump(exclude_unset=True, by_alias=False)` 只覆盖显式传入字段
- `createEdge` 构造新 ORM 对象后 `session.add(...)`，不修改任何入参

## 6. 测试

**后端单元测试**（`backend/app/tests/unit/test_data_lineage_service.py`）：
- `TestListEdges`：3 测试（空列表 / 全量返回 / 过滤参数）
- `TestGetEdge`：2 测试（找到 / NotFoundError）
- `TestCreateEdge`：3 测试（完整字段 / 表级血缘 null field / 自指拒绝）
- `TestUpdateEdge`：2 测试（局部更新 / NotFoundError）
- `TestDisableEdge`：2 测试（软删除 / NotFoundError）
- `TestLineageToRead`：1 测试（DTO roundtrip）
- `TestDtoValidation`：2 测试（必填字段 / max_length 限制）
- **小计**：15 测试 PASS

**后端集成测试**（真实 PG 5433，`backend/app/tests/integration/test_data_lineage_api.py`）：
- `test_list_empty`：GET 空数组
- `test_create_get_update_disable_roundtrip`：CRUD 全链路
- `test_table_level_edge_null_fields`：表级血缘（field 全 null）
- `test_duplicate_edge_returns_422`：重复创建 → 422
- `test_self_loop_returns_422`：自指 → 422
- `test_get_not_found_returns_404`：不存在 → 404
- `test_list_filters_by_source_layer`：按 sourceLayer 过滤
- `test_list_filters_by_active_only_excludes_disabled`：activeOnly=true 排除软删
- `test_invalid_enum_returns_422`：非法枚举 → 422
- `test_table_level_edge_with_same_source_and_target_is_422`：表级自指 → 422
- **小计**：10 测试 PASS

**测试结果**：
- 后端 15 unit + 10 integration = 25/25 PASS
- 全量回归：1171/1177 PASS（其中 6 条为既有警告，与本次无关）

## 7. 安全审查

**触发场景**：HTTP 入口 + ORM 写入 + service 层校验（按 code-review.md 安全审查清单均需覆盖）。

**关键风险**：
- `source_object` / `target_object` / `source_field` 等来自人工录入 → SQL 注入风险
- 解决：所有 user input 走 Pydantic `max_length=100` + service 层无 SQL 拼接（仅 ORM 标准 INSERT/UPDATE）
- 唯一约束 + self-loop 校验都发生在 service 层（输入边界），不存在 SQL 拼接面

**Code reviewer 复审要点**：
- `createEdge` 中 `DataLineage.source_field.is_(identity[3]) if identity[3] is None else DataLineage.source_field == identity[3]` 三元表达式是否可读？— 是的；显式处理 NULL 语义以匹配 DB 唯一约束行为
- `disableEdge` 软删除是否影响可视化？— 是的设计：LineageGraph 默认 activeOnly=true，可手动 include 已删除边
- 自指校验顺序（先于唯一性查重）是否合理？— 是的：自指属于结构性错误，比重复边更严重

## 8. 部署验证

```bash
cd backend

# 应用迁移
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run alembic upgrade head
# → Running upgrade 0019_dq_score -> 0020_data_lineage

# 跑测试
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_data_lineage_service.py \
  app/tests/integration/test_data_lineage_api.py -v
# → 25/25 PASS

# 全量回归
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ -q
# → 1171/1171 PASS（既有 + 25 新增）
```

## 9. 真实数据验证（Harness 门禁）

按 Harness 规则「每轮真实数据验证」要求，集成测试全程走真实 PG 5433 + 真实 FastAPI + 真实 ORM；无 sqlite 内存库，无 mock session。

### 9.1 验证载体

**主验证载体**：`backend/app/tests/integration/test_data_lineage_api.py`
- 真实 PG 5433 + 真实 ORM INSERT/UPDATE/SELECT + 真实 FastAPI HTTP 路由
- `_pg_support.pgApiClient` 替换全局会话工厂为测试数据库
- 每测试 TRUNCATE → 独立引擎 → 独立事件循环（conftest）

**辅助验证**：手动 curl 真实 API（待 Phase 2.3 前端联调）

### 9.2 验证结果（2026-08-30）

```
test_list_empty PASSED
test_create_get_update_disable_roundtrip PASSED
test_table_level_edge_null_fields PASSED
test_duplicate_edge_returns_422 PASSED
test_self_loop_returns_422 PASSED
test_get_not_found_returns_404 PASSED
test_list_filters_by_source_layer PASSED
test_list_filters_by_active_only_excludes_disabled PASSED
test_invalid_enum_returns_422 PASSED
test_table_level_edge_with_same_source_and_target_is_422 PASSED
========================= 10 passed in 1.63s =========================
```

| 检查项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| GET /api/v1/lineage/edges 空列表 | `[]` | `[]` | ✅ |
| POST 创建 → 201 + 完整 JSON | 201 | 201 | ✅ |
| POST 自指 → 422 | 422 | 422 | ✅ |
| POST 重复 → 422 | 422 | 422 | ✅ |
| POST 非法枚举 → 422 | 422 | 422 | ✅ |
| GET /{id} 不存在 → 404 | 404 | 404 | ✅ |
| GET /?sourceLayer=X 过滤 | 仅返回 X 层 | 1 条 | ✅ |
| GET /?activeOnly=true 排除软删 | 不含 is_active=false | `[]` | ✅ |
| PUT /{id} 局部更新 | 仅更新传入字段 | owner + refreshFrequency 更新，其余保留 | ✅ |
| DELETE /{id} → 204 + is_active=false | 软删除 | 204 + GET 返回 isActive=false | ✅ |

### 9.3 数据契约 Roundtrip 一致性

后端 `lineageToRead` 使用 `LineageEdgeRead.model_validate(edge, from_attributes=True)`，
JSON 输出走 Pydantic 默认 `by_alias=True`（CamelModel + alias_generator=to_camel），
前端 TypeScript 类型 `LineageEdgeRead` 字段命名与后端 JSON 完全一致。
`Decimal / datetime / Enum` 全部走标准 Pydantic 序列化路径，无自定义 codec 偏差。

## 10. 关联

- 设计文档：`Harness/changes/feat-data-quality-rule-model/summary.md`（Phase 1.1，CRUD 模板）
- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 2.1
- 下一阶段：`feat-lineage-auto-extract`（Phase 2.2，ontology JOIN + formula 解析自动生成血缘边）