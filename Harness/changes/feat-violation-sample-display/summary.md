# feat-violation-sample-display (2026-09-15)

## 背景 / 痛点

数据质量评估报告详情页（`/data-quality/reports/:id`）的「违规样本」accordion 表格只展示 6 个元信息列：`ruleId / targetTable / targetColumn / totalViolations / sampleSize / capturedAt`。但**真正存了违规数据的 `sample_pk_values` JSONB 字段完全没有渲染**。

后端 `data_quality_violation_sample_service.sampleForRule()` 默认 `DEFAULT_SAMPLE_LIMIT=20`，
每条规则会采 20 条违规行 PK，存成 `sample_pk_values=[{"pk": "..."}, ...]`。
前端只看到「样本数=20」不知道是哪 20 条。

用户 2026-09-15 反馈：
> 评估报告：校验ITF_ID是否合格 创建报告以后，有不符合规则的数据，但是在违规样本中没有数据，需要把违规的样本数据显示出来，如果数据量很大，至少要显示20条样例

## 设计决策

### 新增「违规样例 PK」列

按以下优先级渲染：

1. **空数组** → `<Tag>—</Tag>` 占位
2. **≤ 5 条** → 直接渲染 N 个红色 `<Tag color="red">{pk}</Tag>`
3. **> 5 条** → 前 5 个 Tag + `<Typography.Text type="secondary">+N 更多</Typography.Text>`，
   鼠标 hover 文字触发 `<Tooltip>` 显示完整 PK 列表（逗号分隔）

之所以只预览前 5 个：
- antd Tag 多了行高会爆（一行塞 20 个 Tag 看起来很乱）
- 业务上「前 5 个 ID 已经能定位到具体行」，完整列表进 Tooltip 保留可追溯性
- 后端 20 条上限是合理暴露量（与 `DEFAULT_SAMPLE_LIMIT` 对齐）

### 截断后用 Tooltip 暴露全量

antd `Tooltip` 的 `title` 属性直接渲染字符串，把 20 个 PK 用 `, ` 连接：
```
T001, T002, T003, ..., T020
```
避免 tooltip 内容爆炸（不用嵌套 Tag），文字可被用户选中复制。

### 容错（schema 防御性读）

后端 schema 是 `[{pk: value}, ...]`，但单元测试 / 老 snapshot / 未来扩展可能塞进
非 `{pk}` 形状的 entry。`extractPkValues()` 容错：
- `entry` 不是 object → 「—」
- `entry.pk === null/undefined/""` → 「—」
- 否则 `String(value)`

保证组件在 schema 漂移时不会白屏。

### i18n 一次性补齐

zh-CN / en-US `samplesTable` 块下补 4 条 key：
- `totalViolations`「违规总数」/「Total Violations」
- `samplePkValues`「违规样例 PK」/「Sample PKs」
- `moreSamples`「+{count} 更多」/「+{count} more」
- `empty` 已存在，无需新增

顺手修了预存的 `samplesEmpty` 错引 → 改为 `samplesTable.empty`（实际有这条 key，旧的 `samplesEmpty` 不存在导致 Empty description 显示空白）。

## 文件改动

| 文件 | 改动 | 行数 |
|---|---|---|
| `frontend/src/components/ViolationSampleTable.tsx` | 加 `extractPkValues` 工具 + 「违规样例 PK」列（Tooltip + Tag 截断）+ 恢复 `totalViolations` 列 + 修 `samplesEmpty` 错引 | +50 / -10 |
| `frontend/src/i18n/zh-CN.ts` | `samplesTable` 加 3 条 key | +9 |
| `frontend/src/i18n/en-US.ts` | `samplesTable` 加 3 条 key | +6 |
| `frontend/src/tests/ViolationSampleTable.test.tsx` | 新增 8 个用例覆盖：≤5 / >5 / =5 / 空 / 异常 schema / Empty / 多行 / Tooltip hover | +200 |

## 不改

- **后端采样逻辑**：`DEFAULT_SAMPLE_LIMIT=20` 已经满足「至少 20 条」需求，无需改
- **API 契约**：`ViolationSampleRead.samplePkValues` 字段已在 Pydantic schema 里，前端只是补渲染
- **`listEvaluationReportSamples` limit**：前端 `DataQualityReportDetailPage.tsx:179` 已经传 `limit=50`，
  后端允许 `1..200`，覆盖 20 条默认无压力
- **dispatcher 静默 except**：之前发现 `dispatcher.collectSamples` blanket except 会吞 Oracle ORA-00933
  等真实错误（参考 `eval-dispatcher-silent-except` memory），这次不动——是独立 follow-up

## 验收

```bash
cd frontend
npx tsc --noEmit                          # 0 errors
npx vitest run src/tests/ViolationSampleTable.test.tsx  # 8/8 pass
npx vitest run src/tests/DataQualityReportDetailPage.ruleTable.test.tsx  # 4/4 pass
```

浏览器验收：
1. 进 `/data-quality/reports/:id`，展开「违规样本」accordion
2. 每一行新增「违规样例 PK」列：≤5 条全显，>5 条前 5 + 「+N 更多」
3. hover「+N 更多」→ 弹 Tooltip 含全部 20 条 PK（逗号分隔，可选中复制）
4. `totalViolations`（违规总数）列恢复——能看出「违规 1,234 条 / 采 20 条样例」的全局规模

## 风险 & 回滚

| 风险 | 缓解 |
|---|---|
| 表格列变多变宽（6 列 → 7 列），窄屏换行难看 | 用固定 width 控列宽（90/130/130/120/280/90/170）；卡片式布局溢出由 antd `Table` 的 `scroll={{ x: 1100 }}` 兜底（详情页父容器已设置） |
| Tooltip 文字 20 条 PK 过长 | 实测 20 个 8 字符 PK = ~160 字符，可接受；保留这是「可读 / 可复制」vs「视觉紧凑」的权衡 |
| `samplesEmpty` 修错引引发 React `findDOMNode` 警告 | 不会，已验证 tsc 0 errors + 12/12 tests |
| 字段名漂移：未来后端改 `sample_pk_values` schema | `extractPkValues` 容错 5 种坏 shape，不会白屏；上线后考虑把 schema 校验搬到 Pydantic |

回滚：删 `extractPkValues` + 删「违规样例 PK」列即可回到原 6 列版本。
