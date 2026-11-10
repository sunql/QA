"""词条字典写入口：admin 门禁 + 映射引用完整性（真实 PG + HTTP 链路）。

背景：
- `POST` / `DELETE /api/v1/term-dictionary` 原先只有 `getCurrentUser`，任何登录用户都能写；
  写入内容会原样进入 NL2SQL 计划阶段 system prompt（治理数据）。
- `createTerm` 不校验 `mappedClassName` / `mappedPropertyName` 是否存活 —— 「供货量」
  词条曾映射到已 tombstone 的 `DWD_GOODS_RECEIPT_LINE`，同一问题因问法不同落到不同表。

本文件钉住两件事：写入口必须是 admin；映射必须指向**存活**的类/属性。
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyProperty

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
USER_HEADERS = {"X-User-Id": "normal-user", "X-User-Roles": "user"}


async def _seedClass(
    session: AsyncSession,
    className: str,
    *,
    expired: bool = False,
    propertyNames: tuple[str, ...] = (),
) -> int:
    cls = OntologyClass(
        class_name=className,
        source_table=f"T_{className}",
        valid_to=datetime(2020, 1, 1, tzinfo=UTC) if expired else None,
        properties=[
            OntologyProperty(property_name=name, data_type="STRING")
            for name in propertyNames
        ],
    )
    session.add(cls)
    await session.commit()
    await session.refresh(cls)
    return cls.id


def _payload(**overrides: object) -> dict:
    base = {"term": "占比", "definition": "某值占总量的比例"}
    return {**base, **overrides}


async def test_non_admin_cannot_create_term(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/term-dictionary", json=_payload(), headers=USER_HEADERS
    )

    assert resp.status_code == 403


async def test_non_admin_cannot_delete_term(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/term-dictionary", json=_payload(), headers=ADMIN_HEADERS
    )
    assert created.status_code == 201

    resp = await client.delete(
        f"/api/v1/term-dictionary/{created.json()['id']}", headers=USER_HEADERS
    )

    assert resp.status_code == 403


async def test_non_admin_can_still_read_terms(client: AsyncClient) -> None:
    """读路径不受写门禁影响：普通用户查看词典应保持 200。

    stub 身份默认角色含 admin，不带角色的请求测不出这个契约 —— 必须显式 `user`。
    """
    resp = await client.get("/api/v1/term-dictionary", headers=USER_HEADERS)

    assert resp.status_code == 200


async def test_admin_can_create_and_delete_without_mapping(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/term-dictionary", json=_payload(), headers=ADMIN_HEADERS
    )
    assert created.status_code == 201

    resp = await client.delete(
        f"/api/v1/term-dictionary/{created.json()['id']}", headers=ADMIN_HEADERS
    )

    assert resp.status_code == 204


async def test_mapping_to_missing_class_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(mappedClassName="NOT_EXIST_CLASS"),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 422
    assert "不存在" in resp.json()["error"]


async def test_mapping_to_expired_class_is_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedClass(dbSession, "DEAD_CLASS", expired=True)

    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(mappedClassName="DEAD_CLASS"),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 422
    assert "已下线" in resp.json()["error"]


async def test_mapping_to_live_class_is_accepted(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedClass(dbSession, "LIVE_CLASS", propertyNames=("收货数量",))

    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(
            term="收货量",
            mappedClassName="LIVE_CLASS",
            mappedPropertyName="收货数量",
        ),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 201


async def test_mapping_by_source_column_is_accepted(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """既有词条普遍写物理列名（「供货量」→ DWD_GOODS_RECEIPT_DTL / RCV_QTY_PUU）。

    `report_semantic_context` 按物理列匹配列名，LLM 也写物理列 ⇒ 写列名必须放行，
    否则历史词条与真实用法都被新校验打死。
    """
    cls = OntologyClass(class_name="DWD_RECEIPT_DTL", source_table="T_DTL")
    cls.properties = [
        OntologyProperty(
            property_name="收货数量", data_type="DECIMAL", source_column="RCV_QTY_PUU"
        )
    ]
    dbSession.add(cls)
    await dbSession.commit()

    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(
            term="供货量",
            mappedClassName="DWD_RECEIPT_DTL",
            mappedPropertyName="RCV_QTY_PUU",
        ),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 201


async def test_property_not_on_class_is_rejected(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedClass(dbSession, "LIVE_CLASS2", propertyNames=("其他属性",))

    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(
            term="收货量",
            mappedClassName="LIVE_CLASS2",
            mappedPropertyName="不存在的属性",
        ),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 422
    assert "不属于" in resp.json()["error"]


async def test_oversized_definition_is_rejected(client: AsyncClient) -> None:
    """词条全文会注入每个用户每次计划的 prompt ⇒ 写入口必须限长（存储型 DoS）。"""
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(definition="巨" * 2001),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 422


async def test_oversized_formula_hint_is_rejected(client: AsyncClient) -> None:
    """formulaHint 与 definition 同样进 prompt，必须独立限长（不能只测一个字段）。"""
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(formulaHint="公" * 2001),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 422


async def test_property_without_class_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/term-dictionary",
        json=_payload(mappedPropertyName="收货数量"),
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 422
    assert "同时指定" in resp.json()["error"]
