# 变更：数据质量规则模型（DataQualityRule）

- **日期**：2026-08-29
- **作者**：AI 助手
- **Phase**：Phase 1（数据治理 — 质量）
- **状态**：done

## 1. 需求

建立 `DataQualityRule` 表 + 完整 CRUD（API + 前端），使运维人员能定义数据质量规则（完整性 / 合理性 / 唯一性 / 一致性 / 引用性），后续 Phase 1.2-1.4 评估引擎、AI 可信度集成、Supplier 360° 视图都将消费本表。

**业务背景**：满足 AI-Ready 数据标准体系 §16-§17、采购域 §七（6 维质量校验 + QUALITY_SCORE）。

## 2. 设计评审

- 单表 `data_quality_rule`：一张表覆盖 5 种 rule_type 维度（COMPLETENESS / VALIDITY / UNIQUENESS / CONSISTENCY / REFERENTIAL），TIMELINESS 留 Phase 2。
- `rule_code` 业务唯一，`rule_expression` 文本表达式（如 `ORDER_QTY > 0`），`threshold` DECIMAL(5,2) 0-100 表示通过率阈值。
- `severity`（HIGH/MEDIUM/LOW/INFO）四级 + `is_enabled` 软启用开关 + `version`（治理版本，不参与 NL2SQL）。
- API 路径：`/api/v1/data-quality/rules`，与 `local_import` 同样挂在 `app/main.py`（避免 v1Router 双重叠加 prefix）。
- 前端沿用「列表 + 过滤 + 模态 CRUD」模式，独立新页面 `DataQualityPage`，左侧导航加「数据质量」入口。

## 3. 数据模型变更

新增表 `data_quality_rule`（Alembic 0017）：
```
id, rule_name, rule_code(unique),
target_table, target_column(nullable),
rule_type(5种+TIMELINESS预留),
rule_expression TEXT, threshold DECIMAL(5,2),
severity, is_enabled, version, owner,
description, created_time, updated_time
```

无 FK 依赖。可独立运行。

## 4. 接口契约变更

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/v1/data-quality/rules` | 列表（可按 type/table/enabled 过滤） |
| GET | `/api/v1/data-quality/rules/{id}` | 详情 |
| POST | `/api/v1/data-quality/rules` | 创建 |
| PUT | `/api/v1/data-quality/rules/{id}` | 更新 |
| DELETE | `/api/v1/data-quality/rules/{id}` | 软删除（is_enabled=false） |

DTO（snake_case 字段 + `CamelModel`）：
- `DataQualityRuleCreate` / `DataQualityRuleUpdate` / `DataQualityRuleRead`
- `RuleType` enum：`COMPLETENESS / VALIDITY / UNIQUENESS / CONSISTENCY / REFERENTIAL / TIMELINESS`
- `Severity` enum：`HIGH / MEDIUM / LOW / INFO`

## 5. 实现要点（已落地）

**后端新增**：
- `app/services/data_quality_service.py`：CRUD + `ruleToRead` 转换；service 工厂 `getDataQualityRuleService` 便于后续注入。
- `app/api/v1/data_quality.py`：5 端点（list/get/create/update/delete）；用 Query 接收 `ruleType` / `targetTable` / `enabledOnly` 过滤。
- `app/main.py`：挂载 `data_quality.router` 到 `/api/v1/data-quality/rules`。

**后端改动**：
- `app/domain/enums.py`：新增 `RuleType`（COMPLETENESS/VALIDITY/UNIQUENESS/CONSISTENCY/REFERENTIAL/TIMELINESS）与 `Severity`（HIGH/MEDIUM/LOW/INFO）。
- `app/domain/models.py`：新增 `DataQualityRule` ORM（含 `uq_data_quality_rule_code` 唯一约束 + 3 个索引）。
- `app/domain/schemas.py`：新增 `DataQualityRuleCreate`（带 `rule_code` 正则 `^[A-Z][A-Z0-9_]*$` + threshold 0-100 校验）/ `DataQualityRuleUpdate` / `DataQualityRuleRead`。
- `app/domain/error_messages.py`：新增 `MSG_SCHEMA_DQ_*` 12 条 DTO 描述文案。
- `app/services/messages_zh.py`：新增 `MSG_DQ_RULE_NOT_FOUND` + `MSG_DQ_RULE_CODE_EXISTS`。
- `alembic/versions/0017_data_quality_rule.py`：迁移（down_revision=0016_ontology_join）。

**前端新增**：
- `src/types/dataQuality.ts`：6 个类型 + `DataQualityRuleListParams`。
- `src/api/dataQuality.ts`：5 个 API（listRules / getRule / createRule / updateRule / disableRule）。
- `src/pages/DataQualityPage.tsx`：表格 + Modal 表单 + 类型/严重级别过滤；rule_code 字段编辑时禁用。

**前端改动**：
- `src/App.tsx`：新增 `path="data-quality"` 路由。
- `src/components/common/AppLayout.tsx`：左侧导航加 `/data-quality`。
- `src/i18n/zh-CN.ts` + `en-US.ts`：新增 `appLayout.menu.dataQuality`、`pages.dataQuality`、`dataQuality.*` 16 条键。

**复用既有模式**：
- ORM：`TimestampMixin` + `BigIntPk` + UniqueConstraint 命名（沿用 `feat-local-import`）。
- 路由：APIRouter + `getDb` 依赖 + `Depends` 工厂注入 service（沿用 `local_import` 模式）。
- Pydantic：`CamelModel` 基类 + `alias_generator=to_camel` + snake_case 字段名。
- 前端：`httpClient`（axios 拦截器统一信封）+ Antd Table/Modal/Form + i18n 双语。

## 6. 测试

**后端集成测试（真实 PG 5433 + 完整 API 链路，10 用例）**：
`app/tests/integration/test_data_quality_api.py`：
- `test_list_empty` 空表返回 `[]`
- `test_create_and_list` 创建 + 列表，camelCase 契约校验
- `test_create_duplicate_code_conflict` rule_code 唯一性冲突返回 422
- `test_create_invalid_code_format_rejected` 小写编码被 Pydantic 正则拒绝（422）
- `test_create_threshold_out_of_range_rejected` threshold > 100 被拒绝（422）
- `test_get_by_id` 详情 200
- `test_get_not_found` 不存在返回 404
- `test_update_partial` 局部更新，未传字段保持原值
- `test_delete_soft` 删除返回 204，列表 `enabledOnly=true` 不再返回
- `test_filter_by_type_and_table` 按 `ruleType` / `targetTable` 过滤

**覆盖率**：
- `app/services/data_quality_service.py` 46 stmts / 0 miss = **100%**
- `app/api/v1/data_quality.py` 28 stmts / 0 miss = **100%**
- 联合覆盖率 100%（≥ 80% 阈值）

**前端**：`npx tsc --noEmit` clean；`npm test` 270 passed (29 files)。

**回归验证**（避免影响既有模块）：同步跑 `test_term_dictionary_api.py` + `test_local_import.py` 24 passed。

## 7. 安全审查

- 鉴权：`router = APIRouter(dependencies=[Depends(getCurrentUser)])`（与既有 API 一致）
- SQL：纯 ORM，无手写 SQL
- 输入校验：Pydantic 校验 rule_code 格式、threshold 范围、severity 枚举

## 8. 部署验证

```bash
cd backend
alembic upgrade head
TEST_DATABASE_URL=... uv run pytest app/tests/services/test_data_quality_service.py \
  app/tests/integration/test_data_quality.py -v
