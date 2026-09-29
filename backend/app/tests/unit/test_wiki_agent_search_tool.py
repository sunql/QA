"""wiki_search Agent 工具的语义优先 / 关键词降级行为（feat-wiki-semantic-search）。

WikiVectorService 整体 mock；关键词降级路径 mock WikiPageService.searchPages。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.agent_tools_wiki as atw
from app.services.agent_tools_wiki import _wikiSearchHandler

_SEM_HIT = {
    "pageId": "PAGE-SEM",
    "title": "准入规则",
    "status": "EFFECTIVE",
    "dimension": "RULE",
    "chunkText": "注册资本一千万",
    "chunkSequence": 0,
    "distance": 0.5,
    "score": 2 / 3,
}


def _ctx() -> MagicMock:
    return MagicMock()


class TestWikiSearchSemanticFirst:
    @pytest.mark.asyncio
    async def test_semantic_path_dedupes_by_page_and_reports_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一条目多 chunk 命中去重取最高分；data.mode = semantic。"""

        async def fakeSearch(self, session, query, *, dimension=None, topK=10):
            dup = dict(_SEM_HIT, score=0.5)  # 同页低分 chunk
            return [dict(_SEM_HIT), dup]

        monkeypatch.setattr(
            "app.services.wiki_vector_service.WikiVectorService.searchSemantic",
            fakeSearch,
        )

        result = await _wikiSearchHandler(MagicMock(), {"query": "门槛"}, _ctx())

        assert result.data["mode"] == "semantic"
        assert result.data["total"] == 1
        assert result.data["items"][0]["pageId"] == "PAGE-SEM"
        assert result.data["items"][0]["score"] == pytest.approx(2 / 3, rel=1e-3)
        assert "语义检索" in result.answer

    @pytest.mark.asyncio
    async def test_vector_error_falls_back_to_keyword(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Milvus/embedding 故障 → 降级 PG 关键词检索，mode = keyword。"""

        async def boom(self, session, query, *, dimension=None, topK=10):
            raise Exception("embedding provider down")

        monkeypatch.setattr(
            "app.services.wiki_vector_service.WikiVectorService.searchSemantic",
            boom,
        )

        page = MagicMock()
        page.page_id = "PAGE-KW"
        page.title = "准入规则"
        page.dimension = "RULE"
        page.status = "EFFECTIVE"
        page.structure_stage = "MARKDOWN"
        page.content = "注册资本一千万"

        with patch.object(
            atw.WikiPageService, "searchPages", new=AsyncMock(return_value=([page], 1))
        ):
            result = await _wikiSearchHandler(
                MagicMock(), {"query": "注册资本"}, _ctx()
            )

        assert result.data["mode"] == "keyword"
        assert result.data["total"] == 1
        assert result.data["items"][0]["pageId"] == "PAGE-KW"

    @pytest.mark.asyncio
    async def test_semantic_empty_hits_do_not_trigger_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """语义成功但零命中 ≠ 基础设施故障：如实报 0，不降级重查。"""
        called = {"keyword": False}

        async def fakeSearch(self, session, query, *, dimension=None, topK=10):
            return []

        async def noKeyword(session, query, limit=5):
            called["keyword"] = True
            return [], 0

        monkeypatch.setattr(
            "app.services.wiki_vector_service.WikiVectorService.searchSemantic",
            fakeSearch,
        )
        with patch.object(atw.WikiPageService, "searchPages", new=noKeyword):
            result = await _wikiSearchHandler(MagicMock(), {"query": "不存在"}, _ctx())

        assert result.data["mode"] == "semantic"
        assert result.data["total"] == 0
        assert called["keyword"] is False
