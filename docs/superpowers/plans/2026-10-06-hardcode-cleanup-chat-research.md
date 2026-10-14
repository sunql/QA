# Chat & Research 硬编码治理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 治理 chat 与 research 模块的硬编码（魔数 / 字符串标识符 / system prompt / UI 文案），消除"半提取""重复定义""散落源码"三类技术债，使所有运行时可调的标识符走统一 SSOT 常量。

**Architecture:**
- 不在已有模块顶部塞新常量（chat_service / chat_stream / chat_multistep 均超 800 行上限），新 SSOT 单独建模块
- Research 模块沿用 `research_agent_ports.py` 作为词汇 SSOT，新增常量都进这里
- Chat 模块新建 `backend/app/services/chat_constants.py` 作为字符串枚举 SSOT
- System prompt 与 UI 文案沿用既有 `messages_zh.py` SSOT（已存在）
- 每个 Phase 一个 commit，TDD：先写"引用常量的导入测试" → 改代码 → 跑测试 → commit

**Tech Stack:** Python 3.12、SQLAlchemy 2（async）、pytest + pytest-asyncio、project 既有 ruff/mypy 配置

## 全局约束

- 编码约定（项目 CLAUDE.md）：函数/变量 `camelCase`、类型 `PascalCase`、常量 `UPPER_SNAKE_CASE`、ORM/Pydantic 字段 `snake_case`（与 DB 列对齐）
- 不可变数据：新建对象、禁原地改
- 文件 ≤ 800 行（已有豁免：chat_multistep.py 853 行，参见 `qa-system-chat-multistep-line-cap-exemption`）
- 命名规范：常量必须有名字，禁止裸字面量出现在业务代码里（tests/migrations/seeds 除外）
- 测试规范（项目规则）：单测覆盖率 ≥ 80%；后端测试用真实 PG（5434）`qa_metadata_test`，禁止 sqlite 内存库
- 测试模式 AAA（Arrange-Act-Assert）；TDD：先 RED → GREEN → IMPROVE
- 写完代码立即 `code-reviewer` 审查（项目开发流程规范）
- 每 Phase 结束必须 commit（conventional commits: `refactor:` / `chore:` / `fix:` / `feat:`）
- 不替换既有测试断言风格（用现有测试模式，新增 import-test 验证常量被引用）
- 不动测试与 seed 文件中的硬编码（属于测试 fixture，不是配置）

## 改动汇总（按 Phase）

| Phase | 模块 | 工作量 | 风险 |
|------|-----|--------|------|
| P1 | Research HIGH 数值 + 重复定义 | ~40 行 | 低 |
| P2 | Research MEDIUM 字符串标识符补全 | ~25 行 | 低 |
| P3 | Chat 字符串三族常量（新文件 + 11 文件替换） | ~80 行 + 30+ 字面量替换 | 中 |
| P4 | 3 个 system prompt 移入 messages_zh.py | ~30 行 | 低 |
| P5 | UI 文案 5 段移入 messages_zh.py | ~25 行 | 低 |

每 Phase 完成后必跑：
- `cd backend && pytest -x -q backend/app/tests/unit/services/research_agent_*.py backend/app/tests/integration/test_research_*.py`（P1/P2）
- `cd backend && pytest -x -q backend/app/tests/unit/services/chat_*.py backend/app/tests/unit/services/multi_step_*.py backend/app/tests/unit/services/step_*.py`（P3/P4/P5）

---

## Phase 1 — Research HIGH：魔数命名 + 重复定义去重

### Task 1.1: `COST_SCALE_DIVISOR` 命名化 `_costFor` 的 `/1000`

**Files:**
- Modify: `backend/app/services/research_agent_ports.py`（新增常量，在 92 行附近 §"计量 purpose / 置信度 / 摘要上限"）
- Modify: `backend/app/services/research_agent_ports.py:358-369`（使用新常量）
- Test: `backend/app/tests/unit/services/test_research_agent_ports.py`（如不存在则创建）

**Step 1.1.1: 写失败测试**

在 `test_research_agent_ports.py` 加：

```python
def test_cost_scale_divisor_value():
    """成本公式 /1000 应走命名常量 COAST_SCALE_DIVISOR，避免除数单位变更静默。"""
    from app.services import research_agent_ports
    assert research_agent_ports.COST_SCALE_DIVISOR == 1000
    assert isinstance(research_agent_ports.COST_SCALE_DIVISOR, int)
```

**Step 1.1.2: 跑测试 → 期望 RED**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/test_research_agent_ports.py::test_cost_scale_divisor_value`
Expected: FAIL with `ImportError` 或 `AttributeError: module has no attribute COST_SCALE_DIVISOR`

**Step 1.1.3: 在 `research_agent_ports.py` 加常量**

在 §"计量 purpose / 置信度 / 摘要上限" 区域（约 line 92）加：

```python
COST_SCALE_DIVISOR = 1000
"""成本公式中 per-1k 转 per-token 的除数（与 cost_per_1k_input/output 列名耦合）。
若定价模型由 per-1k 切到 per-million/per-token，此为唯一静默断点，必须同步改列名 + 此常量。"""
```

**Step 1.1.4: 替换 `/Decimal(1000)` 为 `/Decimal(COST_SCALE_DIVISOR)`**

`research_agent_ports.py:358` 与 `:363` 两处除法替换。检查文件中是否还有其他裸 `1000`。

**Step 1.1.5: 跑测试 → GREEN + 全套 research 测试**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/research_agent_*.py`
Expected: 全绿。

