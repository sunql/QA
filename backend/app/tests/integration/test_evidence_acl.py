"""Integration tests for /evidences 归属守卫（R2 必修 2，security H2）。

评审发现：evidences.py 三个 GET 端点只有认证没有归属校验 —— 任意认证用户
可枚举他人 session 的 SQL 原文 + result_hash（低基数结果可离线爆破）。

归属事实源 = session_message.user_id（0047 为 doc_qa ownership 守卫引入；
chat 行此前恒 NULL，本轮起 chat 落库时经服务端 actor 打标）。守卫语义与
wiki.py wiki_qa / documents.py docQa 同模式：

- session 已有归属标记（user_id 非空）且不属于当前用户（非 admin）→ 403
- 无标记行（新会话 / 存量 NULL 行）→ 放行（fail-open，兼容存量）
- admin 放行（与 getAdminOnlyActor 同判据："admin" in user.roles）
- 无 session 维度过滤的列表：携带 session 的证据行收敛为「本人拥有的
  session」∪「无 session 的 claim 挂靠行」（堵批量枚举洞）

ACL 四项：mass-assignment（GET 无 body，N/A）/ 403 侧信道（detail 不回显
session 归属者）/ actor 派生（userId 来自服务端 getCurrentUser，非客户端
自报字段）/ 非 admin 集成测试（X-User-Roles 显式降权，双向断言）。
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import SessionMessage
from app.domain.wiki_models import Evidence
from app.services import evidence_record_service as ers
from app.services.chat_service import ChatService

pytestmark = pytest.mark.integration

# 非 admin 用户：必须显式 X-User-Roles 降权（stub 默认角色含 admin）
ALICE_HEADERS = {"X-User-Id": "alice", "X-User-Roles": "user"}
BOB_HEADERS = {"X-User-Id": "bob", "X-User-Roles": "user"}
ADMIN_HEADERS = {"X-User-Id": "admin-user"}  # 不在 DB → stub 默认角色含 admin

_SESS_ALICE = "sess-alice-owned"
_SESS_BOB = "sess-bob-owned"
_SESS_LEGACY = "sess-legacy-null-owner"


async def _seedSessionsAndEvidences(session: AsyncSession) -> dict[str, int]:
    """播种两个有归属的 chat session + 一个存量 NULL 行 session + 4 条 evidence。"""
    rows = [
        SessionMessage(
            session_id=_SESS_ALICE, role="user", content="q", user_id="alice"
        ),
        SessionMessage(
            session_id=_SESS_BOB, role="user", content="q", user_id="bob"
        ),
        # 存量 chat 行：user_id NULL（打标逻辑上线前的常态）
        SessionMessage(session_id=_SESS_LEGACY, role="user", content="q"),
    ]
    session.add_all(rows)

    eAlice = Evidence(
        source_type="SQL_QUERY",
        session_id=_SESS_ALICE,
        payload={"sql": "SELECT 'alice'", "result_hash": "ha"},
    )
    eBob = Evidence(
        source_type="SQL_QUERY",
        session_id=_SESS_BOB,
        payload={"sql": "SELECT 'bob'", "result_hash": "hb"},
    )
    eLegacy = Evidence(
        source_type="SQL_QUERY",
        session_id=_SESS_LEGACY,
        payload={"sql": "SELECT 'legacy'", "result_hash": "hl"},
    )
    eClaimless = Evidence(
        source_type="DOCUMENT",
        content="claim 挂靠行（无 session 维度）",
    )
    session.add_all([eAlice, eBob, eLegacy, eClaimless])
    await session.commit()
    return {
        "alice": eAlice.id,
        "bob": eBob.id,
        "legacy": eLegacy.id,
        "claimless": eClaimless.id,
    }


# ---------------------------------------------------------------------------
# by-session 端点
# ---------------------------------------------------------------------------


async def test_by_session_owner_can_read(client: AsyncClient, dbSession: AsyncSession):
    """双向正面：本人访问自己 session 不被拦。"""
    await _seedSessionsAndEvidences(dbSession)
    resp = await client.get(
        f"/api/v1/evidences/by-session/{_SESS_ALICE}", headers=ALICE_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert all(item["sessionId"] == _SESS_ALICE for item in body["items"])


async def test_by_session_other_user_blocked(
    client: AsyncClient, dbSession: AsyncSession
):
    """双向负面：他人 session 被拦（此前任意认证用户可枚举 —— H2 洞）。"""
    await _seedSessionsAndEvidences(dbSession)
    resp = await client.get(
        f"/api/v1/evidences/by-session/{_SESS_ALICE}", headers=BOB_HEADERS
    )
    assert resp.status_code == 403


async def test_by_session_admin_bypass(client: AsyncClient, dbSession: AsyncSession):
    """admin 放行（getAdminOnlyActor 同判据）。"""
    await _seedSessionsAndEvidences(dbSession)
    resp = await client.get(
        f"/api/v1/evidences/by-session/{_SESS_ALICE}", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200


async def test_by_session_legacy_null_owner_fail_open(
    client: AsyncClient, dbSession: AsyncSession
):
    """存量会话（行无归属标记）放行 —— 与 wiki.py wiki_qa 守卫同语义。"""
    await _seedSessionsAndEvidences(dbSession)
    resp = await client.get(
        f"/api/v1/evidences/by-session/{_SESS_LEGACY}", headers=BOB_HEADERS
    )
    assert resp.status_code == 200


async def test_by_session_unknown_session_empty_ok(
    client: AsyncClient, dbSession: AsyncSession
):
    """全新 session（无任何消息行）→ 200 空列表（新会话在打标前可读）。"""
    resp = await client.get(
        "/api/v1/evidences/by-session/sess-brand-new", headers=ALICE_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# 无 session 过滤的列表：堵批量枚举洞
# ---------------------------------------------------------------------------


async def test_unscoped_list_hides_other_users_session_evidence(
    client: AsyncClient, dbSession: AsyncSession
):
    """非 admin 不带 session_id 过滤时，看不到他人 session 的证据行。"""
    ids = await _seedSessionsAndEvidences(dbSession)
    resp = await client.get(
        "/api/v1/evidences",
        params={"source_type": "SQL_QUERY"},
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 200
    seenIds = {item["id"] for item in resp.json()["items"]}
    assert ids["bob"] in seenIds, "本人 session 证据必须可见"
    assert ids["alice"] not in seenIds, "他人 session 证据被批量枚举可见 —— H2 洞"


async def test_unscoped_list_admin_sees_all(
    client: AsyncClient, dbSession: AsyncSession
):
    """admin 不受归属收敛限制。"""
    ids = await _seedSessionsAndEvidences(dbSession)
    resp = await client.get(
        "/api/v1/evidences",
        params={"source_type": "SQL_QUERY"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    seenIds = {item["id"] for item in resp.json()["items"]}
    assert {ids["alice"], ids["bob"], ids["legacy"]} <= seenIds


async def test_unscoped_list_claimless_rows_visible_to_all(
    client: AsyncClient, dbSession: AsyncSession
):
    """无 session 的 claim 挂靠行（Document 语义）不受归属收敛限制。"""
    ids = await _seedSessionsAndEvidences(dbSession)
    resp = await client.get(
        "/api/v1/evidences",
        params={"source_type": "DOCUMENT"},
        headers=BOB_HEADERS,
    )
    assert resp.status_code == 200
    seenIds = {item["id"] for item in resp.json()["items"]}
    assert ids["claimless"] in seenIds


# ---------------------------------------------------------------------------
# by-id 详情端点
# ---------------------------------------------------------------------------


async def test_get_by_id_other_users_session_blocked(
    client: AsyncClient, dbSession: AsyncSession
):
    """他人 session 证据详情被拦。"""
    ids = await _seedSessionsAndEvidences(dbSession)
    resp = await client.get(
        f"/api/v1/evidences/{ids['alice']}", headers=BOB_HEADERS
    )
    assert resp.status_code == 403


async def test_get_by_id_owner_ok_and_claimless_ok(
    client: AsyncClient, dbSession: AsyncSession
):
    """本人 session 证据 + 无 session 的行双向正面（正常访问不被误拦）。"""
    ids = await _seedSessionsAndEvidences(dbSession)
    own = await client.get(
        f"/api/v1/evidences/{ids['alice']}", headers=ALICE_HEADERS
    )
    assert own.status_code == 200
    claimless = await client.get(
        f"/api/v1/evidences/{ids['claimless']}", headers=BOB_HEADERS
    )
    assert claimless.status_code == 200


async def test_get_by_id_missing_still_404(
    client: AsyncClient, dbSession: AsyncSession
):
    """不存在 → 404（既有语义不变）。"""
    resp = await client.get("/api/v1/evidences/999999", headers=BOB_HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 归属打标：chat 落库写入服务端 actor（守卫的数据来源）
# ---------------------------------------------------------------------------


async def test_store_session_messages_stamps_chat_user(
    dbSession: AsyncSession,
):
    """chat 行归属打标：contextvar 带入的 actor 落到 session_message.user_id。

    此前 chat 渠道恒 NULL（doc_qa/wiki_qa 由 API 层透传，chat 没有）——
    守卫的数据源就是这里。
    """
    sessionToken = ers.setChatSessionId("sess-stamp")
    userToken = ers.setChatUserId("alice")
    try:
        await ChatService()._storeSessionMessages(
            dbSession, "sess-stamp", "问题", "回答", "SELECT 1"
        )
    finally:
        ers.resetChatUserId(userToken)
        ers.resetChatSessionId(sessionToken)

    stamped = (
        await dbSession.execute(
            SessionMessage.__table__.select().where(
                SessionMessage.session_id == "sess-stamp"
            )
        )
    ).fetchall()
    assert len(stamped) == 2
    assert all(row.user_id == "alice" for row in stamped), (
        "chat 消息行未写入服务端 actor —— 归属守卫将无数据可用"
    )
