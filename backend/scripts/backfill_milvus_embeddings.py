"""回填 Milvus 本体向量（一次性数据修复）。

背景：ontology_embeddings 集合存在但实体残缺——类向量有、绝大多数属性向量缺失
（历史 490 个种子属性从未建向量），`_selectRelevantClasses` 按类/属性文本向量
召回，属性向量缺失会让基于属性语义的检索失灵。本脚本为本体类**及其属性**生成
向量并写入 Milvus，可全量回填，也可用 `--sources` 只处理指定表。

文本构造（与运行时 `/ontology/embeddings/sync` 及 apply_price_ontology.py 一致）：
- 类：`" ".join(class_name, class_alias, description)`
- 属性：`" ".join(property_name, *business_aliases, description)`（中文名+业务别名+说明，
  使「报价」这类自然语言能命中属性）

密钥来源（只读，不写入代码）：与 app 运行配置一致——`Settings` 从进程环境变量
及 cwd 的 `.env` 读取，embedding 取 EMBEDDING_API_KEY 或 OPENAI_API_KEY
（见 app/config.py）；显式配置 base_url 时用占位 key（keyless 本地服务）。
Milvus 默认连 localhost:19530（docker-compose 的 qa-milvus 已发布该端口）。
注意：不要加载 ../docker/.env——那是容器内配置，其 DATABASE_URL 指向 postgres 主机，
本地无法解析。

幂等：syncEmbedding 按 ontology_id 先删后插，重复运行安全。

用法：
    EMBEDDING_API_KEY=... uv run python scripts/backfill_milvus_embeddings.py [--dry-run]
    uv run python scripts/backfill_milvus_embeddings.py --sources PPRICCONF,PPRICLIST
    uv run python scripts/backfill_milvus_embeddings.py --skip-classes
    uv run python scripts/backfill_milvus_embeddings.py --cleanup

[--dry-run] 只验证全链路（连库、取类/属性、embedding 生成），不写入 Milvus；
注意 dry-run 仍会调用 embedding API 产生费用。
[--sources A,B] 只处理 source_table 在列表中的类（含其属性）；缺省为全部类。
[--skip-classes] 只回填属性，跳过类的 embedding（类向量已就绪时省调用）。
[--cleanup] 确定性收敛：以 PG 为唯一真源，把集合重建为「去重后 PG 全量」。
Milvus 批量 delete-then-insert 在重负载下不可靠（全量回填 532 次同步后残留
重复行），故 cleanup 改为 读全量 -> 按 (ontology_id, type) 去重（保留最新）->
删集重建 -> 一次整批插入，并补回 PG 有而 Milvus 缺失的向量（不重新嵌入已有）。
与普通回填互斥。
"""

from __future__ import annotations

import asyncio
import sys

from app.domain.models import OntologyClass, OntologyProperty
from app.infrastructure import milvus_client as milvus
from app.infrastructure.database import getSessionFactory
from app.services.embedding_service import EmbeddingService
from app.services.ontology_service import OntologyService


def _parseArgs(
    argv: list[str],
) -> tuple[bool, set[str] | None, bool, bool]:
    dryRun = "--dry-run" in argv
    skipClasses = "--skip-classes" in argv
    cleanup = "--cleanup" in argv
    sources: set[str] | None = None
    if "--sources" in argv:
        idx = argv.index("--sources")
        if idx + 1 >= len(argv):
            raise SystemExit("用法错误：--sources 需要参数（逗号分隔的 source_table 列表）")
        raw = argv[idx + 1]
        sources = {s.strip() for s in raw.split(",") if s.strip()}
    if cleanup and (sources or skipClasses):
        raise SystemExit("用法错误：--cleanup 与 --sources/--skip-classes 互斥")
    return dryRun, sources, skipClasses, cleanup


def _classText(cls: OntologyClass) -> str:
    return " ".join(x for x in (cls.class_name, cls.class_alias, cls.description) if x)


def _propertyText(prop: OntologyProperty) -> str:
    return " ".join(
        filter(
            None,
            [
                prop.property_name,
                *(prop.business_aliases or []),
                prop.description or "",
            ],
        )
    )


def _rowDetails(row: dict) -> tuple[str, str, str]:
    """Milvus 行的 (name, alias, description)；None 归一为 ""（与 PG 比对口径一致）。"""
    return (row.get("name") or "", row.get("alias") or "", row.get("description") or "")


