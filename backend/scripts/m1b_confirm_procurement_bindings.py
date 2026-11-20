"""M1-b 绑定确认批：采购域 9 个头类的源/属性绑定 候选 → 确认 + 域首发布。

解决什么（M1-b 确认闸第 8 条「每条绑定已确认（非候选）」）：
- dev 库 `procurement_source_binding` 为 0 行 ⇒ 采购域发布只是"名词卡片"，
  NL2SQL 无权威的概念→表/列映射（D-3：候选不得被读路径消费）。
- 本脚本用 **service 层**（复用全部校验/审计/版本冻结点不变量）落：
  1. 补 1 条 JOIN 边（DWD_SUPPLIER_PRICE_LIST_HEADER ↔ DWD_SUPPLIER_PRICE_LIST，
     PRICE_LIST_CODE）—— 否则该类零 JOIN 边 ⇒ SOURCE_BINDING_JOIN_MISSING（ERROR）
     阻断发布；
  2. 9 个头类（`procurement_object_catalog.allHeaderClassNames()`，SSOT）各建
     1 条源绑定（绑**唯一**在 schema_cache 命中其 source_table 的**活跃**数据源）
     + 属性绑定（`ontology_property.source_column` 非空者全绑；
     `is_primary_key` 者按名序标 key_ordinal 1..N）→ 确认属性 → 确认源绑定；
  3. 域首发布：createDraft(domainCode='PROCUREMENT') → markValidated → activate；
  4. 校验输出：manifest classes ≥8、sourceBindings 全确认、checks 汇总。

幂等性：已存在的未失效源绑定（同 class+ds+table）直接跳过；已 ACTIVE 的同版本
域发布跳过；JOIN 边已存在跳过。重复运行 = no-op。

安全性：拒绝在非 `qa_metadata` 库上运行（防误打测试库/生产库）；`--dry-run`
只打印计划不写库。演练（回滚）另见 --drill。

用法：
    cd backend && uv run python scripts/m1b_confirm_procurement_bindings.py --dry-run
    cd backend && uv run python scripts/m1b_confirm_procurement_bindings.py
    cd backend && uv run python scripts/m1b_confirm_procurement_bindings.py --drill
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.domain.models import (  # noqa: E402
    DataSource,
    OntologyClass,
    OntologyProperty,
    SchemaCache,
)
from app.domain.procurement_binding_models import ProcurementSourceBinding  # noqa: E402
from app.domain.procurement_object_catalog import allHeaderClassNames  # noqa: E402
from app.domain.schemas import OntologyJoinCreate  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.schemas.procurement_binding import (  # noqa: E402
    PropertyBindingCreate,
    SourceBindingCreate,
)
from app.services.ontology_service import OntologyService  # noqa: E402
from app.services.procurement_binding_service import (  # noqa: E402
    ProcurementBindingService,
)
from app.services.semantic_registry_service import semanticRegistry  # noqa: E402
from app.services.semantic_release_service import (  # noqa: E402
    semanticReleaseService,
)

ACTOR = "m1-b-confirm"
DEPARTMENTS: tuple[str, ...] | None = None
RELEASE_VERSION = "v-m1b-confirm-1"
DRILL_VERSION = "v-m1b-drill-1"
RELEASE_NOTE = "M1-b 绑定确认批：9 头类源/属性绑定确认 + 域首发布（Harness/changes/2026-11-20-m1b-binding-confirm）"

_PRICE_LIST_HEADER = "DWD_SUPPLIER_PRICE_LIST_HEADER"
_PRICE_LIST = "DWD_SUPPLIER_PRICE_LIST"


async def assertDevDatabase(session: AsyncSession) -> None:
    """护栏：只允许在开发元数据库上跑（本脚本写治理数据）。"""
    dbName = (await session.execute(text("SELECT current_database()"))).scalar_one()
    if dbName != "qa_metadata":
        raise SystemExit(
            f"拒绝运行：当前库是 {dbName!r}，本脚本只允许在 qa_metadata（dev 5433）上执行"
        )


#: 需要存在的 JOIN 边（端点必须是**活跃**类——编译器会丢弃任一端已墓碑的边）。
#: 依据：_compileJoins 跳过死端点边 ⇒ 绑定类的既有边全连死类 ⇒ 判孤岛（ERROR）。
_JOIN_EDGES: tuple[tuple[str, str, str, str, str], ...] = (
    # (src 类名, src 列, tgt 类名, tgt 列, 说明)
    (_PRICE_LIST_HEADER, "PRICE_LIST_CODE", _PRICE_LIST, "PRICE_LIST_CODE",
     "M1-b：QUOT 报价头 → 报价行（PRICE_LIST_CODE）"),
    ("DWD_PURCHASE_INVOICE", "SUPPLIER_CODE", "DIM_SUPPLIER", "BPSNUM_0",
     "M1-b：采购发票 → 供应商主数据（SUPPLIER_CODE，复用 GOODS_RECEIPT_DTL 同款口径）"),
    ("DWD_SUPPLIER_PAYMENT", "SUPPLIER_CODE", "DIM_SUPPLIER", "BPSNUM_0",
     "M1-b：采购付款 → 供应商主数据（SUPPLIER_CODE）"),
    ("DWD_PURCHASE_REQUISITION_LINE", "MATERIAL_CODE", "DIM_IMATERIAL", "ITMREF_0",
     "M1-b：请购行 → 物料主数据（MATERIAL_CODE）"),
)


async def ensureJoinEdges(session: AsyncSession, dryRun: bool) -> None:
    """按 _JOIN_EDGES 补齐缺失边（幂等）：同端点+同列组合的边已存在 ⇒ 跳过。"""
    nameById = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(OntologyClass.id, OntologyClass.class_name).where(
                    OntologyClass.valid_to.is_(None)
                )
            )
        ).all()
    }
    byName = {v: k for k, v in nameById.items()}
    existing = (
        await session.execute(
            text(
                "SELECT source_class_id, source_columns, target_class_id, target_columns "
                "FROM ontology_join"
            )
        )
    ).all()
    existingKeys = {
        (r[0], tuple(r[1] or []), r[2], tuple(r[3] or [])) for r in existing
    }
    for srcName, srcCol, tgtName, tgtCol, note in _JOIN_EDGES:
        srcId, tgtId = byName.get(srcName), byName.get(tgtName)
        if srcId is None or tgtId is None:
            raise SystemExit(f"JOIN 边端点不存在或已墓碑：{srcName} / {tgtName}")
        if (srcId, (srcCol,), tgtId, (tgtCol,)) in existingKeys:
            print(f"[join] 已存在 {srcName}.{srcCol} → {tgtName}.{tgtCol} ⇒ 跳过")
            continue
        print(f"[join] 补边 {srcName}.{srcCol} → {tgtName}.{tgtCol}")
        if not dryRun:
            await OntologyService().createJoin(
                session,
                OntologyJoinCreate(
                    source_class_id=srcId,
                    source_columns=[srcCol],
                    target_class_id=tgtId,
                    target_columns=[tgtCol],
                    join_type="INNER",
                    relation_type="business",
                    description=note,
                ),
                actor=ACTOR,
            )


async def resolveDatasourceForTable(
    session: AsyncSession, sourceTable: str
) -> tuple[int, str]:
    """找唯一「活跃 + schema_cache 命中该表」的数据源（与召回过滤同口径）。

    ⚠️ schema_name 取**含该表的那一行缓存**——单源多 owner 是实证事实
    （ds1 有 OUTLN/THBI/ZJTH 三行），取第一行会拿到错误 owner。
    """
    hits: list[tuple[int, str]] = []
    upper = sourceTable.upper()
    for ds in (
        await session.execute(
            select(DataSource).where(DataSource.is_active.is_(True))
        )
    ).scalars():
        rows = (
            await session.execute(
                select(SchemaCache).where(SchemaCache.datasource_id == ds.id)
            )
        ).scalars().all()
        for row in rows:
            names = {
                (t.get("table_name") or "").upper()
                for t in (row.schema_data or [])
                if t.get("table_name")
            }
            qualified = {
                f"{t.get('owner')}.{t.get('table_name')}".upper()
                for t in (row.schema_data or [])
                if t.get("table_name") and t.get("owner")
            }
            if upper in names or upper in qualified:
                hits.append((ds.id, row.schema_name or ds.name))
                break
    if len(hits) != 1:
        raise SystemExit(
            f"拒绝绑定：{sourceTable} 在活跃数据源中命中 {len(hits)} 个（须恰好 1 个）：{hits}"
        )
    return hits[0]


async def confirmClassBinding(
    session: AsyncSession,
    svc: ProcurementBindingService,
    className: str,
    dryRun: bool,
) -> str:
    """单个头类：建源绑定 + 属性绑定 + 全部确认（幂等）。返回动作摘要。"""
    cls = (
        await session.execute(
            select(OntologyClass).where(
                OntologyClass.class_name == className,
                OntologyClass.valid_to.is_(None),
            )
        )
    ).scalar_one()
    if cls is None:
        raise SystemExit(f"类 {className} 不存在或已墓碑")
    datasourceId, schemaName = await resolveDatasourceForTable(session, cls.source_table)

    existing = (
        await session.execute(
            select(ProcurementSourceBinding).where(
                ProcurementSourceBinding.class_id == cls.id,
                ProcurementSourceBinding.datasource_id == datasourceId,
                ProcurementSourceBinding.source_table == cls.source_table,
                ProcurementSourceBinding.valid_to.is_(None),
            )
        )
    ).scalars().first()
    if existing is not None and not existing.is_candidate:
        return f"[skip] {className}：绑定 id={existing.id} 已确认"
    if existing is not None:
        source = existing
        print(f"[reuse] {className}：已有候选绑定 id={existing.id}，继续确认流程")
    else:
        if dryRun:
            print(
                f"[plan] {className}: ds={datasourceId} schema={schemaName} "
                f"table={cls.source_table} role=BASE"
            )
            return "[plan]"
        source = await svc.createSourceBinding(
            session,
            SourceBindingCreate(
                class_id=cls.id,
                datasource_id=datasourceId,
                schema_name=schemaName,
                source_table=cls.source_table,
                binding_role="BASE",
                filter_expression=None,
                filter_allowlist=[],
            ),
            actor=ACTOR,
        )
        print(f"[bind] {className}: 源绑定 id={source.id}（候选）")

    # 属性绑定：source_column 非空的全绑；is_primary_key 按 property_name 序标 1..N
    props = (
        await session.execute(
            select(OntologyProperty)
            .where(
                OntologyProperty.class_id == cls.id,
                OntologyProperty.source_column.is_not(None),
            )
            .order_by(OntologyProperty.property_name)
        )
    )
    props = props.scalars().all()
    keyProps = [p for p in props if p.is_primary_key]
    keyOrdinalByPropId = {
        p.id: ordinal + 1 for ordinal, p in enumerate(keyProps)
    }
    pendingProps = []
    for p in props:
        pb = (
            await session.execute(
                text(
                    "SELECT id FROM procurement_property_binding "
                    "WHERE source_binding_id = :sid AND property_id = :pid "
                    "AND valid_to IS NULL"
                ),
                {"sid": source.id, "pid": p.id},
            )
        ).scalar_one_or_none()
        if pb is not None:
            pendingProps.append((p, pb, True))
        else:
            pendingProps.append((p, None, False))
    if not dryRun:
        for p, _pbId, exists in pendingProps:
            if exists:
                continue
            await svc.createPropertyBinding(
                session,
                source.id,
                PropertyBindingCreate(
                    property_id=p.id,
                    source_column=p.source_column,
                    key_ordinal=keyOrdinalByPropId.get(p.id),
                    transform_expression=None,
                ),
                actor=ACTOR,
            )
    keyNote = f"（键 {len(keyProps)} 列）" if keyProps else ""
    print(
        f"[props] {className}: {len(props)} 属性绑定{keyNote}"
        + ("（dry-run 不写）" if dryRun else "")
    )
    if dryRun:
        return "[plan]"

    # 确认序：先全部属性，再源绑定（service 强制该顺序）
    active = (
        await session.execute(
            text(
                "SELECT id, is_candidate FROM procurement_property_binding "
                "WHERE source_binding_id = :sid AND valid_to IS NULL "
                "ORDER BY id"
            ),
            {"sid": source.id},
        )
    ).all()
    for pbId, isCandidate in active:
        if not isCandidate:
            continue
        await svc.confirmPropertyBinding(
            session, source.id, pbId, actor=ACTOR
        )
    await svc.confirmSourceBinding(session, source.id, actor=ACTOR)
    print(f"[confirm] {className}: 源绑定 id={source.id} 已确认（版本冻结点）")
    return "[confirm]"


async def runRelease(session: AsyncSession, dryRun: bool) -> None:
    """域首发布（幂等）：已有 ACTIVE 域发布且绑定齐 ⇒ 跳过；DRAFT（陈旧预览）⇒ 换后缀。"""
    activeNow = await semanticRegistry.getActive(session, domainCode="PROCUREMENT")
    if activeNow is not None and len(activeNow.manifest.get("sourceBindings") or []) >= 8:
        print(
            f"[release] 已有 ACTIVE 域发布 id={activeNow.id} 且 sourceBindings="
            f"{len(activeNow.manifest.get('sourceBindings'))} ⇒ 跳过创建"
        )
        return
    existing = (
        await session.execute(
            text(
                "SELECT id, status FROM semantic_release "
                "WHERE version = :v AND domain_code = 'PROCUREMENT'"
            ),
            {"v": RELEASE_VERSION},
        )
    ).first()
    version = RELEASE_VERSION
    if existing is not None and existing[1] in ("ACTIVE", "VALIDATED"):
        print(f"[release] 版本 {version} 已存在（status={existing[1]}）⇒ 跳过创建")
        releaseId = existing[0]
    else:
        if existing is not None:
            # DRAFT = 上次失败的陈旧预览（manifest 是编译时快照，不可复用）⇒ 换后缀
            for suffix in range(2, 100):
                taken = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM semantic_release WHERE version = :v "
                            "AND domain_code = 'PROCUREMENT'"
                        ),
                        {"v": f"{RELEASE_VERSION}-{suffix}"},
                    )
                ).scalar_one_or_none()
                if taken is None:
                    version = f"{RELEASE_VERSION}-{suffix}"
                    break
            print(f"[release] 版本 {RELEASE_VERSION} 被陈旧 DRAFT 占用 ⇒ 改用 {version}")
        if dryRun:
            print(f"[plan] 域发布 version={version} domain=PROCUREMENT")
            return
        op = await semanticReleaseService.createDraft(
            session,
            version=version,
            domainCode="PROCUREMENT",
            actor=ACTOR,
            departments=DEPARTMENTS,
            note=RELEASE_NOTE,
        )
        releaseId = op.release.id
        print(f"[release] DRAFT id={releaseId}")
        op = await semanticReleaseService.markValidated(
            session, releaseId, actor=ACTOR, departments=DEPARTMENTS
        )
        for blocker in op.blockers:
            print(f"[blocker] {blocker[:200]}")
        print(f"[release] VALIDATED id={releaseId}（blockers={len(op.blockers)}）")
        await semanticReleaseService.activate(
            session, releaseId, actor=ACTOR, departments=DEPARTMENTS
        )
        print(f"[release] ACTIVE id={releaseId}")

    # 校验输出：manifest 逐段计数 + checks 汇总
    active = await semanticRegistry.getActive(session, domainCode="PROCUREMENT")
    if active is None:
        raise SystemExit("发布后取不到 ACTIVE 快照（异常）")
    manifest = active.manifest or {}
    classes = manifest.get("classes") or []
    bindings = manifest.get("sourceBindings") or []
    metrics = manifest.get("metrics") or []
    print(
        f"[verify] manifest: classes={len(classes)} metrics={len(metrics)} "
        f"sourceBindings={len(bindings)}"
    )
    if len(classes) < 8:
        raise SystemExit(f"闸门 #7 不达标：manifest classes={len(classes)} < 8")
    if len(bindings) < 8:
        raise SystemExit(f"闸门 #7 不达标：sourceBindings={len(bindings)} < 8")
    checkRows = (
        await session.execute(
            text(
                "SELECT check_code, severity, result, count(*) FROM semantic_release_check "
                "WHERE release_id = :rid GROUP BY 1,2,3 ORDER BY 1"
            ),
            {"rid": active.id},
        )
    ).all()
    for code, severity, result, n in checkRows:
        print(f"[check] {code} {severity}/{result} ×{n}")
    errors = [r for r in checkRows if r[1] == "ERROR" and r[2] == "FAIL"]
    if errors:
        raise SystemExit(f"ACTIVE 快照仍带 ERROR 校验项：{errors}")
    print("[verify] M1-b 闸门 #7 达标（≥8 类 + 属性 + join + 指标 + 源绑定，且全确认）")


async def runDrill(session: AsyncSession) -> None:
    """回滚演练（M1-a 结构闸第 4 条）：副本式回滚 → 再激活 ⇒ manifestHash 一致。

    以当前 ACTIVE 的域发布为源新建 {DRILL_VERSION} 副本（D-11），激活后比对
    manifest_hash —— 相等即「回滚后内容一致」记录在案。
    """
    active = await semanticRegistry.getActive(session, domainCode="PROCUREMENT")
    if active is None:
        raise SystemExit("演练前提：需要已有 ACTIVE 域发布（先跑主流程）")
    existingHash = active.manifest_hash
    existing = (
        await session.execute(
            text(
                "SELECT id, status FROM semantic_release WHERE version = :v "
                "AND domain_code = 'PROCUREMENT'"
            ),
            {"v": DRILL_VERSION},
        )
    ).first()
    if existing is None:
        op = await semanticReleaseService.rollback(
            session,
            active.id,
            version=DRILL_VERSION,
            actor=ACTOR,
            departments=DEPARTMENTS,
            note="M1-a 结构闸第 4 条：回滚演练（副本式 D-11）",
        )
        drillId = op.release.id
        # D-11 副本式回滚：副本**出生即 ACTIVE**（状态链在 rollback 内部完成）
        print(f"[drill] 回滚副本 id={drillId}（出生即 ACTIVE，原 {active.id} 同事务归档）")
    else:
        drillId = existing[0]
        print(f"[drill] 版本 {DRILL_VERSION} 已存在（status={existing[1]}）⇒ 复核哈希")
    after = await semanticRegistry.getActive(session, domainCode="PROCUREMENT")
    if after.manifest_hash != existingHash:
        raise SystemExit(
            f"演练失败：回滚后 manifest_hash 不一致 "
            f"({existingHash!r} → {after.manifest_hash!r})"
        )
    print(f"[drill] ✅ 回滚后 manifest_hash 一致 = {after.manifest_hash[:16]}…（M1-a 第 4 条记录在案）")


async def main() -> None:
    dryRun = "--dry-run" in sys.argv
    drill = "--drill" in sys.argv
    factory = getSessionFactory()
    async with factory() as session:
        await assertDevDatabase(session)
        if drill:
            await runDrill(session)
            return
        svc = ProcurementBindingService()
        await ensureJoinEdges(session, dryRun)
        for className in allHeaderClassNames():
            print(await confirmClassBinding(session, svc, className, dryRun))
        if dryRun:
            print("[dry-run] 未写库；去掉 --dry-run 执行")
            return
        await runRelease(session, dryRun)


if __name__ == "__main__":
    asyncio.run(main())
