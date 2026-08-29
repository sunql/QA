# 变更：多步执行计划消失 —— 规则拆分凌驾 LLM 判定 + 单步统一展示

- **日期**：2026-08-16
- **作者**：AI 助手
- **Phase**：bugfix（多步 NL2SQL 拆步判定 + 单步执行计划展示）
- **状态**：done

## 1. 需求

用户报障：智能问答中"执行计划"（多步拆解卡 `MultiStepPlanCard`）不显示，只剩"查询计划"（单步 `QueryPlanCard`）。

用户原句：

> "第一步查 2025 年 1-5 月各供应商的采购数量和金额，第二步查 2026 年同期，第三步对比两年变化并分析趋势"

验收标准：
- 「第X步」等显式标号 → 规则拆分，零 LLM 调用，多步计划卡正确渲染。
- 顺序指令型问题（"先…再/然后…"）继续走 LLM 拆步，无回归。
- 单步查询也下发 `multi_step_plan` + `step_plan` + `step_result` 三事件，前端始终用 `MultiStepPlanCard` 渲染（统一体验）。
- 单步不可答、单步失败回退多步、显式分步三种路径的事件序列与现有前端 handler（`onStepPlanOverview`/`onStepPlan`/`onStepResult`）契约一致。
- 单元测试 + 集成测试全量通过、无回归。

## 2. 设计评审

### 根因

两层问题（详见 `wiki/qa-system/regression-multi-step-plan-missing.md`）：

1. **拆步 LLM 否决权**：`is_explicit_multi_step` 已识别关键词 `第一步`（用户原句含），但命中后会再调拆步 LLM（`_plan_by_llm`），LLM 看到"对比 + 分析趋势"字样倾向"可单条 SQL 完成"返回 `isMultiStep: false`，多步被静默吞掉。
2. **单步无执行计划展示**：即使走单步，前端只有 `QueryPlanCard`，无 `MultiStepPlanCard`。事件序列不包含 `multi_step_plan` / `step_plan` / `step_result`。

### 决策

1. **规则拆分凌驾 LLM 判定**：用户用「第X步」显式标号时（`第[〇一二...十百千万0-9]+步` 命中 ≥2 次），跳过 LLM 拆步直接规则拆分。新增独立方法 `StepQueryPlanner.plan_explicit()` 与静态 `rule_based_split()`，**不动** `plan()`（守约 `test_plan_always_calls_llm`）。调用方（`ChatService`）先调 `plan_explicit`，未命中再调 `_detectMultiStep`（原 LLM 拆步路径）。
2. **统一展示**：单步也下发三事件，与多步事件序列同构（`multi_step_plan → step_plan → plan/sql/chart → step_result → done`）。前端 `chatStore.onStepPlanOverview / onStepPlan / onStepResult` 三个 handler 已支持 1 步场景，`MultiStepPlanCard` 用 antd `Steps` 组件支持 N=1，**前端零改动**。
3. **复用现成辅助**：`_summarizeStepData` / `_stepResultEvent` / `_step_result_to_read` / `_clipText` 均已存在，直接复用避免重复实现。

### 与既有修复的关系

- 上游：`feat-multi-step-nl2sql`（多步特性首发）、`fix-multi-step-explicit-trigger`（顺序指令放宽）。
- 本次彻底解决 LLM 否决权问题，并补齐单步的执行计划展示缺口。

## 3. 数据模型变更

无。

## 4. 接口契约变更

`ChatResponse.steps` 字段语义扩展：

| 之前 | 之后 |
|------|------|
| 单步：`steps=None` | 单步：`steps=[1 元素 StepResultRead]` |
| 不可答：`steps=None` | 不可答：`steps=[1 元素，description="无法回答"]` |
| 多步：`steps=[N 元素]` | 多步：保持不变 |

SSE 事件序列扩展（单步路径新增 4 个事件：`multi_step_plan` / `step_plan` / `step_result`）：

| 路径 | 之前 | 之后 |
|------|------|------|
| 单步正常 | `meta → plan → sql → chart → token×N → done` | `meta → multi_step_plan → step_plan → plan → sql → chart → token×N → step_result → done` |
| 单步不可答 | `meta → plan? → token → done` | `meta → multi_step_plan → step_plan → plan? → token → step_result → done` |
| 多步（不变） | `meta → multi_step_plan → step_plan/step_result × N → token → done` | 同左 |

前端 handler 无需调整（chatStore.ts:163-195 + MessageItem.tsx:79-83 + MultiStepPlanCard.tsx 已支持）。

## 5. 实现要点

### `backend/app/services/step_query_planner.py`

- 新增常量：
  - `_RULE_STEP_PATTERN = re.compile(r"第[〇一二三四五六七八九十百千万0-9]+步")`
  - `_RULE_DESCRIPTION_LIMIT = 20`
  - `_RULE_AGG_HINT = "请基于前序步骤结果汇总对比"`
  - `_RULE_STRIP_CHARS = "，,。、：; "`
- 新增静态方法 `rule_based_split(question)`：≥2 个「第X步」标号时直接切句子，每段作数据步，末尾追加 `aggregation_only=True` 的汇总步。复用 `multi_step_plan._clip_text` 做 description 截断。无 LLM 调用。
- 新增异步方法 `plan_explicit(question)`：先调 `rule_based_split`，命中返回 `StepPlanResult(plan=...)`（token=0）；未命中返回 `StepPlanResult(plan=None)`，调用方回退到 `_detectMultiStep`。
- `plan()`（line 62-77）**未改动**，保留 `test_plan_always_calls_llm` 守约。

### `backend/app/services/chat_service.py`

