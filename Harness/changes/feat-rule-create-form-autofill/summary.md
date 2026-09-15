# 变更:单规则「新建」Modal 表单自动填充

- **日期**:2026-09-15
- **作者**:Claude
- **Phase**:Phase 6 数据质量(规则管理 UI)
- **状态**:implemented(2026-09-15 13:30)
- **关联变更**:[[feat-uniqueness-auto-expression]] / [[feat-batch-complete-expr-autofill]] / [[feat-rule-edit-modal-lock]]
- **迁移版本**:无(纯前端 UI 行为变更 + 新增 4 个 effect)
- **MEMORY**:`../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/rule-create-form-autofill.md`

---

## 1. 需求

用户在「单规则新建」Modal(2026-09-15 反馈)提了 4 类手填痛点:

1. **`ruleCode` 需手填**:手工维护容易撞码 + 正则约束容易写错。
2. **`ruleName` 完全手填**:命名不规范,团队难统一。
3. **`targetTable` 不跟数据源联动**:从全部历史表里挑,经常选错表。
4. **`targetColumn` 不跟 table 联动**:完全手填,容易打错列名。
5. **规则表达式(尤其 `COMPLETENESS` / `UNIQUENESS`)留空**:与批量创建页 + 规则生成向导不对齐。

**目标**:让单规则新建 Modal 拥有和批量创建页 / 生成向导同等程度的自动填充能力,但保留「用户可手改」的逃生口。

---

## 2. 设计

### 2.1 自动填充矩阵

| 字段 | 新建 | 编辑(见 [[feat-rule-edit-modal-lock]]) |
|---|---|---|
| `ruleCode` | 自动 = `fetchNextRuleCode({className})` 返回值;**可手改**(改后锁自动) | **disabled** |
| `ruleName` | 自动拼接 `dsName - table - column - ruleTypeCN`,空段跳过;**可手改** | **disabled** |
| `sourceClassId`(新加) | 选类触发 code 建议 + ontology property 拉取 | **disabled** |
| `datasourceId` | 触发 schema 拉取(`getDatasourceSchema` → fallback `introspectDatasource`) | **disabled** |
| `targetTable` | 改为 Select,选项 = `dsSchema.map(t => tableName)` | **disabled** |
| `targetColumn` | 改为 Select,选项 = 当前 `targetTable.columns`,显示 `name (dataType)` | **disabled** |
| `ruleType` | 触发 `suggestRuleExpression({column, ruleType, ontologyProperty})` 自动填表达式 | **disabled** |
| `ruleExpression` | 自动填(只有当前为空才覆盖,避免覆盖用户手改) | 可编辑 |
| `threshold` / `severity` / `isEnabled` / `owner` / `description` | 不变 | 可编辑 |

### 2.2 状态机

- `userEditedCode` / `userEditedName` 两个 ref 跟踪「用户是否手改过」;改过则跳过对应自动填充 effect。
- `Form.useWatch("datasourceId" / "targetTable" / "targetColumn" / "ruleType", form)` 4 个响应式订阅避免 `getFieldsValue()` 异步时差。
- `isCreating = creating && !editing` 是「自动填充 effect」的统一闸门——编辑模式下所有 effect 早返回,绝不覆盖 `writeEditingToForm()` 已写入的值。

### 2.3 关键复用

- `frontend/src/api/dataQualityRuleNextCode.ts:15` `fetchNextRuleCode({className})`:复用现有 next-code 端点
- `frontend/src/api/datasource.ts:52,64` `getDatasourceSchema` / `introspectDatasource`:复用现有 schema 拉取
- `frontend/src/api/ontology.ts:69` `listPropertiesByClass`:复用现有 ontology property 查询
- `frontend/src/utils/ruleExpressionTemplates.ts` `suggestRuleExpression`:复用现有表达式模板(由 [[feat-uniqueness-auto-expression]] 扩展)
- `frontend/src/components/dq/RuleBatchStepBasic.tsx`:schema fetch + introspect fallback 双轨模式

---

