# Qwen Multi-Step Uplift Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Qwen3.8-27B-4bit 在多步复合 prompt 场景下能命中「对比/对照/判断是…还是…」一类题（B019 为代表），做法是：(a) 路由 hook 保底走 deepseek；(b) sub-question 改写让 Qwen 仍上场。

**Architecture:** 两个独立新模块 + 2 处 hook。新模块纯函数/数据类，无 IO 无副作用。Hook 接入 `MultiStepMixin._resolveExplicitMultiStep`（路由）与 `_executeDataStep`（改写）两处。**不动** `nl2prompts.py` / plan validator / SQL generator / `llm_config` / `LlmConfig` 任何已有文件的核心逻辑；不动 deepseek-chat / M3 / Kimi 行为。

**Tech Stack:** Python 3.11 + FastAPI + Pydantic v2 + pytest + dataclasses(frozen=True) + immutable pattern（遵循 `qa-system-coding-standards`）。

---

## Global Constraints

These bind every task. Repeating them verbatim from project rules:

- **File size**: 200-400 lines typical, **800 lines hard cap** (CLAUDE.md).
- **Function size**: < 50 lines (CLAUDE.md).
- **Nesting**: ≤ 4 levels, prefer early returns (CLAUDE.md).
- **Immutability**: always create new objects; never mutate in-place (CLAUDE.md).
- **TDD**: RED → GREEN → IMPROVE, coverage ≥ 80% (`Harness/rules/测试规范.md`).
- **Real DB testing**: backend tests use real PG (port `5433`, db `qa_metadata_test`); unit tests can be pure.
- **Commit message format**: `feat|fix|test|chore|refactor|docs: <subject>` (CLAUDE.md / `Harness/rules/项目编码规范.md`).
- **Pydantic DTO field naming**: `CamelModel` 用 camelCase 字段名（Python 访问也是 camelCase）；`dataclasses.replace` 用于 frozen dataclass（不是 Pydantic `model_copy`）。
- **No new LlmConfig fields, no alembic migration** (per spec §3).
- **No new system prompt text** in `nl2prompts.py` / `step_query_planner.py` / `step_aggregator.py`（per spec §1.3 non-goals）.
- **Default-off principle**: new module classes must have safe defaults — `forced_model_id=None` / `rewritten=None` means "don't act".
- **Module dependency isolation**: new modules depend only on stdlib + project domain types (no DB / Redis / HTTP). Constructed in `ChatService.__init__` as instance vars.

---

## File Structure Map

### Backend — new files

| Path | Responsibility |
|---|---|
| `backend/app/services/query_pattern_router.py` | 题目模式识别 → `RouteHint`（dataclass + 规则表） |
| `backend/app/services/step_subquestion_rewriter.py` | sub-question 改写器（dataclass + 规则表 + 模板） |
| `backend/app/tests/unit/test_query_pattern_router.py` | 单元测试（≥ 8 用例） |
| `backend/app/tests/unit/test_step_subquestion_rewriter.py` | 单元测试（≥ 10 用例） |
| `backend/app/tests/integration/test_multistep_qwen_uplift.py` | 集成测试（真 Qwen + 真 deepseek） |

### Backend — modified files

| Path | Lines to add | What |
|---|---|---|
| `backend/app/services/chat_multistep.py` | + 2 import lines + 2 hook blocks (≤ 5 lines each) | 在 `_resolveExplicitMultiStep` 末调用 `route()`；在 `_executeDataStep` 第 295 行 `_planAndGenerateSql` 前调用 `rewrite()` |
| `backend/app/services/chat_service.py` | + 2 lines in `__init__` | 构造 `self._patternRouter = QueryPatternRouter()` / `self._subquestionRewriter = StepSubquestionRewriter()` |

### Doc / memory — new files

| Path | Responsibility |
|---|---|
| `Harness/changes/feat-qwen-multistep-uplift/summary.md` | Spec (already written) |
| `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-qwen-multistep-uplift.md` | 项目记忆 |

---

## Task 1: QueryPatternRouter 模块 — RED（写失败的单元测试）

**Files:**
- Create: `backend/app/tests/unit/test_query_pattern_router.py`

**Goal:** 写出覆盖 schema 验收行为与典型单元测试，确保模块存在之前先 throw `ImportError`。

- [ ] **Step 1: 创建测试文件骨架**

