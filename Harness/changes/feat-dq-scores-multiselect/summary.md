# 变更：数据质量评分 scope 多选（单数据源 + 多表 + 多规则类型）

- **日期**：2026-09-15
- **作者**：Claude
- **Phase**：数据质量（Phase 1.3 评分）
- **状态**：done
- **关联变更**：[feat-dq-scores-scope](../feat-dq-scores-scope/summary.md)（前置：scope 字段全 optional 单选）→ 本次扩为多选
- **后续**：无

## 1. 需求

旧 schema `ComputeScoresRequest` 三个 scope 字段 `datasource_id` / `target_table` / `rule_type` 全部单选。用户在评分页面期望「单数据源下，可同时勾选多张目标表 + 多个规则类型」做一次完整评分（旧 UI 只能选 1 个 targetTable + 1 个 ruleType，要重跑就得改 schema 客户端拆多次请求）。

## 2. 设计评审

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 后端 schema 改 `list[str]` / `list[RuleType]`，SQL 用 `IN` 组合 | 一处修改、前后端一致 | **选** |
| B. 前端拆 N 次请求，每个组合 1 次 evaluate | 不改后端 | 拒：重复评估、汇总状态难看；不可控；后端落库时同 `(target_table, GLOBAL)` key 被重复 INSERT 触发冲突 |
| C. 加新 endpoint `/scores/compute-multi` | 后端双套 API | 拒：YAGNI；schema 改名足以覆盖场景 |

最终：**A**。

- `datasource_id` 仍单选（业务上下文粒度足够）
- `target_tables: list[str] | None`：空 / None = 全表；非空 = IN 匹配
- `rule_types: list[RuleType] | None`：同上
- 三个字段仍 AND 组合；scope 命中 0 条规则 → 空响应、不写库、不写 GLOBAL（与旧行为一致）
- 向后兼容：旧 `target_table` / `rule_type` 单值字段直接废弃，无客户端传值所以无需 deprecation 过渡

## 3. 改动清单

### 3.1 后端

| 文件 | 改动 |
|------|------|
| `backend/app/domain/schemas.py` | `ComputeScoresRequest`：`target_table` / `rule_type` 单值字段 → `target_tables: list[str] \| None` / `rule_types: list[RuleType] \| None` |
| `backend/app/services/data_quality_score_service.py` | `computeScores` + `_listEnabledRules` 改用 `.in_(list(target_tables))` / `.in_(list(rule_types))`；空列表走全量 |
| `backend/app/api/v1/data_quality.py` | `computeDataQualityScores` 把 payload 透传给 service |
| `backend/app/tests/unit/test_data_quality_score_service.py` | `_fake_list_enabled` monkeypatch 签名同步改成 `target_tables` / `rule_types`；调用点同步更新 |
| `backend/app/tests/integration/test_data_quality_score_api.py` | 单值 `"targetTable"` 改 `"targetTables": ["PORDER"]` |

### 3.2 前端

| 文件 | 改动 |
|------|------|
| `frontend/src/types/dataQualityScore.ts` | `ComputeScoresRequest` 类型同步 |
| `frontend/src/pages/DataQualityPage.tsx` (`ScoresTab`) | 两个 Select 加 `mode="multiple"` + `maxTagCount="responsive"`；state 从 `string \| undefined` 改 `string[]`；payload 非空才传 |

## 4. 验收

```bash
# 后端
docker exec qa-backend bash -c 'cd /app && \
  TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata_test" \
  python3 -m pytest app/tests/unit/test_data_quality_score_service.py \
            app/tests/integration/test_data_quality_score_api.py \
            app/tests/integration/test_data_quality_score_audit.py -q'
# 期望：32 passed

# 前端
cd frontend && npx tsc --noEmit
# 期望：0 errors
```

手动：进 `/data-quality?tab=scores`，选 1 个数据源 + 2 张表 + 2 个规则类型 → 点「计算评分」 → 后端评估那批规则、聚合落库。

## 5. 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| 旧测试调用 `service.computeScores(target_table="X")` 报错 | 已同步更新 `_fake_list_enabled` 签名 + 调用点 |
| 多表 + GLOBAL 行 key 重复 | 旧 `_buildEntities` 按 `(target_table, score_type)` 分组；多表分别产生不同 target_table 的 GLOBAL 行，命中不同 key，无冲突 |
| 大量 target_tables 拼成大 IN | 现实业务一般 ≤ 100 张表；超出 SQLAlchemy 自动 bindparam 扩展，无 N+1 |

## 6. 回滚

后端 4 文件 + 前端 2 文件，git revert 即可。无 DB 迁移、无 alembic 变更。