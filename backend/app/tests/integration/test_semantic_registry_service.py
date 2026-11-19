"""语义发布 Registry（P1b / D8）的真实 PG 行为。

数据库不是可选项：`getActive()` 的语义建立在**部分唯一索引 + JSONB 快照**之上，
用假 session 只能测「代码调用了什么」，测不出「从库里读出来是不是那一行」。

本文件钉住的方法（P0 裁决一的「钉住方式」）：
   编译 → 激活 → 修改本体治理表 → 重读发布，断言快照内容不变。

以及 D8 完成定义：无激活发布返回 `None`（不抛错）、写时失效生效。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError
from app.domain.models import OntologyClass
from app.domain.semantic_manifest_schema import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_V1,
    manifestHash,
)
from app.domain.semantic_release_models import SemanticRelease
from app.services.nl2sql_schema import buildSchemaText
from app.services.ontology_service import OntologyService
from app.services.semantic_compiler import compileManifest
from app.services.semantic_registry_service import (
    SemanticReleaseIntegrityError,
    semanticRegistry,
)
from app.services.semantic_schema_adapter import (
    SnapshotClass,
    loadClassesForSchema,
)
from app.tests.unit._semantic_rows import makeClass, makeProperty
from scripts.semantic_schema_shadow_diff import _blockContentMismatches

_MINIMAL_MANIFEST: dict[str, Any] = {
    "schemaVersion": SCHEMA_VERSION,
    "domains": [],
    "classes": [],
    "joins": [],
    "metrics": [],
    "kpis": [],
    "businessObjects": [],
    "sourceBindings": [],
}

# v1 形状的**旧版本**快照（没有 `sourceBindings` 段）——
# 用来说明「Registry 原样回传正文版本，不替它盖当前版本」。
_V1_MINIMAL_MANIFEST: dict[str, Any] = {
    "schemaVersion": SCHEMA_VERSION_V1,
    "domains": [],
    "classes": [],
    "joins": [],
    "metrics": [],
    "kpis": [],
    "businessObjects": [],
}


@pytest.fixture(autouse=True)
def _clearRegistryCache() -> Any:
    """模块级单例跨用例存活；不清会拿到上一个用例的快照。"""
    semanticRegistry.invalidate()
    yield
    semanticRegistry.invalidate()


async def _insertRelease(
    session: AsyncSession,
    *,
    version: str = "2026.11.10-1",
    status: str = "DRAFT",
    domainCode: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> SemanticRelease:
    body = manifest if manifest is not None else dict(_MINIMAL_MANIFEST)
    row = SemanticRelease(
        version=version,
        status=status,
        domain_code=domainCode,
        manifest_json=body,
        manifest_hash=manifestHash(body),
        # 与生产写路径同一口径（`semantic_release_service`）：
        # 行版本**跟着正文走**，不替它盖当前版本 ——
        # 盖版本号会让「正文说 v1、行上写 v2」⇒ 复原层判漂移 ⇒ 降级直读，
        # 而版本漂移的根因恰恰是有人绕过发布路径改过库 ⇒ 必须暴露而不是抹平。
        schema_version=body.get("schemaVersion") or SCHEMA_VERSION,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# 完成定义：无激活发布 → None
# ---------------------------------------------------------------------------


async def test_get_active_returns_none_when_nothing_published(
    dbSession: AsyncSession,
) -> None:
    assert await semanticRegistry.getActive(dbSession) is None


async def test_get_active_ignores_non_active_statuses(dbSession: AsyncSession) -> None:
    for index, status in enumerate(("DRAFT", "VALIDATED", "ARCHIVED")):
        await _insertRelease(dbSession, version=f"2026.11.10-{index}", status=status)

    assert await semanticRegistry.getActive(dbSession) is None


async def test_get_active_returns_published_snapshot(dbSession: AsyncSession) -> None:
    """旧版本（v1）快照原样回传：Registry **不替它盖当前版本**。

    盖版本号会让「快照正文说 v1、行上写 v2」⇒ 复原层判版本漂移 ⇒ 降级直读；
    而版本漂移的根因恰恰是有人绕过发布路径改过库 ⇒ 必须暴露而不是抹平。
    """
    manifest = dict(_V1_MINIMAL_MANIFEST)
    await _insertRelease(dbSession, status="ACTIVE", manifest=manifest)

    snapshot = await semanticRegistry.getActive(dbSession)

    assert snapshot is not None
    assert snapshot.version == "2026.11.10-1"
    assert snapshot.manifest == manifest
    assert snapshot.manifest_hash == manifestHash(manifest)
    assert snapshot.schema_version == "v1"


async def test_returned_manifest_is_a_private_copy_on_real_pg(
    dbSession: AsyncSession,
) -> None:
    """真实行上钉住副本语义：缓存命中路径也不能把同一个 dict 递出去。"""
    await _insertRelease(dbSession, status="ACTIVE")

    first = await semanticRegistry.getActive(dbSession)
    assert first is not None
    first.manifest["classes"].append({"className": "被篡改"})

    second = await semanticRegistry.getActive(dbSession)
    assert second is not None
    assert second.manifest["classes"] == []


async def test_release_table_default_matches_current_schema_version(
    dbSession: AsyncSession,
) -> None:
    """**库侧**默认值必须等于当前契约版本（迁移 0156 把 `'v1'` 改成 `'v2'`）。

    为什么钉在真实库上：ORM 的 `server_default` 与迁移里的字面量是**两处**，
    单测断言 ORM 那一处变绿时，迁移可能还写着 `'v1'` ⇒ 真实入库的行会带上
    旧版本号 ⇒ 复原层判「版本漂移」⇒ 降级直读（功能正确、性能回退，且每次
    请求打 WARNING，排查时极容易误判）。

    ⚠️ 这里断言的是**迁移的结果**（`information_schema` 里的真实默认值），
    不是迁移脚本里的字面量 —— 迁移必须冻结（改它要写新迁移），不能被测试
    「顺手改成当前值」而失去意义。
    """
    raw = await dbSession.scalar(
        text(
            "select column_default from information_schema.columns "
            "where table_name = 'semantic_release' and column_name = 'schema_version'"
        )
    )

    assert raw is not None, "semantic_release.schema_version 缺少库侧默认值"
    # PG 把默认值渲染成 `'v2'::character varying` ⇒ 取字面量部分。
    assert raw.split("::")[0].strip("'") == SCHEMA_VERSION


async def test_tampered_snapshot_is_rejected(dbSession: AsyncSession) -> None:
    """绕过发布路径把哈希改坏的行，不得被当成语义使用。"""
    row = await _insertRelease(dbSession, status="ACTIVE")
    row.manifest_hash = "b" * 64
    await dbSession.commit()

    with pytest.raises(SemanticReleaseIntegrityError):
        await semanticRegistry.getActive(dbSession)


# ---------------------------------------------------------------------------
# 裁决一：发布后治理表再改，快照必须不变
# ---------------------------------------------------------------------------


async def test_published_manifest_survives_governance_changes(
    dbSession: AsyncSession,
) -> None:
    """编译 → 激活 → 改本体表 → 重读发布，快照逐字节不变（裁决一的钉住方式）。"""
    dbSession.add(makeClass(901, "供应商", alias="Supplier", sourceTable="DIM_SUPPLIER"))
    await dbSession.commit()

    manifest = await compileManifest(dbSession)
    classNames = [item["className"] for item in manifest["classes"]]
    # 应用启动会 seed 本体基线数据，不能假设 classes[0] 是自己刚插的那行。
    assert "供应商" in classNames

    await _insertRelease(dbSession, status="ACTIVE", manifest=manifest)

    first = await semanticRegistry.getActive(dbSession)
    assert first is not None
    assert first.manifest == manifest

    # 治理表原地 UPDATE（Phase 6 的真实写路径），发布内容不得随之漂移。
    row = await dbSession.get(OntologyClass, 901)
    assert row is not None
    row.class_alias = "改名后的别名"
    await dbSession.commit()

    second = await semanticRegistry.getActive(dbSession)
    assert second is not None
    assert second.manifest == manifest
    assert second.manifest_hash == manifestHash(manifest)

    # 即使缓存被清掉，Registry 也只能从 semantic_release 重读，不能重新编译。
    semanticRegistry.invalidate()
    third = await semanticRegistry.getActive(dbSession)
    assert third is not None
    assert third.manifest == manifest


# ---------------------------------------------------------------------------
# 缓存 + 写时失效
# ---------------------------------------------------------------------------


async def test_cache_returns_stale_until_invalidate(dbSession: AsyncSession) -> None:
    """写路径必须在提交后调 `invalidate()`——缓存不会自己发现库变了。

    这是「进程内缓存 + 写时失效」的契约：D9/D10 的发布/激活/回滚服务负责调用。
    """
    old = await _insertRelease(dbSession, status="ACTIVE", version="2026.11.10-1")
    cached = await semanticRegistry.getActive(dbSession)
    assert cached is not None and cached.id == old.id

    old.status = "ARCHIVED"
    new = await _insertRelease(dbSession, status="ACTIVE", version="2026.11.10-2")
    await dbSession.commit()

    stillCached = await semanticRegistry.getActive(dbSession)
    assert stillCached is not None and stillCached.id == old.id

    semanticRegistry.invalidate()
    fresh = await semanticRegistry.getActive(dbSession)
    assert fresh is not None and fresh.id == new.id


# ---------------------------------------------------------------------------
# 按域作用域
# ---------------------------------------------------------------------------


async def test_global_and_domain_scopes_do_not_shadow_each_other(
    dbSession: AsyncSession,
) -> None:
    """全局快照与按域快照可以同时 ACTIVE（部分唯一索引的 COALESCE 语义）。"""
    globalRow = await _insertRelease(dbSession, status="ACTIVE", version="2026.11.10-1", domainCode=None)
    domainRow = await _insertRelease(
        dbSession,
        status="ACTIVE",
        version="2026.11.10-1",
        domainCode="PROCUREMENT",
    )

    globalSnapshot = await semanticRegistry.getActive(dbSession)
    domainSnapshot = await semanticRegistry.getActive(dbSession, domainCode="PROCUREMENT")

    assert globalSnapshot is not None and globalSnapshot.id == globalRow.id
    assert domainSnapshot is not None and domainSnapshot.id == domainRow.id


# ---------------------------------------------------------------------------
# getById / listReleases
# ---------------------------------------------------------------------------


async def test_get_by_id_returns_snapshot_and_missing_raises(
    dbSession: AsyncSession,
) -> None:
    row = await _insertRelease(dbSession, version="2026.11.10-1")

    snapshot = await semanticRegistry.getById(dbSession, row.id)
    assert snapshot.id == row.id
    assert snapshot.status == "DRAFT"

    with pytest.raises(NotFoundError):
        await semanticRegistry.getById(dbSession, 999_999)


async def test_list_releases_is_newest_first_and_filterable(
    dbSession: AsyncSession,
) -> None:
    first = await _insertRelease(dbSession, version="2026.11.10-1", status="ARCHIVED")
    second = await _insertRelease(dbSession, version="2026.11.10-2", status="VALIDATED")
    third = await _insertRelease(dbSession, version="2026.11.10-3", status="DRAFT")

    allRows = await semanticRegistry.listReleases(dbSession)
    assert [item.id for item in allRows] == [third.id, second.id, first.id]

    activeOnly = await semanticRegistry.listReleases(dbSession, status="VALIDATED")
    assert [item.id for item in activeOnly] == [second.id]


async def test_list_releases_summaries_do_not_carry_the_manifest(
    dbSession: AsyncSession,
) -> None:
    """列表页不得把 ~1MB 快照整份读出来——详细内容走 getById。"""
    await _insertRelease(dbSession, version="2026.11.10-1")

    summaries = await semanticRegistry.listReleases(dbSession)

    assert summaries
    assert not hasattr(summaries[0], "manifest")


async def test_list_releases_domain_filter_three_states(
    dbSession: AsyncSession,
) -> None:
    """不传 = 全量；`None` = 只筛全局；字符串 = 该域。

    这是 D9 列表接口的契约，三态一旦串味，前端「全部 / 全局 / 某域」筛选就会出错。
    """
    globalRow = await _insertRelease(dbSession, version="2026.11.10-1", domainCode=None)
    procurementRow = await _insertRelease(dbSession, version="2026.11.10-2", domainCode="PROCUREMENT")
    logisticsRow = await _insertRelease(dbSession, version="2026.11.10-3", domainCode="LOGISTICS")

    allRows = await semanticRegistry.listReleases(dbSession)
    assert {item.id for item in allRows} == {
        globalRow.id,
        procurementRow.id,
        logisticsRow.id,
    }

    globalOnly = await semanticRegistry.listReleases(dbSession, domainCode=None)
    assert [item.id for item in globalOnly] == [globalRow.id]

    procurementOnly = await semanticRegistry.listReleases(dbSession, domainCode="PROCUREMENT")
    assert [item.id for item in procurementOnly] == [procurementRow.id]


async def test_count_releases_matches_the_list_filters(
    dbSession: AsyncSession,
) -> None:
    await _insertRelease(dbSession, version="2026.11.10-1", status="DRAFT")
    await _insertRelease(dbSession, version="2026.11.10-2", status="DRAFT")
    await _insertRelease(dbSession, version="2026.11.10-3", status="ACTIVE", domainCode="PROCUREMENT")

    assert await semanticRegistry.countReleases(dbSession) == 3
    assert await semanticRegistry.countReleases(dbSession, status="DRAFT") == 2
    assert await semanticRegistry.countReleases(dbSession, domainCode="PROCUREMENT") == 1
    # 前两条没传 domainCode ⇒ 全局快照（`None` 是筛选值不是「不筛选」）。
    assert await semanticRegistry.countReleases(dbSession, domainCode=None) == 2


async def test_list_releases_paginates(dbSession: AsyncSession) -> None:
    for index in range(3):
        await _insertRelease(dbSession, version=f"2026.11.10-{index}")

    page = await semanticRegistry.listReleases(dbSession, limit=2, offset=1)

    assert len(page) == 2
    assert [item.version for item in page] == ["2026.11.10-1", "2026.11.10-0"]


# ---------------------------------------------------------------------------
# D12：快照闸门在**真实 PG** 上的行为
#
# 为什么这条链必须真实库：判据是「从库里读出来的那一行**是不是被当成快照
# 用**」—— 假 session 只能证明「代码调用了 registry」，证明不了「读出来的那
# 行被复原成渲染视图」。
#
# 比对用的是影子比对工具（D13/D14）的**同一把尺**
# （``_blockContentMismatches``：逐类块比较行多重集）：
# ⇒ 集成测试与工具不会各用一套口径而互相漂移。
# 逐块（而不是整段文本）比，是因为「类内列序」两侧本就不同（快照按属性名
# 排序，直读按物理行序），而「事实归属」（哪一列挂在哪个类上）必须逐块相等。
# ---------------------------------------------------------------------------


async def test_active_release_makes_the_schema_path_use_the_snapshot(
    dbSession: AsyncSession,
) -> None:
    dbSession.add(
        makeClass(901, "供应商", alias="Supplier", sourceTable="DIM_SUPPLIER")
    )
    dbSession.add(
        makeProperty(
            901,
            "SUPPLIER_CODE",
            alias="供方编码",
            sourceColumn="SUPPLIER_CODE",
            isPrimaryKey=True,
        )
    )
    await dbSession.commit()

    manifest = await compileManifest(dbSession)
    await _insertRelease(dbSession, status="ACTIVE", manifest=manifest)

    ontology = OntologyService()
    ormClasses = await ontology.listClasses(dbSession)
    ormJoins = await ontology.listJoins(dbSession)
    subset = [cls for cls in ormClasses if cls.class_name == "供应商"]
    assert subset, "治理表里应当能读到刚种下的类"

    snapshotClasses, snapshotJoins = await loadClassesForSchema(
        subset, ormJoins, session=dbSession, enabled=True
    )

    # 走的确实是快照路径（复原视图），不是把 ORM 对象原样透传。
    assert snapshotClasses
    assert all(isinstance(cls, SnapshotClass) for cls in snapshotClasses)

    # 两侧渲染出的**事实**必须等价（逐类块比行多重集）。
    ormText = buildSchemaText(subset, joins=ormJoins)
    snapshotText = buildSchemaText(snapshotClasses, joins=snapshotJoins)
    assert _blockContentMismatches(
        ormText.splitlines(), snapshotText.splitlines()
    ) == []


async def test_snapshot_path_degrades_to_direct_read_when_class_is_missing(
    dbSession: AsyncSession,
) -> None:
    """ACTIVE 快照里没有被召回的类 ⇒ 降级直读治理表（不是失败）。

    快照落后于治理表是**发布节奏的常态**（发布之后又加了类）⇒ 让问答整体
    失败是错的。
    """
    # 治理表里有这个类，但 ACTIVE 快照（最小清单）里没有 ⇒ 快照陈旧。
    dbSession.add(
        makeClass(902, "采购订单", alias="PurchaseOrder", sourceTable="PURCHASE_ORDER")
    )
    await dbSession.commit()
    await _insertRelease(dbSession, status="ACTIVE")

    ontology = OntologyService()
    ormClasses = await ontology.listClasses(dbSession)
    ormJoins = await ontology.listJoins(dbSession)
    subset = [cls for cls in ormClasses if cls.class_name == "采购订单"]
    assert subset, "治理表里应当能读到刚种下的类"

    restored, restoredJoins = await loadClassesForSchema(
        subset, ormJoins, session=dbSession, enabled=True
    )

    assert restored is subset
    assert restoredJoins is ormJoins


async def test_disabled_switch_returns_the_input_objects_untouched(
    dbSession: AsyncSession,
) -> None:
    """开关关 ⇒ 原样返回，连库都不碰（今天的行为必须逐字节不变）。"""
    ontology = OntologyService()
    ormClasses = await ontology.listClasses(dbSession)
    ormJoins = await ontology.listJoins(dbSession)

    restored, restoredJoins = await loadClassesForSchema(
        ormClasses, ormJoins, session=dbSession, enabled=False
    )

    assert restored is ormClasses
    assert restoredJoins is ormJoins
