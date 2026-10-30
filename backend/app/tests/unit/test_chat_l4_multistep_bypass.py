"""显式分步问题不得被 L4 Agent Loop 截走（2026-10-31 生产缺陷修复）。

缺陷：`_handleNl2SqlAgent` 用朴素子串匹配探索性关键词（"是什么/为什么/如何"…），
多步式问题「第一步…第二步…分别是什么」只要含任一关键词就被 L4 Agent Loop 整题
截走 —— 直接产出大段文本（无分析计划卡 / 无查询计划 / 无分步 SQL，且单次
12~18 万 token）。而 L2 管线里的拆步规则 / 拆步 LLM 才是这类问题的正主
（MultiStepPlanCard + 分步 SQL 可见）。

修复：`is_explicit_multi_step(question)` 命中（零 LLM 成本的规则判定）时，
L4 直接让路（返回 None 降级到 L2/L3），纯探索性问题行为不变。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.schemas import ChatRequest, ChatResponse
from app.services.chat_service import ChatService

_SENTINEL_L4 = ChatResponse(answer="[L4:answered] 假答案", intent="new_query")


def _makeService(*, l4Enabled: bool = True) -> tuple[ChatService, list[str]]:
    """绕开 __init__ 构造 ChatService，spy 掉 L4 两个协作方法。"""
    svc = object.__new__(ChatService)
    l4Calls: list[str] = []

    async def _enabled(_session: Any) -> bool:
        return l4Enabled

    async def _runL4(**kwargs: Any) -> Any:
        l4Calls.append(kwargs["dto"].question)
        # 与 AgentLoopResult 的被读字段同形（terminated_reason / answer_text）
        return type("_FakeLoopResult", (), {"terminated_reason": "answered", "answer_text": "x"})()

    async def _build(*, session: Any, dto: ChatRequest, result: Any, **kw: Any) -> ChatResponse:
        return _SENTINEL_L4

    svc._isL4AgentLoopEnabled = _enabled  # type: ignore[method-assign]
    svc._runL4AgentLoop = _runL4  # type: ignore[method-assign]
    svc._buildL4ChatResponse = _build  # type: ignore[method-assign]
    return svc, l4Calls


def _dto(question: str) -> ChatRequest:
    return ChatRequest(sessionId="l4-bypass-test", question=question, datasourceId=1)


# 生产实锤问题（15:45-15:49 连发 4 次、单次 12~18 万 token 的那类问法）：
# 含「是什么」探索性关键词 + 显式「第X步」标号
_MULTISTEP_WITH_KEYWORD = (
    "第一步，查询一下5月份供货量最多的是那三家供应商，"
    "第二步，看一下这三家供应商各自供货量前三的物料分别是什么，"
    "第三步，分析一下这三家供货商最少的物料是什么？"
)


class TestExplicitMultistepBypassesL4:
    @pytest.mark.asyncio
    async def test_step_numbered_question_bypasses_l4(self) -> None:
        """「第X步」标号 + 含「是什么」→ 不进 L4，降级 L2（多步拆步接管）。"""
        svc, l4Calls = _makeService()
        result = await svc._handleNl2SqlAgent(None, _dto(_MULTISTEP_WITH_KEYWORD))
        assert result is None, "显式分步问题不应被 L4 Agent Loop 截走"
        assert l4Calls == [], "L4 agent loop 不应被执行"

    @pytest.mark.asyncio
    async def test_fenbu_analysis_with_why_bypasses_l4(self) -> None:
        """「分步分析：…为什么…」同时命中分步关键词与「为什么」→ 分步优先。"""
        svc, l4Calls = _makeService()
        question = "分步分析：B019 圣特公司近 12 个月供货量下降的原因。第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，为什么下降要给结论"
        result = await svc._handleNl2SqlAgent(None, _dto(question))
        assert result is None
        assert l4Calls == []

    @pytest.mark.asyncio
    async def test_sequential_question_bypasses_l4(self) -> None:
        """「先…然后…」顺序指令型分步问题同样让路（与 is_explicit_multi_step 同口径）。"""
        svc, l4Calls = _makeService()
        result = await svc._handleNl2SqlAgent(
            None, _dto("先查各供应商的收货数量，然后再解释这些供应商的到货及时率是如何计算的")
        )
        assert result is None
        assert l4Calls == []


class TestExploratoryStillHitsL4:
    @pytest.mark.asyncio
    async def test_pure_exploratory_question_still_enters_l4(self) -> None:
        """无分步结构的探索性问题 → L4 行为不变（回归守卫）。"""
        svc, l4Calls = _makeService()
        result = await svc._handleNl2SqlAgent(
            None, _dto("为什么各供应商的收货数量不同？")
        )
        assert result is _SENTINEL_L4
        assert l4Calls == ["为什么各供应商的收货数量不同？"]

    @pytest.mark.asyncio
    async def test_l4_disabled_still_returns_none(self) -> None:
        """开关关闭 → 无论问法一律 None（既有行为不回归）。"""
        svc, l4Calls = _makeService(l4Enabled=False)
        result = await svc._handleNl2SqlAgent(None, _dto("为什么各供应商的收货数量不同？"))
        assert result is None
        assert l4Calls == []

    @pytest.mark.asyncio
    async def test_multistep_without_keyword_still_bypasses(self) -> None:
        """无探索性关键词的分步问题本来就不进 L4（行为不回归）。"""
        svc, l4Calls = _makeService()
        result = await svc._handleNl2SqlAgent(
            None, _dto("第一步查询5月份供货量最多的三家供应商，第二步看这三家各自前三的物料")
        )
        assert result is None
        assert l4Calls == []
