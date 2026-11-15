"""P3a D20：v2 回填脚本的单测（**不连 Milvus、不连 PG**）。

覆盖脚本的纯逻辑部分：
- 按 ``(type, ontology_id)`` 去重（v1 集合可能残留同一实体的多份向量）；
- 治理维度补齐（**不原地改原行**；查不到 ⇒ 写空维度「未归域」）；
- 维度覆盖统计（区分「已归域」与「未归域」——D20 影子报告的归因缺口口径）；
- 只处理指定类型；
- 写目标闸门（生产库必须显式 opt-in）。

⚠️ import 路径必须是 ``scripts.backfill_registry_v2``（包内导入）。
"""

from __future__ import annotations

import pytest

from scripts import backfill_registry_v2 as bf


class Test去重:
    def test_同一实体保留最新一行(self) -> None:
        rows = [
            {"type": "class", "ontology_id": 10, "id": 1, "name": "旧"},
            {"type": "class", "ontology_id": 10, "id": 7, "name": "新"},
            {"type": "class", "ontology_id": 20, "id": 3, "name": "别的"},
        ]

        deduped = bf._dedupeByKey(rows)

        assert len(deduped) == 2
        assert deduped[("class", 10)]["name"] == "新"
        assert deduped[("class", 20)]["name"] == "别的"

    def test_不同类型同一id互不覆盖(self) -> None:
        """class 的 10 与 property 的 10 是两个实体 ⇒ 不能被去重吃掉。"""
        rows = [
            {"type": "class", "ontology_id": 10, "id": 1},
            {"type": "property", "ontology_id": 10, "id": 2},
        ]

        deduped = bf._dedupeByKey(rows)

        assert set(deduped) == {("class", 10), ("property", 10)}


class Test维度补齐:
    def test_补齐维度不修改原行(self) -> None:
        rows = [{"type": "class", "ontology_id": 10, "name": "供应商"}]
        dims = {("class", 10): {"domain_codes": ("SUP",), "org_codes": ("org_sup",)}}

        prepared = bf._attachDimensions(rows, dims)

        assert "domain_codes" not in rows[0]  # 原行不动（不可变约定）
        assert prepared[0]["domain_codes"] == ("SUP",)
        assert prepared[0]["org_codes"] == ("org_sup",)

    def test_查不到治理事实时写空维度(self) -> None:
        rows = [{"type": "class", "ontology_id": 99, "name": "孤儿"}]

        prepared = bf._attachDimensions(rows, {})

        assert prepared[0]["domain_codes"] == ()
        assert prepared[0]["org_codes"] == ()
        assert prepared[0]["entity_type"] == ""

    def test_缺省的治理字段补零值(self) -> None:
        """维度对象缺字段时补 0 / ""（不是缺字段 ⇒ Milvus 写不进去）。"""
        rows = [{"type": "metric", "ontology_id": 5, "name": "M"}]
        dims = {("metric", 5): {"domain_codes": ("MAT",)}}

        prepared = bf._attachDimensions(rows, dims)

        assert prepared[0]["valid_from"] == 0
        assert prepared[0]["valid_to"] == 0
        assert prepared[0]["release_id"] == 0
        assert prepared[0]["entity_type"] == ""


class Test覆盖统计:
    def test_区分已归域与未归域(self) -> None:
        records = [
            {"type": "class", "domain_codes": ("SUP",), "org_codes": ("o1",)},
            {"type": "class", "domain_codes": (), "org_codes": ()},
            {"type": "property", "domain_codes": ("SUP", "MAT"), "org_codes": ()},
        ]

        summary = bf._summarizeCoverage(records)

        assert summary["total"] == 3
        assert summary["withDomain"] == 2
        assert summary["withoutDomain"] == 1
        assert summary["withOrg"] == 1
        assert summary["byType"] == {"class": 2, "property": 1}


class Test类型过滤:
    def test_只保留指定类型(self) -> None:
        rows = [
            {"type": "class", "ontology_id": 1},
            {"type": "property", "ontology_id": 2},
            {"type": "metric", "ontology_id": 3},
            {"type": "kpi", "ontology_id": 4},
        ]

        kept = bf._filterByTypes(rows, ("class", "metric"))

        assert [r["ontology_id"] for r in kept] == [1, 3]


class Test写目标闸门:
    def test_生产库必须显式opt_in(self) -> None:
        with pytest.raises(SystemExit):
            bf._resolveTargetDb("default", allowProdWrite=False)

    def test_显式opt_in后放行(self) -> None:
        assert bf._resolveTargetDb("default", allowProdWrite=True) == "default"

    def test_隔离库免opt_in(self) -> None:
        assert bf._resolveTargetDb("qa_test", allowProdWrite=False) == "qa_test"

    def test_空库名归一到生产库并被拦(self) -> None:
        with pytest.raises(SystemExit):
            bf._resolveTargetDb("", allowProdWrite=False)
