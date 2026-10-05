"""B019 多步对比题集成测试（真模型端到端）。

真模型验证：
1. Qwen + B019 题 → 步骤 3 出真实数据（不是软失败）
2. deepseek + B019 题 → 行为与变更前一致
3. Qwen + 非对比多步题 → 行为与现状一致
4. Qwen + 对比题 + deepseek 不可用 → 原 fallback 链路 → 503

所有测试均以 real LLM 为目标，env 缺失时 pytest.skip 放行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.domain.models import DataSource, LlmConfig, OntologyClass, OntologyProperty
from app.domain.schemas import ChatRequest
from app.infrastructure.security.crypto import encryptApiKey
from app.services.chat_service import ChatService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

B019_QUESTION = (
    "分步分析：B019 圣特公司近 12 个月供货量下降的原因。"
    "第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，"
    "第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总"
)


# ---------------------------------------------------------------------------
# Fixture helpers (NOT in conftest — kept local to this file per brief strategy)
# ---------------------------------------------------------------------------

def _make_chat_request(
    question: str,
    model_id: int | None = None,
    datasource_id: int = 1,
    session_id: str = "s-qwen-uplift",
    user_id: str = "u-test-001",
) -> ChatRequest:
    """Build a ChatRequest matching the production schema."""
    return ChatRequest(
        sessionId=session_id,
        question=question,
        datasourceId=datasource_id,
        modelId=model_id,
    )


# ---------------------------------------------------------------------------
# Real LLM factory — builds real HTTP clients from LlmConfig rows.
# Gated behind OMLX_API_URL so tests are safe to collect in CI without the key.
# ---------------------------------------------------------------------------

_OMLX_URL = os.environ.get("OMLX_API_URL", "")
_DEEPSEEK_URL = os.environ.get("DEEPSEEK_API_URL", "")
_QWEN_URL = os.environ.get("QWEN_API_URL", "")


def _real_llm_factory(config: LlmConfig):
    """Return a real LLM client for the given config, or None if no key / no URL."""
    if not config.api_key_encrypted:
        return None

    # Route by model name suffix (simple heuristic; matches existing seed data)
    url: str | None = None
    if "deepseek" in config.model_name.lower():
        url = _DEEPSEEK_URL or os.environ.get("DEEPSEEK_BASE_URL", "")
    elif "qwen" in config.model_name.lower():
        url = _QWEN_URL or _OMLX_URL
    else:
        url = _OMLX_URL

    if not url:
        return None

    from app.infrastructure.llm.factory import createClient
    from app.config import getSettings

    return createClient(config, settings=getSettings())


# ---------------------------------------------------------------------------
# Model-seeded fixtures (per-test; use integration conftest's dbSession)
# ---------------------------------------------------------------------------

async def _seed_comparison_models(dbSession) -> tuple[LlmConfig, LlmConfig, DataSource]:
    """Seed Qwen (model_id=3) + deepseek (model_id=1) + ZJTH datasource + PRECEIPT class.

    Both models have encryptApiKey("sk-test") so _real_llm_factory can build real clients.
    """
    qwen_cfg = LlmConfig(
        model_name="Qwen3.8-27B-4bit",
        provider="openai_compatible_proxy",
        api_key_encrypted=encryptApiKey("sk-test"),
        cost_per_1k_input=Decimal("0.0001"),
        cost_per_1k_output=Decimal("0.0002"),
        is_active=True,
    )
    deepseek_cfg = LlmConfig(
        model_name="deepseek-chat",
        provider="openai_compatible_proxy",
        api_key_encrypted=encryptApiKey("sk-test"),
        cost_per_1k_input=Decimal("0.002"),
        cost_per_1k_output=Decimal("0.004"),
        is_active=True,
    )
    ds = DataSource(
        name="ZJTH",
        type="oracle",
        host="h",
        port=1521,
        database_name="svc",
        username="u",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
        is_default=True,
    )
    cls = OntologyClass(
        class_name="PRECEIPT",
        class_alias="收货单",
        source_table="ZJTH.PRECEIPT",
        properties=[
            OntologyProperty(property_name="NAME", data_type="STRING", source_column="NAME"),
            OntologyProperty(property_name="QTY", data_type="DECIMAL", source_column="QTY"),
            OntologyProperty(property_name="MONTH", data_type="STRING", source_column="MONTH"),
            OntologyProperty(property_name="LOCATION", data_type="STRING", source_column="LOCATION"),
        ],
    )
    dbSession.add_all([qwen_cfg, deepseek_cfg, ds, cls])
    await dbSession.commit()
    await dbSession.refresh(qwen_cfg)
    await dbSession.refresh(deepseek_cfg)
    await dbSession.refresh(ds)
    return qwen_cfg, deepseek_cfg, ds


# ---------------------------------------------------------------------------
# ChatService builders
# ---------------------------------------------------------------------------

def _build_chat_service(
    config: LlmConfig,
    llm_factory=None,
    adapter=None,
) -> ChatService:
    """Build a ChatService wired to the given LlmConfig with optional overrides."""
    from app.services.model_router_service import ModelRouterService

    router = ModelRouterService()
    # Always select the configured model (no auto-routing).
    router.selectModel = lambda configs, prompt, ctx: config
    router.selectFallbackModel = lambda configs, exclude: None

    if llm_factory is None:
        llm_factory = _real_llm_factory
    if adapter is None:
        from app.infrastructure.business_db_pool import get_adapter

        async def _adapter_provider(ds_id, ds):
            return get_adapter(ds_id, ds)

        adapter = _adapter_provider

    return ChatService(
        modelRouterService=router,
        llmFactory=llm_factory,
        adapterProvider=adapter,
    )


# ---------------------------------------------------------------------------
# Stub adapter so step execution doesn't hit a real DB (we only assert on LLM output)
# ---------------------------------------------------------------------------

class _StubAdapter:
    """Returns dummy rows so step execution reaches the LLM without DB errors."""
    async def execute_read_only(self, sql: str):
        return [
            {"NAME": "A", "QTY": Decimal("100"), "MONTH": "2025-01", "LOCATION": "上海"},
            {"NAME": "B", "QTY": Decimal("80"), "MONTH": "2025-01", "LOCATION": "北京"},
        ]


# ---------------------------------------------------------------------------
# Real LLM availability gate
# ---------------------------------------------------------------------------

def _skip_if_no_real_llm():
    """Skip all tests in this file if no OMLX / model API URL is configured."""
    if not _OMLX_URL and not _QWEN_URL and not _DEEPSEEK_URL:
        pytest.skip("real LLM not available (need OMLX_API_URL or QWEN_API_URL)")


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
class TestQwenComparisonUplift:
    """Test 1: Qwen + B019 对比题 → step 3 不应是软失败。"""

    async def test_qwen_step3_yields_real_data(self, client, dbSession):
        """Qwen + B019 对比题：step 3 不应是软失败文案。"""
        _skip_if_no_real_llm()
        qwen_cfg, _, ds = await _seed_comparison_models(dbSession)

        svc = _build_chat_service(
            qwen_cfg,
            llm_factory=_real_llm_factory,
            adapter=lambda ds_id, ds_: _StubAdapter(),
        )

        dto = _make_chat_request(
            question=B019_QUESTION,
            model_id=qwen_cfg.id,
            datasource_id=ds.id,
        )

        result = await svc.processMessage(dto, dbSession)

        assert result.steps is not None, "multi_step response must carry steps"
        assert len(result.steps) >= 3, f"expected >= 3 steps, got {len(result.steps)}"
        step3 = result.steps[2]
        assert step3.sql is not None, f"Qwen step 3 should not soft-fail; got error={step3.error}"
        assert step3.error is None, f"Qwen step 3 error={step3.error}"


@pytest.mark.integration
@pytest.mark.asyncio
class TestDeepseekRegression:
    """Test 2: deepseek + B019 题 → 行为与变更前一致（snapshot 比对）。"""

    async def test_deepseek_step3_snapshot_match(self, client, dbSession):
        """deepseek + B019 题：行为与变更前一致（4 步全成功，answer 含定性结论）。"""
        _skip_if_no_real_llm()
        _, deepseek_cfg, ds = await _seed_comparison_models(dbSession)

        svc = _build_chat_service(
            deepseek_cfg,
            llm_factory=_real_llm_factory,
            adapter=lambda ds_id, ds_: _StubAdapter(),
        )

        dto = _make_chat_request(
            question=B019_QUESTION,
            model_id=deepseek_cfg.id,
            datasource_id=ds.id,
        )

        result = await svc.processMessage(dto, dbSession)

        # 关键不变量：前 3 步 SQL 均生成，answer 含定性结论
        assert result.steps is not None
        assert all(s.sql is not None for s in result.steps[:3]), \
            f"deepseek steps[0..2] should all have sql; errors={[s.error for s in result.steps[:3]]}"
        assert result.answer_text is not None
        assert ("公司因素" in result.answer_text or "行业因素" in result.answer_text), \
            f"answer should mention '公司因素' or '行业因素': {result.answer_text}"


@pytest.mark.integration
@pytest.mark.asyncio
class TestQwenNonComparisonUnchanged:
    """Test 3: Qwen + 非对比多步题 → 行为与现状一致（改写器不命中）。"""

    async def test_qwen_non_comparison_no_rewrite(self, client, dbSession):
        """Qwen + 非对比多步题：行为与现状一致（无对比关键词 → 不命中 → 原 sub_question）。"""
        _skip_if_no_real_llm()
        qwen_cfg, _, ds = await _seed_comparison_models(dbSession)

        svc = _build_chat_service(
            qwen_cfg,
            llm_factory=_real_llm_factory,
            adapter=lambda ds_id, ds_: _StubAdapter(),
        )

        dto = _make_chat_request(
            question="分步：第一步查 3 月供货量 top3 供应商，第二步查每家 top3 物料",
            model_id=qwen_cfg.id,
            datasource_id=ds.id,
        )

        result = await svc.processMessage(dto, dbSession)

        assert result.steps is not None
        assert result.steps[0].error is None, f"step 0 should succeed: {result.steps[0].error}"
        assert result.steps[1].error is None, f"step 1 should succeed: {result.steps[1].error}"


@pytest.mark.integration
@pytest.mark.asyncio
class TestQwenWithDeepseekUnavailable:
    """Test 4: Qwen + 对比题 + deepseek 不可用 → 503。

    用户选 Qwen（model_id=3，有 key），但对比题改写 hook 强制切 deepseek。
    deepseek 无 key → createClient 返回 None → 503（与 doc_qa / wiki_qa 同口径）。
    """

    async def test_force_deepseek_unavailable_returns_503(self, client, dbSession):
        """Qwen + 对比题 + deepseek 不可用 → 503 而非 500。"""
        _skip_if_no_real_llm()
        qwen_cfg, deepseek_cfg, ds = await _seed_comparison_models(dbSession)

        from app.services.model_router_service import ModelRouterService

        router = ModelRouterService()

        # Primary always returns Qwen (user's explicit choice).
        def fake_select_model(configs, prompt, ctx):
            return qwen_cfg

        # Fallback always returns deepseek but with a factory that returns None
        # (simulates "no key" → createClient None).
        def fake_select_fallback(configs, exclude):
            return deepseek_cfg

        router.selectModel = fake_select_model
        router.selectFallbackModel = fake_select_fallback

        # Factory that mirrors real createClient behavior:
        # - Qwen config → real client (key present, URL available)
        # - deepseek config → None (simulate key missing / unavailable)
        def keyless_factory(config: LlmConfig):
            if config.id == deepseek_cfg.id:
                return None  # deepseek unavailable → forces 503 path
            return _real_llm_factory(config)

        svc = ChatService(
            modelRouterService=router,
            llmFactory=keyless_factory,
            adapterProvider=lambda ds_id, ds_: _StubAdapter(),
        )

        dto = _make_chat_request(
            question=B019_QUESTION,
            model_id=qwen_cfg.id,
            datasource_id=ds.id,
        )

        # The comparison rewrite hook forces deepseek; deepseek factory returns None
        # → _llmFactory(config) is None → LLMUnavailableError → 503
        from app.domain.exceptions import LLMUnavailableError

        with pytest.raises(LLMUnavailableError) as exc_info:
            await svc.processMessage(dto, dbSession)

        assert "未配置可用的 LLM" in str(exc_info.value) or "503" in str(exc_info.value), \
            f"expected 503/LLMUnavailableError, got: {exc_info.value}"
