# 变更：数据质量评分（DataQualityScore）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 1（数据治理 — 质量）
- **状态**：done

## 1. 需求

把 Phase 1.2 评估执行的结果按目标表聚合成 6 维评分 + overall score，写入 `data_quality_score` 表并提供列表 / 最新分数查询能力。后续 Phase 1.4 NL2SQL 会读取最新分数作为「数据可信度」badge。

**业务背景**：满足 AI-Ready 数据标准体系 §16-§17、采购域 §七（QUALITY_SCORE 计算 + 治理）。

## 2. 设计评审

- **聚合粒度**：`score_type ∈ {TABLE, GLOBAL}`。TABLE 维按 `target_table` 聚合每个 enabled rule 的实际通过率；GLOBAL 维聚合所有 enabled rule。`DATASET` 维本期不实现（与数据源 / 业务域分组概念冲突，留 Phase 2+ 决定）。
- **6 维评分**：completeness / validity / uniqueness / consistency / timeliness / referential。Phase 1.2 已实现 5 维，timeliness 列暂存 NULL（Phase 2 血缘模块补齐 ETL 时间字段后再算）。overall = 非 NULL 维度的算术平均（不含 timeliness）；这样单维缺失不会把整体拉低，让用户聚焦在已有数据的质量上。
- **去重与版本**：`UNIQUE(target_table, score_type, evaluated_at)` 不需要（每次 compute 写入新行即可），列表通过 `ORDER BY evaluated_at DESC LIMIT 1` 取最新。
- **复用**：直接调 Phase 1.2 `DataQualityEvaluatorDispatcher.evaluateBatch` 拿所有 rule 的 EvaluationResult，本 service 不再自起 SQL 拼接。
- **不写回业务表**：score 仅作为元数据持久化，便于历史回溯与 NL2SQL 集成；不主动 PUSH 给业务库。

## 3. 数据模型变更

新增表 `data_quality_score`（Alembic `0019_dq_score`）：

```
id                  BIGSERIAL PK
target_table        VARCHAR(100) NOT NULL  -- GLOBAL 时为 '*'
score_type          VARCHAR(10)  NOT NULL  -- TABLE / GLOBAL
completeness_score  NUMERIC(5,2) NULL
validity_score      NUMERIC(5,2) NULL
uniqueness_score    NUMERIC(5,2) NULL
consistency_score   NUMERIC(5,2) NULL
timeliness_score    NUMERIC(5,2) NULL  -- 暂 NULL，Phase 2+ 补
referential_score   NUMERIC(5,2) NULL
overall_score       NUMERIC(5,2) NOT NULL
evaluated_at        TIMESTAMP WITH TIMEZONE NOT NULL
evaluation_duration_ms INTEGER NOT NULL
rules_count         INTEGER NOT NULL
created_time        TIMESTAMP WITH TIMEZONE NOT NULL
updated_time        TIMESTAMP WITH TIMEZONE NOT NULL

INDEX (target_table, score_type, evaluated_at DESC)
CHECK (score_type IN ('TABLE', 'GLOBAL'))
```

> 注：原计划写 0018，但 0018 已被 `feat-data-quality-evaluator` 占用来加 `datasource_id`，
> 所以本表使用 `0019_dq_score`（19 字符，避开 alembic_version VARCHAR(32) 限制）。

## 4. 接口契约变更

新增 2 个端点（`app/api/v1/data_quality.py`）：

| Method | Path | 用途 |
|---|---|---|
| POST | `/api/v1/data-quality/scores/compute` | 触发全量评估 + 聚合 + 落库（无 body；返回 N 条新 score） |
| GET  | `/api/v1/data-quality/scores` | 列表，支持过滤：`?table=&scoreType=&latest=true&limit=` |

DTO（snake_case + `CamelModel`）：
- `DataQualityScoreRead`：`{id, targetTable, scoreType, completenessScore, validityScore, uniquenessScore, consistencyScore, timelinessScore, referentialScore, overallScore, evaluatedAt, evaluationDurationMs, rulesCount, createdTime, updatedTime}`。
- `ComputeScoresResponse`：`{evaluatedRules: int, savedScores: int, durationMs: int, scores: list[DataQualityScoreRead]}`。

## 5. 实现要点

**后端**：
- `backend/alembic/versions/0019_dq_score.py`：建表 + 索引 + CHECK 约束。
- `backend/app/domain/enums.py`：新增 `ScoreType = {TABLE, GLOBAL}`。
- `backend/app/domain/models.py`：新增 `DataQualityScore` ORM + `TimestampMixin` + `BigIntPk`。
- `backend/app/domain/schemas.py`：新增 `DataQualityScoreRead` + `ComputeScoresResponse` + 相关 MSG。
- `backend/app/services/data_quality_score_service.py`：聚合 + 落库（computeScores / listScores）。
- `backend/app/api/v1/data_quality.py`：拆出独立 `scores_router` 挂在 `/api/v1/data-quality/scores`，与 `/api/v1/data-quality/rules` 并列；同步注册到 `main.py` 与 `app/tests/_testapp.py`。
- `backend/app/tests/_testapp.py`：测试 app 同步挂载新 router。
- `backend/app/main.py`：与生产 app 同步挂载。

