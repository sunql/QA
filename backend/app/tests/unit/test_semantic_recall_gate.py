"""召回侧的语义钉住闸门（P3d D30）单测。

`loadRecallOntology` 是**整条召回链**的钉住入口（不是只切 DIM——混合口径
「DIM 从快照、其它从活表」会静默破坏「按发布读」的合同）：

- **没钉住** ⇒ `None`（召回照旧直读治理表，与现状一致）；
- **钉住了** ⇒ 那一版快照**全量**复原成**可变**视图（`RecallClass` /
  `RecallJoinView`）——可变是硬要求：召回管线会往类对象上写 `_recall_score`
  （`cls._recall_score = ...`），frozen 视图会当场炸；
- **任何语义面异常** ⇒ `None` + 留痕（召回降级直读，问答不因语义面失败）。

`RecallClass` 的字段名与 ORM `OntologyClass` 逐字对齐 ⇒ 召回管线的层判定
（`_getClassLayer`）、ODS 过滤、排序、join 扩边对两种对象一视同仁。
"""

from __future__ import annotations

from app.services.semantic_recall_gate import loadRecallOntology


def _manifest() -> dict:
    return {
        "classes": [
            {
                "className": "物料维度",
                "classAlias": "物料",
                "description": "物料主数据维度",
                "sourceTable": "DIM_IMATERIAL",
                "parentClassName": None,
                "properties": [],
            },
            {
                "className": "收货明细",
                "classAlias": None,
                "description": None,
                "sourceTable": "DWD_GOODS_RECEIPT_DTL",
                "parentClassName": None,
                "properties": [],
            },
            {
                "className": "贴源原始",
                "classAlias": None,
                "description": None,
                "sourceTable": "ODS_RAW_PO",
                "parentClassName": None,
                "properties": [],
            },
        ],
        "joins": [
            {
                "sourceClassName": "收货明细",
                "sourceColumns": ["ITEM_CODE"],
                "targetClassName": "物料维度",
                "targetColumns": ["ITMREF_0"],
                "joinType": "INNER",
                "relationType": "business",
                "description": "收货→物料",
            }
        ],
        "metrics": [],
        "kpis": [],
        "businessObjects": [],
    }


class _Registry:
    def __init__(self, snapshot=None, error: Exception | None = None) -> None:
        self.snapshot = snapshot
        self.error = error

    async def getById(self, session, releaseId: int):
        if self.error is not None:
            raise self.error
        return self.snapshot


def _snapshot(manifest=None, releaseId: int = 5):
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


async def test_没钉住时返回_None() -> None:
    assert await loadRecallOntology(session=None, pinnedReleaseId=None) is None


async def test_钉住时全量复原为可变视图() -> None:
    view = await loadRecallOntology(
        session=None, pinnedReleaseId=5, registry=_Registry(snapshot=_snapshot())
    )
    assert view is not None
    assert view.releaseId == 5
    assert {c.class_name for c in view.classes} == {"物料维度", "收货明细", "贴源原始"}
    assert len(view.joins) == 1
    # 可变性（召回管线要写 _recall_score）——frozen 会当场炸
    cls = next(c for c in view.classes if c.class_name == "物料维度")
    cls._recall_score = 0.5  # type: ignore[attr-defined]
    assert cls._recall_score == 0.5  # type: ignore[attr-defined]


async def test_视图字段与_ORM_对齐_层判定可用() -> None:
    """`_getClassLayer` 按 source_table 前缀判层 ⇒ 视图必须带 source_table。"""
    from app.services.chat_recall import _getClassLayer, _isOdsBusinessTable

    view = await loadRecallOntology(
        session=None, pinnedReleaseId=5, registry=_Registry(snapshot=_snapshot())
    )
    assert view is not None
    byName = {c.class_name: c for c in view.classes}
    assert _getClassLayer(byName["物料维度"]) == "DIM"
    assert _getClassLayer(byName["收货明细"]) == "DWD"
    assert _isOdsBusinessTable(byName["贴源原始"]) is True


async def test_语义面异常降级_None() -> None:
    view = await loadRecallOntology(
        session=None, pinnedReleaseId=5, registry=_Registry(error=RuntimeError("down"))
    )
    assert view is None