## 3. 文件改动

| 文件 | 改动 | 行数 |
|---|---|---|
| `frontend/src/pages/DataQualityPage.tsx` | imports +5(`getDatasourceSchema` / `introspectDatasource` / `listPropertiesByClass` / `fetchNextRuleCode` / `suggestRuleExpression`);新增 6 个 state(`dsSchema` / `schemaLoading` / `sourceClassId` / `ontologyProperties` / `codeSuggestion` / `userEditedCode` / `userEditedName`);新增 6 个 effect(schema / properties / code suggestion / code auto-fill / name auto-fill / expression auto-fill / table-change clear column);改 6 处 Form.Item(ruleCode / ruleName / sourceClassId / targetTable / targetColumn / ruleType);handleCreate / handleUpdate / onCancel 增加 state reset | ~110 行 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | 加 5 个 key:`ruleCodeAutoHint` / `sourceClass` / `sourceClassPlaceholder` / `targetTablePlaceholder` / `targetColumnPlaceholder` | 各 5 行 |
| `frontend/src/tests/DataQualityPage.test.tsx` | 加 5 个 mock 模块(`fetchNextRuleCode` / `getDatasourceSchema` / `introspectDatasource` / `listPropertiesByClass`);renderPage 加 `MemoryRouter` 包装(顺带修 1 个 pre-existing 失败用例);新增 describe「新建规则自动填充」3 个测试 | +180 行 |

后端 / 数据库 schema / 迁移:**0 改动**。

---

## 4. 验收

```bash
# 前端单测
cd frontend && npx vitest run src/tests/DataQualityPage.test.tsx
# 期望:14 tests passed(含 3 个新自动填充测试)

# 前端编译
cd frontend && npm run build
# 期望:0 errors, ✓ built in ~3.5s
```

浏览器验收(进 `/data-quality?tab=rules` → 点「新建规则」Modal):
- 选 `sourceClassId` → 后端调 `/data-quality/rules/next-code?className=MU-DQ-PURCHASE_ORDER` → `ruleCode` 自动填
- 选 `datasourceId` → 后端调 `/datasources/1/schema` → `targetTable` 下拉出现 schema 表
- 选 `targetTable` → `targetColumn` 下拉出现该表列,显示 `PO_QTY (NUMBER)` 等
- 选 `ruleType = COMPLETENESS` → `ruleExpression` 自动填 `PO_QTY IS NOT NULL`
- `ruleName` 实时拼接 `oracle-erp-PORDER-PO_QTY-完整性`(列空时跳过该段)

---

## 5. 风险 & 回滚

| 风险 | 缓解 |
|---|---|
| `userEditedCode` 锁住后再点「取消再开」残留 state | onCancel / handleCreate / handleUpdate 三处都 reset state |
| `fetchNextRuleCode` 用户没选类时如何 key | 现有规则:**必须有 sourceClassId**;不选就 `codeSuggestion="""`,不自动填——`ruleCode` 必填校验会拦下提交,用户回补 |
| schema 加载失败(introspect 抛错) | fallback introspectDatasource;两者都失败弹 toast,`targetTable` 退化(用户可手填但不可选) |
| `targetTable` Select 在编辑模式 `disabled` + 原表已不在新 schema | 已知:disabled Select 直接显示原 value 字符串,不强制 schema 对齐;后端 update 不验证 target_table 是否存在(用户改了 targetTable 也不会触发更新,因为 disabled) |

**回滚**:删除 6 个 effect + 改 6 处 Form.Item 回原 Input/无 disabled 即可,无数据 / 迁移影响。

---

## 6. 关键文件路径速查

- 主文件:`frontend/src/pages/DataQualityPage.tsx:160-634`(state + effects)
- Form.Item 改造:`frontend/src/pages/DataQualityPage.tsx:1156-1274`
- 测试新增:`frontend/src/tests/DataQualityPage.test.tsx:401-655`(helpers + 3 个新测试)
- i18n:`frontend/src/i18n/zh-CN.ts:667-676` / `en-US.ts`
