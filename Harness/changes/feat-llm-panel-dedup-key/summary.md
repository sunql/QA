# 变更：feat-llm-panel-dedup-key

- **日期**：2026-09-14
- **作者**：
- **Phase**：feat-dq-rule-auto-generation 后续 bugfix
- **状态**：shipped

## 1. 需求

数据质量规则自动生成向导 `/data-quality/generate` Step 3 的 LLM advisory 面板（LlmPanel）
在 React dev mode 下报 duplicate key 警告：

> Encountered two children with the same key, `1140`
> at LlmPanel (DataQualityRuleGeneratePage.tsx:186)
> at DataQualityRuleGeneratePage (...:445)

报错时 LLM 返回的 `suggestions` 中两条建议 `propertyId` 相同。

## 2. 根因

`parsePropertyDescriptions`（`backend/app/services/data_quality_rule_llm_service.py:100-170`）
直接把 LLM 输出 `parsed["suggestions"]` 转成 `PropertyConstraintSuggestionRead`，无去重：

- LLM 抖动时同一 `(property_name, kind)` 可能输出多次 → 后端按 property_name 映射成同一
  `property_id` 后，前端 `items.map()` 用 `key={item.propertyId}` 撞 key。
- 同一 `property` 不同 `kind`（如 `not_null` + `allowed_values`）是合法多约束，
  但纯 propertyId key 仍会撞。
- LLM 输出 LLM 引用了本体中不存在的 `property_name` → `property_id` 留为占位 `0`，
  原版错误地返回给前端（前端 button 无效）。

## 3. 修复

### 后端（根治）

`parsePropertyDescriptions` 在 line 154 后增加 dedup：

1. 按 `(property_id, kind)` 去重，保留首次出现（LLM 抖动丢弃后续）
2. 丢弃 `property_id == 0` 的占位项（LLM 引用不存在的属性名）

```python
# 去重 + 丢弃未映射项：保留首次出现的 (property_id, kind)；
# 同一 property 不同 kind 是合法多约束，保留。
# property_id=0 是 LLM 输出未在本体 prop_map 命中的 property_name，
# 丢弃避免前端拿占位 0 当真实 id。
deduped: list[PropertyConstraintSuggestionRead] = []
seen: set[tuple[int, str]] = set()
for s in suggestions:
    key = (s.property_id, s.kind)
    if key in seen:
        continue
    seen.add(key)
    if s.property_id == 0:
        continue
    deduped.append(s)
suggestions = deduped
```

### 前端（双保险）

`DataQualityRuleGeneratePage.tsx` LlmPanel `items.map()`：

```tsx
// 修复前
key={item.propertyId}

// 修复后
key={`${item.propertyId}-${item.kind}`}
```

复合 key 即使后端 dedup 未来不生效仍能避免 React 警告。

## 4. 数据模型 / 接口契约

无 schema 变化。响应内容语义微调：

| 场景 | 修复前 | 修复后 |
|---|---|---|
| LLM 输出同 (property_name, kind) 抖动重复 | 返回 N 条 | 返回 1 条 |
| LLM 输出同 property 不同 kind（合法多约束） | 返回 N 条 | 返回 N 条（不变） |
| LLM 输出 property_name 不在本体 | 返回 propertyId=0 的占位项 | 丢弃 |

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/services/data_quality_rule_llm_service.py` | `parsePropertyDescriptions` 加 dedup（line 154-167 之后） |
| `backend/app/tests/integration/test_dq_rule_generate_llm_api.py` | 新增 `test_parse_descriptions_dedups_by_property_id_and_kind`；`LLM_JSON` 改为真实属性 `po_key`（避免既有 fixture 用不存在的 `status` 占位被新 dedup 丢） |
| `frontend/src/pages/DataQualityRuleGeneratePage.tsx` | LlmPanel `items.map` key 改复合 `${propertyId}-${kind}` |
| `frontend/src/pages/__tests__/DataQualityRuleGeneratePage.adoptContract.test.ts` | 新增契约测试：`uses composite key (propertyId-kind) for items.map` |

## 6. 测试

### 后端集成（真实 PG）

```
test_parse_descriptions_dedups_by_property_id_and_kind     PASSED
  LLM 输出 4 条：po_key+allowed_values (×2 抖动) + po_key+not_null + unknown_property
  期望：2 条（po_key+allowed_values 首条 + po_key+not_null）
  实际：2 条，propertyId=0 占位被丢，重复 allowed_values 被 dedup
```

完整 `test_dq_rule_generate_llm_api.py`：**16 passed**（15 既有 + 1 新）。

回归触发的既有测试 fixture 改动：`LLM_JSON` 从 `status`（本体中不存在 → 占位 0）
改为 `po_key`（`ensureClassWithProperty` 默认建的就是 `po_key`）。修复前这些 fixture 实际上
测的是「propertyId=0 占位能渲染」这条错误契约，修复后用真实属性名才符合生产行为。

### 后端全相关文件

```
test_dq_rule_generate_llm_api.py        16 passed
test_data_quality_api.py                22 passed
test_dq_rule_generate_api.py             8 passed
                                       ────────────
                                        46 passed
```

### 前端契约测试

```
DataQualityRuleGeneratePage.adoptContract.test.ts   11 passed (含 1 新)
DataQualityRuleGeneratePage.test.tsx                 3 passed
DataQualityRuleGeneratePage.llmSelectorContract      passed
DataQualityRuleGeneratePage.contract                 passed
                                                    ─────────
                                                    29 passed
```

`tsc --noEmit` 干净。

## 7. 安全审查

未触发 security-reviewer。本次改动：

- 丢弃 `property_id == 0` 占位项 = 仅过滤未映射的无效数据，不引入新写入端点 / 鉴权变更
- dedup 是幂等的纯函数操作，无副作用
- 前端 key 改为模板表达式，运行时计算无 XSS 风险（仅字符串拼接）

## 8. 部署验证

```bash
# 后端
docker cp backend/app/. qa-backend:/app/app/
docker restart qa-backend

# 前端（vite 打包构建）
cd docker && docker compose -f docker-compose.yml build frontend \
  && docker compose -f docker-compose.yml up -d frontend
```

## 9. 真实数据验证

开发模式（`localhost:5173`）进入 `/data-quality/generate`：

1. Step 1 选本体类 → Step 2 选数据源 → Step 3 展开 LLM 面板
2. React dev console **不再报** `Encountered two children with the same key, 1140` 等 warning
3. LLM 返回的每条建议（含同 property 多 kind）独立渲染为卡片，无 key 冲突

## 10. 关联

- **前置**：`feat-dq-rule-auto-generation`（line 100-170 引入 `parsePropertyDescriptions`，
  本次补 dedup）
- **后置**：`Harness/wiki/data-quality.md` 收录 LlmPanel dedup 行为（与既有约束一并写）
- **规则**：`Harness/rules/开发流程规范.md` §10 阶段；TDD 强制 RED → GREEN → IMPROVE