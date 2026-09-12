"""机制 1 分类调整 + 学习闭环反馈的集成测试（feat-wiki-knowledge M3）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。

覆盖两条**入口**（PATCH 维度覆盖 / POST reclassify）与**同一条落库路径**：
- CONFIRM / MODIFY / REJECT 三态判定
- 无建议（auto_classification 为空）时不产生反馈事件
- 未提供 dimension 的 PATCH 不产生反馈
- 非法维度 422 时不留反馈事件（与维度写入同事务）
- actor 取自认证用户（不信任请求体）
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_learning_models import LearningFeedback
from app.domain.wiki_models import WikiPage

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"

# admin 头：用户名不命中 DB 用户时回退 stub 默认（含 admin 角色，dbUserId=None）
_ADMIN_HEADERS = {"X-User-Id": "wiki-feedback-admin", "X-User-Roles": "admin"}

_SUGGESTION = {
    "primary": "RULE",
    "confidence": 0.92,
    "alternatives": ["POLICY"],
    "reason": "含准入门槛阈值",
}


async def _pageWithSuggestion(
    client: AsyncClient,
    dbSession: AsyncSession,
    *,
    suggestion: dict | None = _SUGGESTION,
    dimension: str | None = "RULE",
) -> str:
    """建一条 Page 并写入机制 1 的分类建议，返回 pageId。

    刻意不走导入链路：本文件要测的是「建议已存在时用户怎么处置它」，
    建议从哪来（LLM 还是别的）与判定逻辑无关，直接造数更稳。
    """
    resp = await client.post(
        _PAGES, json={"title": "供应商准入规则", "content": "注册资本 >= 1000 万"}
    )
    assert resp.status_code == 201
    pageId = resp.json()["pageId"]

    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    page.auto_classification = suggestion
    page.dimension = dimension
    await dbSession.commit()
    return pageId


async def _feedbacks(dbSession: AsyncSession) -> list[LearningFeedback]:
    result = await dbSession.execute(
        select(LearningFeedback).order_by(LearningFeedback.id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# PATCH 入口：维度覆盖即反馈
# ---------------------------------------------------------------------------


async def test_patch_same_dimension_records_confirm(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """PATCH 的维度与建议一致 → CONFIRM，且不写 user_modification。"""
    # Arrange
    pageId = await _pageWithSuggestion(client, dbSession)

    # Act
    resp = await client.patch(f"{_PAGES}/{pageId}", json={"dimension": "RULE"})

    # Assert
    assert resp.status_code == 200
    rows = await _feedbacks(dbSession)
    assert len(rows) == 1
    row = rows[0]
    assert row.mechanism == "CLASSIFY"
    assert row.entity_type == "WIKI_PAGE"
    assert row.entity_id == pageId
    assert row.user_action == "CONFIRM"
    assert row.system_output == _SUGGESTION
    assert row.user_modification is None
    # 建议生成时的输入快照：事后回溯要能看当时喂给模型的是什么
    assert row.input_snapshot["title"] == "供应商准入规则"


async def test_patch_snapshot_uses_pre_edit_title(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同时改标题与维度时，快照必须是**改动前**的标题。

    这是「输入 → 输出」配对的正确性要求：``system_output`` 里那条建议是基于
    旧标题/旧正文算出来的，若快照取到改写后的新标题，反馈表里就出现一组
    现实中从未同时存在的配对，日后训练分类器等于喂脏数据。
    """
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.patch(
        f"{_PAGES}/{pageId}",
        json={"title": "供应商准入规则（2026 修订）", "dimension": "PROCESS"},
    )

    assert resp.status_code == 200
    assert resp.json()["title"] == "供应商准入规则（2026 修订）"  # 页面确实改了
    rows = await _feedbacks(dbSession)
    assert len(rows) == 1
    assert rows[0].user_action == "MODIFY"
    # 快照仍是改动前的标题：与 system_output 的建议同源
    assert rows[0].input_snapshot["title"] == "供应商准入规则"


