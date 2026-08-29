# 变更：多步「分析计划」不显示 —— 顺序指令未命中显式分步信号

- **日期**：2026-08-15
- **作者**：AI 助手
- **Phase**：bugfix（多步 NL2SQL 拆步判定）
- **状态**：done

## 1. 需求

用户报障：复合问题（"先找到 2025 年采购金额最多的 20 种物料，分析其 2025 年采购数量每月变化趋势，然后把 2026/2025 年含税价格对比"）现在看不到「查看分析计划」（多步计划卡）。

验收标准：
- 顺序指令型问题（"先…然后/再/接着…"）能被识别为显式分步，直接走多步并下发 `multi_step_plan` 事件，前端渲染「分析计划」卡。
- 单独的"对比/比较/再/随后"（无「先」）不误触发，仍走单步优先。
- 单元测试全量通过、无回归。

## 2. 设计评审

### 根因
前端渲染管线（`chatStore.onStepPlanOverview` → `MultiStepPlanCard`）与后端 SSE（`_streamMultiStep` 下发 `multi_step_plan`/`step_plan`/`step_result`）链路完整无误。问题在**触发条件**：`_streamQuery` 仅在 `StepQueryPlanner.is_explicit_multi_step()` 返回 True 时才调用拆步判定。该函数的关键词集为 `("分步", "逐步", "一步步", "拆步", "第一步")`，不含"先…然后/再…"顺序指令。用户的复合问题缺这些关键词 → 走单步优先；单步 plan 校验修复后（见 [[fix-groupby-qualified-timebucket]]）不再报错，单步执行未触发失败回退 → 永不进入多步，故「分析计划」卡不出现。

这与 `feat-multi-step-nl2sql` 需求自相矛盾：需求示例即"先查 2024 年销售额，再查 2025 年，对比给出趋势"，但实现的关键词集覆盖不到该句式。

### 决策
`is_explicit_multi_step` 增加顺序指令信号：`「先」 + 「然后/再/接着/随后/接下来」` 同时出现即视为显式分步。保守起见仅当「先」与顺序连词同时出现才判定，避免单个"对比/再/随后"误触发。最终是否真拆步仍由拆步 LLM（`StepQueryPlanner.plan`）判定，关键词只是零成本的"是否值得调拆步 LLM"预筛，故放宽不改变最终正确性，仅多一次拆步 LLM 调用。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无对外 API/DTO 变更。仅 `is_explicit_multi_step` 判定逻辑拓宽。

## 5. 实现要点

- `app/services/step_query_planner.py`：
  - 新增 `_SEQUENTIAL_START = "先"`、`_SEQUENTIAL_CONJUNCTIONS = ("然后", "再", "接着", "随后", "接下来")`。
  - `is_explicit_multi_step()`：先查原关键词集，再查「先」+顺序连词组合；更新 docstring 说明顺序指令与最终仲裁。

## 6. 测试

- `test_step_query_planner.py`：
  - `test_explicit_signal_true` 增 3 例：先…再…、先…然后…、先…接着… → True。
  - `test_no_explicit_signal_false` 增 2 例：单个「再」、单个「随后」无「先」→ False（守住院有"对比"单步优先语义）。
- 全量单元测试 **696 passed**（+5），无回归。
- `test_chat_multi_step.py` 增 1 例（parametrize ×2 句式）：
  `test_sequential_instruction_triggers_multi_step`——「先…再…」「先…然后…」无「分步」关键词，
  用 `_OkAdapter`（单步可成功）验证直接走多步（intent=multi_step + steps + 拆步 LLM 被调用）。
- 集成测试（真实 PG）：`test_chat_multi_step.py` 6 passed；全量 `app/tests/integration/` **196 passed**（+2），无回归。

## 7. 安全审查

- 无新增安全面：纯字符串判定，不调 LLM、不碰 SQL；放宽后仍以拆步 LLM 与 SQL Guard 为准。

## 8. 部署验证

- 单元测试全量通过即验证（纯函数判定逻辑，无数据/运维动作）。

## 9. 关联

- 上游：`feat-multi-step-nl2sql`（多步特性，单步优先策略）。
- 同源修复：`fix-groupby-qualified-timebucket`（使单步 plan 校验通过，暴露本触发缺口）。
- Wiki：`Harness/wiki/nl2sql-engine.md`
