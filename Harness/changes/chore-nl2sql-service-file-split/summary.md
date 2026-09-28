# 变更：nl2sql_service.py 拆分为 8 文件（§2.4 LOW 文件拆分 Phase 1.1）

- **日期**：2026-09-28
- **作者**：Claude (with user direction)
- **Phase**：§2.4 LOW 文件拆分（Phase 1.1）
- **状态**：done

## 1. 需求

`backend/app/services/nl2sql_service.py` 达 **2589 行**，远超 800 行软上限。
按用户要求「缩小行数、拆成多个文件、不影响当前功能、利于后续扩展」重评估并拆分。

## 2. 设计评审（关键洞察）

实测推翻旧假设：`Nl2SqlService` **无实例状态**——无 `__init__`、无 `self.x =` 赋值，
所有 `self.xxx()` 均为类内跨方法调用，依赖全走参数。⇒ **不存在循环依赖**，
方法体 → 模块函数是纯机械替换（`self.xxx()` → `xxx()`），零语义变化。

命名沿用项目规范（`函数/变量：camelCase`），**所有函数/常量名保持不变**，只换模块位置。

## 3. 模块布局（DAG 单向，无环）

| 模块 | 行数 | 职责 |
|---|---|---|
| `nl2sql_dialects.py` | 158 | `SqlDialect` + 方言规则 + `resolveDialect` |
| `nl2sql_scope.py` | 96 | 范围感知行数限制（`_coerceRowLimit`/`_hasTimeScope`/`_applyScopeRowLimit`） |
| `nl2sql_refs.py` | 273 | 属性/类引用归一化 + 校验提示 + 公式属性提取 |
| `nl2sql_refine.py` | 380 | REFINE 捷径（纯代码 SQL 改写） |
| `nl2sql_service.py`（门面） | 474 | `SqlResult` + `Nl2SqlService` 薄委托 + `generateSql`/`parseSqlFromResponse`/`generateValidatedPlan` 编排 + re-export |
| `nl2sql_plan.py` | 498 | `generateQueryPlan`/`validatePlan`/`validateConnectivity`/`_finalizePlan`/`_parsePlanOutcome`/`_isEmptyPlan` |
| `nl2sql_prompts.py` | 517 | Plan/SQL 两阶段 Prompt 拼装 + `_sanitizeContext` 注入护栏 |
| `nl2sql_schema.py` | 540 | `buildSchemaText` + JOIN 图 + 净化原语 |

依赖链：`dialects`/`refs`/`refine`/`schema`（叶） → `prompts`/`scope`（中） → `plan`（上） → `service`（门面）。
`scope → refine`（`_hasExplicitRowIntent` 复用 `_extractLimit`），仍无环。

## 4. 门面 re-export（30+ 处既有 import 零改动）

门面底部显式 re-export 所有被测试/生产直接 import 的私有名：
`_sanitizeContext`、`_safeSchemaPrefix`、`_sanitizeSchemaField`、`_buildJoinGraph`、`_findJoinPath`、
`_resolveRefTable`、`_normalizePlanProperties`、`_splitCompoundRef`、`_applyScopeRowLimit`、`_coerceRowLimit`、
`_hasExplicitRowIntent`、`_hasTimeScope`、`_isEmptyPlan`、`_REFINE_MAX_LIMIT`、`_normalizeDate`、
`_NL2SQL_MAX_TOKENS`、`_NL2SQL_TRUNCATION_BACKOFF`、`_renderStatePart`、`_SQL_DIALECTS`、`DataSourceType`、`SqlResult`。

已核对 import surface 全覆盖：`chat_service.py`、`term_dictionary_service.py`、`multi_step_plan.py` +
10+ 测试文件。

## 5. 关键取舍：`generateValidatedPlan` 保留在门面

`generateValidatedPlan` 原本也迁入 `nl2sql_plan.py`，但测试
`test_property_ref_normalize.TestGenerateValidatedPlanRetryNormalizes` 通过
**monkeypatch 实例方法**（`service.generateQueryPlan = fake` / `service._finalizePlan = capture`）
隔离校验循环。若迁成模块函数，模块内 `generateQueryPlan(...)` 走全局函数名、绕过实例补丁，
该测试即失败。故 `generateValidatedPlan` 保留为门面**真实方法**、经 `self.` 分发
`self.generateQueryPlan` / `self.validatePlan` / `self._finalizePlan`，其余方法仍薄委托模块函数。

## 6. 验收

`TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test`
跑 nl2sql 相关单测：

- **402 passed**，唯一失败 `test_query_plan_generation::test_missing_fields_use_defaults`
  **为既有失败**（`{"target":"只有目标"}` 被方案B `_isEmptyPlan` 判空，原始 HEAD 同样失败）。

既有失败（与本次拆分无关，已在原始代码复现核对）：
- `test_chat_service.py`：36 失败（原始代码同样 36 失败，本拆分零新增）。
- `tests/unit/test_nl2sql_prior_cte.py`：4 失败（陈旧测试，先于 `prior_cte` WITH-less 契约变更 commit `72d34a1`）。
