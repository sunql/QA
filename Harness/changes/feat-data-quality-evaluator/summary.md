# 变更：数据质量评估执行器（5 维校验）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 1（数据治理 — 质量）
- **状态**：done

## 1. 需求

实现 5 维数据质量评估执行器（COMPLETENESS / VALIDITY / UNIQUENESS / CONSISTENCY / REFERENTIAL），对 Phase 1.1 定义的 `DataQualityRule` 跑出实际通过率，供 AI 应用读取 `QUALITY_SCORE` 判断结论可信度（满足采购域 §七）。

TIMELINESS 留 Phase 2 血缘模块（依赖最近评估时间戳，需要 lineage 自动提取的 ETL 时间字段，本期不实现）。

## 2. 设计评审

- **复用 SQL 只读护栏**：每个 evaluator 构造的 SQL 统一通过 `BusinessDbAdapter.execute_read_only`，自动享受 `_assert_read_only` + 行数上限 + 超时三重保护。
- **SQL 注入防护（双层）**：
  - **第一层**：identifier 强校验。`target_table` / `target_column` / 解析自 rule_expression 的列名必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`（不含数字开头的 CJK，留 Phase 2 处理 ERP 库非 ASCII 场景）。失败返回 422。
  - **第二层**：表达式白名单。VALIDITY / CONSISTENCY 的 `rule_expression` 仅允许 `[\w\s\.\(\)\>\<\=\!\,\*\+\-\/]+`（标识符 + 比较 + 算术 + 括号 + 小数点）；并对禁用关键字做大小写不敏感的子串 / 词边界匹配。失败返回 422。
- **REFERENTIAL 编码**：复用 `rule_expression` 存结构化 `REF <ref_table>.<ref_column>`（正则大小写不敏感），避免新增 ref_table/ref_column 列触发表结构变更。
- **dispatcher**：单例 `DataQualityEvaluatorDispatcher` 按 `rule_type` 分发到对应 evaluator；返回 `EvaluationResult`（pass_rate 0-100 + total/passed + 实际值）。评估失败（缺数据源 / 不支持类型 / 异常）走 `_errorResult` 返回 status=ERROR + message。
- **规则绑定数据源**：Phase 1.1 表 `data_quality_rule` 新增 `datasource_id` 列（迁移 0018 + ON DELETE RESTRICT FK），避免评估时再去业务 metadata 推断连接信息。编辑模式下数据源下拉禁用，保证「规则绑定业务库」语义不漂移。
- **不写 score 表**：评估执行结果仅作为 API 响应返回；持久化由 Phase 1.3 `feat-data-quality-score-model` 接管。

## 3. 数据模型变更

| 文件 | 变更 |
|---|---|
| `backend/alembic/versions/0018_dq_datasource.py` | 新增：`data_quality_rule.datasource_id BIGINT NOT NULL` + FK to `data_source(id)` ON DELETE RESTRICT + 索引 + 历史数据回填到首个 active 数据源 |
| `backend/app/domain/models.py` | `DataQualityRule` 加 `datasource_id` 字段 + `ForeignKeyConstraint` 入 `__table_args__` |
| `backend/app/domain/schemas.py` | `DataQualityRuleCreate` / `Update` / `Read` 加 `datasource_id` |
| `backend/app/domain/error_messages.py` | 加 `MSG_SCHEMA_DQ_DATASOURCE_ID` |

注：0018 命名被截到 `0018_dq_datasource`（19 字符），因为 `alembic_version.version_num` 列是 VARCHAR(32)，原 `0018_data_quality_rule_datasource` 会触发 `StringDataRightTruncationError`。

## 4. 接口契约变更

新增 2 个端点（`app/api/v1/data_quality.py`）：

| Method | Path | 用途 |
|---|---|---|
| POST | `/api/v1/data-quality/rules/{ruleId}/evaluate` | 评估单条规则（按 datasource_id 找业务库适配器 + targetTable 查询） |
| POST | `/api/v1/data-quality/rules/evaluate-batch` | 批量评估（body: `ruleIds: int[]`，缺失 rule_id 返回 ERROR 不抛异常） |

DTO（snake_case + `CamelModel`）：
- `EvaluationResult`：`{ruleId, ruleCode, ruleType, datasourceId, totalCount, passedCount, passRate, status: PASS/FAIL/ERROR, evaluatedAt, durationMs, message}`。
- `EvaluateBatchRequest` / `EvaluateBatchResponse`：`ruleIds[]` + 多个 `EvaluationResult` + 整体 `summary{summaryTotal, summaryPassed}`。

前端契约变化：`DataQualityRuleCreate` / `Update` / `Read` 新增 `datasourceId: number`（必填 / 可选 / 只读），DataQualityPage 表单加数据源下拉（编辑时禁用）、表格新增「数据源」列。

## 5. 实现要点

**后端**：
- `app/services/data_quality_evaluator.py`：dispatcher + 异常兜底 + `_safeRuleType` + `_resolveEvaluatedAt` 助手。
- `app/services/data_quality_evaluators/__init__.py`：保持空 `__all__`（避免与 dispatcher 形成循环导入；dispatcher 用绝对路径导入子模块）。
- `app/services/data_quality_evaluators/_common.py`：`validate_identifier` / `validate_expression` / 关键字阻断（含符号类 vs alnum 类的双匹配策略）。
- `app/services/data_quality_evaluators/{completeness,validity,uniqueness,consistency,referential}.py`：5 个独立 evaluator，每个只暴露 `async def evaluate(rule, adapter) -> (total, passed)`，便于单测与并行扩展。
- `app/services/data_quality_service.py`：写入 `datasource_id`。
- `app/api/v1/data_quality.py`：挂 2 端点，DI 注入 dispatcher。
- 复用：`app/infrastructure/business_db_pool.py:execute_read_only`（SQL 安全 + 行数 + 超时）。

**前端**：
- `frontend/src/types/dataQuality.ts`：`DataQualityRule{,Create,Update}` 加 `datasourceId: number`。
- `frontend/src/pages/DataQualityPage.tsx`：表单数据源 Select（联动 `listDataSources(true)`）、表格新增数据源列（按 id 反查 name）。
- `frontend/src/i18n/{zh-CN,en-US}.ts`：`dataQuality.{datasource,datasourcePlaceholder}` 两个 key。

## 6. 测试

- `app/tests/unit/test_data_quality_evaluators.py`：33 用例，覆盖 5 个 evaluator 的 pass/fail / 必填校验 / 空结果 / `_common` 白名单（含大小写、关键字边界、符号类 vs alnum 类关键字）。
- `app/tests/unit/test_data_quality_evaluator_dispatcher.py`：6 用例，覆盖 dispatcher 自身（规则缺失 / 数据源缺失 / 不支持 type / 异常捕获 / 空 batch / 混合 batch）。
- `app/tests/integration/test_data_quality_api.py`：10 用例（Phase 1.1 CRUD 全链路 + 数据源依赖）。
- `app/tests/integration/test_data_quality_eval_api.py`：7 用例（5 维评估 / 批量 / 缺失规则 → ERROR / 空 batch）。
- 业务库 adapter 通过 monkeypatch `app.services.data_quality_evaluator.get_adapter` 注入 fake，避免依赖外部 DB；规则元数据走真实 PG。
- 目标覆盖率：dispatcher 95%、evaluator 子模块 94-100%、综合 92%。

## 7. 安全审查

- identifier 白名单 + expression 白名单双重校验（含禁用关键字大小写不敏感 + 词边界 / 子串双策略）。
- SQL 走 `execute_read_only`（DDL/DML 自动拒绝）。
- 评估 SQL 不引入 ORM 参数化盲区（identifier 是 SQL 标识符，必须静态拼接 → 必须强校验）。
- `datasource_id` FK ON DELETE RESTRICT 阻止评估历史悬空。

## 8. 部署验证

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  .venv/bin/python -m pytest \
    app/tests/unit/test_data_quality_evaluators.py \
    app/tests/unit/test_data_quality_evaluator_dispatcher.py \
    app/tests/integration/test_data_quality_api.py \
    app/tests/integration/test_data_quality_eval_api.py
# 结果：56 passed, coverage ≥92%

cd frontend
npx tsc --noEmit                    # 0 errors
npm test                            # 270 passed
```

## 9. 已知缺口

- TIMELINESS 留 Phase 2 血缘模块
- 不写 score 历史表（Phase 1.3）
- 不做调度（用户决策：一次性触发 + 手动）
- 中文/CJK 标识符不支持（避免 Oracle/PG 标识符解析差异引入新风险；Phase 2 单独决策）
- 数据源删除路径走硬阻止（FK RESTRICT），后续若需要「软删除数据源」需要在 DataSource 层加 is_archived 状态字段（不在本期范围）

## 10. 关联

- 前置：`feat-data-quality-rule-model`（Phase 1.1）
- 后续：`feat-data-quality-score-model`（Phase 1.3）+ `feat-nl2sql-quality-integration`（Phase 1.4）
- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 1.2