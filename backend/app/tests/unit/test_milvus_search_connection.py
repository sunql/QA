"""Milvus 检索入口必须自建连接（否则新进程的首次检索必失败）。

背景（2026-09-30 实测，生产容器 `qa-backend`）：`searchEmbeddingsByTypeRouted` /
`_searchCollection` 只做 `Collection(name, using=_connAlias())`，**从不调用
`_connect()`**。pymilvus 的 ORM 接口要求连接别名先注册，否则抛
`ConnectionNotExistException: should create connection first.`。

于是「第一次向量检索能否成功」取决于**别的**代码路径（`ensureQueryCollection` /
`checkHealth` / 本体向量同步）有没有抢先把连接建起来。容器重建后进程是全新的，
第一条 chat 提问大概率抢在前面 —— 实测 13:00:21 第一次提问：

    WARNING app.services.chat_recall: 本体类裁剪检索失败，进入降级路径
    pymilvus.exceptions.ConnectionNotExistException: should create connection first.
    app.domain.exceptions.MilvusError: 向量检索失败
    WARNING app.services.chat_recall: 本体类回退降级 reason=search_error total=32 kept=12

用户可见后果：前端 `classRecallFallback` 文案
「数据表智能召回暂不可用，本次已按数仓分层顺序选取数据表…」，召回退化为按层截断，
且此后**同一进程**的检索又正常（连接已被别的路径建起）——典型的「首问必降级、
后续正常」间歇症状，最容易被当成偶发抖动放过。

同目录的 `milvus_query_helpers._ensureCollectionByName` 与
`milvus_dual_write.rebuildOntologyCollections` 都是先 `_connect()` 再拿句柄，
只有 `milvus_search` 这个读取热点漏了。

用假 Collection 而非真实 Milvus：真集合夹具会 drop 线上本体向量
（同 test_milvus_search_dedup.py 的理由）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pymilvus.exceptions import ConnectionNotExistException

import app.infrastructure.milvus_search as milvus_search

_DIM = 4
_COLLECTION_NAMES = (
    "ontology_class_embeddings",
    "ontology_property_embeddings",
    "ontology_metric_embeddings",
)


class _FakeHit:
    def __init__(self, row: dict, distance: float) -> None:
        self.distance = distance
        self.entity = SimpleNamespace(get=lambda k: row.get(k))


def _row(ontologyId: int = 7001) -> dict:
    return {
        "ontology_id": ontologyId,
        "type": "class",
        "name": "收货明细",
        "alias": "收货",
        "description": "收货事实表",
    }


class _FakeCollection:
    """最小 Collection 替身：每次检索返回一条固定命中。"""

    def __init__(self) -> None:
        self.loadCount = 0

    def load(self) -> None:
        self.loadCount += 1

    def search(self, *, data, anns_field, param, limit, output_fields):
        return [[_FakeHit(_row(), 0.25)]]


class _AliasRegistry:
    """模拟 pymilvus 的连接别名注册表。

    未注册别名就构造 Collection → 抛 ``ConnectionNotExistException``，
    与真实 pymilvus 一致（已实测确认语义相同）。
    """

    def __init__(self) -> None:
        self.isConnected = False
        self.connectCount = 0
        self.collectionNames: list[str] = []

    def connect(self) -> None:
        self.isConnected = True
        self.connectCount += 1

    def buildCollection(self, name: str, using: str | None = None) -> _FakeCollection:
        if not self.isConnected:
            raise ConnectionNotExistException(
                message="should create connection first."
            )
        self.collectionNames.append(name)
        return _FakeCollection()


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> _AliasRegistry:
    """把检索入口的建连与 Collection 构造都换成可控替身。

    ``raising=False``：被测模块当前不 import ``_connect`` 正是本缺陷的一部分，
    夹具不该因此在 RED 阶段抛 AttributeError 而不是暴露真实症状。
    """
    reg = _AliasRegistry()
    monkeypatch.setattr(milvus_search, "_connect", reg.connect, raising=False)
    monkeypatch.setattr(milvus_search, "Collection", reg.buildCollection)
    return reg


def test_search_connects_when_alias_not_registered(registry) -> None:
    """新进程（别名未注册）里检索必须自己建连，而不是把 ConnectionNotExist 抛给调用方。"""
    hits = milvus_search.searchEmbeddingsByTypeRouted(
        [0.0] * _DIM, topK=3, typeFilter="class"
    )

    assert registry.isConnected is True
    assert hits == [
        {
            "ontology_id": 7001,
            "type": "class",
            "name": "收货明细",
            "alias": "收货",
            "description": "收货事实表",
            "distance": 0.25,
        }
    ]


def test_search_connects_once_for_all_three_collections(registry) -> None:
    """typeFilter=None 跨 3 个集合：只建连一次，不做 3 遍连接健康检查。"""
    milvus_search.searchEmbeddingsByTypeRouted([0.0] * _DIM, topK=3)

    assert registry.connectCount == 1
    assert tuple(registry.collectionNames) == _COLLECTION_NAMES


def test_search_always_goes_through_connect_even_when_alias_registered(registry) -> None:
    """别名已注册也要走 `_connect()` 快路径 —— 那是「连接已失效则重连」的唯一入口。

    自建连不能写成 `if not has_connection(...)`：那样 Milvus 重启后（别名还在、
    连接已死）检索会一直失败且永不重连。
    """
    registry.isConnected = True

    milvus_search.searchEmbeddingsByTypeRouted([0.0] * _DIM, topK=1, typeFilter="metric")

    assert registry.connectCount == 1
