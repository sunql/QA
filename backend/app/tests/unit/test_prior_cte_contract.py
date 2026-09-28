"""prior_cte 契约测试（M8）。

契约（本批钉死）：`prior_cte` 的唯一合法形态是 **WITH-less 片段**
`cte_alias AS (cte_body)`，由 `nl2sql_service.generateSql` 自行补上唯一的
前导 `WITH`。任何带前导 `WITH` 的入参必须**立即拒绝**。

历史缺陷：两边各拼一次 WITH ⇒ `WITH WITH ...`；`_assert_read_only` 只看首个
token（`WITH` 在白名单）故放行，直到库侧才报语法错，又被上层宽
`except Exception` 吞成 `success=False` —— 无栈、无原因、难定位。

覆盖：
- render_prior_cte 产出 WITH-less 片段（与 generateSql 的拼接口径一致）
- generateSql 拒绝带前导 WITH 的 prior_cte（给可操作消息）
- 拼接后的 SQL 恰好一个前导 WITH（`WITH WITH` 不可构造）
- WITH-less 片段必须能过安全校验（其首 token 是标识符而非 SELECT/WITH）
- CTE body 内的写操作仍被拦（改契约不得放宽安全）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from app.domain.chained_step_plan import ChainedStep, render_prior_cte
from app.domain.exceptions import Nl2SqlError
from app.domain.models import OntologyClass
from app.services.nl2sql_service import Nl2SqlService

# ---------------------------------------------------------------------------
# 本文件是纯函数 / 假 LLM 测试，不需要 DB。
# 覆盖 conftest 的 autouse DB fixtures —— 否则每个用例都会 TRUNCATE 整个测试库
# （seedEngine 里 _truncateAll），既慢又会抹掉集成测试依赖的迁移种子数据。
# ---------------------------------------------------------------------------

@pytest.fixture()
def dbSession() -> Any:
    """覆盖 conftest autouse dbSession —— 本文件不用 DB。"""
    return None


@pytest_asyncio.fixture()
async def seedEngine() -> Any:
    """覆盖 conftest autouse seedEngine —— 不建引擎、不 truncate。"""
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry() -> Any:
    """覆盖 conftest autouse warmBusinessObjectRegistry —— 无需 DB。"""
    yield


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 10
        self.completionTokens = 5


class _FakeLlm:
    """按顺序弹出预置回复的假客户端。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append([(m.role, m.content) for m in messages])
        return _Resp(self._responses.pop(0))


def _llmConfig() -> SimpleNamespace:
    return SimpleNamespace(model_name="test-model", temperature=0.0)


def _buildClass(name: str, table: str) -> OntologyClass:
    return OntologyClass(class_name=name, class_alias=None, source_table=table, properties=[])


def _steps() -> tuple[ChainedStep, ...]:
    return (
        ChainedStep("ratio", 0, "per-supplier ratio", "SELECT 1 AS total", (), "ratio_cte"),
        ChainedStep("region", 1, "region aggregation", "SELECT 2 AS rate", ("ratio",), "region_cte"),
    )


class TestRenderPriorCteContract:
    """render_prior_cte 产出 WITH-less 片段（与 f"WITH {prior_cte}" 口径一致）。"""

    def test_returns_with_less_fragment(self) -> None:
        rendered = render_prior_cte(_steps(), 2)
        assert rendered == "ratio_cte AS (SELECT 1 AS total), region_cte AS (SELECT 2 AS rate)"
        assert not rendered.upper().startswith("WITH"), (
            f"render_prior_cte 不得自带 WITH —— generateSql 会再拼一次（M8）: {rendered!r}"
        )

    def test_returns_empty_when_no_prior_steps(self) -> None:
        assert render_prior_cte(_steps(), 0) == ""

    def test_returns_first_step_only_for_index_one(self) -> None:
        rendered = render_prior_cte(_steps(), 1)
        assert rendered == "ratio_cte AS (SELECT 1 AS total)"
        assert "region_cte" not in rendered


