"""nl2sql 两阶段生成循环的「同模型瞬态重试」接线单测（M4）。

判定/退避策略本身在 `test_llm_retry_policy.py`；本文件只测**接线**：
`generateQueryPlan` / `generateSql` 的 `for attempt` 循环里
- 首轮（`attempt == 0`）遇到可重试错误 → 同一模型上多试一次；
- 非首轮 / 永久错误（401 等）→ 立即上抛，不额外花钱；
- 逃逸异常必须带上本轮已累加的用量（否则「已测得」的 token 凭空消失，是计量盲区）；
- 重试不写入 `errors`（否则重试噪声被注入后续 prompt）。

假客户端按脚本逐次响应，退避等待置零（真实 1s~4s 会拖长单测）。
"""

from __future__ import annotations

import json

import pytest
from tenacity import wait_none

from app.domain.exceptions import LlmClientError
from app.domain.models import OntologyClass
from app.domain.query_plan import Aggregation, QueryPlan
from app.services import llm_retry_policy as policy
from app.services.llm_retry_policy import consumedTokens
from app.services.nl2sql_service import Nl2SqlService


class _Resp:
    def __init__(self, content: str, promptTokens: int = 10, completionTokens: int = 5) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.isApproximateUsage = False


class _ScriptedLlm:
    """按脚本逐次响应：元素是 _Resp 则返回，是异常则抛出；记录每次调用的 kwargs。"""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append({"messages": messages, **kwargs})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, _Resp)
        return item

    def userPrompts(self) -> list[str]:
        """每次调用的 user prompt 文本（messages[1].content）。"""
        return [c["messages"][1].content for c in self.calls]


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


_UNPARSEABLE = "这轮回复不是 JSON，解析不出计划"
_VALID_SQL = "```sql\nSELECT 1 FROM T_PRECEIPT\n```"


def _retryable() -> LlmClientError:
    """可重试错误（无 __cause__.status_code ⇒ 默认可重试），文案含 503 便于断言不回流。"""
    return LlmClientError("LLM 调用失败", provider="qwen", detail="503 Service Unavailable")


def _permanent() -> LlmClientError:
    """永久错误：__cause__ 携带 401 ⇒ 不可重试。"""
    cause = Exception("unauthorized")
    cause.status_code = 401  # type: ignore[attr-defined]
    exc = LlmClientError("LLM 调用失败", provider="qwen", detail="401 Unauthorized")
    exc.__cause__ = cause
    return exc


@pytest.fixture(autouse=True)
def noBackoffWait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "RETRY_WAIT_EXPONENTIAL", wait_none())