async def test_patch_different_dimension_records_modify(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """PATCH 改成别的维度 → MODIFY，user_modification 记下改成了什么。"""
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.patch(f"{_PAGES}/{pageId}", json={"dimension": "PROCESS"})

    assert resp.status_code == 200
    assert resp.json()["dimension"] == "PROCESS"
    rows = await _feedbacks(dbSession)
    assert len(rows) == 1
    assert rows[0].user_action == "MODIFY"
    assert rows[0].user_modification == {"dimension": "PROCESS"}
    # 原始建议不能被改写：它才是「系统当时说了什么」的证据
    assert rows[0].system_output == _SUGGESTION


async def test_patch_null_dimension_records_reject(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """显式置空维度 = 否掉这个分类 → REJECT（区别于「未提供」= 保持）。"""
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.patch(f"{_PAGES}/{pageId}", json={"dimension": None})

    assert resp.status_code == 200
    assert resp.json()["dimension"] is None
    rows = await _feedbacks(dbSession)
    assert len(rows) == 1
    assert rows[0].user_action == "REJECT"
    assert rows[0].user_modification is None


async def test_patch_without_dimension_records_nothing(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """只改标题/正文不是对分类的反馈，不该污染反馈流。"""
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.patch(f"{_PAGES}/{pageId}", json={"title": "改个标题"})

    assert resp.status_code == 200
    assert await _feedbacks(dbSession) == []


async def test_patch_dimension_without_suggestion_records_nothing(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """从未被分类过的条目（autoClassify 关闭时导入）→ 无建议可反馈，不记事件。

    这条守的是「反馈」的定义：反馈是「对系统输出的处置」。把人工首次设定
    维度记成 MODIFY，训练数据里就会出现大量「系统什么都没说却被改」的噪声。
    """
    pageId = await _pageWithSuggestion(client, dbSession, suggestion=None, dimension=None)

    resp = await client.patch(f"{_PAGES}/{pageId}", json={"dimension": "CONCEPT"})

    assert resp.status_code == 200
    assert resp.json()["dimension"] == "CONCEPT"
    assert await _feedbacks(dbSession) == []


async def test_patch_invalid_dimension_writes_no_feedback(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """非法维度 422 → 维度没改，反馈也不该留下（同一事务，一起回滚）。"""
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.patch(f"{_PAGES}/{pageId}", json={"dimension": "NOT_A_DIM"})

    assert resp.status_code == 422
    assert await _feedbacks(dbSession) == []
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    assert page.dimension == "RULE"  # 保持原值


async def test_patch_feedback_actor_comes_from_auth_not_body(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """反馈的 actor 取自已认证用户（dbUserId），不接受请求体自述。

    回归点：若能由 body 指定 feedbackUserId，谁都可以伪造「某人确认过分类」，
    学习闭环的归因就失去了证据价值。
    """
    from scripts.seed_rbac import seedRbacBaseline

    await seedRbacBaseline(dbSession)
    created = await client.post(
        "/api/v1/users",
        headers=_ADMIN_HEADERS,
        json={"username": "wiki-curator", "display_name": "wiki-curator", "email": None},
    )
    assert created.status_code == 201, created.text
    curatorId = created.json()["id"]

    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.patch(
        f"{_PAGES}/{pageId}",
        headers={"X-User-Id": "wiki-curator"},
        json={"dimension": "PROCESS", "feedbackUserId": 99999},
    )

    assert resp.status_code == 200
    rows = await _feedbacks(dbSession)
    assert len(rows) == 1
    assert rows[0].feedback_user_id == curatorId


# ---------------------------------------------------------------------------
# reclassify 端点
# ---------------------------------------------------------------------------


async def test_reclassify_returns_page_and_action(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """reclassify 返回更新后的条目 + 本次记录的处置动作。"""
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.post(f"{_PAGES}/{pageId}/reclassify", json={"dimension": "PROCESS"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "MODIFY"
    assert body["page"]["pageId"] == pageId
    assert body["page"]["dimension"] == "PROCESS"
    assert len(await _feedbacks(dbSession)) == 1


async def test_reclassify_same_dimension_reports_confirm(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.post(f"{_PAGES}/{pageId}/reclassify", json={"dimension": "RULE"})

    assert resp.status_code == 200
    assert resp.json()["action"] == "CONFIRM"


async def test_reclassify_null_reports_reject(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """显式 null = 打回该分类，动作是 REJECT 而不是 MODIFY。"""
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.post(f"{_PAGES}/{pageId}/reclassify", json={"dimension": None})

    assert resp.status_code == 200
    assert resp.json()["action"] == "REJECT"
    assert resp.json()["page"]["dimension"] is None


async def test_reclassify_without_suggestion_reports_no_action(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """无建议时 action 为 null —— 如实告诉调用方「这次没记反馈」。

    前端据此提示「已设定维度」而非「已记录修正」，避免对用户撒谎。
    """
    pageId = await _pageWithSuggestion(client, dbSession, suggestion=None, dimension=None)

    resp = await client.post(f"{_PAGES}/{pageId}/reclassify", json={"dimension": "RULE"})

    assert resp.status_code == 200
    assert resp.json()["action"] is None
    assert await _feedbacks(dbSession) == []


async def test_reclassify_missing_dimension_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """dimension 是必填键：缺失 → 422（不能把「漏传」当成「清空」）。"""
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.post(f"{_PAGES}/{pageId}/reclassify", json={})

    assert resp.status_code == 422
    assert await _feedbacks(dbSession) == []


async def test_reclassify_invalid_dimension_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    pageId = await _pageWithSuggestion(client, dbSession)

    resp = await client.post(
        f"{_PAGES}/{pageId}/reclassify", json={"dimension": "NOT_A_DIM"}
    )

    assert resp.status_code == 422
    assert await _feedbacks(dbSession) == []
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    assert page.dimension == "RULE"


async def test_reclassify_missing_page_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        f"{_PAGES}/NO-SUCH-PAGE/reclassify", json={"dimension": "RULE"}
    )
    assert resp.status_code == 404


async def test_reclassify_requires_auth(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉 stub auth（模拟生产）→ 未认证的重新分类必须 403。"""
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")
    resp = await client.post(f"{_PAGES}/ANY/reclassify", json={"dimension": "RULE"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# FeedbackLoop.record 自身的不变量
# ---------------------------------------------------------------------------


async def test_record_modify_without_modification_raises(
    dbSession: AsyncSession,
) -> None:
    """MODIFY 必须带 user_modification：缺了它就说不清「改成了什么」。"""
    from app.domain.exceptions import ValidationError
    from app.services.learning.feedback_loop import FeedbackLoop

    with pytest.raises(ValidationError):
        await FeedbackLoop().record(
            dbSession,
            mechanism="CLASSIFY",
            entityType="WIKI_PAGE",
            entityId="PAGE-X",
            userAction="MODIFY",
        )


async def test_record_confirm_with_modification_raises(
    dbSession: AsyncSession,
) -> None:
    """CONFIRM/REJECT 不能带 user_modification：否则三态统计口径会重叠。"""
    from app.domain.exceptions import ValidationError
    from app.services.learning.feedback_loop import FeedbackLoop

    with pytest.raises(ValidationError):
        await FeedbackLoop().record(
            dbSession,
            mechanism="CLASSIFY",
            entityType="WIKI_PAGE",
            entityId="PAGE-X",
            userAction="CONFIRM",
            userModification={"dimension": "RULE"},
        )


async def test_record_unknown_action_raises(dbSession: AsyncSession) -> None:
    from app.domain.exceptions import ValidationError
    from app.services.learning.feedback_loop import FeedbackLoop

    with pytest.raises(ValidationError):
        await FeedbackLoop().record(
            dbSession,
            mechanism="CLASSIFY",
            entityType="WIKI_PAGE",
            entityId="PAGE-X",
            userAction="APPROVE",
        )


async def test_record_unknown_entity_type_raises(dbSession: AsyncSession) -> None:
    from app.domain.exceptions import ValidationError
    from app.services.learning.feedback_loop import FeedbackLoop

    with pytest.raises(ValidationError):
        await FeedbackLoop().record(
            dbSession,
            mechanism="CLASSIFY",
            entityType="NOT_AN_ENTITY",
            entityId="X",
            userAction="CONFIRM",
        )


async def test_record_persists_snapshots(dbSession: AsyncSession) -> None:
    """record 只把行加进当前事务，由调用方 commit（与 Page 更新同生共死）。"""
    from app.services.learning.feedback_loop import FeedbackLoop

    row = await FeedbackLoop().record(
        dbSession,
        mechanism="RELATE",
        entityType="RELATION",
        entityId="42",
        userAction="CONFIRM",
        inputSnapshot={"title": "甲"},
        systemOutput={"confidence": 0.7},
        userId=None,
    )
    await dbSession.commit()

    stored = (
        await dbSession.execute(
            select(LearningFeedback).where(LearningFeedback.id == row.id)
        )
    ).scalar_one()
    assert stored.mechanism == "RELATE"
    assert stored.entity_type == "RELATION"
    assert stored.entity_id == "42"
    assert stored.input_snapshot == {"title": "甲"}
    assert stored.system_output == {"confidence": 0.7}
    assert stored.feedback_at is not None
