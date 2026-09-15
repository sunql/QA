# 变更:批量创建规则自动补全 COMPLETENESS 表达式

- **日期**:2026-09-15
- **作者**:Claude
- **Phase**:Phase 6 数据质量(规则批量新建)
- **状态**:implemented(2026-09-15 12:55)
- **关联变更**:[[feat-rule-batch-create]] 表达式模板链路
- **迁移版本**:无(纯前端 utility 函数调整)
- **MEMORY**:`../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/batch-complete-expr-autofill.md`

---

## 1. 需求

用户在「批量新建规则」(2026-09-15 feat-rule-batch-create)的 Step 2 反馈:
- 给某列选 `COMPLETENESS`(完整性)这类自动规则类型时,**表达式列空着**,不像「规则生成向导」(`data_quality_rule_generator.py`)那样自动补齐 `COL IS NOT NULL`。
- 期望:**跟通过规则生成向导那样自动补充规则表达式**。

**目标**:`suggestRuleExpression` 里把 `COMPLETENESS` 改成返回 `${col} IS NOT NULL`,与生成器行为对齐。`UNIQUENESS` 保持 `null`(生成器也不产生表达式)。

---

## 2. 设计评审

### 2.1 行为对齐

| 规则类型 | 后端 `data_quality_rule_generator.py` 行为 | 前端 `suggestRuleExpression` 改动 |
|----------|-------------------------------------------|-----------------------------------|
| COMPLETENESS | `f"{column} IS NOT NULL"`(`data_quality_rule_generator.py:199`)| **改为** `{expression: "${col} IS NOT NULL", templateId: "T_COMPLETENESS_NOT_NULL", canAutoFill: true}` |
| UNIQUENESS | `UNIQUE({column})`(line 193)但**评估器不依赖 `rule_expression`**,靠 DB 唯一约束 | **保持** `{expression: null, templateId: null, canAutoFill: true}` |
| VALIDITY/CONSISTENCY/REFERENTIAL/TIMELINESS | 各自有模板 | **不动**(本次仅修 COMPLETENESS) |

### 2.2 为什么 COMPLETENESS 表达式对评估语义无害

`backend/app/data_quality_evaluators/completeness.py` 用的是 `SELECT COUNT(*) AS total, COUNT(<column>) AS passed`,**不依赖 `rule_expression`**——前端表达式仅用于:
- 批量创建页 Step 2 表格给用户看「自动生成的 SQL 片段」+「自动」徽标
- 审计/导出 CSV 时给运维一个可读文本

**不会破坏评估语义**。

---

## 3. 文件改动

| 文件 | 改动 |
|---|---|
| `frontend/src/utils/ruleExpressionTemplates.ts:107-119` | 拆 `COMPLETENESS / UNIQUENESS` 合并分支;COMPLETENESS 走新模板(返回 `${col} IS NOT NULL` + `templateId: "T_COMPLETENESS_NOT_NULL"`);UNIQUENESS 单独分支保持 `null` |
| `frontend/src/tests/ruleExpressionTemplates.test.ts:77-91` | 改 VARCHAR+COMPLETENESS 用例(从 `null` → `${col} IS NOT NULL`)+ 新增 NUMBER+COMPLETENESS 用例 |

后端、Step 组件(`RuleBatchStepColumns.tsx`)、批量创建主页面:**0 改动**。组件已正确调用 `suggestRuleExpression`,模板函数修好后自动显示「自动」徽标和表达式。

---

## 4. 验收

```bash
# 前端编译
cd frontend && npx tsc --noEmit
# 期望:0 errors

# 前端单测
cd frontend && npx vitest run src/tests/ruleExpressionTemplates.test.ts
# 期望:13 tests passed(新增 NUMBER+COMPLETENESS 用例 + 改写 VARCHAR+COMPLETENESS 用例)
```

**浏览器**:进 `/data-quality/rules/batch-create`
- Step 1 选数据源/类/表 → Step 2 默认 `ruleType=VALIDITY`(沿用原行为,表达式按列名生成)
- 切某一列 `ruleType=COMPLETENESS` → 表达式立即变成 `COL_NAME IS NOT NULL`,addonAfter 显示「自动」徽标
- 改一下表达式文本(去掉 `NOT`)→ 徽标变「自定义」
- 切回 `VALIDITY` → 表达式重新跑模板并更新

---

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 用户手动改 COMPLETENESS 表达式后切 `ruleType` 又被覆盖 | 已用 `isAutoExpression=false` 标记,改后再切换才覆盖(沿用原契约) |
| COMPLETENESS 表达式被 `validators` / SQLGuard 当成可执行 SQL | 评估器**不读** `rule_expression`,仅展示用;但若未来给评估器加 SQL 模板注入,要记得该字段不能直接执行 |
| 其它规则类型未自动补全(用户没明确要求) | 用户只举 COMPLETENESS 一个例子;VALIDITY 等已有合理模板;UNIQUENESS 评估不依赖表达式 |

---

## 6. 回滚

- 把 `ruleExpressionTemplates.ts:107-119` 合并回原 `if (COMPLETENESS || UNIQUENESS) return { expression: null, ... }` 一行即可。
- 测试用例同时回滚。