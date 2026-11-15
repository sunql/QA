"""P3a 复核：v2 集合的**就绪探针**（开关打开时的启动期告警依据）。

为什么需要
----------
v2 集合缺失或为空 ⇒ 带过滤的召回**安静地**返回空 ⇒ 用户看到的是
「查不到实体」，没有任何降级痕迹 ⇒ 排查成本极高（且容易被误判成
「模型没召回」）。故开关打开时要在启动期探一次，把「未就绪」喊出来。

口径
----
- 行数用 Milvus 的**物理**行数（``num_entities``）⇒ 只能判「有没有写过」，
  **不能**当可见行数用（可见行数要按可见去重计数，见回填脚本 ``_countV2Rows``）；
- 探不到（异常）⇒ 记 ``-1``，与「集合为空 / 不存在（0）」区分开。

全部用 fake —— 不连 Milvus。
"""

from __future__ import annotations

import pytest

from app.infrastructure import milvus_registry_v2 as milvusV2
from app.services import semantic_registry_rag_gate as gate


class Test行数探针:
    def test_未知类型被拒绝(self) -> None:
        with pytest.raises(ValueError):
            milvusV2.registryV2RowCount("nope")

    def test_集合不存在时返回零(self, monkeypatch) -> None:
        monkeypatch.setattr(milvusV2, "hasEmbeddingCollection", lambda name: False)

        assert milvusV2.registryV2RowCount("class") == 0

    def test_集合存在时返回物理行数(self, monkeypatch) -> None:
        monkeypatch.setattr(milvusV2, "hasEmbeddingCollection", lambda name: True)
        monkeypatch.setattr(
            milvusV2, "_collectionEntityCount", lambda name: 3_791, raising=False
        )

        assert milvusV2.registryV2RowCount("class") == 3_791


class Test就绪探针:
    def test_集合为空时行数为零(self, monkeypatch) -> None:
        monkeypatch.setattr(gate, "registryV2RowCount", lambda type_: 0)

        readiness = gate.registryRagReadiness()

        assert readiness == {"class": 0, "property": 0, "metric": 0}

    def test_探针异常时记为负一(self, monkeypatch) -> None:
        def _boom(type_: str) -> int:
            raise RuntimeError("milvus down")

        monkeypatch.setattr(gate, "registryV2RowCount", _boom)

        readiness = gate.registryRagReadiness()

        assert readiness == {"class": -1, "property": -1, "metric": -1}

    def test_只探必需集合(self, monkeypatch) -> None:
        probed: list[str] = []

        def _fake(type_: str) -> int:
            probed.append(type_)
            return 10

        monkeypatch.setattr(gate, "registryV2RowCount", _fake)

        gate.registryRagReadiness()

        assert probed == ["class", "property", "metric"]