class TestGenerateSqlPriorCteContract:
    """generateSql 的 prior_cte 入参契约。"""

    async def test_rejects_prior_cte_with_leading_with(self) -> None:
        """带前导 WITH 的入参必须立即拒绝（否则拼成 WITH WITH）。"""
        fake = _FakeLlm(["```sql\nSELECT 1 AS val\n```"])
        service = Nl2SqlService()
        with pytest.raises(Nl2SqlError) as excInfo:
            await service.generateSql(
                "问题",
                [_buildClass("PRECEIPT", "PRECEIPT")],
                fake,
                _llmConfig(),
                maxRetries=0,
                prior_cte="WITH ratio_cte AS (SELECT 1 AS total)",
            )
        message = str(excInfo.value) + str(getattr(excInfo.value, "detail", ""))
        assert "prior_cte" in message
        # 可操作：告诉调用方该传什么形态，而不是只说「未通过校验」
        assert "WITH" in message
        # 拒绝发生在调 LLM 之前
        assert fake.calls == []

    async def test_accepts_with_less_prior_cte_and_splices_one_with(self) -> None:
        """WITH-less 片段必须能过校验，且拼接后恰好一个前导 WITH。"""
        fake = _FakeLlm(["```sql\nSELECT 1 AS val\n```"])
        service = Nl2SqlService()
        priorCte = render_prior_cte(_steps(), 2)
        result = await service.generateSql(
            "问题",
            [_buildClass("PRECEIPT", "PRECEIPT")],
            fake,
            _llmConfig(),
            maxRetries=0,
            prior_cte=priorCte,
        )
        upper = result.sql.upper()
        assert upper.startswith("WITH "), f"拼接后应以唯一 WITH 开头: {result.sql!r}"
        assert "WITH WITH" not in upper, f"WITH WITH 不可构造: {result.sql!r}"
        assert upper.count("WITH") == 1, f"前导 WITH 只能有一个: {result.sql!r}"
        assert "ratio_cte AS (SELECT 1 AS total)" in result.sql
        assert "region_cte AS (SELECT 2 AS rate)" in result.sql

    async def test_with_less_prior_cte_reaches_system_prompt(self) -> None:
        """WITH-less 片段仍注入 system prompt（能力保留，只改形态）。"""
        fake = _FakeLlm(["```sql\nSELECT 1 AS val\n```"])
        service = Nl2SqlService()
        priorCte = render_prior_cte(_steps(), 1)
        await service.generateSql(
            "问题",
            [_buildClass("PRECEIPT", "PRECEIPT")],
            fake,
            _llmConfig(),
            maxRetries=0,
            prior_cte=priorCte,
        )
        systemPrompt = fake.calls[0][0][1]
        assert "<prior_cte>" in systemPrompt
        assert "ratio_cte AS (SELECT 1 AS total)" in systemPrompt

    async def test_still_rejects_write_inside_prior_cte_body(self) -> None:
        """安全不得因改契约而放宽：CTE body 内的写操作仍被拦。

        断言**拒绝原因**（而非仅断言抛错）：否则「把所有入参都拒掉」也能让本用例通过。
        """
        fake = _FakeLlm(["```sql\nSELECT 1 AS val\n```"])
        service = Nl2SqlService()
        with pytest.raises(Nl2SqlError) as excInfo:
            await service.generateSql(
                "问题",
                [_buildClass("PRECEIPT", "PRECEIPT")],
                fake,
                _llmConfig(),
                maxRetries=0,
                prior_cte="x_cte AS (DELETE FROM T)",
            )
        assert "DELETE" in str(getattr(excInfo.value, "detail", "")), (
            "应给出被拒的具体操作（M2 拒绝原因回注口径）"
        )
        assert fake.calls == []

    async def test_still_rejects_multi_statement_prior_cte(self) -> None:
        """拼接形态下多语句注入仍被拦（拒绝原因是多语句，非「首 token 非 SELECT」）。"""
        fake = _FakeLlm(["```sql\nSELECT 1 AS val\n```"])
        service = Nl2SqlService()
        with pytest.raises(Nl2SqlError) as excInfo:
            await service.generateSql(
                "问题",
                [_buildClass("PRECEIPT", "PRECEIPT")],
                fake,
                _llmConfig(),
                maxRetries=0,
                prior_cte="x_cte AS (SELECT 1); DROP TABLE T",
            )
        assert "多语句" in str(getattr(excInfo.value, "detail", ""))
        assert fake.calls == []

    async def test_leading_with_rejected_in_all_spellings(self) -> None:
        """前导 WITH 的大小写 / 换行变体都要拦住（不能用 `[:5] == 'WITH '` 这种近似判定）。"""
        service = Nl2SqlService()
        for raw in ("WITH x_cte AS (SELECT 1)", "with x_cte AS (SELECT 1)", "WITH\nx_cte AS (SELECT 1)"):
            fake = _FakeLlm(["```sql\nSELECT 1 AS val\n```"])
            with pytest.raises(Nl2SqlError):
                await service.generateSql(
                    "问题",
                    [_buildClass("PRECEIPT", "PRECEIPT")],
                    fake,
                    _llmConfig(),
                    maxRetries=0,
                    prior_cte=raw,
                )
            assert fake.calls == [], f"{raw!r} 应在调 LLM 前被拒"
