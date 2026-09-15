"""站内消息 service + API 集成测试（feat-dq-evaluation-report，Phase 7c）。

覆盖：
- create_message：写入 + 必要字段校验
- list_for_user：按 user_id 隔离；unread_only 过滤
- mark_read：未读→已读；非 owner / 已读消息不被改写
- API：GET /messages、/messages/unread-count、POST /messages/{id}/read
"""

from __future__ import annotations

import pytest

from app.dependencies import CurrentUser
from app.domain.models import InAppMessage
from app.services.in_app_message_service import InAppMessageService

USER_A = CurrentUser(
    userId="msg-a",
    tenantId="default",
    roles=("user",),
    departments=("procurement",),
    dbUserId=None,
)
USER_B = CurrentUser(
    userId="msg-b",
    tenantId="default",
    roles=("user",),
    departments=("finance",),
    dbUserId=None,
)


def _headers(actor: CurrentUser) -> dict[str, str]:
    h = {"X-User-Id": actor.userId, "X-User-Roles": ",".join(actor.roles)}
    if actor.departments:
        h["X-User-Departments"] = ",".join(actor.departments)
    return h


@pytest.mark.asyncio
class TestInAppMessageService:
    async def test_create_and_list(self, dbSession) -> None:
        svc = InAppMessageService()
        m = await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="t1",
            body="b1",
            link_url="/x",
        )
        await dbSession.commit()
        assert m.id > 0
        rows = await svc.list_for_user(dbSession, user_id=USER_A.userId)
        assert len(rows) == 1
        assert rows[0].title == "t1"

    async def test_empty_recipient_raises(self, dbSession) -> None:
        svc = InAppMessageService()
        with pytest.raises(ValueError):
            await svc.create_message(
                dbSession,
                recipient_user_id="",
                title="t",
                body="b",
            )

    async def test_user_isolation(self, dbSession) -> None:
        svc = InAppMessageService()
        await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="a",
            body="b",
        )
        await svc.create_message(
            dbSession,
            recipient_user_id=USER_B.userId,
            title="c",
            body="d",
        )
        await dbSession.commit()
        a_rows = await svc.list_for_user(dbSession, user_id=USER_A.userId)
        b_rows = await svc.list_for_user(dbSession, user_id=USER_B.userId)
        assert {r.title for r in a_rows} == {"a"}
        assert {r.title for r in b_rows} == {"c"}

    async def test_unread_only_filter(self, dbSession) -> None:
        svc = InAppMessageService()
        m1 = await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="u1",
            body="b",
        )
        m2 = await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="u2",
            body="b",
        )
        await dbSession.commit()
        # 把 m1 标已读
        await svc.mark_read(
            dbSession, message_id=m1.id, user_id=USER_A.userId,
        )
        rows = await svc.list_for_user(
            dbSession, user_id=USER_A.userId, unread_only=True,
        )
        assert len(rows) == 1
        assert rows[0].id == m2.id

    async def test_unread_count(self, dbSession) -> None:
        svc = InAppMessageService()
        await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="a",
            body="b",
        )
        await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="c",
            body="d",
        )
        await dbSession.commit()
        n = await svc.unread_count(dbSession, user_id=USER_A.userId)
        assert n == 2

    async def test_mark_read_wrong_user_noop(self, dbSession) -> None:
        svc = InAppMessageService()
        m = await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="x",
            body="y",
        )
        await dbSession.commit()
        # USER_B 试图标 m 已读：noop，read_at 仍空
        ok = await svc.mark_read(
            dbSession, message_id=m.id, user_id=USER_B.userId,
        )
        assert ok is False
        await dbSession.refresh(m)
        assert m.read_at is None

    async def test_mark_read_already_read(self, dbSession) -> None:
        svc = InAppMessageService()
        m = await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="x",
            body="y",
        )
        await dbSession.commit()
        ok1 = await svc.mark_read(
            dbSession, message_id=m.id, user_id=USER_A.userId,
        )
        ok2 = await svc.mark_read(
            dbSession, message_id=m.id, user_id=USER_A.userId,
        )
        assert ok1 is True
        assert ok2 is False  # 已读，再调返 False


@pytest.mark.asyncio
class TestMessageApi:
    async def test_list_endpoint(self, client, dbSession) -> None:
        svc = InAppMessageService()
        await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="ep1",
            body="body1",
        )
        await dbSession.commit()
        resp = await client.get(
            "/api/v1/messages", headers=_headers(USER_A),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["title"] == "ep1"

    async def test_unread_count_endpoint(self, client, dbSession) -> None:
        svc = InAppMessageService()
        await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="x",
            body="y",
        )
        await dbSession.commit()
        resp = await client.get(
            "/api/v1/messages/unread-count", headers=_headers(USER_A),
        )
        assert resp.status_code == 200
        assert resp.json()["unreadCount"] == 1

    async def test_mark_read_endpoint(self, client, dbSession) -> None:
        svc = InAppMessageService()
        m = await svc.create_message(
            dbSession,
            recipient_user_id=USER_A.userId,
            title="x",
            body="y",
        )
        await dbSession.commit()
        resp = await client.post(
            f"/api/v1/messages/{m.id}/read",
            headers=_headers(USER_A),
        )
        assert resp.status_code == 204
        # 再次调，未读列表应空
        resp2 = await client.get(
            "/api/v1/messages?unreadOnly=true",
            headers=_headers(USER_A),
        )
        assert resp2.status_code == 200
        assert resp2.json() == []