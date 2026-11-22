"""增量监测（G4-1）：采集判变 → 只在**变化**时起运行。

1. `reused=true`（同内容）⇒ `changed=false`、**不起运行**（不烧 LLM）；
2. `reused=false` ⇒ 建 RUNNING 运行（trigger=DIFF），剖析/候选交给后台；
3. 源不存在 / 停用 ⇒ 明确拒绝。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.discovery_models import DiscoverySource
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataSource
from app.services.discovery_monitor_service import DiscoveryMonitorService
from app.services.discovery_snapshot_service import SnapshotCapture


class _FakeSnapshots:
    def __init__(self, *, reused: bool, snapshotId: int = 5) -> None:
        self.reused = reused
        self.snapshotId = snapshotId
        self.calls = 0

    async def captureSnapshot(self, session: Any, *, sourceId: int, createdBy: Any = None):
        self.calls += 1
        return SnapshotCapture(
            snapshotId=self.snapshotId,
            sourceId=sourceId,
            schemaHash="a" * 64,
            tableCount=1,
            columnCount=2,
            capturedAt=datetime.now(UTC),
            reused=self.reused,
        )


class _FakeRuns:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def startRun(self, session: Any, **kwargs: Any):
        self.calls.append(kwargs)

        class _Run:
            id = 9

        return _Run()


async def _seed(session: Any, *, status: str = "ACTIVE") -> int:
    datasource = DataSource(
        name=f"MON_SRC_{status}",
        type="oracle",
        host="h",
        port=1521,
        database_name="THBI",
        username="THBI",
        password_encrypted="dummy-ciphertext-not-decrypted-in-tests",
        is_active=True,
    )
    session.add(datasource)
    await session.flush()
    source = DiscoverySource(
        name=f"监测源{status}",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
        status=status,
    )
    session.add(source)
    await session.commit()
    return source.id


async def test_unchanged_snapshot_does_not_start_a_run(dbSession: Any) -> None:
    sourceId = await _seed(dbSession)
    runs = _FakeRuns()

    result = await DiscoveryMonitorService(
        snapshotService=_FakeSnapshots(reused=True), runService=runs
    ).checkSource(dbSession, sourceId=sourceId, createdBy="tester")

    assert result.changed is False
    assert result.runId is None
    assert runs.calls == []


async def test_changed_snapshot_starts_a_diff_run(dbSession: Any) -> None:
    sourceId = await _seed(dbSession)
    runs = _FakeRuns()

    result = await DiscoveryMonitorService(
        snapshotService=_FakeSnapshots(reused=False, snapshotId=7), runService=runs
    ).checkSource(dbSession, sourceId=sourceId, createdBy="tester")

    assert result.changed is True
    assert result.runId == 9
    assert result.snapshotId == 7
    assert runs.calls[0]["trigger"] == "DIFF"
    assert runs.calls[0]["snapshotId"] == 7


async def test_unknown_source_is_rejected(dbSession: Any) -> None:
    with pytest.raises(NotFoundError):
        await DiscoveryMonitorService(
            snapshotService=_FakeSnapshots(reused=True), runService=_FakeRuns()
        ).checkSource(dbSession, sourceId=999999)


async def test_disabled_source_is_rejected(dbSession: Any) -> None:
    sourceId = await _seed(dbSession, status="DISABLED")

    with pytest.raises(ValidationError):
        await DiscoveryMonitorService(
            snapshotService=_FakeSnapshots(reused=True), runService=_FakeRuns()
        ).checkSource(dbSession, sourceId=sourceId)
