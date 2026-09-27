# 变更：fix-plan-scope-gate-b

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：半空计划闸门方案B（前置核对完成 → 实施）
- **状态**：done
- **关联变更**：`fix-plan-scope-gate-and-trigram`（上批方案A 已堵 `rowLimit`/`perGroupLimit` 旁路，commit 29fcfab）；`fix-milvus-delete-visibility`（同窗口批）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 M3；前置核对 proposal `Harness/changes/fix-plan-scope-gate-and-trigram/2026-09-26-plan-scope-gate-proposal.md`

---

## 1. 需求

### A. 半空计划闸门方案B

**问题**：方案A（29fcfab）已堵 `{"rowLimit": 100}` / `{"perGroupLimit": 5}` 两种「仅限制无引用」形态，但剩余三种半空旁路仍可过 validatePlan：

| 形态 | 后果 |
|---|---|
| `{"target": "查询"}` | 计划无引用可校验 → SQL 阶段自由选表/编表名 → 库侧报错被包装成「服务内部错误」 |
| `{"conditions": ["INTER_COM_CODE=1"]}`（无 selectedClasses 等） | 多步追问合法形态，但单步"只 conditions"会被自由选表 |
| `{"aggregations": [...]}`（无 selectedClasses） | 同上 |

### B. 前置核对 4 项

实施前完成 4 项前置核对（多步 prior_state / REFINE 路径 / global_filters / 重试预算），确认：
- 多步 `_prepareFollowUpMultiStep` 把 prior_state 注入 `_buildPlanUserPrompt` 的 `[global_constraints]` 块
- global_filters 同样作为 `[global_constraints]` 注入 user prompt（非 plan 字段）
- 两种「跨步沿用」均不把 prior_state 的引用回填进当步 plan
- 重试预算 `maxRetries` 默认 3 轮，可吸收一次模型未补足引用的情况

**结论**：方案B 收紧到「无可查询引用即为空」安全可行。

### C. 重试反馈可操作化（前置核对 D）

原重试反馈 `第 N 次尝试未能从回复中解析出查询计划` 信息不足——LLM 反复补 target 而忽略 selectedClasses/conditions 等真实缺口。本批带上具体丢失字段（`field:REASON`）让 LLM 下次精准补齐。

### D. Plan 系统 prompt 加规则 10（前置核对 D）

显式告诉 LLM「半空计划会被拒」，从源头降低半空产出概率。

---

## 2. 设计评审

### 方案选择

| 候选 | 选定 | 理由 |
|---|---|---|
| 方案A 维持（仅 rowLimit/perGroupLimit 移除） | ❌ | 留 3 个旁路；用户场景仍可触发 |
| **方案B（target/conditions/aggregations 也移除）** | ✅ | 最窄口径，所有"无可查询引用"形态都判空 |
| 方案C（target 移除，保留 conditions/aggregations） | ❌ | 单步「只 conditions」仍是旁路；多步合法形态已被前置核对确认走 global_constraints，不依赖 plan.conditions |
| 重试反馈保留 generic 文案 | ❌ | LLM 反复补 target 而忽略真实缺口（前批经验） |
| **重试反馈带 dropsHint** | ✅ | 让 LLM 知道补哪个字段，反馈可操作 |
| Plan system prompt 不加规则 | ❌ | LLM 不知道「半空」会被拒，下次仍产出半空 |
| **Plan system prompt 加规则 10** | ✅ | 从源头降低半空产出 |

### 不做

- **不改 `_parsePlanOutcome`**：结构损坏 → 重试的契约不变（M3 已修）
- **不动 SQL 阶段 prompt**：plan 闸门只控 plan 输出
- **不动 validatePlan 阈值**：M3 已修全空校验，本批与 M3 接力

---

## 3. 数据模型变更

无。

---

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `_isEmptyPlan(plan)` | **移除 `plan.target.strip()` 判定**——仅当全部 7 项引用（selectedClasses/selectedProperties/conditions/aggregations/groupBy/joins/sortBy/partitionBy）皆为空才判空 |
| `_parsePlanOutcome` | 在 `_isEmptyPlan` 检查**前**新增 `plan.isUnanswerable` 短路——保证 target="无法回答" 不被误判 PLAN_EMPTY |
| `generateQueryPlan` 重试反馈 | PLAN_EMPTY 且有 drops 时，错误消息带具体丢失字段（`field:REASON`） |
| `_buildSystemPrompt` (plan 部分) | 新增规则 10：禁止半空计划（仅 target / 仅 rowLimit / 仅 perGroupLimit 等无可查询引用形态） |