**Step 1.1.6: commit**

```bash
git add backend/app/services/research_agent_ports.py backend/app/tests/unit/services/test_research_agent_ports.py
git commit -m "refactor(research): 命名化 cost 算分母 COAST_SCALE_DIVISOR=1000

成本公式 /Decimal(1000) 与 cost_per_1k_* 列名隐式耦合，定价单位变更是静默断点。
提取为命名常量后续改动有 grep 锚点。

Ref: hardcode-audit 2026-10-06 HIGH#1"
```

---

### Task 1.2: `CONFIDENCE_ROUND_DIGITS` 命名化落库精度

**Files:**
- Modify: `backend/app/services/research_agent_ports.py`（新增常量）
- Modify: `backend/app/services/research_agent_phases.py:272`（使用新常量）
- Test: 同上文件

**Step 1.2.1: 写失败测试**

```python
def test_confidence_round_digits_value():
    from app.services import research_agent_ports
    assert research_agent_ports.CONFIDENCE_ROUND_DIGITS == 6
```

**Step 1.2.2: 跑测试 → RED**

Expected: AttributeError。

**Step 1.2.3: 加常量**

紧邻 `COST_SCALE_DIVISOR` 加：

```python
CONFIDENCE_ROUND_DIGITS = 6
"""落库 hypothesis.confidence 的精度（与 SQL Numeric 精度对齐）。
变更此处会重塑所有已存 finding 的可信度对比口径，属配置决策而非魔数。"""
```

**Step 1.2.4: 替换 `research_agent_phases.py:272`**

定位行：`confidence = round(candidateConfidence(state, candidate) * (VERIFY_OK_FACTOR if verified else VERIFY_FAIL_FACTOR), 6)`
改为：`..., CONFIDENCE_ROUND_DIGITS)`

并 import：`from app.services.research_agent_ports import ..., CONFIDENCE_ROUND_DIGITS`

**Step 1.2.5: 跑测试 → 全绿**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/research_agent_phases.py`

**Step 1.2.6: commit**

```bash
git add backend/app/services/research_agent_ports.py backend/app/services/research_agent_phases.py backend/app/tests/unit/services/test_research_agent_ports.py
git commit -m "refactor(research): 命名化落库精度 CONFIDENCE_ROUND_DIGITS=6"
```

---

### Task 1.3: `LOG_SQL_TRUNCATE_LEN` 命名化 SQL 日志截断

**Files:**
- Modify: `backend/app/services/research_sql_runner.py`（新增常量 + 使用）
- Test: 新增

**Step 1.3.1: 写测试**

```python
def test_log_sql_truncate_len():
    from app.services import research_sql_runner
    assert research_sql_runner.LOG_SQL_TRUNCATE_LEN == 120
```

**Step 1.3.2: 跑测试 → RED**

**Step 1.3.3: 加常量**

`research_sql_runner.py` 顶部 VERIFICATION_ROW_CAP 附近加：

```python
LOG_SQL_TRUNCATE_LEN = 120
"""验证查询超行截断日志里 SQL 字符串的字符上限。仅影响运维日志可读性。"""
```

**Step 1.3.4: 替换 `research_sql_runner.py:94`**

```python
logger.warning("验证查询结果超过行上限 %d，已截断: %s", VERIFICATION_ROW_CAP, sql[:LOG_SQL_TRUNCATE_LEN])
```

**Step 1.3.5: 跑测试 → GREEN**

**Step 1.3.6: commit**

```bash
git add backend/app/services/research_sql_runner.py backend/app/tests/unit/services/test_research_sql_runner.py
git commit -m "refactor(research): 命名化 SQL 日志截断 LOG_SQL_TRUNCATE_LEN=120"
```

---

### Task 1.4: `MSG_ALL_STEPS_FAILED` 命名化 sentinel RuntimeError

**Files:**
- Modify: `backend/app/services/research_agent_ports.py`（新增）
- Modify: `backend/app/services/research_agent_execution.py:358-363`（使用）
- Test: 现有 execution 测试可能断言 `RuntimeError`，需同步更新

**Step 1.4.1: 检查现有断言**

```bash
grep -n "allStepsFailed\|全部步失败\|MSG_ALL_STEPS_FAILED" backend/app/tests/ -r 2>/dev/null
```

**Step 1.4.2: 写测试**

```python
def test_msg_all_steps_failed_exists():
    from app.services import research_agent_ports
    assert isinstance(research_agent_ports.MSG_ALL_STEPS_FAILED, str)
    assert "全部步失败" in research_agent_ports.MSG_ALL_STEPS_FAILED