class TestGenerateQueryPlanTransientRetry:
    """计划阶段循环的瞬态重试接线。"""

    async def test_first_attempt_transient_error_retries_same_model(self) -> None:
        """maxRetries=0（仅 1 轮）也能拿到 1 次额外尝试 ⇒ 总 2 次调用。"""
        fake = _ScriptedLlm([_retryable(), _Resp(_validPlanJson(), 3000, 500)])
        service = Nl2SqlService()
        result = await service.generateQueryPlan(
            "问题", [_cls()], fake, _llmConfig(), maxRetries=0,
        )
        assert result.plan is not None
        assert len(fake.calls) == 2
        # 重试用的是同一模型、同一 prompt（不换模型、不改输入）
        assert fake.calls[0]["model"] == fake.calls[1]["model"]
        assert fake.calls[0]["messages"] == fake.calls[1]["messages"]
        # 用量只来自真正返回的那次
        assert (result.promptTokens, result.completionTokens) == (3000, 500)

    async def test_second_attempt_transient_error_is_not_retried(self) -> None:
        """非首轮不再额外尝试（预算闸门）：第 2 轮抛错直接上抛。"""
        fake = _ScriptedLlm([
            _Resp(_UNPARSEABLE, 3000, 500),
            _retryable(),
        ])
        service = Nl2SqlService()
        with pytest.raises(LlmClientError):
            await service.generateQueryPlan("问题", [_cls()], fake, _llmConfig(), maxRetries=1)
        assert len(fake.calls) == 2

    async def test_permanent_error_is_not_retried(self) -> None:
        fake = _ScriptedLlm([_permanent()])
        service = Nl2SqlService()
        with pytest.raises(LlmClientError):
            await service.generateQueryPlan("问题", [_cls()], fake, _llmConfig(), maxRetries=2)
        assert len(fake.calls) == 1

    async def test_escaping_error_carries_accumulated_tokens(self) -> None:
        """M4 反例：第 1 轮测得 3000/500 已累加，第 2 轮抛出 ⇒ 异常上必须是 3000/500。

        修复前这里逃出的是裸 LlmClientError，`1694-1698` 的
        `Nl2SqlError(tokens=...)` 根本到不了 ⇒ 3500 token 静默消失。
        """
        fake = _ScriptedLlm([_Resp(_UNPARSEABLE, 3000, 500), _retryable()])
        service = Nl2SqlService()
        with pytest.raises(LlmClientError) as err:
            await service.generateQueryPlan("问题", [_cls()], fake, _llmConfig(), maxRetries=1)
        assert consumedTokens(err.value) == (3000, 500)

    async def test_worst_case_budget_is_retries_plus_one_extra(self) -> None:
        """最坏调用数 = (maxRetries+1) + 1：maxRetries=1 ⇒ 3 次，不是每轮翻倍。

        额外的那一次只花在首轮（`allowRetry = attempt == 0`）：首轮用掉 2 次
        （失败 + 重试），第 2 轮只 1 次即上抛。
        """
        fake = _ScriptedLlm([
            _retryable(),                 # 第 1 轮：瞬态故障
            _Resp(_UNPARSEABLE),          # 第 1 轮重试：解析失败（预算用尽）
            _retryable(),                 # 第 2 轮：非首轮 → 不再重试，立即上抛
        ])
        service = Nl2SqlService()
        with pytest.raises(LlmClientError):
            await service.generateQueryPlan("问题", [_cls()], fake, _llmConfig(), maxRetries=1)
        assert len(fake.calls) == 3

    async def test_retry_does_not_pollute_retry_feedback(self) -> None:
        """重试不写入 errors：后续轮次的 prompt 里不得出现瞬态错误文本。"""
        fake = _ScriptedLlm([
            _retryable(),
            _Resp(_UNPARSEABLE),
            _Resp(_validPlanJson()),
        ])
        service = Nl2SqlService()
        result = await service.generateQueryPlan(
            "问题", [_cls()], fake, _llmConfig(), maxRetries=2,
        )
        assert result.plan is not None
        assert len(fake.calls) == 3
        assert all("503" not in p for p in fake.userPrompts()), fake.userPrompts()
        # 解析失败的反馈仍须照常注入（重试只影响用量/不再额外调用，不吞掉既有反馈）
        assert "解析出查询计划" in fake.userPrompts()[2]

    async def test_retry_exhausted_raises_nl2sql_error_with_totals(self) -> None:
        """既有契约不变：所有轮次都解析失败 → Nl2SqlError 携带累计用量。"""
        from app.domain.exceptions import Nl2SqlError

        fake = _ScriptedLlm([_Resp(_UNPARSEABLE, 10, 5), _Resp(_UNPARSEABLE, 20, 7)])
        service = Nl2SqlService()
        with pytest.raises(Nl2SqlError) as err:
            await service.generateQueryPlan("问题", [_cls()], fake, _llmConfig(), maxRetries=1)
        assert err.value.tokens == (30, 12)


class TestGenerateSqlTransientRetry:
    """SQL 阶段循环的瞬态重试接线。"""

    async def test_first_attempt_transient_error_retries_same_model(self) -> None:
        fake = _ScriptedLlm([_retryable(), _Resp(_VALID_SQL, 3000, 500)])
        service = Nl2SqlService()
        result = await service.generateSql(
            "问题", [_cls()], fake, _llmConfig(), maxRetries=0,
        )
        assert "SELECT 1 FROM T_PRECEIPT" in result.sql
        assert len(fake.calls) == 2
        assert (result.promptTokens, result.completionTokens) == (3000, 500)

    async def test_escaping_error_carries_accumulated_tokens(self) -> None:
        fake = _ScriptedLlm([_Resp(_UNPARSEABLE, 3000, 500), _retryable()])
        service = Nl2SqlService()
        with pytest.raises(LlmClientError) as err:
            await service.generateSql("问题", [_cls()], fake, _llmConfig(), maxRetries=1)
        assert consumedTokens(err.value) == (3000, 500)

    async def test_permanent_error_is_not_retried(self) -> None:
        fake = _ScriptedLlm([_permanent()])
        service = Nl2SqlService()
        with pytest.raises(LlmClientError):
            await service.generateSql("问题", [_cls()], fake, _llmConfig(), maxRetries=1)
        assert len(fake.calls) == 1

    async def test_truncation_backoff_budget_not_disturbed_by_inner_retry(self) -> None:
        """截断扩容（maxTokens 翻倍）在每轮调用前算一次，内层重试不改它。"""
        from app.services.nl2sql_service import _NL2SQL_MAX_TOKENS_DEFAULT

        truncated = _Resp(_VALID_SQL, 10, _NL2SQL_MAX_TOKENS_DEFAULT)
        fake = _ScriptedLlm([_retryable(), truncated, _Resp(_VALID_SQL, 10, 5)])
        service = Nl2SqlService()
        result = await service.generateSql(
            "问题", [_cls()], fake, _llmConfig(), maxRetries=2,
        )
        assert result.sql
        # 第 1 轮的两次调用（原始 + 内层重试）共用同一轮预算
        assert fake.calls[0]["maxTokens"] == _NL2SQL_MAX_TOKENS_DEFAULT
        assert fake.calls[1]["maxTokens"] == _NL2SQL_MAX_TOKENS_DEFAULT
        # 截断命中后翻倍只影响后续轮次
        assert fake.calls[2]["maxTokens"] == _NL2SQL_MAX_TOKENS_DEFAULT * 2
