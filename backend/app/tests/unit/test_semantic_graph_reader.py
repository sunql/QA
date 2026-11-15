"""「按发布读图」路由器（P3b D25）的单测。

读侧切换的**机制**本体：开关（`SEMANTIC_GRAPH_RELEASE_ID`）打开 ⇒ 本体读
路由到**发布钉住的镜像**；关闭（默认）⇒ 路由到主图。一键回退 = 把开关拿掉。

本文件只测**路由与代理行为**（底层读函数用替身，不连数据库）——
镜像读函数的 CQL 形态在 `test_neo4j_mirror_client.py`。
"""

from __future__ import annotations

import pytest

from app.config import getSettings
from app.services import semantic_graph_reader as reader


@pytest.fixture(autouse=True)
def _resetSettingsCache():
    """每个用例前后都清 settings 缓存（环境变量互不串味）。"""
    getSettings.cache_clear()
    yield
    getSettings.cache_clear()


def test_开关关闭时路由到主图(monkeypatch) -> None:
    monkeypatch.delenv("SEMANTIC_GRAPH_RELEASE_ID", raising=False)
    r = reader.resolveOntologyGraphReader()
    assert r.mode == "main"
    assert r.releaseId is None


def test_开关打开时路由到发布镜像(monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_GRAPH_RELEASE_ID", "1")
    r = reader.resolveOntologyGraphReader()
    assert r.mode == "release"
    assert r.releaseId == 1


def test_开关给_0_视同关闭(monkeypatch) -> None:
    """0/空 = 不钉住 ⇒ 主图（运维误设 0 不应该炸或钉到不存在的发布）。"""
    monkeypatch.setenv("SEMANTIC_GRAPH_RELEASE_ID", "0")
    r = reader.resolveOntologyGraphReader()
    assert r.mode == "main"


def test_开关非法值要报错_不能静默当主图(monkeypatch) -> None:
    """「按发布读图」设了个不是数字的东西 ⇒ 显式炸掉（静默回主图 = 假切换）。"""
    monkeypatch.setenv("SEMANTIC_GRAPH_RELEASE_ID", "abc")
    with pytest.raises(ValueError):
        reader.resolveOntologyGraphReader()


def test_主图_reader_代理主图读函数(monkeypatch) -> None:
    monkeypatch.delenv("SEMANTIC_GRAPH_RELEASE_ID", raising=False)
    calls: list[str] = []

    def _fakeMain(name: str):
        def _fn(*args, **kwargs):
            calls.append(name)
            return {"name"}
        return _fn

    monkeypatch.setattr(reader.neo4j, "getClassIds", _fakeMain("getClassIds"))
    monkeypatch.setattr(reader.neo4j, "getPropertyIds", _fakeMain("getPropertyIds"))
    monkeypatch.setattr(reader.neo4j, "getMetricIds", _fakeMain("getMetricIds"))
    monkeypatch.setattr(reader.neo4j, "getKpiCatalogIds", _fakeMain("getKpiCatalogIds"))
    monkeypatch.setattr(reader.neo4j, "getJoinPairs", _fakeMain("getJoinPairs"))
    monkeypatch.setattr(reader.neo4j, "getRelationTriples", _fakeMain("getRelationTriples"))
    r = reader.resolveOntologyGraphReader()
    assert r.classIds() == {"name"}
    assert r.joinPairs() == {"name"}
    assert r.relationTriples() == {"name"}
    assert set(calls) == {"getClassIds", "getJoinPairs", "getRelationTriples"}


def test_发布_reader_代理镜像读函数_钉住同一发布(monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_GRAPH_RELEASE_ID", "1")
    seen: list[int] = []

    def _fakeMirror(name: str):
        def _fn(releaseId: int, *args, **kwargs):
            seen.append(releaseId)
            return {"mirror"}
        return _fn

    monkeypatch.setattr(reader.mirror, "getReleaseClassIds", _fakeMirror("class"))
    monkeypatch.setattr(reader.mirror, "getReleasePropertyIds", _fakeMirror("property"))
    monkeypatch.setattr(reader.mirror, "getReleaseMetricIds", _fakeMirror("metric"))
    monkeypatch.setattr(reader.mirror, "getReleaseKpiCatalogIds", _fakeMirror("kpi"))
    monkeypatch.setattr(reader.mirror, "getReleaseJoinPairs", _fakeMirror("join"))
    monkeypatch.setattr(reader.mirror, "getReleaseRelationTriples", _fakeMirror("rel"))
    r = reader.resolveOntologyGraphReader()
    assert r.classIds() == {"mirror"}
    assert r.joinPairs() == {"mirror"}
    assert r.relationTriples() == {"mirror"}
    assert seen and set(seen) == {1}


def test_一键回退_开关拿掉后回到主图(monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_GRAPH_RELEASE_ID", "1")
    assert reader.resolveOntologyGraphReader().mode == "release"
    # settings 是进程级缓存 ⇒ 环境变量变化后要重载（真实运维 = 重启服务），
    # 这里 cache_clear 等价于重载。
    monkeypatch.delenv("SEMANTIC_GRAPH_RELEASE_ID", raising=False)
    getSettings.cache_clear()
    assert reader.resolveOntologyGraphReader().mode == "main"


def test_settings_声明了发布开关字段() -> None:
    """配置字段存在且有默认 None（不钉住 = 主图，与现状一致）。"""
    import os

    os.environ.pop("SEMANTIC_GRAPH_RELEASE_ID", None)
    getSettings.cache_clear()
    assert getSettings().semanticGraphReleaseId is None
