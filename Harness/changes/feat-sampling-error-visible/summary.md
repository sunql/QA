# feat-sampling-error-visible (2026-09-15)

## 背景 / 痛点

评估报告「违规样本」表（`ViolationSampleTable`）即使有违规数据也可能显示空——

- `dispatcher.collectSamples` 在 dispatcher 内部 blanket `except Exception: logger.exception(...); return []`
- `sampleForRule` 看到 `samples=[]` 就 `return None` 不写 DB 行
- 列表接口 `GET /reports/{id}/samples` 自然拿不到这行
- 用户看到「违规总数 N 条但违规样本 0 条」不知道是：
  - 真没命中（合法）
  - sampler 抛错（Oracle ORA-00933 / 列不存在 / LIMIT 语法等）

旧日志虽然记了 `logger.exception` 但运维/用户都看不见。

memory `eval-dispatcher-silent-except` 复盘：Oracle ORA-00933 这样埋 3 个月。

## 设计决策

### 端到端信号传递：`tuple[list, str | None]`

把 `dispatcher.collectSamples` 签名从 `list[dict]` 改成 `tuple[list[dict], str | None]`：
- 成功：`({pk...}, None)`
- 配置错误（rule=None / unknown type / 缺 datasource）：`([], None)` —— 调用方不写行（与旧语义一致）
- sampler 抛错：`([], str(exc)[:500])` —— 调用方写 `sampling_error` 字段

`str(exc)[:500]` 截断避免 DB 字段无界。

### 新增 `sampling_error` 列

`data_quality_violation_sample.sampling_error TEXT NULL`（alembic 0075）。
- NULL = 采样成功或 0 命中（旧数据兼容）
- 非 NULL = 异常文本（≤500 chars）

### `sampleForRule` 永远写行（除非真的 0 命中且无错）

```python
samples, error = await dispatcher.collectSamples(...)
if not samples and not error:
    return None  # 0 命中：与旧行为一致，不写行
row = DataQualityViolationSample(
    ...
    sample_size=len(samples),
    sample_pk_values=[{"pk": s} for s in samples],
    sampling_error=error,
)
session.add(row)
```

这样 `listForReport` 拿得到所有尝试过的规则——前端能区分「真没数据」与「采样失败」。

### 前端渲染优先级

`ViolationSampleTable` 的 samplePkValues 列渲染：

```tsx
if (record.samplingError) {
  return <Tooltip title={record.samplingError} color="red">
    <Tag color="red">⚠ 采样失败</Tag>
  </Tooltip>;
}
// 否则走原有 PK Tag 列表 / +N 更多 渲染
```

失败优先：让用户立刻知道是 SQL / 适配器问题，而不是「真没数据」。

## 文件改动

### 后端

| 文件 | 改动 | 行数 |
|---|---|---|
| `backend/alembic/versions/0075_dq_sample_error.py` | 新增 migration：`ALTER TABLE ... ADD COLUMN sampling_error TEXT NULL` | 23 |
| `backend/app/domain/models.py:1146-1149` | `DataQualityViolationSample` 加 `sampling_error: Mapped[str \| None] = mapped_column(Text, nullable=True)` | +4 |
| `backend/app/domain/schemas.py:3248-3267` | `ViolationSampleRead` 加 `sampling_error: str \| None = None` | +3 |
| `backend/app/services/data_quality_evaluator.py:235-285` | `collectSamples` 返 `tuple[list[dict], str \| None]`：成功/配置错误/运行错误三分支 | +15 / -10 |
| `backend/app/services/data_quality_violation_sample_service.py:34-77` | `sampleForRule` 总是写行（0 命中无错除外），落库 `sampling_error` | +12 / -7 |
| `backend/app/tests/integration/test_evaluation_violation_sample.py` | 适配 tuple 返回；新增 2 个测试：sampler 抛错返 error + sampleForRule 落库 error 行 | +80 |
| `backend/app/tests/integration/test_evaluation_report_service.py:454-456` | `_FakeDispatcher.collectSamples` mock 改返 `([], None)` | +2 |

### 前端

| 文件 | 改动 | 行数 |
|---|---|---|
| `frontend/src/types/evaluationReport.ts:67-73` | `ViolationSampleRead` 加 `samplingError?: string \| null` | +3 |
| `frontend/src/components/ViolationSampleTable.tsx:74-99` | 渲染时 `samplingError` 非空优先显示「采样失败」红色 Tag + Tooltip | +18 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | `samplesTable.samplingFailed` i18n key | +2 |

## 验收

```bash
# 后端
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/ -k "eval or report or violation_sample or sample_service"
# 108 passed

# 前端
cd frontend
npx tsc --noEmit          # 0 errors
npx vitest run src/tests/ViolationSampleTable.test.tsx  # 11/11
```

浏览器验收：
1. 进 `/data-quality/reports/:id`，展开「违规样本」accordion
2. 正常规则行：PK Tag 列表（≤5 全显 / >5 前5 +「+N 更多」+ Tooltip 全量）
3. **新增** sampler 失败的规则行：`采样失败` 红色 Tag + hover Tooltip 看完整异常文本（如 `ORA-00933: SQL command not properly ended`）

## 风险 & 回滚

| 风险 | 缓解 |
|---|---|
| `collectSamples` 签名 breaking change（5 个测试 mock + 1 个真实调用） | 改 2 个文件、跑 27 个原测试全过、新增 2 个测试覆盖失败路径 |
| 老 snapshot 无 `sampling_error` 字段（前端 undefined） | Pydantic 默认 `None` + TS 可选字段；undefined 走正常 PK 渲染路径 |
| `str(exc)` 可能含敏感信息（密码、表结构）放 DB | 截断 500 chars；评估器只对业务库表跑 SQL，错误多为 ORA 错误码 + 标识符，不含连接串 |
| alembic 0075 文件名 23 字符 ≤ varchar(32) | 满足 memory `alembic-version-filename-32-char-limit` |
| 下游使用者若 stub 了 `collectSamples` 返 `[]`（不走 tuple）会 TypeError | 2 个测试已修；外部代码 search 过无其他调用方 |

回滚：
- 删 migration `0075_dq_sample_error.py`
- 改 `dispatcher.collectSamples` 返 `[]`（cast）
- 改 `sampleForRule` 删 `sampling_error` 字段
- 改 schema + i18n + 前端

## 关键文件路径速查

**后端（改 5 文件 + 1 migration）**:
- `backend/alembic/versions/0075_dq_sample_error.py:31-44` — migration 主改动
- `backend/app/services/data_quality_evaluator.py:235-285` — `collectSamples` 签名 + 三分支
- `backend/app/services/data_quality_violation_sample_service.py:34-77` — `sampleForRule` 总写行
- `backend/app/domain/models.py:1146-1149` — model 加列
- `backend/app/domain/schemas.py:3259-3263` — Pydantic 加字段

**后端（测试 2 文件）**:
- `backend/app/tests/integration/test_evaluation_violation_sample.py:128-260` — tuple 适配 + 2 个新测试
- `backend/app/tests/integration/test_evaluation_report_service.py:454-456` — mock 适配

**前端（改 4 文件）**:
- `frontend/src/components/ViolationSampleTable.tsx:74-99` — 渲染优先级
- `frontend/src/types/evaluationReport.ts:67-73` — TS 类型
- `frontend/src/i18n/zh-CN.ts:899-913` / `en-US.ts:891-905` — i18n key

**复用模式（不改）**:
- `backend/app/services/data_quality_evaluators/_common.py:230-249` — `sample_limit_clause` 已 dialect-aware
- `backend/app/infrastructure/business_db_pool.py` — adapter quote_identifier