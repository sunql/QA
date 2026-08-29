# 变更：并列复合问题的隐式多步拆解（无显式分步信号）

- **日期**：2026-08-17
- **作者**：QA System
- **Phase**：bugfix（多步 NL2SQL 拆步判定 + answer prompt）
- **状态**：done

## 1. 需求

用户报障：「查询3月份采购订单数量、Top 10物料占比、Top 10物料在4月份的订单数量」这类**用 +/和/，并列复合的问题**（无任何显式分步信号），系统走单步路径只生成覆盖第一件事的 SQL，answer LLM 在数据不足时**自行编造**"Step 2/3 暂无数据 + 询问是否继续"的拟人化回复。

用户原句（系统响应反推）：

> "查询3月份采购订单数量、Top 10物料占比、Top 10物料在4月份的订单数量"

实际系统响应：

```
1. 采购订单数量：3 月份采购订单共 3,317 份，采购总数量为 90,793,450.025
2. Top 10物料占比分析：需要进一步查询各物料的采购数量明细，才能计算出 Top 10物料的占比，目前暂无物料的分布查询结果。
3. Top 10物料在4月份的订单数量分析：需要先确定3月份的 Top 10物料，再查询这些物料在4月份的订单数据，才能进行分析。
需要我继续查询3月份各物料的采购明细，以获取 Top 10物料及其4月份的订单信息吗？
```

验收标准：
- 用 +/和/，并列的复合问题 → 自动识别并拆解为多步顺序执行
- 单条查询（即使含逗号，如"按金额降序"修饰）不被误拆
- answer LLM 在数据不足时**不输出反向追问**（硬约束兜底）
- 性能：单条查询不增加 LLM 调用；复合问题 +1 次（拆步判定）

## 2. 设计评审

### 根因

`chat_service.py:303-314` 的入口设计假设"显式分步信号（分步/首先/其次…）是触发多步的必要条件"。并列复合问题没有任何显式信号，被当作单条可解查询 → 单步 SQL 只覆盖第一件事 → answer LLM 拿到"3 件事问题 + 1 件数据"后**自由发挥**输出"Step 2/3 暂无数据 + 询问是否继续"。

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| (A) 仅扩展关键词白名单 | ❌ 列举无尽 | 松散复合问题永远有遗漏 |
| (B) answer 自检兜底（检测"暂无/需要继续"触发多步重试） | ❌ 依赖 LLM 文本，脆弱 | LLM 文案每次不同，正则无法穷举 |
| **(C) 启发式触发 + LLM 拆步前置 + answer 硬约束（三层）** | ✅ 推荐 | L1 严格守约不误拆；L2 复用现有 plan()；L3 兜底禁止 LLM 拟人化追问 |

### 关键设计点

1. **L1 启发式严格守约"单条查询不误拆"**：
   - 触发条件：列举连接词切出 ≥2 段，每段含至少 1 个查询动词，且不含禁用短语
   - **两种模式任一触发**：
     - 多动词并列：≥2 段都含查询动词
     - 头+列表：≥3 段且首段含查询动词（动词隐式作用于所有尾段，如用户场景）
   - 反例守约：
     - "对比 A 和 B" → 2 段、1 动词 → 不触发（避免误拆"对比"型单步查询）
     - "查询 A，按金额降序" → "按金额降序" 无动词 → 不触发（修饰语）
     - "查询 A 和 B" → 2 段、1 动词 → 不触发（保守）
   - 禁用短语白名单：`不要拆 / 不要分 / 用一条 / 单条查询 / 一条 SQL`

2. **L2 复用现有 `_resolveExplicitMultiStep`**（`chat_service.py:701-727`）：内部已经"先规则 + 后 plan()"——零改动，仅调整调用位置到 L1 命中时。

3. **`step_query_planner.py` 完全不改**：`plan()` 已能独立调用，返回 `StepPlanResult(plan=None)` 表示 LLM 判定单步。复用成本 = +1 次 `purpose="step_plan"` LLM 调用。

4. **L3 硬约束兜底**：`chat_stream_output.py:31-34` `_ANSWER_SYSTEM_PROMPT` 追加禁止反向追问。即使单步路径漏判，LLM 也不会编造"询问继续"。

5. **流式路径同构改动**：`processMessageStream`（`chat_service.py:1331-1343`）与 `processMessage` 入口判断同构，已同步加 L1+L2。

6. **性能/成本**：
   - 单条查询：零额外 LLM（L1 不触发）
   - 复合问题：+1 次 LLM 拆步判定（`_resolveExplicitMultiStep` 复用现有审计 + token 计量）

### 用户误解澄清

