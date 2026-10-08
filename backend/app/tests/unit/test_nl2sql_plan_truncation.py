"""计划阶段的截断检测 + 预算翻倍单测（对齐 SQL 阶段既有范式）。

背景（2026-10-03 真机）：计划阶段 maxTokens 上限 2048（`generateQueryPlan`
签名硬编码默认，`generateValidatedPlan` 的 common 字典**不含** maxTokens ⇒
`NL2SQL_MAX_TOKENS` 系统参数对它无效）。推理模型写满2048 仍没写完思维链，
回复被截断在 JSON 之前 ⇒ 连续 3 次 `PLAN_REPLY_EMPTY` ⇒ chat 报 400。

SQL 阶段早有截断退避（`nl2sql_service.py`），计划阶段没有。本模块锁住补齐后的行为：
- 达到 token 上限 ⇒ 判截断、翻倍预算重试，**不得**把截断片段当成功解析
- 翻倍有封顶（`_NL2SQL_TRUNCATION_BACKOFF_DEFAULT`），不无限增长
- 未达上限 ⇒ 不触发翻倍
- `isApproximateUsage=True`（近似计量）时不判截断——不知道真实用量

注：翻倍封顶 4096 小于实测需求 6387，故本改动**单独不足以修复** MiniMax-M3，
它是关不掉 thinking 的模型（M2.x）的唯一防线；根治靠 `disable_thinking`（省 86-92%）。
"""

from __future__ import annotations

import json

import pytest
from tenacity import wait_none

from app.domain.exceptions import Nl2SqlError
from app.domain.models import OntologyClass
from app.domain.query_plan import Aggregation, QueryPlan
from app.services import llm_retry_policy as policy
from app.services.nl2sql_plan import _NL2SQL_TRUNCATION_BACKOFF_DEFAULT
from app.services.nl2sql_service import Nl2SqlService


class _Resp:
    def __init__(
        self,
        content: str,
        promptTokens: int = 10,
        completionTokens: int = 5,
        *,
        approximate: bool = False,
    ) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.isApproximateUsage = approximate


class _ScriptedLlm:
    def __init__(self, script: list[_Resp]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append({"messages": messages, **kwargs})
        return self._script.pop(0)


def _cls(name: str = "PRECEIPT") -> OntologyClass:
    return OntologyClass(class_name=name, source_table=f"T_{name}")


def _llmConfig():
    from types import SimpleNamespace

    return SimpleNamespace(model_name="test-model", temperature=0.0)


def _validPlanJson() -> str:
    plan = QueryPlan(
        target="各供应商收货数量",
        selectedClasses=("PRECEIPT",),
        selectedProperties=("BPSNUM", "QTY"),
        aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
        groupBy=("BPSNUM",),
    )
    return json.dumps(plan.to_dict(), ensure_ascii=False)


# 截断回复：usage 顶到上限，内容只有半个 JSON（解析必然失败）
def _truncated(atLimit: int) -> _Resp:
    return _Resp('{"selectedClasses": ["PRE', 300, atLimit)


@pytest.fixture(autouse=True)
def noBackoffWait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "RETRY_WAIT_EXPONENTIAL", wait_none())


class TestPlanStageTruncationBackoff:
    async def test_budget_doubles_after_truncation(self) -> None:
        """首轮顶到 2048 ⇒ 第二轮预算翻倍到 4096。

        注意断言的kwarg 名是 maxTokens（BaseLlmClient 层），
        不是 max_tokens（那是 SDK 内部转换后的名字）。
        """
        fake = _ScriptedLlm([_truncated(2048), _Resp(_validPlanJson(), 300, 900)])

        await Nl2SqlService().generateQueryPlan(
            "问题", [_cls()], fake, _llmConfig(), maxRetries=1, maxTokens=2048,
        )

        assert [c["maxTokens"] for c in fake.calls] == [2048, 4096]

    async def test_retry_after_backoff_can_succeed(self) -> None:
        """翻倍后写出完整计划 ⇒ 正常返回 plan。"""
        fake = _ScriptedLlm([_truncated(2048), _Resp(_validPlanJson(), 300, 900)])

        result = await Nl2SqlService().generateQueryPlan(
            "问题", [_cls()], fake, _llmConfig(), maxRetries=1, maxTokens=2048,
        )

        assert result.plan.selectedClasses == ("PRECEIPT",)

    async def test_backoff_is_capped(self) -> None:
        """连续截断时预算不得无限增长——封顶值来自既有派生常量。

        两次都截断 ⇒ 重试耗尽，按既有契约抛 Nl2SqlError（detail 含两次失败原因）。
        预算序列 [2048, 4096] 即封顶证据：第三轮若发生会是 8192。
        """
        fake = _ScriptedLlm([_truncated(2048), _truncated(4096)])

        with pytest.raises(Nl2SqlError) as exc:
            await Nl2SqlService().generateQueryPlan(
                "问题", [_cls()], fake, _llmConfig(), maxRetries=1, maxTokens=2048,
            )

        assert [c["maxTokens"] for c in fake.calls] == [2048, 4096]
        assert _NL2SQL_TRUNCATION_BACKOFF_DEFAULT == 4096
        # 截断原因必须回进 detail，否则运维看到的是"解析失败"而非"预算不够"
        assert "达到 token 上限" in (exc.value.detail or "")

    async def test_no_backoff_when_under_limit(self) -> None:
        """未达上限 ⇒ 不翻倍，预算保持原值。"""
        fake = _ScriptedLlm([_Resp(_validPlanJson(), 300, 500)])

        await Nl2SqlService().generateQueryPlan(
            "问题", [_cls()], fake, _llmConfig(), maxRetries=1, maxTokens=2048,
        )

        assert [c["maxTokens"] for c in fake.calls] == [2048]

    async def test_approximate_usage_does_not_trigger_backoff(self) -> None:
        """近似计量时不知道真实用量（isApproximateUsage=True）⇒ 不判截断。"""
        fake = _ScriptedLlm([_Resp(_validPlanJson(), 300, 99999, approximate=True)])

        await Nl2SqlService().generateQueryPlan(
            "问题", [_cls()], fake, _llmConfig(), maxRetries=1, maxTokens=2048,
        )

        assert len(fake.calls) == 1
        assert fake.calls[0]["maxTokens"] == 2048