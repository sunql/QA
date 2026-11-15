"""P3a 复核：v2 维度独立核对脚本的纯逻辑单测（**不连 Milvus、不连 PG**）。

这个脚本的职责：把「v2 行里实际存的治理维度」与「PG 治理事实（独立 SQL 推导）」
逐行对上 ⇒ 给 D20 的「泄漏 0」补上「不是自指的」这层证据。

⚠️ import 路径必须是 ``scripts.verify_registry_v2_dimensions``（包内导入）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from scripts import verify_registry_v2_dimensions as verify


def _expectedRow(**kwargs) -> dict:
    row = {
        "dim_type": "class",
        "ontology_id": 1,
        "entity_type": "SUPPLIER",
        "primary_domain": "PROCUREMENT",
        "attached_domains": "QUALITY",
        "domain_org": "ORG_A",
        "owner_org": "",
        "valid_from": None,
        "valid_to": None,
    }
    row.update(kwargs)
    return row


def _v2Row(**kwargs) -> dict:
    row = {
        "type": "class",
        "ontology_id": 1,
        "domain_codes": ["PROCUREMENT", "QUALITY"],
        "org_codes": ["ORG_A"],
        "entity_type": "SUPPLIER",
        "valid_from": 0,
        "valid_to": 0,
    }
    row.update(kwargs)
    return row


class Test期望维度:
    def test_主域与附域合并去重排序(self) -> None:
        expected = verify.expectedDimension(
            _expectedRow(primary_domain="PROCUREMENT", attached_domains="QUALITY,PROCUREMENT")
        )

        assert expected["domain_codes"] == ("PROCUREMENT", "QUALITY")

    def test_无主域时只用附域(self) -> None:
        expected = verify.expectedDimension(
            _expectedRow(primary_domain="", attached_domains="QUALITY")
        )

        assert expected["domain_codes"] == ("QUALITY",)

    def test_完全没有域时为空数组(self) -> None:
        expected = verify.expectedDimension(
            _expectedRow(primary_domain="", attached_domains="")
        )

        assert expected["domain_codes"] == ()

    def test_组织码合并域组织与属主组织(self) -> None:
        expected = verify.expectedDimension(
            _expectedRow(domain_org="ORG_A", owner_org="ORG_B")
        )

        assert expected["org_codes"] == ("ORG_A", "ORG_B")

    def test_属主不匹配组织时不算组织(self) -> None:
        """``object_owner`` 匹配不到组织码就不并入（SQL 已判定 owner_org 为空）。"""
        expected = verify.expectedDimension(
            _expectedRow(domain_org="ORG_A", owner_org="")
        )

        assert expected["org_codes"] == ("ORG_A",)

    def test_实体码为空串(self) -> None:
        expected = verify.expectedDimension(_expectedRow(entity_type=""))

        assert expected["entity_type"] == ""

    def test_有效期换算成epoch毫秒(self) -> None:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        expected = verify.expectedDimension(
            _expectedRow(valid_from=moment, valid_to=None)
        )

        assert expected["valid_from"] == int(moment.timestamp() * 1000)
        assert expected["valid_to"] == 0

    def test_naive时间视作utc(self) -> None:
        assert verify._epochMs(datetime(2026, 1, 1)) == verify._epochMs(
            datetime(2026, 1, 1, tzinfo=UTC)
        )

    def test_空值换算成零(self) -> None:
        assert verify._epochMs(None) == 0


class Test逐行比对:
    def test_完全一致时无不一致(self) -> None:
        report = verify.compareDimensions(
            [_v2Row()], [_expectedRow(valid_from=None)]
        )

        assert report["mismatched"] == 0
        assert report["compared"] == 1
        assert report["matched"] == 1
        assert report["mismatches"] == []

    def test_域码不同被记为不一致(self) -> None:
        report = verify.compareDimensions(
            [_v2Row(domain_codes=["QUALITY"])], [_expectedRow()]
        )

        assert report["mismatched"] == 1
        assert report["mismatches"][0]["field"] == "domain_codes"
        assert report["mismatches"][0]["expected"] == ("PROCUREMENT", "QUALITY")
        assert report["mismatches"][0]["actual"] == ("QUALITY",)

    def test_组织码顺序不同不算不一致(self) -> None:
        """写入口径是「排序去重」⇒ 顺序不同是同义，不该报不一致。"""
        report = verify.compareDimensions(
            [_v2Row(org_codes=["ORG_B", "ORG_A"])],
            [_expectedRow(domain_org="ORG_B", owner_org="ORG_A")],
        )

        assert report["mismatched"] == 0

    def test_有效期不同被记为不一致(self) -> None:
        report = verify.compareDimensions(
            [_v2Row(valid_from=123)], [_expectedRow()]
        )

        assert report["mismatched"] == 1
        assert report["mismatches"][0]["field"] == "valid_from"

    def test_实体码不同被记为不一致(self) -> None:
        report = verify.compareDimensions(
            [_v2Row(entity_type="OTHER")], [_expectedRow()]
        )

        assert report["mismatches"][0]["field"] == "entity_type"

    def test_v2里多出的行记为孤儿(self) -> None:
        """实体已从治理表删除 ⇒ 漂移，不算维度错。"""
        report = verify.compareDimensions([_v2Row(ontology_id=99)], [_expectedRow()])

        assert report["orphanRows"] == [{"type": "class", "ontology_id": 99}]
        assert report["mismatched"] == 0

    def test_pg有实体但v2没有向量行记为缺失(self) -> None:
        """不是每个实体都有向量 ⇒ 不算错。"""
        report = verify.compareDimensions([], [_expectedRow()])

        assert report["missingInV2"] == [{"type": "class", "ontology_id": 1}]
        assert report["mismatched"] == 0

    def test_多行多类都能比对(self) -> None:
        report = verify.compareDimensions(
            [
                _v2Row(type="class", ontology_id=1),
                _v2Row(
                    type="property",
                    ontology_id=2,
                    domain_codes=["PROCUREMENT", "QUALITY"],
                    org_codes=[],
                    entity_type="",
                ),
            ],
            [
                _expectedRow(ontology_id=1),
                _expectedRow(
                    dim_type="property",
                    ontology_id=2,
                    entity_type="",
                    domain_org="",
                    owner_org="",
                ),
            ],
        )

        assert report["compared"] == 2
        assert report["mismatched"] == 0
