"""P3a D19：v2 双写（写路径接线）。

双写的三条口径（本文件逐条钉死）
--------------------------------
1. **开关关 ⇒ 一个字节都不写 v2**：v1 保持今天的唯一真源，P3a 整体未启用
   ⇒ 读路径逐字节等于今天、写路径也逐字节等于今天。
2. **v2 写入失败不影响主流程**：v2 是影子副本（读路径有 fail-closed 兜底），
   挂了只告警 + 计数，不许把 CRUD 拖成 500。
3. **记录不可变**：治理维度是**新对象**上补的，不许就地改调用方的 record
   （调用方常常复用同一批 record 再写 v1）。

维度从治理表解析（真实 PG）：域/组织/实体/有效期，见
`app/services/ontology_dimension_service.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import registry_v2_write as writer


@pytest.fixture
def switchOn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        writer, "getSettings", lambda: SimpleNamespace(enableSemanticRegistryRag=True)
    )


@pytest.fixture
def switchOff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        writer, "getSettings", lambda: SimpleNamespace(enableSemanticRegistryRag=False)
    )


def _installMilvus(
    monkeypatch: pytest.MonkeyPatch, *, error: Exception | None = None
) -> dict[str, Any]:
    """替换 v2 写入/删除为假实现，返回捕获盒。"""
    box: dict[str, Any] = {"inserted": [], "deleted": []}

    def _fakeInsert(records: list[dict[str, Any]]) -> None:
        if error is not None:
            raise error
        box["inserted"].extend(records)

    def _fakeDelete(ontologyId: int, type_: str) -> None:
        box["deleted"].append((ontologyId, type_))

    monkeypatch.setattr(writer, "insertRegistryV2", _fakeInsert)
    monkeypatch.setattr(writer, "deleteRegistryV2", _fakeDelete)
    return box


def _classRecord(ontologyId: int = 1) -> dict[str, Any]:
    return {
        "ontology_id": ontologyId,
        "type": "class",
        "name": "供应商",
        "alias": "supplier",
        "description": "采购域的供应商主数据",
        "embedding": [0.01] * 4,
    }


# ---------------------------------------------------------------------------
# 1. 开关
# ---------------------------------------------------------------------------


class Test开关:
    async def test_开关关闭时不写v2(self, switchOff, monkeypatch) -> None:
        box = _installMilvus(monkeypatch)

        result = await writer.syncRegistryV2Embeddings([_classRecord()])

        assert box["inserted"] == []
        assert result["written"] == 0
        assert result["skipped"] is True

    async def test_开关关闭时不解析维度(self, switchOff, monkeypatch) -> None:
        """开关关 ⇒ 连 DB 都不碰（零行为变化的硬要求）。"""
        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("开关关闭时不应解析治理维度")

        monkeypatch.setattr(writer, "buildRegistryV2Records", _boom)
        _installMilvus(monkeypatch)

        await writer.syncRegistryV2Embeddings([_classRecord()])

    async def test_开关开启时写入v2(self, switchOn, monkeypatch, dbSession) -> None:
        box = _installMilvus(monkeypatch)

        result = await writer.syncRegistryV2Embeddings(
            [_classRecord()], session=dbSession
        )

        assert len(box["inserted"]) == 1
        assert result["written"] == 1
        assert result["skipped"] is False


# ---------------------------------------------------------------------------
# 2. 治理维度 + 不可变
# ---------------------------------------------------------------------------


class Test治理维度:
    async def test_写入记录带治理维度(self, switchOn, monkeypatch, dbSession) -> None:
        await _seedGovernance(dbSession)
        box = _installMilvus(monkeypatch)

        classId = await _seedClass(dbSession, className="W_CLASS")
        await dbSession.commit()

        await writer.syncRegistryV2Embeddings(
            [_classRecord(ontologyId=classId)], session=dbSession
        )

        assert len(box["inserted"]) == 1
        row = box["inserted"][0]
        assert row["domain_codes"] == ("WDOM",)
        assert row["org_codes"] == ("worg",)
        assert row["entity_type"] == "WBO"
        assert row["valid_from"] == 1_767_225_600_000
        assert row["valid_to"] == 0
        assert row["release_id"] == 0

    async def test_原记录不被就地修改(self, switchOn, monkeypatch, dbSession) -> None:
        """不可变原则：治理维度补在**新对象**上。"""
        await _seedGovernance(dbSession)
        _installMilvus(monkeypatch)
        record = _classRecord()

        await writer.syncRegistryV2Embeddings([record], session=dbSession)

        assert "domain_codes" not in record
        assert "org_codes" not in record
        assert "entity_type" not in record

    async def test_未归域的类写入空维度(self, switchOn, monkeypatch, dbSession) -> None:
        """查不到治理事实 ⇒ 写空维度（fail-closed 的显式表达，不是猜一个域）。"""
        box = _installMilvus(monkeypatch)
        classId = await _seedClass(
            dbSession, className="W_CLASS_ORPHAN", withBo=False
        )
        await dbSession.commit()

        await writer.syncRegistryV2Embeddings(
            [_classRecord(ontologyId=classId)], session=dbSession
        )

        assert len(box["inserted"]) == 1
        row = box["inserted"][0]
        assert row["domain_codes"] == ()
        assert row["org_codes"] == ()
        assert row["entity_type"] == ""


# ---------------------------------------------------------------------------
# 3. 失败不影响主流程
# ---------------------------------------------------------------------------


class Test失败处理:
    async def test_写入异常被吞掉并计数(self, switchOn, monkeypatch, dbSession) -> None:
        _installMilvus(monkeypatch, error=RuntimeError("milvus down"))

        result = await writer.syncRegistryV2Embeddings(
            [_classRecord()], session=dbSession
        )

        assert result["written"] == 0
        assert result["failed"] == 1

    async def test_空记录列表直接返回(self, switchOn, monkeypatch) -> None:
        box = _installMilvus(monkeypatch)

        result = await writer.syncRegistryV2Embeddings([])

        assert box["inserted"] == []
        assert result["written"] == 0


# ---------------------------------------------------------------------------
# 种子
# ---------------------------------------------------------------------------


async def _seedGovernance(session: AsyncSession) -> None:
    """种一个组织 + 一个域（治理链路的事实来源）。"""
    from app.domain.models import DataDomain
    from app.models.rbac import Organization

    org = Organization(code="worg", name="写路径组织", sort_order=0)
    session.add(org)
    await session.flush()
    session.add(
        DataDomain(code="WDOM", name="写路径域", primary_org_id=org.id, enabled=True)
    )
    await session.flush()


async def _seedClass(
    session: AsyncSession, *, className: str, withBo: bool = True
) -> int:
    from app.domain.models import BusinessObject, OntologyClass

    row = OntologyClass(
        class_name=className,
        class_alias=None,
        description="写路径用例",
        version=1,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_to=None,
        object_owner=None,
    )
    session.add(row)
    await session.flush()
    if withBo:
        session.add(
            BusinessObject(
                code="WBO",
                name="写路径 BO",
                header_class_id=row.id,
                primary_domain_id="WDOM",
            )
        )
        await session.flush()
    return row.id