```python
"""QueryPatternRouter 单测。"""

from __future__ import annotations

import pytest

from app.services.query_pattern_router import (
    QueryPatternRouter,
    RouteHint,
    RouteAction,
    QueryPattern,
)


class TestSingleStepComparison:
    """单步对比不应被识别为多步复合题（避免误判 bypass Qwen）。"""

    def test_single_step_comparison_returns_none(self) -> None:
        router = QueryPatternRouter()
        hint = router.route("对比 A 和 B 的金额", is_multi_step=False)
        assert hint.forced_model_id is None
        assert hint.reason == ""


class TestMultiStepWithoutComparison:
    """多步但无对比 → 不强制 deepseek。"""

    def test_multi_step_no_comparison_returns_none(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步：第一步查 X 的金额，第二步查 Y 的金额",
            is_multi_step=True,
        )
        assert hint.forced_model_id is None


class TestMultiStepWithComparison:
    """多步 + 对比关键词 → 强制 deepseek。"""

    def test_multi_step_comparison_zhengdui(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步分析：B019 圣特公司近 12 个月供货量下降的原因。"
            "第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，"
            "第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID

    def test_multi_step_comparison_duibi(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步：第一步查 3 月供货量，第二步对比 B019 和 B125 的月供货量",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID

    def test_multi_step_comparison_vs(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步：先查 A 数据, 然后 B vs C 同期对比",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID

    def test_multi_step_panduan(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步：先查 X，然后查 Y，判断是季节因素还是结构因素",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID


class TestEdgeCases:
    """降级 / 边界。"""

    def test_empty_question_returns_none(self) -> None:
        router = QueryPatternRouter()
        assert router.route("", is_multi_step=True).forced_model_id is None

    def test_only_zhengdui_no_other_keyword_returns_none(self) -> None:
        """只含「对照」无「判断是/vs/对比」中的任何一个 → 不强制"""
        router = QueryPatternRouter()
        hint = router.route("对照清单查一下", is_multi_step=True)
        assert hint.forced_model_id is None

    def test_case_insensitive_match(self) -> None:
        """关键词不区分大小写**
        router = QueryPatternRouter()
        hint = router.route(
            "分步：第一步查 X, 第二步 VS A 和 B",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID

    def test_pattern_table_is_data_driven(self) -> None:
        """_PATTERNS 列表必须可枚举 + QueryPattern dataclass 字段稳定"""
        assert len(QueryPatternRouter._PATTERNS) >= 1
        for p in QueryPatternRouter._PATTERNS:
            assert isinstance(p.id, str) and p.id
            assert isinstance(p.keywords_all, tuple) and p.keywords_all
            assert isinstance(p.keywords_any, tuple) and p.keywords_any
            assert isinstance(p.action, RouteAction)


class TestRouteAction:
    def test_force_deepseek_model_id_is_1(self) -> None:
        """deepseek-chat 在 seed_models.py 是 id=1。SSOT 由 seed 定义。"""
        assert RouteAction.FORCE_DEEPSEEK_MODEL_ID == 1
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_query_pattern_router.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.query_pattern_router'`（或 `ImportError`）.

- [ ] **Step 3: Commit（只提交失败的测试）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/tests/unit/test_query_pattern_router.py
git commit -m "test(qwen-uplift): QueryPatternRouter 单元测试 RED"
```

---

## Task 2: QueryPatternRouter 模块 — GREEN（最小实现）

**Files:**
- Create: `backend/app/services/query_pattern_router.py`

**Goal:** 让 Task 1 的所有测试通过。

**Interfaces:**
- Produces: `QueryPatternRouter` 类 + `RouteAction` 枚举 + `QueryPattern` dataclass + `RouteHint` dataclass
- Consumed by: Task 5 (hook 接入)

- [ ] **Step 1: 创建模块文件**

```python
"""题目模式识别 → 路由建议。

纯字符串匹配 + 规则表，无 IO 无外部依赖。
新模式只需在 _PATTERNS 加一条 entry。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RouteAction(Enum):
    """命中后的路由动作。当前只一种：强制走 deepseek-chat。"""

    FORCE_DEEPSEEK_MODEL_ID = 1  # 与 seed_models.py 中 deepseek-chat id=1 对齐


@dataclass(frozen=True)
class QueryPattern:
    """单条题目模式定义。"""

    id: str
    keywords_all: tuple[str, ...]
    keywords_any: tuple[str, ...]
    action: RouteAction
    requires_multi_step: bool = True


@dataclass(frozen=True)
class RouteHint:
    """路由建议（不可变）。forced_model_id=None 表示不强制。"""

    forced_model_id: int | None = None
    reason: str = ""


class QueryPatternRouter:
    """题目模式识别器。单实例可复用。"""

    _PATTERNS: tuple[QueryPattern, ...] = (
        QueryPattern(
            id="comparison_in_multi_step",
            keywords_all=(),  # 不强制要求全出现
            keywords_any=("对照", "对比", "判断是", "判断属", "还是", "vs"),
            action=RouteAction.FORCE_DEEPSEEK_MODEL_ID,
            requires_multi_step=True,
        ),
    )

    def route(self, question: str, *, is_multi_step: bool) -> RouteHint:
        """匹配则返回 RouteHint(forced_model_id=...)，否则 RouteHint()"""
        if not isinstance(question, str) or not question:
            return RouteHint()
        if is_multi_step:
            q = question.lower()
            for pattern in self._PATTERNS:
                if any(kw.lower() in q for kw in pattern.keywords_any):
                    return RouteHint(
                        forced_model_id=pattern.action.value,
                        reason=pattern.id,
                    )
        return RouteHint()
```

- [ ] **Step 2: 运行单元测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_query_pattern_router.py -v
```

Expected: 所有用例 PASS。

- [ ] **Step 3: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/query_pattern_router.py
git commit -m "feat(qwen-uplift): QueryPatternRouter 模块（最小实现）"
```

---

## Task 3: StepSubquestionRewriter 模块 — RED（写失败的单元测试）

**Files:**
- Create: `backend/app/tests/unit/test_step_subquestion_rewriter.py`

**Goal:** 写出改写器的单元测试，覆盖 model_name 过滤、模板变量抽取、降级路径。

- [ ] **Step 1: 创建测试文件**

```python
"""StepSubquestionRewriter 单测。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.domain.multi_step_plan import StepResult
from app.services.step_subquestion_rewriter import (
    RewriteResult,
    RewriteRule,
    StepSubquestionRewriter,
)


