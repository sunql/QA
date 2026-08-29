# 变更：多步漏拆 —— 序数副词序列规则拆分（首先/其次/再者/最后）

- **日期**：2026-08-17
- **作者**：QA System
- **Phase**：bugfix（多步 NL2SQL 拆步判定）
- **状态**：done

## 1. 需求

用户报障：智能问答中典型的"序数副词序列"（首先/其次/再者/最后）三步提问，系统未拆分执行。

用户原句：

> "首先统计4月份采购订单数量，其次统计4月份主要top10采购物料的占比，然后看这top10物料在5月份下的订单数量信息分析"

实际反馈：仅返回第 1 步数据（"4 月份采购订单数量：3,734 份"），并说明"Top 10 物料占比 和 5 月份订单信息 这两部分数据，目前还没有对应的查询结果"。用户看到的是"系统没分步执行"。

验收标准：
- 「首先…其次…然后/再者/最后…」类典型序数副词序列 → 规则拆分，零 LLM 调用，3 个子问题全部执行。
- 单序数副词（如「首先看供应商 A 的收货数量」） → 仍视为单步，不误拆。
- 对比/比较类问题（含"再次"） → 仍视为单步，不误拆。
- 顺序指令型问题（"先 X，再 Y"） → 仍走 LLM 拆步，无回归。
- 单测 + 关联下游测试全量通过、无回归。

## 2. 设计评审

### 根因

`is_explicit_multi_step` 已正确返回 True（"首先"含"先"字符 + "其次"在 `_SEQUENTIAL_CONJUNCTIONS` 中），但下游 `rule_based_split` 仅识别「第X步」标号，序数副词序列无对应正则 → 回退到 `plan()` 调 LLM 拆步；LLM 看到"对比 + 分析趋势"字样倾向"可单条 SQL 完成"返回 `isMultiStep: false`，多步被静默吞掉。

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| (A) 扩 `_EXPLICIT_MULTI_STEP_KEYWORDS` 与 `_SEQUENTIAL_START` | ❌ 不直接解决问题 | `is_explicit_multi_step` 已对"首先…其次…"返回 True，问题在 `rule_based_split` |
| (B) LLM 拆步 prompt 加白名单 | ❌ 不可行 | 仍需 LLM 调用、token 成本，且 prompt 启发式无法穷举 |
| **(C) `rule_based_split` 加序数副词规则** | ✅ 推荐 | 零 LLM 调用、确定性、与「第X步」入口同款接口；正则简单到无需单测复杂度溢出 |

### 关键决策

1. **`_ORDINAL_STEP_PATTERN` 一并纳入"然后/接着/接下来"**：用户原句第 3 步用"然后"分隔，是序数序列的常见收尾（"首先 A，其次 B，然后 C"结构中"然后"与"首先/其次"并列充当分步词）。若仅匹配"首先/其次/再者/最后"，第 3 步内容会被合并进第 2 步。
2. **只收 2 字定长词**：避免与单字"先"/"再"混淆（守约 `test_no_marker_returns_none`："先查 X，再查 Y，对比趋势" 必须返回 None）。所有匹配项（首先/其次/再者/再次/最后/其一/其二/其三/其四/然后/接着/接下来）均为 2 字。
3. **`_splitByRulePattern` 抽出公共函数**：`rule_based_split` 由两个 `_splitByRulePattern` 调用组成——先试「第X步」，再试序数副词，匹配 ≥2 个标记才视为多步（与 `MultiStepPlan.is_single_step` 保持一致）。
4. **不修改 `is_explicit_multi_step`**：原判定已对"首先…其次…"返回 True，问题在 `rule_based_split` 不在判定；扩大判定词表会让"首先看供应商 A 的收货数量"（单步）也被标 True，反而触发不必要的 LLM 拆步。

### 反例守约

