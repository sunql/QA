"""流式断连落库（H4）集成测试：真实 PG + 真实 ASGI 断连时序。

**为什么不用 httpx**：`ASGITransport` 会把响应体全部缓冲后才返回 ⇒ 客户端无法在
中途断连，测不到断连路径。这里按 ASGI 协议自建驱动：`receive()` 在指定 SSE 帧
发出后返回 `http.disconnect` —— 与真实 uvicorn 的断连机制同源（Starlette 在
spec_version < 2.4 时靠 `listen_for_disconnect` 感知断连，随后在收敛任务组**之外**
`await background()`，H4 的兜底钩子就挂在这里）。

**断言的是「用户已经看到的东西不丢」**：断连后库里仍有本轮 user+assistant 两行，
assistant 行 `interrupted=true`，内容是已下发的部分答案（一个字都没产出时用固定
占位文案），查询状态已保存；**已落库后再断连不得重复写**（单发标志），正常跑完的
一轮则 `interrupted=false` 且内容完整。

真实 ASGI 时序不可用单测替代的原因（实测）：断连时生成器多数停在 `yield` 上，
此时它不在任务栈上 —— `except CancelledError` 与 `finally` **都不会触发**，
只有 `StreamingResponse(background=...)` 这条路径确定会跑到。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest
from sqlalchemy import select

from app.domain.models import SessionMessage, SessionQueryState
from app.infrastructure.llm.base_client import StreamChunk
from app.services.messages_zh import MSG_STREAM_INTERRUPTED_EMPTY
from app.tests._testapp import buildTestApp
from app.tests.integration.test_chat_api import _seed
from app.tests.integration.test_chat_stream_api import (
    _installStreamFakes,
    _StreamingPipelineLlm,
)

_STREAM_PATH = "/api/v1/chat/stream"
_FULL_ANSWER = "查询完成，共 2 条记录。"

# 回答流块间隔（秒）。**必须取真实量级，不能用 0**：生产者的生成器在**内层任务**
# 里跑，SSE 帧经中间件的内存队列由**外层任务**转发给 ASGI send —— 两者一解耦，
# 生成器就永远领先一帧。实测（`sleep(0)`）：驱动侧收到第 1 块 token 帧、置断连闸
# 的同一毫秒内，生成器已产出第 2 块 ⇒ 取消永远赶不上，落库的是完整答案。
# 50ms 与真实 LLM 流的块间隔同量级，断连（进程内一跳，亚毫秒）得以落在「等下一块」处。
_PIECE_GAP_SECONDS = 0.05


class _SuspendingAnswerLlm(_StreamingPipelineLlm):
    """回答流每段之间按真实块间隔挂起（供断连测试用）。

    `_StreamingPipelineLlm.completeStream` 是纯内存 fake：恢复生成器**不会挂起**，
    于是断连请求的取消要等到下一个真实 await 才送达 —— 实测落在答复用量
    `_recordUsage → commit → flush`，此时整段回答早已产出（落库内容是完整答案
    ⇒ 测不到「半截答案」）。真实网络流每块之间都在等 IO，故这里在**取下一块之前**
    挂起一次：取消正好落在「等下一块」的位置，与生产时序同形。
    """

    async def completeStream(self, messages: list, **kwargs):
        for piece in ("查询完成，", "共 2 条记录。"):
            await asyncio.sleep(_PIECE_GAP_SECONDS)  # 取消的着陆点：等下一块
            yield StreamChunk(
                content=piece, isDone=False,
                promptTokens=0, completionTokens=0, modelName="test-model",
            )
        await asyncio.sleep(_PIECE_GAP_SECONDS)
        yield StreamChunk(
            content="", isDone=True,
            promptTokens=10, completionTokens=5, modelName="test-model",
        )


async def _driveStreamWithDisconnect(
    app,
    *,
    payload: dict,
    disconnectWhen: Callable[[str], bool] | None,
) -> list[dict]:
    """驱动一次 SSE 请求；`disconnectWhen` 命中时注入 `http.disconnect`（None = 不断连）。

    返回收到的 ASGI 消息，供断言「断在哪一帧」。
    """
    body = json.dumps(payload).encode("utf-8")
    requestSent = False
    disconnectGate: asyncio.Event | None = (
        asyncio.Event() if disconnectWhen is not None else None
    )
    received: list[dict] = []

    async def receive() -> dict:
        nonlocal requestSent
        if not requestSent:
            requestSent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnectGate is None:
            # 不断连：响应发完后收尾任务会被取消，这里的挂起是可取消的
            await asyncio.Event().wait()
        await disconnectGate.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        received.append(message)
        if disconnectGate is None or disconnectGate.is_set():
            return
        if message["type"] == "http.response.body" and disconnectWhen(
            message.get("body", b"").decode("utf-8", "ignore")
        ):
            disconnectGate.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},  # uvicorn 0.52 实测上报值
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _STREAM_PATH,
        "raw_path": _STREAM_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return received


@pytest.fixture()
def asgiApp(dbSession):
    """真实 PG 会话工厂支撑的测试 app（与 client fixture 同一工厂）。

    `dbSession` 是前置依赖：它保证 `pgApiClient` 已把全局会话工厂换成真实 PG 测试库。
    """
    from app.infrastructure import database as dbModule

    return buildTestApp(dbModule.getSessionFactory())


async def _rows(dbSession) -> list[SessionMessage]:
    result = await dbSession.execute(select(SessionMessage).order_by(SessionMessage.id))
    return list(result.scalars().all())


async def _drive(dbSession, monkeypatch, asgiApp, *, disconnectWhen, sessionId: str) -> None:
    """造数 + 装 fake + 驱动一次请求（含断连时序）。"""
    import app.api.v1.chat as chat_module

    config, ds = await _seed(dbSession)
    _installStreamFakes(monkeypatch, config)
    # 回答流换成「每块之间挂起」的版本，否则取消只在整段回答产出后才送达
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _SuspendingAnswerLlm())
    # 造数/装 fake 都会隐式开启事务，先关掉，避免请求内的会话拿不到已提交数据
    await dbSession.commit()
    await _driveStreamWithDisconnect(
        asgiApp,
        payload={"sessionId": sessionId, "question": "各供应商的收货数量汇总", "datasourceId": ds.id},
        disconnectWhen=disconnectWhen,
    )


class TestStreamDisconnectPersistence:
    """断连兜底：部分产出必须落库并标记中断，已落库的轮次不得重复写。"""

    async def test_disconnect_mid_answer_persists_partial_answer(
        self, dbSession, monkeypatch, asgiApp
    ) -> None:
        """回答流中间断连（停在 yield 上，finally 抓不到）⇒ 已下发的部分答案落库。"""
        await _drive(
            dbSession, monkeypatch, asgiApp,
            disconnectWhen=lambda text: "event: token" in text,
            sessionId="s-disconnect-mid-answer",
        )

        msgs = await _rows(dbSession)
        assert [m.role for m in msgs] == ["user", "assistant"], [
            (m.role, m.interrupted, m.content) for m in msgs
        ]
        assistant = msgs[1]
        assert assistant.interrupted is True
        # 部分是「已下发的真前缀」，且没骗人说答完了
        assert _FULL_ANSWER.startswith(assistant.content)
        assert 0 < len(assistant.content) < len(_FULL_ANSWER)
        assert msgs[0].content == "各供应商的收货数量汇总"
        # 查询状态已保存（追问链不因断连而断）
        state = (
            await dbSession.execute(
                select(SessionQueryState).where(
                    SessionQueryState.session_id == "s-disconnect-mid-answer"
                )
            )
        ).scalar_one_or_none()
        assert state is not None
        assert state.last_question == "各供应商的收货数量汇总"
        assert state.last_sql is not None

    async def test_disconnect_before_answer_persists_placeholder(
        self, dbSession, monkeypatch, asgiApp
    ) -> None:
        """回答文本一个字都没产出就断连 ⇒ 落占位文案 + 中断标记（回合不凭空消失）。"""
        await _drive(
            dbSession, monkeypatch, asgiApp,
            disconnectWhen=lambda text: "event: sql" in text,
            sessionId="s-disconnect-no-answer",
        )

        msgs = await _rows(dbSession)
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[1].interrupted is True
        assert msgs[1].content == MSG_STREAM_INTERRUPTED_EMPTY

    async def test_disconnect_after_store_does_not_duplicate(
        self, dbSession, monkeypatch, asgiApp
    ) -> None:
        """已落库后再断连 ⇒ 兜底空操作（单发标志；重复写会变 4 行）。"""
        await _drive(
            dbSession, monkeypatch, asgiApp,
            disconnectWhen=lambda text: "event: done" in text,
            sessionId="s-disconnect-post-store",
        )

        msgs = await _rows(dbSession)
        assert len(msgs) == 2, [(m.role, m.interrupted, m.content) for m in msgs]
        assert msgs[1].interrupted is False
        assert msgs[1].content == _FULL_ANSWER

    async def test_normal_completion_persists_full_answer(
        self, dbSession, monkeypatch, asgiApp
    ) -> None:
        """对照组：正常跑完 ⇒ 完整答案 + interrupted=false + 无重复行。"""
        await _drive(
            dbSession, monkeypatch, asgiApp,
            disconnectWhen=None,
            sessionId="s-normal",
        )

        msgs = await _rows(dbSession)
        assert len(msgs) == 2
        assert msgs[1].interrupted is False
        assert msgs[1].content == _FULL_ANSWER