```

**Step 1.4.3: 跑测试 → RED**

**Step 1.4.4: 加常量**

```python
MSG_ALL_STEPS_FAILED = "研究计划全部步失败（无一成功），本 turn 终止"
"""_guardedRun 错误映射的**唯一识别串**：该字符串决定 `research.error.{turn_failed}` 事件码。
任何拼写变化都会让 all-steps-failed 错误被吞成普通 RuntimeError，必须走常量。"""
```

**Step 1.4.5: 替换 `research_agent_execution.py` 的 `RuntimeError` 字符串**

```python
raise RuntimeError(MSG_ALL_STEPS_FAILED)
```

并 import 增量。检查 `_guardedRun` 处是否按字面量判定：

```bash
grep -n "全部步失败\|RuntimeError" backend/app/services/research_agent_execution.py | head
```

若按字面量比较，需改为常量等值。

**Step 1.4.6: 更新现有测试断言**

若现有测试断言的是字面量，改为：

```python
from app.services.research_agent_ports import MSG_ALL_STEPS_FAILED
assert str(exc.value) == MSG_ALL_STEPS_FAILED
```

**Step 1.7: 跑测试 → GREEN**

**Step 1.8: commit**

```bash
git add backend/app/services/research_agent_ports.py backend/app/services/research_agent_execution.py backend/app/tests/
git commit -m "refactor(research): 命名化 all-steps-failed sentinel RuntimeError"
```

---

### Task 1.5: 合并重复的 `DEFAULT_MODE`

**Files:**
- Modify: `backend/app/services/research_session_service.py:41`（删除重复定义，改为 import）
- Test: 新增断言「两处常量值一致」

**Step 1.5.1: 写测试**

```python
def test_default_mode_ssot_single_source():
    """DEFAULT_MODE 只允许 research_agent_ports.py 一处定义。"""
    from app.services import research_agent_ports
    from app.services.research_session_service import DEFAULT_MODE
    assert DEFAULT_MODE is research_agent_ports.DEFAULT_MODE
```

**Step 1.5.2: 跑测试 → RED**（两处各自定义，identity 不同）

**Step 1.5.3: 删除 `research_session_service.py:41` 的定义**

把：
```python
DEFAULT_MODE = "research"
```
改为：
```python
from app.services.research_agent_ports import DEFAULT_MODE  # re-export for backward compat
```

确认 `research_session_service.py` 所有用法仍可访问 `DEFAULT_MODE`（re-export 模式）。

**Step 1.5.4: 跑测试 → GREEN**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/research_session_service.py`

**Step 1.5.5: commit**

```bash
git add backend/app/services/research_session_service.py backend/app/tests/unit/services/test_research_agent_ports.py
git commit -m "refactor(research): 去重 DEFAULT_MODE，SSOT 收口到 ports.py"
```

---

## Phase 2 — Research MEDIUM：补齐枚举常量化

### Task 2.1: 4 个 phase 常量补全

**Files:**
- Modify: `backend/app/services/research_agent_ports.py`（新增）
- Modify: `backend/app/services/research_agent_phases.py:136,143,182,223,224,250`（替换裸字符串）
- Modify: `backend/app/services/research_agent_service.py:175-180, 276`（`_buildStages` 字典键、replan startPhase）

**Step 2.1.1: 写测试**

```python
def test_phase_constants_match_phases_tuple():
    """PHASE_* 字符串必须与 PHASES 元组对应位置的成员完全一致。"""
    from app.services.research_agent_ports import PHASES, PHASE_PLAN, PHASE_EXECUTE, PHASE_VERIFY, PHASE_REPORT
    assert PHASE_PLAN == PHASES[2]
    assert PHASE_EXECUTE == PHASES[3]
    assert PHASE_VERIFY == PHASES[5]
    assert PHASE_REPORT == PHASES[6]
```

**Step 2.1.2: 跑测试 → RED**

**Step 2.1.3: 加常量**

`research_agent_ports.py:39` 附近：

```python
PHASE_INTENT = "intent"  # PHASES[0]
PHASE_PLAN = "plan"      # PHASES[2]
PHASE_EXECUTE = "execute"  # PHASES[3]
PHASE_HYPOTHESIS = "hypothesis"  # PHASES[4]
PHASE_VERIFY = "verify"  # PHASES[5]
PHASE_REPORT = "report"  # PHASES[6]
# PHASE_ESL 已在 ports:41；保持原顺序不动
```

**Step 2.1.4: 替换裸字符串**

在 `research_agent_phases.py` 中搜：`resumePhase="plan"`、`resumePhase="execute"`、`resumePhase="verify"`、`abortPhase="report"` 全部替换为常量。

在 `research_agent_service.py:175-180` 把字典键改为常量。

