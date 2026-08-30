"""HistoryService 单元测试（Phase 4.5 governance hardening）。

无 DB；用 MagicMock 模拟 session + KpiCatalog（构造实体 + __table__.columns）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.domain.models import KpiCatalog
from app.services.history_service import HistoryService


def _fakeKpi(id: int = 1, revision: int = 0, kpiCode: str = "X") -> KpiCatalog:
    """构造脱离 session 的 KpiCatalog（mock __table__.columns.keys）。"""
    kpi = KpiCatalog()
    kpi.id = id
    kpi.revision_count = revision
    kpi.kpi_code = kpiCode
    # __table__.columns.keys() 在 SQLAlchemy 真实类上工作；KpiCatalog 是声明式类，无需 mock
    return kpi


def _mockSession() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    return s


class TestHistoryServiceSnapshot:
    def setup_method(self) -> None:
        self._svc = HistoryService()
        self._session = _mockSession()

    def test_snapshot_uses_kpi_columns(self) -> None:
        """snapshot_json 包含 KpiCatalog 所有表列。"""
        import asyncio

        kpi = _fakeKpi(id=7, revision=2, kpiCode="KPI_X")
        asyncio.run(self._svc.snapshot(self._session, kpi=kpi, changed_by="alice"))
        callArg = self._session.add.call_args[0][0]
        # 应包含 KpiCatalog 实际列
        expectedCols = set(KpiCatalog.__table__.columns.keys())
        assert expectedCols.issubset(set(callArg.snapshot_json.keys()))
        assert callArg.snapshot_json["id"] == 7
        assert callArg.snapshot_json["kpi_code"] == "KPI_X"

    def test_revision_matches_kpi_revision_count(self) -> None:
        """history.revision 与 kpi.revision_count 对齐。"""
        import asyncio

        kpi = _fakeKpi(revision=3)
        asyncio.run(self._svc.snapshot(self._session, kpi=kpi, changed_by="alice"))
        callArg = self._session.add.call_args[0][0]
        assert callArg.revision == 3

    def test_snapshot_does_not_commit(self) -> None:
        """snapshot 只 session.add，不 commit（事务与 service 层绑定）。"""
        import asyncio

        kpi = _fakeKpi()
        asyncio.run(self._svc.snapshot(self._session, kpi=kpi, changed_by="alice"))
        self._session.add.assert_called_once()
        self._session.commit.assert_not_called()
        self._session.flush.assert_not_called()

    def test_snapshot_records_changed_by(self) -> None:
        import asyncio

        kpi = _fakeKpi()
        asyncio.run(self._svc.snapshot(self._session, kpi=kpi, changed_by="alice@dept"))
        callArg = self._session.add.call_args[0][0]
        assert callArg.changed_by == "alice@dept"

    def test_snapshot_changed_by_optional(self) -> None:
        """changed_by 为 None 时也应能写入（兜底：anonymous 也会传 None）。"""
        import asyncio

        kpi = _fakeKpi()
        asyncio.run(self._svc.snapshot(self._session, kpi=kpi, changed_by=None))
        callArg = self._session.add.call_args[0][0]
        assert callArg.changed_by is None

    def test_snapshot_records_kpi_id_for_fk(self) -> None:
        """kpi_id 字段写入，用于 FK 关联（即便后续删除 KPI，FK ON DELETE SET NULL 也不会抛）。"""
        import asyncio

        kpi = _fakeKpi(id=99)
        asyncio.run(self._svc.snapshot(self._session, kpi=kpi, changed_by="alice"))
        callArg = self._session.add.call_args[0][0]
        assert callArg.kpi_id == 99