async def _backfill(
    dryRun: bool, sources: set[str] | None, skipClasses: bool
) -> int:
    svc = OntologyService()
    embedding = EmbeddingService()
    factory = getSessionFactory()
    synced = failed = 0

    async with factory() as session:
        classes = await svc.listClasses(session)
        for cls in classes:
            if sources and cls.source_table not in sources:
                continue
            tag = f"{cls.class_name} [{cls.source_table}]"
            if not skipClasses:
                try:
                    vec = await embedding.generateEmbedding(_classText(cls))
                    if not dryRun:
                        svc.syncEmbedding(
                            ontologyId=cls.id,
                            type="class",
                            name=cls.class_name,
                            alias=cls.class_alias,
                            description=cls.description,
                            embedding=vec,
                        )
                    synced += 1
                    print(f"  ok class id={cls.id} {tag} dim={len(vec)}")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print(f"  FAIL class id={cls.id} {tag}: {exc}")

            props = await svc.listPropertiesByClass(session, cls.id)
            for prop in props:
                try:
                    vec = await embedding.generateEmbedding(_propertyText(prop))
                    if not dryRun:
                        svc.syncEmbedding(
                            ontologyId=prop.id,
                            type="property",
                            name=prop.property_name,
                            alias=prop.property_alias,
                            description=prop.description,
                            embedding=vec,
                        )
                    synced += 1
                    print(
                        f"  ok prop id={prop.id} {cls.class_name}.{prop.property_name} "
                        f"dim={len(vec)}"
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print(
                        f"  FAIL prop id={prop.id} {cls.class_name}.{prop.property_name}: {exc}"
                    )

    suffix = " [dry-run]" if dryRun else ""
    if sources:
        suffix += f" [sources={','.join(sorted(sources))}]"
    if skipClasses:
        suffix += " [skip-classes]"
    print(f"Milvus 本体向量回填: {synced} 成功 / {failed} 失败{suffix}")
    return failed


async def _cleanup(dryRun: bool) -> int:
    """确定性收敛 ontology_embeddings：读全量 -> 去重 -> 补缺失 -> 删集重建 -> 整批插入。

    背景：Milvus 批量 delete-then-insert 在重负载下不可靠（本次全量回填 532 次
    同步后残留 835 行 / 299 重复）。与其逐条增量删除，不如以 PG 为唯一真源重建：
    读取当前全部向量，按 (ontology_id, type) 去重保留最新，对 PG 有而 Milvus 无的
    实体补生成向量，然后删集、重建空集合、一次性插入全部去重行——不依赖批量 delete。

    内容过期检测：历史 id 漂移（旧版 updateClass 的 INSERT 新行策略）会留下
    「ontology_id 指向了改换语义的类」的陈旧向量（如 id=18 旧名 PurchaseOrder、
    现名 SupplierPriceDetail）。此类向量 key 存在但内容错误，仅按 key 对账会漏掉；
    故对已有向量逐一比对 name/alias/description 与 PG 当前值，不一致即重新生成。

    指标（metric）不参与：本体指标只删不插（见 deleteMetric），本不产生向量。
    """
    svc = OntologyService()
    embedding = EmbeddingService()
    factory = getSessionFactory()
    failed = 0

    # 1) PG 期望集合（唯一真源）
    async with factory() as session:
        classes = await svc.listClasses(session)
        properties: list[OntologyProperty] = []
        for cls in classes:
            properties.extend(await svc.listPropertiesByClass(session, cls.id))
    expected = {(c.id, "class") for c in classes} | {
        (p.id, "property") for p in properties
    }
    # key -> PG 当前 (name, alias, description)，用于内容过期比对（与插入口径一致：None 归一为 ""）
    expectedDetails: dict[tuple[int, str], tuple[str, str, str]] = {
        **{(c.id, "class"): (c.class_name, c.class_alias or "", c.description or "") for c in classes},
        **{(p.id, "property"): (p.property_name, p.property_alias or "", p.description or "") for p in properties},
    }
    print(f"PG 期望: {len(classes)} 类 + {len(properties)} 属性 = {len(expected)}")

    # 2) Milvus 现状
    rows = milvus.listAllEmbeddings()
    present = {(r["ontology_id"], r["type"]) for r in rows}
    print(f"Milvus 当前: {len(rows)} 行")

    # 3) 补缺失（PG 有而 Milvus 无；如被手删的 oid=1 属性）
    #    先批量生成向量再一次性整批插入：逐条 syncEmbedding（delete+insert+flush）
    #    在当前 Milvus 部署下单条可达 10-25s，批量场景必须整批一次 flush。
    missingKeys = sorted(expected - present)
    if missingKeys:
        print(f"补缺失 {len(missingKeys)} 个实体: {missingKeys}")
        classById = {c.id: c for c in classes}
        propById = {p.id: p for p in properties}
        records: list[dict] = []
        for oid, typ in missingKeys:
            # 分支取具体类型对象，避免 union-attr；text 与 _backfill 文本构造一致
            if typ == "class":
                missingClass = classById.get(oid)
                if missingClass is None:
                    failed += 1
                    print(f"  FAIL 找不到 PG 类 oid={oid}")
                    continue
                name, alias, description, text = (
                    missingClass.class_name, missingClass.class_alias,
                    missingClass.description, _classText(missingClass),
                )
            else:
                missingProp = propById.get(oid)
                if missingProp is None:
                    failed += 1
                    print(f"  FAIL 找不到 PG 属性 oid={oid}")
                    continue
                name, alias, description, text = (
                    missingProp.property_name, missingProp.property_alias,
                    missingProp.description, _propertyText(missingProp),
                )
            try:
                vec = await embedding.generateEmbedding(text)
                records.append({
                    "ontology_id": oid,
                    "type": typ,
                    "name": name,
                    "alias": alias,
                    "description": description,
                    "embedding": vec,
                })
                print(f"  ok 重生成 oid={oid} type={typ} {name} dim={len(vec)}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAIL 重生成 oid={oid} type={typ}: {exc}")
        if records and not dryRun:
            milvus.insertEmbeddings(records)
            print(f"  整批插入 {len(records)} 条（单次 flush）")
        if not dryRun:
            rows = milvus.listAllEmbeddings()
            print(f"补缺失后 Milvus: {len(rows)} 行")

    # 4) 按 (oid,type) 去重保留最新（auto_id 最大），并剔除 PG 之外的陈旧向量
    best: dict[tuple[int, str], dict] = {}
    for r in rows:
        key = (r["ontology_id"], r["type"])
        if key in expected and (key not in best or r["id"] > best[key]["id"]):
            best[key] = r

    # 4.5) 内容过期检测：name/alias/description 与 PG 不一致（历史 id 漂移遗留）→ 重生成。
    #      key 存在但内容错误仅靠 key 对账抓不到，必须比字段（id=18 旧名 PurchaseOrder 即此症）。
    #      批量执行：按 type 批量 delete + 整批 insert，避免逐条 flush（单次 10-25s）。
    staleKeys = sorted(
        key
        for key, row in best.items()
        if _rowDetails(row) != expectedDetails[key]
    )
    if staleKeys:
        print(f"内容过期 {len(staleKeys)} 条，重生成: {staleKeys}")
        classById = {c.id: c for c in classes}
        propById = {p.id: p for p in properties}
        records: list[dict] = []
        for oid, typ in staleKeys:
            try:
                if typ == "class":
                    entity, text = classById[oid], _classText(classById[oid])
                    name, alias, description = (
                        entity.class_name, entity.class_alias, entity.description,
                    )
                else:
                    entity, text = propById[oid], _propertyText(propById[oid])
                    name, alias, description = (
                        entity.property_name, entity.property_alias, entity.description,
                    )
                vec = await embedding.generateEmbedding(text)
                records.append({
                    "ontology_id": oid, "type": typ, "name": name,
                    "alias": alias or "", "description": description or "",
                    "embedding": vec,
                })
                print(f"  ok 过期重生成 oid={oid} type={typ} {name} dim={len(vec)}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAIL 过期重生成 oid={oid} type={typ}: {exc}")
        if records and not dryRun:
            for typ in ("class", "property"):
                ids = [r["ontology_id"] for r in records if r["type"] == typ]
                milvus.deleteByOntologyIds(ids, typ)
            milvus.insertEmbeddings(records)
            print(f"  过期批量替换 {len(records)} 条（批量 delete + 整批 insert）")
        for r in records:
            best[(r["ontology_id"], r["type"])] = {
                "id": float("inf"),
                **r,
            }

    deduped = list(best.values())
    print(
        f"去重后: {len(deduped)} = "
        f"{sum(1 for r in deduped if r['type'] == 'class')} 类 + "
        f"{sum(1 for r in deduped if r['type'] == 'property')} 属性"
    )

    # 5) 删集重建 + 整批插入（丢弃不可靠的批量 delete 路径）
    if dryRun:
        print("[dry-run] 未删集重建")
        return failed
    milvus.dropCollection()
    milvus.ensureCollection()
    milvus.insertEmbeddings([
        {
            "ontology_id": r["ontology_id"],
            "type": r["type"],
            "name": r["name"],
            "alias": r["alias"],
            "description": r["description"],
            "embedding": r["embedding"],
        }
        for r in deduped
    ])

    # 6) 校验：收敛到期望集合，无缺失、无陈旧 key、无重复、内容不过期
    finalRows = milvus.listAllEmbeddings()
    finalKeys = {(r["ontology_id"], r["type"]) for r in finalRows}
    stillMissing = sorted(expected - finalKeys)
    stale = sorted(finalKeys - expected)
    hasDuplicates = len(finalRows) != len(finalKeys)
    contentStale = sorted(
        (r["ontology_id"], r["type"])
        for r in finalRows
        if (r["ontology_id"], r["type"]) in expectedDetails
        and _rowDetails(r) != expectedDetails[(r["ontology_id"], r["type"])]
    )
    print(f"重建后: {len(finalRows)} 行（期望 {len(expected)}）")
    if hasDuplicates:
        failed += 1
        print(f"  FAIL 存在重复: {len(finalRows) - len(finalKeys)} 对")
    if stillMissing:
        failed += 1
        print(f"  FAIL 仍缺失: {stillMissing}")
    if stale:
        failed += 1
        print(f"  FAIL 残留陈旧: {stale}")
    if contentStale:
        failed += 1
        print(f"  FAIL 内容过期: {contentStale}")
    if not failed:
        print("Milvus ontology_embeddings 已收敛（无重复、无缺失、无陈旧 key、内容与 PG 一致）。")
    return failed


async def main() -> None:
    dryRun, sources, skipClasses, cleanup = _parseArgs(sys.argv)
    if cleanup:
        failed = await _cleanup(dryRun=dryRun)
    else:
        failed = await _backfill(dryRun=dryRun, sources=sources, skipClasses=skipClasses)
    if failed > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
