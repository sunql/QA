"""Gap A：向量检索按**本地类 id** 收敛到 Milvus ``expr``。

缺陷（生产实证 2026-11-19）：数据源过滤只作用在**应用侧候选集**（304 全局类筛到
94 个本数据源类），而**向量检索窗口仍是全局的** ⇒ top20 名额被跨源类、墓碑类吃光
⇒ ``hits=20 total=94`` 里能解析到本地类的为 0 ⇒ 走 ``no_match`` 早退。

本批把「本地类 id 集」推进 Milvus 的 ``expr`` ⇒ 向量窗口与候选集口径一致。

三条不可让步的口径（本文件逐条钉死）
------------------------------------
1. **``None`` = 不限**（不施加过滤）⇒ 与今天逐字节一致。
2. **空集 = deny-all**（恒空）——**绝不能**当成「不限」：
   「作用域里有 0 个类」与「没有作用域」是两件事。
3. **作用域只作用于 ``typeFilter == "class"`` 的集合**：Milvus 的 ``ontology_id``
   在类/属性之间**共用 id 序列**（见 ``milvus_client.VALID_EMBEDDING_TYPES`` 注释）
   ⇒ 把 class 的 id 集套到 property 集合上会误伤。

全部用 fake —— 不连 Milvus（本地 19530 是生产数据面，禁跑真连接）。
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

# 先导 milvus_client 再导 milvus_search：milvus_client 末尾反向 import 了
# milvus_search（见其文件尾说明），先导 milvus_search 会在收集期撞循环导入。
from app.infrastructure.milvus_client import buildClassIdScopeExpr
import app.infrastructure.milvus_search as milvus_search
import app.infrastructure.milvus_registry_v2 as milvus_registry_v2
from app.services import semantic_registry_rag_gate as gate

_DIM = 8


def _vec(coordinate: int) -> list[float]:
    v = [0.0] * _DIM
    v[coordinate] = 1.0
    return v


class _FakeHit:
    def __init__(self, row: dict, distance: float) -> None:
        self.distance = distance
        self.entity = SimpleNamespace(get=lambda k: row.get(k))


class _FakeCollection:
    """最小 Collection 替身：记录收到的 expr，**按 expr 过滤后**按距离排序 + 截断。

    替身必须**真的执行 expr 过滤**：本批要复现的生产症状正是「跨源类占满 topK
    名额」——若替身无视 expr，这条症状就复现不出来，测试会假绿。
    """

    def __init__(self, rows: list[dict], queryVector: list[float]) -> None:
        self._rows = rows
        self._query = queryVector
        self.receivedLimit: int | None = None
        self.receivedExpr: str | None = None

    def load(self) -> None:
        pass

    def search(self, *, data, anns_field, param, limit, output_fields, expr=None):
        self.receivedLimit = limit
        self.receivedExpr = expr
        kept = [row for row in self._rows if _matchesExpr(row, expr)]
        scored = [(math.dist(self._query, r["embedding"]), r) for r in kept]
        scored.sort(key=lambda x: x[0])
        return [[_FakeHit(r, d) for d, r in scored[:limit]]]


def _matchesExpr(row: dict, expr: str | None) -> bool:
    """替身的 expr 求值：只认作用域两种形态，治理维度 expr 一律放行。

    治理 expr（``domain_codes like [...]`` 等）本批不测 ⇒ 放行即可；
    作用域 expr（``ontology_id in [...]`` / ``ontology_id < 0``）必须真判。
    """
    if not expr:
        return True
    for part in expr.split(" and "):
        clause = part.strip().strip("()").strip()
        if clause.startswith("ontology_id in ["):
            ids = {
                int(token)
                for token in clause[len("ontology_id in ["):-1].split(",")
                if token.strip()
            }
            if row["ontology_id"] not in ids:
                return False
        elif clause == "ontology_id < 0":
            if not row["ontology_id"] < 0:
                return False
    return True


@pytest.fixture()
def fakeSearch(monkeypatch: pytest.MonkeyPatch):
    """装配一个可控的 Collection 替身（按集合名分别记录）。

    **同时切断真连接**：本地 Milvus 19530 是生产数据面 ⇒ 本文件一律不连。
    ``_connect`` / ``hasEmbeddingCollection`` 都是只读探针，但「连上去」这件事
    本身就不该在单元测试里发生（连上了就可能误写真集合）。
    """

    def _configure(rows: list[dict], queryVector: list[float]) -> dict[str, _FakeCollection]:
        fakes: dict[str, _FakeCollection] = {}

        def _factory(name, *a, **kw):
            fake = _FakeCollection(rows, queryVector)
            fakes[name] = fake
            return fake

        monkeypatch.setattr(milvus_search, "Collection", _factory)
        monkeypatch.setattr(milvus_registry_v2, "Collection", _factory)
        monkeypatch.setattr(milvus_search, "_connect", lambda: None)
        monkeypatch.setattr(milvus_search, "hasEmbeddingCollection", lambda name: True)
        monkeypatch.setattr(milvus_registry_v2, "hasEmbeddingCollection", lambda name: True)
        return fakes

    return _configure


def _rows() -> list[dict]:
    return [
        {"ontology_id": 11, "type": "class", "name": "本地类A",
         "alias": "", "description": "", "embedding": _vec(0)},
        {"ontology_id": 22, "type": "class", "name": "跨源类B",
         "alias": "", "description": "", "embedding": _vec(1)},
    ]


# ---------------------------------------------------------------------------
# 1. expr 构造：三态
# ---------------------------------------------------------------------------


class TestBuildClassIdScopeExpr:
    def test_none表示不限(self) -> None:
        """``None`` = 没有作用域 ⇒ 不施加任何过滤（与今天逐字节一致）。"""
        assert buildClassIdScopeExpr(None) is None

    def test_空集是deny_all而不是不限(self) -> None:
        """空集 = 作用域里一个类都没有 ⇒ 恒空。

        **不能**当成「不限」——那是把「本数据源没有类」误读成「全局都能看」。
        """
        expr = buildClassIdScopeExpr([])
        assert expr is not None
        assert expr != buildClassIdScopeExpr(None)
        # deny-all 形态：本体类 id 恒为正 ⇒ 该表达式命中不了任何行
        assert "<" in expr

    def test_非空集拼成in列表(self) -> None:
        assert buildClassIdScopeExpr([3, 1, 2]) == "ontology_id in [1,2,3]"

    def test_重复id去重(self) -> None:
        assert buildClassIdScopeExpr([5, 5, 6]) == "ontology_id in [5,6]"

    def test_顺序稳定可复现(self) -> None:
        """同一集合不同输入顺序 ⇒ 同一表达式（便于日志/缓存比对）。"""
        assert buildClassIdScopeExpr([9, 2]) == buildClassIdScopeExpr([2, 9])


# ---------------------------------------------------------------------------
# 2. v1 路径：_searchCollection 透传 expr
# ---------------------------------------------------------------------------


class TestSearchCollectionExpr:
    def test_expr透传给服务端(self, fakeSearch) -> None:
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_search._searchCollection(
            "ontology_class_embeddings", _vec(0), topK=5, expr="ontology_id in [11]",
        )

        assert fakes["ontology_class_embeddings"].receivedExpr == "ontology_id in [11]"

    def test_不给作用域时expr为None(self, fakeSearch) -> None:
        """不施加作用域 ⇒ 不带 expr（与今天逐字节一致）。"""
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_search._searchCollection("ontology_class_embeddings", _vec(0), topK=5)

        assert fakes["ontology_class_embeddings"].receivedExpr is None


# ---------------------------------------------------------------------------
# 3. v1 路径：type-routed 路由时作用域只落 class 集合
# ---------------------------------------------------------------------------


class TestTypeRoutedScope:
    def test_class类型施加作用域(self, fakeSearch) -> None:
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_search.searchEmbeddingsByTypeRouted(
            _vec(0), topK=5, typeFilter="class", classOntologyIds=[11],
        )

        assert fakes["ontology_class_embeddings"].receivedExpr == "ontology_id in [11]"

    def test_不给作用域时任何集合都不带expr(self, fakeSearch) -> None:
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_search.searchEmbeddingsByTypeRouted(_vec(0), topK=5, typeFilter="class")

        assert fakes["ontology_class_embeddings"].receivedExpr is None

    def test_property类型不受class作用域影响(self, fakeSearch) -> None:
        """Milvus 的 ontology_id 在类/属性之间**共用 id 序列** ⇒ class 的 id 集
        套到 property 集合上会误伤（把 id 相同的属性行滤掉）。"""
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_search.searchEmbeddingsByTypeRouted(
            _vec(0), topK=5, typeFilter="property", classOntologyIds=[11],
        )

        assert fakes["ontology_property_embeddings"].receivedExpr is None

    def test_不限定类型时只对class集合施加(self, fakeSearch) -> None:
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_search.searchEmbeddingsByTypeRouted(
            _vec(0), topK=5, typeFilter=None, classOntologyIds=[11],
        )

        assert fakes["ontology_class_embeddings"].receivedExpr == "ontology_id in [11]"
        assert fakes["ontology_property_embeddings"].receivedExpr is None
        assert fakes["ontology_metric_embeddings"].receivedExpr is None

    def test_空作用域传给class检索时是deny_all(self, fakeSearch) -> None:
        """作用域为空集 ⇒ class 集合拿到 deny-all 表达式（不是「不限」）。"""
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_search.searchEmbeddingsByTypeRouted(
            _vec(0), topK=5, typeFilter="class", classOntologyIds=[],
        )

        expr = fakes["ontology_class_embeddings"].receivedExpr
        assert expr is not None
        assert expr == buildClassIdScopeExpr([])


# ---------------------------------------------------------------------------
# 4. v2 路径：治理维度 expr 与作用域 expr 合并（and），不互相覆盖
# ---------------------------------------------------------------------------


class TestRegistryV2Scope:
    def test_治理expr与作用域expr用and合并(self, fakeSearch) -> None:
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_registry_v2.searchRegistryV2(
            _vec(0), topK=5, typeFilter="class",
            expr="domain_codes like [\"SUP\"]", classOntologyIds=[11],
        )

        expr = fakes["ontology_class_embeddings_v2"].receivedExpr
        assert expr is not None
        assert "domain_codes" in expr
        assert "ontology_id in [11]" in expr
        assert " and " in expr

    def test_无治理expr时只带作用域expr(self, fakeSearch) -> None:
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_registry_v2.searchRegistryV2(
            _vec(0), topK=5, typeFilter="class", expr=None, classOntologyIds=[11],
        )

        assert fakes["ontology_class_embeddings_v2"].receivedExpr == "ontology_id in [11]"

    def test_不限定类型时作用域只落class集合(self, fakeSearch) -> None:
        fakes = fakeSearch(_rows(), _vec(0))

        milvus_registry_v2.searchRegistryV2(
            _vec(0), topK=5, typeFilter=None, expr=None, classOntologyIds=[11],
        )

        assert fakes["ontology_class_embeddings_v2"].receivedExpr == "ontology_id in [11]"
        assert fakes["ontology_property_embeddings_v2"].receivedExpr is None


# ---------------------------------------------------------------------------
# 5. 生产症状复现：跨源类占满 topK ⇒ 命中解析不出本地类
# ---------------------------------------------------------------------------


class TestProductionSymptom:
    """复现 2026-11-19 生产日志 ``hits=20 total=94`` 的成因。

    场景：304 个类里，前 20 个最像问句的**全是跨源类/墓碑类**（向量检索窗口
    是全局的）⇒ 候选集虽已按数据源收窄到 94 个本地类，但向量命中里能解析到
    本地类的为 0 ⇒ ``reason=no_match`` 早退 ⇒ 走 Gap B 救援/兜底。

    施加作用域后：向量窗口只含本地类 ⇒ 命中解析得出类 ⇒ 走正常裁剪路径。
    """

    @staticmethod
    def _productionRows() -> list[dict]:
        """20 个跨源类（向量上最像）+ 2 个本地类（向量上较远）。"""
        rows: list[dict] = []
        for index in range(20):
            rows.append({
                "ontology_id": 1000 + index, "type": "class",
                "name": f"跨源类{index}", "alias": "", "description": "",
                "embedding": _vec(0),
            })
        rows.append({
            "ontology_id": 11, "type": "class", "name": "本地类A",
            "alias": "", "description": "", "embedding": _vec(1),
        })
        rows.append({
            "ontology_id": 12, "type": "class", "name": "本地类B",
            "alias": "", "description": "", "embedding": _vec(2),
        })
        return rows

    def test_无作用域时跨源类占满窗口本地类一个都进不来(self, fakeSearch) -> None:
        """今天的症状：不施加作用域 ⇒ topK 全是跨源类 ⇒ 本地类 0 命中。"""
        fakeSearch(self._productionRows(), _vec(0))

        hits = milvus_search.searchEmbeddingsByTypeRouted(
            _vec(0), topK=20, typeFilter="class",
        )

        assert len(hits) == 20
        assert not [h for h in hits if h["ontology_id"] in (11, 12)]

    def test_施加作用域后窗口只含本地类(self, fakeSearch) -> None:
        """施加作用域 ⇒ 跨源类出局，本地类进得来（根因修复点）。"""
        fakeSearch(self._productionRows(), _vec(0))

        hits = milvus_search.searchEmbeddingsByTypeRouted(
            _vec(0), topK=20, typeFilter="class", classOntologyIds=[11, 12],
        )

        assert [h["ontology_id"] for h in hits] == [11, 12]

    def test_空作用域恒空而不是回落全量(self, fakeSearch) -> None:
        """作用域为空集 ⇒ 恒空（**不**回落成「不限」）。

        回落成「不限」= 把「本数据源没有类」误读成「全局都能看」⇒ 跨源类又回来了。
        """
        fakeSearch(self._productionRows(), _vec(0))

        hits = milvus_search.searchEmbeddingsByTypeRouted(
            _vec(0), topK=20, typeFilter="class", classOntologyIds=[],
        )

        assert hits == []


# ---------------------------------------------------------------------------
# 6. 闸门：作用域透传到 v1 / v2
# ---------------------------------------------------------------------------


class TestGateScopeForwarding:
    def test_v1路径透传作用域(self, monkeypatch) -> None:
        seen: dict[str, Any] = {}

        def _fake(queryEmbedding, topK=5, typeFilter=None, classOntologyIds=None):
            seen["classOntologyIds"] = classOntologyIds
            return []

        monkeypatch.setattr(
            gate, "getSettings", lambda: SimpleNamespace(enableSemanticRegistryRag=False)
        )
        monkeypatch.setattr(gate.milvus, "searchEmbeddingsByTypeRouted", _fake)

        gate.searchRegistryEmbeddings([0.1] * 4, topK=5, classOntologyIds=[11, 12])

        assert seen["classOntologyIds"] == [11, 12]

    def test_v2路径透传作用域(self, monkeypatch) -> None:
        seen: dict[str, Any] = {}

        def _fakeHas(type_: str) -> bool:
            return True

        def _fakeSearch(queryEmbedding, topK=5, typeFilter=None, expr=None,
                        classOntologyIds=None):
            seen["classOntologyIds"] = classOntologyIds
            return []

        monkeypatch.setattr(
            gate, "getSettings", lambda: SimpleNamespace(enableSemanticRegistryRag=True)
        )
        monkeypatch.setattr(gate, "hasRegistryV2Collection", _fakeHas)
        monkeypatch.setattr(gate, "searchRegistryV2", _fakeSearch)

        gate.searchRegistryEmbeddings([0.1] * 4, topK=5, classOntologyIds=[11])

        assert seen["classOntologyIds"] == [11]

    def test_不给作用域时调用形态不变(self, monkeypatch) -> None:
        """**不施加作用域时，调用形态逐字不变**（不向下游多传 kwarg）。

        这样既有替身（只认 ``(queryEmbedding, topK, typeFilter)``）不必改，
        也保证「无作用域 = 与今天完全一致」这条不变量在调用层就成立。
        """
        calls: list[dict[str, Any]] = []

        def _fake(queryEmbedding, topK=5, typeFilter=None):
            calls.append({"topK": topK, "typeFilter": typeFilter})
            return []

        monkeypatch.setattr(
            gate, "getSettings", lambda: SimpleNamespace(enableSemanticRegistryRag=False)
        )
        monkeypatch.setattr(gate.milvus, "searchEmbeddingsByTypeRouted", _fake)

        gate.searchRegistryEmbeddings([0.1] * 4, topK=5)

        assert calls == [{"topK": 5, "typeFilter": None}]