**Step 2.1.5: 跑测试 → 全绿**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/research_agent_phases.py backend/app/tests/unit/services/research_agent_service.py`

**Step 2.1.6: commit**

```bash
git add backend/app/services/research_agent_ports.py backend/app/services/research_agent_phases.py backend/app/services/research_agent_service.py backend/app/tests/
git commit -m "refactor(research): 补齐 PHASE_* 常量，与 PHASES tuple 锁定"
```

---

### Task 2.2: `ROLE_USER` / `ACTION_MODIFY` 与已有 ROLE_CHECKPOINT / ACTION_REJECT 对齐

**Files:**
- Modify: `backend/app/services/research_agent_ports.py`（新增）
- Modify: `backend/app/services/research_agent_service.py:200,273,306`（替换）

**Step 2.2.1: 测试**

```python
def test_role_action_constants_match_status_dict():
    """ROLE_USER 与 ACTION_MODIFY 字符串必须与 STATUS dict 值/ACTION_STATUS key 一致。"""
    from app.services.research_agent_ports import ROLE_USER, ACTION_MODIFY, ACTION_STATUS
    assert ROLE_USER == "user"
    assert ACTION_MODIFY == "modify"
    assert ACTION_MODIFY in ACTION_STATUS
```

**Step 2.2.2: 跑测试 → RED**

**Step 2.2.3: 加常量**

```python
ROLE_USER = "user"
ACTION_MODIFY = "modify"
ACTION_CONFIRM = "confirm"
```

**Step 2.2.4: 替换**

- `research_agent_service.py:200,306` `role="user"` → `role=ROLE_USER`
- `research_agent_service.py:273` `action == "modify"` → `action == ACTION_MODIFY`
- 同时检查 service.py 中是否有 `action == "confirm"` 也一并替换

**Step 2.2.5: 跑测试 → 全绿**

**Step 2.2.6: commit**

```bash
git add backend/app/services/research_agent_ports.py backend/app/services/research_agent_service.py backend/app/tests/
git commit -m "refactor(research): 补齐 ROLE_USER / ACTION_MODIFY，与已有 CHECKPOINT/REJECT 对齐"
```

---

### Task 2.3: `OPT_STEP_INDEX` 在 error payload 复用

**Files:**
- Modify: `backend/app/services/research_agent_execution.py:380`（替换）
- Modify: `backend/app/services/research_agent_ports.py`（确认常量已存在）

**Step 2.3.1: 测试**

```python
def test_opt_step_index_reused_in_error_payload():
    """OPT_STEP_INDEX 已是 stepIndex 字符串 SSOT，errorPayload 也必须复用。"""
    from app.services.research_agent_execution import _build_error_payload  # 实际函数名按代码确认
    import inspect
    from app.services.research_agent_ports import OPT_STEP_INDEX
    src = inspect.getsource(_build_error_payload)
    assert OPT_STEP_INDEX in src
```

**Step 2.3.2: 跑测试 → RED**

**Step 2.3.3: 替换 `stepIndex=...` 字面量**

定位 `errorPayload(stepErrorCode(error), error, stepIndex=result["index"])`，改为：

```python
errorPayload(stepErrorCode(error), error, **{OPT_STEP_INDEX: result["index"]})
```

并 import `OPT_STEP_INDEX`。

**Step 2.3.4: 跑测试 → 全绿**

**Step 2.3.5: commit**

```bash
git add backend/app/services/research_agent_execution.py backend/app/tests/
git commit -m "refactor(research): errorPayload stepIndex 复用 OPT_STEP_INDEX SSOT"
```

---

## Phase 3 — Chat 字符串三族常量（新 SSOT 文件 + 11 文件替换）

### Task 3.1: 新建 `chat_constants.py` SSOT

**Files:**
- Create: `backend/app/services/chat_constants.py`

**Step 3.1.1: 创建文件**

```python
"""Chat 模块的字面常量 SSOT（避免散落字面量与跨文件重复）。

本模块不承载运行时行为；仅为下列字符串标识符提供唯一真相：

- ``ROUTING_LAYER_*``：路由层标签（落 routing_metrics SQL 维度，前端展示）
- ``USAGE_PURPOSE_*``：token usage 用途标识（落 token_usage.purpose，SQL 聚合维度）
- ``AUDIT_*``：审计实体/动作/状态（落 audit_log.entity_type/action）

三族均与 DB 列及 SQL 聚合锁定，**禁止**改值不改下游：DB 列已存数据，重命名会让历史
行 group by 出空集。

新增规则：往这三族里加项前必须确认 DB 列长度（routing_layer VARCHAR / purpose VARCHAR
/ entity_type VARCHAR）。现有长度基于 audit_history_api 迁移定义；超长会被 DB 截断。
"""

from __future__ import annotations

# =============================================================================
# 路由层（落 routing_metrics.routing_layer；routing_metrics_service 按此 group by）
# =============================================================================

ROUTING_LAYER_L1 = "L1"  # 单轮 SQL 直答（简单查询，无追问、无拆步）
ROUTING_LAYER_L2 = "L2"  # 多轮对话 / 上下文召回 / 拆步计划（绝大多数生产流量）
ROUTING_LAYER_L3 = "L3"  # 多步持久化执行（chat_multistep 路径，独立 run 表）
ROUTING_LAYER_L4 = "L4"  # L4 Agent Loop（研究类问题，触发 _L4_EXPLORATORY_KEYWORDS）


