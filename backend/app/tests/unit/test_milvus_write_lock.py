"""Milvus 集合级写入锁（2026-11-17 flush 配额事故的客户端兜底）。

背景与口径见 ``app/infrastructure/milvus_write_lock.py`` 的模块文档。
本文件钉死三件事：

1. 同一集合的写入临界区**不重叠** —— 任一时刻只有一个 flush 在飞 ⇒
   服务端令牌桶的下一个令牌不会被兄弟请求抢走 ⇒ pymilvus 的重试预算足够。
2. 不同集合的写入**互不阻塞** —— 服务端令牌桶本来就是**每集合**的
   （class / property / metric / kpi 各有各的桶）⇒ 全局锁会把无关集合拖慢 10 倍。
3. 多集合锁按**固定顺序**获取 ⇒ 不会死锁；跨事件循环不互串。
"""

from __future__ import annotations

import asyncio

import pytest

from app.infrastructure.milvus_write_lock import (
    WRITE_LOCK_TIMEOUT_SECONDS,
    MilvusWriteLockTimeout,
    milvusWriteLock,
    milvusWriteLocks,
)


def test_same_collection_writes_are_serialized() -> None:
    """同一集合的两个写入临界区不得重叠。"""
    order: list[str] = []

    async def scenario() -> None:
        async def writer(tag: str) -> None:
            async with milvusWriteLock("ontology_property_embeddings"):
                order.append(f"{tag}-in")
                await asyncio.sleep(0.01)
                order.append(f"{tag}-out")

        await asyncio.gather(writer("a"), writer("b"))

    asyncio.run(scenario())

    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


def test_different_collections_do_not_block_each_other() -> None:
    """不同集合的临界区**同时**在飞（否则等于把令牌桶的粒度放大成全局）。"""
    events: list[str] = []

    async def scenario() -> None:
        async def writer(collection: str, tag: str) -> None:
            async with milvusWriteLock(collection):
                events.append(f"{tag}-in")
                await asyncio.sleep(0.01)
                events.append(f"{tag}-out")

        await asyncio.gather(
            writer("ontology_class_embeddings", "c"),
            writer("ontology_property_embeddings", "p"),
        )

    asyncio.run(scenario())

    assert [e.endswith("-in") for e in events] == [True, True, False, False]


def test_multi_collection_locks_do_not_deadlock() -> None:
    """两把多集合锁以**相反顺序**请求同样的集合 ⇒ 固定排序 ⇒ 不死锁。"""

    async def scenario() -> None:
        async def take(collections: list[str]) -> None:
            async with milvusWriteLocks(collections):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(
            asyncio.gather(
                take(["ontology_property_embeddings", "ontology_class_embeddings"]),
                take(["ontology_class_embeddings", "ontology_property_embeddings"]),
            ),
            timeout=5,
        )

    asyncio.run(scenario())


def test_lock_is_per_event_loop() -> None:
    """另一个事件循环里的同名集合**不被**本 loop 的临界区挡住。

    asyncio.Lock 绑定**首次使用**它的那个 loop ⇒ 跨 loop 复用同一把锁会抛
    RuntimeError。键里带 loop ⇒ 每个 loop 各一把（互斥语义不跨 loop，也不需要）。
    """
    entered: list[str] = []

    async def holder() -> None:
        async with milvusWriteLock("ontology_class_embeddings"):
            entered.append("holder-in")
            # 在**别的** loop 里取同名锁：不得被挡住，也不得抛 RuntimeError。
            await asyncio.to_thread(lambda: asyncio.run(other()))
            entered.append("holder-out")

    async def other() -> None:
        async with milvusWriteLock("ontology_class_embeddings"):
            entered.append("other-in")

    asyncio.run(holder())

    assert entered == ["holder-in", "other-in", "holder-out"]


def test_waiting_for_lock_times_out_instead_of_hanging() -> None:
    """等锁**必须有上限** —— 服务端限流是「拒绝」不是「排队」。

    没有上限时，到达率一旦高于排空率（配额 0.1 qps 下单次写占锁约 20s），
    等待队列会**单调发散** ⇒ 请求永不返回 ⇒ 每个等待者都是一个 asyncio 任务 +
    一条 HTTP 连接 ⇒ worker 被挂死（比改前的「重试 210s 报错」更糟）。
    """
    collection = "ontology_property_embeddings"

    async def scenario() -> None:
        async def holder() -> None:
            async with milvusWriteLock(collection):
                await asyncio.sleep(0.3)

        async def waiter() -> None:
            await asyncio.sleep(0.05)  # 确保 holder 已持锁
            async with milvusWriteLock(collection, timeoutSeconds=0.05):
                pytest.fail("不应当拿到锁")

        with pytest.raises(MilvusWriteLockTimeout) as excinfo:
            await asyncio.gather(holder(), waiter())

        assert excinfo.value.collection == collection
        assert excinfo.value.timeoutSeconds == 0.05

    asyncio.run(scenario())


def test_lock_is_released_after_timeout() -> None:
    """超时的等待者不得污染锁状态 ⇒ holder 释放后其它等待者仍能拿到。"""
    collection = "ontology_class_embeddings"

    async def scenario() -> None:
        released = asyncio.Event()

        async def holder() -> None:
            async with milvusWriteLock(collection):
                await asyncio.sleep(0.2)
                released.set()

        async def timingOutWaiter() -> None:
            await asyncio.sleep(0.02)
            try:
                async with milvusWriteLock(collection, timeoutSeconds=0.02):
                    pytest.fail("不应当拿到锁")
            except MilvusWriteLockTimeout:
                pass

        async def laterWaiter() -> str:
            async with milvusWriteLock(collection):
                assert released.is_set()
                return "acquired"

        results = await asyncio.gather(
            holder(), timingOutWaiter(), laterWaiter()
        )
        assert results[2] == "acquired"

    asyncio.run(scenario())


def test_default_timeout_is_bounded() -> None:
    """默认上限必须存在且是有限值（0 或 None 会让等待无界）。"""
    assert 0 < WRITE_LOCK_TIMEOUT_SECONDS <= 300


def test_multi_collection_lock_honours_timeout() -> None:
    """``milvusWriteLocks`` 逐个取锁 ⇒ 每个都要有上限（不能只有单锁有）。"""
    collections = ["ontology_class_embeddings", "ontology_property_embeddings"]

    async def scenario() -> None:
        async def holder() -> None:
            async with milvusWriteLocks(collections):
                await asyncio.sleep(0.3)

        async def waiter() -> None:
            await asyncio.sleep(0.05)
            async with milvusWriteLocks(collections, timeoutSeconds=0.05):
                pytest.fail("不应当拿到锁")

        with pytest.raises(MilvusWriteLockTimeout):
            await asyncio.gather(holder(), waiter())

    asyncio.run(scenario())
