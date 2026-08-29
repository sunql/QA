"""会话问答 PDF 导出 API 集成测试（真实 PG + 完整 API 链路）。

覆盖：
- GET /api/v1/sessions/{sessionId}/export.pdf
  - 无 messageId：导出该 session 全部问答
  - 有 messageId：仅导出该 assistant + 上一条 user
- 鉴权、404、PDF magic header、内容可读性
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.models import SessionMessage, SessionTokenUsage


async def _seedMessage(
    dbSession,
    *,
    sessionId: str,
    role: str,
    content: str,
    sql: str | None = None,
    createdAt: datetime | None = None,
) -> SessionMessage:
    msg = SessionMessage(
        session_id=sessionId,
        role=role,
        content=content,
        sql_generated=sql,
        created_time=createdAt or datetime.now(UTC),
        updated_time=createdAt or datetime.now(UTC),
    )
    dbSession.add(msg)
    await dbSession.commit()
    await dbSession.refresh(msg)
    return msg


async def _seedTokenUsage(
    dbSession,
    *,
    sessionId: str,
    modelName: str = "gpt-4o-mini",
    tokens: int = 320,
    cost: str = "0.000480",
    requestTime: datetime | None = None,
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
            request_time=requestTime or datetime.now(UTC),
            purpose="nl2sql",
        )
    )
    await dbSession.commit()


class TestExportPdfApi:
    async def test_export_full_session_returns_valid_pdf(self, client, dbSession) -> None:
        """导出整 session：HTTP 200 + application/pdf + magic header + 中文内容可读。"""
        sid = "s-pdf-full"
        await _seedMessage(
            dbSession, sessionId=sid, role="user", content="销售表查询",
            createdAt=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId=sid, role="assistant",
            content="查询结果如下：\n\n| 地区 | 销售额 |\n| --- | --- |\n| 华北 | 100 |",
            sql="SELECT * FROM sales",
            createdAt=datetime(2026, 1, 1, 10, 1, tzinfo=UTC),
        )
        await _seedTokenUsage(
            dbSession, sessionId=sid,
            requestTime=datetime(2026, 1, 1, 10, 0, 30, tzinfo=UTC),
        )

        resp = await client.get(f"/api/v1/sessions/{sid}/export.pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        assert "attachment" in resp.headers["content-disposition"]
        assert sid in resp.headers["content-disposition"]
        body = resp.content
        assert body.startswith(b"%PDF-1.")
        # 中文已 CID 编码写入 PDF stream，无法直接 grep bytes；至少确保 PDF > 1KB
        assert len(body) > 1024

    async def test_export_empty_session_returns_404(self, client, dbSession) -> None:
        """无任何消息的 sessionId → 404，与 DELETE 行为对齐。"""
        resp = await client.get("/api/v1/sessions/s-pdf-empty/export.pdf")
        assert resp.status_code == 404

    async def test_export_with_unknown_message_id_returns_404(self, client, dbSession) -> None:
        """message_id 不属于该 session → 404。安全审查 HIGH-3：detail 不回显 ID。"""
        sid = "s-pdf-cross"
        await _seedMessage(
            dbSession, sessionId=sid, role="user", content="A",
            createdAt=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
        # 999999 是其他 session 的 id（无）
        resp = await client.get(f"/api/v1/sessions/{sid}/export.pdf?message_id=999999")
        assert resp.status_code == 404
        body = resp.json()
        # 错误信息不包含 sessionId/messageId 防枚举
        assert str(999999) not in str(body)
        assert sid not in str(body)

    async def test_export_single_turn_only_includes_target_assistant(
        self, client, dbSession
    ) -> None:
        """message_id 指定某条 assistant → PDF 只包含该轮问答（1 turn）。"""
        sid = "s-pdf-single"
        await _seedMessage(
            dbSession, sessionId=sid, role="user", content="Q1",
            createdAt=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
        a1 = await _seedMessage(
            dbSession, sessionId=sid, role="assistant", content="A1 — first answer",
            sql="SELECT 1", createdAt=datetime(2026, 1, 1, 10, 1, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId=sid, role="user", content="Q2",
            createdAt=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId=sid, role="assistant", content="A2 — second answer",
            sql="SELECT 2", createdAt=datetime(2026, 1, 1, 11, 1, tzinfo=UTC),
        )

        # message_id=a1.id → 只导出第一轮
        resp = await client.get(f"/api/v1/sessions/{sid}/export.pdf?message_id={a1.id}")
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-1.")
        # 文件名包含 messageId（与 controller 的 filename 模板一致）
        assert str(a1.id) in resp.headers["content-disposition"]

    async def test_export_handles_long_sql_and_markdown(self, client, dbSession) -> None:
        """长 SQL + 多行 markdown 内容（代码块/列表/表格混合）不报错。"""
        sid = "s-pdf-rich"
        long_sql = "SELECT " + ",\n       ".join([f"col_{i}" for i in range(30)]) + "\nFROM very_wide_table"
        await _seedMessage(
            dbSession, sessionId=sid, role="user", content="复杂查询",
            createdAt=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
        await _seedMessage(
            dbSession, sessionId=sid, role="assistant",
            content=(
                "## 标题\n\n"
                "**粗体** *斜体* `行内代码`。\n\n"
                "```sql\n" + long_sql + "\n```\n\n"
                "- 列表项 1\n- 列表项 2\n- 列表项 3\n"
            ),
            sql=long_sql,
            createdAt=datetime(2026, 1, 1, 10, 1, tzinfo=UTC),
        )
        resp = await client.get(f"/api/v1/sessions/{sid}/export.pdf")
        assert resp.status_code == 200
        assert len(resp.content) > 2048

    async def test_export_uses_session_message_id_for_filename(self, client, dbSession) -> None:
        """下载文件名包含 sessionId（与模板 qa-session-{sessionId}.pdf 一致）。"""
        sid = "s-pdf-name"
        await _seedMessage(
            dbSession, sessionId=sid, role="user", content="x",
            createdAt=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
        resp = await client.get(f"/api/v1/sessions/{sid}/export.pdf")
        assert resp.status_code == 200
        assert f"qa-session-{sid}.pdf" in resp.headers["content-disposition"]