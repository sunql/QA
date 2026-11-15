"""P3a 复核：v2 维度独立核对脚本的纯逻辑单测（**不连 Milvus、不连 PG**）。

这个脚本的职责：把「v2 行里实际存的治理维度」与「PG 治理事实（独立 SQL 推导）」
逐行对上 ⇒ 给 D20 的「泄漏 0」补上「不是自指的」这层证据。

⚠️ import 路径必须是 ``scripts.verify_registry_v2_dimensions``（包内导入）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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

    def test_跨类型同号各自计为不一致(self) -> None:
        """回归守卫：``ontology_id`` 在不同类型下会重号 ⇒ 不一致计数必须用复合键。

        旧口径只用 ``ontology_id`` 做键 ⇒ 同号跨类型会被并成 1 个 ⇒
        ``mismatched``/``matched`` 计数失真。
        """
        report = verify.compareDimensions(
            [
                _v2Row(type="class", ontology_id=1, domain_codes=["WRONG"]),
                _v2Row(type="property", ontology_id=1, domain_codes=["WRONG"]),
            ],
            [
                _expectedRow(dim_type="class", ontology_id=1),
                _expectedRow(dim_type="property", ontology_id=1),
            ],
        )

        assert report["mismatched"] == 2
        assert report["compared"] == 2
        assert report["matched"] == 0


# ---------------------------------------------------------------------------
# 退出码分层（安全评审 MEDIUM-5）
# ---------------------------------------------------------------------------


def _report(**kwargs) -> dict:
    report = {
        "v2Rows": 0,
        "compared": 0,
        "matched": 0,
        "mismatched": 0,
        "orphanRows": [],
        "missingInV2": [],
        "mismatches": [],
    }
    report.update(kwargs)
    return report


class Test退出码:
    @pytest.fixture(autouse=True)
    def _databaseUrl(self, monkeypatch) -> None:
        """脚本要求显式的 ``DATABASE_URL``（防默认 DSN 打到别的库）。"""
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata",
        )

    def test_环境异常返回二(self, monkeypatch) -> None:
        """环境故障（PG/Milvus 不可达）不是「维度错乱」⇒ 必须与 1 区分开。"""

        async def _boom(args):
            raise RuntimeError("pg down")

        monkeypatch.setattr(verify, "run", _boom)

        assert verify.main(["--target-db", "qa_registry_v2"]) == 2

    def test_零行比对返回三(self, monkeypatch) -> None:
        """零行比对 = 假绿（未回填 / 指错库）⇒ 必须非 0。"""

        async def _fake(args):
            return _report()

        monkeypatch.setattr(verify, "run", _fake)

        assert verify.main(["--target-db", "qa_registry_v2"]) == 3

    def test_有不一致返回一(self, monkeypatch) -> None:
        async def _fake(args):
            return _report(compared=2, matched=1, mismatched=1)

        monkeypatch.setattr(verify, "run", _fake)

        assert verify.main(["--target-db", "qa_registry_v2"]) == 1

    def test_全部一致返回零(self, monkeypatch) -> None:
        async def _fake(args):
            return _report(v2Rows=2, compared=2, matched=2)

        monkeypatch.setattr(verify, "run", _fake)

        assert verify.main(["--target-db", "qa_registry_v2"]) == 0

    def test_缺少数据库地址返回二(self, monkeypatch) -> None:
        """不显式给 ``DATABASE_URL`` ⇒ 拒绝执行（防默认 DSN 打到别的库）。"""
        monkeypatch.delenv("DATABASE_URL", raising=False)

        called = False

        async def _fake(args):
            nonlocal called
            called = True
            return _report()

        monkeypatch.setattr(verify, "run", _fake)

        assert verify.main(["--target-db", "qa_registry_v2"]) == 2
        assert called is False
