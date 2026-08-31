"""seed_ontology Neo4j 同步单元测试（#65）。

验证 seed_ontology 的 Neo4j 同步逻辑：
- _syncToNeo4j：按 CLASSES/PROPERTIES 幂等 upsert 类/属性节点并建立关系
- seed：属性创建后记录 pid 并触发 _syncToNeo4j

Neo4j 客户端通过 monkeypatch mock，不依赖外部服务。
"""

from __future__ import annotations

import pytest

import app.infrastructure.neo4j_client as neo4j
import seed_ontology
from seed_ontology import BUSINESS_JOINS, CLASSES, PROPERTIES, _syncToNeo4j


class _Recorder:
    """记录每次调用的位置参数与关键字参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


def _fakeMaps() -> tuple[dict[str, int], dict[tuple[int, str], int]]:
    """构造与 seed() 一致的 cid/pid 映射（不依赖数据库）。"""
    cid: dict[str, int] = {c["source_table"]: i for i, c in enumerate(CLASSES, start=1)}
    pid: dict[tuple[int, str], int] = {}
    for src_table, props in PROPERTIES.items():
        class_id = cid[src_table]
        for j, p in enumerate(props, start=1):
            pid[(class_id, p["name"])] = class_id * 1000 + j
    return cid, pid


def _totalProperties() -> int:
    return sum(len(props) for props in PROPERTIES.values())


def _totalFkProperties() -> int:
    """镜像 _syncToNeo4j 的 linkPropertyReferences 调用：FK 目标类存在于 CLASSES 时才调用。"""
    cid = {c["source_table"]: i for i, c in enumerate(CLASSES, start=1)}
    n = 0
    for src_table, props in PROPERTIES.items():
        for p in props:
            fk_table = p.get("fk")
            if not fk_table:
                continue
            if fk_table not in cid:
                continue  # 目标类不存在 → KeyError → except → skip
            n += 1
    return n


def _expectedJoinCount() -> int:
    """镜像 _seedJoins 的去重逻辑，计算 join 目录应产生的边数。

    单主键外键数 + curated 数，按 join_key 去重（源类/源列 → 目标类/目标列）。
    """
    from app.services.ontology_service import makeJoinKey

    cid = {c["source_table"]: i for i, c in enumerate(CLASSES, start=1)}
    pk_cols = {
        src: [p["col"] for p in props if p.get("pk")]
        for src, props in PROPERTIES.items()
    }
    keys: set[str] = set()
    for src_table, props in PROPERTIES.items():
        for p in props:
            fk_table = p.get("fk")
            if not fk_table:
                continue
            tgt_pks = pk_cols.get(fk_table, [])
            if len(tgt_pks) != 1:
                continue
            keys.add(makeJoinKey(cid[src_table], [p["col"]], cid[fk_table], tgt_pks))
    for src_table, src_cols, tgt_table, tgt_cols, _, _ in BUSINESS_JOINS:
        keys.add(makeJoinKey(cid[src_table], src_cols, cid[tgt_table], tgt_cols))
    return len(keys)


@pytest.fixture(autouse=True)
def recorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Recorder]:
    """将 neo4j_client 的写入函数替换为记录器，供断言调用次数与参数。"""
    result: dict[str, _Recorder] = {
        "upsertClassNode": _Recorder(),
        "upsertPropertyNode": _Recorder(),
        "linkClassHasProperty": _Recorder(),
        "linkPropertyReferences": _Recorder(),
    }
    for name, recorder in result.items():
        monkeypatch.setattr(neo4j, name, recorder)
    return result


def testSyncToNeo4jUpsertsEveryClass(recorders: dict[str, _Recorder]) -> None:
    cid, pid = _fakeMaps()
    _syncToNeo4j(cid, pid)

    assert len(recorders["upsertClassNode"].calls) == len(CLASSES)
    firstKw = recorders["upsertClassNode"].calls[0][1]
    assert firstKw["id"] == cid[CLASSES[0]["source_table"]]
    assert firstKw["name"] == CLASSES[0]["class_name"]
    assert firstKw["sourceTable"] == CLASSES[0]["source_table"]


def testSyncToNeo4jUpsertsAndLinksEveryProperty(
    recorders: dict[str, _Recorder],
) -> None:
    cid, pid = _fakeMaps()
    _syncToNeo4j(cid, pid)

    assert len(recorders["upsertPropertyNode"].calls) == _totalProperties()
    # 每个属性都建立 HAS_PROPERTY 关系
    assert len(recorders["linkClassHasProperty"].calls) == _totalProperties()
    # 外键属性建立 REFERENCES 关系
    assert len(recorders["linkPropertyReferences"].calls) == _totalFkProperties()

    # 每个 PG 属性 id 都被 upsert 到 Neo4j，且与 pid 记录一致
    propIds = {kw["id"] for _, kw in recorders["upsertPropertyNode"].calls}
    assert propIds == set(pid.values())
    # 抽查：属性名非空、isForeignKey 与 pid 记录一致
    firstKw = recorders["upsertPropertyNode"].calls[0][1]
    assert firstKw["name"]
    assert firstKw["id"] > 0


def testPropertyHelperCarriesAliasesAndDesc() -> None:
    """P() 扩展：携带业务别名与列描述，供 schema prompt 注入（报价语义可达）。"""
    from seed_ontology import P

    p = P("单价", "PRI_0", "DECIMAL", aliases=["报价"], desc="供应商报价")
    assert p["aliases"] == ["报价"]
    assert p["desc"] == "供应商报价"


def testPriceUnitPropertyHasBusinessAliases() -> None:
    """PPRICLIST.单价(PRI_0) 配置供应商报价类业务别名，让比价类查询命中报价表。"""
    price = next(
        p for p in PROPERTIES["PPRICLIST"]
        if p["name"] == "单价" and p["alias"] == "PRI_0"
    )
    assert price.get("aliases") == ["报价", "供应商报价", "采购报价"]
    assert "报价" in (price.get("desc") or "")


def testPurchaseOrderToPriceDetailJoinExists() -> None:
    """BUSINESS_JOINS 含 PORDERQ.ITMREF_0 → PPRICLIST.PLICRI2_0 直达边（比价查询）。"""
    edges = [
        j for j in BUSINESS_JOINS
        if j[0] == "PORDERQ" and j[2] == "PPRICLIST" and j[3] == ["PLICRI2_0"]
    ]
    assert edges, "缺少 PORDERQ → PPRICLIST 直达 join"


def testPriceListMaterialCodePointsToRealColumn() -> None:
    """PPRICLIST 物料编码必须映射到 PLICRI2_0（实证存物料），而非恒为空的 CPNITMREF_0。"""
    plicri2 = [
        p for p in PROPERTIES["PPRICLIST"]
        if p["col"] == "PLICRI2_0" and p.get("fk") == "ITMMASTER"
    ]
    assert plicri2, "PPRICLIST 缺少 PLICRI2_0 → ITMMASTER 的物料编码 FK 属性"
    assert plicri2[0]["name"] == "物料编码"
    assert "价格条件3" in (plicri2[0].get("aliases") or [])
    # 错误映射必须消失：CPNITMREF_0 在本库恒为空格，绝不应作为物料 FK 暴露
    bad = [
        p for p in PROPERTIES["PPRICLIST"]
        if p["col"] == "CPNITMREF_0" and p.get("fk") == "ITMMASTER"
    ]
    assert not bad, "PPRICLIST 仍把空列 CPNITMREF_0 当作物料 FK"


def testSyncToNeo4jLinksFkToCorrectRefClass(
    recorders: dict[str, _Recorder],
) -> None:
    cid, pid = _fakeMaps()
    _syncToNeo4j(cid, pid)

    # 验证每条 REFERENCES 都指向合法的属性与目标类（位置参数调用）
    refCalls = recorders["linkPropertyReferences"].calls
    assert len(refCalls) > 0
    for args, _ in refCalls:
        propertyId, refClassId = args
        assert propertyId in pid.values()
        assert refClassId in set(cid.values())


def testSyncToNeo4jToleratesNeo4jFailure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        neo4j,
        "upsertClassNode",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("neo4j down")),
    )
    cid, pid = _fakeMaps()
    # 不抛出异常（best-effort，PG 数据已就绪）
    _syncToNeo4j(cid, pid)


async def testSeedRecordsPidAndInvokesSyncToNeo4j(
    seedEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, engine = seedEngine
    monkeypatch.setattr(seed_ontology, "getEngine", lambda: engine)
    monkeypatch.setattr(seed_ontology, "getSessionFactory", lambda: factory)
    syncCalls: list[tuple] = []
    monkeypatch.setattr(
        seed_ontology,
        "_syncToNeo4j",
        lambda cid, pid, mid=None: syncCalls.append((cid, pid, mid)),
    )

    await seed_ontology.seed()

    # seed() 结束时恰好触发一次 Neo4j 同步
    assert len(syncCalls) == 1
    cid, pid, mid = syncCalls[0]
    assert len(cid) == len(CLASSES)
    # pid 覆盖全部属性，且其 class_id 全部来自 cid
    assert len(pid) == _totalProperties()
    classIds = set(cid.values())
    for classId, _ in pid:
        assert classId in classIds


async def testSeedCreatesPriceUnitWithAliasesAndDescription(
    seedEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """seed 创建 PPRICLIST.单价(PRI_0) 时落库业务别名与说明（比价类查询可达）。"""
    from sqlalchemy import select

    from app.domain.models import OntologyClass, OntologyProperty

    factory, engine = seedEngine
    monkeypatch.setattr(seed_ontology, "getEngine", lambda: engine)
    monkeypatch.setattr(seed_ontology, "getSessionFactory", lambda: factory)
    monkeypatch.setattr(seed_ontology, "_syncToNeo4j", lambda cid, pid, mid=None: None)

    await seed_ontology.seed()

    async with factory() as session:
        cls = (
            await session.execute(
                select(OntologyClass).where(OntologyClass.source_table == "PPRICLIST")
            )
        ).scalar_one()
        prop = (
            await session.execute(
                select(OntologyProperty).where(
                    OntologyProperty.class_id == cls.id,
                    OntologyProperty.property_name == "单价",
                )
            )
        ).scalar_one()
        assert prop.property_alias == "PRI_0"
        assert prop.business_aliases == ["报价", "供应商报价", "采购报价"]
        assert prop.description and "报价" in prop.description


async def testSeedJoinsMaterializesEdgesAndIsIdempotent(
    seedEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_seedJoins：外键（写对目标主键列）+ curated 业务流转，幂等去重。

    断言：join 边数 = 单主键外键数 + curated 数；BPTNUM_0 → BPARTNER 的目标列
    是 BPRNUM_0（而非同名 BPTNUM_0）；重跑不增行。
    """
    from sqlalchemy import select

    from app.domain.models import OntologyClass, OntologyJoin

    factory, engine = seedEngine
    monkeypatch.setattr(seed_ontology, "getEngine", lambda: engine)
    monkeypatch.setattr(seed_ontology, "getSessionFactory", lambda: factory)
    # 不依赖 Neo4j：seed() 内 _syncToNeo4j 替换为 no-op（本测试只关心 join 目录）
    monkeypatch.setattr(seed_ontology, "_syncToNeo4j", lambda cid, pid, mid=None: None)

    await seed_ontology.seed()

    async with factory() as session:
        joins = (await session.execute(select(OntologyJoin))).scalars().all()
        classes = (await session.execute(select(OntologyClass))).scalars().all()

    assert len(joins) == _expectedJoinCount()

    # class_id → source_table（反向解析，供具体边断言）
    tableById = {c.id: c.source_table for c in classes}

    def hasEdge(srcTable: str, srcCols: list[str], tgtTable: str, tgtCols: list[str]) -> bool:
        for j in joins:
            if (
                tableById.get(j.source_class_id) == srcTable
                and tableById.get(j.target_class_id) == tgtTable
                and j.source_columns == srcCols
                and j.target_columns == tgtCols
            ):
                return True
        return False

    # 修复点：PRECEIPT.BPTNUM_0（承运人）→ BPCARRIER.BCRNUM_0（而非 BPARTNER.BPRNUM_0）
    assert hasEdge("PRECEIPT", ["BPTNUM_0"], "BPCARRIER", ["BCRNUM_0"])
    assert not hasEdge("PRECEIPT", ["BPTNUM_0"], "BPARTNER", ["BPTNUM_0"])
    # curated 业务流转：收货明细 → 采购订单明细（复合列对）
    assert hasEdge("PRECEIPTD", ["POHNUM_0", "POPLIN_0"], "PORDERQ", ["POHNUM_0", "POPLIN_0"])
    # 比价直达：采购订单明细 → 供应商价格明细（物料编码在 PLICRI2_0）
    assert hasEdge("PORDERQ", ["ITMREF_0"], "PPRICLIST", ["PLICRI2_0"])

    # 幂等：重跑不增行
    await seed_ontology.seed()
    async with factory() as session:
        joinsAfter = (await session.execute(select(OntologyJoin))).scalars().all()
    assert len(joinsAfter) == len(joins)
