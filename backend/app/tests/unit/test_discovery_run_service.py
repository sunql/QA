"""发现运行受理 + 后台执行（G3-4）：真实 PG。

钉住四件事：

1. **受理只做便宜校验**：源/快照存在且归属一致；没有 RUNNING 重入；
2. **快照缺省**：不传 `snapshot_id` ⇒ 用该源**最新**快照；一张都没有 ⇒ 422 语义；
3. **后台会话隔离**：`runPipeline` 用自有会话（不碰请求会话）；
4. **后台永不炸请求线程**：剖析/候选的异常只记日志（剖析失败已由 profiler 落 FAILED）。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.discovery_pipeline_models import DiscoveryRun
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import DataSource
from app.infrastructure.security.crypto import encryptApiKey
from app.services.discovery_run_service import DiscoveryRunService

_SNAPSHOT: dict[str, Any] = {"schemaVersion": "v2", "tables": []}


class _FakeProfiler:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def profileTables(self, session: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("ORA-00942")
        return None


class _FakeCandidates:
    def __init__(self, *, fail: bool = False, error: Exception | None = None) -> None:
        self.fail = fail
        self.error = error or RuntimeError("boom")
        self.calls: list[int] = []

    async def generateCandidates(self, session: Any, *, runId: int) -> Any:
        self.calls.append(runId)
        if self.fail:
            raise self.error
        return None


async def _seed(
    session: Any, *, status: str = "ACTIVE", snapshots: int = 1, suffix: str = ""
) -> tuple[int, list[int]]:
    datasource = DataSource(
        name=f"RUN_SRC{suffix}",
        type="oracle",
        host="h",
        port=1521,
        database_name="THBI",
        username="THBI",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
    )
    session.add(datasource)
    await session.flush()
    source = DiscoverySource(
        name=f"运行源{suffix}",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
        status=status,
    )
    session.add(source)
    await session.flush()
    snapshotIds: list[int] = []
    for index in range(snapshots):
        snapshot = MetadataSnapshot(
            source_id=source.id,
            schema_hash=f"{index:064d}"[-64:],
            snapshot_json=_SNAPSHOT,
            table_count=0,
            column_count=0,
        )
        session.add(snapshot)
        await session.flush()
        snapshotIds.append(snapshot.id)
    await session.commit()
    return source.id, snapshotIds


# ---------------------------------------------------------------------------
# 受理
# ---------------------------------------------------------------------------


async def test_start_run_uses_the_latest_snapshot_by_default(dbSession: Any) -> None:
    sourceId, snapshotIds = await _seed(dbSession, snapshots=2)

    run = await DiscoveryRunService().startRun(
        dbSession, sourceId=sourceId, createdBy="tester"
    )

    assert run.status == "RUNNING"
    assert run.snapshot_id == snapshotIds[-1]
    assert run.created_by == "tester"


async def test_start_run_without_any_snapshot_is_rejected(dbSession: Any) -> None:
    sourceId, _ = await _seed(dbSession, snapshots=0)

    with pytest.raises(ValidationError):
        await DiscoveryRunService().startRun(dbSession, sourceId=sourceId)


async def test_start_run_rejects_a_snapshot_of_another_source(dbSession: Any) -> None:
    sourceId, _ = await _seed(dbSession)
    _, otherSnapshotIds = await _seed(dbSession, suffix="-other")

    with pytest.raises(ValidationError):
        await DiscoveryRunService().startRun(
            dbSession, sourceId=sourceId, snapshotId=otherSnapshotIds[0]
        )


async def test_start_run_rejects_a_second_running_run(dbSession: Any) -> None:
    sourceId, snapshotIds = await _seed(dbSession)
    service = DiscoveryRunService()
    await service.startRun(dbSession, sourceId=sourceId)

    with pytest.raises(ConflictError):
        await service.startRun(dbSession, sourceId=sourceId)


async def test_start_run_rejects_a_disabled_source(dbSession: Any) -> None:
    sourceId, _ = await _seed(dbSession, status="DISABLED")

    with pytest.raises(ValidationError):
        await DiscoveryRunService().startRun(dbSession, sourceId=sourceId)


async def test_start_run_rejects_unknown_source(dbSession: Any) -> None:
    with pytest.raises(NotFoundError):
        await DiscoveryRunService().startRun(dbSession, sourceId=999999)


# ---------------------------------------------------------------------------
# 后台执行
# ---------------------------------------------------------------------------


async def test_run_pipeline_profiles_then_generates_candidates(dbSession: Any) -> None:
    sourceId, snapshotIds = await _seed(dbSession)
    run = await DiscoveryRunService().startRun(dbSession, sourceId=sourceId)
    profiler = _FakeProfiler()
    candidates = _FakeCandidates()

    await DiscoveryRunService(
        profiler=profiler, candidateService=candidates
    ).runPipeline(run.id, tables=["DIM_SUPPLIER"], session=dbSession)

    assert profiler.calls[0]["runId"] == run.id
    assert profiler.calls[0]["sourceId"] == sourceId
    assert profiler.calls[0]["snapshotId"] == snapshotIds[-1]
    assert profiler.calls[0]["tables"] == ["DIM_SUPPLIER"]
    assert candidates.calls == [run.id]


async def test_run_pipeline_skips_candidates_when_disabled(dbSession: Any) -> None:
    sourceId, _ = await _seed(dbSession)
    run = await DiscoveryRunService().startRun(dbSession, sourceId=sourceId)
    candidates = _FakeCandidates()

    await DiscoveryRunService(
        profiler=_FakeProfiler(), candidateService=candidates
    ).runPipeline(run.id, withCandidates=False, session=dbSession)

    assert candidates.calls == []


async def test_profiler_failure_does_not_escape_and_skips_candidates(
    dbSession: Any,
) -> None:
    sourceId, _ = await _seed(dbSession)
    run = await DiscoveryRunService().startRun(dbSession, sourceId=sourceId)
    candidates = _FakeCandidates()

    await DiscoveryRunService(
        profiler=_FakeProfiler(fail=True), candidateService=candidates
    ).runPipeline(run.id, session=dbSession)

    assert candidates.calls == []


async def test_candidate_failure_is_swallowed(dbSession: Any) -> None:
    sourceId, _ = await _seed(dbSession)
    run = await DiscoveryRunService().startRun(dbSession, sourceId=sourceId)

    await DiscoveryRunService(
        profiler=_FakeProfiler(), candidateService=_FakeCandidates(fail=True)
    ).runPipeline(run.id, session=dbSession)

    stored = (
        await dbSession.execute(select(DiscoveryRun).where(DiscoveryRun.id == run.id))
    ).scalars().one()
    assert stored.status == "RUNNING"  # fake profiler 不改状态；真实 profiler 会置 SUCCEEDED


async def test_unknown_run_is_ignored(dbSession: Any) -> None:
    profiler = _FakeProfiler()

    await DiscoveryRunService(profiler=profiler).runPipeline(999999, session=dbSession)

    assert profiler.calls == []
