"""研究事件总线与 SSE 帧机制（Task 8）：背压 / 订阅生命周期 / 心跳 / 终态判定。

心跳按**机制**测（把 `HEARTBEAT_SECONDS` 压到 0.01s），不按墙钟测（15s 真睡眠太慢）。
"""

from __future__ import annotations

import logging

import pytest

from app.api.v1 import research as researchModule
from app.services.research_event_bus import EVENT_CONNECTED, QUEUE_MAX, ResearchEventBus

# ---------------------------------------------------------------------------
# 总线：投递 / 背压 / 订阅生命周期
# ---------------------------------------------------------------------------


async def test_publish_reaches_every_subscriber_of_session() -> None:
    """同一会话的多个订阅者（多标签页）都收到同一帧；其它会话不受影响。"""
    bus = ResearchEventBus()
    first = bus.subscribe("s1")
    second = bus.subscribe("s1")
    other = bus.subscribe("s2")

    await bus.publish("s1", "research.intent", {"intent": "research"})

    assert first.get_nowait() == ("research.intent", {"intent": "research"})
    assert second.get_nowait() == ("research.intent", {"intent": "research"})
    assert other.empty()


async def test_queue_full_drops_oldest_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """背压：队列满丢**最旧**（新事件优先），并留 warning —— 不阻塞、不抛。"""
    bus = ResearchEventBus(maxQueue=3)
    queue = bus.subscribe("s1")

    with caplog.at_level(logging.WARNING):
        for index in range(5):
            await bus.publish("s1", "research.step.done", {"index": index})

    assert queue.qsize() == 3
    assert [queue.get_nowait()[1]["index"] for _ in range(3)] == [2, 3, 4]
    assert "丢弃最旧事件" in caplog.text


async def test_default_queue_cap_is_200() -> None:
    """brief 契约：队列上限 200。"""
    assert QUEUE_MAX == 200
    bus = ResearchEventBus()
    queue = bus.subscribe("s1")
    assert queue.maxsize == QUEUE_MAX


async def test_unsubscribe_is_idempotent_and_clears_session_key() -> None:
    """退订幂等；最后一个订阅者离开后会话键被清掉（字典不无界增长）。"""
    bus = ResearchEventBus()
    queue = bus.subscribe("s1")
    assert bus.subscriberCount("s1") == 1

    bus.unsubscribe("s1", queue)
    bus.unsubscribe("s1", queue)  # 幂等
    assert bus.subscriberCount("s1") == 0

    # 无订阅者时 publish 是安全 no-op（状态机不受推流影响）
    await bus.publish("s1", "research.done", {})


# ---------------------------------------------------------------------------
# 流端帧机制：首事件 / 心跳 / 退订
# ---------------------------------------------------------------------------


async def test_event_frames_yields_connected_then_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首帧 `research.connected`；空闲超过心跳间隔即发 `: ping` 注释行。"""
    monkeypatch.setattr(researchModule, "HEARTBEAT_SECONDS", 0.01)
    frames = researchModule._eventFrames("s-heartbeat")
    try:
        first = await frames.__anext__()
        second = await frames.__anext__()
    finally:
        await frames.aclose()

    assert first.startswith(f"event: {EVENT_CONNECTED}\n")
    assert second == ": ping\n\n"
    # 退出（含客户端断连的 GeneratorExit 路径）即退订
    assert researchModule._bus.subscriberCount("s-heartbeat") == 0


async def test_event_frames_renders_published_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总线收到的事件被渲染为 `event:` + `data:` 帧。"""
    monkeypatch.setattr(researchModule, "HEARTBEAT_SECONDS", 5)
    queue = researchModule._bus.subscribe("s-frame")
    frames = researchModule._eventFrames("s-frame")
    try:
        await frames.__anext__()  # connected
        await researchModule._bus.publish("s-frame", "research.esl", {"metrics": []})
        frame = await frames.__anext__()
    finally:
        await frames.aclose()
        researchModule._bus.unsubscribe("s-frame", queue)

    assert frame == 'event: research.esl\ndata: {"metrics": []}\n\n'


@pytest.mark.parametrize(
    ("event", "payload", "expected"),
    [
        ("research.done", {"degraded": False}, True),
        ("research.error", {"code": "turn_failed"}, True),
        ("research.error", {"code": "llm_unavailable"}, False),
        ("research.error", {"code": "sql_validation_failed"}, False),
        ("research.step.done", {"index": 0}, False),
        ("research.checkpoint", {"phase": "intent"}, False),
    ],
)
def test_is_terminal_only_closes_on_done_or_turn_failure(
    event: str, payload: dict, expected: bool
) -> None:
    """终态判定：done 或 `turn_failed`；降级类 error（llm_unavailable / 步失败）不关流。"""
    assert researchModule._isTerminal(event, payload) is expected
