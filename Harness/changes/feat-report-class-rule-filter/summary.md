# 变更:评估报告选规则按 class 自动过滤

- **日期**:2026-09-15
- **作者**:Claude
- **Phase**:Phase 6 数据质量(评估报告向导)
- **状态**:implemented(2026-09-15 12:55)
- **关联变更**:无
- **迁移版本**:无(纯前端 client-side 过滤,不动后端)
- **MEMORY**:`../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/report-class-rule-filter.md`

---

## 1. 需求

用户在「新建评估报告」向导(2026-09-15 feat-dq-evaluation-report)的第 2 步反馈:
- 选完对象(本体类)后,每个对象卡片的规则下拉框显示**全部已启用规则**,无论规则是否属于该对象。
- 期望:**下拉框只有这个对象下的规则能看到。**

**目标**:Step 2 的规则 Select 选项按 `r.sourceClassId === cid` 过滤,只展示当前对象卡片的规则。

---

## 2. 设计评审

### 2.1 过滤策略

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. **客户端 `.filter`**(本次方案) | 数据已在 `allRules` 中全量加载;`<500` 条规模下 `O(N)` 一次扫描够用 | **选**:改动最小,不引入异步态 |
| B. 后端 `?sourceClassId=N` 过滤 | 后端已支持(`backend/app/api/v1/data_quality.py:76`);切对象触发新请求 | 拒:多对象时 N 次请求 / 状态管理更复杂;本次场景规模不划算 |
| C. 按对象预拉取列表 | Step 1 切完对象后并发拉每个对象的规则 | 拒:过度工程,数据有重叠浪费 |

### 2.2 隐藏规则的处理

- `sourceClassId === null / undefined` 的规则(历史/无对象归属)**不进选择面**,与用户「只看到对象下规则」一致。
- 不提供「全部规则」逃生入口——用户要这类规则时,应回管理页改 `source_class_id` 或单条编辑走一遍。

---

## 3. 文件改动

| 文件 | 改动 |
|---|---|
| `frontend/src/pages/DataQualityReportCreatePage.tsx:283-289` | `allRules.map(...)` 前加 `.filter(r => r.sourceClassId === cid)`;useMemo deps 不变(`allRules` 已在) |
| `frontend/src/types/dataQuality.ts:29-30` | `DataQualityRule` 加 `sourceClassId?: number \| null` + `sourcePropertyId?: number \| null`(后端 `DataQualityRuleRead` 本就返这俩字段,前端类型以前漏了)|

后端:**0 改动**(已支持 `?sourceClassId=N`)。

---

## 4. 验收

```bash
# 前端编译
cd frontend && npx tsc --noEmit
# 期望:0 errors
```

**浏览器**:
- 进 `/data-quality/reports/new` → 第 1 步选 1 个对象 → 第 2 步打开该对象卡片 → 规则下拉里**只有该对象下的规则**
- 选 2 个对象 → 第 2 步两个卡片各自显示各自的规则
- 老数据 `source_class_id IS NULL` 的规则不出现在任何卡片

---

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 老规则 `source_class_id IS NULL` → 向导里看不到 | 用户需求明确;老规则要走评估报告时,先回管理页改归属 |
| 批量创建规则时 `sourceClassId` 没写入 | 已在 `feat-rule-batch-create` 强制写入(参考 memory `rule-batch-create`);本次不动 |
| type 加可选字段可能影响已有测试 | 已 `npx tsc --noEmit` 0 错误;既有用例不受影响 |

---

## 6. 回滚

- 删 `DataQualityReportCreatePage.tsx:286` 的 `.filter(r => r.sourceClassId === cid)` 一行。
- 不删类型字段(后端本就返,留着不破坏契约)。