# =============================================================================
# Token usage 用途（落 token_usage.purpose；按此维度做成本归因与告警）
# =============================================================================

USAGE_PURPOSE_CLARIFY = "clarify"
USAGE_PURPOSE_NL2SQL = "nl2sql"
USAGE_PURPOSE_ANSWER = "answer"
USAGE_PURPOSE_CHART = "chart"
USAGE_PURPOSE_STEP_PLAN = "step_plan"
USAGE_PURPOSE_FOLLOW_UP_REWRITE = "follow_up_rewrite"
USAGE_PURPOSE_MULTISTEP_GLOBAL_FILTER = "multistep_global_filter"
USAGE_PURPOSE_SUPPLIER_RISK = "supplier_risk"
USAGE_PURPOSE_AGENT_RUN = "agent_run"
USAGE_PURPOSE_L4_AGENT_LOOP = "l4_agent_loop"
# 降级路径下的"父 purpose"前缀（chat_service._callWithFallback 用 f"fallback_{purpose}"）
USAGE_PURPOSE_FALLBACK_PREFIX = "fallback_"
USAGE_PURPOSE_ANSWER_STREAM_FAILED = "answer_stream_failed"
USAGE_PURPOSE_FALLBACK_ANSWER = "fallback_answer"


# =============================================================================
# 审计字段（落 audit_log）
# =============================================================================

# 实体类型
AUDIT_ENTITY_AGENT_RUN_LOG = "agent_run_log"

# 动作
AUDIT_ACTION_CREATE = "CREATE"

# 运行状态
AUDIT_RUN_STATUS_SUCCESS = "SUCCESS"
AUDIT_RUN_STATUS_FAILED = "FAILED"

# 错误分类（chat_domain._emitAgentRunAuditAfter 5 处）
AGENT_RUN_ERROR_AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
AGENT_RUN_ERROR_PERMISSION_DENIED = "PERMISSION_DENIED"
AGENT_RUN_ERROR_AGENT_NOT_RUNNABLE = "AGENT_NOT_RUNNABLE"
AGENT_RUN_ERROR_BAD_INPUT = "BAD_INPUT"
AGENT_RUN_ERROR_UNEXPECTED = "UNEXPECTED"
```

**Step 3.1.2: commit**

```bash
git add backend/app/services/chat_constants.py
git commit -m "refactor(chat): 新建 chat_constants.py SSOT（路由层/usage purpose/审计字段三族）"
```

---

### Task 3.2: 替换 `routing_layer` 11 处字面量

**Files:**
- Modify: 11 处文件（见 grep 结果）
- Test: 新增 import 测试

**Step 3.2.1: 测试**

```python
def test_routing_layer_constants():
    """routing_layer 字面量必须走 chat_constants.SSOT。"""
    import pathlib
    targets = ["chat_service.py", "chat_stream.py", "chat_multistep.py", "chat_l4.py"]
    base = pathlib.Path(__file__).resolve().parents[3] / "services"
    for t in targets:
        src = (base / t).read_text()
        # 排除 docstring 与 type-hint 后，允许的位置只剩:
        # (a) import 块 (c) 注释
        # 不允许的: 出现在 assignment/recordUsage/kwargs 里
        for lit in ('"L1"', '"L2"', '"L4"'):
            # 仅检查"出现位置必须在 ROUTING_LAYER_* 常量定义中或 docstring"
            if lit not in src:
                continue
            # 简化检查：任何 .routing_layer=<literal> 应已替换
            assert f"routing_layer={lit}" not in src, f"{t} 仍存在 routing_layer={lit} 字面量"
```

**Step 3.2.2: 跑测试 → RED**

**Step 3.2.3: 批量替换**

| 文件 | 行 | 字面量 | 改为 |
|------|----|--------|------|
| chat_service.py | 425 | `"L1"` | `ROUTING_LAYER_L1` |
| chat_service.py | 683 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_service.py | 1720 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_multistep.py | 564 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_multistep.py | 700 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_multistep.py | 906 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_stream.py | 516 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_stream.py | 750 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_stream.py | 1005 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_stream.py | 1273 | `"L2"` | `ROUTING_LAYER_L2` |
| chat_l4.py | 178 | `"L4"` | `ROUTING_LAYER_L4` |

每个文件顶部加：
```python
from app.services.chat_constants import ROUTING_LAYER_L2  # 等
```

**Step 3.2.4: 跑测试 → GREEN**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/chat_*.py backend/app/tests/unit/multi_step_*.py backend/app/tests/unit/step_*.py`

**Step 3.2.5: commit**

```bash
git add backend/app/services/chat_*.py backend/app/tests/
git commit -m "refactor(chat): routing_layer 11 处字面量改用 chat_constants.ROUTING_LAYER_*"
```

---

### Task 3.3: 替换 `purpose=` 15+ 处

**Files:** 6 个文件按 grep 结果

**Step 3.3.1: 测试**