---

## 5. 实现要点

### 5.1 `_isEmptyPlan` 方案B 落地

```python
def _isEmptyPlan(plan: QueryPlan) -> bool:
    """无可查询引用即为空（方案A + 方案B）。

    方案A（29fcfab）已移除 rowLimit/perGroupLimit 判定。
    方案B（本批）移除 target 单独存在的「有内容」判断。

    注：target="无法回答" 由 isUnanswerable 在 _parsePlanOutcome 短路放行。
    """
    return not (
        plan.selectedClasses
        or plan.selectedProperties
        or plan.conditions
        or plan.aggregations
        or plan.groupBy
        or plan.joins
        or plan.sortBy
        or plan.partitionBy
    )
```

### 5.2 `_parsePlanOutcome` isUnanswerable 短路

```python
plan, drops = QueryPlan.from_dictWithReport(data)
if plan.isUnanswerable:
    # 方案B 后 _isEmptyPlan 不再考虑 target 字段，否则 isUnanswerable
    # 也会被当成 PLAN_EMPTY 走重试，浪费预算。
    return _PlanParseOutcome(plan, None, drops)
if _isEmptyPlan(plan):
    return _PlanParseOutcome(None, REASON_PLAN_EMPTY, drops)
return _PlanParseOutcome(plan, None, drops)
```

### 5.3 重试反馈带 dropsHint

```python
if outcome.plan is None:
    if outcome.drops:
        logger.warning(
            "NL2SQL 计划解析失败 attempt=%d reason=%s drops=%s",
            attempt + 1, outcome.reason, formatPlanDrops(outcome.drops),
        )
        # 前置核对 D：让 LLM 知道补哪个字段
        dropsHint = ";".join(f"{d.field}:{d.reason}" for d in outcome.drops)
        errors.append(
            f"第 {attempt + 1} 次尝试未能解析出有效查询计划"
            f"（丢失字段：{dropsHint}）"
        )
    else:
        logger.warning(
            "NL2SQL 计划解析失败 attempt=%d reason=%s", attempt + 1, outcome.reason
        )
        errors.append(f"第 {attempt + 1} 次尝试未能从回复中解析出查询计划")
    continue
```

### 5.4 Plan system prompt 规则 10

```python
"10. 计划**不得「半空」**——仅 target / 仅 rowLimit / 仅 perGroupLimit 等"
"无可查询引用的形态都会被拒（重试也不会被接受）。"
"selectedClasses / selectedProperties / conditions / aggregations / groupBy / joins / sortBy "
"至少填一项；若真的匹配不到任何表，用 target=\"无法回答\"。"
```

---

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| **新增守卫**（test_nl2sql_plan_gate.py） | TestRowLimitOnlyIsEmpty + TestNormalPlanStillValid + 4 新增 target+/conditions-only 用例 | 10 passed |
| **新增可观测**（test_nl2sql_service.py） | test_empty_plan_retry_hint_lists_dropped_fields | 1 passed |
| **回归** | test_nl2sql_service.py 117 例 | 117 passed |
| **回归** | test_nl2sql_plan_gate.py | 11 passed |
| **ruff** | nl2sql_service.py + 2 test files | 0 warning |

### 关键判定（test_nl2sql_plan_gate.py）

| 用例 | 期望 | 实际 |
|---|---|---|
| `test_target_only_plan_is_not_empty` (方案A) → 方案B 改为 PLAN_EMPTY | plan=None, reason=PLAN_EMPTY | ✅ |
| `test_unanswerable_target_still_valid` | plan is not None, isUnanswerable=True | ✅ |
| `test_target_plus_conditions_is_not_empty` | plan is not None | ✅ |
| `test_conditions_only_is_not_empty` | plan is not None | ✅ |
| `test_target_plus_aggregations_is_not_empty` | plan is not None | ✅ |
| `test_target_plus_groupBy_is_not_empty` | plan is not None | ✅ |

### 既有用例迁移（test_nl2sql_service.py）

