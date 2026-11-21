"""采购源绑定 API 集成测试（M1-1.1）——真实 PostgreSQL + 完整 API 链路。

覆盖 HTTP 契约（camelCase JSON）与**业务不变量**：

- 新建一律落**候选**，候选不得出现在 `GET /effective`（决策 D-3）；
- 确认人取**当前登录用户**（不接受客户端传入）；
- 过滤表达式必须给出白名单（越权读闸门）；
- 复合键序号必须从 1 起连续；
- 软失效源绑定 ⇒ 其属性绑定一并失效（不留孤儿映射）；
- 每次写入都留审计（CREATE / UPDATE）。

⚠️ 本文件**自包含**（不依赖 Milvus / Neo4j 夹具）：CI 分片按「排序后下标 mod 4」
切分 integration 文件，新文件会重排分片 ⇒ 跨文件的顺序敏感点必须自查。
本文件只依赖 `client` / `dbSession` 两个夹具（每测试 TRUNCATE 全库 ⇒ 互不依赖）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditLog, DataSource, OntologyClass, OntologyProperty

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
USER_HEADERS = {"X-User-Id": "test-user", "X-User-Roles": "user"}

_API = "/api/v1/procurement-bindings"


async def _seed(
    dbSession: AsyncSession, suffix: str
) -> tuple[int, int, int, int]:
    """种一个本体类（2 个属性）+ 一个数据源。

    返回 `(classId, propertyId1, propertyId2, datasourceId)`。
    """
    cls = OntologyClass(class_name=f"采购订单明细_{suffix}", source_table="ZJTH.DWD_PO_DTL")
    cls.properties = [
        OntologyProperty(property_name="订单数量", data_type="DECIMAL"),
        OntologyProperty(property_name="订单金额", data_type="DECIMAL"),
    ]
    dbSession.add(cls)
    datasource = DataSource(
        name=f"采购源_{suffix}",
        type="oracle",
        host="db.example.internal",
        port=1521,
        database_name="ZJTH",
        username="qa_readonly",
        password_encrypted="encrypted",
        is_active=True,
    )
    dbSession.add(datasource)
    await dbSession.commit()
    await dbSession.refresh(cls)
    await dbSession.refresh(datasource)
    propertyIds = [prop.id for prop in cls.properties]
    return cls.id, propertyIds[0], propertyIds[1], datasource.id


def _sourcePayload(
    classId: int, datasourceId: int, suffix: str, **overrides: object
) -> dict[str, object]:
    """源绑定创建载荷（默认合法）。"""
    payload: dict[str, object] = {
        "classId": classId,
        "datasourceId": datasourceId,
        "schemaName": "ZJTH",
        "sourceTable": "DWD_PURCHASE_ORDER_DTL",
        "bindingRole": "BASE",
    }
    payload.update(overrides)
    return payload


class TestCreateSourceBinding:
    async def test_create_returns_candidate(self, client, dbSession: AsyncSession) -> None:
        classId, _, _, datasourceId = await _seed(dbSession, "A")
        resp = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "A"), headers=ADMIN_HEADERS
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["isCandidate"] is True
        assert body["confirmedBy"] is None
        assert body["confirmedTime"] is None
        assert body["classId"] == classId
        assert body["className"] == "采购订单明细_A"
        assert body["bindingRole"] == "BASE"
        assert body["propertyCount"] == 0

    async def test_create_requires_admin(self, client, dbSession: AsyncSession) -> None:
        """写入口限 admin（绑定是治理事实）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "B")
        resp = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "B"), headers=USER_HEADERS
        )
        assert resp.status_code == 403

    async def test_create_rejects_unknown_class(self, client, dbSession: AsyncSession) -> None:
        """类不存在 ⇒ 422（库层 FK 也会拒，但服务层要先给可读文案）。"""
        _, _, _, datasourceId = await _seed(dbSession, "C")
        resp = await client.post(
            _API, json=_sourcePayload(999999, datasourceId, "C"), headers=ADMIN_HEADERS
        )
        assert resp.status_code == 422
        assert "本体类" in resp.text

    async def test_create_rejects_unknown_datasource(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, _, _, _ = await _seed(dbSession, "D")
        resp = await client.post(
            _API, json=_sourcePayload(classId, 999999, "D"), headers=ADMIN_HEADERS
        )
        assert resp.status_code == 422
        assert "数据源" in resp.text

    async def test_create_rejects_invalid_role(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, _, _, datasourceId = await _seed(dbSession, "E")
        resp = await client.post(
            _API,
            json=_sourcePayload(classId, datasourceId, "E", bindingRole="SOURCE"),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "bindingRole" in resp.text

    async def test_duplicate_key_conflicts(self, client, dbSession: AsyncSession) -> None:
        """同键位（类+源+schema+表+角色）只允许一条生效行 ⇒ 409。"""
        classId, _, _, datasourceId = await _seed(dbSession, "F")
        first = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "F"), headers=ADMIN_HEADERS
        )
        assert first.status_code == 201
        second = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "F"), headers=ADMIN_HEADERS
        )
        assert second.status_code == 409

    async def test_filter_expression_requires_allowlist(
        self, client, dbSession: AsyncSession
    ) -> None:
        """给了过滤表达式却没给白名单 ⇒ 拒绝（不能默认放行 = 越权）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "G")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId, datasourceId, "G", filterExpression="STATUS <> 'X'"
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "白名单" in resp.text

    async def test_filter_expression_rejects_identifier_outside_allowlist(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, _, _, datasourceId = await _seed(dbSession, "H")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "H",
                filterExpression="STATUS <> 'X' AND SECRET_FLAG = 1",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "SECRET_FLAG" in resp.text

    async def test_filter_expression_with_allowlist_accepted(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, _, _, datasourceId = await _seed(dbSession, "I")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "I",
                filterExpression="STATUS <> 'X'",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201
        assert resp.json()["filterAllowlist"] == ["STATUS"]


class TestCandidateVisibility:
    async def test_candidate_not_in_effective_view(
        self, client, dbSession: AsyncSession
    ) -> None:
        """D-3：未确认的候选不得出现在生效视图（唯一对普通用户开放的视图）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "J")
        created = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "J"), headers=ADMIN_HEADERS
        )
        assert created.status_code == 201
        effective = await client.get(f"{_API}/effective", headers=USER_HEADERS)
        assert effective.status_code == 200
        assert effective.json() == []

    async def test_effective_view_open_to_plain_user(
        self, client, dbSession: AsyncSession
    ) -> None:
        """生效视图对普通登录用户开放（不含 requireAdmin）。"""
        effective = await client.get(f"{_API}/effective", headers=USER_HEADERS)
        assert effective.status_code == 200

    async def test_full_list_requires_admin(self, client, dbSession: AsyncSession) -> None:
        """全量列表（含候选）限 admin ⇒ 普通用户不会把候选当结论。"""
        resp = await client.get(_API, headers=USER_HEADERS)
        assert resp.status_code == 403