# 预期：≥18 passed
```

## 9. 已知缺口

- 本期不实现评估执行（Phase 1.2 `feat-data-quality-evaluator`）
- 本期不实现评分（Phase 1.3 `feat-data-quality-score-model`）
- 本期不与 NL2SQL 集成（Phase 1.4 `feat-nl2sql-quality-integration`）

> **Phase 1.2 后续追加**：评估执行需要按 `datasource_id` 找业务库适配器，因此在 0018
> 迁移中追加 `data_quality_rule.datasource_id`（NOT NULL + FK ON DELETE RESTRICT）。
> 表结构在 Phase 1.1 与 1.2 之间发生了不可忽视的扩展，code review 应一并核查
> `feat-data-quality-evaluator/summary.md` §3 的迁移说明。

## 10. 关联

- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 1.1
- 评估：`docs/data-knowledge/系统差距评估报告.md`
- 采购域：`docs/data-knowledge/采购域.md` §七（数据质量）
- 既有模式：`backend/alembic/versions/0016_ontology_join.py`、`Harness/changes/feat-local-import/summary.md`
- 后续：
  - `feat-data-quality-evaluator`（Phase 1.2，已完成 2026-08-30）— 消费本表做评估
  - `feat-data-quality-score-model`（Phase 1.3，已完成 2026-08-30）— 消费本表 `target_table` + `rule_type` 聚合评分

## 11. 事故记录：live 库迁移不同步（2026-08-30）

- **现象**：前端数据质量页打开报错，`GET /api/v1/data-quality/rules` 500
  （Internal Server Error）。
- **根因**：0017（建表）/0018（datasource_id）/0019（评分表）迁移只应用在测试库
  `qa_metadata_test`（version=0018→0019），live 库 `qa_metadata` 停在
  `0016_ontology_join`，`data_quality_rule` 表不存在 → `SELECT` 抛
  `UndefinedTableError` → 500。集成测试连的是迁移齐全的测试库，因此全绿，掩盖了缺表。
- **修复**：对 live 库执行 `DATABASE_URL=...alembic upgrade head`（应用
  0017/0018/0019），`alembic_version` 升到 `0019_dq_score`，接口转绿（HTTP 200，返回 `[]`）。
- **防复发**：见 [开发流程规范](../rules/开发流程规范.md)「数据库迁移同步（部署门禁）」；
  `deploy-verify` 技能追加步骤 0（迁移版本校验）与冒烟项 6（数据质量端点）。
  此后数据模型变更必须对每个目标环境应用迁移，并对新端点冒烟后再视为可部署。
