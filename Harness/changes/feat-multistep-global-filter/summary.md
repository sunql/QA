# feat-multistep-global-filter — 多步问题跨步骤共享过滤继承

## 背景

多步问题（`第一步...第二步...第三步...`）的口径经常跨步骤共享，但当前实现只能把
"前序步骤的实体列表"作为 WHERE IN 筛选条件传给后续步骤（[entity_list] / [aggregate]
标签机制），范围类过滤（外购/内外贸/站点/物料类别/财年等）只能在每一步的子问题里
单独出现——LLM 偶尔会丢，导致步骤之间口径漂移。

复现案例（2026-09-19 用户实测）：
- 第一次询问 3 月三步问题，步骤 1 SQL 含 `TCLCOD_0 IN ('A02'~'A05')` +
  `INTER_COM_CODE='1'` + `INTER_SITE_CODE='1'`；步骤 2/3 漏掉相同条件，物料明细
  范围略宽于步骤 1 排名口径。
- 第二次追问 4 月，全部步骤都加上了相同过滤。
- 期望：无论第几次询问，所有数据步骤 SQL 必须沿用相同的范围类 WHERE 条件。

## 设计：A+B 双层落地（2026-09-25）

参考 [[qa-system-feuture-follow-up-cascade]] 的 A+B+C 分级策略，本特性只用 A+B 两层
（C 风险过大，已在前置讨论中确认不采用）：

### A 层：措辞强化（不消耗 LLM token）

修改 plan / SQL 两个阶段 prompt 的「前序步骤结果」头部文案：
- 旧：「可作为后续步骤的筛选条件使用」
- 新：「**必须沿用**前序步骤的范围类 WHERE 条件——外购/内外贸/站点/物料类别/
  财年等跨步骤口径约束；只有当该过滤已被聚合列或前序 JOIN 的实体限定完整覆盖时才
  可省略」

适用位置：
- `app/services/nl2sql_service.py` `_renderStatePart()` — system prompt 头部文案
  （plan / SQL 共用）
- `app/domain/multi_step_plan.py` `StepExecutionContext.inject_to_prompt()` —
  注入产物的头部文案（与 system prompt 同步）

A 层覆盖了"前序步骤过滤条件"在两个阶段的注入，但只对步骤 2+ 生效——步骤 1 没有
前序结果可继承。A 层必须配合 B 层才能闭环。

### B 层：跨步骤约束预抽取（一次 LLM 调用，多步复用）

新增「全局过滤提取器」LLM 调用，从多步主问题中抽取跨步骤共享的范围类约束
（`global_filters`），渲染进每一步 plan / SQL prompt 的 `[global_constraints]` 块。

数据流：
1. `ChatService._resolveGlobalFilters(session, dto, pc)` — 仅在 NEW_QUERY /
   QUERY 意图的多步场景调用一次
2. `StepQueryPlanner.extract_global_filters()` — 调 LLM，返回 `GlobalFilters`
   frozen dataclass；失败降级返回 None（仅靠 A 层措辞）
3. `ChatService._executeMultiStep(global_filters=...)` / `_streamMultiStep(.)`
   — 把 `GlobalFilters` 注入 `StepExecutionContext.global_filters`
4. `StepExecutionContext.with_step()` 保留 `global_filters`（沿用每一步）
5. `inject_to_prompt()` 在注入产物渲染 `[global_constraints]` 块（步骤 0 也渲染）
6. `_planAndGenerateSql(global_filters=...)` 抽出 `global_filters.text` 透传给
   `_twoStageGenerate(globalFiltersText=...)`
7. `Nl2SqlService.generateValidatedPlan` / `generateQueryPlan` 透传到
   `_buildPlanUserPrompt(globalFiltersText=...)`
8. `_renderGlobalConstraintsPart()` 渲染 `[global_constraints]` 块到 plan user prompt
   （与 `<scope_hint>` / schema 段不冲突）

每步只多一次 LLM 调用（仅第一次多步规划时），后续步骤零开销（`global_filters`
是 frozen dataclass，直接 `dataclasses.replace` / 字段透传）。

## 实现细节

### 文件改动