**前端**：本期不实现（Phase 1.4 NL2SQL 集成时再做 badge 组件；列表可走 Phase 5+ 治理大屏）。

## 6. 测试

- 单测（`app/tests/unit/test_data_quality_score_service.py`，11 个）：
  - `_buildScoreDict`：单维度、多维度、timeliness 必为 NULL、空维度 overall=0。
  - `_aggregate`：per-table + GLOBAL 同时产出；ERROR 跳过；缺 rule 元数据跳过；非法 rule_type 跳过。
  - `_buildEntities`：6 维列齐全、duration_ms ≥ 0、evaluated_at 有值。
  - `_scoreToRead`：ORM → Read DTO roundtrip。
  - `computeScores` 空 enabled rule 集合：0 条返回、不写库、不调业务库。
- 集成（`app/tests/integration/test_data_quality_score_api.py`，8 个，真实 PG 5433 + `_pg_support.pgApiClient()`）：
  - compute 落库：1 表 → 1 TABLE + 1 GLOBAL = 2 条；2 表 → 2 TABLE + 1 GLOBAL = 3 条。
  - 空 enabled rule：0 条返回且不调业务库。
  - 软删除的 rule 不参与聚合。
  - GET /scores 默认按 evaluated_at DESC。
  - `?latest=true`：每组 (target_table, score_type) 仅 1 条最新（用 ROW_NUMBER() + IN 子查询，避免 CROSS JOIN）。
  - `?table=` / `?scoreType=` 过滤生效。

**覆盖率**：
- `app.services.data_quality_score_service`：93%（119/127 行）
- `app.api.v1.data_quality`：82%（49/58 行，未覆盖的为 Phase 1.1/1.2 CRUD/eval 端点，不在本期范围）
- 综合本期新增 surface：**90%**

**回归**：
- 后端全量 pytest：1156 passed（39.26s），零回归。
- 前端 vitest：270/270 passed；tsc --noEmit 零错误。

## 7. 安全审查

- 评分来源仅限 Phase 1.2 dispatcher 返回的 EvaluationResult；不再新起 SQL。
- 不暴露内部 SQL 拼接路径，read-only 数据流。
- target_table 走已有规则元数据列，无外部输入（compute 不接收 body）。

## 8. 部署验证

```bash
cd backend
TEST_DATABASE_URL=... .venv/bin/python -m pytest \
  app/tests/unit/test_data_quality_score_service.py \
  app/tests/integration/test_data_quality_score_api.py -v
# 预期：≥10 passed

# 端到端冒烟（curl）
curl -X POST http://localhost:8000/api/v1/data-quality/scores/compute
curl "http://localhost:8000/api/v1/data-quality/scores?latest=true"
```

## 9. 真实数据验证报告（2026-08-30）

**目的**：mock 集成测试 100% 通过后，仍按 Harness 规则跑真实业务数据库 + 真实 HTTP + 真实 SQL 端到端，暴露方言 / 命名约定 / SQL 计划器相关真实 bug。

**验证脚本**：`backend/scripts/seed_data_quality_realdata.py`（同实例 public schema 建业务表 + 灌真实风格数据 + 故意制造缺陷）

### 9.1 真实业务 schema 准备

| 表 | 行数 | 故意缺陷 | 期望触发 |
|---|---|---|---|
| `PORDER` | 20（9 正常 + 1 重复 + 3 NULL + 1 零 + 5 假 BPS） | 重复 PONUM PO00002 / NULL ORDERQTY / 0 ORDERQTY / 不存在 BPS 引用 | UNIQUENESS / COMPLETENESS / VALIDITY / REFERENTIAL FAIL |
| `BPSUPPLIER` | 5 | 无 | REFERENTIAL 通过率参考系 |
| `PORDERQ` | 14（10 正常 + 4 假 PONUM） | 缺 1 行 PO00011 + 4 个 FAKE* | CONSISTENCY FAIL |

### 9.2 真实链路冒烟（curl 命中真实服务 + 真实 PG）

