"""F4 test — 验证 preamble 并发隔离（跨用户不串答案）。

bug: ChatService 是进程级单例，_runL1AndL4Preamble 把 L1/L4 结果存在
self._preamble_l1_response / self._preamble_l4_response 两个实例属性上。
普通（非流式）入口从 self（不是从返回值）读这两个属性，于是：
用户 A 的请求在 self 写了答案，用户 B 的请求并发进来覆盖了同一属性，
A 继续执行时从 self 读到 B 的答案。

修法：在 PreambleResult 上加 full_response 字段，承载完整 ChatResponse，
调用方从 preamble 局部变量取，不走 self。
"""

from __future__ import annotations

import asyncio
import uuid

from app.domain.models import DataSource, KpiCatalog, LlmConfig
from app.infrastructure.security.crypto import encryptApiKey


class _KpiLlm:
    """KPI-L1 专用 fake LLM：按 KPI code 返回可区分的回答。"""

    def __init__(self) -> None:
        self._answers: dict[str, str] = {}

    def set_answer(self, kpi_code: str, answer: str) -> None:
        self._answers[kpi_code] = answer

    async def complete(self, messages: list, **kwargs) -> object:
        user = messages[1].content if len(messages) > 1 else ""
        answer = "DEFAULT_KPI_ANSWER"
        for code, ans in self._answers.items():
            if code in user:
                answer = ans
                break

        class _Resp:
            content = answer
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        return _Resp()


class _MockKpiMatcher:
    """可动态切换 match 行为（供 monkeypatch.setattr）的 mock。"""

    def __init__(self, match_fn) -> None:
        self._match_fn = match_fn

    async def match(self, question: str):
        return await self._match_fn(question)


async def _seed_kpis(dbSession) -> tuple[LlmConfig, DataSource, KpiCatalog, KpiCatalog]:
    """写入 LLM 配置 / 数据源 / 两个不同的 KPI 类目。"""
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.002,
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
    kpi_a = KpiCatalog(
        kpi_code="KPI_TEST_A",
        kpi_name="Test KPI Alpha",
        formula="100",
        unit="个",
        datasource_id=ds.id,
    )
    kpi_b = KpiCatalog(
        kpi_code="KPI_TEST_B",
        kpi_name="Test KPI Beta",
        formula="200",
        unit="件",
        datasource_id=ds.id,
    )
    dbSession.add_all([config, ds, kpi_a, kpi_b])
    await dbSession.commit()
    await dbSession.refresh(config)
    await dbSession.refresh(ds)
    await dbSession.refresh(kpi_a)
    await dbSession.refresh(kpi_b)
    return config, ds, kpi_a, kpi_b


class _RouterFor:
    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:
        return self._config

    def selectFallbackModel(self, configs, excludeId) -> LlmConfig | None:
        return None


class _FakeAdapter:
    async def execute_read_only(self, sql: str) -> list[dict]:
        return [{"NAME": "X", "QTY": 42}]


class _StubEmbeddingService:
    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None

    async def searchSimilarQueries(self, question: str, **kwargs):
        return []


def _install_fakes(monkeypatch, config: LlmConfig, llm) -> None:
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: llm)
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


def _chat_payload(question: str, datasourceId: int = 1, sessionId: str = "s1") -> dict:
    return {"sessionId": sessionId, "question": question, "datasourceId": datasourceId}


