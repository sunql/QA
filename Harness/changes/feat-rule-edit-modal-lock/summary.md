# 变更:编辑规则弹窗字段锁定(防止误改核心维度)

- **日期**:2026-09-15
- **作者**:Claude
- **Phase**:Phase 6 数据质量(规则管理 UI)
- **状态**:implemented(2026-09-15 13:30)
- **关联变更**:[[feat-rule-create-form-autofill]] 同 Modal 新建/编辑共享
- **迁移版本**:无(纯前端 UI 行为变更)
- **MEMORY**:`../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/rule-edit-modal-lock.md`

---

## 1. 需求

用户在「编辑规则」反馈(2026-09-15):打开编辑弹窗后,关键维度字段(`ruleCode` / `ruleName` / `datasourceId` / `targetTable` / `targetColumn` / `ruleType`)都可改,**误改后评估器整体 ERROR**(target_table 改了但 evaluator 还在按旧 schema 跑)。

**目标**:编辑模式下锁定核心维度字段,只允许修改「评估器行为相关可调参数」:规则表达式 / 阈值 / 严重级别 / 是否启用 / 责任方 / 描述。

---

## 2. 设计

### 2.1 锁定矩阵(编辑模式)

| 字段 | 编辑态 | 原因 |
|---|---|---|
| `ruleCode` | **disabled** | 编码是规则身份,改了等于新建 |
| `ruleName` | **disabled** | 显示名,误改会污染列表展示 |
| `sourceClassId` | **disabled** | 同上 |
| `datasourceId` | **disabled** | 切换数据源会导致 targetTable / column 全错 |
| `targetTable` | **disabled** | 改了会让 evaluator 按新表跑 SQL,语义断裂 |
| `targetColumn` | **disabled** | 同上 |
| `ruleType` | **disabled** | 改了会让 evaluator 走不同 evaluator 路径,语义断裂 |
| `ruleExpression` | 可改 | 评估器读它,需要可调 |
| `threshold` | 可改 | 评估器读它,需要可调 |
| `severity` | 可改 | 标签,无副作用 |
| `isEnabled` | 可改 | 启停用 |
| `owner` | 可改 | 责任方调整 |
| `description` | 可改 | 备注 |

### 2.2 实现模式

每个 Form.Item 控件加 `disabled={!!editing}`,与现有 `ruleCode` / `datasourceId` 的 disabled 写法对齐。

- `disabled` 在 antd Form 上不会「从 values 里剔除」——`form.validateFields()` 仍能拿到这些字段,后端 `updateRule()` Pydantic schema 又是全 Optional,不会因多余字段报错。
- antd Form 提交时确实会跳过 `disabled` 字段(`getFieldsValue` 默认行为),但 `setFieldsValue` 在编辑 `afterOpenChange(open=true)` 时已写入,后端 update 收到的 payload 包含这些 disabled 字段值,与原值一致 = 无副作用。

### 2.3 单 Modal 双模式

新建/编辑共用同一 Modal(`open={creating || editing !== null}`,title 切换 `dataQuality.editRule` ↔ `dataQuality.createRule`)。因此:
- 所有自动填充 effect 用 `isCreating = creating && !editing` 闸门——编辑时不开
- 锁定用 `disabled={!!editing}`——新建时不开

两个开关互补,保证「新建时自动填」+「编辑时锁定」。

---

## 3. 文件改动

| 文件 | 改动 | 行数 |
|---|---|---|
| `frontend/src/pages/DataQualityPage.tsx` | 6 处 Form.Item 控件加 `disabled={!!editing}`(ruleCode / ruleName / sourceClassId / datasourceId / targetTable / targetColumn / ruleType) | 改 ~7 行 |

后端 / 数据库 schema / 迁移 / 测试基础设施:**0 改动**(测试基础设施的 `MemoryRouter` 包装在 [[feat-rule-create-form-autofill]] 的 commit 顺手修了,与本变更相关)。

测试新增 1 个 describe「编辑弹窗字段锁定」1 个 it:
- `frontend/src/tests/DataQualityPage.test.tsx:659-757`

---

## 4. 验收

```bash
# 前端单测
cd frontend && npx vitest run src/tests/DataQualityPage.test.tsx
# 期望:14 tests passed(含新增「编辑弹窗字段锁定」测试)

# 前端编译
cd frontend && npm run build
# 期望:0 errors
```

浏览器验收(进 `/data-quality?tab=rules` → 点任意规则的「编辑」):
- `ruleCode` / `ruleName` / `datasourceId` / `targetTable` / `targetColumn` / `ruleType` 控件灰色不可点
- `ruleExpression` / `threshold` / `severity` / `isEnabled` / `owner` / `description` 可改
- 保存后评估器链路不受影响(target_table / column 没动)

---

## 5. 风险 & 回滚

| 风险 | 缓解 |
|---|---|
| 用户真想改 target_table(数据源表已重命名) | 走「停用 → 新建」流程;UI 加 tooltip 解释锁定原因(可后续 PR) |
| 锁定字段在原值被后端清掉后显示不一致 | `disabled` Select 显示原 value 字符串,即使不在新 schema 内也不影响保存(后端 update 不验证 target_table 是否存在);前端不强制 schema 对齐 |
| antd Form `disabled` 字段在 `getFieldsValue` 默认被过滤 → 后端收到残缺 payload | 当前 `updateRule` Pydantic schema 是全 Optional + 编辑模式用户根本没改这些字段,等于没传——安全 |
| 用户用浏览器 devtools 强行改 input disabled 属性 | 已知:这是用户主动越权,后端 update 不验证 target_table 仍允许改;这与原行为一致,不是新风险 |

**回滚**:删除 7 处 `disabled={!!editing}` 即可。无数据 / 迁移影响。

---

## 6. 关键文件路径速查

- 主文件 Form.Item 改造:
  - `ruleCode`:`frontend/src/pages/DataQualityPage.tsx:1174-1179`
  - `ruleName`:`frontend/src/pages/DataQualityPage.tsx:1186-1191`
  - `sourceClassId`:`frontend/src/pages/DataQualityPage.tsx:1196-1207`
  - `datasourceId`:`frontend/src/pages/DataQualityPage.tsx:1213-1220`
  - `targetTable`:`frontend/src/pages/DataQualityPage.tsx:1229-1242`
  - `targetColumn`:`frontend/src/pages/DataQualityPage.tsx:1244-1258`
  - `ruleType`:`frontend/src/pages/DataQualityPage.tsx:1264-1270`
- 测试新增:`frontend/src/tests/DataQualityPage.test.tsx:659-757`
