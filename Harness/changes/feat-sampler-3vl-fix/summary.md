# feat-sampler-3vl-fix (2026-09-15)

## 背景 / 痛点

2026-09-15 用户报告 002：「校验 ITF_ID」报告 regenerate 后，**违规总数 568 条但违规样本 0 条**。

即使已经加了 `feat-sampling-error-visible` 让 sampler 失败对前端可见，**sampler 仍然没报错**——它返回空 list、`sampleForRule` 视为「真无命中」不写行。

### 根因：SQL 三值逻辑（3VL）不一致

| 路径 | SQL | 结果 |
|---|---|---|
| evaluator (rule 286) | `SELECT COUNT(*), SUM(CASE WHEN (expr) THEN 1 ELSE 0 END) FROM mdmtoerp` | total=1268, passed=700 |
| sampler (rule 286, 旧) | `SELECT ITF_ID FROM mdmtoerp WHERE NOT (expr) LIMIT 5` | **0 行** |

业务表 568 条违规行全来自 `expr IS NULL`（列含 NULL）——SQL 三值逻辑下 `NOT (NULL) = NULL`，`WHERE NOT (expr)` 不匹配 NULL 行。evaluator 用 `CASE WHEN (expr) THEN 1 ELSE 0 END` 把 NULL 行算 0 = 不通过。两条路径的「违规」定义不一致。

### 三条连锁 bug

修复此 bug 过程中暴露 3 个相关问题：

1. **Sampler `WHERE NOT (expr)` 漏 NULL 行** —— 真正根因（validity + consistency）
2. **`sampleForRule` 存 `[{"pk": <sampler_dict>}]` 而不是 `[{"pk": <row_id>}]`** —— 老代码把整个 sampler dict 塞进 `pk`，前端 `String(dict)` = `"[object Object]"`，用户看到一堆乱码 Tag。feat-sampling-error-visible 没碰这块
3. **dev DB 缺 `sampling_error` 列** —— 即使重建镜像跑新代码，INSERT 也会 UndefinedColumnError 把整批吞掉

## 设计决策

### 1. Sampler WHERE 补 `OR (expr) IS NULL`

`validity.py:69` + `consistency.py:73`：

```python
# 旧：
sql = f"SELECT {q_col} AS row_id FROM {q_tbl} WHERE NOT ({expr})"
# 新：
sql = (
    f"SELECT {q_col} AS row_id FROM {q_tbl} "
    f"WHERE NOT ({expr}) OR ({expr}) IS NULL"
)
```

语义对齐 evaluator：CASE WHEN (expr) THEN 1 ELSE 0 把 NULL 算不通过 → sampler 把 NULL 行也算违规样本。

**只动 VALIDITY + CONSISTENCY**：
- COMPLETENESS：用的是 `WHERE col IS NULL`，天然包含 NULL 行，无需改
- UNIQUENESS：用 `WHERE col IN (subquery HAVING COUNT>1)`，NULL 不在重复值集合里（SQL NULL 不等于自身），无需改
- REFERENTIAL：用 `WHERE col IS NOT NULL AND NOT EXISTS (...)`，显式排 NULL，无需改

### 2. sampleForRule 存 row_id（不是 dict）

```python
# 旧：
sample_pk_values=[{"pk": s} for s in samples]  # s 是 {row_id, target_column, target_table} dict
# 新：
sample_pk_values=[{"pk": s.get("row_id")} for s in samples]
```

storage shape: `[{"pk": "P001"}]` 与前端 `extractPkValues` 假设一致。`target_table` / `target_column` 是冗余的——已在行的 `target_table` / `target_column` 字段。

### 3. 前端向后兼容（不改数据库老行）

`ViolationSampleTable.extractPkValues` 检测两种形态：
- 新：`{"pk": "P001"}` → 取 pk 字符串
- 老：`{"pk": {"row_id": "P001", ...}}` → 取 `pk.row_id`

已存在的 9 条老 sample 行（id 1-8 之外的 mdmtoerp COMPLETENESS 规则）兼容可读。**不再写入老形态**——用户 regenerate 报告后只会产生新形态行。

### 4. dev DB 应用 0075 migration

测试框架 `_pg_support.py` 只跑测试库的 `alembic upgrade head`——dev / staging / prod 库需运维手动 apply。本 commit 之后必须在部署流水线加 `alembic upgrade head` 步骤。memory `alembic-migration-required` 已有此规则。