之前回复中提到的"用户可能希望从 Step 1 数据推导 Top 10"是误解——Step 1 返回的是合计单行（订单数 + 总数量），数学上**推不出** Top 10。Step 2 必须 GROUP BY 物料生成新 SQL。本次修复不是改 Step 2 的数据来源，而是确保"Step 2 应被识别为独立子查询"。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无 API 契约变更。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/services/chat_service.py` | (1) `import re`；(2) 模块级常量 `_COMPOUND_SEPARATOR_RE` + `_QUERY_VERBS_RE` + `_COMPOUND_DENY_PHRASES`；(3) helper `_looks_like_compound_question(question)` 双模式触发；(4) `processMessage` 入口（line 346-372）L1+L2 分支；(5) `processMessageStream` 入口（line 1332-1361）同构 L1+L2 |
| `backend/app/services/chat_stream_output.py` | `_ANSWER_SYSTEM_PROMPT`（line 31-37）追加 2 句禁止反向追问硬约束 |
| `backend/app/tests/unit/test_compound_question_heuristic.py` | **新建**，20 条 RED 用例覆盖正反例 |
| `backend/app/tests/unit/test_chat_service.py` | +1 条 `TestAnswerSystemPromptHardConstraint` 守约 |

## 6. 测试

### 新增（21 条用例）

- `test_compound_question_heuristic.py`：20 条
  - 5 正例（用户场景、加号、顿号、"和"连接、头+列表）
  - 10 反例（"对比 A 和 B"、排序修饰、"查询 A 和 B"无动词、单条、空/不含连接词）
  - 4 禁用短语
  - 1 空字符串
- `test_chat_service.py::TestAnswerSystemPromptHardConstraint`：1 条

### 覆盖率

`chat_service.py`：`_looks_like_compound_question` 100% 覆盖。
`chat_stream_output.py`：`_ANSWER_SYSTEM_PROMPT` 包含硬约束关键词守约。

### 回归

- `test_compound_question_heuristic.py`：**20/20 通过**
- `test_chat_service.py`：含新增 1 条，**86/86 通过**
- `test_chat_service_stream.py`：**25/25 通过**
- `test_step_query_planner.py`：**43/43 通过**
- `test_query_plan_validation.py`：**38/38 通过**
- `test_query_plan_generation.py` + `test_nl2sql_service.py`：**126/126 通过**
- `test_scope_row_limit.py`：**38/38 通过**
- 全套 unit：**763 通过，0 回归**

## 7. 安全审查

- `_COMPOUND_SEPARATOR_RE` 与 `_QUERY_VERBS_RE` 是固定字符类/alternation 正则，无嵌套量词 → **无 ReDoS**。
- `_looks_like_compound_question` 是纯函数（无副作用），可单元测试、易推理。
- answer prompt 硬约束只限制 LLM 行为，不进入 SQL/Shell/文件系统 → 无注入面。
- L1 启发式 + LLM 拆步判定组合：miss 场景（启发式不触发但实际复合）由 L3 answer 硬约束兜底，不会输出"询问继续"；hit 场景（启发式触发但实际单条）由 `_resolveExplicitMultiStep` plan=None 自然退回单步。

## 8. 部署验证

无环境变量改动，无迁移，无 API 变更。

### 手动冒烟

| 用户原问题 | 期望行为 |
|---|---|
| "查询3月份采购订单数量、Top 10物料占比、Top 10物料在4月份的订单数量" | L1 命中 → L2 拆 3 数据步 → 顺序执行 → 汇总答案 |
| "对比 2024 和 2025 年的销售额" | L1 不触发（单动词"对比"）→ 走单步 |
| "统计各供应商的收货数量，按金额降序" | L1 不触发（第 2 段"按金额降序"无动词）→ 走单步 |
| "查询 A 和 B" | L1 不触发（B 段无动词）→ 走单步 |
| "用一条 SQL 查 A 和 B" | L1 不触发（禁用短语）→ 走单步 |

## 9. 关联

- 历史变更：`Harness/changes/fix-multi-step-ordinal-adverb-split/summary.md`（2026-08-17，多步拆步序数副词）
- 历史变更：`Harness/changes/fix-nl2sql-derived-metric-formula-required/summary.md`（2026-08-17，占比 formula 必填）
- 关联 fix：本次修复后，用户「查询…Top 10…4月份…」三步问题会被正确拆为 3 步执行，不再出现"询问继续"的拟人化回复。
- 现有 integration test `test_single_step_success_does_not_decompose`（`tests/integration/test_chat_multi_step.py:191-214`）守约"对比类问题单步成功不拆步"——本次改动对"对比 A 和 B"不触发 L1，该断言仍成立。