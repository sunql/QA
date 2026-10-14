"""会话归属守卫集成测试（真实 PG + 完整 API 链路）。

背景：chat 的归属守卫（v3.1 B6）此前**只有** `GET /chat/sessions/{id}/hypotheses`
一个调用者，于是下列端点全都没有校验 —— 任何登录用户只要拿到 sessionId 就能读别人的
会话、删别人的会话（连带成本台账）、继承别人会话的服务端追问锚点：

- GET    /api/v1/sessions/{sid}/messages
- DELETE /api/v1/sessions/{sid}
- POST   /api/v1/sessions/{sid}/export.pdf
- POST   /api/v1/chat
- POST   /api/v1/chat/stream

每个端点都做**双向**断言：别人的会话被拦（403），自己的 / 无归属的放行。
只测「被拦」会漏掉「守卫把自己人也拦了」这种更常见的回归。

读/删/导出三类端点服务 **chat / doc_qa / wiki_qa 三个渠道**，所以每条断言都要在
非 chat 渠道上复验：守卫的归属查询曾写死 `channel='chat'`，那会让另外两个渠道恒返
空集而静默 fail-open（doc_qa 行由 rag_qa_service 打标、wiki_qa 行由 wiki 侧打标）。

身份用 stub 头表达：`X-User-Id: alice` + `X-User-Roles: user`（不带 admin）。
不带 header 的请求走 DEFAULT_STUB_ROLES（含 admin）→ 放行，正好用来测 admin 旁路。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.models import SessionMessage
from app.services.messages_zh import MSG_SESSION_NOT_OWNED
from app.tests.integration.test_chat_api import _chat_payload, _seed
from app.tests.integration.test_chat_stream_api import _installStreamFakes

ALICE = {"X-User-Id": "alice", "X-User-Roles": "user"}
BOB = {"X-User-Id": "bob", "X-User-Roles": "user"}
# 不带 X-User-* → stub 默认角色含 admin（旁路路径）
ADMIN = {}


async def _seedMessage(
    db_session,
    *,
    sessionId: str,
    owner: str | None,
    role: str = "assistant",
    content: str = "回答",
    channel: str = "chat",
) -> SessionMessage:
    """插入一行带归属标记（或 NULL 归属）的会话消息。"""
    now = datetime.now(UTC)
    msg = SessionMessage(
        session_id=sessionId,
        role=role,
        content=content,
        channel=channel,
        user_id=owner,
        created_time=now,
        updated_time=now,
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return msg


async def _countMessages(db_session, sessionId: str) -> int:
    from sqlalchemy import func, select

    rows = await db_session.execute(
        select(func.count()).select_from(SessionMessage).where(
            SessionMessage.session_id == sessionId
        )
    )
    return int(rows.scalar() or 0)


class TestMessagesEndpointOwnership:
    async def test_foreign_session_blocked_without_side_channel(
        self, pg_client, db_session
    ) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice")

        resp = await pg_client.get("/api/v1/sessions/s-alice/messages", headers=BOB)

        assert resp.status_code == 403, resp.text
        # detail 不回显归属者（防侧信道枚举）
        assert resp.json()["detail"] == MSG_SESSION_NOT_OWNED
        assert "alice" not in resp.text

    async def test_own_session_allowed(self, pg_client, db_session) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice")

        resp = await pg_client.get("/api/v1/sessions/s-alice/messages", headers=ALICE)

        assert resp.status_code == 200, resp.text
        assert len(resp.json()["messages"]) == 1

    async def test_untagged_legacy_session_allowed(self, pg_client, db_session) -> None:
        # 存量行（user_id IS NULL，2026-09-30 之前写的 920 行都是这样）→ fail-open
        await _seedMessage(db_session, sessionId="s-legacy", owner=None)

        resp = await pg_client.get("/api/v1/sessions/s-legacy/messages", headers=BOB)

        assert resp.status_code == 200, resp.text

    async def test_admin_bypasses_guard(self, pg_client, db_session) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice")

        resp = await pg_client.get("/api/v1/sessions/s-alice/messages", headers=ADMIN)

        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("channel", ["doc_qa", "wiki_qa"])
    async def test_foreign_non_chat_channel_session_blocked(
        self, pg_client, db_session, channel
    ) -> None:
        """回归：守卫的归属事实源曾写死 channel='chat'。

        `/messages` 服务三个渠道，而 doc_qa / wiki_qa 的行分别由 rag_qa_service 与
        wiki 侧打标 —— 只查 chat 时它们恒返空集 ⇒ 守卫静默 fail-open，别人的文档问答 /
        Wiki 问答照样能读。这条用例在修之前必红。
        """
        await _seedMessage(
            db_session, sessionId="s-alice-doc", owner="alice", channel=channel
        )

        resp = await pg_client.get("/api/v1/sessions/s-alice-doc/messages", headers=BOB)

        assert resp.status_code == 403, resp.text

    @pytest.mark.parametrize("channel", ["doc_qa", "wiki_qa"])
    async def test_own_non_chat_channel_session_allowed(
        self, pg_client, db_session, channel
    ) -> None:
        # 双向：拓宽口径不能把本人也拦了
        await _seedMessage(
            db_session, sessionId="s-alice-doc", owner="alice", channel=channel
        )

        resp = await pg_client.get("/api/v1/sessions/s-alice-doc/messages", headers=ALICE)

        assert resp.status_code == 200, resp.text


class TestDeleteEndpointOwnership:
    async def test_foreign_session_blocked_and_nothing_deleted(
        self, pg_client, db_session
    ) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice")

        resp = await pg_client.delete("/api/v1/sessions/s-alice", headers=BOB)

        assert resp.status_code == 403, resp.text
        # 关键：拒绝不能「先删后拒」—— 行必须还在
        assert await _countMessages(db_session, "s-alice") == 1

    async def test_own_session_deleted(self, pg_client, db_session) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice")

        resp = await pg_client.delete("/api/v1/sessions/s-alice", headers=ALICE)

        assert resp.status_code == 204, resp.text
        assert await _countMessages(db_session, "s-alice") == 0

    async def test_untagged_legacy_session_deletable(self, pg_client, db_session) -> None:
        await _seedMessage(db_session, sessionId="s-legacy", owner=None)

        resp = await pg_client.delete("/api/v1/sessions/s-legacy", headers=BOB)

        assert resp.status_code == 204, resp.text

    async def test_foreign_doc_qa_session_blocked_and_nothing_deleted(
        self, pg_client, db_session
    ) -> None:
        # 破坏性操作 + 非 chat 渠道：最坏组合，必须双向都钉住
        await _seedMessage(
            db_session, sessionId="s-alice-doc", owner="alice", channel="doc_qa"
        )

        resp = await pg_client.delete("/api/v1/sessions/s-alice-doc", headers=BOB)

        assert resp.status_code == 403, resp.text
        assert await _countMessages(db_session, "s-alice-doc") == 1


class TestExportEndpointOwnership:
    async def test_foreign_session_blocked(self, pg_client, db_session) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice", role="user")

        resp = await pg_client.post(
            "/api/v1/sessions/s-alice/export.pdf", json={}, headers=BOB
        )

        assert resp.status_code == 403, resp.text

    async def test_own_session_exports_pdf(self, pg_client, db_session) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice", role="user", content="问题")
        await _seedMessage(db_session, sessionId="s-alice", owner="alice", role="assistant", content="回答")

        resp = await pg_client.post(
            "/api/v1/sessions/s-alice/export.pdf", json={}, headers=ALICE
        )

        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/pdf")

    async def test_foreign_doc_qa_session_blocked(self, pg_client, db_session) -> None:
        await _seedMessage(
            db_session, sessionId="s-alice-doc", owner="alice", role="user",
            content="问题", channel="doc_qa",
        )

        resp = await pg_client.post(
            "/api/v1/sessions/s-alice-doc/export.pdf", json={}, headers=BOB
        )

        assert resp.status_code == 403, resp.text

    async def test_untagged_legacy_session_export_allowed(
        self, pg_client, db_session
    ) -> None:
        # 双向：无归属标记（存量 NULL / 全新会话）必须仍然可导出，否则守卫把自己人也拦了
        await _seedMessage(db_session, sessionId="s-legacy", owner=None, role="user", content="问题")

        resp = await pg_client.post(
            "/api/v1/sessions/s-legacy/export.pdf", json={}, headers=BOB
        )

        assert resp.status_code == 200, resp.text


class TestUsageEndpointOwnership:
    """用量端点也是「按 sessionId 暴露单会话内容」的对称端点：拿到 id 就能读别人的
    token / 成本 / 模型明细。归属口径与 messages / delete / export 一致。"""

    async def test_foreign_session_blocked_on_summary(self, pg_client, db_session) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice")

        resp = await pg_client.get("/api/v1/sessions/s-alice/usage", headers=BOB)

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == MSG_SESSION_NOT_OWNED

    async def test_foreign_session_blocked_on_usage_list(
        self, pg_client, db_session
    ) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice")

        resp = await pg_client.get("/api/v1/sessions/s-alice/usage/list", headers=BOB)

        assert resp.status_code == 403, resp.text

    async def test_own_session_allowed(self, pg_client, db_session) -> None:
        await _seedMessage(db_session, sessionId="s-alice", owner="alice")

        resp = await pg_client.get("/api/v1/sessions/s-alice/usage", headers=ALICE)

        assert resp.status_code == 200, resp.text
        assert resp.json()["sessionId"] == "s-alice"

    async def test_untagged_legacy_session_allowed(self, pg_client, db_session) -> None:
        # 双向：无归属标记（存量 NULL / 全新会话）仍可读，否则守卫把所有人的面板都拦了
        await _seedMessage(db_session, sessionId="s-legacy", owner=None)

        resp = await pg_client.get("/api/v1/sessions/s-legacy/usage/list", headers=BOB)

        assert resp.status_code == 200, resp.text


class TestChatWriteEndpointsOwnership:
    """写端点也要守卫：追问锚点（last_plan/last_sql）按 session_id 存在服务端，
    不校验归属就能被继承 —— 「拿到别人的 sessionId」等于「用别人的上下文提问」。"""

    async def test_chat_foreign_session_blocked(self, pg_client, db_session, monkeypatch) -> None:
        config, ds = await _seed(db_session)
        _installStreamFakes(monkeypatch, config)
        await _seedMessage(db_session, sessionId="s-alice", owner="alice", role="user", content="上一轮")

        resp = await pg_client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "s-alice"),
            headers=BOB,
        )

        assert resp.status_code == 403, resp.text

    async def test_chat_own_session_allowed(self, pg_client, db_session, monkeypatch) -> None:
        config, ds = await _seed(db_session)
        _installStreamFakes(monkeypatch, config)
        await _seedMessage(db_session, sessionId="s-alice", owner="alice", role="user", content="上一轮")

        resp = await pg_client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "s-alice"),
            headers=ALICE,
        )

        assert resp.status_code == 200, resp.text

    async def test_chat_new_session_allowed(self, pg_client, db_session, monkeypatch) -> None:
        # 全新会话没有任何消息行 → fail-open（否则前端第一条消息就发不出去）
        config, ds = await _seed(db_session)
        _installStreamFakes(monkeypatch, config)

        resp = await pg_client.post(
            "/api/v1/chat",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "s-brand-new"),
            headers=ALICE,
        )

        assert resp.status_code == 200, resp.text

    async def test_chat_stream_foreign_session_blocked(
        self, pg_client, db_session, monkeypatch
    ) -> None:
        config, ds = await _seed(db_session)
        _installStreamFakes(monkeypatch, config)
        await _seedMessage(db_session, sessionId="s-alice", owner="alice", role="user", content="上一轮")

        resp = await pg_client.post(
            "/api/v1/chat/stream",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "s-alice"),
            headers=BOB,
        )

        # 守卫在流开始之前 → 还能回 HTTP 状态码（进了 eventSource 就只能发 error 事件）
        assert resp.status_code == 403, resp.text

    async def test_chat_stream_own_session_allowed(
        self, pg_client, db_session, monkeypatch
    ) -> None:
        config, ds = await _seed(db_session)
        _installStreamFakes(monkeypatch, config)
        await _seedMessage(db_session, sessionId="s-alice", owner="alice", role="user", content="上一轮")

        resp = await pg_client.post(
            "/api/v1/chat/stream",
            json=_chat_payload("各供应商的收货数量汇总", ds.id, "s-alice"),
            headers=ALICE,
        )

        assert resp.status_code == 200, resp.text
        assert "event: done" in resp.text
