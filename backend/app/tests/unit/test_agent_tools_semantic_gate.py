"""Agent NL2SQL 工具的语义闸门（P3d D29）单测。

`loadToolOntology` 是工具读本体的**钉住入口**：

- **没钉住**（`pinnedReleaseId=None`）⇒ `None`（调用方直读治理表，与现状一致）；
- **钉住了** ⇒ 按 P2 的既有降级语义（`resolveSchemaRelease`）取那一版，
  再**全量**复原成渲染视图（`SnapshotClass/Join` 与 ORM 字段对齐）⇒
  工具分支渲染出与直读**逐字节一致**的输出；
- **任何语义面异常**（那一版没了 / 完整性失败 / 内容畸形 / 意外异常）
  ⇒ `None` + 留痕 —— 工具**绝不**因为语义面炸（G4「随时可回落」）。
"""

from __future__ import annotations

from app.services.agent_tools_semantic_gate import loadToolOntology


def _manifest() -> dict:
    return {
        "classes": [
            {
                "className": "采购订单",
                "classAlias": "PO",
                "description": "采购订单头",
                "sourceTable": "DWD_PURCHASE_ORDER",
                "parentClassName": None,
                "properties": [
                    {
                        "propertyName": "po_no",
                        "propertyAlias": "单号",
                        "description": "采购订单号",
                        "dataType": "VARCHAR",
                        "sourceColumn": "PO_NO",
                        "isPrimaryKey": True,
                        "isForeignKey": False,
                        "refClassName": None,
                        "isNotNull": True,
                    },
                    {
                        "propertyName": "supplier_id",
                        "propertyAlias": None,
                        "description": None,
                        "dataType": "BIGINT",
                        "sourceColumn": "SUPPLIER_ID",
                        "isPrimaryKey": False,
                        "isForeignKey": True,
                        "refClassName": "供应商",
                        "isNotNull": None,
                    },
                ],
            },
            {
                "className": "供应商",
                "classAlias": None,
                "description": None,
                "sourceTable": "DIM_SUPPLIER",
                "parentClassName": None,
                "properties": [],
            },
        ],
        "joins": [
            {
                "sourceClassName": "采购订单",
                "sourceColumns": ["SUPPLIER_ID"],
                "targetClassName": "供应商",
                "targetColumns": ["SUPPLIER_ID"],
                "joinType": "INNER",
                "relationType": "business",
                "description": None,
            }
        ],
        "metrics": [],
        "kpis": [],
        "businessObjects": [],
    }


class _Registry:
    """Registry 替身：getById 按 P2 契约返回快照或抛异常。"""

    def __init__(self, snapshot=None, error: Exception | None = None) -> None:
        self.snapshot = snapshot
        self.error = error
        self.askedId: int | None = None

    async def getById(self, session, releaseId: int):
        self.askedId = releaseId
        if self.error is not None:
            raise self.error
        return self.snapshot


def _snapshot(manifest=None, releaseId: int = 7):
    import hashlib

    from app.domain.semantic_manifest_schema import canonicalManifestJson
    from app.services.semantic_registry_service import SemanticReleaseSnapshot

    body = manifest if manifest is not None else _manifest()
    canonical = canonicalManifestJson(body)
    return SemanticReleaseSnapshot(
        id=releaseId,
        version="2026.11.15-1",
        domain_code=None,
        status="ACTIVE",
        schema_version="1.0",
        manifest_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        activated_time=None,
        _manifestJson=canonical,
    )


async def test_没钉住时返回_None_调用方直读() -> None:
    assert await loadToolOntology(session=None, pinnedReleaseId=None) is None


async def test_钉住时全量复原为工具视图() -> None:
    registry = _Registry(snapshot=_snapshot())
    view = await loadToolOntology(
        session=None, pinnedReleaseId=7, registry=registry
    )
    assert view is not None
    assert view.releaseId == 7
    assert view.version == "2026.11.15-1"
    assert {c.class_name for c in view.classes} == {"采购订单", "供应商"}
    assert len(view.joins) == 1
    po = next(c for c in view.classes if c.class_name == "采购订单")
    assert po.source_table == "DWD_PURCHASE_ORDER"
    assert {p.property_name for p in po.properties} == {"po_no", "supplier_id"}


async def test_钉住的那一版被删时降级_None() -> None:
    from app.domain.exceptions import NotFoundError

    registry = _Registry(error=NotFoundError("no such release"))
    view = await loadToolOntology(
        session=None, pinnedReleaseId=7, registry=registry
    )
    assert view is None


async def test_完整性失败时降级_None() -> None:
    from app.services.semantic_registry_service import SemanticReleaseIntegrityError

    registry = _Registry(error=SemanticReleaseIntegrityError("hash mismatch"))
    assert await loadToolOntology(session=None, pinnedReleaseId=7, registry=registry) is None


async def test_内容畸形时降级_None() -> None:
    """同名类重复 ⇒ 复原抛 SemanticSnapshotMalformedError ⇒ 降级直读。"""
    bad = _manifest()
    bad["classes"].append(dict(bad["classes"][0]))
    registry = _Registry(snapshot=_snapshot(manifest=bad))
    assert await loadToolOntology(session=None, pinnedReleaseId=7, registry=registry) is None


async def test_意外异常也降级_工具不许因语义面炸() -> None:
    registry = _Registry(error=RuntimeError("boom"))
    assert await loadToolOntology(session=None, pinnedReleaseId=7, registry=registry) is None
