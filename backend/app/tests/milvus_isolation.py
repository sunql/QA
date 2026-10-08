"""测试用 Milvus 库隔离策略（独立成模块，便于单元测试覆盖）。

回归 2026-09-30：integration 的破坏性清理夹具 drop 的是**真实容器上的本体集合**，
而连接挂在默认库 = 生产库 —— 一次裸 pytest 就把线上向量删空，症状是「向量库信息
不完整 / 语义召回无命中」。

政策两条：
1. 测试进程连独立逻辑库（MILVUS_DB_NAME，根 conftest 钉为 qa_test）。
2. 清理前过闸：不是独立库就大声失败，绝不静默删。

**为什么不在 conftest 里就地实现**：闸门是「不删生产」的最后一道防线，必须能被
单元测试直接调用。写在 conftest 里就只能靠「跑一次 integration 看看」来验证，
而删掉闸门调用同样会让 integration 全绿 —— 正是本模块要堵的回归类。

**连接级 vs 按调用 db_name**：本仓库的 Milvus 2.4.6 只认连接级 db_name，
`MilvusClient.query(..., db_name=...)` 这类按调用参数会被静默忽略并落回默认库。
核对隔离效果时务必走 `connections.connect(db_name=...)`（即下面 `_connect` 的路径）。
"""

from __future__ import annotations

from app.config import getSettings
from app.infrastructure.milvus_client import (
    _connAlias,
    _connect,
    dropCollection,
    ensureClassCollection,
    ensureMetricCollection,
    ensurePropertyCollection,
    getDbName,
)
from pymilvus import Collection, utility

# Milvus 默认逻辑库名（生产就在这个库里）。
DEFAULT_DATABASE_NAME = "default"

# M2 起本体的 3 个类型化集合。
_ONTOLOGY_COLLECTIONS = (
    "ontology_class_embeddings",
    "ontology_property_embeddings",
    "ontology_metric_embeddings",
)


def guardIsolatedDatabase() -> None:
    """拒绝在 Milvus 默认库（生产）上做破坏性清理。

    取的是 ``getDbName()`` —— 也就是 ``_connect()`` 实际会挂的库，保证「闸门放行的库」
    与「连接真正连的库」是同一个（而非各算各的）。
    """
    if getDbName() == DEFAULT_DATABASE_NAME:
        raise RuntimeError(
            "拒绝执行 Milvus 破坏性清理：当前 database 是默认库（生产）。"
            "测试进程必须设 MILVUS_DB_NAME 指向独立测试库（见 app/tests/conftest.py）。"
        )


def ensureIsolatedDatabase(dbName: str) -> None:
    """确保测试库存在（幂等）。

    走 MilvusClient（新 API）而非 ORM utility：pymilvus 3.0.1 的 ORM utility
    已没有 list_databases/create_database。库不存在时无法先连它，故用独立客户端
    在默认库上建库。
    """
    from pymilvus import MilvusClient

    client = MilvusClient(uri=getSettings().milvusUri)
    try:
        if dbName not in client.list_databases():
            client.create_database(dbName)
    finally:
        client.close()


def dropOntologyCollectionsForTest() -> None:
    """删除并重建 3 个本体集合（idempotent；集合不存在 no-op）。

    终点态是「3 个空集合」：reconcile 类测试要求集合存在但无数据。
    入口第一件事就是过闸（``guardIsolatedDatabase``），默认库下在**发起任何连接之前**
    即抛错 —— 见单测 test_destructive_drop_refuses_before_touching_milvus。
    """
    guardIsolatedDatabase()
    ensureIsolatedDatabase(getDbName())
    _connect()

    # 旧单集合（Task 14 后已是历史包袱，drop 失败无所谓）
    try:
        dropCollection()  # drops _COLLECTION_NAME = "ontology_embeddings"
    except Exception:
        pass  # idempotent

    for name in _ONTOLOGY_COLLECTIONS:
        try:
            if utility.has_collection(name, using=_connAlias()):
                Collection(name, using=_connAlias()).drop()
        except Exception:
            pass  # idempotent

    # 重建空集合，使 reconcile() 即使在全空状态下也能查询
    ensureClassCollection()
    ensurePropertyCollection()
    ensureMetricCollection()