class TestPropertyBinding:
    async def test_create_property_binding(self, client, dbSession: AsyncSession) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "K")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "K"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        resp = await client.post(
            f"{_API}/{sourceId}/properties",
            json={
                "propertyId": propertyId,
                "sourceColumn": "ORDER_QTY",
                "keyOrdinal": 1,
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["isCandidate"] is True
        assert body["sourceColumn"] == "ORDER_QTY"
        assert body["keyOrdinal"] == 1
        assert body["propertyName"] == "订单数量"

    async def test_property_must_belong_to_same_class(
        self, client, dbSession: AsyncSession
    ) -> None:
        """跨类绑定会让读路径错位 ⇒ 拒绝。"""
        classId, _, _, datasourceId = await _seed(dbSession, "L")
        _, otherPropertyId, _, _ = await _seed(dbSession, "L2")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "L"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        resp = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": otherPropertyId, "sourceColumn": "ORDER_QTY"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "不属于" in resp.text

    async def test_duplicate_property_conflicts(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "M")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "M"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        payload = {"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1}
        first = await client.post(
            f"{_API}/{sourceId}/properties", json=payload, headers=ADMIN_HEADERS
        )
        assert first.status_code == 201
        second = await client.post(
            f"{_API}/{sourceId}/properties", json=payload, headers=ADMIN_HEADERS
        )
        assert second.status_code == 409

    async def test_invalid_column_name_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "N")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "N"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        resp = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "QTY; DROP TABLE X"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422


class TestConfirmFlow:
    async def test_confirm_requires_properties(
        self, client, dbSession: AsyncSession
    ) -> None:
        """没有属性绑定就确认 ⇒ 拒绝（FILTER 角色除外）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "O")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "O"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        resp = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert resp.status_code == 422
        assert "属性绑定" in resp.text

    async def test_filter_role_can_confirm_without_properties(
        self, client, dbSession: AsyncSession
    ) -> None:
        """FILTER 角色不承载列映射 ⇒ 允许零属性确认。"""
        classId, _, _, datasourceId = await _seed(dbSession, "P")
        source = await client.post(
            _API,
            json=_sourcePayload(classId, datasourceId, "P", bindingRole="FILTER"),
            headers=ADMIN_HEADERS,
        )
        sourceId = source.json()["id"]
        resp = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["isCandidate"] is False

    async def test_confirm_records_current_user(
        self, client, dbSession: AsyncSession
    ) -> None:
        """确认人取当前登录用户（请求体不接受确认人字段）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "Q")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "Q"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        prop = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1},
            headers=ADMIN_HEADERS,
        )
        assert prop.status_code == 201
        propId = prop.json()["id"]

        confirmedProp = await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        assert confirmedProp.status_code == 200
        assert confirmedProp.json()["confirmedBy"] == "test-admin"

        confirmed = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert confirmed.status_code == 200
        body = confirmed.json()
        assert body["isCandidate"] is False
        assert body["confirmedBy"] == "test-admin"
        assert body["confirmedTime"] is not None

    async def test_confirmed_binding_visible_in_effective_view(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "R")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "R"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        prop = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1},
            headers=ADMIN_HEADERS,
        )
        propId = prop.json()["id"]
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)

        effective = await client.get(f"{_API}/effective", headers=USER_HEADERS)
        assert effective.status_code == 200
        rows = effective.json()
        assert len(rows) == 1
        assert rows[0]["id"] == sourceId
        assert rows[0]["isCandidate"] is False
        assert rows[0]["propertyCount"] == 1

    async def test_double_confirm_conflicts(self, client, dbSession: AsyncSession) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "S")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "S"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        prop = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1},
            headers=ADMIN_HEADERS,
        )
        propId = prop.json()["id"]
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        first = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert first.status_code == 200
        second = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert second.status_code == 409

    async def test_non_contiguous_key_ordinal_rejected_at_create(
        self, client, dbSession: AsyncSession
    ) -> None:
        """复合键缺位 ⇒ **建的时候就拒**（否则要等到确认才撞墙）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "T")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "T"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        prop = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 2},
            headers=ADMIN_HEADERS,
        )
        assert prop.status_code == 422
        assert "连续" in prop.text

    async def test_non_contiguous_key_ordinal_rejected_at_confirm(
        self, client, dbSession: AsyncSession
    ) -> None:
        """失效掉中间序号造成缺位 ⇒ 确认时仍要拒（连续性闸门是最后一道）。"""
        classId, propertyId1, propertyId2, datasourceId = await _seed(dbSession, "T")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "T"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        first = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId1, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1},
            headers=ADMIN_HEADERS,
        )
        assert first.status_code == 201
        second = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId2, "sourceColumn": "AMT", "keyOrdinal": 2},
            headers=ADMIN_HEADERS,
        )
        assert second.status_code == 201
        await client.post(
            f"{_API}/{sourceId}/properties/{first.json()['id']}/expire",
            headers=ADMIN_HEADERS,
        )
        resp = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert resp.status_code == 422
        assert "连续" in resp.text


class TestExpire:
    async def test_expire_removes_from_effective_and_expires_children(
        self, client, dbSession: AsyncSession
    ) -> None:
        """软失效：父绑定失效 ⇒ 子属性绑定一并失效（不留孤儿映射）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "U")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "U"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        prop = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1},
            headers=ADMIN_HEADERS,
        )
        propId = prop.json()["id"]
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)

        expired = await client.post(f"{_API}/{sourceId}/expire", headers=ADMIN_HEADERS)
        assert expired.status_code == 200
        assert expired.json()["validTo"] is not None

        effective = await client.get(f"{_API}/effective", headers=USER_HEADERS)
        assert effective.json() == []

        detail = await client.get(f"{_API}/{sourceId}", headers=ADMIN_HEADERS)
        assert detail.status_code == 200
        child = detail.json()["properties"][0]
        assert child["validTo"] is not None, "父失效后子绑定必须一并失效"

    async def test_double_expire_conflicts(self, client, dbSession: AsyncSession) -> None:
        classId, _, _, datasourceId = await _seed(dbSession, "V")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "V"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        first = await client.post(f"{_API}/{sourceId}/expire", headers=ADMIN_HEADERS)
        assert first.status_code == 200
        second = await client.post(f"{_API}/{sourceId}/expire", headers=ADMIN_HEADERS)
        assert second.status_code == 409

    async def test_expired_binding_allows_new_binding_same_key(
        self, client, dbSession: AsyncSession
    ) -> None:
        """失效后同键位可再建（部分唯一索引只约束生效行）⇒ 软版本化成立。"""
        classId, _, _, datasourceId = await _seed(dbSession, "W")
        first = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "W"), headers=ADMIN_HEADERS
        )
        firstId = first.json()["id"]
        await client.post(f"{_API}/{firstId}/expire", headers=ADMIN_HEADERS)
        second = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "W"), headers=ADMIN_HEADERS
        )
        assert second.status_code == 201
        assert second.json()["id"] != firstId


