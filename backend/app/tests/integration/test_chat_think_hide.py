"""Think_Hide 系统参数端到端（think 块剥离）。

推理模型（MiniMax-M3 等）把思维链以 ``<think>…</think>`` 内联在答案里。
Think_Hide=0（默认，迁移 0108 种 '0'）→ 原样透传；Think_Hide=1 → 剥离，
且实时下发内容与落库内容一致（不能只改其一）。

覆盖两种机制：整块剥离（非流式 applyThinkPolicy）与增量过滤
（流式 ThinkStreamFilter，标签切在片缝里）。
"""

from __future__ import annotations

import json

from sqlalchemy import text

from app.infrastructure.llm.base_client import StreamChunk
from app.tests.integration.test_chat_api import (
    _FakeAdapter,
    _RouterFor,
    _seed,
    _StubEmbeddingService,
)
from app.tests.integration.test_chat_stream_api import _parseFrames
from app.services.stream_events import EVENT_DONE, EVENT_TOKEN

_THINK_ANSWER = "<think>推理 A\n推理 B</think>\n\n最终答案：42。"
_STRIPPED_ANSWER = "最终答案：42。"


async def _seedThinkHide(dbSession, value: str) -> None:
    """conftest TRUNCATE 后 system_config 为空，测试自行种子（原生 SQL upsert）。"""
    await dbSession.execute(
        text(
            "INSERT INTO system_config (key, value, description) "
            "VALUES ('Think_Hide', :value, 'test') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ),
        {"value": value},
    )
    await dbSession.commit()


class _ThinkPipelineLlm:
    """非流式 complete()：回答阶段返回带 think 块的文本。"""

    async def complete(self, messages: list, **kwargs) -> object:
        system = messages[0].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "解析为查询计划" in system:
            _Resp.content = '{"target":"各供应商的收货数量汇总","selectedClasses":["PRECEIPT"]}'
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            _Resp.content = _THINK_ANSWER
        return _Resp()


def _install(monkeypatch, config) -> None:
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _ThinkPipelineLlm())
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


class TestThinkHideNonStream:
    async def test_default_missing_row_passthrough(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        _install(monkeypatch, config)
        resp = await client.post(
            "/api/v1/chat",
            json={"sessionId": "s-think-a", "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        )
        assert resp.status_code == 200, resp.text
        # 缺省（等效 Think_Hide=0）→ 字节级透传，不吞任何内容
        assert resp.json()["answer"] == _THINK_ANSWER

    async def test_value_one_strips_think_block(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        await _seedThinkHide(dbSession, "1")
        _install(monkeypatch, config)
        resp = await client.post(
            "/api/v1/chat",
            json={"sessionId": "s-think-b", "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["answer"] == _STRIPPED_ANSWER

    async def test_value_zero_passthrough(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        await _seedThinkHide(dbSession, "0")
        _install(monkeypatch, config)
        resp = await client.post(
            "/api/v1/chat",
            json={"sessionId": "s-think-c", "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["answer"] == _THINK_ANSWER

    async def test_stripped_answer_is_what_gets_persisted(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """落库的 assistant 消息必须与下发内容一致（不能流里剥了、库里还是原文）。"""
        config, ds = await _seed(dbSession)
        await _seedThinkHide(dbSession, "1")
        _install(monkeypatch, config)
        resp = await client.post(
            "/api/v1/chat",
            json={"sessionId": "s-think-d", "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        )
        assert resp.status_code == 200, resp.text
        row = (
            await dbSession.execute(
                text(
                    "SELECT content FROM session_message "
                    "WHERE session_id = 's-think-d' AND role = 'assistant'"
                )
            )
        ).first()
        assert row is not None
        assert row[0] == _STRIPPED_ANSWER


class _ThinkStreamLlm(_ThinkPipelineLlm):
    """回答流：think 标签切在片缝里（增量过滤的最苛刻输入）。"""

    async def completeStream(self, messages: list, **kwargs):
        for piece in ("<think>推理 A", "\n推理 B</thin", "k>\n\n最终答案：42。"):
            yield StreamChunk(
                content=piece, isDone=False, promptTokens=0,
                completionTokens=0, modelName="test-model",
            )
        yield StreamChunk(
            content="", isDone=True, promptTokens=10, completionTokens=5,
            modelName="test-model",
        )


def _installStream(monkeypatch, config) -> None:
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _ThinkStreamLlm())
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


def _tokenText(resp_text: str) -> tuple[str, bool]:
    frames = _parseFrames(resp_text)
    tokens = "".join(data.get("content", "") for e, data in frames if e == EVENT_TOKEN)
    hasDone = any(e == EVENT_DONE for e, _ in frames)
    return tokens, hasDone


class TestThinkHideStream:
    async def test_stream_default_passthrough(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        _installStream(monkeypatch, config)
        resp = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s-think-s0", "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        )
        assert resp.status_code == 200, resp.text
        tokens, hasDone = _tokenText(resp.text)
        assert hasDone
        assert tokens == _THINK_ANSWER

    async def test_stream_value_one_filters_incrementally(
        self, client, dbSession, monkeypatch,
    ) -> None:
        config, ds = await _seed(dbSession)
        await _seedThinkHide(dbSession, "1")
        _installStream(monkeypatch, config)
        resp = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s-think-s1", "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        )
        assert resp.status_code == 200, resp.text
        tokens, hasDone = _tokenText(resp.text)
        assert hasDone
        # 逐片过滤后与全文剥离一致；且没有任何 think 片段漏到 token 帧
        assert tokens == _STRIPPED_ANSWER
        assert "<think" not in tokens
        assert "推理" not in tokens
