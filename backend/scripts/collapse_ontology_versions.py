"""折叠本体类历史版本（一次性数据修复）。

背景：Phase 6 版本管理曾让 updateClass 产生新主键行，且早期实现未克隆属性、
未同步 Milvus，导致当前版本行属性为空壳（Customer v3 / Supplier v2 等）、
Milvus 向量指向已失效的旧 id——智能问答报"无法生成通过校验的查询计划"
且属性视图为空。版本管理移除后，需要把每类多行折叠回当前行：

- keeper：valid_to IS NULL 的行（listClasses/getClass 使用的行）；
- 把过期版本行上的属性并入 keeper（按 property_name 去重，keeper 已有则保留）；
- 自引用 FK（ref_class_id 指向本类旧行）重映射到 keeper；
- 全局重映射：任意引用指向过期行（parent_class_id / ref_class_id / target_class_id）
  的，改指向对应 keeper；
- 删除过期版本行；
- best-effort 重同步 Milvus embedding（向量由 EmbeddingService 生成）。

幂等：重复运行安全（第二次运行时每类仅一行，无操作）。

用法：
    uv run python scripts/collapse_ontology_versions.py
    uv run python scripts/collapse_ontology_versions.py --dry-run   # 只报告不落库
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import delete, select, update

from app.domain.models import (
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
)
from app.infrastructure import milvus_client as milvus
from app.infrastructure.database import getSessionFactory
from app.services.ontology_service import OntologyService


async def _collapse(dryRun: bool) -> None:
    factory = getSessionFactory()
    async with factory() as session:
        rows = (await session.execute(select(OntologyClass))).scalars().all()
        byName: dict[str, list[OntologyClass]] = {}
        for r in rows:
            byName.setdefault(r.class_name, []).append(r)

        # 过期行 id -> keeper id（全局映射，供引用重映射）
        expiredToKeeper: dict[int, int] = {}
        keeperPerName: dict[str, int] = {}
        affected: list[str] = []
        for name, classRows in byName.items():
            keepers = [r for r in classRows if r.valid_to is None]
            if len(keepers) != 1:
                # 全部失效（整体软删除）或异常多行有效：跳过，保持原样
                continue
            keeper = keepers[0]
            expired = [r for r in classRows if r.valid_to is not None]
            if not expired:
                continue
            affected.append(name)
            keeperPerName[name] = keeper.id
            for r in expired:
                expiredToKeeper[r.id] = keeper.id

        if not affected:
            print("没有需要折叠的类（每类均单行）——无需修复。")
            return

        # 1) 属性并入 keeper（property_name 去重，keeper 已有则保留）
        reassignedProps = 0
        droppedProps = 0
        for name in affected:
            keeperId = keeperPerName[name]
            groupIds = [keeperId] + [
                rid for rid, kid in expiredToKeeper.items() if kid == keeperId
            ]
            # 按版本号降序取数：过期行间同名属性保留 version 较高的定义
            # （确定性选择，避免依赖无 ORDER BY 的行序）
            props = (
                await session.execute(
                    select(OntologyProperty)
                    .join(OntologyClass, OntologyClass.id == OntologyProperty.class_id)
                    .where(OntologyProperty.class_id.in_(groupIds))
                    .order_by(OntologyClass.version.desc(), OntologyProperty.id)
                )
            ).scalars().all()
            # seen 预置 keeper 已有属性：过期行同名属性直接丢弃（保留 keeper 最新意图）
            seen: set[str] = {p.property_name for p in props if p.class_id == keeperId}
            for p in props:
                if p.class_id == keeperId:
                    continue
                if p.property_name in seen:
                    droppedProps += 1
                    continue  # 保留 keeper 版本，过期行同名属性随行删除
                seen.add(p.property_name)
                p.class_id = keeperId
                if p.ref_class_id in expiredToKeeper:
                    p.ref_class_id = expiredToKeeper[p.ref_class_id]
                reassignedProps += 1

        # 2) 关键：先 flush，让并入的属性在 DB 上把 class_id 落到 keeper。
        #    生产 database.py 关闭了 autoflush，若不在删除前显式 flush，
        #    后续 delete(props WHERE class_id=过期行) 会按 DB 旧值误删刚并入的属性。
        await session.flush()

        # 3) 全局引用重映射（跨类：过期行可能被任意行引用）
        expiredIds = list(expiredToKeeper.keys())
        await _remapRefs(session, expiredToKeeper)

        # 4) 删除过期版本行（其未并入的重复属性随行删除）
        for rid in expiredIds:
            await session.execute(
                delete(OntologyProperty).where(OntologyProperty.class_id == rid)
            )
            await session.execute(delete(OntologyClass).where(OntologyClass.id == rid))
            # 同步清理该过期 id 的 Milvus 向量（best-effort；否则残留指向已删类的陈旧向量，
            # 语义检索会返回死 id）。Milvus 不可达/无索引时静默跳过，不影响 PG 折叠。
            try:
                milvus.deleteByOntologyId(rid, "class")
            except Exception as exc:  # noqa: BLE001
                print(f"  Milvus 过期向量清理失败 id={rid}: {exc}")

        summary = [
            f"折叠类: {len(affected)}",
            f"删除过期行: {len(expiredIds)}",
            f"并入属性: {reassignedProps}",
            f"丢弃重复属性: {droppedProps}",
        ]
        print(" / ".join(summary))
        for name in sorted(affected):
            print(f"  - {name}: keeper id={keeperPerName[name]}")

        if dryRun:
            print("[dry-run] 未落库")
            return
        await session.commit()
        print("已提交。")

    # 4) best-effort 重同步 Milvus embedding（向量需 LLM 生成，失败仅告警）
    if dryRun:
        return
    svc = OntologyService()
    factory2 = getSessionFactory()
    async with factory2() as session:
        current = await svc.listClasses(session)
        synced = 0
        failed = 0
        for cls in current:
            text = " ".join(
                x for x in (cls.class_name, cls.class_alias, cls.description) if x
            )
            try:
                from app.services.embedding_service import EmbeddingService

                vec = await EmbeddingService().generateEmbedding(text)
                svc.syncEmbedding(
                    ontologyId=cls.id,
                    type="class",
                    name=cls.class_name,
                    alias=cls.class_alias,
                    description=cls.description,
                    embedding=vec,
                )
                synced += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  Milvus 同步失败 id={cls.id} name={cls.class_name}: {exc}")
        print(f"Milvus embedding 重同步: {synced} 成功 / {failed} 失败")


async def _remapRefs(session, expiredToKeeper: dict[int, int]) -> None:
    """把三类引用从过期行 id 改指向对应 keeper id。"""
    cases = {oid: kid for oid, kid in expiredToKeeper.items()}
    for oldId, newId in cases.items():
        await session.execute(
            update(OntologyProperty)
            .where(OntologyProperty.ref_class_id == oldId)
            .values(ref_class_id=newId)
        )
        await session.execute(
            update(OntologyClass)
            .where(OntologyClass.parent_class_id == oldId)
            .values(parent_class_id=newId)
        )
        await session.execute(
            update(OntologyMetric)
            .where(OntologyMetric.target_class_id == oldId)
            .values(target_class_id=newId)
        )


async def main() -> None:
    dryRun = "--dry-run" in sys.argv
    await _collapse(dryRun=dryRun)


if __name__ == "__main__":
    asyncio.run(main())
