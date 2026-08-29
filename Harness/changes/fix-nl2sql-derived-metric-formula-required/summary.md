# 变更：占比/比率/百分比派生指标 formula 必填硬约束

- **日期**：2026-08-17
- **作者**：QA System
- **Phase**：bugfix（NL2SQL 计划校验 + prompt）
- **状态**：done

## 1. 需求

用户报障：多步问题中"首先统计4月份采购订单数量，其次统计4月份主要top10采购物料的占比..."的 Step 2 失败被标 `error="无法回答（LLM 判定无有效查询计划）"`，Step 3 因依赖被卡。

用户原句：

> "首先统计4月份采购订单数量，其次统计4月份主要top10采购物料的占比，然后看这top10物料在5月份下的订单数量信息分析"

验收标准：
- 问题含「占比/比率/百分比/比例/ratio/percent/share/pct」→ 计划阶段 prompt 强引导 + validatePlan 硬约束 alias 必须 formula（窗口函数 SUM(x)/SUM(SUM(x)) OVER ()）
- 重试链路下 LLM 在反馈注入下能自愈（带可操作 hint：窗口函数 + property 填真实属性）
- 普通聚合 alias（如 `TOTAL_QTY`、`COUNT_NUM`）不被误伤
- 已有 `test_formula_with_valid_properties_passes` 等守约用例继续通过
- 单测 + 关联下游测试全量通过、无回归

## 2. 设计评审

### 根因（两步叠加）

1. **prompt 误导**：`_buildPlanSystemPrompt`（`nl2sql_service.py:1736-1737`）的示例用 `数量` 当 formula 的属性占位符。LLM 照抄生成 `formula="SUM(数量) / SUM(SUM(数量)) OVER ()"`，但 PORDERQ 实际属性名是 `采购数量`（`QTYUOM_0`）。
2. **校验漏判**：`validatePlan`（`nl2sql_service.py:1418-1437`）校验 formula 引用的属性：抽出 `数量` → 不在本体 → 报"公式中的属性 数量 不属于选定的任何类" → `maxPlanAttempts=2` 重试耗尽 → `Nl2SqlError` → 多步循环收纳为 `error="无法回答"`。

且 prompt 规则 4（`nl2sql_service.py:1747-1753`）把 formula 标"可选"，无强引导；alias="占比" 不带 formula 时 validatePlan 也不拦截（占比沦为普通列别名，SQL 不算百分比）。

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| (A) ontology 加 metric 字段（formula 在 schema 层定义） | ❌ v1 不做 | 需 alembic 迁移路径 + buildSchemaText 渲染改造，超出 bug fix 范围 |
| (B) 仅改 prompt | ❌ 不可行 | LLM 仍有概率抄错属性名；prompt 启发式无法穷举 |
| **(C) prompt 改占位 + 规则 4 强约束 + validatePlan 硬约束** | ✅ 推荐 | 双层防御：prompt 减小出错面 + 代码层兜底拦截漏网；与 2026-08-14 sortBy 派生指标规则同款 |

### 关键设计点

1. **不动 `Aggregation.formula` 字段定义**（`query_plan.py:34` 保持 `str \| None = None`）—— formula 是可选字段本身合理（普通 SUM 不需要 formula），硬约束只在 alias 暗示派生指标时触发。
2. **alias 关键词集合收紧**：中英文常见命名 `占比 / 比率 / 比例 / 百分比 / ratio / percent / share / pct`，大小写不敏感。匹配策略：alias 经 `lower()` 后 contains 任一关键词。"总占比" 这种复合名含"占比"仍触发——确实应是派生指标，宁可过度保守（让 LLM 多写 formula），不让漏报（占比列算错比多写一个公式后果严重）。
3. **报错文案必须可操作**——直接告诉 LLM 用什么公式结构、property 取什么。这与 `sortBy_undeclared_derived_alias_reports_actionable_hint`（2026-08-14）已有的同款"带示例引导"风格一致。
4. **prompt 规则 4 强约束带例外**："问题含 X 时必填"，而不是"任何聚合都必填"——避免破坏普通 `SUM(数量) AS TOTAL_QTY` 这类最常见用法。
5. **不动 `_buildSystemPrompt`（SQL 阶段）的规则 8**（`nl2sql_service.py:1845`）——已正确引导"按 formula 生成 SUM(x)/SUM(SUM(x))OVER()"，本次不动。

### 用户误解澄清

用户认为"Step 1 详细信息有了"就能推 Top 10。**实际上 Step 1 返回的是 `[{订单数量: 3734, 采购数量: 84267197.99}]` 合计单行**，数学上**推不出** Top 10——Step 2 必须 GROUP BY 物料才能算出每种物料的采购量与占比。这是**合法的独立 NL2SQL 查询**，不是"应复用 Step 1 数据"。

## 3. 数据模型变更

无。`Aggregation.formula` 仍为可选（`query_plan.py:34`），硬约束在 validatePlan 层。

## 4. 接口契约变更