```python
def test_purpose_string_constants():
    """purpose 字面量在业务调用方不应存在，应走 USAGE_PURPOSE_* 常量。"""
    # 检查 chat_usage.py / chat_multistep.py / chat_stream.py / chat_service.py / chat_l4.py / chat_domain.py
    # 中除 import/docstring/常量定义外是否还有裸字面量
```

**Step 3.3.2: 跑测试 → RED**

**Step 3.3.3: 批量替换**

按审计报告 grep 结果逐个替换，每个文件顶部加对应 import。

**Step 3.3.4: 跑测试 → GREEN**

**Step 3.3.5: commit**

```bash
git add backend/app/services/chat_*.py backend/app/tests/
git commit -m "refactor(chat): usage purpose 15 处字面量改用 USAGE_PURPOSE_* 常量"
```

---

### Task 3.4: 替换 audit 字段 5×6 处

**Files:** `backend/app/services/chat_domain.py`

**Step 3.4.1: 测试**

```python
def test_audit_field_constants():
    from app.services.chat_constants import (
        AUDIT_ENTITY_AGENT_RUN_LOG, AUDIT_ACTION_CREATE,
        AUDIT_RUN_STATUS_SUCCESS, AUDIT_RUN_STATUS_FAILED,
    )
    assert AUDIT_ENTITY_AGENT_RUN_LOG == "agent_run_log"
    assert AUDIT_ACTION_CREATE == "CREATE"
    assert AUDIT_RUN_STATUS_SUCCESS == "SUCCESS"
    assert AUDIT_RUN_STATUS_FAILED == "FAILED"
```

**Step 3.4.2: 跑测试 → RED**

**Step 3.4.3: 替换 `chat_domain.py:283-441`**

- `run_status = "SUCCESS"` → `AUDIT_RUN_STATUS_SUCCESS`
- `run_status = "FAILED"` → `AUDIT_RUN_STATUS_FAILED`
- `entity_type="agent_run_log"` → `entity_type=AUDIT_ENTITY_AGENT_RUN_LOG`
- `action="CREATE"` → `action=AUDIT_ACTION_CREATE`
- 5 个 error 字段（AGENT_NOT_FOUND 等）→ 对应 AGENT_RUN_ERROR_* 常量

顶部加：
```python
from app.services.chat_constants import (
    AUDIT_ENTITY_AGENT_RUN_LOG, AUDIT_ACTION_CREATE,
    AUDIT_RUN_STATUS_SUCCESS, AUDIT_RUN_STATUS_FAILED,
    AGENT_RUN_ERROR_AGENT_NOT_FOUND, AGENT_RUN_ERROR_PERMISSION_DENIED,
    AGENT_RUN_ERROR_AGENT_NOT_RUNNABLE, AGENT_RUN_ERROR_BAD_INPUT,
    AGENT_RUN_ERROR_UNEXPECTED,
)
```

**Step 3.4.4: 跑测试 → 全绿**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/chat_domain.py`

**Step 3.4.5: commit**

```bash
git add backend/app/services/chat_domain.py backend/app/tests/
git commit -m "refactor(chat): audit 5×6 处字面量改用 chat_constants.AUDIT_* / AGENT_RUN_ERROR_*"
```

---

## Phase 4 — System Prompts 移入新建 `prompts_zh.py` 模块

> 决策记录（pre-flight）：用户裁定 Phase 4 走"新建模块"路线而非"复用 messages_zh.py"。
> `messages_zh.py:12-13` docstring 明文规定 LLM prompt 不在该模块（"LLM prompt 模板属于模型指令，归模块常量"），故新建 `prompts_zh.py` 集中 3 个 prompt，职责互不重叠。

### Task 4.1: 新建 `prompts_zh.py` 并替换 3 处使用

**Files:**
- Create: `backend/app/services/prompts_zh.py`
- Modify: `backend/app/services/chat_stream_output.py:32-38`
- Modify: `backend/app/services/chat_domain.py:67-70`
- Modify: `backend/app/services/step_query_planner.py:69-78`
- Test: `backend/app/tests/unit/services/test_prompts_zh.py`

**Step 4.1.1: 写失败测试**

```python
def test_prompts_zh_constants_present():
    """3 个 system prompt 必须进新建 prompts_zh.py SSOT。"""
    from app.services.prompts_zh import (
        PROMPT_ANSWER_SYSTEM,
        PROMPT_CLARIFY_SYSTEM,
        PROMPT_STEP_PLANNER_SYSTEM,
    )
    assert "禁止反向追问" in PROMPT_ANSWER_SYSTEM
    assert "企业数据分析助手" in PROMPT_CLARIFY_SYSTEM
    assert "查询拆分器" in PROMPT_STEP_PLANNER_SYSTEM
```

**Step 4.1.2: 跑测试 → RED**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/test_prompts_zh.py::test_prompts_zh_constants_present`
Expected: FAIL with `ImportError: cannot import name 'PROMPT_ANSWER_SYSTEM'`

**Step 4.1.3: 创建 `backend/app/services/prompts_zh.py`**

