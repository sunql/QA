"""Milvus 连接按 database 隔离（防测试 drop 打到生产集合）。

回归 2026-09-30：integration 的 milvusCleanClient 夹具 drop 的是**真实容器上的
本体集合**，而连接挂在默认库 = 生产库 —— 一次裸 pytest 就把线上向量删空。
现在 MILVUS_DB_NAME 决定连接挂哪个逻辑库（空 → "default"），且**配置变了要重连**，
否则被缓存的默认库连接会把「测试库」配置静默吞掉，drop 依旧打回生产。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.infrastructure.milvus_client as milvus_client
from app.tests import milvus_isolation


@pytest.fixture()
def fakeMilvusConn(monkeypatch: pytest.MonkeyPatch):
    """隔离 pymilvus：记录 connect/disconnect 调用，has_connection 可控。"""
    monkeypatch.setattr(milvus_client, "_connectedDbName", None)
    events: list[tuple] = []
    state = {"hasConnection": False}

    monkeypatch.setattr(
        milvus_client.connections, "has_connection", lambda alias: state["hasConnection"]
    )

    def _connect(**kwargs):
        events.append(("connect", kwargs.get("db_name")))
        state["hasConnection"] = True

    monkeypatch.setattr(milvus_client.connections, "connect", _connect)
    monkeypatch.setattr(
        milvus_client.connections, "disconnect",
        lambda alias: (events.append(("disconnect", alias)), state.update(hasConnection=False)),
    )
    monkeypatch.setattr(milvus_client.utility, "list_collections", lambda using=None: [])
    monkeypatch.setattr(milvus_client, "_getUri", lambda: "http://localhost:19530")
    return events, monkeypatch


def _setDbName(monkeypatch, value: str) -> None:
    monkeypatch.setattr(
        milvus_client, "getSettings", lambda: SimpleNamespace(milvusUri="http://x:19530", milvusDbName=value),
    )


class TestDbNameResolution:
    def test_empty_setting_means_default_database(self, fakeMilvusConn) -> None:
        events, monkeypatch = fakeMilvusConn
        _setDbName(monkeypatch, "")

        milvus_client._connect()

        assert events == [("connect", "default")]

    def test_named_database_is_passed_through(self, fakeMilvusConn) -> None:
        events, monkeypatch = fakeMilvusConn
        _setDbName(monkeypatch, "qa_test")

        milvus_client._connect()

        assert events == [("connect", "qa_test")]


class TestGuardRefusesDefaultDatabase:
    """闸门本身必须被测：它才是「不静默删生产」的最后一道防线。

    上面那组用例只覆盖 _connect 的透传/重连；即便有人把 _dropOntologyCollections
    里的 guardIsolatedDatabase() 调用删掉，它们照样全绿，而裸 pytest 会重新
    删空线上向量 —— 正是本次事故的回归类。
    """

    def test_refuses_when_database_is_default(self, fakeMilvusConn) -> None:
        _, monkeypatch = fakeMilvusConn
        _setDbName(monkeypatch, "")

        with pytest.raises(RuntimeError, match="默认库"):
            milvus_isolation.guardIsolatedDatabase()

    def test_refuses_when_database_is_explicitly_default(self, fakeMilvusConn) -> None:
        _, monkeypatch = fakeMilvusConn
        _setDbName(monkeypatch, "default")

        with pytest.raises(RuntimeError):
            milvus_isolation.guardIsolatedDatabase()

    def test_allows_isolated_database(self, fakeMilvusConn) -> None:
        """反向用例：独立库不能被闸门误拦（否则测试套件整体不可跑）。"""
        _, monkeypatch = fakeMilvusConn
        _setDbName(monkeypatch, "qa_test")

        milvus_isolation.guardIsolatedDatabase()  # 不抛即通过

    def test_destructive_drop_refuses_before_touching_milvus(self, fakeMilvusConn) -> None:
        """drop 入口必须自带闸门：默认库下抛错，且不发起任何连接。"""
        events, monkeypatch = fakeMilvusConn
        _setDbName(monkeypatch, "")

        with pytest.raises(RuntimeError):
            milvus_isolation.dropOntologyCollectionsForTest()

        assert events == [], "闸门必须拦在连接/删集合之前"


class TestTestProcessIsPinnedToIsolatedDatabase:
    """回归 2026-09-30：根 conftest 把 MILVUS_DB_NAME 钉到独立库。

    若有人把 app/tests/conftest.py 的赋值改回 os.environ.setdefault(...)，外部
    未设该变量时测试进程会落回默认库（= 生产）；本用例即为此而设。用真实
    getSettings()（不做 monkeypatch），测的就是进程实际生效的配置。
    """

    def test_pinned_database_is_not_production(self) -> None:
        from app.config import getSettings

        dbName = getSettings().milvusDbName
        assert dbName and dbName != "default", (
            f"测试进程的 Milvus database 落在了默认库（{dbName or '空'}）："
            "破坏性夹具会删生产本体向量。见 app/tests/conftest.py 的 MILVUS_DB_NAME 赋值。"
        )


class TestDbNameSwitchForcesReconnect:
    def test_reconnects_when_database_setting_changes(self, fakeMilvusConn) -> None:
        """复用连接的快路径必须比 database：配置变更后被吞掉就删回生产集合。"""
        events, monkeypatch = fakeMilvusConn
        _setDbName(monkeypatch, "qa_test")
        milvus_client._connect()
        events.clear()

        _setDbName(monkeypatch, "")
        milvus_client._connect()

        assert events[0][0] == "disconnect"
        assert events[-1] == ("connect", "default")

    def test_reuses_connection_when_database_unchanged(self, fakeMilvusConn) -> None:
        events, monkeypatch = fakeMilvusConn
        _setDbName(monkeypatch, "qa_test")
        milvus_client._connect()
        events.clear()

        milvus_client._connect()

        assert events == []
