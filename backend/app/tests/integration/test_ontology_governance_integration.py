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

from sqlalchemy import select, text

from app.domain.enums import ObjectType
from app.domain.models import OntologyClass


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
        import seed_ontology
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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

        assert len(rows) == len(CLASSES)
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
