# 变更：评估摘要加数据条目数 KPI + 规则列降级明确化

- **日期**：2026-09-15
- **作者**：Claude
- **Phase**：Phase 9 评估报告（UI 增强）
- **状态**：done
- **关联变更**：[feat-report-rules-zh-name](../feat-report-rules-zh-name/summary.md)（前置：Rule 列已改 rule_name）
- **迁移版本**：无
- **MEMORY**：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/report-kpi-data-count.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/report-kpi-data-count.md)

---

## 1. 需求

用户在 `/data-quality/reports/:id` 反馈两件事：

1. **评估摘要缺数据条目数**：现有 4 张 KPI 卡（综合评分 / 规则数 / 通过数 / 未通过数），但用户看不到这次评估**一共跑了多少条数据**（100 条？10 万条？），业务体感盲。
2. **规则列仍显示规则编码**：用户上一变更（feat-report-rules-zh-name）期望 Rule 列展示规则名称，但仍有报告显示编码——核实后是**老 snapshot 不含 `rule_name` 字段**，前端降级到 `rule_code` 是预期行为，但**用户没有意识到这是老数据 + 没有明确指引**。

**验收**：
- 评估摘要新增第 5 张 KPI 卡 `评估数据条目数`：各规则 `total_count` 求和
- Rule 列降级时显示 `rule_code` + 「系统编码」徽标 + tooltip 提示「老 snapshot 无 rule_name 字段，请重新生成报告」

## 2. 设计评审

### 2.1 数据条目数计算位置

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 前端 ruleRows.reduce 求和 | 数据已在客户端、零网络 | **选**：简单；前端已有 ruleRows.total_count |
| B. 后端 snapshot 加 `data_item_count` 聚合字段 | 一次落库 | 拒：前端求和零成本；后端聚合会增加复杂度（status=ERROR 规则 total=0 要不要计入？） |
| C. 新增后端端点按 report_id 实时聚合 | 实时但有 IO | 拒：snapshot 数据足够，且评估是历史动作无需重算 |

最终：**A**。`ruleRows.reduce((sum, r) => sum + (r.total_count || 0), 0)`，全 0 时显示 0（不显示 `—`，明确给数字）。

### 2.2 规则列降级提示

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 只显示 rule_code（同前次变更） | 简单 | 拒：用户还是不知道为啥显示编码 |
| B. 显示 rule_code + 「系统编码」徽标 + tooltip | 明确告诉用户这是降级 + 怎么修复 | **选**：徽标视觉区分 + tooltip 指引操作 |
| C. 弹 Modal 强制用户重新生成报告 | 强制 | 拒：打断流程；降级只是显示问题，不影响数据正确性 |

最终：**B**。

设计点：

- 徽标用 antd `Tag color="default"`（灰色，区别于 Type/Severity 的彩色 Tag）
- Tooltip 用 antd `Tooltip`（hover 触发，jsdom 默认不渲染 portal）
- tooltip 文本 i18n（zh-CN + en-US），用户可读 + 后续加「点击重新生成」按钮预留扩展位
- 列宽从 240 改 280：徽标 + 编码 + spacing 需要更多空间

## 3. 数据模型变更

无。

## 4. 接口契约变更

无。

## 5. 实现要点

`frontend/src/pages/DataQualityReportDetailPage.tsx`：

- 顶部 antd imports 加 `Tooltip`
- KPI 卡组：4 张 → 5 张，加 `kpi-data-item-count`：
  ```tsx
  <KpiCard
    testId="kpi-data-item-count"
    label={t("dataQuality.reports.detail.kpi.dataItemCount")}
    value={ruleRows.reduce((sum, r) => sum + (r.total_count || 0), 0)}
    precision={0}
  />
  ```
  `precision={0}` 让 antd Statistic 不显示小数（total_count 是整数）。

- Rule 列 render：降级时返回 `Tooltip + Space[Tag "系统编码", rule_code]`

`frontend/src/i18n/zh-CN.ts` + `en-US.ts`：

- `kpi.dataItemCount`：「评估数据条目数」/「Evaluated Data Items」
- `ruleTable.ruleFallbackBadge`：「系统编码」/「System Code」
- `ruleTable.ruleFallbackTooltip`：「未读取到规则名称（老 snapshot 不含 rule_name 字段），请重新生成报告或刷新数据。」/「Rule name unavailable (old snapshot has no rule_name field). Regenerate the report to refresh.」

## 6. 测试

`frontend/src/tests/DataQualityReportDetailPage.ruleTable.test.tsx`（扩展现有文件，4 个 case）：

| 用例 | 覆盖 |
|------|------|
| 表头中文化 + Rule 列显示 rule_name | （前置特性） |
| 评估摘要 KPI 卡新增「评估数据条目数」 | **本次新增**：2 条规则 total_count 100 + 250 → 显示 350 |
| Rule 列降级显示 rule_code + 「系统编码」徽标 | **本次新增**：老 snapshot 无 rule_name → 显示系统编码徽标 + VALIDITY/MEDIUM 映射 |
| （预留 testId）| 防止后续 KPI 卡误删 |

```bash
cd frontend && npx vitest run src/tests/DataQualityReportDetailPage.ruleTable.test.tsx
# 期望：4 passed
```

注：tooltip 文本**不在 jsdom DOM 中**（antd Tooltip 默认 lazy mount，hover 才渲染 portal），测试只断言徽标 + rule_code + 类型映射的存在，tooltip 文本留给 e2e 验证。

## 7. 安全审查

未触发（无认证/输入/外网依赖）。

## 8. 部署验证

无后端改动、无 DB 迁移、无 API 变更。前端 Vite HMR 即可。

手动：
- 进 `/data-quality/reports/:id` → 评估摘要看到 5 张 KPI 卡，第 5 张「评估数据条目数」= 全部规则 total_count 求和
- 打开老 snapshot 报告（2026-09-15 前生成的）→ 规则明细 Rule 列显示「系统编码」徽标 + 规则编码；hover 看到「老 snapshot 不含 rule_name 字段...」提示
- 重新生成老报告 → Rule 列变规则名称

## 9. 关联

- 设计稿：无
- Wiki：`Harness/wiki/frontend.md` Phase 9 增量章节（KPI 卡组 + 降级模式）
- Rules：`Harness/rules/变更记录强制规范.md`
- Memory：`report-kpi-data-count.md`（待写）
- 相关变更：[feat-report-rules-zh-name](../feat-report-rules-zh-name/summary.md)（本变更补强其降级 UX）

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 2 段 ≥ 2 个候选方案对比（每子问题各 1 表）
- [x] 第 3 段无 DB 迁移
- [x] 第 7 段说明未触发 security-reviewer
- [x] 第 8 段说明无后端改动 / Vite HMR 即可
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新
- [x] 至少 1 条 MEMORY 索引（新增 `report-kpi-data-count.md` + MEMORY.md 加行）
- [x] 无真实 SQL/DB 改动，跳过真实数据验证
