"""chat_usage 透传 cost_source 到 SessionTokenUsage。

RED: cost_source 字段完全不存在。
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_recordUsage_passes_cost_source():
    """TokenUsageService.recordUsage 把 cost_source 写入新行。"""
    from app.services.token_usage_service import TokenUsageService

    svc = TokenUsageService()
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    usage_obj = MagicMock()
    usage_obj.cost = Decimal("0.001")
    # 让 refresh 返回 usage 对象的属性可读
    session.refresh.return_value = None

    captured = {}
    def capture_add(obj):
        captured["obj"] = obj
    session.add.side_effect = capture_add

    await svc.recordUsage(
        session,
        sessionId="s1",
        modelConfigId=None,
        modelName="test-model",
        promptTokens=10,
        completionTokens=5,
        cost=Decimal("0.001"),
        purpose="test",
        cost_source="estimated",
    )
    # cost_source 必须出现在 ORM 对象构造参数里
    assert captured["obj"].cost_source == "estimated"
