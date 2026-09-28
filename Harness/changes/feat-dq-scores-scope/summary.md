# 变更：数据质量评分 scope 字段（datasource_id / target_table / rule_type）

- **日期**：2026-09-15（事后补档；特性实现于同日更早的 commit）
- **作者**：Claude
- **Phase**：数据质量（Phase 1.3 评分）
- **状态**：done（已 superseded by [feat-dq-scores-multiselect](../feat-dq-scores-multiselect/summary.md)）
- **关联变更**：被 [feat-dq-scores-multiselect](../feat-dq-scores-multiselect/summary.md) 替代为多选

## 1. 需求

原 `ComputeScoresRequest` 无 scope 字段：每次 `POST /scores/compute` 都跑全库 enabled rules 评估 + 聚合落库。当库里有 100+ 条规则、用户只想评估 PORDER 表的完整性规则时，要么全跑（慢），要么前端拆 N 次请求（不可控、落库时同 `(target_table, GLOBAL)` key 触发 INSERT 冲突）。

引入 3 个 optional scope 字段：`datasource_id / target_table / rule_type`，三条件 AND 组合；scope 命中 0 条规则 → 空响应、不写库、不写 GLOBAL（与旧行为兼容；全 None = 全量）。

## 2. 设计评审

候选路径：

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 在 `_listEnabledRules` 加 3 个 WHERE 子句（=匹配） | 最小改动 | **选**：AND 组合天然支持零规则短路；空 disabled / 空规则提前 return |
| B. 抽 `RuleScopeFilter` 抽象层 | 干净但 YAGNI | 拒：3 个字段够简单，硬抽就是过度工程 |
| C. 用 JSONB 存 scope 数组 | 灵活但难建索引 | 拒：当前 3 字段基本够用，未来要扩再重构 |

最终：**A**。

## 3. 改动清单

### 3.1 后端

| 文件 | 改动 |
|------|------|
| `backend/app/domain/schemas.py` | `ComputeScoresRequest` 加 3 个 optional 字段 |
| `backend/app/services/data_quality_score_service.py` | `computeScores` + `_listEnabledRules` 加 3 个 AND 过滤 |
| `backend/app/api/v1/data_quality.py` | payload 透传 |

### 3.2 前端

| 文件 | 改动 |
|------|------|
| `frontend/src/types/dataQualityScore.ts` | `ComputeScoresRequest` 类型同步 |
| `frontend/src/pages/DataQualityPage.tsx` (`ScoresTab`) | 3 个 scope Select 控件（datasource 单选 + targetTable/ruleType 单选） |

## 4. 验收

```bash
# 后端
docker exec qa-backend bash -c 'cd /app && \
  TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata_test" \
  python3 -m pytest app/tests/integration/test_data_quality_score_api.py -q -k scope'
# 期望：4 passed
```

手动：进 `/data-quality?tab=scores`，选 1 个数据源 / 1 张表 / 1 个规则类型 → 点「计算评分」 → 仅命中过滤的规则被评估、聚合写库。

## 5. 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| 前端多选需求 | superseded by [feat-dq-scores-multiselect](../feat-dq-scores-multiselect/summary.md) |
| AuditService 误写「PORDER 之外」为缺考 | `_queryExistingBeforeAdd` 仅查本次实际落库的 key |
| 评估 dispatcher 内部用 `time_window` 不带 scope | scope 在 `_listEnabledRules` 阶段过滤；dispatcher 拿到的是已缩子集 |

## 6. 回滚

后端 3 文件 + 前端 2 文件，git revert 即可。无 DB 迁移。

## 7. 后续 → Supersede

本次实现的 `target_table: str` / `rule_type: RuleType` 单值字段在 2026-09-15 同日被
[feat-dq-scores-multiselect](../feat-dq-scores-multiselect/summary.md) 替换为
`list[str]` / `list[RuleType]`，旧单值字段直接废弃；新 schema 兼容本变更的
AND 过滤语义（空列表 = 全量）。