class TestDetailAndAudit:
    async def test_detail_contains_properties(self, client, dbSession: AsyncSession) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "X")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "X"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1},
            headers=ADMIN_HEADERS,
        )
        detail = await client.get(f"{_API}/{sourceId}", headers=ADMIN_HEADERS)
        assert detail.status_code == 200
        body = detail.json()
        assert body["binding"]["id"] == sourceId
        assert len(body["properties"]) == 1
        assert body["properties"][0]["propertyId"] == propertyId

    async def test_detail_unknown_id_returns_404(
        self, client, dbSession: AsyncSession
    ) -> None:
        resp = await client.get(f"{_API}/999999", headers=ADMIN_HEADERS)
        assert resp.status_code == 404

    async def test_writes_are_audited(self, client, dbSession: AsyncSession) -> None:
        """每次写入都留审计：CREATE（源绑定）+ CREATE（属性绑定）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "Y")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "Y"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1},
            headers=ADMIN_HEADERS,
        )
        result = await dbSession.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "procurement_source_binding")
            .where(AuditLog.entity_id == sourceId)
        )
        rows = list(result.scalars().all())
        assert len(rows) == 1
        assert rows[0].action == "CREATE"
        assert rows[0].actor == "test-admin"

    async def test_confirm_is_audited_as_update(
        self, client, dbSession: AsyncSession
    ) -> None:
        """候选→确认复用 UPDATE 审计动作（审计动作白名单已闭合，无需新迁移）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "Z")
        source = await client.post(
            _API, json=_sourcePayload(classId, datasourceId, "Z"), headers=ADMIN_HEADERS
        )
        sourceId = source.json()["id"]
        prop = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 1},
            headers=ADMIN_HEADERS,
        )
        propId = prop.json()["id"]
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)

        result = await dbSession.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "procurement_source_binding")
            .where(AuditLog.entity_id == sourceId)
            .order_by(AuditLog.id)
        )
        rows = list(result.scalars().all())
        actions = [row.action for row in rows]
        assert actions == ["CREATE", "UPDATE"]
        updateRow = rows[-1]
        assert updateRow.before_json is not None
        assert updateRow.after_json is not None
        assert updateRow.before_json["isCandidate"] is True
        assert updateRow.after_json["isCandidate"] is False