无 API 契约变更。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/services/nl2sql_service.py` | (1) 加模块级常量 `_DERIVED_METRIC_ALIAS_KEYWORDS`（中英文 8 个关键词）；(2) 加 helper `_aliasRequiresFormula(alias: str \| None) -> bool`；(3) `validatePlan` aggregation 循环内加 `_aliasRequiresFormula(agg.alias) and not agg.formula` 检查，返回 actionable issue；(4) prompt 示例 `数量` → 显式占位 `<当前选中类的真实属性名>`；(5) 规则 4 改"formula 可选"为"占比/比率/百分比必填 + 窗口函数 + property 用真实属性名 + 严禁照抄占位符" |
| `backend/app/tests/unit/test_query_plan_validation.py` | +4 用例：alias="占比" 无 formula 拒 + 窗口函数 hint；alias="占比" 有 formula 通过守约；alias="RATIO" 无 formula 拒（英文 case-insensitive）；alias="TOTAL_QTY" 无 formula 不被拦守约 |
| `backend/app/tests/unit/test_query_plan_generation.py` | +2 用例：(a) `_buildPlanSystemPrompt` 输出含"必填"+"窗口函数"+ 显式占位；(b) 端到端：占比 alias 无 formula → 校验拒绝 → 重试带 hint → 加 formula 通过 |

## 6. 测试

### 新增（6 条用例）

- `test_alias_zhanshi_without_formula_reported_with_window_function_hint`：真实回归，alias="占比" 无 formula 拒 + hint 含窗口函数/property 引导
- `test_alias_zhanshi_with_formula_passes`：守约与 `test_formula_with_valid_properties_passes` 一致
- `test_alias_ratio_en_without_formula_reported`：英文 RATIO 命中（大小写不敏感）
- `test_alias_ordinary_total_qty_not_flagged`：普通聚合不被拦守约
- `test_plan_prompt_marks_zhanshi_formula_mandatory`：prompt 显式占位 + 必填语气守约
- `test_zhanshi_alias_without_formula_self_heals_on_retry`：端到端 retry 自愈链路守住

### 覆盖率

`nl2sql_service.py`：`_aliasRequiresFormula` / `_DERIVED_METRIC_ALIAS_KEYWORDS` 100% 覆盖。

### 回归

- `test_query_plan_validation.py`：**38/38 通过**（含新增 4 条）
- `test_query_plan_generation.py` + `test_nl2sql_service.py`：**66 + 60 = 126 通过**（含新增 2 条）
- `test_step_query_planner.py`：**43/43 通过**（多步拆步链路无影响）
- `test_chat_service.py` + `test_chat_service_stream.py`：**96/96 通过**（多步循环无影响）
- `test_scope_row_limit.py`：**38/38 通过**（相邻范围感知链路无影响）

**合计 240+ 条单测全绿。**

## 7. 安全审查

- `_aliasRequiresFormula` 是简单 contains 检查，无正则 → **无 ReDoS**。
- 关键词集合是固定 tuple，LLM 不可注入新关键词。
- 报错文案只进 user prompt（经 `_sanitizeContext` 转义），不进入 SQL/Shell/文件系统 → 无注入面。
- validatePlan 返回 issues 经 `_buildPlanUserPrompt` 拼接后由 LLM 消费（用户问题经 HTML 转义），无 prompt injection 风险。

## 8. 部署验证

无环境变量改动，无迁移，无 API 变更。

### 手动冒烟

| 问题 | 期望行为 |
|---|---|
| "统计4月份主要top10采购物料的占比" | Step 1 成功；Step 2 生成 `alias="占比"` + `formula="SUM(采购数量)/SUM(SUM(采购数量))OVER()"` 的 SQL；占比列正确显示百分比；Step 3 不被卡 |
| "各供应商收货数量占比" | 同上，property 取 QTY |
| "统计收货数量"（无占比字样） | 普通 SUM 聚合，alias 取 TOTAL_QTY，无 formula（守约） |
| "各年份销售额比例" | alias="比例" → 硬约束触发，LLM 在重试时加 formula |

### 关键提示文案（守约）

```
聚合别名 占比 是派生指标（占比/比率/百分比/比例/ratio/percent/share/pct），
必须使用 formula 表达式（窗口函数 SUM(x)/SUM(SUM(x)) OVER ()），
且 property 须填当前选中类的真实属性名；
若想用 SUM/COUNT/AVG 等基础聚合命名，请改用别名如 TOTAL_QTY/COUNT_NUM
```

## 9. 关联

- 调研（SSOT 在前）：`Harness/wiki/` 待补"NL2SQL 派生指标 formula 硬约束"小节
- 历史变更：`Harness/changes/fix-multi-step-ordinal-adverb-split/summary.md`（2026-08-17，多步拆步序数副词）
- 历史回归：2026-08-14 sortBy 派生指标未声明公式聚合（已修），本次复用同款"actionable hint" 风格
- 关联 fix：`fix-multi-step-ordinal-adverb-split` 修复后用户三步问题才走到 Step 2，Step 2 失败暴露本次 bug