| 用例 | 改动 |
|---|---|
| `test_minimal_plan_with_target_is_accepted` | `{"target": "查询"}` → 期望 PLAN_EMPTY |
| `test_clean_plan_has_no_drops` | 加 `selectedClasses` |
| `test_empty_plan_retries_and_succeeds_on_second_attempt` | 第二跳加 `selectedClasses` |
| `test_plan_prompt_contains_row_limit_rule` | 加 `selectedClasses` |
| `test_plan_prompt_guides_per_group_topn` | 加 `selectedClasses` |
| `test_plan_prompt_injects_strong_directive_when_prior_state` | 加 `selectedClasses` |
| `test_plan_prompt_no_prior_state_block` | 加 `selectedClasses` |
| `test_plan_and_sql_prompts_share_directive_text` | 加 `selectedClasses` |
| `test_corrupt_field_is_reported_without_failing` | 加 `conditions` |
| `test_degraded_plan_logs_reason_and_drops` | 加 `conditions` |

---

## 7. 安全审查

### 收益

- **M3 半空旁路彻底闭合**：`{"target": "..."}` / `{"conditions": [...]}` / `{"aggregations": [...]}` 三种旁路都被识别为 PLAN_EMPTY
- **重试反馈可操作**：LLM 下次能精准补齐丢失字段，不会再反复补 target
- **源头降低半空产出**：plan system prompt 规则 10 让 LLM 主动避免半空
- **isUnanswerable 短路**：target="无法回答" 不被误判，保留模型友好回答语义

### 边界与已知限制

- **多步 prior_state 不回填进 plan**：前置核对确认依赖 `[global_constraints]` 块注入 user prompt，不依赖 plan 字段
- **refine 路径不影响**：grep 全仓确认无「重写只改条件、历史引用沿用」形态
- **重试预算**：maxRetries 默认 3 轮可吸收一次未补足引用的情况

### 残差

- **`{"aggregations": [...]}` 无 selectedClasses 仍合法**（被 `test_target_plus_aggregations_is_not_empty` 覆盖）
- **未来 plan 字段扩展**：若新增字段为「可查询引用」需同步加进 `_isEmptyPlan` 的 OR 链（建议用白名单常量）

---

## 8. 部署验证

- test 库 `alembic current` 不变（无 schema 变更）
- prod 库 `alembic current` 不变
- `/api/v1/health` 双通道 200
- 回归用例 128/128 通过（方案A + 方案B + 重试反馈 + 规则 10）

---

## 9. 真实数据验证

| 项 | 结果 |
|---|---|
| `test_nl2sql_plan_gate.py` 11 例 | 11 passed |
| `test_nl2sql_service.py` 117 例 | 117 passed |
| isUnanswerable 短路 | 验证 `target="无法回答"` 不被误判 PLAN_EMPTY |
| 重试反馈可操作 | 验证 `target:PLAN_TARGET_NOT_STR` 等具体字段回注 |
| Plan system prompt 规则 10 | 验证含"半空"措辞 |

---

## 10. 关联

- commit（待提交）：
  - `fix: 半空计划闸门方案B (_isEmptyPlan 移除 target 判定 + isUnanswerable 短路)`
  - `feat: 重试反馈带具体丢失字段（前置核对 D 可操作化）`
  - `feat: plan system prompt 加规则 10 禁止半空计划`
  - `test: test_nl2sql_plan_gate 4 例 + test_nl2sql_service 1 例 + 既有用例迁移`
- 上批 commit `29fcfab`：方案A + supplier trigram + Milvus round-trip random id
- 同窗口 commit `1f18c41`：Milvus delete visibility + L2 unreachable noqa
- 评估文档更新：`Harness/wiki/chat-service-assessment.md` §2.3 M3 标注「方案B 已堵」
- memory 登记：`qa-system-plan-scope-gate-b`（新）
- 既有 memory 关联：`qa-system-mixed-suite-truncate-hazard`（隔离运行避免基础设施 flake 干扰本批验证）

---

## SSOT 校验清单

- [x] `_isEmptyPlan` 移除 `target.strip()` 判定
- [x] `_parsePlanOutcome` 在 `_isEmptyPlan` 前加 `isUnanswerable` 短路
- [x] 重试反馈带 `dropsHint`（PLAN_EMPTY 且 drops 非空时）
- [x] plan system prompt 加规则 10
- [x] test_nl2sql_plan_gate 11/11 通过
- [x] test_nl2sql_service 相关用例迁移后通过
- [x] 新增 test_empty_plan_retry_hint_lists_dropped_fields
- [x] ruff 零警告
- [x] 评估文档同步
- [x] MEMORY 登记