async def _createSource(client, classId: int, datasourceId: int, suffix: str, **over) -> int:
    """建一条候选源绑定，返回其 id。"""
    resp = await client.post(
        _API, json=_sourcePayload(classId, datasourceId, suffix, **over), headers=ADMIN_HEADERS
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _createProperty(
    client, sourceId: int, propertyId: int, column: str, ordinal: int | None = None, **over
) -> int:
    """在源绑定下建一条候选属性绑定，返回其 id。"""
    payload: dict[str, object] = {"propertyId": propertyId, "sourceColumn": column}
    if ordinal is not None:
        payload["keyOrdinal"] = ordinal
    payload.update(over)
    resp = await client.post(
        f"{_API}/{sourceId}/properties", json=payload, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestConfirmInvariants:
    """确认流转的不变量（代码评审 HIGH-2 / MED-3 / MED-4 + 安全审查 HIGH-1）。"""

    async def test_confirm_source_requires_confirmed_properties(
        self, client, dbSession: AsyncSession
    ) -> None:
        """属性绑定还是候选时确认源绑定 ⇒ 拒绝。

        否则生效视图会出现「父已确认、子还是候选」的空壳绑定。
        """
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AA")
        sourceId = await _createSource(client, classId, datasourceId, "AA")
        await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)

        resp = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert resp.status_code == 422
        assert "确认" in resp.text

    async def test_confirm_source_rejects_expired_binding(
        self, client, dbSession: AsyncSession
    ) -> None:
        """已软失效的绑定不能再被确认（否则产生「已确认但已失效」的僵尸行）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AB")
        sourceId = await _createSource(client, classId, datasourceId, "AB")
        expired = await client.post(f"{_API}/{sourceId}/expire", headers=ADMIN_HEADERS)
        assert expired.status_code == 200

        resp = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert resp.status_code == 422
        assert "失效" in resp.text

    async def test_confirm_property_rejects_expired_property(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AC")
        sourceId = await _createSource(client, classId, datasourceId, "AC")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)
        expired = await client.post(
            f"{_API}/{sourceId}/properties/{propId}/expire", headers=ADMIN_HEADERS
        )
        assert expired.status_code == 200

        resp = await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 422
        assert "失效" in resp.text

    async def test_create_property_under_expired_parent_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """父已失效 ⇒ 不能再挂新子绑定（否则出现「父已过期、子还在生效」的孤儿）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AD")
        sourceId = await _createSource(client, classId, datasourceId, "AD")
        await client.post(f"{_API}/{sourceId}/expire", headers=ADMIN_HEADERS)

        resp = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "失效" in resp.text

    async def test_add_property_to_confirmed_source_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """父已确认 ⇒ 子集合冻结（否则键序号连续性闸门被绕过）。

        复现路径：确认父 → 追加 ordinal=3 的子 → 确认子 ⇒ 生效键序号变成 [1, 3]。
        """
        classId, propertyId1, propertyId2, datasourceId = await _seed(dbSession, "AE")
        sourceId = await _createSource(client, classId, datasourceId, "AE")
        propId = await _createProperty(client, sourceId, propertyId1, "ORDER_QTY", 1)
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        confirmed = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert confirmed.status_code == 200, confirmed.text

        resp = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId2, "sourceColumn": "LINE_AMT", "keyOrdinal": 3},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409
        assert "冻结" in resp.text

    async def test_confirm_property_revalidates_key_ordinals(
        self, client, dbSession: AsyncSession
    ) -> None:
        """属性确认时要**重跑**键序号连续性闸门。

        已失效的序号**不占位** ⇒ 「已失效的序号 1 + 剩下的序号 2」不是连续序列，
        此时确认序号 2 必须被拒（否则生效映射出现缺位键）。
        """
        classId, propertyId1, propertyId2, datasourceId = await _seed(dbSession, "AF")
        sourceId = await _createSource(client, classId, datasourceId, "AF")
        prop1 = await _createProperty(client, sourceId, propertyId1, "ORDER_QTY", 1)
        prop2 = await _createProperty(client, sourceId, propertyId2, "LINE_AMT", 2)
        expired = await client.post(
            f"{_API}/{sourceId}/properties/{prop1}/expire", headers=ADMIN_HEADERS
        )
        assert expired.status_code == 200

        resp = await client.post(
            f"{_API}/{sourceId}/properties/{prop2}/confirm", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 422
        assert "连续" in resp.text

        detail = await client.get(f"{_API}/{sourceId}", headers=ADMIN_HEADERS)
        remaining = [
            item for item in detail.json()["properties"] if item["validTo"] is None
        ]
        assert [item["keyOrdinal"] for item in remaining] == [2]

    async def test_confirm_property_rejects_foreign_source_id(
        self, client, dbSession: AsyncSession
    ) -> None:
        """路径里的父 id 与属性绑定的实际归属必须一致（杜绝跨父确认）。

        ⚠️ 必须用**真实存在的另一个父**来测：用不存在的 id 会因为「父不存在」
        提前 404 ⇒ 归属校验删掉也照样 404 ⇒ 假绿。
        """
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AG")
        sourceId = await _createSource(client, classId, datasourceId, "AG")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)

        otherClassId, _, _, _ = await _seed(dbSession, "AG2")
        otherSourceId = await _createSource(
            client, otherClassId, datasourceId, "AG2", sourceTable="DWD_OTHER_TABLE"
        )

        resp = await client.post(
            f"{_API}/{otherSourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 404
        assert "不属于" in resp.text


class TestPropertyExpire:
    async def test_expire_property_binding(self, client, dbSession: AsyncSession) -> None:
        """单条属性绑定可单独软失效（绑错了列不必把整个源绑定作废）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AH")
        sourceId = await _createSource(client, classId, datasourceId, "AH")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)

        resp = await client.post(
            f"{_API}/{sourceId}/properties/{propId}/expire", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 200
        assert resp.json()["validTo"] is not None

    async def test_expire_property_rejects_foreign_source_id(
        self, client, dbSession: AsyncSession
    ) -> None:
        """属性失效路径同样要校验归属父 id（拿 A 的路径去失效 B 的子绑定 ⇒ 404）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AI2")
        sourceId = await _createSource(client, classId, datasourceId, "AI2")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)

        otherClassId, _, _, _ = await _seed(dbSession, "AI3")
        otherSourceId = await _createSource(
            client, otherClassId, datasourceId, "AI3", sourceTable="DWD_OTHER_TABLE"
        )

        resp = await client.post(
            f"{_API}/{otherSourceId}/properties/{propId}/expire", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 404
        assert "不属于" in resp.text

    async def test_double_expire_property_conflicts(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AI")
        sourceId = await _createSource(client, classId, datasourceId, "AI")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)
        first = await client.post(
            f"{_API}/{sourceId}/properties/{propId}/expire", headers=ADMIN_HEADERS
        )
        assert first.status_code == 200
        second = await client.post(
            f"{_API}/{sourceId}/properties/{propId}/expire", headers=ADMIN_HEADERS
        )
        assert second.status_code == 409

    async def test_expire_property_under_confirmed_source_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """父已确认 ⇒ 子集合冻结 ⇒ 单条失效也不允许（改绑必须走「失效 → 新版本」）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AJ")
        sourceId = await _createSource(client, classId, datasourceId, "AJ")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        confirmed = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert confirmed.status_code == 200, confirmed.text

        resp = await client.post(
            f"{_API}/{sourceId}/properties/{propId}/expire", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 409
        assert "冻结" in resp.text

    async def test_cascade_expire_audits_children(
        self, client, dbSession: AsyncSession
    ) -> None:
        """级联失效改了子行 ⇒ 子行也要有审计（改了行就要留痕）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AK")
        sourceId = await _createSource(client, classId, datasourceId, "AK")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        expired = await client.post(f"{_API}/{sourceId}/expire", headers=ADMIN_HEADERS)
        assert expired.status_code == 200

        result = await dbSession.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "procurement_property_binding")
            .where(AuditLog.entity_id == propId)
            .order_by(AuditLog.id)
        )
        rows = list(result.scalars().all())
        actions = [row.action for row in rows]
        assert actions == ["CREATE", "UPDATE", "UPDATE"], (
            f"级联失效必须有子行审计，实际={actions}"
        )
        cascadeRow = rows[-1]
        assert cascadeRow.before_json is not None
        assert cascadeRow.after_json is not None
        assert cascadeRow.before_json["validTo"] is None
        assert cascadeRow.after_json["validTo"] is not None


