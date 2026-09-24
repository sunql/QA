"""机制 3 冲突检测 + 处置的集成测试（feat-wiki-knowledge M5）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。
LLM 走 patch 注入假客户端 —— 不联外网，但保留真实调用链
（ModelConfigService 查配置 → createClient → completeJson → 计量落库）。

覆盖：
- 确定性三路：STALENESS（引用失效条目）/ GAP（悬空引用）/ OVERLAP（标题重复）
- SUPERSEDES 是「合法地指向旧版本」，不该被误报成 STALENESS
- LLM 一路：矛盾判定 + **模型编造的 pageId 必须被丢弃**
- LLM 失败不吞掉确定性结果；不给 modelId 时不调模型
- 幂等（重复检测不重复建冲突）+ 处置后可再次产生
- page_ids 排序不变量（部分唯一索引成立的前提）
- 处置（RESOLVED/IGNORED/MERGED）三态 + 重复处置 409
- 未认证 403 / 不存在 404 / 非法动作 422
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import LlmClientError
from app.domain.models import LlmConfig
from app.domain.wiki_learning_models import KnowledgeConflict, WikiTokenUsage
from app.domain.wiki_models import KnowledgeRelation, WikiPage
from app.infrastructure.llm.base_client import LlmResponse

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_CONFLICTS = "/api/v1/wiki/conflicts"

_INVOKER_CLIENT = "app.services.learning.llm_invoker.createClient"

# 假模型返回：判定候选页 B 与本文矛盾
_CONTRADICTION_JSON = json.dumps(
    {"contradictions": [{"pageId": "PLACEHOLDER", "reason": "阈值相互矛盾", "severity": "HIGH"}]}
)


class _FakeLlmClient:
    """假 LLM 客户端（与 test_wiki_relation_api 同形）。"""

    def __init__(self, content: str, *, raiseError: bool = False) -> None:
        self._content = content
        self._raiseError = raiseError
        self.calls = 0
        self.lastMessages = None

    async def complete(self, messages, **kwargs) -> LlmResponse:
        self.calls += 1
        self.lastMessages = messages
        if self._raiseError:
            raise LlmClientError("LLM 服务不可用")
        return LlmResponse(
            content=self._content,
            modelName="fake-model",
            promptTokens=100,
            completionTokens=50,
            totalTokens=150,
        )


async def _seedModel(dbSession: AsyncSession, *, modelName: str = "wiki-conflict-model") -> int:
    config = LlmConfig(
        model_name=modelName,
        provider="openai_compatible_proxy",
        is_active=True,
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config.id


async def _createPage(
    client: AsyncClient, *, title: str, content: str = "正文内容够长以便引用检测"
) -> str:
    resp = await client.post(_PAGES, json={"title": title, "content": content})
    assert resp.status_code == 201, resp.text
    return resp.json()["pageId"]


async def _setStatus(dbSession: AsyncSession, pageId: str, status: str) -> None:
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    page.status = status
    await dbSession.commit()


async def _addRelation(
    dbSession: AsyncSession,
    *,
    upstreamPageId: str,
    downstreamType: str,
    downstreamId: str,
    relationType: str = "REFERENCES",
) -> None:
    """直接造一条关系（本文件测的是「给定关系后如何判冲突」，与发现机制无关）。"""
    dbSession.add(
        KnowledgeRelation(
            upstream_page_id=upstreamPageId,
            downstream_type=downstreamType,
            downstream_id=downstreamId,
            relation_type=relationType,
            confirmed=True,
        )
    )
    await dbSession.commit()


async def _detect(
    client: AsyncClient, pageId: str, *, modelId: int | None = None
) -> dict:
    resp = await client.post(
        f"{_PAGES}/{pageId}/conflicts/detect", json={"modelId": modelId}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _conflicts(dbSession: AsyncSession) -> list[KnowledgeConflict]:
    result = await dbSession.execute(
        select(KnowledgeConflict).order_by(KnowledgeConflict.id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 确定性三路
# ---------------------------------------------------------------------------


async def test_staleness_detected_when_referencing_expired_page(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """引用了一条已失效的知识 → STALENESS（这是「按老规矩办事」的根因）。"""
    # Arrange
    staleId = await _createPage(client, title="供应商准入规则二零二四")
    await _setStatus(dbSession, staleId, "EXPIRED")
    freshId = await _createPage(client, title="采购作业指引手册")
    await _addRelation(
        dbSession,
        upstreamPageId=freshId,
        downstreamType="PAGE",
        downstreamId=staleId,
    )

    # Act
    body = await _detect(client, freshId)

    # Assert
    assert body["total"] == 1
    assert body["conflicts"][0]["conflictType"] == "STALENESS"
    assert body["conflicts"][0]["detectedBy"] == "RULE"
    assert body["llmStatus"] == "SKIPPED"


async def test_supersedes_relation_is_not_staleness(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """SUPERSEDES 就是「我替代了旧版本」，指向失效条目是**正确用法**，不是冲突。

    反例保护：把这一路写成「只要目标是 EXPIRED 就报冲突」，会把每一份正常的
    版本演进都刷成冲突，看板立刻变成噪声，用户开始忽略整个机制。
    """
    expiredId = await _createPage(client, title="供应商准入规则旧版")
    await _setStatus(dbSession, expiredId, "EXPIRED")
    newId = await _createPage(client, title="供应商准入规则新版")
    await _addRelation(
        dbSession,
        upstreamPageId=newId,
        downstreamType="PAGE",
        downstreamId=expiredId,
        relationType="SUPERSEDES",
    )

    body = await _detect(client, newId)

    assert body["total"] == 0


async def test_gap_detected_for_dangling_class_reference(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """关系指向一个不存在的本体类 → GAP（悬空引用，知识指向了不存在的东西）。"""
    pageId = await _createPage(client, title="物料主数据说明")
    await _addRelation(
        dbSession,
        upstreamPageId=pageId,
        downstreamType="ONTOLOGY_CLASS",
        downstreamId="ClassThatNeverExisted",
    )

    body = await _detect(client, pageId)

    assert body["total"] == 1
    assert body["conflicts"][0]["conflictType"] == "GAP"


async def test_gap_not_raised_for_existing_class(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """目标类存在时不报 GAP —— 否则正常条目全被刷成冲突。"""
    from app.domain.models import OntologyClass

    dbSession.add(OntologyClass(class_name="SupplierMaster"))
    await dbSession.commit()
    pageId = await _createPage(client, title="供应商主数据说明")
    await _addRelation(
        dbSession,
        upstreamPageId=pageId,
        downstreamType="ONTOLOGY_CLASS",
        downstreamId="SupplierMaster",
    )

    body = await _detect(client, pageId)

    assert body["total"] == 0


async def test_overlap_detected_for_duplicate_normalized_title(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """两条条目标题归一化后相同 → OVERLAP（疑似同一件事写了两遍）。"""
    pageId = await _createPage(client, title="供应商准入规则")

    # 直接改库：Create 接口不允许建重名？不，title 无唯一约束，走接口建
    otherId = await _createPage(client, title="供应商 准入 规则")

    body = await _detect(client, pageId)

    assert body["total"] == 1
    conflict = body["conflicts"][0]
    assert conflict["conflictType"] == "OVERLAP"
    # **排序后**落库（不是「集合相同」）：这是部分唯一索引能去重的前提。
    # 不排序时 ['B','A'] 与 ['A','B'] 是数组意义上不相等的两个值，
    # 唯一索引挡不住，每次检测都会新增一行。
    assert conflict["pageIds"] == sorted([pageId, otherId])


# ---------------------------------------------------------------------------
# LLM 一路：矛盾判定
# ---------------------------------------------------------------------------


async def test_llm_contradiction_detected_for_related_pages(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """有 modelId 时，对「已有关系连接的」条目对跑一次模型矛盾判定。"""
    modelId = await _seedModel(dbSession)
    otherId = await _createPage(client, title="供应商准入规则甲")
    pageId = await _createPage(client, title="供应商准入规则乙")
    await _addRelation(
        dbSession, upstreamPageId=pageId, downstreamType="PAGE", downstreamId=otherId
    )
    clientFake = _FakeLlmClient(
        json.dumps(
            {
                "contradictions": [
                    {"pageId": otherId, "reason": "阈值一条写 1000 万一条写 500 万", "severity": "HIGH"}
                ]
            }
        )
    )

    with patch(_INVOKER_CLIENT, return_value=clientFake):
        body = await _detect(client, pageId, modelId=modelId)

    assert body["llmStatus"] == "SUCCEEDED"
    assert body["total"] == 1
    conflict = body["conflicts"][0]
    assert conflict["conflictType"] == "CONTRADICTION"
    assert conflict["detectedBy"] == "LLM"
    assert conflict["severity"] == "HIGH"
    assert sorted(conflict["pageIds"]) == sorted([pageId, otherId])


async def test_llm_invented_page_id_is_discarded(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """模型返回的候选对必须在「本次给它的候选集」内。

    不校验的后果不是错一行数据那么轻：模型编一个不存在的 pageId，冲突表里就
    多一条指向空气的记录，而 ``page_ids`` 是无 FK 的多态数组，DB 层拦不住。
    """
    modelId = await _seedModel(dbSession, modelName="wiki-conflict-halluc")
    otherId = await _createPage(client, title="供应商准入规则甲")
    pageId = await _createPage(client, title="供应商准入规则乙")
    await _addRelation(
        dbSession, upstreamPageId=pageId, downstreamType="PAGE", downstreamId=otherId
    )
    clientFake = _FakeLlmClient(
        json.dumps(
            {
                "contradictions": [
                    {"pageId": "PAGE-NOT-IN-CANDIDATES", "reason": "编的", "severity": "HIGH"}
                ]
            }
        )
    )

    with patch(_INVOKER_CLIENT, return_value=clientFake):
        body = await _detect(client, pageId, modelId=modelId)

    assert body["llmStatus"] == "SUCCEEDED"
    assert body["total"] == 0


async def test_llm_failure_keeps_rule_conflicts(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """模型挂了不能把已经算出来的确定性冲突一起丢掉。"""
    modelId = await _seedModel(dbSession, modelName="wiki-conflict-down")
    staleId = await _createPage(client, title="供应商准入规则过期版")
    await _setStatus(dbSession, staleId, "EXPIRED")
    pageId = await _createPage(client, title="采购作业指引手册二")
    await _addRelation(
        dbSession, upstreamPageId=pageId, downstreamType="PAGE", downstreamId=staleId
    )
    clientFake = _FakeLlmClient("{}", raiseError=True)

    with patch(_INVOKER_CLIENT, return_value=clientFake):
        body = await _detect(client, pageId, modelId=modelId)

    assert body["llmStatus"] == "FAILED"
    assert body["total"] == 1
    assert body["conflicts"][0]["conflictType"] == "STALENESS"


async def test_no_llm_call_without_model_id(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """不给 modelId 就一次模型都不调（本路径必须零 token 成本）。"""
    pageId = await _createPage(client, title="一份普通的知识条目")
    clientFake = _FakeLlmClient(_CONTRADICTION_JSON)

    with patch(_INVOKER_CLIENT, return_value=clientFake):
        body = await _detect(client, pageId, modelId=None)

    assert clientFake.calls == 0
    assert body["llmStatus"] == "SKIPPED"


# ---------------------------------------------------------------------------
# 幂等与生命周期
# ---------------------------------------------------------------------------


async def test_detect_is_idempotent(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """重复检测同一未解决的冲突 → 不重复建行，返回的也是空。"""
    staleId = await _createPage(client, title="供应商准入规则过期版乙")
    await _setStatus(dbSession, staleId, "EXPIRED")
    pageId = await _createPage(client, title="采购作业指引手册三")
    await _addRelation(
        dbSession, upstreamPageId=pageId, downstreamType="PAGE", downstreamId=staleId
    )

    first = await _detect(client, pageId)
    second = await _detect(client, pageId)

    assert first["total"] == 1
    assert second["total"] == 0
    assert len(await _conflicts(dbSession)) == 1


async def test_detect_after_resolve_creates_new_conflict(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """处置（未解决 → 已解决）后不再是「未解决」，同一冲突可再次被检出。

    部分唯一索引只约束「未解决」的那些行，正是为了这条：同一对知识可能反复
    冲突、反复处理，历史要留痕。
    """
    staleId = await _createPage(client, title="供应商准入规则过期版丙")
    await _setStatus(dbSession, staleId, "EXPIRED")
    pageId = await _createPage(client, title="采购作业指引手册四")
    await _addRelation(
        dbSession, upstreamPageId=pageId, downstreamType="PAGE", downstreamId=staleId
    )
    first = await _detect(client, pageId)
    conflictId = first["conflicts"][0]["id"]
    await client.post(f"{_CONFLICTS}/{conflictId}/resolve", json={"action": "RESOLVED"})

    second = await _detect(client, pageId)

    assert second["total"] == 1
    assert len(await _conflicts(dbSession)) == 2


async def test_detect_records_token_usage(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """调了模型就必须留下计量行（核心约束）。"""
    modelId = await _seedModel(dbSession, modelName="wiki-conflict-meter")
    otherId = await _createPage(client, title="供应商准入规则甲二")
    pageId = await _createPage(client, title="供应商准入规则乙二")
    await _addRelation(
        dbSession, upstreamPageId=pageId, downstreamType="PAGE", downstreamId=otherId
    )
    clientFake = _FakeLlmClient(json.dumps({"contradictions": []}))

    with patch(_INVOKER_CLIENT, return_value=clientFake):
        await _detect(client, pageId, modelId=modelId)

    rows = (
        await dbSession.execute(
            select(WikiTokenUsage).where(WikiTokenUsage.mechanism == "CONFLICT")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 100
    assert rows[0].completion_tokens == 50


# ---------------------------------------------------------------------------
# 处置
# ---------------------------------------------------------------------------


async def _oneConflict(client: AsyncClient, dbSession: AsyncSession) -> dict:
    """造出一条 STALENESS 冲突并返回它的读模型。"""
    staleId = await _createPage(client, title="供应商准入规则过期版丁")
    await _setStatus(dbSession, staleId, "EXPIRED")
    pageId = await _createPage(client, title="采购作业指引手册五")
    await _addRelation(
        dbSession, upstreamPageId=pageId, downstreamType="PAGE", downstreamId=staleId
    )
    body = await _detect(client, pageId)
    return body["conflicts"][0]


async def test_resolve_conflict_records_action(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """处置一条冲突：盖章 resolved_at + 记动作 + 记处置人。"""
    conflict = await _oneConflict(client, dbSession)

    resp = await client.post(
        f"{_CONFLICTS}/{conflict['id']}/resolve",
        json={"action": "IGNORED"},
        headers={"X-User-Id": "wiki-conflict-reviewer", "X-User-Roles": "admin"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolutionAction"] == "IGNORED"
    assert body["resolvedAt"] is not None


async def test_resolve_ignored_writes_reject_feedback(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """处置动作要落进学习闭环，且 IGNORED 记成 REJECT（= 系统误报）。

    这条映射是机制 3 准确率的**唯一**数据来源：把 IGNORED 记成 CONFIRM，
    误报率就永远是 0，机制 3 会「看起来一直很准」。
    """
    from app.domain.wiki_learning_models import LearningFeedback

    conflict = await _oneConflict(client, dbSession)

    await client.post(
        f"{_CONFLICTS}/{conflict['id']}/resolve", json={"action": "IGNORED"}
    )

    rows = (
        await dbSession.execute(
            select(LearningFeedback).where(LearningFeedback.mechanism == "CONFLICT")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].entity_type == "CONFLICT"
    assert rows[0].entity_id == str(conflict["id"])
    assert rows[0].user_action == "REJECT"
    assert rows[0].system_output["conflictType"] == "STALENESS"


async def test_resolve_twice_conflicts_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """重复处置同一条冲突 → 409（不做「静默改写上一次结论」）。"""
    conflict = await _oneConflict(client, dbSession)
    first = await client.post(
        f"{_CONFLICTS}/{conflict['id']}/resolve", json={"action": "RESOLVED"}
    )
    assert first.status_code == 200

    second = await client.post(
        f"{_CONFLICTS}/{conflict['id']}/resolve", json={"action": "IGNORED"}
    )

    assert second.status_code == 409


async def test_resolve_invalid_action_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    conflict = await _oneConflict(client, dbSession)

    resp = await client.post(
        f"{_CONFLICTS}/{conflict['id']}/resolve", json={"action": "NOT_AN_ACTION"}
    )

    assert resp.status_code == 422


async def test_resolve_missing_conflict_404(client: AsyncClient) -> None:
    resp = await client.post(
        f"{_CONFLICTS}/99999999/resolve", json={"action": "RESOLVED"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def test_list_conflicts_filters_by_status(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    conflict = await _oneConflict(client, dbSession)
    await client.post(
        f"{_CONFLICTS}/{conflict['id']}/resolve", json={"action": "RESOLVED"}
    )

    openRows = await client.get(f"{_CONFLICTS}?status=OPEN")
    doneRows = await client.get(f"{_CONFLICTS}?status=RESOLVED")

    assert openRows.json()["total"] == 0
    assert doneRows.json()["total"] == 1


async def test_list_page_conflicts(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    conflict = await _oneConflict(client, dbSession)
    pageId = conflict["pageIds"][0]

    resp = await client.get(f"{_PAGES}/{pageId}/conflicts")

    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


# ---------------------------------------------------------------------------
# 边界与授权
# ---------------------------------------------------------------------------


async def test_detect_missing_page_404(client: AsyncClient) -> None:
    resp = await client.post(
        f"{_PAGES}/PAGE-DOES-NOT-EXIST/conflicts/detect", json={"modelId": None}
    )
    assert resp.status_code == 404


async def test_conflict_routes_require_auth(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉 stub auth（模拟生产）→ 未认证的检测/处置必须 403。"""
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")

    assert (
        await client.post(f"{_PAGES}/ANY/conflicts/detect", json={})
    ).status_code == 403
    assert (await client.get(_CONFLICTS)).status_code == 403
    assert (
        await client.post(f"{_CONFLICTS}/1/resolve", json={"action": "RESOLVED"})
    ).status_code == 403