class TestPreambleConcurrentIsolation:
    """L1 preamble 并发隔离测试。

    测试策略（正确的窗口）：

    真实缺陷窗口在 chat_service.py:
        preamble = await self._runL1AndL4Preamble(...)  # ← 写了 self._preamble_l1_response = l1_a
        ...
        await self._storeSessionMessages(...)           # ← ★ 正确的阻塞点（写之后、读之前）
        return self._preamble_l1_response               # ← 读在这里

    因此在 _storeSessionMessages 里 block A：
    1. A 进 _storeSessionMessages，通知 a_written（A 已写完 singleton），
       等待 proceed_a（等 B 完整跑完并覆写 singleton）
    2. B 等 a_written 后，完整跑一遍（覆写 singleton），返回 B 的答案，set proceed_a
    3. A 从 _storeSessionMessages 的 await 里出来，return self._preamble_l1_response
       ⇒ 读到 B 的值（Beta）

    修复前（bug 存在）：A 读到 B 的值 => 测试红。
    修复后（full_response 走返回值）：A 的 preamble.full_response 不受 B 影响 => 测试绿。
    """

    async def test_l1_concurrent_requests_do_not_cross_contaminate(
        self, client, dbSession, monkeypatch
    ) -> None:
        import app.api.v1.chat as chat_module

        config, ds, kpi_a, kpi_b = await _seed_kpis(dbSession)

        a_written = asyncio.Event()
        proceed_a = asyncio.Event()

        orig_store = chat_module._service._storeSessionMessages

        async def _blocking_store(session, sessionId, question, answer, sql, **kwargs):
            if sessionId.startswith("s-a-"):
                a_written.set()
                await proceed_a.wait()
            return await orig_store(session, sessionId, question, answer, sql, **kwargs)

        async def _match_for_a(question: str):
            from app.services.kpi_semantic_match_service import KpiMatchResult
            return KpiMatchResult(code="KPI_TEST_A", confidence=0.95)

        async def _match_for_b(question: str):
            from app.services.kpi_semantic_match_service import KpiMatchResult
            return KpiMatchResult(code="KPI_TEST_B", confidence=0.95)

        # Wrap match functions in mock objects so monkeypatch.setattr can patch them.
        mock_matcher_a = _MockKpiMatcher(_match_for_a)
        mock_matcher_b = _MockKpiMatcher(_match_for_b)

        # Use monkeypatch.setattr throughout — framework auto-restores on test exit,
        # even if test is interrupted or runs out-of-order in parallel scheduling.
        monkeypatch.setattr(chat_module._service, "_storeSessionMessages", _blocking_store)
        monkeypatch.setattr(chat_module._service, "_kpiMatcher", mock_matcher_a)
        monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _KpiLlm())

        async def request_a():
            # Update mock matcher to A's version (same wrapper object, swap fn)
            monkeypatch.setattr(chat_module._service, "_kpiMatcher", mock_matcher_a)
            monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _KpiLlm())
            session_id = f"s-a-{uuid.uuid4().hex[:8]}"
            resp = await client.post(
                "/api/v1/chat",
                json=_chat_payload("KPI_TEST_A query A", ds.id, sessionId=session_id),
            )
            assert resp.status_code == 200, resp.text
            return resp.json()["answer"]

        async def request_b():
            await a_written.wait()
            monkeypatch.setattr(chat_module._service, "_kpiMatcher", mock_matcher_b)
            monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _KpiLlm())
            session_id = f"s-b-{uuid.uuid4().hex[:8]}"
            resp = await client.post(
                "/api/v1/chat",
                json=_chat_payload("KPI_TEST_B query B", ds.id, sessionId=session_id),
            )
            assert resp.status_code == 200, resp.text
            proceed_a.set()
            return resp.json()["answer"]

        answer_a, answer_b = await asyncio.gather(
            request_a(), request_b(),
        )

        # 断言：每个请求拿到自己的答案（不同 KPI 有不同 kpi_name，回答文本不同）
        assert "Alpha" in answer_a, (
            f"request A got {answer_a!r} (expected answer containing 'Alpha') - "
            "singleton _preamble_l1_response was contaminated by concurrent request B"
        )
        assert "Beta" in answer_b, (
            f"request B got {answer_b!r} (expected answer containing 'Beta')"
        )

    async def test_chat_service_instance_has_no_singleton_preamble_attributes(
        self, client, dbSession, monkeypatch
    ) -> None:
        """确认 ChatService 实例上不再有 _preamble_l1_response / _preamble_l4_response。

        这是结构性守卫（钉死属性名不存在），不是行为断言。
        若将来重构时有意将 full_response 改名，请同时修改本断言。
        """
        import app.api.v1.chat as chat_module

        config, ds, kpi_a, kpi_b = await _seed_kpis(dbSession)
        _install_fakes(monkeypatch, config, _KpiLlm())

        await client.post(
            "/api/v1/chat",
            json=_chat_payload("KPI_TEST_A query", ds.id),
        )

        # Structural guard: property must not exist on the singleton instance.
        # If this assertion fails after renaming full_response, update it to the new name.
        assert not hasattr(chat_module._service, "_preamble_l1_response"), (
            "_preamble_l1_response should not exist on ChatService instance - "
            "it was a process-wide singleton that caused cross-user data leakage"
        )
        assert not hasattr(chat_module._service, "_preamble_l4_response"), (
            "_preamble_l4_response should not exist on ChatService instance - "
            "it was a process-wide singleton that caused cross-user data leakage"
        )