class TestBoundaryValidation:
    """写边界的输入校验（安全审查 HIGH-2 / MEDIUM-1 / MEDIUM-4）。"""

    async def test_transform_expression_injection_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """列级变换表达式会被拼进读路径投影 ⇒ 必须与过滤表达式同级的闸门。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AL")
        sourceId = await _createSource(client, classId, datasourceId, "AL")
        resp = await client.post(
            f"{_API}/{sourceId}/properties",
            json={
                "propertyId": propertyId,
                "sourceColumn": "ORDER_QTY",
                "transformExpression": "1); DROP TABLE ontology_class --",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    async def test_transform_expression_with_own_column_accepted(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AM")
        sourceId = await _createSource(client, classId, datasourceId, "AM")
        resp = await client.post(
            f"{_API}/{sourceId}/properties",
            json={
                "propertyId": propertyId,
                "sourceColumn": "ORDER_QTY",
                "transformExpression": "TRUNC(ORDER_QTY)",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["transformExpression"] == "TRUNC(ORDER_QTY)"

    async def test_allowlist_item_not_identifier_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """白名单项会被读路径当列名用 ⇒ 非标识符必须拒绝（不能被 PG 静默截断）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AN")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "AN",
                filterExpression="STATUS = 'X'",
                filterAllowlist=["STATUS); DROP TABLE X --"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    async def test_allowlist_item_too_long_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """超长白名单项会被 PostgreSQL **静默截断**（数组元素超长不报错）⇒ 边界上先拒。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AO")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "AO",
                filterExpression="STATUS = 'X'",
                filterAllowlist=["B" * 150],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    async def test_long_user_id_rejected_not_500(
        self, client, dbSession: AsyncSession
    ) -> None:
        """超长 X-User-Id ⇒ 422（不是 500）。

        库列是 `String(64)` ⇒ 超长会抛 `StringDataRightTruncationError`
        （**不是** IntegrityError）⇒ 未处理就是 500。

        ⚠️ 断言必须钉住**边界先拒**这条路径：若删掉服务层的 actor 长度检查，
        错误会退化成「库层兜底翻译」（文案变成「字段长度超限」）⇒ 只断言 422
        会让这层检查变成假绿。
        """
        classId, _, _, datasourceId = await _seed(dbSession, "AP")
        resp = await client.post(
            _API,
            json=_sourcePayload(classId, datasourceId, "AP"),
            headers={"X-User-Id": "u" * 100, "X-User-Roles": "admin"},
        )
        assert resp.status_code == 422
        assert "操作用户标识" in resp.text

    async def test_key_ordinal_over_smallint_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """键序号的库列是 SMALLINT ⇒ 越界值必须在边界上被拒（否则绑定参数抛 DataError → 500）。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AQ")
        sourceId = await _createSource(client, classId, datasourceId, "AQ")
        resp = await client.post(
            f"{_API}/{sourceId}/properties",
            json={"propertyId": propertyId, "sourceColumn": "ORDER_QTY", "keyOrdinal": 40000},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422


class TestEffectivePropertyView:
    async def test_effective_properties_visible_after_confirm(
        self, client, dbSession: AsyncSession
    ) -> None:
        """生效的属性映射是**读路径的唯一入口** ⇒ 确认后可见，候选时不可见。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AR")
        sourceId = await _createSource(client, classId, datasourceId, "AR")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)

        before = await client.get(f"{_API}/effective/properties", headers=USER_HEADERS)
        assert before.status_code == 200
        assert before.json() == []

        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)

        after = await client.get(f"{_API}/effective/properties", headers=USER_HEADERS)
        assert after.status_code == 200
        rows = after.json()
        assert len(rows) == 1
        assert rows[0]["id"] == propId
        assert rows[0]["sourceColumn"] == "ORDER_QTY"
        assert rows[0]["isCandidate"] is False

    async def test_effective_properties_excluded_while_parent_candidate(
        self, client, dbSession: AsyncSession
    ) -> None:
        """父仍是候选 ⇒ 子即便已确认也**不得**进入生效视图。

        父未确认 = 整组映射还不是结论（D-3）。这是「父必须已确认」谓词的
        **唯一可观测场景**（父失效由级联失效保证 ⇒ 那条谓词冗余）。
        """
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AU3")
        sourceId = await _createSource(client, classId, datasourceId, "AU3")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )

        after = await client.get(f"{_API}/effective/properties", headers=USER_HEADERS)
        assert after.status_code == 200
        assert after.json() == []

        await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        confirmed = await client.get(f"{_API}/effective/properties", headers=USER_HEADERS)
        assert len(confirmed.json()) == 1

    async def test_effective_properties_empty_when_parent_expired(
        self, client, dbSession: AsyncSession
    ) -> None:
        """父失效 ⇒ 子不再出现在生效视图（不留孤儿映射）。

        主要机制是**级联失效**（父失效时子一并作废）；查询侧另有一条父状态
        冗余谓词作防御。两者都覆盖 ⇒ 本例对「父未失效」那条冗余谓词的变异
        不敏感（预期 MISSED）。
        """
        classId, propertyId, _, datasourceId = await _seed(dbSession, "AS")
        sourceId = await _createSource(client, classId, datasourceId, "AS")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        await client.post(f"{_API}/{sourceId}/expire", headers=ADMIN_HEADERS)

        after = await client.get(f"{_API}/effective/properties", headers=USER_HEADERS)
        assert after.json() == []


class TestListFilters:
    async def test_list_filters_by_class_id(self, client, dbSession: AsyncSession) -> None:
        """管理台列表的过滤条件必须真的生效（否则「按类查」会拿到全部）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AT")
        otherClassId, _, _, _ = await _seed(dbSession, "AT2")
        await _createSource(client, classId, datasourceId, "AT")
        await _createSource(client, otherClassId, datasourceId, "AT2")

        resp = await client.get(f"{_API}?classId={classId}", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["classId"] == classId

    async def test_list_filters_by_datasource_id(self, client, dbSession: AsyncSession) -> None:
        classId, _, _, datasourceId = await _seed(dbSession, "AU")
        await _createSource(client, classId, datasourceId, "AU")

        matched = await client.get(
            f"{_API}?datasourceId={datasourceId}", headers=ADMIN_HEADERS
        )
        assert len(matched.json()) == 1
        unmatched = await client.get(
            f"{_API}?datasourceId={datasourceId + 9999}", headers=ADMIN_HEADERS
        )
        assert unmatched.json() == []

    async def test_list_effective_only_excludes_candidates(
        self, client, dbSession: AsyncSession
    ) -> None:
        classId, _, _, datasourceId = await _seed(dbSession, "AV")
        await _createSource(client, classId, datasourceId, "AV")

        resp = await client.get(f"{_API}?effectiveOnly=true", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []


class TestSecurityHardening:
    """安全审查第二轮（HIGH-1 / HIGH-2 / MEDIUM-1/2/3）的落地断言。"""

    async def test_filter_expression_with_or_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`STATUS = 'x' OR 1=1` 恒真 ⇒ 行级过滤被抹平成全表读 ⇒ 必须拒。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AW")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "AW",
                filterExpression="STATUS = 'x' OR 1=1",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "OR" in resp.text

    async def test_filter_expression_with_unknown_function_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """函数是**代码** ⇒ 走受控清单；白名单自报也不放行。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AX")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "AX",
                filterExpression="PG_SLEEP(10) > 0",
                filterAllowlist=["PG_SLEEP"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "函数" in resp.text

    async def test_filter_expression_with_subquery_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """表达式里的结构词不许出现 ⇒ 堵死「子查询跨表读」。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AY")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "AY",
                filterExpression="EXISTS (SELECT 1 FROM OTHER_TBL)",
                filterAllowlist=["OTHER_TBL"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "结构词" in resp.text

    async def test_allowlist_with_reserved_word_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """白名单项是保留字 ⇒ 拒（白名单项会被拼进 `SELECT <列清单>`）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AY2")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "AY2",
                filterExpression="STATUS = 'x'",
                filterAllowlist=["STATUS", "SELECT"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "保留字" in resp.text

    async def test_dollar_column_cannot_bypass_allowlist(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`status$1` 抽不出来就等于白名单外列（MEDIUM-1）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "AZ")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "AZ",
                filterExpression="status$1 > 0",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    async def test_blank_filter_expression_stored_as_null(
        self, client, dbSession: AsyncSession
    ) -> None:
        """纯空白表达式 ⇒ 落库为 NULL（不能与「没有过滤」变成两个状态）。"""
        classId, _, _, datasourceId = await _seed(dbSession, "BA")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId, datasourceId, "BA", filterExpression="   "
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201
        assert resp.json()["filterExpression"] is None

    async def test_effective_view_property_count_excludes_expired(
        self, client, dbSession: AsyncSession
    ) -> None:
        """生效视图的属性条数不得把**已失效**的行算进去（D-3 的侧面泄漏）。

        可观测场景：`FILTER` 角色的源绑定不需要属性绑定就能确认 ⇒
        可以先建一条属性绑定再失效它、然后确认父 ⇒ 父进入生效视图，
        而它名下的属性行全是已失效的 ⇒ 条数必须是 0。
        """
        classId, propertyId, _, datasourceId = await _seed(dbSession, "BB")
        sourceId = await _createSource(
            client, classId, datasourceId, "BB", bindingRole="FILTER"
        )
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)
        expired = await client.post(
            f"{_API}/{sourceId}/properties/{propId}/expire", headers=ADMIN_HEADERS
        )
        assert expired.status_code == 200
        confirmed = await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)
        assert confirmed.status_code == 200

        effective = await client.get(f"{_API}/effective", headers=USER_HEADERS)
        assert effective.status_code == 200
        rows = effective.json()
        assert len(rows) == 1
        assert rows[0]["propertyCount"] == 0

        admin = await client.get(f"{_API}/{sourceId}", headers=ADMIN_HEADERS)
        assert admin.json()["binding"]["propertyCount"] == 1

    async def test_unknown_and_foreign_property_share_same_404(
        self, client, dbSession: AsyncSession
    ) -> None:
        """「不存在」与「不属于该父」必须**同一句话** ⇒ 不给存在性探测留口子。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "BC")
        sourceId = await _createSource(client, classId, datasourceId, "BC")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)

        unknown = await client.post(
            f"{_API}/{sourceId}/properties/999999/confirm", headers=ADMIN_HEADERS
        )
        assert unknown.status_code == 404
        assert "不存在或不属于源绑定" in unknown.text

        otherClassId, _, _, _ = await _seed(dbSession, "BD")
        otherSourceId = await _createSource(
            client, otherClassId, datasourceId, "BD", sourceTable="DWD_OTHER_TABLE"
        )
        foreign = await client.post(
            f"{_API}/{otherSourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        assert foreign.status_code == 404
        assert "不存在或不属于源绑定" in foreign.text


class TestFilterExpressionHardening:
    """代码评审第三轮 HIGH-1 / HIGH-2 的落地断言。

    HIGH-1：结构词禁表漏 `TABLE` ⇒ `EXISTS (TABLE other_tbl)` 跨表读绕过；
    且 `EXISTS` 落在 SQL 关键字表里 ⇒ 被提前滤掉 ⇒ 那条禁令是**死代码**。
    HIGH-2：行级过滤仍可被构造成**恒真** ⇒ 禁 `OR` 只堵住了一种写法。
    """

    async def test_filter_expression_with_table_keyword_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`EXISTS (TABLE other_tbl)` 不出现 SELECT/FROM 也能跨表读 ⇒ 必须拒。"""
        classId, _, _, datasourceId = await _seed(dbSession, "BF")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "BF",
                filterExpression="EXISTS (TABLE OTHER_TBL)",
                filterAllowlist=["OTHER_TBL"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "结构词" in resp.text

    async def test_filter_expression_with_table_keyword_alone_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`TABLE` **单独**出现也必须被拒（它是结构词，不依赖 EXISTS 才成立）。

        为什么单独测：`EXISTS (TABLE t)` 里 `EXISTS` 本身也是结构词 ⇒
        禁掉 `TABLE` 之后依然会因 `EXISTS` 被拒 ⇒ 那条断言**观察不到**
        `TABLE` 这条禁令 ⇒ 变异的「守得住」是假的。
        """
        classId, _, _, datasourceId = await _seed(dbSession, "BL1")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "BL1",
                filterExpression="STATUS = 'X' AND TABLE OTHER_TBL",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "结构词" in resp.text

    async def test_filter_expression_with_exists_keyword_alone_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`EXISTS` **单独**出现也必须被拒（它必须能被结构词禁表看见）。

        为什么单独测：`EXISTS (SELECT 1 FROM t)` 里 `SELECT` / `FROM` 也是
        结构词 ⇒ 把 `EXISTS` 挪回 SQL 关键字表后依然被拒 ⇒ 观察不到
        「`EXISTS` 的禁令是不是死代码」。
        """
        classId, _, _, datasourceId = await _seed(dbSession, "BL2")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "BL2",
                filterExpression="EXISTS (1)",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "结构词" in resp.text

    async def test_filter_expression_with_case_constant_true_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`CASE ... ELSE 1 END = 1` 恒真（NULL 也走 ELSE）⇒ 行级过滤被抹平。"""
        classId, _, _, datasourceId = await _seed(dbSession, "BG")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "BG",
                filterExpression="CASE WHEN STATUS = 'x' THEN 1 ELSE 1 END = 1",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "结构词" in resp.text

    async def test_filter_expression_self_comparison_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`STATUS = STATUS` 恒真 ⇒ 引用的是合法列 ⇒ 白名单闸门看不出问题。"""
        classId, _, _, datasourceId = await _seed(dbSession, "BH")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "BH",
                filterExpression="STATUS = STATUS",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "列 与 常量" in resp.text

    async def test_filter_expression_function_only_comparison_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`UPPER(STATUS) = 'X'` 不是裸列比较 ⇒ 恒真族仍可达 ⇒ 必须拒。

        代价（刻意取舍）：需要函数时另配一处裸列比较（见下一个正例）。
        """
        classId, _, _, datasourceId = await _seed(dbSession, "BI")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "BI",
                filterExpression="UPPER(STATUS) = 'X'",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "列 与 常量" in resp.text

    async def test_filter_expression_non_negative_function_rejected(
        self, client, dbSession: AsyncSession
    ) -> None:
        """`ABS(STATUS) >= 0` 恒真（函数值恒非负）⇒ 同一族。"""
        classId, _, _, datasourceId = await _seed(dbSession, "BJ")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "BJ",
                filterExpression="ABS(STATUS) >= 0",
                filterAllowlist=["STATUS"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422
        assert "列 与 常量" in resp.text

    async def test_filter_expression_with_column_and_function_comparison_accepted(
        self, client, dbSession: AsyncSession
    ) -> None:
        """正例：函数比较 **加** 一处裸列比较 ⇒ 合取式依赖行 ⇒ 放行。"""
        classId, _, _, datasourceId = await _seed(dbSession, "BK")
        resp = await client.post(
            _API,
            json=_sourcePayload(
                classId,
                datasourceId,
                "BK",
                filterExpression="STATUS = '有效' AND UPPER(NAME) = 'X'",
                filterAllowlist=["STATUS", "NAME"],
            ),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201, resp.text


class TestValidityWindowNesting:
    """父子有效期窗口必须**嵌套**（子不能比父活得久）。"""

    async def test_cascade_expire_nests_child_validity_within_parent(
        self, client, dbSession: AsyncSession
    ) -> None:
        """级联失效后：子 `valid_to` 必须 ≤ 父 `valid_to`。"""
        classId, propertyId, _, datasourceId = await _seed(dbSession, "BL")
        sourceId = await _createSource(client, classId, datasourceId, "BL")
        propId = await _createProperty(client, sourceId, propertyId, "ORDER_QTY", 1)
        await client.post(
            f"{_API}/{sourceId}/properties/{propId}/confirm", headers=ADMIN_HEADERS
        )
        await client.post(f"{_API}/{sourceId}/confirm", headers=ADMIN_HEADERS)

        expired = await client.post(f"{_API}/{sourceId}/expire", headers=ADMIN_HEADERS)
        assert expired.status_code == 200

        detail = await client.get(f"{_API}/{sourceId}", headers=ADMIN_HEADERS)
        assert detail.status_code == 200
        body = detail.json()
        parentValidTo = body["binding"]["validTo"]
        childValidTo = body["properties"][0]["validTo"]
        assert parentValidTo is not None and childValidTo is not None
        assert datetime.fromisoformat(childValidTo) <= datetime.fromisoformat(parentValidTo), (
            f"子行失效时刻 {childValidTo} 晚于父行 {parentValidTo} ⇒ 有效期窗口未嵌套"
        )
