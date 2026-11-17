"""Milvus 集合级写入锁：同一集合的「删 + 插 + flush」串行化。

背景（2026-11-17 生产事故，见
``Harness/changes/2026-11-17-fix-milvus-flush-quota/summary.md``）：

- Milvus 出厂把**每个集合**的 flush 频率限死在 0.1 qps（每 10 秒 1 次，
  ``quotaAndLimits.flushRate.collection.max``）；
- 本系统的本体向量写路径对同一集合连续发 **2 次 flush**（delete 一次 + insert 一次）
  ⇒ 第 2 次落在同一个令牌窗口内 ⇒ 被服务端 grpc RateLimiter 拒绝并进入重试；
- 批量导入时 N 个属性各起一个后台同步任务 ⇒ **2N 次 flush 抢同一个令牌桶**
  ⇒ pymilvus 的 75 次重试（约 210s）预算被互相抢光 ⇒ 报错
  ``rate limit exceeded[rate=0.1]`` ⇒ **该实体的向量同步硬失败**。

为什么客户端要排队，而不是靠服务端拒绝兜底：服务端的限流是**拒绝**，不是排队；
被拒绝的请求各自重试 ⇒ 反而**互相抢**下一个令牌 ⇒ N 个并发写者里必然有人饿死
（这正是「重试 75 次仍失败」的机制）。把同一集合的写入在客户端串起来 ⇒
任一时刻只有一个 flush 在飞 ⇒ 令牌不会被兄弟请求抢走 ⇒ pymilvus 的重试预算
足够等到下一个令牌窗口 ⇒ **最终必成功**。

粒度 = **集合**，不是全局：Milvus 的令牌桶也是**每集合**的
（``ontology_class_embeddings`` / ``ontology_property_embeddings`` /
``ontology_metric_embeddings`` / ``kpi_catalog_embeddings`` 各有各的桶）
⇒ 不同集合的写入互不阻塞；用全局锁会把无关集合拖慢一个数量级。

⚠️ ``asyncio.Lock`` 绑定**首次使用**它的那个事件循环（跨 loop 使用会抛
``RuntimeError``）⇒ 键里带 loop；引用计数归零即删除 ⇒ 既不跨 loop 误用，也不泄漏
（与 ``ontology_service._vectorSyncLock`` 同款约定）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager

# 等锁上限（秒）。**必须有上限**：服务端限流是「拒绝」而不是「排队」⇒ 若配额仍是
# 出厂的 0.1 qps，单次写入自身就可能占锁约 20s（2 次 flush × 10s 令牌间隔）；
# 没有上限时，到达率一旦高于排空率，等待队列会**单调发散** ⇒ 请求永不返回
# （每个等待者都是一个 asyncio 任务 + 一条 HTTP 连接 ⇒ worker 被挂死），
# 比改前的「重试 210s 后报硬错误」更糟。超时即抛 `MilvusWriteLockTimeout`，
# 调用方转成业务错误 ⇒ **有界失败**（向量少一条可由对账补齐，worker 挂死不能自愈）。
WRITE_LOCK_TIMEOUT_SECONDS = 30.0

# key = (事件循环, 集合名) ⇒ 同 loop 同集合共用一把锁。
_WRITE_LOCKS: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}
_WRITE_LOCK_USERS: dict[tuple[asyncio.AbstractEventLoop, str], int] = {}


class MilvusWriteLockTimeout(RuntimeError):
    """等集合写入锁超时（同集合写入排队过久）。

    调用方应转成业务错误（可重试 / 走对账补齐），**不要**让它变成裸 500。
    """

    def __init__(self, collection: str, timeoutSeconds: float) -> None:
        super().__init__(
            f"等 Milvus 集合写入锁超时: collection={collection} "
            f"timeout={timeoutSeconds}s"
        )
        self.collection = collection
        self.timeoutSeconds = timeoutSeconds


@asynccontextmanager
async def milvusWriteLock(
    collection: str,
    *,
    timeoutSeconds: float = WRITE_LOCK_TIMEOUT_SECONDS,
) -> AsyncIterator[None]:
    """取某个 Milvus 集合的写入锁（临界区 = 一次完整的删+插+flush）。

    Raises:
        MilvusWriteLockTimeout: 等待超过 ``timeoutSeconds``（fail fast，不无限排队）。
    """
    loop = asyncio.get_running_loop()
    key = (loop, collection)
    lock = _WRITE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _WRITE_LOCKS[key] = lock
    _WRITE_LOCK_USERS[key] = _WRITE_LOCK_USERS.get(key, 0) + 1
    acquired = False
    try:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeoutSeconds)
        except asyncio.TimeoutError as exc:
            raise MilvusWriteLockTimeout(collection, timeoutSeconds) from exc
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        remaining = _WRITE_LOCK_USERS.get(key, 1) - 1
        if remaining <= 0:
            _WRITE_LOCK_USERS.pop(key, None)
            _WRITE_LOCKS.pop(key, None)
        else:
            _WRITE_LOCK_USERS[key] = remaining


@asynccontextmanager
async def milvusWriteLocks(
    collections: Iterable[str],
    *,
    timeoutSeconds: float = WRITE_LOCK_TIMEOUT_SECONDS,
) -> AsyncIterator[None]:
    """取多个集合的写入锁（一次写入横跨多个集合时用）。

    按集合名**排序后依次获取** ⇒ 固定顺序 ⇒ 两个请求以相反顺序请求同样的集合
    时也不会互相死锁。
    """
    ordered = sorted(set(collections))
    async with AsyncExitStack() as stack:
        for collection in ordered:
            await stack.enter_async_context(
                milvusWriteLock(collection, timeoutSeconds=timeoutSeconds)
            )
        yield