```python
"""Chat 模块 LLM System Prompt SSOT。

与 ``messages_zh.py`` 的职责区分：

- ``messages_zh.py``：面向终端用户的 UI 文案（按 CLAUDE.md 政策"UI 文案集中"）。
- 本模块：发给 LLM 的 system message 模板。属"模型指令"，不该与 UI 文案混在一起，
  否则做 i18n 翻译时容易把 prompt 一起翻坏。

变更提示：调整 prompt 文案前确认下游的 JSON 输出契约（尤其 ``PROMPT_STEP_PLANNER_SYSTEM``）
仍然匹配 parser 期望的 shape——改 prompt shape 等于改契约，配套 plan 加 step。
"""

from __future__ import annotations

PROMPT_ANSWER_SYSTEM = (
    "你是一名企业数据分析助手。根据查询结果用简洁的中文回答用户问题，"
    "不要编造数据，不要输出 SQL。"
    "**禁止反向追问**：不要询问用户'需要继续查询吗 / 是否需要进一步分析 / "
    "还需要看其他吗'。"
    "如查询结果不足以回答问题，直接说明当前结果能回答什么、不能回答什么即可。"
)
PROMPT_CLARIFY_SYSTEM = (
    "你是一名企业数据分析助手。用户正在询问某个业务概念/术语的含义，"
    "请结合提供的本体元数据用简洁的中文解释，不要编造、不要输出 SQL。"
)
PROMPT_STEP_PLANNER_SYSTEM = (
    "你是查询拆分器。判定用户问题是否需要拆成多个子查询。\n"
    "若需要，返回 JSON: {\"isMultiStep\": true, "
    "\"steps\": [{\"description\": \"...\", \"subQuestion\": \"...\"}], "
    "\"aggregationHint\": \"如何汇总\"}\n"
    "若不需要，返回 {\"isMultiStep\": false}。\n"
    "只拆**彼此独立、无法用一条 SQL 完成**的子问题；一次 SQL 能算完的对比/汇总不要拆。\n"
    "**如实列出全部子问题，不要因为数量多就自行合并或截断**——"
    "系统会按上限决定是否受理，你少报会让用户拿到不完整的答案。"
)
```

**Step 4.1.4: 替换 3 处**

- `chat_stream_output.py:32-38`：删 `_ANSWER_SYSTEM_PROMPT`，改 import
  ```python
  from app.services.prompts_zh import PROMPT_ANSWER_SYSTEM as _ANSWER_SYSTEM_PROMPT
  ```
  （保留模块内别名以减少 diff 体积；或直接改名为 PROMPT_ANSWER_SYSTEM）

- `chat_domain.py:67-70`：同上模式，import `PROMPT_CLARIFY_SYSTEM`

- `step_query_planner.py:69-78`：同上模式，import `PROMPT_STEP_PLANNER_SYSTEM`

**Step 4.1.5: 跑测试 → 全绿**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/chat_stream_output.py backend/app/tests/unit/services/chat_domain.py backend/app/tests/unit/services/step_query_planner.py backend/app/tests/unit/services/test_prompts_zh.py`

**Step 4.1.6: commit**

```bash
git add backend/app/services/prompts_zh.py backend/app/services/chat_stream_output.py backend/app/services/chat_domain.py backend/app/services/step_query_planner.py backend/app/tests/
git commit -m "refactor(chat): 3 个 system prompt 移入新 SSOT prompts_zh.py

与 messages_zh.py 职责分离：messages_zh 收 UI 文案，prompts_zh 收模型指令。
原 messages_zh.py docstring 明文规定 prompt 不在该模块，故另建模块。
"
```

---

## Phase 5 — UI 文案 5 段移入 messages_zh.py

### Task 5.1: 加 5 段 UI 文案到 messages_zh.py

**Files:**
- Modify: `backend/app/services/messages_zh.py`（新增 §"Chat"）
- Modify: `chat_service.py` / `chat_helpers.py`

**Step 5.1.1: 写测试**

```python
def test_chat_ui_copy_in_messages_zh():
    from app.services.messages_zh import (
        MSG_CHAT_UNANSWERABLE_NO_DATA,
        MSG_CHAT_UNANSWERABLE_MISSING_VECTOR,
        MSG_CHAT_STEP_GEN_FAILED_PREFIX,
        MSG_CHAT_STEP_EXEC_FAILED_PREFIX,
        MSG_CHAT_STEP_UNANSWERABLE,
        MSG_CHAT_STEP_AGGREGATION_SKIPPED,
    )
    assert "抱歉，当前系统中没有与您的问题相关的业务数据" in MSG_CHAT_UNANSWERABLE_NO_DATA
    assert "向量同步" in MSG_CHAT_UNANSWERABLE_MISSING_VECTOR
    assert MSG_CHAT_STEP_GEN_FAILED_PREFIX == "该步骤查询生成失败："
```

**Step 5.1.2: 跑测试 → RED**

**Step 5.1.3: 加常量**

```python
# =============================================================================
# Chat 模块用户可见文案
# =============================================================================

