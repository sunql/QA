"""缓存 Schema 异步任务的进程内注册表。

设计见 docs/superpowers/specs/2026-11-25-schema-cache-progress-design.md：
- 纯内存（单进程 uvicorn 假设；横向扩容需换共享存储，另批处理）；
- 不可变：update 返回新实例，绝不原地改既有 job；
- 惰性淘汰：每次 create/get 顺带清理超过 TTL 的终态 job；
- 并发保护：同一 (datasourceId, schemaName) 只允许一个 running job。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

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

    def createOrGet(
        self, *, datasourceId: int, schemaName: str
    ) -> SchemaIntrospectJob:
        """返回该目标上仍在运行的 job；没有则新建一个。"""
        with self._lock:
            self._evict()
            for job in self._jobs.values():
                if (
                    job.datasourceId == datasourceId
                    and job.schemaName == schemaName
                    and job.status == "running"
                ):
                    return job
            created = SchemaIntrospectJob(
                jobId=uuid.uuid4().hex,
                datasourceId=datasourceId,
                schemaName=schemaName,
                status="running",
                updatedAt=self._clock(),
            )
            self._jobs[created.jobId] = created
            return created

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