## 文件改动

### 后端

| 文件 | 改动 | 行数 |
|---|---|---|
| `backend/app/services/data_quality_evaluators/validity.py:68-74` | WHERE 补 `OR (expr) IS NULL` + 注释 | +5 / -2 |
| `backend/app/services/data_quality_evaluators/consistency.py:73-78` | 同上 | +5 / -1 |
| `backend/app/services/data_quality_violation_sample_service.py:74` | `{"pk": s.get("row_id")}` 替换 `{"pk": s}` | +5 / -1 |
| `backend/alembic/versions/0075_dq_sample_error.py` | feat-sampling-error-visible 迁移，dev 库升级 | — |

### 前端

| 文件 | 改动 | 行数 |
|---|---|---|
| `frontend/src/components/ViolationSampleTable.tsx` | `extractPkValues` 兼容老 dict 形态 | +6 / -2 |

### 不改

- 后端 evaluator SQL（用 `CASE WHEN THEN 1 ELSE 0` 已经把 NULL 行算违规）
- 其它 3 个 sampler（COMPLETENESS / UNIQUENESS / REFERENTIAL）语义已正确
- 老 sample 行不动——前端兼容读取

## 验收

### 后端

```bash
cd backend
TEST_DATABASE_URL=... uv run pytest app/tests/integration/ -k "eval or report or violation_sample or sample_service"
# 108 passed

uv run pytest app/tests/unit/test_data_quality_evaluators.py app/tests/unit/test_data_quality_evaluator_time_window.py
# 59 passed
```

### Dev DB

```bash
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  uv run alembic upgrade head
# Running upgrade 0074_dq_eval_report_progress -> 0075_dq_sample_error
```

### 容器重建

```bash
docker compose -f docker/docker-compose.yml build backend
docker compose -f docker/docker-compose.yml up -d --no-deps --force-recreate backend
```

### 业务验证

```bash
curl -X POST http://localhost:8000/api/v1/data-quality/reports/9/regenerate \
  -H "X-User-Id: admin" -H "X-User-Roles: admin" -H "X-Tenant-Id: default"
# → snapshot 重算，sample 写入

docker exec qa-postgres psql -U qa_user -d qa_metadata \
  -c "SELECT id, sample_size, sampling_error, sample_pk_values FROM data_quality_violation_sample WHERE report_id=9;"
#  id | sample_size | sampling_error | sample_pk_values
#  14 |          20 | <NULL>         | [{"pk": "1A17__"}, {"pk": "1A20__"}, ...]
```

## 风险 & 回滚

| 风险 | 缓解 |
|---|---|
| WHERE 子句变更让 sampler 取到比之前「合规」的 NULL 行——可能让老报告 sample 数变化 | 这是修复——之前漏算 NULL 行是 bug，不是回归 |
| 老 sample 行（id 1-8 等）的 dict 形态前端不识别 | extractPkValues 兼容读取 `pk.row_id`，已写测试覆盖 |
| dev 库未迁移导致新代码 INSERT 失败 | 本 commit 已 apply 0075；后续需在 deploy 脚本加 alembic upgrade head |
| 重建镜像期间服务短暂不可用 | docker compose up -d --force-recreate 整体重启 ~10s，业务可接受 |

回滚：
- 把 `WHERE NOT (expr) OR (expr) IS NULL` 改回 `WHERE NOT (expr)`（validity.py:69 + consistency.py:73）
- 把 `{"pk": s.get("row_id")}` 改回 `{"pk": s}`
- alembic downgrade -1

## 关键文件路径速查

**后端（改 3 文件）**:
- `backend/app/services/data_quality_evaluators/validity.py:68-74` — 3VL 主修复
- `backend/app/services/data_quality_evaluators/consistency.py:73-78` — 同上
- `backend/app/services/data_quality_violation_sample_service.py:74` — 存储格式

**前端（改 1 文件）**:
- `frontend/src/components/ViolationSampleTable.tsx:25-43` — extractPkValues 兼容老形态

**关联 memory**:
- [[eval-dispatcher-silent-except]] — silent-failure 复盘 + 3 次 commit 演进
- [[sampling-error-visible]] — 第一次让失败对前端可见
- [[violation-sample-display]] — 渲染 PK Tag 列表
- [[alembic-migration-required]] — 必须 apply migration