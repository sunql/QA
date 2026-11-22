"""发现提案发布（G4）：**唯一写口** `semanticReleaseService.publishProposal`。

钉住四件事：

1. **状态门**：只有 APPROVED 且未发布过的提案可以发布；
2. **manifest 不变形**：发布出去的就是提案里那份（同 `manifest_hash`），
   不是「当前治理表编译结果」；
3. **走完整发布链**：DRAFT → VALIDATED → ACTIVE（不是直接写 ACTIVE 行）；
4. **提案回填**：状态 PUBLISHED + `release_id`。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from sqlalchemy import select

from app.domain.discovery_models import DiscoverySource
from app.domain.discovery_pipeline_models import DiscoveryProposal
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import DataSource
from app.domain.semantic_manifest_schema import manifestHash
from app.domain.semantic_release_models import SemanticRelease
from app.infrastructure.security.crypto import encryptApiKey
from app.services.semantic_release_service import semanticReleaseService
from app.tests._knowledge_manifest import sampleManifest


def _healthyManifest() -> dict[str, Any]:
    """能过发布闸门的样本（补齐 RATIO 落点与源类引用）。"""
    manifest = copy.deepcopy(sampleManifest())
    landings = {
        "supplier_delivery_days": ("Supplier", None),
        "po_amount": ("PurchaseOrder", None),
        "iqc_pass_rate": ("InspectionRecord", "PurchaseOrder"),
    }
    for metric in manifest["metrics"]:
        numeratorClass, denominatorClass = landings[metric["metricName"]]
        metric["numeratorMeasure"] = {
            "agg": "SUM",
            "expr": None,
            "filter": None,
            "className": numeratorClass,
            "classAlias": None,
        }
        metric["denominatorMeasure"] = (
            {
                "agg": "SUM",
                "expr": None,
                "filter": None,
                "className": denominatorClass,
                "classAlias": None,
            }
            if denominatorClass
            else None
        )
        metric["sources"] = [{"className": numeratorClass, "role": "BASE"}]
    # MET-002 是 WARNING，不阻断；绑定一个启用中的源只为减少噪音。
    return manifest


async def _seedProposal(
    session: Any, *, status: str = "APPROVED", releaseId: int | None = None
) -> int:
    datasource = DataSource(
        name=f"PUB_SRC_{status}_{releaseId}",
        type="oracle",
        host="h",
        port=1521,
        database_name="THBI",
        username="THBI",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
    )
    session.add(datasource)
    await session.flush()
    source = DiscoverySource(
        name=f"发布源{status}{releaseId}",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
    )
    session.add(source)
    await session.flush()
    manifest = _healthyManifest()
    proposal = DiscoveryProposal(
        title="发布提案",
        source_id=source.id,
        diff_json={"kind": "MANIFEST_REPLACEMENT", "baseReleaseId": None, "manifest": manifest},
        status=status,
        proposer="alice",
        release_id=releaseId,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    return proposal.id


async def test_publish_runs_the_full_release_chain_and_marks_the_proposal(
    dbSession: Any,
) -> None:
    proposalId = await _seedProposal(dbSession)
    proposal = await dbSession.get(DiscoveryProposal, proposalId)
    expectedHash = manifestHash(proposal.diff_json["manifest"])

    operation = await semanticReleaseService.publishProposal(
        dbSession,
        proposalId=proposalId,
        version="2026.11.22-prop1",
        actor="bob",
    )

    assert operation.release.status == "ACTIVE"
    assert operation.release.manifest_hash == expectedHash, "发布的必须是提案那份 manifest"

    stored = await dbSession.get(DiscoveryProposal, proposalId)
    assert stored.status == "PUBLISHED"
    assert stored.release_id == operation.release.id

    release = await dbSession.get(SemanticRelease, operation.release.id)
    assert release.status == "ACTIVE"
    # 校验结论也落了表（走的是既有链，不是裸插一行）
    from app.domain.semantic_release_check_models import SemanticReleaseCheck

    checks = (
        await dbSession.execute(
            select(SemanticReleaseCheck).where(
                SemanticReleaseCheck.release_id == release.id
            )
        )
    ).scalars().all()
    assert checks, "发布链必须留下校验结论"


async def test_publish_requires_approved_status(dbSession: Any) -> None:
    proposalId = await _seedProposal(dbSession, status="IN_REVIEW")

    with pytest.raises(ConflictError):
        await semanticReleaseService.publishProposal(
            dbSession, proposalId=proposalId, version="v-x", actor="bob"
        )


async def test_publish_rejects_a_second_publish(dbSession: Any) -> None:
    proposalId = await _seedProposal(dbSession)
    first = await semanticReleaseService.publishProposal(
        dbSession, proposalId=proposalId, version="2026.11.22-prop2", actor="bob"
    )

    with pytest.raises(ConflictError):
        await semanticReleaseService.publishProposal(
            dbSession, proposalId=proposalId, version="2026.11.22-prop3", actor="bob"
        )

    stored = await dbSession.get(DiscoveryProposal, proposalId)
    assert stored.release_id == first.release.id


async def test_publish_rejects_unknown_proposal(dbSession: Any) -> None:
    with pytest.raises(NotFoundError):
        await semanticReleaseService.publishProposal(
            dbSession, proposalId=999999, version="v-x", actor="bob"
        )


async def test_publish_rejects_a_tampered_manifest(dbSession: Any) -> None:
    """绕过提案服务直接改库里的 manifest ⇒ 发布前仍要过契约闸。"""
    proposalId = await _seedProposal(dbSession)
    proposal = await dbSession.get(DiscoveryProposal, proposalId)
    broken = dict(proposal.diff_json)
    broken["manifest"] = {**broken["manifest"], "joins": [{"joinKey": "x"}]}
    proposal.diff_json = broken
    await dbSession.commit()

    with pytest.raises(ValidationError):
        await semanticReleaseService.publishProposal(
            dbSession, proposalId=proposalId, version="v-tampered", actor="bob"
        )


async def test_publish_rejects_a_missing_manifest(dbSession: Any) -> None:
    proposalId = await _seedProposal(dbSession)
    proposal = await dbSession.get(DiscoveryProposal, proposalId)
    proposal.diff_json = {"kind": "MANIFEST_REPLACEMENT"}
    await dbSession.commit()

    with pytest.raises(ValidationError):
        await semanticReleaseService.publishProposal(
            dbSession, proposalId=proposalId, version="v-nomanifest", actor="bob"
        )


async def test_publish_then_rollback_restores_the_previous_manifest(dbSession) -> None:
    """G4-2 回滚演练：发布新提案 ⇒ 回滚到旧版本 ⇒ ACTIVE 内容回到旧那份。"""
    from app.domain.semantic_manifest_schema import SCHEMA_VERSION

    baseManifest = _healthyManifest()
    base = SemanticRelease(
        version="2026.11.22-base-active",
        status="ACTIVE",
        domain_code=None,
        manifest_json=baseManifest,
        manifest_hash=manifestHash(baseManifest),
        schema_version=SCHEMA_VERSION,
    )
    dbSession.add(base)
    await dbSession.commit()
    await dbSession.refresh(base)
    baseId = base.id

    proposalId = await _seedProposal(dbSession)
    published = await semanticReleaseService.publishProposal(
        dbSession, proposalId=proposalId, version="2026.11.22-drill-pub", actor="bob"
    )
    assert published.release.status == "ACTIVE"

    rolled = await semanticReleaseService.rollback(
        dbSession, baseId, version="2026.11.22-drill-rb", actor="bob"
    )

    assert rolled.release.status == "ACTIVE"
    assert rolled.release.manifest_hash == manifestHash(baseManifest)
    archived = await dbSession.get(SemanticRelease, published.release.id)
    assert archived.status == "ARCHIVED"