- `test_no_marker_returns_none`："先查 X，再查 Y，对比趋势" → 顺序指令但无序数副词 → None（继续走 LLM）。
- `test_must_not_match_comparison_questions`："再次查询各供应商的收货数量" → 1 个"再次"匹配 → None（<2 标记阈值）。
- `test_single_ordinal_returns_none`："首先看供应商 A 的收货数量" → 1 个"首先"匹配 → None（<2 标记阈值）。

## 3. 数据模型变更

无。`MultiStepPlan` / `StepPlan` 结构不变。

## 4. 接口契约变更

无 API 契约变更（拆步结果经 `plan_explicit` 返回 `StepPlanResult`，字段与类型不变）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/services/step_query_planner.py` | 加 `_ORDINAL_STEP_PATTERN` 正则（首先/其次/再者/再次/最后/其一/其二/其三/其四/然后/接着/接下来）；抽 `_splitByRulePattern` 公共函数；`rule_based_split` 改成先试「第X步」再试序数副词；注释里固化 2026-08-17 回归溯源 |
| `backend/app/tests/unit/test_step_query_planner.py` | `TestRuleBasedSplit` +5 用例（用户实际场景、四步序列、古文序数、单序数 False、对比场景 False） |

## 6. 测试

### 新增（5 条用例）

- `test_ordinal_sequence_shu_xian_ran_hou_splits_three_steps`：用户实际场景（2026-08-17 bug），3 数据步 + 1 汇总
- `test_ordinal_sequence_shu_xian_zai_zhe_zui_hou_splits_four_steps`：四步序列
- `test_ordinal_sequence_qi_yi_qi_er_qi_san_splits_three_steps`：古文序数「其一/其二/其三」
- `test_single_ordinal_returns_none`：单序数不视为多步
- `test_comparison_with_ordinal_anchor_still_none`：对比场景含"再次"仍返回 None

### 覆盖率

`step_query_planner.py`：相关函数（`rule_based_split` / `_splitByRulePattern`）100% 覆盖。

### 回归

- `tests/unit/test_step_query_planner.py`：**43/43 通过**（含新增 5 条）
- `tests/unit/test_chat_service.py` + `test_chat_service_stream.py`：**96/96 通过**（下游消费者）
- `tests/unit/test_scope_row_limit.py` + `test_query_plan_generation.py`：**65/65 通过**（相邻多步链路）
- `tests/unit/test_multi_step_plan.py`：**19/19 通过**
- `tests/integration/test_chat_multi_step.py`：8 条因本环境无 PostgreSQL 跳过（项目硬约束：禁止 sqlite 内存库）

## 7. 安全审查

- 正则仅匹配 2 字定长中文，无嵌套量词、无贪婪、无回溯 → **无 ReDoS**。
- `_splitByRulePattern` 返回新 `MultiStepPlan`（frozen dataclass `StepPlan`），无 mutation。
- 子问题字符串仅作为 LLM/SQL prompt 输入，不进入正则/Shell/文件系统 → 无注入面。

## 8. 部署验证

无环境变量改动，无迁移，无 API 变更。

### 手动冒烟

| 问题 | 期望拆步 |
|---|---|
| "首先查 A，其次查 B，然后查 C" | 3 数据步 + 1 汇总 |
| "首先查 A，其次查 B，再者查 C，最后对比" | 4 数据步 + 1 汇总 |
| "其一查 X，其二查 Y，其三查 Z" | 3 数据步 + 1 汇总 |
| "首先看供应商 A 的收货数量" | 单步（None） |
| "先查 X，再查 Y，对比趋势" | 单步（None，走 LLM） |

## 9. 关联

- 历史变更：`Harness/changes/fix-multi-step-rule-based-with-execution-plan/summary.md`（2026-08-16，第X步标号规则拆分）
- 历史变更：`Harness/changes/fix-multi-step-explicit-trigger/summary.md`（2026-08 早期，is_explicit_multi_step 触发逻辑）
- Wiki：`Harness/wiki/nl2sql-engine.md`（如后续需扩复杂多步判定策略，可补"规则拆分触发条件"小节）
