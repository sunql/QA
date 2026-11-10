"""chat 领域命令的治理写入口守卫（/metric、/define、/map）。

背景：本体指标路由已收紧到 `requireAdmin`，但 chat 的斜杠命令绕过了路由层 ——
`ChatService._handleDomainCommand` 里 `/metric` 直接调用 `OntologyService.createMetric`、
`/define` 调 `createClass`、`/map` 调 `updateProperty`。任何登录用户都能经
`POST /api/v1/chat` 写治理数据（安全评审 C1：路由门禁被完全绕过）。

本文件钉住：

1. 非 admin 走 `/metric`、`/define`、`/map` ⇒ **不产生任何写入**，只返回权限提示；
2. 非 admin 走 `/metric <名>`（无公式，只回格式引导）不受影响；
3. admin 走同一链路功能不变。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    DataSource,
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
)

ADMIN_HEADERS = {
    "X-User-Id": "test-admin",
    "X-User-Roles": "admin",
    "X-User-Tenant": "default",
}
USER_HEADERS = {
    "X-User-Id": "normal-user",
    "X-User-Roles": "user",
    "X-User-Tenant": "default",
}
_DS_ID = 9711


async def _seedDatasource(dbSession: AsyncSession) -> None:
    dbSession.add(
        DataSource(
            id=_DS_ID,
            name=f"ds-chat-write-gate-{_DS_ID}",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="x",
            username="u",
            password_encrypted="x",
        )
    )
    await dbSession.commit()


def _chatPayload(question: str) -> dict:
    return {"sessionId": "sess-chat-write-gate", "question": question, "datasourceId": _DS_ID}


async def _metricNamed(session: AsyncSession, name: str) -> OntologyMetric | None:
    rows = await session.execute(
        select(OntologyMetric).where(OntologyMetric.metric_name == name)
    )
    return rows.scalar_one_or_none()


async def _classNamed(session: AsyncSession, name: str) -> OntologyClass | None:
    rows = await session.execute(
        select(OntologyClass).where(OntologyClass.class_name == name)
    )
    return rows.scalar_one_or_none()


async def test_non_admin_slash_metric_does_not_create_metric(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedDatasource(dbSession)

    resp = await client.post(
        "/api/v1/chat",
        json=_chatPayload("/metric 越权指标 = SUM(x)"),
        headers=USER_HEADERS,
    )

    assert resp.status_code == 200
    assert "管理员" in resp.json()["answer"]
    assert await _metricNamed(dbSession, "越权指标") is None, (
        "非 admin 经 chat /metric 仍写入了 ontology_metric —— 路由门禁被绕过"
    )


async def test_non_admin_slash_define_does_not_create_class(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedDatasource(dbSession)

    resp = await client.post(
        "/api/v1/chat",
        json=_chatPayload("/define 越权类GATE"),
        headers=USER_HEADERS,
    )

    assert resp.status_code == 200
    assert "管理员" in resp.json()["answer"]
    assert await _classNamed(dbSession, "越权类GATE") is None


async def test_non_admin_slash_map_does_not_change_property(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedDatasource(dbSession)
    source = OntologyClass(
        class_name="GATE_SOURCE", source_table="T_GATE_SOURCE",
        properties=[OntologyProperty(property_name="归属标记", data_type="STRING")],
    )
    target = OntologyClass(class_name="GATE_TARGET", source_table="T_GATE_TARGET")
    dbSession.add_all([source, target])
    await dbSession.commit()
    propertyId = source.properties[0].id

    resp = await client.post(
        "/api/v1/chat",
        json=_chatPayload("/map 归属标记 -> GATE_TARGET"),
        headers=USER_HEADERS,
    )

    assert resp.status_code == 200
    assert "管理员" in resp.json()["answer"]
    await dbSession.refresh(source.properties[0])
    prop = await dbSession.get(OntologyProperty, propertyId)
    assert prop is not None
    assert prop.is_foreign_key is False, "非 admin 经 chat /map 修改了属性外键"
    assert prop.ref_class_id is None


async def test_non_admin_metric_name_only_still_lists_metrics(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """无公式 → 走列举（读路径），不应被 admin 门禁误伤。"""
    await _seedDatasource(dbSession)
    dbSession.add(OntologyMetric(metric_name="可读指标", formula="COUNT(*)"))
    await dbSession.commit()

    resp = await client.post(
        "/api/v1/chat",
        json=_chatPayload("/metric 只有名字"),
        headers=USER_HEADERS,
    )

    assert resp.status_code == 200
    assert "可读指标" in resp.json()["answer"], "非 admin 的指标列举被写门禁误伤"
    assert await _metricNamed(dbSession, "只有名字") is None


async def test_non_admin_natural_language_metric_does_not_create_metric(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """自然语言「定义指标 …」与 `/metric` 同一分发点，同样必须被拦。"""
    await _seedDatasource(dbSession)

    resp = await client.post(
        "/api/v1/chat",
        json=_chatPayload("定义指标 自然语言越权指标 = SUM(x)"),
        headers=USER_HEADERS,
    )

    assert resp.status_code == 200
    assert "管理员" in resp.json()["answer"]
    assert await _metricNamed(dbSession, "自然语言越权指标") is None


async def test_non_admin_stream_slash_metric_does_not_create_metric(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """流式入口走的是同一个 `_handleDomainCommand`，门禁不能只覆盖非流式。"""
    await _seedDatasource(dbSession)

    resp = await client.post(
        "/api/v1/chat/stream",
        json=_chatPayload("/metric 流式越权指标 = SUM(x)"),
        headers=USER_HEADERS,
    )

    assert resp.status_code == 200
    assert "管理员" in resp.text
    assert await _metricNamed(dbSession, "流式越权指标") is None, (
        "非 admin 经 /chat/stream 仍写入了 ontology_metric"
    )


async def test_admin_slash_metric_still_creates_metric(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedDatasource(dbSession)

    resp = await client.post(
        "/api/v1/chat",
        json=_chatPayload("/metric 管理员指标 = SUM(y)"),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 200
    assert await _metricNamed(dbSession, "管理员指标") is not None
