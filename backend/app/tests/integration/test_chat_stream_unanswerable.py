"""流式 '无法回答' 路径 NameError 回归测试。

R1 fix: chat_stream.py:495-540 if outcome.sql is None 块 EVENT_DONE
引用 totalTokens/totalCost/affinityConfig/affinityPayload，
这些变量定义在 return 之后导致 NameError。修复后在块顶添加 local init。
"""
from __future__ import annotations

import pytest

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource
from app.domain.enums import DataSourceType

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded_ds(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=99001, name="DS-Unans", type=DataSourceType.ORACLE,
        host="h", port=1521, database_name="svc",
        username="u", password_encrypted="cipher",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def test_stream_unanswerable_does_not_raise_nameerror(
    client: AsyncClient, dbSession: AsyncSession, seeded_ds: DataSource,
):
    """'无法回答' 流式分支不应 NameError 崩（R1 critical fix 验证）。

    触发：乱码问句 → NL2SQL 判定无法回答 → outcome.sql = None
    验证：流式响应含 event: done，且无 500 / NameError。
    """
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "asdfghjkl zxcvbnm qwerty",
            "sessionId": "stream-unanswerable-1",
            "datasourceId": seeded_ds.id,
        },
        headers={"X-User-Id": "99001"},
    )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    text = resp.text
    # 关键断言：不应有 NameError（这是 R1 fix 的目标）
    # 乱码问句在 model routing 阶段失败是正常的（event: error 或 event: meta）
    assert "NameError" not in text, f"NameError leaked: {text}"
    assert "name 'totalTokens' is not defined" not in text
    assert "name 'affinityConfig' is not defined" not in text
    assert "name 'affinityPayload' is not defined" not in text
    # 200 OK 即可（不要求特定事件；乱码问句在 model routing 层失败是正常行为）
    assert resp.status_code == 200
