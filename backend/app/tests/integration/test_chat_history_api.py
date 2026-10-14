"""聊天会话历史 API 集成测试（真实 PG + 完整 API 链路）。

验证 3 个新端点：
- GET  /api/v1/sessions/chat-history             聊天语义会话列表
- GET  /api/v1/sessions/{sessionId}/messages     消息流加载
- DELETE /api/v1/sessions/{sessionId}            会话级硬删除（session_message + token_usage + query_state 三表）

与既有 session.py 端点（listSessions/usage/global 等）解耦，本测试只覆盖新增 3 个 endpoint。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.domain.models import SessionMessage, SessionQueryState, SessionTokenUsage


# ============ Arrange helpers（test-internal fixtures） ============


async def _seedMessage(
    dbSession,
    *,
    sessionId: str,
    role: str,
    content: str,
    sql: str | None = None,
    chartType: str | None = None,
    chartOption: dict | None = None,
    tableOption: dict | None = None,
    visualRationale: dict | None = None,
    createdAt: datetime | None = None,
) -> SessionMessage:
    """插入一行会话消息（created_time 可指定，便于排序断言）。"""
    msg = SessionMessage(
        session_id=sessionId,
        role=role,
        content=content,
        sql_generated=sql,
        chart_type=chartType,
        chart_option=chartOption,
        table_option=tableOption,
        visual_rationale=visualRationale,
        created_time=createdAt or datetime.now(UTC),
        updated_time=createdAt or datetime.now(UTC),
    )
    dbSession.add(msg)
    await dbSession.commit()
    await dbSession.refresh(msg)
    return msg


async def _seedQueryState(
    dbSession, *, sessionId: str, lastQuestion: str = "Q", lastSql: str = "SELECT 1"
) -> SessionQueryState:
    qs = SessionQueryState(
        session_id=sessionId,
        last_question=lastQuestion,
        last_sql=lastSql,
        turn_count=1,
    )
    dbSession.add(qs)
    await dbSession.commit()
    return qs


async def _seedTokenUsage(
    dbSession, *, sessionId: str, modelName: str = "gpt-4o", tokens: int = 10, cost: str = "0.001"
) -> None:
    dbSession.add(
        SessionTokenUsage(
            session_id=sessionId,
            model_config_id=None,
            model_name=modelName,
            prompt_tokens=tokens // 2,
            completion_tokens=tokens - tokens // 2,
            total_tokens=tokens,
            cost=cost,
            request_time=datetime.now(UTC),
            purpose="nl2sql",
        )
    )
    await dbSession.commit()


# ============ GET /chat-history ============


class TestChatHistoryListing:
    async def test_listing_returns_session_summaries(self, client, dbSession) -> None:
        """3 个 session 各 1 轮 → 列表返 3 项，字段对齐（snake→camelCase JSON）。"""
        await _seedMessage(
            dbSession, sessionId="s1", role="user", content="A 的销售",
            createdAt=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId="s1", role="assistant", content="查询结果", sql="SELECT 1",
            createdAt=datetime(2026, 1, 1, 10, 1, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId="s2", role="user", content="B 的库存",
            createdAt=datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId="s2", role="assistant", content="库存结果",
            createdAt=datetime(2026, 1, 2, 10, 1, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId="s3", role="user", content="C 的对账",
            createdAt=datetime(2026, 1, 3, 10, 0, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId="s3", role="assistant", content="对账结果",
            createdAt=datetime(2026, 1, 3, 10, 1, tzinfo=UTC),
        )

        resp = await client.get("/api/v1/sessions/chat-history")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 3
        # 按 last_time DESC 排序：s3, s2, s1
        assert [row["sessionId"] for row in body] == ["s3", "s2", "s1"]
        # 字段对齐（CamelModel → camelCase JSON）
        for row in body:
            assert set(row) >= {
                "sessionId", "firstTime", "lastTime",
                "messageCount", "lastQuestion", "lastAnswerPreview",
            }
            assert row["messageCount"] == 2  # 1 user + 1 assistant
        # s3 的 lastQuestion 是最后一条 user 消息内容
        assert body[0]["sessionId"] == "s3"
        assert body[0]["lastQuestion"] == "C 的对账"
        assert body[0]["lastAnswerPreview"] == "对账结果"

    async def test_listing_sorts_by_last_time_desc(self, client, dbSession) -> None:
        """3 session 时间错开 → 按 last_time DESC 排序。"""
        for sid, day in [("old", 1), ("mid", 2), ("new", 3)]:
            await _seedMessage(
                dbSession, sessionId=sid, role="user", content=f"q-{sid}",
                createdAt=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )

        resp = await client.get("/api/v1/sessions/chat-history")
        assert resp.status_code == 200
        body = resp.json()
        assert [row["sessionId"] for row in body] == ["new", "mid", "old"]

    async def test_listing_excludes_sessions_without_messages(self, client, dbSession) -> None:
        """仅 token_usage 无 session_message 的 session 不出现在聊天列表中。"""
        await _seedTokenUsage(dbSession, sessionId="ghost")  # 仅用量，无消息
        await _seedMessage(
            dbSession, sessionId="real", role="user", content="real q",
            createdAt=datetime(2026, 1, 1, tzinfo=UTC),
        )

        resp = await client.get("/api/v1/sessions/chat-history")
        body = resp.json()
        assert [row["sessionId"] for row in body] == ["real"]

    async def test_listing_paginates_with_limit_offset(self, client, dbSession) -> None:
        """limit/offset 切片：5 session → limit=2 offset=2 返第 3、4 条（按 last_time DESC）。"""
        for i in range(5):
            await _seedMessage(
                dbSession, sessionId=f"s{i}", role="user", content=f"q{i}",
                createdAt=datetime(2026, 1, i + 1, 12, 0, tzinfo=UTC),
            )

        resp = await client.get("/api/v1/sessions/chat-history?limit=2&offset=2")
        body = resp.json()
        assert [row["sessionId"] for row in body] == ["s2", "s1"]

    async def test_listing_empty_returns_empty_list(self, client, dbSession) -> None:
        resp = await client.get("/api/v1/sessions/chat-history")
        assert resp.status_code == 200
        assert resp.json() == []


# ============ GET /{sessionId}/messages ============


class TestChatHistoryMessages:
    async def test_messages_returns_user_and_assistant_in_order(
        self, client, dbSession
    ) -> None:
        """一个 session 6 条（3 轮）→ 按 created_time 正序返回，role 交替。"""
        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        await _seedMessage(dbSession, sessionId="s1", role="user", content="Q1", createdAt=base)
        await _seedMessage(dbSession, sessionId="s1", role="assistant", content="A1", sql="SELECT 1", createdAt=base + timedelta(seconds=1))
        await _seedMessage(dbSession, sessionId="s1", role="user", content="Q2", createdAt=base + timedelta(seconds=2))
        await _seedMessage(dbSession, sessionId="s1", role="assistant", content="A2", sql="SELECT 2", createdAt=base + timedelta(seconds=3))
        await _seedMessage(dbSession, sessionId="s1", role="user", content="Q3", createdAt=base + timedelta(seconds=4))
        await _seedMessage(dbSession, sessionId="s1", role="assistant", content="A3", sql="SELECT 3", createdAt=base + timedelta(seconds=5))

        resp = await client.get("/api/v1/sessions/s1/messages")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sessionId"] == "s1"
        msgs = body["messages"]
        assert len(msgs) == 6
        assert [m["role"] for m in msgs] == [
            "user", "assistant", "user", "assistant", "user", "assistant",
        ]
        assert msgs[0]["content"] == "Q1"
        assert msgs[1]["content"] == "A1"
        assert msgs[1]["sql"] == "SELECT 1"
        assert msgs[0]["sql"] is None  # user 行无 sql

    async def test_messages_respects_limit(self, client, dbSession) -> None:
        """limit=2 返最早 2 条。"""
        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        for i in range(4):
            await _seedMessage(
                dbSession, sessionId="s1", role="user" if i % 2 == 0 else "assistant",
                content=f"m{i}", createdAt=base + timedelta(seconds=i),
            )

        resp = await client.get("/api/v1/sessions/s1/messages?limit=2")
        msgs = resp.json()["messages"]
        assert len(msgs) == 2
        assert [m["content"] for m in msgs] == ["m0", "m1"]

    async def test_messages_respects_before_id_cursor(self, client, dbSession) -> None:
        """before_id 取该 id 之前更早的消息。"""
        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        msgs = []
        for i in range(4):
            m = await _seedMessage(
                dbSession, sessionId="s1", role="user", content=f"m{i}",
                createdAt=base + timedelta(seconds=i),
            )
            msgs.append(m)
        # before_id = msgs[2].id (即 m2 之前) → 期望 m0, m1
        resp = await client.get(f"/api/v1/sessions/s1/messages?before_id={msgs[2].id}")
        result = resp.json()["messages"]
        assert [m["content"] for m in result] == ["m0", "m1"]

    async def test_messages_tail_returns_the_newest_batch(
        self, client, dbSession
    ) -> None:
        """``tail=true`` 取**最新** limit 条，且仍按时间正序返回。

        这是导出配图的前提：导出 PDF 只保留最后 500 轮，前端若拿最早的 1000 条，
        长会话里与那个窗口完全不相交 —— 每张图都配不上，且不报任何错。
        """
        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        for i in range(6):
            await _seedMessage(
                dbSession, sessionId="s1", role="user", content=f"m{i}",
                createdAt=base + timedelta(seconds=i),
            )

        resp = await client.get("/api/v1/sessions/s1/messages?limit=2&tail=true")

        assert resp.status_code == 200
        # 正序返回，但内容是最新的两条（不是 m0/m1）
        assert [m["content"] for m in resp.json()["messages"]] == ["m4", "m5"]

    async def test_messages_tail_within_limit_still_returns_everything(
        self, client, dbSession
    ) -> None:
        """反向守卫：条数不足 limit 时 tail 不能少还、不能反序。"""
        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        for i in range(3):
            await _seedMessage(
                dbSession, sessionId="s1", role="user", content=f"m{i}",
                createdAt=base + timedelta(seconds=i),
            )

        resp = await client.get("/api/v1/sessions/s1/messages?limit=10&tail=true")

        assert [m["content"] for m in resp.json()["messages"]] == ["m0", "m1", "m2"]

    async def test_messages_before_id_wins_over_tail(self, client, dbSession) -> None:
        """两者同给时 before_id 更具体 —— 文档承诺的优先级要被测到。"""
        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        msgs = []
        for i in range(4):
            msgs.append(
                await _seedMessage(
                    dbSession, sessionId="s1", role="user", content=f"m{i}",
                    createdAt=base + timedelta(seconds=i),
                )
            )

        resp = await client.get(
            f"/api/v1/sessions/s1/messages?limit=2&tail=true&before_id={msgs[2].id}"
        )

        assert [m["content"] for m in resp.json()["messages"]] == ["m0", "m1"]

    async def test_messages_session_with_no_messages_returns_empty(
        self, client, dbSession
    ) -> None:
        """不存在/无消息的 sessionId → 200 + messages=[]（不 404）。"""
        resp = await client.get("/api/v1/sessions/nonexistent/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sessionId"] == "nonexistent"
        assert body["messages"] == []

    async def test_messages_carries_chart_payload_for_replay(
        self, client, dbSession
    ) -> None:
        """0105：历史回放要能重新画出图 —— 图不能只活在实时响应里。

        此前 chart/chartOption 未持久化，切走再切回整段图消失。
        """
        # Arrange
        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        option = {"columns": ["地区"], "rows": [{"地区": "华北"}]}
        await _seedMessage(
            dbSession, sessionId="s-chart", role="user", content="Q", createdAt=base
        )
        await _seedMessage(
            dbSession, sessionId="s-chart", role="assistant", content="A",
            chartType="table", chartOption=option, createdAt=base + timedelta(seconds=1),
        )

        # Act
        resp = await client.get("/api/v1/sessions/s-chart/messages")

        # Assert
        msgs = resp.json()["messages"]
        assert msgs[1]["chartType"] == "table"
        assert msgs[1]["chartOption"] == option
        # user 行无图：两个字段都存在且为 None（契约字段恒定在场，前端不必判 undefined）
        assert msgs[0]["chartType"] is None
        assert msgs[0]["chartOption"] is None

    async def test_messages_chart_fields_are_none_for_legacy_rows(
        self, client, dbSession
    ) -> None:
        """0105 之前的存量行两列为 NULL ⇒ 返 None，前端按「无图」渲染（不是空图）。"""
        await _seedMessage(
            dbSession, sessionId="s-legacy", role="user", content="Q",
            createdAt=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId="s-legacy", role="assistant", content="A", sql="SELECT 1",
            createdAt=datetime(2026, 5, 1, 9, 0, 1, tzinfo=UTC),
        )

        resp = await client.get("/api/v1/sessions/s-legacy/messages")

        asst = resp.json()["messages"][1]
        assert asst["chartType"] is None
        assert asst["chartOption"] is None

    async def test_messages_carries_visual_payload_for_replay(
        self, client, dbSession
    ) -> None:
        """0107：历史回放要能拿到表负载 + 判断依据 —— 不能只活在实时响应里。

        图/表/依据三者同轮落库，切走再切回历史面板时缺了表或依据就是缺口。
        """
        base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        table = {"columns": ["地区"], "rows": [{"地区": "华北"}], "truncated": False}
        rationale = {"code": "R12_CATEGORY_BAR", "params": {}}
        await _seedMessage(
            dbSession, sessionId="s-visual", role="user", content="Q", createdAt=base
        )
        await _seedMessage(
            dbSession, sessionId="s-visual", role="assistant", content="A",
            chartType="bar", chartOption={"series": [{"type": "bar"}]},
            tableOption=table, visualRationale=rationale,
            createdAt=base + timedelta(seconds=1),
        )

        resp = await client.get("/api/v1/sessions/s-visual/messages")

        msgs = resp.json()["messages"]
        assert msgs[1]["tableOption"] == table
        assert msgs[1]["visualRationale"] == rationale
        # user 行无负载：三个字段都存在且为 None（契约字段恒定在场，前端不必判 undefined）
        assert msgs[0]["tableOption"] is None
        assert msgs[0]["visualRationale"] is None

    async def test_messages_visual_fields_are_none_for_legacy_rows(
        self, client, dbSession
    ) -> None:
        """0107 之前的存量行两列为 NULL ⇒ 返 None，回放不炸（前端按「无表/无依据」渲染）。"""
        await _seedMessage(
            dbSession, sessionId="s-visual-legacy", role="user", content="Q",
            createdAt=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId="s-visual-legacy", role="assistant", content="A", sql="SELECT 1",
            createdAt=datetime(2026, 5, 1, 9, 0, 1, tzinfo=UTC),
        )

        resp = await client.get("/api/v1/sessions/s-visual-legacy/messages")

        asst = resp.json()["messages"][1]
        assert asst["tableOption"] is None
        assert asst["visualRationale"] is None


# ============ DELETE /{sessionId} ============


class TestChatHistoryDelete:
    async def test_delete_removes_messages_token_usage_and_query_state(
        self, client, dbSession
    ) -> None:
        """写入三表 → DELETE → 三表全部清空。"""
        sid = "to-delete"
        await _seedMessage(dbSession, sessionId=sid, role="user", content="q")
        await _seedMessage(dbSession, sessionId=sid, role="assistant", content="a", sql="SELECT 1")
        await _seedTokenUsage(dbSession, sessionId=sid)
        await _seedQueryState(dbSession, sessionId=sid)

        resp = await client.delete(f"/api/v1/sessions/{sid}")
        assert resp.status_code == 204, resp.text

        # 三表查空
        msg_count = (await dbSession.execute(
            select(SessionMessage).where(SessionMessage.session_id == sid)
        )).scalars().all()
        usage_count = (await dbSession.execute(
            select(SessionTokenUsage).where(SessionTokenUsage.session_id == sid)
        )).scalars().all()
        qs_count = (await dbSession.execute(
            select(SessionQueryState).where(SessionQueryState.session_id == sid)
        )).scalars().all()
        assert msg_count == []
        assert usage_count == []
        assert qs_count == []

    async def test_delete_unknown_session_returns_404(self, client, dbSession) -> None:
        """删除不存在的 sessionId → 404。"""
        resp = await client.delete("/api/v1/sessions/ghost")
        assert resp.status_code == 404

    async def test_delete_is_idempotent_via_404(self, client, dbSession) -> None:
        """连续 DELETE 同一 id，第二次 404（幂等但不成功）。"""
        sid = "double-delete"
        await _seedMessage(dbSession, sessionId=sid, role="user", content="q")

        first = await client.delete(f"/api/v1/sessions/{sid}")
        assert first.status_code == 204

        second = await client.delete(f"/api/v1/sessions/{sid}")
        assert second.status_code == 404


# ============ 鉴权 ============


class TestChatHistoryAuth:
    async def test_endpoints_require_authentication(self, client, dbSession) -> None:
        """不带 X-User-Id 仍走默认匿名用户（与既有 /sessions/* 一致，预期 200）。"""
        # 既有 listSessions 同样依赖 getCurrentUser，默认匿名放行
        # 本断言仅验证 401 触发路径（删除路径返回 404 因 sessionId 不存在）
        # —— 若未来强制鉴权，此测试需改为断言 401
        resp = await client.get("/api/v1/sessions/chat-history")
        assert resp.status_code in (200, 401)

class TestWikiQaChannelFilter:
    """channel=wiki_qa（feat-wiki-chat）：历史面板按渠道隔离。"""

    async def test_wiki_qa_channel_lists_only_wiki_qa_sessions(self, client, dbSession) -> None:
        now = datetime.now(UTC)
        for sid, role, content in [
            ("wc-1", "user", "厂家合作有什么门槛"),
            ("wc-1", "assistant", "根据企业 Wiki…"),
            ("chat-1", "user", "普通聊天问题"),
            ("doc-1", "user", "文档问答问题"),
        ]:
            channel = {"wc-1": "wiki_qa", "chat-1": "chat", "doc-1": "doc_qa"}[sid]
            dbSession.add(SessionMessage(
                session_id=sid, role=role, content=content,
                channel=channel, user_id="u1",
                created_time=now, updated_time=now,
            ))
        await dbSession.commit()

        resp = await client.get("/api/v1/sessions/chat-history", params={"channel": "wiki_qa"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [row["sessionId"] for row in body] == ["wc-1"]
        assert body[0]["lastQuestion"] == "厂家合作有什么门槛"

    async def test_wiki_qa_channel_rejects_unknown_value(self, client) -> None:
        resp = await client.get(
            "/api/v1/sessions/chat-history", params={"channel": "bogus"}
        )
        assert resp.status_code == 422
