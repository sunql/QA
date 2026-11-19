"""Phase 3.4 本体治理字段集成测试（真实 PG 5433）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL + 完整 API 链路，
禁止 sqlite 内存库。client / dbSession fixtures 走 _pg_support.pgApiClient()，
每测试 TRUNCATE 隔离。

覆盖：
1. 迁移后 ontology_class 含 object_type / object_owner 列
2. API 创建类携带治理字段 -> 读回一致；非法 object_type 422
3. API 更新治理字段 -> 生效
4. 列表返回治理字段
5. seed 回填 27 类治理字段 + 重跑幂等
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ObjectType
from app.domain.models import (
    BusinessObject,
    DataDomain,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
)
from app.services.semantic_compiler import compileFromRows
from app.services.semantic_domain_slice import sliceManifestByDomain


class TestGovernanceMigration:
    async def test_columns_exist_after_migration(self, dbSession) -> None:
        """Alembic upgrade head 后 ontology_class 含 2 个治理列。"""
        result = await dbSession.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'ontology_class'"
            )
        )
        cols = {row[0] for row in result}
        assert "object_type" in cols
        assert "object_owner" in cols


class TestGovernanceApi:
    async def test_create_class_with_governance_fields(self, client) -> None:
        """Phase 4.5：objectOwner 由服务端从 actor.departments[0] 派生，
        DTO 已移除 objectOwner 字段（防越权）。
        """
        response = await client.post(
            "/api/v1/ontology/classes",
            json={
                "className": "GovernanceOrder",
                "classAlias": "治理订单",
                "sourceTable": "t_gov_order",
                "objectType": "Transaction",
            },
            headers={"X-User-Departments": "procurement"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["objectType"] == "Transaction"
        # objectOwner 由服务端按部门派生，不再来自 body
        assert data["objectOwner"] == "procurement"

    async def test_create_class_rejects_invalid_object_type(self, client) -> None:
        response = await client.post(
            "/api/v1/ontology/classes",
            json={"className": "BadType", "objectType": "Nonsense"},
        )
        assert response.status_code == 422

    async def test_update_class_governance_fields(self, client) -> None:
        """Update DTO 中 objectOwner 已移除（防越权转移 owner）。
        本测试仅验 objectType 更新；objectOwner 修改需走独立特权接口。
        """
        created = await client.post(
            "/api/v1/ontology/classes",
            json={"className": "UpdOrder", "sourceTable": "t_upd"},
            headers={"X-User-Departments": "procurement"},
        )
        cid = created.json()["id"]
        response = await client.put(
            f"/api/v1/ontology/classes/{cid}",
            json={"objectType": "Reference"},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
        assert response.status_code == 200
        assert response.json()["objectType"] == "Reference"
        # objectOwner 保留创建时的派生值
        assert response.json()["objectOwner"] == "procurement"

    async def test_update_clears_governance_fields_with_null(self, client) -> None:
        """显式 null 清空：PUT objectType:null 应落 NULL（前端清空下拉依赖此契约）。

        objectOwner 不再支持 Update 清空（DTO 移除）；admin 改 owner 需走独立接口。
        """
        created = await client.post(
            "/api/v1/ontology/classes",
            json={
                "className": "ClearOrder",
                "objectType": "Transaction",
            },
        )
        cid = created.json()["id"]
        assert created.json()["objectType"] == "Transaction"

        response = await client.put(
            f"/api/v1/ontology/classes/{cid}",
            json={"objectType": None},
        headers={"X-User-Id": "test-admin", "X-User-Roles": "admin"},
        )
        assert response.status_code == 200
        assert response.json()["objectType"] is None

    async def test_list_includes_governance_fields(self, client) -> None:
        await client.post(
            "/api/v1/ontology/classes",
            json={
                "className": "ListOrder",
                "objectType": "Master",
            },
            headers={"X-User-Departments": "procurement"},
        )
        listed = await client.get("/api/v1/ontology/classes")
        assert listed.status_code == 200
        match = next(c for c in listed.json() if c["className"] == "ListOrder")
        assert match["objectType"] == "Master"
        assert match["objectOwner"] == "procurement"

    async def test_get_class_returns_governance_fields(self, client) -> None:
        created = await client.post(
            "/api/v1/ontology/classes",
            json={"className": "GetOrder", "objectType": "Event"},
            headers={"X-User-Departments": "procurement"},
        )
        cid = created.json()["id"]
        got = await client.get(f"/api/v1/ontology/classes/{cid}")
        assert got.json()["objectType"] == "Event"
        assert got.json()["objectOwner"] == "procurement"


class TestGovernanceSeedBackfill:
    async def _runSeed(self, monkeypatch) -> None:
        """seed() 走独立真实 PG 引擎（seed 内部会 dispose，故每跑新建引擎）。"""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        import seed_ontology
        from app.tests import _pg_support

        url = _pg_support.resolveTestDatabaseUrl()
        engine = create_async_engine(url, echo=False)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(seed_ontology, "getEngine", lambda: engine)
        monkeypatch.setattr(seed_ontology, "getSessionFactory", lambda: factory)
        monkeypatch.setattr(seed_ontology, "_syncToNeo4j", lambda cid, pid, mid=None: None)
        await seed_ontology.seed()

    async def test_seed_backfills_governance_fields(self, client, dbSession, monkeypatch) -> None:
        """seed 后 27 类治理字段与 CLASSES 契约一致。"""
        from seed_ontology import CLASSES

        await self._runSeed(monkeypatch)

        rows = (await dbSession.execute(select(OntologyClass))).scalars().all()
        byTable = {r.source_table: r for r in rows}
        valid = {m.value for m in ObjectType}
        seedByTable = {c["source_table"]: c for c in CLASSES}

        # 库里的行数 = len(CLASSES) + 1：多出来的是 conftest 补种的
        # IncomingInspection（不在 seed_ontology.CLASSES 里，但 seed_business_objects
        # 要用它解析 IQC 的 header_class_id ⇒ 必须补种）。
        # ⇒ 不能拿「总行数 == len(CLASSES)」当判据（2026-11-18 实测 28 != 27）；
        # 判据是「CLASSES 的每个 source_table 恰好一行、无重复」，并把
        # 「额外行只允许是补种 header 类」钉死（不是放宽断言）。
        classRows = [r for r in rows if r.source_table in seedByTable]
        assert len(classRows) == len(CLASSES), "CLASSES 的类应恰好各一行"
        assert len({r.source_table for r in classRows}) == len(CLASSES), "CLASSES 的类不应重复"

        extraRows = [r for r in rows if r.source_table not in seedByTable]
        assert {r.class_name for r in extraRows} <= {"IncomingInspection"}, (
            f"CLASSES 之外只允许 conftest 补种的 IncomingInspection，实得 "
            f"{ {r.class_name for r in extraRows} }"
        )

        for table, seed in seedByTable.items():
            row = byTable[table]
            assert row.object_type == seed["object_type"], table
            assert row.object_type in valid, table
            assert row.object_owner == seed["object_owner"], table

    async def test_seed_re_run_is_idempotent(self, client, dbSession, monkeypatch) -> None:
        """治理字段回填幂等：重跑后类数量与字段值不变。"""
        await self._runSeed(monkeypatch)
        before = (await dbSession.execute(select(OntologyClass))).scalars().all()
        await self._runSeed(monkeypatch)
        after = (await dbSession.execute(select(OntologyClass))).scalars().all()
        assert len(after) == len(before)
        byTable = {r.source_table: r.object_type for r in after}
        assert byTable["PORDER"] == "Transaction"
        assert byTable["ITMMASTER"] == "Master"


# ===========================================================================
# M1-1.4：采购本体对象核对与补齐（迁移 0158）
# ===========================================================================
#
# 为什么并进本文件而不是新建 integration 文件：
# CI 集成测试按「find | sort 的下标 mod 4」切分 ⇒ 新建文件会重排分片 ⇒ 假 net-new。
# 本文件已有真实 PG + 完整链路夹具 ⇒ 直接并入。
#
# 测什么（**用真实 PG + 真实 SQL**，不 mock）：
# 1. 复活：墓碑类被清 valid_to（原地复活 ⇒ class_id 不变 ⇒ 指标源不丢）；
# 2. 口径不丢：复活前指标源被编译器**整条丢弃**，复活后保留 ⇒ 这是本批的核心不变量；
# 3. 补 BO：4 个新 BO 落库（头类 / 域 / 图标签）；
# 4. 改指：INVOICE 的头类从 DWD_AP_INVOICE_DTL 换到 DWD_PURCHASE_INVOICE；
# 5. 幂等：跑两遍结果相同；
# 6. no-op：Lineage A 环境（无 DWD_* 类）里整批不改（测试库本身就是该形状）；
# 7. 可逆：revert 回到补齐前的状态。
#
# 业务后果（域切片可达性）：补齐后 KPI 指标才**归得了属**⇒ 进得了采购域切片。

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "0158_procurement_object_reconciliation.py"
)


def _loadReconciliationMigration():
    """加载迁移 0158（自包含模块，只含常量与函数 ⇒ 可安全 exec）。"""
    assert _MIGRATION_PATH.exists(), f"迁移文件不存在：{_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("_mig_0158", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seedClass(
    dbSession: AsyncSession, className: str, *, tombstone: bool
) -> int:
    """种一个本体类（可选墓碑态），返回 id。"""
    cls = OntologyClass(class_name=className, source_table=className, version=1)
    if tombstone:
        cls.valid_to = datetime(2026, 9, 19, 4, 4, tzinfo=UTC)
    dbSession.add(cls)
    await dbSession.commit()
    await dbSession.refresh(cls)
    return int(cls.id)


async def _applyStatements(dbSession: AsyncSession, module, statements) -> None:
    """在**异步**会话上执行迁移语句序列。

    迁移的 ``op.get_bind()`` 是同步 Connection，而测试跑在异步 Session 上 ⇒
    两条路径执行器不同 ⇒ 迁移把 SQL 抽成「语句序列」（纯函数），
    两条路径消费**同一份 SQL** ⇒ 不会各写一份而漂移。
    """
    for sql, params in statements:
        await dbSession.execute(text(sql), params)


_NEW_BO_CODES: tuple[str, ...] = ("PAY", "POL", "PR", "QUOT")
"""本批新增的 4 个 BO 码（与迁移 ``_NEW_BUSINESS_OBJECTS`` 一致）。"""


async def _newBusinessObjects(
    dbSession: AsyncSession,
) -> list[tuple[str, str, str]]:
    """返回本批新增 BO 的 (code, graph_label, 头类名)，按 code 升序。

    为什么**按码过滤**而不是整表计数
    ------------------------------
    集成 conftest 的 autouse fixture 会补种 5 个**基线** BO
    （SUPPLIER / MATERIAL / PO / GR / IQC，来自 ``seed_business_objects``）
    ⇒ ``business_object`` 整表计数**恒 > 0** ⇒ 断言整表计数等于 0 必然假红。
    断言必须**收窄到本批的 4 个码**（它们不与基线码重叠）。
    """
    placeholders = ", ".join(
        f"CAST(:c{index} AS varchar(20))" for index in range(len(_NEW_BO_CODES))
    )
    params = {f"c{index}": code for index, code in enumerate(_NEW_BO_CODES)}
    rows = (
        await dbSession.execute(
            text(
                "SELECT b.code, b.graph_label, c.class_name "
                "  FROM business_object b "
                "  JOIN ontology_class c ON c.id = b.header_class_id "
                f" WHERE b.code IN ({placeholders}) "
                " ORDER BY b.code"
            ),
            params,
        )
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def _compileNow(dbSession: AsyncSession) -> dict:
    """按**真实治理表**编译一份 manifest（与发布路径同款编译器）。"""
    classes = (await dbSession.execute(select(OntologyClass))).scalars().all()
    metrics = (await dbSession.execute(select(OntologyMetric))).scalars().all()
    sources = (
        await dbSession.execute(select(OntologyMetricSource))
    ).scalars().all()
    businessObjects = (
        await dbSession.execute(select(BusinessObject))
    ).scalars().all()
    domains = (await dbSession.execute(select(DataDomain))).scalars().all()
    return compileFromRows(
        classes=classes,
        properties=[],
        joins=[],
        metrics=metrics,
        metricSources=sources,
        kpis=[],
        businessObjects=businessObjects,
        businessObjectDomains=[],
        domains=domains,
        datasources=[],
    )


class TestProcurementObjectReconciliationMigration:
    """M1-1.4 迁移 0158 的行为（真实 PostgreSQL）。"""

    async def test_revives_the_tombstone_class_in_place(self, dbSession) -> None:
        """复活是**原地**的（清 valid_to）⇒ class_id 不变 ⇒ 指标源仍指向同一行。"""
        module = _loadReconciliationMigration()
        classId = await _seedClass(
            dbSession, "DWD_SUPPLIER_PAYMENT", tombstone=True
        )

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()

        row = (
            await dbSession.execute(
                text("SELECT valid_to FROM ontology_class WHERE id = :i"),
                {"i": classId},
            )
        ).first()
        assert row is not None
        assert row[0] is None, "墓碑类应被复活（valid_to 清空）"

    async def test_metric_source_is_dropped_before_and_kept_after(
        self, dbSession
    ) -> None:
        """**口径不丢**：墓碑类的指标源会被编译器整条丢弃 ⇒ 复活才保住它。"""
        module = _loadReconciliationMigration()
        classId = await _seedClass(
            dbSession, "DWD_PURCHASE_ORDER_LINE", tombstone=True
        )
        metric = OntologyMetric(
            metric_name="KPI_TEST_OTD",
            metric_alias="KPI_TEST_OTD",
            formula="口径声明：测试用",
            agg_function="COUNT",
            metric_type="RATIO",
        )
        dbSession.add(metric)
        await dbSession.commit()
        await dbSession.refresh(metric)
        dbSession.add(
            OntologyMetricSource(
                metric_id=metric.id, class_id=classId, role="NUMERATOR"
            )
        )
        await dbSession.commit()

        before = await _compileNow(dbSession)
        beforeMetric = next(
            item for item in before["metrics"] if item["metricName"] == "KPI_TEST_OTD"
        )
        assert beforeMetric["sources"] == [], "墓碑类的源应被丢弃（这正是要修的缺陷）"

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()

        after = await _compileNow(dbSession)
        afterMetric = next(
            item for item in after["metrics"] if item["metricName"] == "KPI_TEST_OTD"
        )
        assert afterMetric["sources"] == [
            {"className": "DWD_PURCHASE_ORDER_LINE", "role": "NUMERATOR"}
        ]

    async def test_inserts_the_four_business_objects(self, dbSession) -> None:
        """补 4 个 BO（头类必须已活跃 ⇒ 头类缺失时整行不插）。"""
        module = _loadReconciliationMigration()
        await _seedClass(dbSession, "DWD_PURCHASE_REQUISITION_LINE", tombstone=False)
        await _seedClass(
            dbSession, "DWD_SUPPLIER_PRICE_LIST_HEADER", tombstone=False
        )
        await _seedClass(dbSession, "DWD_PURCHASE_ORDER_LINE", tombstone=False)
        await _seedClass(dbSession, "DWD_SUPPLIER_PAYMENT", tombstone=False)
        await dbSession.commit()

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()

        rows = (
            await dbSession.execute(
                text(
                    "SELECT code, primary_domain_id, graph_label, header_class_id "
                    "FROM business_object "
                    "WHERE code IN (CAST(:c0 AS varchar(20)), CAST(:c1 AS varchar(20)), "
                    "CAST(:c2 AS varchar(20)), CAST(:c3 AS varchar(20))) "
                    "ORDER BY code"
                ),
                {
                    "c0": "PAY",
                    "c1": "POL",
                    "c2": "PR",
                    "c3": "QUOT",
                },
            )
        ).all()
        codes = [row[0] for row in rows]
        assert codes == ["PAY", "POL", "PR", "QUOT"]
        for row in rows:
            assert row[1] == "PROCUREMENT", "BO 必须挂在采购域"
            assert row[2], "BO 必须有图标签"
            assert row[3], "BO 必须有头类"

        # 图标签 = 头类名（读路径按 graphLabel 找类）
        labelled = await _newBusinessObjects(dbSession)
        assert labelled == [
            ("PAY", "DWD_SUPPLIER_PAYMENT", "DWD_SUPPLIER_PAYMENT"),
            ("POL", "DWD_PURCHASE_ORDER_LINE", "DWD_PURCHASE_ORDER_LINE"),
            ("PR", "DWD_PURCHASE_REQUISITION_LINE", "DWD_PURCHASE_REQUISITION_LINE"),
            ("QUOT", "DWD_SUPPLIER_PRICE_LIST_HEADER", "DWD_SUPPLIER_PRICE_LIST_HEADER"),
        ]

    async def test_repoints_the_invoice_business_object(self, dbSession) -> None:
        """INVOICE 的头类：DWD_AP_INVOICE_DTL（46 行旁表）→ DWD_PURCHASE_INVOICE。

        降级路径也在这里验证（原先是零覆盖）：``graph_label`` 故意种成
        **非类名**，证明降级是**读 journal 还原原值**，而不是把类名硬编码猜回去。
        """
        module = _loadReconciliationMigration()
        oldId = await _seedClass(dbSession, "DWD_AP_INVOICE_DTL", tombstone=False)
        newId = await _seedClass(
            dbSession, "DWD_PURCHASE_INVOICE", tombstone=True
        )
        oldLabel = "发票（文件抽取旁表）"
        dbSession.add(
            BusinessObject(
                code="INVOICE",
                name="发票",
                header_class_id=oldId,
                primary_domain_id="PROCUREMENT",
                graph_label=oldLabel,
            )
        )
        await dbSession.commit()

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()

        row = (
            await dbSession.execute(
                text(
                    "SELECT header_class_id, graph_label FROM business_object "
                    "WHERE code = 'INVOICE'"
                )
            )
        ).first()
        assert row is not None
        assert row[0] == newId, "INVOICE 的头类应改指采购发票头"
        assert row[1] == "DWD_PURCHASE_INVOICE"

        await _applyStatements(dbSession, module, module.revertStatements())
        await dbSession.commit()

        reverted = (
            await dbSession.execute(
                text(
                    "SELECT header_class_id, graph_label FROM business_object "
                    "WHERE code = 'INVOICE'"
                )
            )
        ).first()
        assert reverted is not None
        assert reverted[0] == oldId, "降级应把头类改回"
        assert reverted[1] == oldLabel, "降级应还原**原** graph_label（journal）"

    async def test_is_idempotent(self, dbSession) -> None:
        """跑两遍结果相同（不重复插 BO、不重复改指）。"""
        module = _loadReconciliationMigration()
        await _seedClass(dbSession, "DWD_PURCHASE_ORDER_LINE", tombstone=True)
        await dbSession.commit()

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()
        first = await _newBusinessObjects(dbSession)

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()
        second = await _newBusinessObjects(dbSession)

        assert first == second, "幂等 ⇒ 跑两遍结果相同"
        assert [row[0] for row in second] == ["POL"], (
            "幂等 ⇒ 第二次不应多出 BO（POL 的头类已存在 ⇒ 只补它）"
        )

    async def test_is_a_noop_without_lineage_b_classes(self, dbSession) -> None:
        """Lineage A 环境（无 DWD_* 类）⇒ 整批不改、不报错。"""
        module = _loadReconciliationMigration()

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()

        # Lineage A 环境：只有基线业务名类（Supplier / ItemMaster …）⇒ 无 DWD_* 类
        dwdClasses = (
            await dbSession.execute(
                text("SELECT count(*) FROM ontology_class WHERE class_name LIKE 'DWD%'")
            )
        ).first()
        assert dwdClasses[0] == 0, "Lineage A 环境不应有 DWD_* 类"
        assert await _newBusinessObjects(dbSession) == [], (
            "Lineage A 环境 ⇒ 本批的 4 个 BO 一个都不该插"
        )

    async def test_revert_restores_the_previous_state(self, dbSession) -> None:
        """降级：类恢复**原墓碑时间戳** + 新增 BO 删除 + 头类改回（见上一个用例）。

        ⚠️ 这里同时钉住「**不误伤**」：升级前就是活跃状态的同名类
        **不得**被降级墓碑化（旧实现按 ``valid_to IS NULL`` 全局命中 ⇒ 会误改
        不属于本迁移的数据）。
        """
        module = _loadReconciliationMigration()
        classId = await _seedClass(
            dbSession, "DWD_PURCHASE_REQUISITION_LINE", tombstone=True
        )
        # 同名但本就活跃的类（模拟「别的环境/种子已复活过」）
        activeId = await _seedClass(
            dbSession, "DWD_PURCHASE_INVOICE_LINE", tombstone=False
        )
        await dbSession.commit()

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()
        await _applyStatements(dbSession, module, module.revertStatements())
        await dbSession.commit()

        row = (
            await dbSession.execute(
                text("SELECT valid_to FROM ontology_class WHERE id = :i"),
                {"i": classId},
            )
        ).first()
        assert row is not None and row[0] is not None, "降级后应重新墓碑化"
        assert row[0] == datetime(2026, 9, 19, 4, 4, tzinfo=UTC), (
            "降级应恢复**原**墓碑时间戳（journal），而不是降级执行时刻"
        )
        assert await _newBusinessObjects(dbSession) == [], "降级后新增的 BO 应被删除"

        survivor = (
            await dbSession.execute(
                text("SELECT valid_to FROM ontology_class WHERE id = :i"),
                {"i": activeId},
            )
        ).first()
        assert survivor is not None and survivor[0] is None, (
            "本迁移从未复活的活跃类，降级不得墓碑化"
        )

    async def test_all_header_classes_are_active_after_apply(self, dbSession) -> None:
        """``ACTIVE_HEADER_CLASSES`` 不是手写声明：跑完迁移后实证全部活跃。

        （单测里只能断言「目录自称是这 6 个」；真实活跃性只有真库能验。）
        """
        from app.domain.procurement_object_catalog import ACTIVE_HEADER_CLASSES

        module = _loadReconciliationMigration()
        tombstoned = {
            "DWD_PURCHASE_REQUISITION_LINE",
            "DWD_PURCHASE_ORDER_LINE",
            "DWD_SUPPLIER_PAYMENT",
            "DWD_PURCHASE_INVOICE",
            "DWD_PURCHASE_INVOICE_LINE",
            "DWD_GOODS_RECEIPT_LINE",
        }
        for className in ACTIVE_HEADER_CLASSES:
            await _seedClass(dbSession, className, tombstone=className in tombstoned)
        await dbSession.commit()

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()

        placeholders = ", ".join(
            f"CAST(:n{index} AS varchar(100))"
            for index in range(len(ACTIVE_HEADER_CLASSES))
        )
        params = {
            f"n{index}": className
            for index, className in enumerate(ACTIVE_HEADER_CLASSES)
        }
        rows = (
            await dbSession.execute(
                text(
                    "SELECT class_name, valid_to FROM ontology_class "
                    f"WHERE class_name IN ({placeholders})"
                ),
                params,
            )
        ).all()
        assert {row[0] for row in rows} == set(ACTIVE_HEADER_CLASSES)
        assert all(row[1] is None for row in rows), (
            f"头类应全部活跃，实得 {[(r[0], r[1]) for r in rows if r[1] is not None]}"
        )

    async def test_procurement_slice_reaches_the_metric_after_reconciliation(
        self, dbSession
    ) -> None:
        """**业务后果**：补齐后采购 KPI 才归得了属 ⇒ 进得了采购域切片。

        切片口径（``semantic_domain_slice``）：类的集合 = 业务对象的头类 + 父类闭包；
        指标按 ``targetClassName`` / ``sources[].className`` 命中该集合。
        墓碑类不在活跃模型 ⇒ 指标源被丢弃 ⇒ 指标归不了属 ⇒ 被 fail-closed 排除。
        """
        module = _loadReconciliationMigration()
        classId = await _seedClass(
            dbSession, "DWD_PURCHASE_ORDER_LINE", tombstone=True
        )
        metric = OntologyMetric(
            metric_name="KPI_TEST_OTD",
            metric_alias="KPI_TEST_OTD",
            formula="口径声明：测试用",
            agg_function="COUNT",
            metric_type="RATIO",
        )
        dbSession.add(metric)
        await dbSession.commit()
        await dbSession.refresh(metric)
        dbSession.add(
            OntologyMetricSource(
                metric_id=metric.id, class_id=classId, role="BASE"
            )
        )
        await dbSession.commit()

        before = sliceManifestByDomain(await _compileNow(dbSession), "PROCUREMENT")
        assert "KPI_TEST_OTD" not in {m["metricName"] for m in before.metrics}

        await _applyStatements(dbSession, module, module.reconciliationStatements())
        await dbSession.commit()

        after = sliceManifestByDomain(await _compileNow(dbSession), "PROCUREMENT")
        assert "KPI_TEST_OTD" in {m["metricName"] for m in after.metrics}
        assert "DWD_PURCHASE_ORDER_LINE" in after.classNames
