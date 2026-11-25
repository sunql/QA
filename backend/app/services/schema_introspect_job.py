"""缓存 Schema 异步任务的进程内注册表。

设计见 docs/superpowers/specs/2026-11-25-schema-cache-progress-design.md：
- 纯内存（单进程 uvicorn 假设；横向扩容需换共享存储，另批处理）；
- 不可变：update 返回新实例，绝不原地改既有 job；
- 惰性淘汰：每次 create/get 顺带清理超过 TTL 的终态 job；
- 并发保护：同一 (datasourceId, schemaName) 只允许一个 running job。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

logger = logging.getLogger(__name__)

JobStatus = Literal["running", "succeeded", "failed"]

_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


@dataclass(frozen=True)
class SchemaIntrospectJob:
    """一次异步内省任务的快照（不可变）。"""

    jobId: str
    datasourceId: int
    schemaName: str
    status: JobStatus
    total: int = 0
    completed: int = 0
    error: str | None = None
    updatedAt: float = 0.0


class SchemaIntrospectJobRegistry:
    """进程内 job 表：创建 / 更新 / 查询 / 惰性淘汰。"""

    def __init__(
        self,
        *,
        ttlSeconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jobs: dict[str, SchemaIntrospectJob] = {}
        self._ttlSeconds = ttlSeconds
        self._clock = clock
        # 进程内共享可变状态：public 方法与 _evict 均持同一把锁。
        # 用 RLock 是因为 createOrGet/get 会先调 _evict（同线程重入）。
        self._lock = threading.RLock()

    def _findRunningLocked(
        self, datasourceId: int, schemaName: str
    ) -> SchemaIntrospectJob | None:
        """`(datasourceId, schemaName)` 上 running 的 job；调用方须持锁。"""
        for job in self._jobs.values():
            if (
                job.datasourceId == datasourceId
                and job.schemaName == schemaName
                and job.status == "running"
            ):
                return job
        return None

    def createOrGet(
        self, *, datasourceId: int, schemaName: str
    ) -> SchemaIntrospectJob:
        """返回该目标上仍在运行的 job；没有则新建一个。"""
        with self._lock:
            self._evict()
            existing = self._findRunningLocked(datasourceId, schemaName)
            if existing is not None:
                return existing
            created = SchemaIntrospectJob(
                jobId=uuid.uuid4().hex,
                datasourceId=datasourceId,
                schemaName=schemaName,
                status="running",
                updatedAt=self._clock(),
            )
            self._jobs[created.jobId] = created
            return created

    def findRunning(
        self, *, datasourceId: int, schemaName: str
    ) -> SchemaIntrospectJob | None:
        """纯查询：该目标上正在运行的 job；无则 None。

        不创建、不改状态、不做淘汰（running 永不进淘汰集合，见 `_evict`）。
        用途：API 判断「是否需要挂后台任务」——不能靠 `createOrGet` 返回值的
        total/completed 是否为零推断，因为刚开始跑尚未报进度的既有 running job
        同样为零，会导致同一 job 被重复挂载执行。
        """
        with self._lock:
            return self._findRunningLocked(datasourceId, schemaName)

    def update(
        self,
        jobId: str,
        *,
        total: int | None = None,
        completed: int | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> SchemaIntrospectJob:
        """写入部分字段并返回新快照（不原地修改）。"""
        with self._lock:
            current = self._jobs.get(jobId)
            if current is None:
                raise KeyError(f"未找到 job: {jobId}")
            changes: dict[str, object] = {"updatedAt": self._clock()}
            if total is not None:
                changes["total"] = total
            if completed is not None:
                changes["completed"] = completed
            if status is not None:
                changes["status"] = status
            if error is not None:
                changes["error"] = error
            updated = replace(current, **changes)
            self._jobs[jobId] = updated
            return updated

    def get(self, jobId: str) -> SchemaIntrospectJob | None:
        with self._lock:
            self._evict()
            return self._jobs.get(jobId)

    def _evict(self) -> None:
        """惰性淘汰：只清终态且超 TTL 的 job（running 永不清）。"""
        now = self._clock()
        stale = [
            jobId
            for jobId, job in self._jobs.items()
            if job.status in _TERMINAL_STATUSES
            and now - job.updatedAt > self._ttlSeconds
        ]
        for jobId in stale:
            del self._jobs[jobId]


#: 进程级单例（API 与后台任务共用同一份状态）
schema_introspect_job_registry = SchemaIntrospectJobRegistry()


# --------------------------------------------------------------------------
# 后台执行入口
# --------------------------------------------------------------------------


#: 适配器提供者（测试可 monkeypatch；生产走 business_db_pool.get_adapter）
def _defaultAdapterProvider(dsId: int, ds: Any):
    from app.infrastructure.business_db_pool import get_adapter

    return get_adapter(dsId, ds)


_adapterProvider = _defaultAdapterProvider


def _userFacingError(exc: BaseException) -> str:
    """把底层异常映射为用户可读文案（不泄露 DSN / 用户名）。"""
    from app.domain.exceptions import DataSourceError

    if isinstance(exc, DataSourceError):
        return str(exc)
    return "数据源 schema 读取失败：请检查数据源连接是否正常"


async def runIntrospectJob(
    *,
    datasourceId: int,
    schemaName: str,
    jobId: str,
    registry: SchemaIntrospectJobRegistry,
) -> None:
    """在后台会话中跑一次分批内省并写缓存，全程维护 job 状态。

    三条硬约束：
    1. 用 ownSession() 自开会话（后台任务复用请求会话会触发 MissingGreenlet，
       见 infrastructure/database.py 的 2026-11-16 事故说明）；
    2. 任一批失败 ⇒ 不写缓存（introspectAndCacheBatched 内聚在单个事务里）；
    3. 对外只暴露友好文案，底层异常文本只进服务端日志。
    """
    from app.infrastructure.database import ownSession
    from app.services.datasource_service import DataSourceService
    from app.services.schema_introspection_service import SchemaIntrospectionService

    session = ownSession()
    try:
        ds = await DataSourceService().get(session, datasourceId)
        service = SchemaIntrospectionService(adapterProvider=_adapterProvider)
        await service.introspectAndCacheBatched(
            session,
            ds,
            owner=schemaName,
            registry=registry,
            jobId=jobId,
        )
        registry.update(jobId, status="succeeded")
    except asyncio.CancelledError:
        # 取消/关停时 CancelledError 是 BaseException，不会被下面的 except Exception
        # 兜住 ⇒ 若不在此落终态，job 会永远停在 running，注册表惰性淘汰又只清终态，
        # createOrGet 将永久复用这个死 job（该数据源再也无法发起内省）。
        # 落 failed 后必须 raise，保留取消语义（调用方/事件循环需要它向上传播）。
        registry.update(jobId, status="failed", error="任务已取消")
        raise
    except Exception as exc:  # noqa: BLE001 - 后台任务边界，必须兜住并落状态
        logger.error(
            "schema 内省 job 失败 jobId=%s datasource=%s schema=%s: %s",
            jobId, datasourceId, schemaName, exc, exc_info=True,
        )
        registry.update(jobId, status="failed", error=_userFacingError(exc))
    finally:
        await session.close()