- 新增 `_resolveExplicitMultiStep(session, dto, pc)` 方法：先调 `plan_explicit`，未命中调 `_detectMultiStep`，返回 `(MultiStepPlan | None, step_tokens, step_cost)`。
- `processMessage` line 303-314 与 `_streamQuery` line 1231-1244 的"显式分步 → 直接多步"分支改为统一调用 `_resolveExplicitMultiStep`。
- 新增静态方法 `_singleStepOverview(description, sub_question)` 与 `_singleStepStart(description, sub_question)`，封装单步的 `multi_step_plan` + `step_plan` 事件。
- `_streamQuery` 单步正常路径（line ~1310）：在 `EVENT_PLAN` 之前 yield `multi_step_plan` + `step_plan`；在 `EVENT_DONE` 之前 yield `EVENT_STEP_RESULT`（携带 sql/data/summary）。
- `_streamQuery` 单步不可答路径（line ~1272）：同样插入三事件，`step_result` 用 `error="..."` 表示不可答。
- `processMessage` 单步正常路径（line 369+）：`ChatResponse.steps=[1 元素 StepResultRead]`。
- `processMessage` 单步不可答路径（`_unanswerableResponse`，line 1635+）：`ChatResponse.steps=[1 元素，description="无法回答"]`。
- 单步失败回退到多步（line ~322-339 / line ~1320+）的 `_detectMultiStep` 路径**未改动**（守约 `test_single_step_failure_falls_back_to_multi_step`）。

## 6. 测试

### 单元测试

- `test_step_query_planner.py`：新增 `TestRuleBasedSplit` 类，10 个测试：
  - 中文/阿拉伯数字拆分
  - 单标号/无标号守约
  - 标点剥离（半角 + 全角 `；` `：` `。` `，` `、`，含全角空格 `　`）
  - 对比类问题不误匹配
  - description 截断
  - `plan_explicit` 命中/未命中（验证零 LLM 调用）
- `test_chat_service_stream.py`：
  - `TestStreamingPipeline.test_query_stream_emits_full_event_sequence` 与 `TestUnanswerablePlanStream.test_short_circuits_without_sql_or_execution` 更新断言：单步路径事件序列现为 `meta → multi_step_plan → step_plan → plan → sql → chart → ... → step_result → done`。
  - 新增 `TestSingleStepExecutionPlanHelpers`（3 例）：验证 `_singleStepOverview` / `_singleStepStart` 的 `stepIndex` / `aggregationOnly=False`（前端 `isStepPlanOverviewItem` 过滤的硬要求）/ `subQuestion` 关键不变量。

**单测全量：712 passed**（+10 新增）。

### 集成测试（真实 PG）

- `test_chat_multi_step.py`：
  - `test_single_step_success_does_not_decompose`：line 205 改为 `body["steps"] is not None and len == 1`，新增 sql/error 字段断言。
  - 新增 `test_explicit_step_marker_skips_llm_decomposition`：验证「第X步」问句零拆步 LLM 调用 + step_plan 用量为 0 token（规则路径审计一致性）。
  - 新增 `test_single_step_renders_execution_plan`：单步返回 `steps=[1 元素]`。
- `test_chat_stream_api.py::test_streams_full_pipeline_events`：更新断言为新事件序列。

**集成测试全量：907 passed**（+2 新增 + 3 更新），无回归。

### Code Review 修复（HIGH/MEDIUM，2026-08-16）

code-reviewer agent 审查后修复的 4 项：

| 严重度 | 问题 | 修复 |
|--------|------|------|
| HIGH | 规则路径无 step_plan 审计行（按 purpose 聚合时缺失） | `_resolveExplicitMultiStep` 在规则命中时补 `_recordUsage(0, 0, purpose="step_plan")`；测试断言 `["answer", "nl2sql", "nl2sql", "nl2sql", "step_plan"]` + `step_plan_rows[0].prompt_tokens == 0` |
| MEDIUM | `_RULE_STRIP_CHARS` 用 ASCII `;` 而非全角 `；` | 替换为 `"，,。、：； 　"`（含全角分号 + 全角空格） |
| MEDIUM | `_singleStepOverview` / `_singleStepStart` 无独立单测 | 新增 `TestSingleStepExecutionPlanHelpers`（3 例） |

## 7. 安全审查

- 新规则为纯字符串切分（`re.finditer`），不调 LLM、不碰 SQL；输入仅做 strip 标点，无注入面。
- `_RULE_STRIP_CHARS` 限定为 `，,。、：; `（句中常见分隔符），不做 SQL 元字符剥离（与 `_sanitize` 职责正交）。
- 新事件字段均为后端构造，前端 `isStepPlanOverviewItem`（`api/chat.ts:72`）已校验 `aggregationOnly` 必须为 boolean，新代码严格遵守。

## 8. 部署验证

- 单元测试全量通过即验证（核心逻辑纯函数判定）。
- 集成测试通过真实 PG + 完整 API 链路，验证 SSE 事件序列、HTTP 响应字段、DB 用量记录。
- 端到端手测需在聊天框输入用户原句"第一步查…第二步…第三步…"，前端 `MultiStepPlanCard` 应渲染 4 个步骤（3 数据 + 1 汇总），后端日志输出"拆步规则命中（第X步标号），跳过 LLM 判定"。

## 9. 关联

- 上游：`feat-multi-step-nl2sql`（多步特性）、`fix-multi-step-explicit-trigger`（顺序指令放宽）、`fix-groupby-qualified-timebucket`（使单步 plan 校验通过）。
- 知识库：`wiki/qa-system/regression-multi-step-plan-missing.md`（回归排查路径）。
- Wiki：`Harness/wiki/nl2sql-engine.md`（NL2SQL 引擎总体说明）。