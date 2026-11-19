"""Milvus 检索按 ontology_id 去重（topK 窗口不被重复实体吃光）。

背景（2026-09-30 线上召回事故）：`ontology_class_embeddings` 残留重复实体
（101 行 / 32 类，每类 3-4 份，delete-then-insert 在批量负载下不可靠）。
Milvus 按距离取 topK，重复项彼此距离为 0，于是 topK=15 的窗口被 4-5 个
*不同的* 类占满——业务真正需要的类（收货明细 / 供应商）被挤出候选集，
LLM 只能幻觉类名，validatePlan 判「不在本体 schema 中」→ 多步全部失败。

修复：`_searchCollection` 放大 limit 后按 ontology_id 去重（保留最近的一份），
再截断到 topK。数据侧另有 scripts/backfill_milvus_embeddings.py --cleanup 收敛。

用 FakeCollection 而非真实 Milvus：integration 的 milvusCleanClient 夹具会
drop 真实本体集合（删掉线上向量），去重逻辑不该靠破坏数据来验证。
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

# 先导 milvus_client 再导 milvus_search：milvus_client 末尾反向 import 了
# milvus_search（见其文件尾说明），先导 milvus_search 会在收集期撞循环导入。
from app.infrastructure.milvus_client import (
    _HNSW_EF_FLOOR,
    hnswSearchParams,
)
import app.infrastructure.milvus_search as milvus_search
from app.infrastructure.milvus_search import (
    _DEDUP_OVERFETCH,
    _searchCollection,
)

_DIM = 8


def _vec(coordinate: int) -> list[float]:
    """基向量：坐标处 1.0，其余 0.0（正交 → L2 距离确定）。"""
    v = [0.0] * _DIM
    v[coordinate] = 1.0
    return v


class _FakeHit:
    def __init__(self, row: dict, distance: float) -> None:
        self.distance = distance
        self.entity = SimpleNamespace(get=lambda k: row.get(k))


class _FakeCollection:
    """最小 Collection 替身：服务端按距离排序 + 截断 limit（与 Milvus 同语义）。"""

    def __init__(self, rows: list[dict], queryVector: list[float]) -> None:
        self._rows = rows
        self._query = queryVector
        self.receivedLimit: int | None = None
        self.receivedParam: dict | None = None

    def load(self) -> None:
        pass

    def search(self, *, data, anns_field, param, limit, output_fields, expr=None):
        self.receivedLimit = limit
        self.receivedParam = param
        scored = [
            (math.dist(self._query, r["embedding"]), r) for r in self._rows
        ]
        scored.sort(key=lambda x: x[0])
        return [[_FakeHit(r, d) for d, r in scored[:limit]]]


@pytest.fixture()
def fakeSearch(monkeypatch: pytest.MonkeyPatch):
    """装配一个可控的 Collection：返回 (rows, query) 设置器。"""

    def _configure(rows: list[dict], queryVector: list[float]) -> _FakeCollection:
        fake = _FakeCollection(rows, queryVector)
        monkeypatch.setattr(milvus_search, "Collection", lambda *a, **kw: fake)
        return fake

    return _configure


def _dupRows() -> list[dict]:
    """oid 3001 有三份重复（同一向量），3002/3003 各一份，距离递增。"""
    return [
        *[{"ontology_id": 3001, "type": "class", "name": "C1",
           "alias": "", "description": "", "embedding": _vec(0)} for _ in range(3)],
        {"ontology_id": 3002, "type": "class", "name": "C2",
         "alias": "", "description": "", "embedding": _vec(1)},
        {"ontology_id": 3003, "type": "class", "name": "C3",
         "alias": "", "description": "", "embedding": _vec(2)},
    ]


class TestSearchCollectionDedup:
    def test_duplicate_entities_do_not_consume_topK_window(self, fakeSearch) -> None:
        """topK=2：重复项不得占满窗口，应返回 2 个 *不同* 的类。"""
        fakeSearch(_dupRows(), _vec(0))

        hits = _searchCollection("ontology_class_embeddings", _vec(0), topK=2)

        ids = [h["ontology_id"] for h in hits]
        assert ids == [3001, 3002], f"应按距离取两个不同的类，实际 {ids}"
        assert len(ids) == len(set(ids))
        # 去重保留的是最近的一份（3001 与查询同向量 → 距离 0）
        assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-9)

    def test_overfetches_beyond_topK_to_fill_window(self, fakeSearch) -> None:
        """去重要求多取：请求 Milvus 的 limit 必须大于 topK。"""
        fake = fakeSearch(_dupRows(), _vec(0))

        _searchCollection("ontology_class_embeddings", _vec(0), topK=2)

        assert fake.receivedLimit == 2 * _DEDUP_OVERFETCH

    def test_truncates_to_topK_when_more_distinct_than_topK(self, fakeSearch) -> None:
        """去重后不同类多于 topK 时，仍按距离截断到 topK。"""
        fakeSearch(_dupRows(), _vec(0))

        hits = _searchCollection("ontology_class_embeddings", _vec(0), topK=1)

        assert [h["ontology_id"] for h in hits] == [3001]

    def test_no_duplicates_behaves_as_before(self, fakeSearch) -> None:
        """回归：无重复时结果与修复前一致（距离升序、条数不超过 topK）。"""
        rows = [
            {"ontology_id": i, "type": "class", "name": f"C{i}",
             "alias": "", "description": "", "embedding": _vec(i)}
            for i in range(5)
        ]
        fakeSearch(rows, _vec(0))

        hits = _searchCollection("ontology_class_embeddings", _vec(0), topK=3)

        assert [h["ontology_id"] for h in hits] == [0, 1, 2]
        distances = [h["distance"] for h in hits]
        assert distances == sorted(distances)


class TestSearchParamEfInvariant:
    """HNSW 服务端校验 ef >= limit；ef 写死 64 而 limit = topK×8 ⇒ topK>8 必炸。

    线上（2026-11-19）：wiki 注入 property 召回 topK=10 → limit=80 > ef=64 →
    MilvusException(code=65535, ef(64) should be larger than k(80))，每轮 chat
    必现，被 chat_service 吞成 warning——property/metric 注入静默失效。
    类召回能活是因为生产 CLASS_FILTER_TOP_K ≤ 8 恰好压线，不是设计使然。
    """

    def test_ef_tracks_limit_when_overfetch_pushes_past_floor(self, fakeSearch) -> None:
        """topK=10（线上默认）：limit=80，ef 必须 ≥ 80 而非写死 64。"""
        fake = fakeSearch(_dupRows(), _vec(0))

        _searchCollection("ontology_property_embeddings", _vec(0), topK=10)

        assert fake.receivedLimit == 10 * _DEDUP_OVERFETCH
        assert fake.receivedParam["params"]["ef"] >= fake.receivedLimit, (
            f"ef={fake.receivedParam['params']['ef']} < limit={fake.receivedLimit}，"
            "Milvus 服务端必拒（ef should be larger than k）"
        )

    def test_ef_keeps_floor_for_small_topK(self, fakeSearch) -> None:
        """小 topK：ef ≥ 下限保持，不变量同样成立。"""
        fake = fakeSearch(_dupRows(), _vec(0))

        _searchCollection("ontology_class_embeddings", _vec(0), topK=2)

        assert fake.receivedLimit == 2 * _DEDUP_OVERFETCH
        assert fake.receivedParam["params"]["ef"] >= max(
            _HNSW_EF_FLOOR, fake.receivedLimit
        )


class TestHnswSearchParamsHelper:
    """helper 直接单测：未来任何调用点重新写死 {"ef": 64} 时，
    此处钉住的语义（ef 随 limit 收敛、不低于下限）仍可作为对照基准。"""

    def test_ef_tracks_large_limit(self) -> None:
        assert hnswSearchParams(80)["params"]["ef"] >= 80

    def test_ef_floors_small_limit(self) -> None:
        assert hnswSearchParams(5)["params"]["ef"] == _HNSW_EF_FLOOR

    def test_metric_type_is_l2(self) -> None:
        assert hnswSearchParams(10)["metric_type"] == "L2"