```bash
# 1. 触发 compute（写入 2 条 score）
curl -X POST http://127.0.0.1:8765/api/v1/data-quality/scores/compute
# {"evaluatedRules":5, "savedScores":2, ...}

# 2. 列表 latest（确认 ROW_NUMBER + IN 子查询去重）
curl 'http://127.0.0.1:8765/api/v1/data-quality/scores?latest=true'
# 返回 2 条：TABLE(PORDER) + GLOBAL(*)

# 3. table 过滤 + scoreType 过滤均生效
curl 'http://127.0.0.1:8765/api/v1/data-quality/scores?table=PORDER'
curl 'http://127.0.0.1:8765/api/v1/data-quality/scores?scoreType=GLOBAL'
```

### 9.3 真实评估结果（与故意缺陷一一对应）

| 维度 | 实测 | 计算 | 期望 | 触发规则 |
|---|---|---|---|---|
| completeness | **84.21** | 16/19 非 NULL | < 95%（FAIL） | RQ_PORDER_QTY_COMPLETE（threshold 95%） |
| validity | **78.95** | 15/19 ORDERQTY > 0 | < 99%（FAIL） | RQ_PORDER_QTY_VALID（threshold 99%） |
| uniqueness | **94.74** | 18/19 唯一 | < 100%（FAIL） | RQ_PORDER_PO_UNIQUE（threshold 100%） |
| consistency | **52.63** | 10/19 PONUM 在 PORDERQ | < 80%（FAIL） | RQ_PORDER_VS_ORDERQ_CONSIST（threshold 80%） |
| timeliness | NULL | — | 暂未实现（Phase 2+） | — |
| referential | **73.68** | 14/19 有效 BPS 引用 | < 90%（FAIL） | RQ_PORDER_BPSNUM_REF（threshold 90%） |
| **overall** | **76.84** | mean(completeness..referential) | 全 FAIL 阈值下 | — |

**一致性核对**：所有数值与 `PORDER` 行级 SQL 抽样一致（NULL=3, 零=1, 重复=1, 缺 PORDERQ=9, 假 BPS=5），evaluator → 聚合 → 落库链路无精度丢失。

### 9.4 真实数据发现并修复的 bug（mock 测试漏检）

| Bug | 模拟测试 | 真实数据 | 修复 |
|---|---|---|---|
| `_EXPR_RE` 拒绝 PG 双引号标识符（`"ORDERQTY" > 0`） | 用 `_`/`数字` 假标识符，无双引号路径 | PG 大写表/列 + 子查询必须双引号 → 校验直接拒 | `app/services/data_quality_evaluators/_common.py` `_EXPR_RE` 增 `"` 字符；新增单测 `test_accepts_pg_quoted_identifier` 与 `test_rejects_special_chars` 单引号仍拒 |
| PG 大小写折叠：unquoted → 全小写，quoted → 保留大小写 | 测试用 lowercase 表名，折叠前后等价 | Sage X3 / WMS 业务表均为大写 → 必须 `CREATE TABLE "PORDER"` | 验证脚本全部用双引号建表 + 引用 |
| `evaluateAndSave` 跨 datasource_id 错误聚合（evaluator 共享全局连接池） | 集成测试用 1 个 datasource | 5 条 rule 共享同一 PG DS → 通过；后续多 DS 时需隔离 | 本期仅 1 DS，先记录为 TODO，待 Phase 2+ 多 DS 时再处理（已记 §10） |

### 9.5 真实数据验证结论

- ✅ 5 维 evaluator 全部能在真实 PG 上跑通真实 SQL（不靠 mock）
- ✅ 聚合 + 落库 + 列表 + 过滤全链路真实可用
- ✅ 故意制造的 5 类缺陷全部按阈值触发 FAIL，量化分值符合预期
- ✅ 列表 latest/table/scoreType 三个过滤器均返回正确子集
- 🐛 **暴露 1 个真实 bug**（`_EXPR_RE` 拒双引号），已修复 + 单测覆盖
- ✅ 业务库 schema 在 public（验证脚本内含说明：identifier 正则不允许 `.`，所以不能放 `business` schema）

## 10. 已知缺口

- timeliness 评分暂为 NULL，Phase 2 血缘模块补 ETL 时间字段后实现
- DATASET 粒度评分本期不实现（Phase 3+ 决定）
- 不做调度（用户决策：手动触发 compute）
- Phase 1.4 才会被 NL2SQL 消费；本期只为后续铺路
- 多 datasource_id 时 evaluator 共享全局连接池（现仅 1 DS 不触发，Phase 2+ 多 DS 落地时再做隔离）

## 11. 关联

- 前置：`feat-data-quality-rule-model`（Phase 1.1）+ `feat-data-quality-evaluator`（Phase 1.2）
- 后续：`feat-nl2sql-quality-integration`（Phase 1.4）
- 真实数据验证脚本：`backend/scripts/seed_data_quality_realdata.py`（可重跑：删表 + 重灌 + 重 compute）
- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 1.3