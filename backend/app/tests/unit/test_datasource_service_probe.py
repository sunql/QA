"""数据源版本探测（连接时顺带探测，填充 oracle_version）单测。

背景（2026-10-02）：方言分发只认 `oracle_version` 字段，但它是「人填的自由文本」——
不填则 Oracle 默认按 12c+（FETCH FIRST），11g 库会 ORA-00933；MySQL/PG 的版本
根本没有载体（MySQL 5.7 无 CTE/窗口函数，若接入而版本不可见则无从防范）。

修复＝三层：
1. adapter.test() 顺带返回 server_version（SQLAlchemy dialect.server_version_info /
   oracledb conn.version，不新增连接）；
2. /test 端点响应带 server_version；create/update 后台自动探测（best-effort，
   失败不阻断），**只在为空时填充，永不覆盖用户显式值**；
3. resolveDialect 兼容点分版本（"11.2.0.1.0" → 11g 分支）。

顺带修复既有 bug：create() 此前根本不持久化 dto.oracle_version（DTO 有字段、
构造器漏赋值），创建时填的版本被静默丢弃。
"""

from __future__ import annotations

import pytest

from app.domain.enums import DataSourceType
from app.domain.schemas import DataSourceCreate, DataSourceTestRequest, DataSourceUpdate
from app.services import datasource_service as ds_module
from app.services.datasource_service import DataSourceService


def _probeTarget() -> dict:
    """一个指向测试 PG（qa-pg-a1）的连接参数（探测会被 monkeypatch 掉，不真连）。"""
    return {
        "type": DataSourceType.POSTGRESQL,
        "host": "qa-pg-a1",
        "port": 5432,
        "database_name": "qa_metadata_test",
        "username": "qa_user",
        "password": "probe-pw",
    }


def _dto(**overrides) -> DataSourceCreate:
    payload = {"name": "probe-ds", **_probeTarget(), **overrides}
    return DataSourceCreate(**payload)


class TestNormalizeServerVersion:
    """版本字符串归一：tuple / str / 异常值。"""

    def test_tuple_joined_by_dot(self) -> None:
        assert ds_module._normalizeServerVersion(("8", "0", "46")) == "8.0.46"

    def test_str_passthrough(self) -> None:
        assert ds_module._normalizeServerVersion("19.0.0.0.0") == "19.0.0.0.0"

    def test_none_returns_none(self) -> None:
        assert ds_module._normalizeServerVersion(None) is None

    def test_oversized_truncated(self) -> None:
        # 防 v$version banner 之类的长文本进字段
        assert len(ds_module._normalizeServerVersion("1.2.3 " * 20) or "") <= 40


class TestCreateFillsProbedVersion:
    async def test_empty_version_filled_by_probe(self, dbSession, monkeypatch) -> None:
        async def fakeProbe(*args, **kwargs):
            return "16.2"

        monkeypatch.setattr(ds_module, "_probeServerVersion", fakeProbe)
        ds = await DataSourceService().create(dbSession, _dto(), createdBy="admin")
        assert ds.oracle_version == "16.2"

    async def test_user_declared_version_wins(self, dbSession, monkeypatch) -> None:
        """用户显式填了版本 → 探测值不得覆盖。"""

        async def fakeProbe(*args, **kwargs):
            return "16.2"

        monkeypatch.setattr(ds_module, "_probeServerVersion", fakeProbe)
        ds = await DataSourceService().create(
            dbSession, _dto(oracle_version="11g"), createdBy="admin"
        )
        assert ds.oracle_version == "11g"

    async def test_probe_failure_does_not_block_create(self, dbSession, monkeypatch) -> None:
        """探测失败（连接不通等）只记日志，创建照常成功。"""

        async def fakeProbe(*args, **kwargs):
            return None

        monkeypatch.setattr(ds_module, "_probeServerVersion", fakeProbe)
        ds = await DataSourceService().create(dbSession, _dto(), createdBy="admin")
        assert ds.oracle_version is None


class TestUpdateRefillsProbedVersion:
    async def _seed(self, dbSession, monkeypatch, version=None) -> int:
        async def fakeProbe(*args, **kwargs):
            return version

        monkeypatch.setattr(ds_module, "_probeServerVersion", fakeProbe)
        ds = await DataSourceService().create(dbSession, _dto(), createdBy="admin")
        return ds.id

    async def test_conn_changed_and_empty_version_refilled(self, dbSession, monkeypatch) -> None:
        dsId = await self._seed(dbSession, monkeypatch, version=None)
        # 探测第二次返回新版本（连接参数变了 → 重新探测）
        versions = iter(["16.9"])

        async def fakeProbe(*args, **kwargs):
            return next(versions)

        monkeypatch.setattr(ds_module, "_probeServerVersion", fakeProbe)
        ds = await DataSourceService().update(
            dbSession, dsId, DataSourceUpdate(port=5433), actor=None
        )
        assert ds.oracle_version == "16.9"

    async def test_existing_version_not_overwritten_when_params_unchanged(
        self, dbSession, monkeypatch
    ) -> None:
        dsId = await self._seed(dbSession, monkeypatch, version="15.1")
        called = []

        async def fakeProbe(*args, **kwargs):
            called.append(1)
            return "16.9"

        monkeypatch.setattr(ds_module, "_probeServerVersion", fakeProbe)
        ds = await DataSourceService().update(
            dbSession, dsId, DataSourceUpdate(description="备注"), actor=None
        )
        assert ds.oracle_version == "15.1"
        assert called == []  # 参数没变、版本已有 → 不应发起探测

    async def test_explicit_update_version_wins_over_probe(self, dbSession, monkeypatch) -> None:
        dsId = await self._seed(dbSession, monkeypatch, version=None)

        async def fakeProbe(*args, **kwargs):
            raise AssertionError("用户显式给了版本，不应探测")

        monkeypatch.setattr(ds_module, "_probeServerVersion", fakeProbe)
        ds = await DataSourceService().update(
            dbSession, dsId, DataSourceUpdate(oracle_version="11g"), actor=None
        )
        assert ds.oracle_version == "11g"


class TestTestConnectionReturnsServerVersion:
    async def test_response_carries_server_version(self, monkeypatch) -> None:
        class _FakeAdapter:
            async def test(self):
                return True, "连接成功", "8.0.46"

            async def dispose(self):
                pass

        captured = {}

        def fakeBuildAdapter(dsType, host, port, dbName, username, password):
            captured["type"] = dsType
            return _FakeAdapter()

        monkeypatch.setattr(ds_module, "build_adapter", fakeBuildAdapter)
        dto = DataSourceTestRequest(**_probeTarget())
        resp = await DataSourceService().test_connection(dto)
        assert resp.success is True
        assert resp.server_version == "8.0.46"
        assert captured["type"] == DataSourceType.POSTGRESQL

    async def test_failed_connection_carries_none_version(self, monkeypatch) -> None:
        class _DeadAdapter:
            async def test(self):
                return False, "拒绝连接", None

            async def dispose(self):
                pass

        monkeypatch.setattr(ds_module, "build_adapter", lambda *a, **k: _DeadAdapter())
        dto = DataSourceTestRequest(**_probeTarget())
        resp = await DataSourceService().test_connection(dto)
        assert resp.success is False
        assert resp.server_version is None
