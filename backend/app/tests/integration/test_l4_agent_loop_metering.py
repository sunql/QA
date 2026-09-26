"""L4 Agent Loop 计量（H2）：每次 complete_with_tools 都要落 session_token_usage。

L4 是全项目唯一「直调 LLM 但不写台账」的路径：``run_agent_loop`` 每轮调
``complete_with_tools`` 后，token 数没有任何字段可承载（``AgentLoopResult`` 只有
一个 USD 数字），而那个数字来自**硬编码 gpt-4o-mini 价格**——与本项目 model
config 的单价无关。后果有两层：

1. 违反核心约束 #3「每次 LLM 调用必须记录 Token 消耗与成本」——L4 花掉的钱完全
   不进台账，会话成本报表与模型路由的预算降级判断都漏掉这部分。
2. 同一个 AgentLoopResult 的 total_cost_usd 被写进 ``session_message.token_cost_usd``
   监控埋点，用错误单价（H9 同类：成本口径不一致），监控面板上的 L4 成本系统性失真。

本文件走完整 API 链路（POST /api/v1/chat + 真实 PG）验证修复。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.domain.models import SessionMessage, SessionTokenUsage
from app.models.system_config import SystemConfig
from app.tests.integration.test_chat_api import (
    _FakeAdapter,
    _PipelineLlm,
    _RouterFor,
    _StubEmbeddingService,
    _seed,
)

SESSION_ID = "l4-sess-metering"

# 假客户端固定用量：与 _seed 的 config 单价 0.001(in) / 0.002(out) 组合出
# cost = 40*0.001/1000 + 10*0.002/1000 = 0.00006 USD
_PROMPT_TOKENS = 40
_COMPLETION_TOKENS = 10
_EXPECTED_COST = Decimal("0.00006")
# 硬编码 gpt-4o-mini 价格（$0.15/1M in + $0.6/1M out）会算出 0.000012，
# 与正确值差 5 倍 —— 断言因此有区分度，不会「改不改都绿」。
_HARDCODED_GPT4O_MINI_COST = 40 * 0.15 / 1_000_000 + 10 * 0.6 / 1_000_000

_QUESTION = "为什么各供应商的收货数量不同？"


class _L4Llm:
    """L4 假客户端：一轮文字作答（无 tool_calls → answered 分支）。"""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def complete_with_tools(self, *, messages, tools, tool_choice="auto"):
        from app.infrastructure.llm.base_client import LlmResponseWithTools

        self.calls.append(messages)
        return LlmResponseWithTools(
            content="因为各供应商的交付批次与质量等级不同。",
            tool_calls=[],
            usage={
                "prompt_tokens": _PROMPT_TOKENS,
                "completion_tokens": _COMPLETION_TOKENS,
            },
            model="test-model",
        )


class _FlakyL4Llm(_PipelineLlm):
    """L4 第 1 轮花钱并请求执行一次只读 SQL，第 2 轮抛错（模拟限流 / 超时）。

    降级后的 L2/L3 由父类 ``complete`` 正常作答，保证请求仍返回 200 —— 本用例要
    验证的是「已经花掉的 token 有没有落台账」，不是降级行为本身。
    """

    def __init__(self) -> None:
        super().__init__()
        self.tool_rounds = 0

    async def complete_with_tools(self, *, messages, tools, tool_choice="auto"):
        from app.domain.exceptions import LlmClientError
        from app.infrastructure.llm.base_client import LlmResponseWithTools, ToolCall

        self.tool_rounds += 1
        if self.tool_rounds > 1:
            raise LlmClientError("模拟第二轮限流")
        return LlmResponseWithTools(
            content="",
            tool_calls=[ToolCall(id="c1", name="execute_sql", args={"sql": "SELECT 1"})],
            usage={
                "prompt_tokens": _PROMPT_TOKENS,
                "completion_tokens": _COMPLETION_TOKENS,
            },
            model="test-model",
        )


async def _seedL4(session) -> tuple:
    """种子：model config + 数据源 + 本体类 + ENABLE_L4_AGENT_LOOP=true。"""
    config, ds = await _seed(session)
    session.add(
        SystemConfig(
            key="ENABLE_L4_AGENT_LOOP", value="true", description="集成测试开启 L4"
        )
    )
    await session.commit()
    return config, ds


def _installL4Fakes(monkeypatch, config, llm) -> None:
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: llm)
    monkeypatch.setattr(
        chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter()
    )
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


async def _runL4Chat(client, datasourceId: int, llm: _L4Llm) -> dict:
    resp = await client.post(
        "/api/v1/chat",
        json={
            "sessionId": SESSION_ID,
            "question": _QUESTION,
            "datasourceId": datasourceId,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 命中 L4 而非降级 L2/L3：answer 前缀是 AgentLoopResult.terminated_reason
    assert body["answer"].startswith("[L4:answered]"), body
    assert len(llm.calls) == 1, f"期望 1 轮 agent loop，实际 {len(llm.calls)}"
    return body


@pytest.mark.asyncio
async def test_l4_agent_loop_writes_token_ledger(client, dbSession, monkeypatch) -> None:
    """H2：L4 的 LLM 调用必须落 session_token_usage，且用真实 model config 单价。

    此前 token 数在 AgentLoopResult 里无处承载 ⇒ 台账零条记录，「L4 花掉的钱」
    在会话成本与预算降级判断里完全不存在。
    """
    config, ds = await _seedL4(dbSession)
    llm = _L4Llm()
    _installL4Fakes(monkeypatch, config, llm)

    await _runL4Chat(client, ds.id, llm)

    rows = list(
        (
            await dbSession.execute(
                select(SessionTokenUsage).where(
                    SessionTokenUsage.session_id == SESSION_ID
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, f"L4 必须落且仅落一条台账，实际 {len(rows)}"

    row = rows[0]
    assert row.purpose == "l4_agent_loop"
    # modelConfigId 必须透传：None 会让成本无法回溯到单价来源
    assert row.model_config_id == config.id
    assert row.model_name == "test-model"
    assert row.prompt_tokens == _PROMPT_TOKENS
    assert row.completion_tokens == _COMPLETION_TOKENS
    assert row.total_tokens == _PROMPT_TOKENS + _COMPLETION_TOKENS
    assert row.cost == _EXPECTED_COST


@pytest.mark.asyncio
async def test_l4_reported_cost_matches_config_pricing(client, dbSession, monkeypatch) -> None:
    """H2 附带：监控埋点 cost 必须来自 model config 单价，不是硬编码 gpt-4o-mini。

    total_cost_usd 会被写进 ``session_message.token_cost_usd``（Phase 5 监控管道）。
    旧实现硬编码 $0.15/1M in + $0.6/1M out，与本项目 config 单价无关，两套口径给出
    的数字差 5 倍，监控面板上的 L4 成本随模型切换而失真。
    """
    config, ds = await _seedL4(dbSession)
    llm = _L4Llm()
    _installL4Fakes(monkeypatch, config, llm)

    await _runL4Chat(client, ds.id, llm)

    asst = (
        await dbSession.execute(
            select(SessionMessage).where(
                SessionMessage.session_id == SESSION_ID,
                SessionMessage.role == "assistant",
            )
        )
    ).scalar_one()
    assert asst.routing_layer == "L4"
    assert asst.token_cost_usd == pytest.approx(float(_EXPECTED_COST))
    # 反向防线：硬编码单价算出的数字绝不能出现在这里
    assert asst.token_cost_usd != pytest.approx(_HARDCODED_GPT4O_MINI_COST)


@pytest.mark.asyncio
async def test_l4_mid_loop_failure_still_meters_spent_tokens(
    client, dbSession, monkeypatch
) -> None:
    """L4 中途失败（限流 / 超时）：**第 1 轮已经花掉的 token 必须仍然落台账**。

    异常从 ``run_agent_loop`` 冒出去 → ``_runL4AgentLoop`` 的 ``except`` 直接 return，
    而 ``_recordUsage`` 在那之后——整轮 L4 的花费随之凭空消失。这与 H2 是同一类
    缺陷（花钱不记账），只是发生在**失败路径**上：``AgentLoopResult`` 里那个
    「error」终止原因被文档和调用方 ``_maybeRunL4AgentLoop`` 检查着，却从来没有
    被真正产生过，异常直接穿透了。
    """
    config, ds = await _seedL4(dbSession)
    llm = _FlakyL4Llm()
    _installL4Fakes(monkeypatch, config, llm)

    resp = await client.post(
        "/api/v1/chat",
        json={
            "sessionId": SESSION_ID,
            "question": _QUESTION,
            "datasourceId": ds.id,
        },
    )
    assert resp.status_code == 200, resp.text
    # L4 失败 → 降级 L2/L3：回答不再带 [L4:<reason>] 前缀
    assert not resp.json()["answer"].startswith("[L4:"), resp.json()["answer"]

    rows = [
        r
        for r in (
            await dbSession.execute(
                select(SessionTokenUsage).where(
                    SessionTokenUsage.session_id == SESSION_ID
                )
            )
        )
        .scalars()
        .all()
        if r.purpose == "l4_agent_loop"
    ]
    assert len(rows) == 1, f"失败路径同样必须落台账，实际 {len(rows)} 行"
    assert rows[0].prompt_tokens == _PROMPT_TOKENS
    assert rows[0].completion_tokens == _COMPLETION_TOKENS
    assert rows[0].cost == _EXPECTED_COST
    assert rows[0].model_config_id == config.id