MSG_CHAT_UNANSWERABLE_NO_DATA = (
    "抱歉，当前系统中没有与您的问题相关的业务数据，无法回答该问题。"
)
MSG_CHAT_UNANSWERABLE_MISSING_VECTOR = (
    "抱歉，向量检索未返回相关本体类，可能尚未同步向量数据。"
    "请在「本体管理→向量同步」中同步向量数据后再试。"
)
MSG_CHAT_STEP_GEN_FAILED_PREFIX = "该步骤查询生成失败："
MSG_CHAT_STEP_EXEC_FAILED_PREFIX = "该步骤执行失败："
MSG_CHAT_STEP_UNANSWERABLE = "无法回答（LLM 判定无有效查询计划）"
MSG_CHAT_STEP_AGGREGATION_SKIPPED = "未执行（前置数据步骤全部失败）"
```

**Step 5.1.4: 替换**

- `chat_service.py:273` `_UNANSWERABLE_ANSWER` → import 改用 `MSG_CHAT_UNANSWERABLE_NO_DATA`
- `chat_service.py:275` `_UNANSWERABLE_ANSWER_MISSING_VECTOR` → import 改用 `MSG_CHAT_UNANSWERABLE_MISSING_VECTOR`
- `chat_helpers.py:237-247` 4 个 `_STEP_*_PREFIX` / `_MSG_STEP_*` 常量改为 import

**Step 5.1.5: 跑测试 → 全绿**

Run: `cd backend && pytest -x -q backend/app/tests/unit/services/chat_service.py backend/app/tests/unit/services/chat_helpers.py`

**Step 5.1.6: commit**

```bash
git add backend/app/services/messages_zh.py backend/app/services/chat_service.py backend/app/services/chat_helpers.py backend/app/tests/
git commit -m "refactor(chat): UI 文案 5 段移入 messages_zh.py SSOT"
```

---

## 完成检查（每阶段后 + 全部阶段后）

### 每阶段后

```bash
cd backend && pytest -x -q backend/app/tests/unit/ backend/app/tests/integration/test_research_*.py 2>&1 | tail -20
```

期望：全绿。

### 全部阶段后

```bash
# 1. 硬编码 grep 验证：以下 pattern 应只在 chat_constants.py / research_agent_ports.py / messages_zh.py 出现
cd backend && grep -rn '"L1"\|"L2"\|"L4"' app/services/chat_*.py app/services/multi_step_*.py app/services/step_*.py | grep -v "^.*:#" | grep -v "constants.py"
# 期望：无输出

cd backend && grep -rn 'purpose="[a-z_]*"' app/services/chat_*.py | grep -v "constants.py" | grep -v "purpose=\"fallback_\""
# 期望：无输出

# 2. coverage 验证
cd backend && pytest --cov=app.services.research_agent_ports app.services.chat_constants app.services.messages_zh --cov-report=term-missing 2>&1 | tail -20
# 期望：≥ 80%
```

### 自检清单

- [ ] Phase 1-5 全部 commit 落地
- [ ] P1-P2 后 research 模块测试全绿
- [ ] P3-P5 后 chat 模块测试全绿
- [ ] grep 验证：业务代码无裸 L1/L2/L4 字面量
- [ ] grep 验证：业务代码无裸 purpose="x" 字面量
- [ ] code-reviewer 审查每 Phase
- [ ] 没有原地 mutation（全部新建/替换）
- [ ] 没有修改 tests/ 既有测试断言（除新增常量测试）

### 风险与回滚

- **风险 1：常量值与 DB 既有数据不匹配**
  - 缓解：常量值从既有字面量原样复制；不改值
  - 回滚：单 commit revert

- **风险 2：替换导致运行时 import cycle**
  - 缓解：所有新常量文件无业务依赖；只常量定义
  - 回滚：import 改为原字面量

- **风险 3：chat_service.py 替换后行数膨胀**
  - 缓解：常量集中在 chat_constants.py，service.py 只加 import 行
  - 回滚：去掉 import + 还原字面量

---

## 不在本期范围（后续 backlog）

- chat_recall.py 的 8 个 `_DEFAULT` 与 system_config seed 一致性 pinning（需新增测试）
- chat_context.py 的 6 个 context budget 常量同上
- multi_step_retry.py 的 `MAX_ATTEMPTS`/`_TRANSIENT_STATUS` 进 `app/config.py`（pydantic Settings）
- `_L4_EXPLORATORY_KEYWORDS` 词表进 vocabulary SSOT（agent-vocabulary SSOT 已存在）
- `STREAM_CHUNK_TIMEOUT_SECONDS = 45.0` 进 `app/config.py`
- `chat_chart_persist._PERSIST_MAX_TABLE_ROWS = 200` 进 `app/config.py`
- `multi_step_compressor.py` 4 个常量（0.7/30/50/5）合并去重
- `messages_zh.py` 增加 i18n 框架（zh-CN 与 en-US）
- `step_subquestion_rewriter.py:22` 的 `"B019"` 真实供应商占位 → 改 sentinel 常量

---

**计划完成。**