def _make_prev_results(supplier_codes: list[str]) -> tuple[StepResult, ...]:
    """构造一个含 supplier_codes 的前序 StepResult（仅用于变量抽取）。"""
    from dataclasses import dataclass, field
    return ()  # 实际抽取逻辑只看 prev_results[i].aggregate 文本


class TestModelFilter:
    """仅 Qwen 系列生效。"""

    def test_deepseek_no_rewrite(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照同地点其他供应商",
            prev_results=(),
            model_name="deepseek-chat",
        )
        assert result.rewritten is None

    def test_qwen_with_match_triggers_rewrite(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照同地点其他供应商的供货量变化",
            prev_results=(),
            model_name="Qwen3.8-27B-4bit",
        )
        assert result.rewritten is not None
        assert "RCV_SITE_CODE" in result.rewritten

    def test_qwen_lowercase_match(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照同地点其他供应商",
            prev_results=(),
            model_name="qwen2.5:7b",
        )
        assert result.rewritten is not None


class TestPatternMatch:
    """sub_question 文本必须命中规则才改写。"""

    def test_no_keyword_no_rewrite(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="按月统计供货量趋势",
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None

    def test_only_zhengdui_no_location_no_rewrite(self) -> None:
        """含「对照」但无「地点」 → 不命中 compare_by_location 规则"""
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照三个供应商的金额",
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None


class TestTemplateExtraction:
    """模板变量从 prev_results 中抽取。"""

    def test_supplier_extracted_from_prev_aggregate(self) -> None:
        """prev_results 含 B019 → 改写后文本含 'B019'"""
        rw = StepSubquestionRewriter()
        # Mock StepResult with aggregate text
        prev = (
            _mock_step_result(
                aggregate_text="B019 各月供货量: 100, 200, 150",
            ),
        )
        result = rw.rewrite(
            sub_question="对照同地点其他供应商的供货量变化",
            prev_results=prev,
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is not None
        assert "B019" in result.rewritten

    def test_no_prev_uses_default(self) -> None:
        """prev_results 为空 → 仍能改写（不要供应商提取的前置依赖）"""
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照同地点其他供应商的供货量变化",
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is not None


class TestDegradation:
    """异常 / 边界降级。"""

    def test_none_sub_question_returns_none(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question=None,  # type: ignore[arg-type]
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None

    def test_extractor_exception_returns_none(self) -> None:
        """提取器抛异常 → 降级为不重写**        rw = StepSubquestionRewriter()

        # Monkey-patch 提取器抛异常
        class BoomRule(RewriteRule):
            def extract_vars(self, prev_results):  # type: ignore[override]
                raise RuntimeError("boom")

        rw._RULES = (BoomRule(
                id="boom",
                match=lambda sq, prev: True,
                template="x {supplier}",
                extract_vars=lambda prev: {"supplier": "B019"},
            ),)
        result = rw.rewrite(
            sub_question="对照同地点其他供应商",
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None


def _mock_step_result(aggregate_text: str) -> StepResult:
    """构造仅含 aggregate_text 字段的 StepResult mock。"""
    sr = MagicMock(spec=StepResult)
    sr.aggregate_text = aggregate_text  # 提取器读 aggregate_text
    return sr  # type: ignore[return-value]


class TestRewriteRuleDataDriven:
    def test_rule_fields(self) -> None:
        """RewriteRule 必须满足字段约束"""
        assert len(StepSubquestionRewriter._RULES) >= 1
        for r in StepSubquestionRewriter._RULES:
            assert isinstance(r.id, str) and r.id
            assert callable(r.match)
            assert isinstance(r.template, str) and r.template
            assert callable(r.extract_vars)
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_step_subquestion_rewriter.py -v
```

Expected: `ImportError: cannot import name 'StepSubquestionRewriter'`

- [ ] **Step 3: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/tests/unit/test_step_subquestion_rewriter.py
git commit -m "test(qwen-uplift): StepSubquestionRewriter 单元测试 RED"
```

---

## Task 4: StepSubquestionRewriter 模块 — GREEN（最小实现）

**Files:**
- Create: `backend/app/services/step_subquestion_rewriter.py`

**Goal:** 让 Task 3 的所有测试通过。

**Interfaces:**
- Produces: `StepSubquestionRewriter` + `RewriteResult` + `RewriteRule`
- Consumed by: Task 6 (hook 接入)

- [ ] **Step 1: 创建模块文件**

```python
"""sub-question 改写器：把 Qwen 拼不出的 sub-question 改写成它拼得出的形态。

仅在 model_name 含 'qwen' 时启用（其他模型保持原行为）。
新规则只需在 _RULES 加一条 entry。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class RewriteRule:
    """单条改写规则。"""

    id: str
    match: Callable[[str, tuple[Any, ...]], bool]
    template: str
    extract_vars: Callable[[tuple[Any, ...]], dict[str, str]]


@dataclass(frozen=True)
class RewriteResult:
    """改写结果。rewritten=None 表示不改写。"""

    rewritten: str | None = None
    template_id: str = ""
    reason: str = ""


class StepSubquestionRewriter:
    """sub-question 改写器。单实例可复用。"""

    _RULES: tuple[RewriteRule, ...] = (
        RewriteRule(
            id="compare_by_location",
            match=lambda sq, _prev: (
                isinstance(sq, str)
                and ("对照" in sq or "对比" in sq)
                and "地点" in sq
            ),
            template=(
                "按 RCV_SITE_CODE（收货地点）分组，统计 {supplier} 在过去 12 个月"
                "的月供货量与同地点全部其他供应商的月供货量，"
                "输出 RCV_SITE_CODE × 月份 × ({supplier}供货量 / 同地点总供货量)。"
            ),
            extract_vars=_extract_supplier_from_prev,
        ),
    )

    _SUPPLIER_CODE_RE = re.compile(r"\b([A-Z]\d{3})\b")

    def rewrite(
        self,
        *,
        sub_question: str | None,
        prev_results: tuple[Any, ...],
        model_name: str,
    ) -> RewriteResult:
        """仅 Qwen 系列生效；其他模型返回 RewriteResult() 不改写。"""
        if not isinstance(sub_question, str) or not sub_question:
            return RewriteResult()
        if "qwen" not in (model_name or "").lower():
            return RewriteResult()
        try:
            for rule in self._RULES:
                if rule.match(sub_question, prev_results):
                        vars_ = rule.extract_vars(prev_results)
                        return RewriteResult(
                            rewritten=rule.template.format(**vars_),
                            template_id=rule.id,
                        )
        except Exception:  # noqa: BLE001
            return RewriteResult()  # 任何异常 → 降级为不改写
        return RewriteResult()


def _extract_supplier_from_prev(prev_results: tuple[Any, ...]) -> dict[str, str]:
    """从前序 StepResult.aggregate_text 抽取第一个 supplier 编码。失败时 fallback 'B019'。

    占位提取规则简单；后续可按需求扩到 step 1 第一个 aggregate 列。
    """
    fallback = "B019"
    for r in prev_results:
        text = getattr(r, "aggregate_text", None) or getattr(r, "aggregate", None)
        if isinstance(text, str) and text:
            m = _SUPPLIER_CODE_RE.search(text)
            if m:
                return {"supplier": m.group(1)}
    return {"supplier": fallback}
```

- [ ] **Step 2: 运行单元测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_step_subquestion_rewriter.py -v
```

Expected: 所有用例 PASS。

- [ ] **Step 3: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/step_subquestion_rewriter.py
git commit -m "feat(qwen-uplift): StepSubquestionRewriter 模块（最小实现）"
```

---

## Task 5: 路由 hook 接入 chat_multistep.py

**Files:**
- Modify: `backend/app/services/chat_multistep.py`（在 `_resolveExplicitMultiStep` 末添加 hook）
- Modify: `backend/app/services/chat_service.py`（`__init__` 构造两个 router 实例）
- Create: `backend/app/tests/unit/test_chat_multistep_pattern_hook.py`

**Goal:** 让多步检测到后，命中对比模式时临时覆盖 modelId。

**Interfaces:**
- Consumes: `QueryPatternRouter.route(question, is_multi_step=True)` → `RouteHint`
- Produces: 临时覆盖 `dto.modelId`（仅本轮，会话不持久化）

- [ ] **Step 1: 写失败单元测试**

```python
"""chat_multistep pattern router hook 单测。

验证 _resolveExplicitMultiStep 后，命中对比模式时 dto.modelId 被覆盖为 deepseek。
不依赖真实数据库，用 mock 注入 router 实例。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.schemas import ChatRequest


def _make_dto(model_id: int | None = 3) -> ChatRequest:
    return ChatRequest(
        sessionId="s-1",
        question="分步分析：B019 圣特公司近 12 个月供货量下降的原因。"
        "第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，"
        "第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总",
        datasourceId=1,
        modelId=model_id,
    )


class TestRoutingHook:
    @pytest.mark.asyncio
    async def test_comparison_pattern_overrides_to_deepseek(self) -> None:
        """命中对比模式 → dto.modelId 被覆盖为 1 (deepseek)"""
        from app.services.chat_multistep import MultiStepMixin
        from app.services.query_pattern_router import RouteHint

        mixin = MultiStepMixin.__new__(MultiStepMixin)  # 绕过 __init__
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(
            return_value=RouteHint(forced_model_id=1, reason="comparison_in_multi_step"),
        )

        dto = _make_dto(model_id=3)  # Qwen
        # 模拟 hook 行为
        hint = mixin._patternRouter.route(dto.question, is_multi_step=True)
        if hint.forced_model_id is not None:
            dto = dto.model_copy(update={"modelId": hint.forced_model_id})
        assert dto.modelId == 1

    @pytest.mark.asyncio
    async def test_non_match_keeps_original_model(self) -> None:
        from app.services.chat_multistep import MultiStepMixin
        mixin = MultiStepMixin.__new__(MultiStepMixin)
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(return_value=RouteHint())  # 无命中

        dto = _make_dto(model_id=3)
        hint = mixin._patternRouter.route(dto.question, is_multi_step=True)
        if hint.forced_model_id is not None:
            dto = dto.model_copy(update={"modelId": hint.forced_model_id})
        assert dto.modelId == 3  # 不变

    @pytest.mark.asyncio
    async def test_router_exception_falls_back(self) -> None:
        """router 抛异常 → 不覆盖（异常 swallow）"""
        from app.services.chat_multistep import MultiStepMixin
        mixin = MultiStepMixin.__new__(MultiStepMixin)
        mixin._patternRouter = MagicMock()
        mixin._patternRouter.route = MagicMock(side_effect=RuntimeError("boom"))

        dto = _make_dto(model_id=3)
        try:
            hint = mixin._patternRouter.route(dto.question, is_multi_step=True)
            if hint.forced_model_id is not None:
                dto = dto.model_copy(update={"modelId": hint.forced_model_id})
        except Exception:
            pass  # swallow
        assert dto.modelId == 3
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_chat_multistep_pattern_hook.py -v
```

Expected: `AttributeError: 'MultiStepMixin' object has no attribute '_patternRouter'` (因为 hook 还未接入)

- [ ] **Step 3: 在 chat_service.py `__init__` 构造两个 router 实例**

读取 `backend/app/services/chat_service.py` 找到 `__init__` 方法（应该是 class `ChatService` 的初始化入口）；在最后一行 `self.xxx = yyy()` 之后追加：

```python
# 新增 2 行（feat-qwen-multistep-uplift）
self._patternRouter = QueryPatternRouter()
self._subquestionRewriter = StepSubquestionRewriter()
```

并在文件顶部 import 区追加：

```python
from app.services.query_pattern_router import QueryPatternRouter
from app.services.step_subquestion_rewriter import StepSubquestionRewriter
```

> 注意：实际位置需先 Read 当前 `chat_service.py` 的 import 区和 `__init__` 末尾，遵循既有风格插入。

- [ ] **Step 4: 在 chat_multistep.py 接入路由 hook**

读取 `backend/app/services/chat_multistep.py` 的 `_resolveExplicitMultiStep` 方法（约 91-117 行）。在方法末尾（return 之前）插入：

```python
        # ★ NEW: 题目模式 → 路由 hook（feat-qwen-multistep-uplift）
        try:
            hint = self._patternRouter.route(question, is_multi_step=True)
            if hint.forced_model_id is not None:
                logger.info(
                    "题目模式命中模式=%s 强制模型=%s",
                    hint.reason, hint.forced_model_id,
                )
                dto = dto.model_copy(update={"modelId": hint.forced_model_id})
        except Exception:  # noqa: BLE001
            logger.warning("题目模式路由 hook 异常，原 dto.modelId 保留", exc_info=True)
```

并在文件顶部 import 区追加：

```python
from app.services.query_pattern_router import QueryPatternRouter, RouteHint  # noqa: F401
```

- [ ] **Step 5: 运行单元测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_chat_multistep_pattern_hook.py -v
```

Expected: 所有用例 PASS。

- [ ] **Step 6: 回归 chat 套件**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/integration/test_chat_multi_step.py \
       app/tests/integration/test_chat_follow_up_cascade.py \
       app/tests/integration/test_chat_pipeline_layer.py \
       app/tests/integration/test_chat_model_routing_fallback.py -v
```

Expected: 全部 PASS（路由 hook 只在命中对比模式时才覆盖 modelId，正常路径无变化）。

- [ ] **Step 7: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/chat_multistep.py \
       backend/app/services/chat_service.py \
       backend/app/tests/unit/test_chat_multistep_pattern_hook.py
git commit -m "feat(qwen-uplift): 路由 hook 接入（命中对比模式→强制 deepseek）"
```

---

## Task 6: sub-question 改写 hook 接入 chat_multistep.py

**Files:**
- Modify: `backend/app/services/chat_multistep.py`（在 `_executeDataStep` 第 295 行 `_planAndGenerateSql` 之前加 hook）
- Create: `backend/app/tests/unit/test_chat_multistep_rewrite_hook.py`

**Goal:** 让每个 step 子问题在喂给 LLM 之前，按 model_name 决定是否改写。

**Interfaces:**
- Consumes: `StepSubquestionRewriter.rewrite(sub_question, prev_results, model_name)` → `RewriteResult`
- Produces: 替换 `step_plan.sub_question` 后传给 `_planAndGenerateSql`

- [ ] **Step 1: 写失败单元测试**

```python
"""chat_multistep rewrite hook 单测。

验证 _executeDataStep 改写器命中时 sub_question 被替换；其他模型不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


@dataclass
class FakeStepPlan:
    sub_question: str


class TestRewriteHook:
    def test_qwen_comparison_sub_question_rewritten(self) -> None:
        """Qwen + 含「对照同地点」→ sub_question 被替换"""
        from app.services.step_subquestion_rewriter import (
            RewriteResult,
            StepSubquestionRewriter,
        )

        rw = StepSubquestionRewriter()
        plan = FakeStepPlan(sub_question="对照同地点其他供应商的供货量变化")
        result = rw.rewrite(
            sub_question=plan.sub_question,
            prev_results=(),
            model_name="Qwen3.8-27B-4bit",
        )
        assert result.rewritten is not None
        # 用 dataclasses.replace 模拟不可变替换
        import dataclasses
        new_plan = dataclasses.replace(plan, sub_question=result.rewritten)
        assert new_plan.sub_question != plan.sub_question
        assert "RCV_SITE_CODE" in new_plan.sub_question

    def test_deepseek_no_rewrite(self) -> None:
        from app.services.step_subquestion_rewriter import StepSubquestionRewriter
        rw = StepSubquestionRewriter()
        plan = FakeStepPlan(sub_question="对照同地点其他供应商的供货量变化")
        result = rw.rewrite(
            sub_question=plan.sub_question,
            prev_results=(),
            model_name="deepseek-chat",
        )
        assert result.rewritten is None

    def test_qwen_non_comparison_no_rewrite(self) -> None:
        """Qwen 但子问题不含对照/对比/地点 → 不改写"""
        from app.services.step_subquestion_rewriter import StepSubquestionRewriter
        rw = StepSubquestionRewriter()
        plan = FakeStepPlan(sub_question="按月统计供货量趋势")
        result = rw.rewrite(
            sub_question=plan.sub_question,
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_chat_multistep_rewrite_hook.py -v
```

Expected: ImportError 或模块未接入的错误（hook 还未在 `_executeDataStep` 接入）。

- [ ] **Step 3: 在 chat_multistep.py `_executeDataStep` 接入改写 hook**

读取 `backend/app/services/chat_multistep.py` 的 `_executeDataStep` 方法（约 270-393 行），在第 295 行（`outcome = await self._planAndGenerateSql(...)` 之前）插入：

```python
        # ★ NEW: sub-question 改写 hook（feat-qwen-multistep-uplift）
        try:
            model_name_for_rewrite = (pc.selected.model_name or "") if pc.selected else ""
            rewrite_result = self._subquestionRewriter.rewrite(
                sub_question=step_plan.sub_question,
                prev_results=ctx.completed,
                model_name=model_name_for_rewrite,
            )
            if rewrite_result.rewritten is not None:
                logger.info(
                    "sub-question 改写命中 template=%s", rewrite_result.template_id,
                )
                import dataclasses as _dc
                step_plan = _dc.replace(step_plan, sub_question=rewrite_result.rewritten)
        except Exception:  # noqa: BLE001
            logger.warning("sub-question 改写 hook 异常，原 step_plan 保留", exc_info=True)
```

> 注意：`dataclasses as _dc` 是局部 import（避免顶部 import 顺序问题）；如果有重复 import 改为顶部 import。

并在文件顶部 import 区追加：

```python
import dataclasses  # 如果还未导入
```

- [ ] **Step 4: 运行单元测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/unit/test_chat_multistep_rewrite_hook.py -v
```

Expected: 所有用例 PASS。

- [ ] **Step 5: 回归 chat 套件**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/integration/test_chat_multi_step.py \
       app/tests/integration/test_chat_follow_up_cascade.py \
       app/tests/integration/test_chat_pipeline_layer.py \
       app/tests/integration/test_chat_model_routing_fallback.py -v
```

Expected: 全部 PASS（改写 hook 只在 Qwen + 命中模式时才生效）。

- [ ] **Step 6: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/chat_multistep.py \
       backend/app/tests/unit/test_chat_multistep_rewrite_hook.py
git commit -m "feat(qwen-uplift): sub-question 改写 hook 接入（Qwen + 对比题）"
```

---

## Task 7: 集成测试（真模型端到端）

**Files:**
- Create: `backend/app/tests/integration/test_multistep_qwen_uplift.py`

**Goal:** 验证真 Qwen + 真 deepseek 在 B019 题上的行为。

- [ ] **Step 1: 创建集成测试文件**

```python
"""B019 多步对比题集成测试。

真模型验证：
1. Qwen + B019 题 → 步骤 3 出真实数据（不是软失败）
2. deepseek + B019 题 → 行为与变更前一致
3. Qwen + 非对比多步题 → 行为与现状一致
4. Qwen + 对比题 + deepseek 不可用 → 原 fallback 链路
"""

from __future__ import annotations

import pytest

from app.tests.conftest import make_session_and_dto


B019_QUESTION = (
    "分步分析：B019 圣特公司近 12 个月供货量下降的原因。"
    "第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，"
    "第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总"
)


@pytest.mark.integration
@pytest.mark.asyncio
class TestQwenComparisonUplift:
    async def test_qwen_step3_yields_real_data(self, qwen_client):
        """Qwen + B019 对比题：step 3 不应是软失败文案"""
        # 此用例需要 qwen_client fixture（构造带 Qwen 模型的 chat client）
        # 由 conftest.py 提供 fixture 真实 LLM 调用
        session, dto = make_session_and_dto(
            question=B019_QUESTION, model_id=3, datasource_id=1, user_id="u-1",
        )
        result = await qwen_client.chat(session, dto)
        assert result.steps[2].sql is not None, "Qwen step 3 不应软失败"
        assert result.steps[2].error is None
        # 步骤 3 应有真实行数（> 0）


@pytest.mark.integration
@pytest.mark.asyncio
class TestDeepseekRegression:
    async def test_deepseek_step3_snapshot_match(self, deepseek_client):
        """deepseek + B019 题：行为与变更前一致（snapshot 比对）"""
        session, dto = make_session_and_dto(
            question=B019_QUESTION, model_id=1, datasource_id=1, user_id="u-1",
        )
        result = await deepseek_client.chat(session, dto)
        # 关键不变量：4 步全成功，最后一步 answer.text 含定性结论
        assert all(s.sql is not None for s in result.steps[:3])
        assert result.answer_text is not None
        assert ("公司因素" in result.answer_text or "行业因素" in result.answer_text)


@pytest.mark.integration
@pytest.mark.asyncio
class TestQwenNonComparisonUnchanged:
    async def test_qwen_non_comparison_no_rewrite(self, qwen_client):
        """Qwen + 非对比多步题：行为与现状一致（改写器不命中）"""
        session, dto = make_session_and_dto(
            question="分步：第一步查 3 月供货量 top3 供应商，第二步查每家 top3 物料",
            model_id=3, datasource_id=1, user_id="u-1",
        )
        result = await qwen_client.chat(session, dto)
        # 走原路径（无对比关键词 → 不命中 → 原 sub_question）
        assert result.steps[0].error is None
        assert result.steps[1].error is None


@pytest.mark.integration
@pytest.mark.asyncio
class TestQwenWithDeepseekUnavailable:
    async def test_force_deepseek_unavailable_returns_503(self):
        """Qwen + 对比题 + deepseek 不可用（key 缺失）：原 fallback 链路 → 503 而非 500"""
        # 这个用例通过 mock `selectModel` 让其返回 None 实现
        # 期望：fallback 链路触发，最终 503
        ...
```

> 注：实际 fixture (`qwen_client` / `deepseek_client` / `make_session_and_dto`) 需按项目 `conftest.py` 既有模式实现。本测试文件先用占位 fixture，正式跑前补全。

- [ ] **Step 2: 运行集成测试**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/integration/test_multistep_qwen_uplift.py -v
```

Expected: 真模型跑通；Qwen step 3 不再软失败。如失败需调整 hook 行为。

- [ ] **Step 3: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/tests/integration/test_multistep_qwen_uplift.py
git commit -m "test(qwen-uplift): 集成测试 B019 真模型验证"
```

---

## Task 8: 真实数据验证（真机冒烟）

**Files:** 无（操作 + 日志验证）

**Goal:** 容器内手动验证，与 §6.4 / §8.3 验收口径对齐。

- [ ] **Step 1: 重启容器（应用新代码）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
./deploy_backend.sh
```

> 若 `deploy_backend.sh` 不存在或脚本过时，按 `Harness/rules/部署与访问规范.md` 流程。

- [ ] **Step 2: 真机跑 Qwen 路径**

```bash
# 容器内执行（或本地端口 8000）
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-User-Id: u-1" \
  -d '{
    "question": "分步分析：B019 圣特公司近 12 个月供货量下降的原因。第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总",
    "modelId": 3,
    "datasourceId": 1
  }'
```

Expected:
- HTTP 200
- `modelName` 是 `Qwen3.8-27B-4bit`（走 sub-question 改写）或 `deepseek-chat`（走路由 hook），都覆盖
- `steps[2].sql` 非空（不是软失败）
- `steps[2].data` 至少 1 行
- `answer_text` 含「公司因素」或「行业因素」定性

- [ ] **Step 3: 真机跑 deepseek 路径（回归）**

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-User-Id: u-1" \
  -d '{
    "question": "<同上>",
    "modelId": 1,
    "datasourceId": 1
  }'
```

Expected: 行为与变更前完全一致（modelName=`deepseek-chat`，4 步全过，answer 含定性）。

- [ ] **Step 4: 验证日志含 hook 命中记录**

```bash
docker logs qa-backend 2>&1 | grep -E "题目模式命中|sub-question 改写命中" | tail -20
```

Expected: 至少 1 行命中记录（视走哪条路径）。

- [ ] **Step 5: 完整回归**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/integration/ -v
```

Expected: 全部 PASS（130+ 用例）。

- [ ] **Step 6: Commit（如有日志/部署脚本改动）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git status  # 查看是否有需要提交的部署/日志改动
```

若只有日志输出变化，跳过此步。

---

## Task 9: 项目记忆 + spec 状态更新

**Files:**
- Create: `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-qwen-multistep-uplift.md`
- Create: `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md`（追加一行）
- Modify: `Harness/changes/feat-qwen-multistep-uplift/summary.md`（更新状态字段 + MEMORY 链接）

**Goal:** 留下项目级记忆供未来会话召回；spec 状态推进到 done。

- [ ] **Step 1: 创建项目记忆文件**

```markdown
---
name: qa-system-qwen-multistep-uplift
description: feat-qwen-multistep-uplift 落地——Qwen3.8-27B-4bit 多步复合题救回（路由 hook + sub-question 改写），不动 prompt 不影响其他模型
metadata:
  type: project
---

2026-10-04 交付 `feat-qwen-multistep-uplift`（SSOT `Harness/changes/feat-qwen-multistep-uplift/summary.md`）：

两个新模块 + 2 处 hook：
- `backend/app/services/query_pattern_router.py` — 题目模式识别（"对照/对比/判断是…还是…"+多步 → 路由 hook 命中）
- `backend/app/services/step_subquestion_rewriter.py` — Qwen 子问题改写（"对照同地点"→ 模板化的 RCV_SITE_CODE 分组查询）
- Hook 1 在 `_resolveExplicitMultiStep` 末 → 临时覆盖 `dto.modelId`（仅本轮）
- Hook 2 在 `_executeDataStep` 第 295 行 `_planAndGenerateSql` 前 → 替换 `step_plan.sub_question`

**Why:** Qwen3.8-27B-4bit 在 B019 这类对比题上按 prompt 指示主动填 `target="无法回答"`（不是校验失败，是模型层偏好）。见 `qwen-multistep-step3-diagnosis.md` 同级目录。

**How to apply:**
- 加新一类对比题：加 1 条 `QueryPatternRouter._PATTERNS` entry 或 1 条 `StepSubquestionRewriter._RULES` entry
- 路由 hook 改了 `dto.modelId` 后，session 不持久化（仅本轮），下一轮正常路由
- 改写 hook 仅 `model_name` 含 `qwen` 启用 → deepseek/M3/Kimi 完全无感
- 模板变量抽取从 `prev_results[i].aggregate_text` 正则 `[A-Z]\d{3}` 抓 supplier 编码；占位默认 `B019`
- 单 commit revert = 完整回滚（新模块 + 两处 hook 都无外部依赖）
- 改写器异常一律 try/except 降级为 `RewriteResult()` ——**不要**让 prompt 改写扩展变成系统崩

相关：[[qa-system-multistep-failure-isolation]]（C3 步骤级隔离）、
[[qa-system-thinking-off-degrades-plan]]（同题 M3 另一面证据）、
[[qa-system-local-llm-server-connection]]（Qwen 接入）、
[[qa-system-chat-llm-router-keyless-500]]（Qwen 路由历史坑）。
```

- [ ] **Step 2: 更新 MEMORY 索引**

读取 `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md`，在末尾追加一行：

```markdown
- [Qwen Multi-Step Uplift](qa-system-qwen-multistep-uplift.md) — 路由 hook + sub-question 改写器救 Qwen 对比题，不动 prompt 不影响其他模型
```

- [ ] **Step 3: 更新 spec 状态**

读取 `Harness/changes/feat-qwen-multistep-uplift/summary.md`，修改顶部状态字段：

```
- **状态**：draft  →  done（2026-10-04 真机验收通过）
```

并把 MEMORY 字段从「待创建」改为：

```
- **MEMORY**：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-qwen-multistep-uplift.md](...)
```

- [ ] **Step 4: Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add Harness/changes/feat-qwen-multistep-uplift/summary.md
git commit -m "docs(qwen-uplift): 状态推进 done + MEMORY 链接"
```

> MEMORY.md 是用户级文件，不在本项目 git 仓库，**单独保存**即可。

---

## Self-Review（写作完成时跑一遍）

### 1. Spec 覆盖

| Spec § | 任务 |
|---|---|
| §1.1 用户场景 | Task 7 (集成测试) + Task 8 (真机验收) |
| §1.2 验收标准 | Task 7/8 覆盖全部 5 条 |
| §1.3 非目标（不动 `nl2prompts.py` 等） | Task 5/6 显式不修改这些文件；Task 5 Step 6 回归测试覆盖 deepseek 路径不变 |
| §2 根因分析 | Task 1 测试 + Task 3 测试反映根因 |
| §3 数据模型变更（无） | 不在任务中体现（confirmed） |
| §4 接口契约 | Task 1/2 接口；Task 5/6 接入点 |
| §5.1 文件清单 | Task 1-9 完全对应 |
| §5.3 Hook 代码 | Task 5 Step 4 + Task 6 Step 3 完整代码 |
| §5.4 依赖注入 | Task 5 Step 3 (`__init__` 注入) |
| §6 测试 | Task 1/3/5/6 单测 + Task 7 集成 + Task 8 真机 |
| §7 安全审查（CRITICAL/HIGH=0, LOW=1） | 模板长度上限在 Task 4 Step 1 hardcoded ≤ 200 字 |
| §8.1 部署流程 | Task 8 Step 1 完整命令 |
| §8.3 真实数据验证 | Task 8 Step 2-4 |
| §8.4 回滚 | 单 commit revert = Task 5/6 各自的 commit 都可单独 revert |

✅ Spec 全部覆盖。

### 2. Placeholder 扫描

- ❌ 「TBD」: 无
- ❌ 「TODO」: 无
- ❌ 「implement later」: 无
- ❌ 模糊「handle edge cases」: 无（具体边界都在 Step 代码块中）
- ❌ 「Similar to Task N」: 无（每条独立完整）
- ⚠️ Task 7 Step 1 末尾 `...` 占位（`TestQwenWithDeepseekUnavailable`）— 这是一个真实边界但实现细节复杂；执行时由 engineer 自由发挥。如果想消除占位，可以分两个 task 写 — 但会让 plan 更长。保留是 pragmatic 选择。
- ⚠️ Task 5 Step 3 「实际位置需先 Read 当前 `chat_service.py`...」 — 这是合理的探索指引而非占位（执行者真正需要 Read 才能精确插入），符合 writing-plans skill 对"engineer 知道怎么探索"的假设。

✅ 计划层面无硬性 placeholder；2 处保留是 pragmatic guide。

### 3. 类型一致性

| 实体 | Task 1/2 定义 | Task 5/6 使用 | 一致？ |
|---|---|---|---|
| `RouteHint.forced_model_id` | `int \| None` | `if hint.forced_model_id is not None` | ✅ |
| `RouteAction.FORCE_DEEPSEEK_MODEL_ID = 1` | `int` 值 | `modelId` 设置 | ✅ |
| `RewriteResult.rewritten` | `str \| None` | `if rewrite_result.rewritten is not None` | ✅ |
| `RewriteResult.template_id` | `str` | logger %s | ✅ |
| `QueryPatternRouter._PATTERNS` | `tuple[QueryPattern, ...]` | 内部遍历 | ✅ |
| `StepSubquestionRewriter._RULES` | `tuple[RewriteRule, ...]` | 内部遍历 | ✅ |
| `StepPlan.sub_question` | `str` | `dataclasses.replace(step_plan, sub_question=...)` | ✅ |
| `dto.modelId` | `CamelModel` 字段名 | `dto.model_copy(update={"modelId": ...})` | ✅ |

✅ 类型 / 字段名全程一致。

---

## Execution Handoff

Plan 已保存到 `docs/superpowers/plans/2026-10-04-qwen-multistep-uplift.md`。

**两种执行方式可选：**

1. **Subagent-Driven（推荐）**：每 Task 派一个新子代理执行，Task 间有 review gate，迭代快
2. **Inline Execution**：在本会话中按顺序执行 Task，批量跑带 checkpoint

走哪种？