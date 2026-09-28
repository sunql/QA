"""模型路由可用性 + 无 key 配置兜底的集成测试（真实 PG + 完整 API 链路）。

复现 2026-09-26 发现的两条互相放大的缺陷：

1. **超预算分支钉死无 key 配置**：`sessionCost >= SESSION_BUDGET` 时
   `selectModel` 直接 `_cheapest(active)`，本地 DB 里最便宜的是无 API key 的
   Qwen3.8-27B-4bit → `createClient` 返回 None → 裸 `AttributeError` 500。
   `/chat` 的调用方只要不带 modelId（脚本 / API 直调 / 前端未选模型）就会踩。
2. **chat 缺 None 兜底**：`doc_qa`（rag_qa_service.py:184）与 `wiki_qa`
   （wiki_qa_service.py:182）都有 `if client is None: raise LLMUnavailableError`
   → 503，chat 流水线漏了 → 失败形态从「明确错误」退化成「内部 500」。

测试用**真实** `ModelRouterService`（注入 `SESSION_BUDGET=0` 确定性触发超预算
分支）+ 按「配置有无 key」返回客户端/None 的假工厂（与 `createClient` 同判据），
避免依赖 tiktoken 与真实外部调用。
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.config import Settings
from app.domain.models import DataSource, LlmConfig, OntologyClass, OntologyProperty
from app.infrastructure.security.crypto import decryptApiKey, encryptApiKey
from app.services.model_router_service import ModelRouterService
from app.services.stream_events import EVENT_DONE, EVENT_ERROR, ErrorType
from app.tests.integration.test_chat_api import (
    _FakeAdapter,
    _PipelineLlm,
    _StubEmbeddingService,
    _chat_payload,
)

USABLE_MODEL = "deepseek-chat"
KEYLESS_MODEL = "Qwen3.8-27B-4bit"


async def _seedTwoModels(session) -> tuple[LlmConfig, LlmConfig, DataSource]:
    """写入「有 key 但更贵」+「无 key 但最便宜」两个配置、数据源与本体类。

    无 key 配置的最低价（cost_per_1k_input 更小）是复现的关键：`_cheapest`
    按输入单价取最小值，必然选中它。
    """
    usable = LlmConfig(
        model_name=USABLE_MODEL,
        provider="openai_compatible_proxy",
        api_key_encrypted=encryptApiKey("sk-test"),
        cost_per_1k_input=Decimal("0.002"),
        cost_per_1k_output=Decimal("0.004"),
        is_active=True,
    )
    keyless = LlmConfig(
        model_name=KEYLESS_MODEL,
        provider="openai_compatible_proxy",
        api_key_encrypted=None,
        cost_per_1k_input=Decimal("0.0001"),
        cost_per_1k_output=Decimal("0.0002"),
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
        ],
    )
    session.add_all([usable, keyless, ds, cls])
    await session.commit()
    await session.refresh(usable)
    await session.refresh(keyless)
    await session.refresh(ds)
    return usable, keyless, ds


def _factoryMirroringKeys(llm):
    """假 LLM 工厂：无 `api_key_encrypted` 的配置返回 None。

    判据与 `createClient` 一致（它还会看各 provider 的 env 回退，测试环境无
    对应 env，故此处只看密文 key 即可等价复现「无可用 key → None」契约）。
    """

    def _factory(config) -> object | None:
        return llm if config.api_key_encrypted else None

    return _factory


def _factoryMirroringDecrypt(llm):
    """假工厂：走**真实** `decryptApiKey`，把「密文非法」的失败原样暴露出来。

    与 `createClient` 的 `_resolveApiKey`（`factory.py:118`）同路径——真实
    `createClient` 在密文非法时抛 `ConfigError`（`crypto.py:51`），此处只把
    网络客户端的构造换成 stub，其余判据都走生产代码。
    """

    def _factory(config) -> object | None:
        if config.api_key_encrypted:
            decryptApiKey(config.api_key_encrypted)
            return llm
        return None

    return _factory


def _install(monkeypatch, llm, *, factory, affinityTurns: int = 0) -> None:
    """注入真实路由器（SESSION_BUDGET=0 → 必然走超预算降级分支）+ 假工厂。"""
    import app.api.v1.chat as chat_module

    router = ModelRouterService(
        settings=Settings(SESSION_BUDGET=0),  # Decimal("0") >= 0 → 超预算分支
        affinityTurns=affinityTurns,
    )
    monkeypatch.setattr(chat_module._service, "_modelRouter", router)
    monkeypatch.setattr(chat_module._service, "_llmFactory", factory(llm))
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


class TestOverBudgetRouting:
    """超预算降级必须落在「能真正调用」的配置上。"""

    async def test_over_budget_routes_around_keyless_config(
        self, client, dbSession, monkeypatch
    ) -> None:
        """超预算 → 排除无 key 的最便宜配置，用有 key 的可用模型回答（200）。

        修复前：`_cheapest(active)` 选中无 key 配置 → `createClient` 返回 None
        → 裸 AttributeError → 500。
        """
        usable, keyless, ds = await _seedTwoModels(dbSession)
        llm = _PipelineLlm()
        _install(monkeypatch, llm, factory=_factoryMirroringKeys)

        resp = await client.post("/api/v1/chat", json=_chat_payload("各供应商收货量", ds.id))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["answer"]
        # 实际服务模型是「有 key 且更贵」的那个，而非最便宜的无 key 配置
        assert body["modelName"] == USABLE_MODEL
        assert body["modelName"] != KEYLESS_MODEL
        assert llm.calls, "LLM 应被调用（无 key 配置若被选中则根本走不到这里）"


class TestUndecryptableConfigIsolation:
    """单个配置的密钥解密失败，不得击穿整条自动路由。"""

    async def test_corrupt_ciphertext_config_is_skipped_not_fatal(
        self, client, dbSession, monkeypatch
    ) -> None:
        """DB 里存在一条密文损坏的配置 → 跳过它并正常用健康模型回答（200）。

        回归背景：`_usableModelConfigs` 为筛选可用池，会对**每一个**配置调用
        `_llmFactory`（= `createClient`），而 `createClient` → `_resolveApiKey`
        → `decryptApiKey` 对非法密文**抛 `ConfigError`**。列表推导没有隔离，
        于是「从别的环境 restore 导致 FERNET key 不匹配」「手工改库」这类
        单行脏数据，会让所有走自动路由的请求失败——尽管其余配置完全健康。

        修复前该请求以 `ConfigError` 结束（非 200）；修复后脏配置按「不可用」
        筛掉，与它「无 key」时同等对待（记 warning）。
        """
        usable = LlmConfig(
            model_name=USABLE_MODEL,
            provider="openai_compatible_proxy",
            api_key_encrypted=encryptApiKey("sk-test"),
            cost_per_1k_input=Decimal("0.002"),
            cost_per_1k_output=Decimal("0.004"),
            is_active=True,
        )
        corrupt = LlmConfig(
            model_name="broken-model",
            provider="openai_compatible_proxy",
            api_key_encrypted="not-a-valid-fernet-token",  # 解密必抛 ConfigError
            cost_per_1k_input=Decimal("0.0001"),  # 最便宜 → 必然会被遍历到
            cost_per_1k_output=Decimal("0.0002"),
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
            ],
        )
        dbSession.add_all([usable, corrupt, ds, cls])
        await dbSession.commit()
        await dbSession.refresh(usable)
        await dbSession.refresh(ds)

        llm = _PipelineLlm()
        _install(monkeypatch, llm, factory=_factoryMirroringDecrypt)

        resp = await client.post("/api/v1/chat", json=_chat_payload("各供应商收货量", ds.id))

        assert resp.status_code == 200, resp.text
        assert resp.json()["modelName"] == USABLE_MODEL
        assert llm.calls, "健康配置应被调用"


class TestNoUsableLlmFallback:
    """所有配置都构造不出客户端时，必须给出明确错误而非内部 500。"""

    async def test_all_configs_keyless_returns_503(
        self, client, dbSession, monkeypatch
    ) -> None:
        """全部配置无 key → 503 LLMUnavailableError（与 doc_qa/wiki_qa 同口径）。"""
        _, _, ds = await _seedTwoModels(dbSession)
        llm = _PipelineLlm()
        _install(monkeypatch, llm, factory=lambda _llm: (lambda config: None))

        resp = await client.post("/api/v1/chat", json=_chat_payload("各供应商收货量", ds.id))

        assert resp.status_code == 503, resp.text
        assert "未配置可用的 LLM" in resp.json()["error"]
        assert not llm.calls

    async def test_all_configs_keyless_stream_emits_domain_error_event(
        self, client, dbSession, monkeypatch
    ) -> None:
        """流式：以 DOMAIN 类 error 事件结束，而非 INTERNAL 兜底。

        断言具体错误类型与文案——只看「最后是 error 事件」会被通用
        `except Exception`（MSG_INTERNAL_ERROR / errorType=internal）蒙过去，
        那样连接虽不断，但用户看到的是「服务内部错误」而非「未配置可用的 LLM」。
        """
        _, _, ds = await _seedTwoModels(dbSession)
        _install(monkeypatch, _PipelineLlm(), factory=lambda _llm: (lambda config: None))

        resp = await client.post(
            "/api/v1/chat/stream", json=_chat_payload("各供应商收货量", ds.id)
        )

        assert resp.status_code == 200, resp.text
        frames: list[tuple[str, dict]] = []
        for block in resp.text.split("\n\n"):
            if not block.strip():
                continue
            event: str | None = None
            data: dict = {}
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event = line[len("event: "):]
                elif line.startswith("data: "):
                    data = json.loads(line[len("data: "):])
            frames.append((event or "", data))

        assert frames, "SSE 应有事件"
        assert frames[-1][0] == EVENT_ERROR
        assert EVENT_DONE not in [e for e, _ in frames]
        error = frames[-1][1]
        assert "未配置可用的 LLM" in error["error"]
        assert error["errorType"] == ErrorType.DOMAIN.value

    async def test_explicitly_selected_keyless_model_returns_503(
        self, client, dbSession, monkeypatch
    ) -> None:
        """用户显式选中无 key 配置 → 503（而非 404「配置不存在或已禁用」）。

        路由候选池的过滤只作用于**自动路由**；显式 modelId 仍按全量配置查找，
        否则「配置存在但没有 key」会退化成误导性的「配置不存在」。
        """
        _, keyless, ds = await _seedTwoModels(dbSession)
        _install(monkeypatch, _PipelineLlm(), factory=lambda _llm: (lambda config: None))

        resp = await client.post(
            "/api/v1/chat",
            json={**_chat_payload("各供应商收货量", ds.id), "modelId": keyless.id},
        )

        assert resp.status_code == 503, resp.text
