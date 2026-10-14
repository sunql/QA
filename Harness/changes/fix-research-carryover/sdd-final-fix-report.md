# final-fix-report — fix/research-carryover 终评修复轮

BASE `8055e15`（分支 fix/research-carryover）。本轮 = must-fix (I001) + 4 条补守卫 + 1 条一致性修复。
所有 pytest 均 `python -m pytest`，unit / integration 分进程，env：
`TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test`
`TEST_NEO4J_URI=bolt://localhost:7688`。

改动文件（5）：
- `backend/app/tests/unit/test_research_resume_decision.py`（项 1 + 项 4）
- `backend/app/tests/integration/test_research_agent_service.py`（项 2）
- `backend/app/tests/unit/test_chart_renderer.py`（项 3）
- `backend/app/tests/integration/test_research_api.py`（项 6）
- `backend/app/services/research_agent_service.py`（项 5，仅注释）

生产代码行为**零改动**（research_agent_service.py 只动注释；chart_renderer.py 为取证临时改过后已逐字节还原）。

---

## 1. MUST FIX — 新 I001（import block unsorted）

**改动**：`test_research_resume_decision.py` 把 `from app.services.research_agent_phases import decideResume`
提到 `from app.services.research_agent_ports import (...)` 之前（isort 按模块路径排序：`phases` < `ports`）。

**证据（before / after）**：
- BEFORE（HEAD 版本，`git show HEAD:... | ruff check --select I001 --stdin-filename ... -`）：
  `I001 [*] Import block is un-sorted or un-formatted --> app/tests/unit/test_research_resume_decision.py:8:1` / `Found 1 error.`
- AFTER：`ruff check --select I001 app/tests/unit/test_research_resume_decision.py` → `All checks passed!`（exit 0）

**同目录其它 I001（不属本项）**：`app/services/research_agent_service.py:34` 在 HEAD 上就有一条 I001
（`from app.config import getSettings` 掉在末尾）；已验证 HEAD 版本同样报，故**非本支新增**，且不在本项范围（未改动它）。

---

## 2. M2 回归守卫缺口 — 补 1 条报告相位计量判别用例

**改动**：`test_research_agent_service.py` 新增 `test_report_metering_records_configured_model_name`
（复用现有 `AliasedLlmClient` 双拼写替身：`_modelName="fake-model"` 配置名 ≠ 响应自报 `"gateway-alias"`），
驱动 `_stageReport` 路径（`autoConfirm=True` 跑到 done），断言交给 recorder 的
`research_report` 记录的 `modelName == "fake-model"`。不 seed `llm_config`（分支刻意设计：只测「传了哪个名字」）。

**RED**（把 `research_agent_phases.py:422` 的 `meteringModelName(client, metered)` 改回 `metered.modelName`）：
```
>       assert report_records[0]["modelName"] == "fake-model"
E       AssertionError: assert 'gateway-alias' == 'fake-model'
E         - fake-model
E         + gateway-alias
FAILED .../test_research_agent_service.py::test_report_metering_records_configured_model_name
1 failed, 1 passed   # 同跑的假设站点用例 test_hypothesis_metering_records_configured_model_name 仍 PASS
```
→ 恰证明终评所述缺口：3/4 计量站点无守卫，改回后全套仍绿（假设站点绿、报告站点此前无覆盖）。

**GREEN**（还原后）：
```
app/tests/integration/test_research_agent_service.py::test_report_metering_records_configured_model_name PASSED
======================== 1 passed, 21 warnings in 1.65s ========================
```
还原核验：`git diff --stat app/services/research_agent_phases.py` 空。

---

## 3. Heatmap / Waterfall 轴名接线断言

**改动**：`test_chart_renderer.py`
- `TestHeatmap` 增 `test_injected_axis_labels_render_dimension_names_on_both_axes`（注入
  `axisLabels={"MATERIAL_NAME": "物料", "SUPPLIER_NAME": "供应商"}`，断言 x 轴名 = dimensions[1] 显示名、
  y 轴名 = dimensions[0] 显示名，且两者都 ≠ 原始列名；取值仍是数据键 `["M1","M2"]`）与
  `test_chat_shape_renders_no_axis_name`（空 `axisLabels` ⇒ 两轴均无 `name` 键）。
- `TestWaterfall` 增 `test_injected_axis_label_renders_dimension_name_and_never_raw_field`
  （注入 `axisLabels={"ITEM": "项目"}` ⇒ `xAxis.name == "项目"`、≠ `"ITEM"`；data 仍 `["期初","采购"]`）与
  `test_chat_shape_renders_no_axis_name`。
形制沿用既有 `test_injected_axis_labels_render_dimension_name_and_never_raw_field`（bar）。

