# 变更:批量新建规则 UNIQUENESS 表达式自动补全(对齐生成器主键分支)

- **日期**:2026-09-15
- **作者**:Claude
- **Phase**:Phase 6 数据质量(规则批量新建)
- **状态**:implemented(2026-09-15 13:00)
- **关联变更**:[[feat-batch-complete-expr-autofill]](`COMPLETENESS` 同模板对齐)
- **迁移版本**:无(纯前端 utility 函数调整)
- **MEMORY**:`../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/uniqueness-auto-expression.md`

---

## 1. 需求

用户在「批量新建规则」(2026-09-15 feat-rule-batch-create)的 Step 2 反馈:
- 选 `UNIQUENESS`(唯一性)规则类型时,**表达式列空着**(`null`),与 `COMPLETENESS` 改完后会显示 `${col} IS NOT NULL` 不一致。
- 用户期望:**至少和后端生成器(`data_quality_rule_generator.py:193` 主键分支)产出的 `UNIQUE({column})` 对齐**,让审计 / 导出的 CSV 看到可读 SQL 片段。

**目标**:`suggestRuleExpression` 里把 `UNIQUENESS` 从 `{expression: null}` 改成 `UNIQUE(${col.name})`,与后端生成器主键分支一致。**不**改评估行为(评估器 `uniqueness.py` 不依赖 `rule_expression`,靠 DB 唯一约束)。

---

## 2. 设计评审

### 2.1 行为对齐

| 规则类型 | 后端 `data_quality_rule_generator.py:191-194` 主键分支 | 前端 `suggestRuleExpression` 改动 |
|----------|----------------------------------------------|-----------------------------------|
| UNIQUENESS | `f"UNIQUE({column})"`(PK 分支,line 193)| **改为** `{expression: "UNIQUE(<col>)", templateId: "T_UNIQUE_COLUMN", canAutoFill: true}` |
| 其他类型 | 不变 | 不变 |

### 2.2 评估语义无害

`backend/app/data_quality_evaluators/uniqueness.py` 用 `SELECT COUNT(*) AS total, COUNT(DISTINCT <column>) AS passed`,**不依赖 `rule_expression`**——前端表达式仅用于:
- 批量创建页 Step 2 表格给用户看「自动生成的 SQL 片段」+「自动」徽标
- 审计/导出 CSV 时给运维一个可读文本
- 单规则创建页 Modal 选完 ruleType + targetColumn 后自动填 ruleExpression

**不会破坏评估语义**。

---

## 3. 文件改动

| 文件 | 改动 |
|---|---|
| `frontend/src/utils/ruleExpressionTemplates.ts:117-126` | UNIQUENESS 分支从 `{expression: null}` 改成 `{expression: \`UNIQUE(${column.name})\`, templateId: "T_UNIQUE_COLUMN", canAutoFill: true}` |
| `frontend/src/tests/ruleExpressionTemplates.test.ts:69-77` | VARCHAR+UNIQUENESS 用例从「期望 null」改为「期望 `UNIQUE(ORDER_NO)` + `templateId: "T_UNIQUE_COLUMN"`」 |

后端、批量创建 Step 组件、单规则 Modal:**0 改动**。两者都已正确调用 `suggestRuleExpression`,模板函数修好后自动显示「自动」徽标和表达式。

---

## 4. 验收

```bash
# 前端单测
cd frontend && npx vitest run src/tests/ruleExpressionTemplates.test.ts
# 期望:13 tests passed (VARCHAR + UNIQUENESS → UNIQUE(ORDER_NO))

# 前端编译
cd frontend && npm run build
# 期望:0 errors, ✓ built
```

浏览器验收(批量新建):
- 进 `/data-quality/rules/batch-create` → Step 2 把任一列 `ruleType` 切到 `UNIQUENESS` → 表达式立即变成 `UNIQUE(COL)`,显示「自动」徽标

---

## 5. 风险 & 回滚

| 风险 | 缓解 |
|---|---|
| 前端表达式与后端 PK 分支脱钩(以后后端改文案) | `UNIQUE(${col})` 是 SQL 标准 DDL,基本不变;`data_quality_rule_generator.py` 注释指明「主键 UNIQUENESS 用 UNIQUE(col)」,前端镜像此行为 |
| 评估器意外开始读 `rule_expression`(以后改 evaluator) | 当前 uniqueness.py 不读,设计原则保留;如未来评估器改了,前端表达式最多冗余不会冲突 |

**回滚**:把 `UNIQUE(${col.name})` 改回 `{expression: null}` 即可。

---

## 6. 关键文件路径速查

- 模板:`frontend/src/utils/ruleExpressionTemplates.ts:117-126`(UNIQUENESS 分支)
- 测试:`frontend/src/tests/ruleExpressionTemplates.test.ts:69-77`
- 后端参照:`backend/app/services/data_quality_rule_generator.py:191-194`
