"""对话流式接口（SSE）集成测试（5.6）。

验证 HTTP 契约：
- POST /api/v1/chat/stream 返回 text/event-stream
- 事件序列：meta → sql → chart → token* → done
- token 增量可拼接为完整回答；done 携带累计 token/成本
- chitchat：meta(chitchat) → token(问候) → done(0 token)
- 数据源不存在 → meta + error 事件
"""

from __future__ import annotations

import json

from app.infrastructure.llm.base_client import StreamChunk
from app.services.stream_events import (
    EVENT_CHART,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_META,
    EVENT_MULTI_STEP_PLAN,
    EVENT_PLAN,
    EVENT_SQL,
    EVENT_STEP_PLAN,
    EVENT_STEP_RESULT,
    EVENT_TOKEN,
)
from app.tests.integration.test_chat_api import _FakeAdapter, _RouterFor, _seed, _StubEmbeddingService


class _StreamingPipelineLlm:
    """非流式 complete() 供计划/SQL/chart；流式 completeStream() 供回答。"""

    async def complete(self, messages: list, **kwargs) -> object:
        system = messages[0].content
        user = messages[1].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}'
        elif "解析为查询计划" in system:
            _Resp.content = '{"target":"各供应商的收货数量汇总","selectedClasses":["PRECEIPT"]}'
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            _Resp.content = "查询完成，共 2 条记录，各供应商收货量分布如下。"
        return _Resp()

    async def completeStream(self, messages: list, **kwargs):
        """回答流：两段增量 + 末块携带 token 统计。"""
        for piece in ("查询完成，", "共 2 条记录。"):
            yield StreamChunk(
                content=piece, isDone=False, promptTokens=0, completionTokens=0, modelName="test-model"
            )
        yield StreamChunk(
            content="", isDone=True, promptTokens=10, completionTokens=5, modelName="test-model"
        )


def _installStreamFakes(monkeypatch, config) -> None:
    """用流式 fake 替换 ChatService 的路由 / LLM / 适配器 / embedding。"""
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _StreamingPipelineLlm())
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


def _parseFrames(text: str) -> list[tuple[str, dict]]:
    """解析 SSE 帧：event: X / data: {json}，按空行分隔。"""
    frames: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        data: dict | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        frames.append((event or "", data or {}))
    return frames


class TestChatStreamApi:
    async def test_streams_full_pipeline_events(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        _installStreamFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s1", "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")

        frames = _parseFrames(resp.text)
        events = [e for e, _ in frames]
        # 2026-08-16：单步查询也下发执行计划事件：
        # meta → multi_step_plan → step_plan → plan → sql → chart → token×2 → step_result → done
        assert events[0] == EVENT_META
        assert events[1] == EVENT_MULTI_STEP_PLAN
        assert frames[1][1]["steps"][0]["stepIndex"] == 0
        assert events[2] == EVENT_STEP_PLAN
        assert events[3] == EVENT_PLAN
        assert events[4] == EVENT_SQL
        assert events[5] == EVENT_CHART
        assert events[-1] == EVENT_DONE
        # step_result 在 done 之前（携带 sql/数据/摘要）
        stepResultFrames = [f for f in frames if f[0] == EVENT_STEP_RESULT]
        assert len(stepResultFrames) == 1
        assert stepResultFrames[0][1]["sql"] is not None
        tokenEvents = [f for f in frames if f[0] == EVENT_TOKEN]
        assert len(tokenEvents) == 2

        metaData = dict(frames[0][1])
        assert metaData["intent"] == "query"
        assert frames[3][1]["plan"]["target"] == "各供应商的收货数量汇总"
        assert "PRECEIPT" in frames[4][1]["sql"]
        chartData = frames[5][1]
        assert chartData["chartType"] == "pie"
        assert chartData["chartOption"] is not None
        assert len(chartData["data"]) == 2
        # token 增量拼接为完整回答
        answer = "".join(d["content"] for e, d in tokenEvents)
        assert answer == "查询完成，共 2 条记录。"
        # done 携带累计 token（计划/SQL 30 + chart 15 + answer 15）
        doneData = frames[-1][1]
        assert doneData["tokensUsed"] == 60
        assert doneData["cost"] > 0

    async def test_stream_records_answer_usage_and_session_messages(
        self, client, dbSession, monkeypatch
    ) -> None:
        config, ds = await _seed(dbSession)
        _installStreamFakes(monkeypatch, config)
        await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s1", "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        )

        from sqlalchemy import select

        from app.domain.models import SessionMessage, SessionTokenUsage

        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        assert sorted(r.purpose for r in usages) == ["answer", "chart", "nl2sql"]

        msgs = list((await dbSession.execute(select(SessionMessage).order_by(SessionMessage.id))).scalars().all())
        assert len(msgs) == 2
        assert msgs[1].role == "assistant"
        assert msgs[1].content == "查询完成，共 2 条记录。"

    async def test_chitchat_streams_greeting(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s1", "question": "你好", "datasourceId": 1},
        )
        assert resp.status_code == 200, resp.text
        frames = _parseFrames(resp.text)
        events = [e for e, _ in frames]
        assert events[0] == EVENT_META
        assert dict(frames[0][1])["intent"] == "chitchat"
        assert events[1] == EVENT_TOKEN
        assert "智能问答助手" in frames[1][1]["content"]
        assert events[2] == EVENT_DONE
        assert frames[2][1]["tokensUsed"] == 0
        # 无 sql / chart 事件
        assert EVENT_SQL not in events
        assert EVENT_CHART not in events
        # 4-4：闲聊轮也持久化消息（user + assistant），历史链不断
        from sqlalchemy import select

        from app.domain.models import SessionMessage

        msgs = list((await dbSession.execute(select(SessionMessage))).scalars().all())
        assert len(msgs) == 2
        assert {m.role for m in msgs} == {"user", "assistant"}

    async def test_datasource_missing_emits_error_event(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/chat/stream",
            json={"sessionId": "s1", "question": "查询各供应商收货数量", "datasourceId": 99999},
        )
        assert resp.status_code == 200, resp.text
        frames = _parseFrames(resp.text)
        events = [e for e, _ in frames]
        assert events[0] == EVENT_META  # 先告知意图，再报错
        assert events[1] == EVENT_ERROR
        assert "不存在" in frames[1][1]["error"]
        assert frames[1][1]["errorType"] == "domain"  # 4-1：NotFoundError 属领域错误
        # 4-4：出错轮也持久化消息（user + assistant）
        from sqlalchemy import select

        from app.domain.models import SessionMessage

        msgs = list((await dbSession.execute(select(SessionMessage))).scalars().all())
        assert len(msgs) == 2
        assert {m.role for m in msgs} == {"user", "assistant"}