**RED A**（`chart_renderer.py` 把 `_axisNameOf(spec, colField)` / `_axisNameOf(spec, catField)` 改成 `None`，
即删掉 heatmap 列轴与 waterfall 类目轴的接线）：
```
>       assert option["xAxis"]["name"] == "项目"
E       KeyError: 'name'                 # Waterfall
FAILED ...::TestWaterfall::test_injected_axis_label_renders_dimension_name_and_never_raw_field
FAILED ...::TestHeatmap::test_injected_axis_labels_render_dimension_names_on_both_axes   # xAxis KeyError: 'name'
2 failed in 1.06s
```
**RED B**（只把 `_axisNameOf(spec, rowField)` 改成 `None`，独立钉 heatmap 行轴）：
```
        assert option["xAxis"]["name"] == "物料"
>       assert option["yAxis"]["name"] == "供应商"
E       KeyError: 'name'
FAILED ...::TestHeatmap::test_injected_axis_labels_render_dimension_names_on_both_axes
1 failed in 0.77s
```
**GREEN**（还原后；`cp /tmp/cr_backup.py` 回填，`git diff --stat app/services/chart_renderer.py` 空）：
```
app/tests/unit/test_chart_renderer.py::TestHeatmap::test_injected_axis_labels_render_dimension_names_on_both_axes PASSED
app/tests/unit/test_chart_renderer.py::TestHeatmap::test_chat_shape_renders_no_axis_name PASSED
app/tests/unit/test_chart_renderer.py::TestWaterfall::test_injected_axis_label_renders_dimension_name_and_never_raw_field PASSED
app/tests/unit/test_chart_renderer.py::TestWaterfall::test_chat_shape_renders_no_axis_name PASSED
============================== 8 passed in 2.51s ===============================
```

---

## 4. `resumeTurn` 对 `decideResume` 的**使用**无判别 — 结构守卫

**改动的文件**：`test_research_resume_decision.py`（M3 的专测文件；其 docstring 本就声明「端点与
`resumeTurn` 必须给出同一个相位」）。新增 `_functionSource()`（ast 取指定函数源码片段）+
`test_resume_turn_delegates_phase_decision_to_decide_resume`：断言 `decideResume(` 出现在
`research_agent_service.py` 的 **`resumeTurn` 函数体内**（不是全文件搜字符串，避免别处出现即假绿）。
判据有牙：只在 resumeTurn 体内搜，且找不到函数即断言失败。

**RED**（把 `resumeTurn` 内 `decision = decideResume(checkpoint, action, choice)` 改为等价的
`decision = _inlineResumeDecision(...)`，模拟「又长回内联判定」）：
```
>       assert "decideResume(" in body, (
E       AssertionError: resumeTurn 未调用 decideResume：恢复决策 SSOT 被破坏（又长出第二份判定）
FAILED ...::test_resume_turn_delegates_phase_decision_to_decide_resume
1 failed in 0.82s
```
**GREEN**（还原后）：
```
app/tests/unit/test_research_resume_decision.py::test_resume_turn_delegates_phase_decision_to_decide_resume PASSED
============================== 7 passed in 2.25s ===============================
```
还原核验：`git diff app/services/research_agent_service.py` 只剩项 5 的注释 2 行。

---

## 5. 注释漂移

**改动**：`research_agent_service.py:430-431`（`_pauseForUser` 里 checkpoint turn 载荷的注释）——
原「仅作展示/调试用途：恢复点 SSOT 是 `checkpoint.options["resumePhase"]`」改为
「恢复点 SSOT 是 `decideResume`（联合 checkpoint.phase / action / choice / options 判定），不是本字段」。
仅注释措辞，无行为影响。（`resumeTurn` 的 docstring 未动：它描述的是默认路径，`resumePhase` 在该路径仍是 SSOT。）

---

## 6. 测试里的裸字面量

**改动**：`test_research_api.py` `test_checkpoint_answer_at_planning_modify_reports_replan_phase` 里
`options={"signal": "fixed_plan", "resumePhase": "execute", "replanCount": 0}` 的裸键改为常量
`OPT_REPLAN_COUNT: 0`，并 import `from app.services.research_agent_ports import OPT_REPLAN_COUNT`。
（全仓仅此一处测试裸写 `"replanCount"`，见 `grep -rn '"replanCount"' app/tests/`。）
一致性修复，非守卫修复（M1 结构守卫按设计不扫测试文件）。

---

## 全套回归（分进程）

- 单元（超集：`test_research_*.py` + `test_chart_*.py` + `test_report_planner.py`，22 文件）：
  **1 failed / 400 passed**。唯一红 = 已知继承红 `test_research_event_bus.py::test_error_specs_covers_exactly_the_ruled_codes`（不属本轮）。
- 集成（7 文件，与基线同集）：**3 failed / 119 passed**。3 红全在
  `test_research_hypothesis_rewrite_wiring.py`（已知继承红）；passed 由基线 118 → **119**（+1 = 项 2 新用例）。
  ⇒ 本轮唯一 delta = 新增用例通过。
- `ruff check`（本支改动的 4 个测试文件）：`All checks passed!`。

## 未做（维持终评「accept-as-is」）

- 另 2 处计量站点（`research_agent_phases.py:386` hypothesis rewrite、`research_agent_execution.py:536` chart）
  的守卫按派单只补了报告站点；这两条与报告站点同构、同一 `meteringModelName` 调用模式，未再逐一补测（派单范围）。
- `research_agent_service.py:34` 的既有 I001（HEAD 即有）、`ChartSpec.axisLabels` 导致 `hash(spec)` 抛错、
  维度取值翻译等其它 deferred Minors — 均按终评裁定 accept-as-is，不在本轮范围。