| 文件 | 改动 |
|---|---|
| `app/domain/multi_step_plan.py` | 新增 `GlobalFilters` frozen dataclass；`StepExecutionContext` 加 `global_filters` 字段；`inject_to_prompt` 渲染 `[global_constraints]` 块；`with_step` 沿用 `global_filters`；A 层措辞强化 |
| `app/services/step_query_planner.py` | 新增 `extract_global_filters()` async 方法 + `_EXTRACT_GLOBAL_FILTERS_SYSTEM_PROMPT` |
| `app/services/nl2sql_service.py` | 新增 `_renderGlobalConstraintsPart()` helper；`_buildPlanUserPrompt` / `generateQueryPlan` / `generateValidatedPlan` 加 `globalFiltersText` 透传；A 层 `_renderStatePart` 措辞强化 |
| `app/services/chat_service.py` | 新增 `_resolveGlobalFilters()` helper；`_executeMultiStep` / `_streamMultiStep` 接 `global_filters` kwarg；`_planAndGenerateSql` 透传 `global_filters` 到 `_twoStageGenerate(globalFiltersText=)`；6 处多步入场点统一调用 `_resolveGlobalFilters` |
| `app/tests/unit/test_step_query_planner.py` | 新增 `TestExtractGlobalFilters` 4 用例 |
| `app/tests/unit/test_multi_step_plan.py` | 新增 `TestGlobalFiltersWording` / `TestGlobalFiltersRendering` 6 用例；强化 A 层头部文案断言 |
| `app/tests/unit/test_nl2sql_service.py` | 新增 `TestGlobalConstraintsPromptInjection` 4 用例（含端到端 `generateQueryPlan` 透传） |
| `app/tests/integration/test_multistep_global_filter.py` | 新增端到端集成测试 4 用例：3 步 plan prompt 全部含 `[global_constraints]`；抽取仅一次；audit 含 `multistep_global_filter`；抽取失败降级 |

### B 层 LLM Prompt

```
你是全局过滤提取器。从用户的多步复合问题中识别跨步骤共享的范围类 WHERE 条件
（外购/内外贸/站点/物料类别/财年等口径约束）。
返回 JSON：{"global": [{"table": "...", "column": "...", "op": "IN|=|>=", "value": "..."}], "step_overrides": []}
若问题无明显跨步骤口径约束，返回 {"global": [], "step_overrides": []}。
```

约束条目渲染示例：

```
- DWD_PURCHASE_ORDER_DTL.TCLCOD_0 IN A02,A03,A04,A05
- DWD_PURCHASE_ORDER_DTL.INTER_COM_CODE = 1
- DWD_PURCHASE_ORDER_DTL.INTER_SITE_CODE = 1
```

### 不可变数据模式

- `GlobalFilters` 是 frozen dataclass（`@dataclass(frozen=True)`）
- `StepExecutionContext.global_filters` 字段 + `with_step` 衍生新实例（不变原实例）
- `replace(result, intent=IntentType.FOLLOW_UP)` 模式遵循前置修复（参考
  [[qa-system-agent-tool-binding]] dataclass 模式）

## 不采用 C 层（兜底补丁）

前置讨论（2026-09-25）确认 C 层（运行时比对前后 SQL 漏掉的过滤条件并自动补回）风险过大：
- 跨表作用域扩张（OR 子句中过滤条件可能不该沿用）
- 时间窗口漂移（步骤 1 限定 3 月的过滤不该无条件复制到步骤 3）
- `INTER_SITE` 等语义反向（外购 + 内贸条件被错误复制到不需要内贸限定的步骤）
- 聚合上下文不一致（步骤 1 的过滤列在步骤 2 已被聚合吃掉）
- NULL 处理歧义（自动补回时 NOT IN/IN 的 NULL 行为不同）
- 子查询边界（CTE / 嵌套子查询中的过滤不该跨边界补）
- 人为故意非对称（业务上步骤 2 故意放开站点过滤做对比）

A+B 已覆盖用户场景（步骤 1 与后续步骤口径一致），不再做 C。

## 决策日志

- 2026-09-25：A+B 落地确认（用户）。本 summary 是 SSOT。
- 2026-09-25：调试历程——发现 step 1 仍漏过滤，根因是 `_buildPlanUserPrompt` 没有
  渲染 `[global_constraints]`（只在 `inject_to_prompt` 即 system prompt 中渲染）；
  修复：新加 `_renderGlobalConstraintsPart` helper + `_buildPlanUserPrompt(globalFiltersText=)`
  + 沿链路透传到 `ChatService._planAndGenerateSql`。
- 2026-09-25：调试历程——发现 step 2 仍漏过滤，根因是 `StepExecutionContext.with_step`
  没有沿用 `global_filters` 字段；修复：`with_step` 加 `global_filters=self.global_filters`。

## Backlog

- **追问入场点已收敛（2026-09-26）**：本文件 §实现要点写的「6 处多步入场点统一调用
  `_resolveGlobalFilters`」现已变为「4 处显式调用 + 追问前置 SSOT 1 处」——B/C 两个追问入场点
  的抽取收敛进 `_prepareFollowUpMultiStep`，因为其中 C 那处曾漏传（见
  [fix-c-fallback-global-filters](../fix-c-fallback-global-filters/summary.md)）。
- Step 1 plan prompt 已有 `[global_constraints]`，但 SQL 阶段（生成 SQL 时）尚未单独
  验证。当前 system prompt 透传机制使 step 1 SQL 也看到了该块（`_renderStatePart` 包裹
  在 `<previous_query_state>` 内），但渲染位置不同——后续可统一为「user prompt 段」
  形态便于审计。
- 抽取 prompt 可视化：管理端查看「这条多步问题抽到了哪些跨步约束」尚无 UI（仅 audit
  日志）。
- 跨方言：当前约束格式假设 SQL 通用语法（IN / =）；方言特有算子（Oracle ROWNUM 形式）
  暂未适配。