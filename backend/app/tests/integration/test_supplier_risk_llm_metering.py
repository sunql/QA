"""供应商风险点 LLM：单价必须来自 model config（USD），且客户端要真能构造出来。

H9 有两处同源缺陷，都在 ``SupplierRiskService._generateRiskPoints``：

1. **成本硬编码 CNY**。该方法按「输入 0.001 / 输出 0.002 CNY per 1k token」定价，
   而全系统其余路径（chat 各阶段 / doc_qa / wiki_qa / L4）都用
   ``llm_config.cost_per_1k_input/output``（USD）。同一张 session_token_usage
   台账里混着两种货币，会话成本报表与模型路由的预算降级判断都在拿人名币数字当美元用。

2. **``llm_factory(None)``——生产上 LLM 路径从未跑过**。生产工厂就是 ``createClient``，
   而本项目没有 ``openaiApiKey`` 环境变量 ⇒ ``createClient(None)`` 返回 None ⇒
   紧接着 ``await llm.complete(...)`` 抛 AttributeError ⇒ 被 ``except Exception``
   吞掉 ⇒ 风险点**永远**是 ``fallback_template``。既有测试用「忽略入参、无论如何都
   返回客户端」的假工厂（如 test_chat_agent_run 的 ``lambda cfg: _NoopLlm()``），
   正好把这个洞照原样盖住了。

本文件走完整 API 链路（POST /api/v1/chat），并且用**诚实假工厂**——cfg 为 None 时
返回 None，与 ``createClient`` 同语义。旧代码在它面前会当场掉进 fallback_template，
所以 ``riskPointsSource == "llm"`` 这条断言本身就能钉住缺陷 2。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig, SessionTokenUsage
from app.tests.integration.test_chat_agent_run import (
    _CHAT_DATASOURCE_ID,
    _FakeAdapter,
    _RouterForConfig,
    _seedDatasource,
    _seedFeatureAndValue,
    _seedSupplier,
    _StubEmbedding,
)

AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}

_DATASOURCE_ID = _CHAT_DATASOURCE_ID
_FEATURE_ID = 2
_SESSION_ID = "sess-supplier-risk-cost"
QUESTION = "供应商 100001 的风险等级"

# 假客户端固定用量
_PROMPT_TOKENS = 100
_COMPLETION_TOKENS = 50
# 故意与硬编码的 0.001/0.002 拉开距离（config 单价 ×30），让「用错单价」必红
_RATE_IN = Decimal("0.03")
_RATE_OUT = Decimal("0.06")
_EXPECTED_COST = (_PROMPT_TOKENS * _RATE_IN + _COMPLETION_TOKENS * _RATE_OUT) / 1000  # 0.006
_HARDCODED_CNY_COST = round(
    _PROMPT_TOKENS / 1000.0 * 0.001 + _COMPLETION_TOKENS / 1000.0 * 0.002, 6
)  # 0.0002


class _RiskLlm:
    """只对风险点提示词作答的假客户端（其余 prompt 回空串，避免污染意图判定）。"""

    @staticmethod
    def _content(msg) -> str:
        if isinstance(msg, dict):
            return msg.get("content", "")
        return getattr(msg, "content", "")

    async def complete(self, messages, **kwargs):  # noqa: ARG002
        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = _PROMPT_TOKENS
            completionTokens = _COMPLETION_TOKENS

        if "采购域风险评估助理" in self._content(messages[0]):
            _Resp.content = "该供应商风险评分触阈值，建议复核来料检验记录。"
        return _Resp()


class _HonestFactory:
    """与 createClient 同语义的假工厂：cfg 为 None → None（无配置无从构造客户端）。"""

    def __init__(self, *, alwaysNone: bool = False) -> None:
        self._alwaysNone = alwaysNone
        self.configs: list = []

    def __call__(self, cfg):
        self.configs.append(cfg)
        if cfg is None or self._alwaysNone:
            return None
        return _RiskLlm()


async def _seed(session: AsyncSession) -> LlmConfig:
    # datasource 必须与 _seedFeatureAndValue 内硬编码的 id 一致（feature_definition
    # 有 FK 指向 data_source），故复用同一 helper 而非自建。
    await _seedDatasource(session)
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        cost_per_1k_input=_RATE_IN,
        cost_per_1k_output=_RATE_OUT,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    await _seedSupplier(session, 100001, "SUP000001")
    await _seedFeatureAndValue(
        session, _FEATURE_ID, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50,
        unit="score", window="12M",
    )
    return config


def _installFakes(monkeypatch, config: LlmConfig, factory: _HonestFactory) -> None:
    import app.api.v1.chat as chatModule

    monkeypatch.setattr(chatModule._service, "_modelRouter", _RouterForConfig(config))
    monkeypatch.setattr(chatModule._service, "_llmFactory", factory)
    monkeypatch.setattr(chatModule._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chatModule._service, "_embedding", _StubEmbedding())


async def _ask(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": _SESSION_ID,
            "question": QUESTION,
            "datasourceId": _DATASOURCE_ID,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_risk", body
    assert body["supplierRisk"] is not None, body
    return body


@pytest.mark.asyncio
async def test_supplier_risk_points_use_config_pricing(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """H9：风险点走 LLM（客户端确实构造出来了）+ 成本按 config 单价（USD）计。"""
    config = await _seed(dbSession)
    factory = _HonestFactory()
    _installFakes(monkeypatch, config, factory)

    body = await _ask(client)
    risk = body["supplierRisk"]

    # 缺陷 2：诚实的假工厂（cfg=None → None）下仍能拿到 LLM 结果 ⇒ 说明调用方
    # 确实解析出了真实 config 并传给了工厂，而不是把 None 当占位符塞进去。
    assert risk["riskPointsSource"] == "llm", risk["riskPointsSource"]
    assert all(cfg is not None for cfg in factory.configs), factory.configs

    # 缺陷 1：单价来自 model config
    assert risk["tokensUsed"] == _PROMPT_TOKENS + _COMPLETION_TOKENS
    assert risk["cost"] == pytest.approx(float(_EXPECTED_COST))
    assert risk["cost"] != pytest.approx(_HARDCODED_CNY_COST)

    # 台账同口径：ledger 里的 cost 也是 USD
    row = (
        await dbSession.execute(
            select(SessionTokenUsage).where(
                SessionTokenUsage.session_id == _SESSION_ID
            )
        )
    ).scalar_one()
    assert row.purpose == "supplier_risk"
    assert row.prompt_tokens == _PROMPT_TOKENS
    assert row.completion_tokens == _COMPLETION_TOKENS
    assert row.cost == _EXPECTED_COST


@pytest.mark.asyncio
async def test_supplier_risk_no_client_degrades_without_fake_ledger_row(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """边界：工厂给不出客户端（无 key）→ 明确降级模板，且不写 0/0 假台账。

    ``createClient`` 返回 None 是「无可用 key」的 SSOT。这条路径此前是靠
    AttributeError 兜进来的（隐式、无日志、且真的花了 0 token 却记账 0.0 元），
    现在是显式分支：tokensUsed=0、模板文案、台账不留行。
    """
    config = await _seed(dbSession)
    factory = _HonestFactory(alwaysNone=True)
    _installFakes(monkeypatch, config, factory)

    risk = (await _ask(client))["supplierRisk"]
    assert risk["riskPointsSource"] == "fallback_template"
    assert risk["tokensUsed"] == 0
    assert risk["cost"] == 0.0
    # 降级必须是「解析出配置了、但客户端构造不出来」这条显式分支，而不是
    # 「把 None 当占位符塞给工厂」——后者是缺陷 2 本身。工厂收到的 config 非空即证明。
    assert factory.configs, "工厂根本没被调用 ⇒ 走的是缺配置分支，不是无 key 分支"
    assert all(cfg is not None for cfg in factory.configs), factory.configs

    rows = list(
        (
            await dbSession.execute(
                select(SessionTokenUsage).where(
                    SessionTokenUsage.session_id == _SESSION_ID
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == [], f"零 LLM 调用不得留台账行，实际 {len(rows)}"
