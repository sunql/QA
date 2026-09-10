"""L3 Chained Steps 执行引擎集成测试（Task 3.3）。

使用真实 PostgreSQL（services/conftest.py）验证：
- test_chained_steps_execute_sequentially    ：3 步链按序执行，前步结果作为后续 prior_cte
- test_chained_steps_isolate_failures        ：中间一步失败不阻断后续步
- test_chained_steps_max_5                   ：超过 5 步抛 ValueError

强制规则：Harness/rules/测试规范.md，真实 PG。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.chained_step_plan import ChainedStep, StepResult as ChainedStepResult
from app.domain.schemas import ChatRequest
from app.services.chat_service import ChatService


class FakeLlmClient:
    """假 LLM 客户端：返回预置 SQL 响应。"""

    def __init__(self, sql_text: str) -> None:
        self._sql_text = sql_text

    async def complete(self, messages, **kwargs):
        class Resp:
            content = self._sql_text
            promptTokens = 10
            completionTokens = 5
        return Resp()


def _make_fake_ds() -> MagicMock:
    """构造完全受控的 fake DataSource。"""
    ds = MagicMock()
    ds.id = 999
    ds.type = "postgresql"
    ds.oracle_version = None
    ds.username = "test_user"
    ds.password = "test_pw"
    ds.host = "localhost"
    ds.port = 5432
    ds.database = "test_db"
    return ds


def _make_fake_pc(ds: MagicMock) -> MagicMock:
    """构造完全受控的 fake _PipelineContext。"""
    fake_config = MagicMock()
    fake_config.id = 1
    fake_config.model_name = "test-model"
    fake_config.cost_per_1k_input = 0
    fake_config.cost_per_1k_output = 0
    fake_client = FakeLlmClient("SELECT 1 AS val")

    pc = MagicMock()
    pc.ds = ds
    pc.classes = []
    pc.configs = []
    pc.selected = fake_config
    pc.client = fake_client
    pc.contextPrompt = ""
    pc.fewShot = None
    pc.valueSamples = {}
    pc.driftWarning = None
    pc.dictionaryText = None
    pc.featureCatalogText = None
    pc.joins = []
    pc.forcedModel = False
    return pc


def _make_fake_dto(ds: MagicMock) -> MagicMock:
    """构造完全受控的 fake ChatRequest dto。"""
    dto = MagicMock()
    dto.datasourceId = ds.id
    dto.question = "test question"
    dto.sessionId = "test-session"
    dto.modelId = None
    dto.history = []
    dto.chartType = None
    return dto


def _make_service() -> ChatService:
    """构造带完全 mock 依赖的 ChatService（仅 _executeChainedSteps 内部逻辑真实）。"""
    service = object.__new__(ChatService)
    service._nl2sql = MagicMock()
    service._ontology = MagicMock()
    service._datasource = MagicMock()
    service._modelRouter = MagicMock()
    service._llmFactory = MagicMock()
    service._tokenUsage = MagicMock()
    service._embedding = MagicMock()
    service._schemaIntrospection = MagicMock()
    service._termDictionary = MagicMock()
    service._featureQueryService = None
    service._kpiMatcher = MagicMock()
    service._graphTraversal = MagicMock()
    service._agentRuntime = MagicMock()
    service._supplierNameResolver = MagicMock()
    service._affinityTurns = None
    return service


class TestExecuteChainedSteps:
    """_executeChainedSteps 执行引擎测试。"""

    @pytest.mark.asyncio()
    async def test_chained_steps_execute_sequentially(self, dbSession) -> None:
        """3 步链按序执行，前步结果作为后续 prior_cte。"""
        ds = _make_fake_ds()
        pc = _make_fake_pc(ds)
        dto = _make_fake_dto(ds)
        service = _make_service()

        # 记录 generateSql 调用参数
        gen_calls: list[dict] = []

        async def fake_generateSql(question, classes, llmClient, modelConfig, **kwargs):
            gen_calls.append({"question": question, "prior_cte": kwargs.get("prior_cte")})
            resp = MagicMock()
            resp.sql = f"SELECT {len(gen_calls)} AS val"
            resp.promptTokens = 10
            resp.completionTokens = 5
            return resp

        # 伪造 _runQuery（用 execute_read_only adapter）
        fake_adapter = MagicMock()
        fake_adapter.execute_read_only = AsyncMock(return_value=[{"val": 1}])
        service._runQuery = AsyncMock(return_value=[{"val": len(gen_calls)}])

        service._nl2sql.generateSql = fake_generateSql

        steps = (
            ChainedStep("ratio", 0, "per-supplier ratio", "supplier_id, SUM(amount) AS total", (), "ratio_cte"),
            ChainedStep("region", 1, "region aggregation", "AVG(total) AS region_rate", ("ratio",), "region_cte"),
            ChainedStep("country", 2, "country aggregation", "MAX(region_rate) AS country_rate", ("region",), "country_cte"),
        )

        results = await service._executeChainedSteps(steps, "test-user", dto, pc, dbSession)

        assert len(results) == 3
        assert all(r.success for r in results), f"All should succeed: {[(r.step_id, r.success, r.error) for r in results]}"
        # 验证 prior_cte 注入：step 0 无 prior_cte，step 1 有 step 0 的 CTE，step 2 有 step 0+1 的 CTE
        assert gen_calls[0]["prior_cte"] is None or gen_calls[0]["prior_cte"] == ""
        assert "ratio_cte" in gen_calls[1]["prior_cte"]
        assert "ratio_cte" in gen_calls[2]["prior_cte"]
        assert "region_cte" in gen_calls[2]["prior_cte"]

    @pytest.mark.asyncio()
    async def test_chained_steps_isolate_failures(self, dbSession) -> None:
        """中间一步失败不阻断后续步。"""
        ds = _make_fake_ds()
        pc = _make_fake_pc(ds)
        dto = _make_fake_dto(ds)
        service = _make_service()

        call_count = 0

        async def fake_generateSql(question, classes, llmClient, modelConfig, **kwargs):
            resp = MagicMock()
            resp.sql = "SELECT 1 AS val"
            resp.promptTokens = 10
            resp.completionTokens = 5
            return resp

        # Step 0 成功，Step 1 抛出异常，Step 2 成功
        async def fake_runQuery(pc, dto, sql, *, user_id=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("intentional SQL error for step 2")
            return [{"val": call_count}]

        service._nl2sql.generateSql = fake_generateSql
        service._runQuery = fake_runQuery

        steps = (
            ChainedStep("ok1", 0, "ok step 1", "1 AS val", (), "ok1_cte"),
            ChainedStep("bad", 1, "failing step", "INVALID SQL", (), "bad_cte"),
            ChainedStep("ok2", 2, "ok step 2", "2 AS val", (), "ok2_cte"),
        )

        results = await service._executeChainedSteps(steps, "test-user", dto, pc, dbSession)

        assert len(results) == 3
        assert results[0].success is True,    f"Step 0 should succeed: {results[0]}"
        assert results[1].success is False,   f"Step 1 should fail: {results[1]}"
        assert results[2].success is True,    f"Step 2 should succeed: {results[2]}"
        assert results[1].error is not None,  "Step 1 error should be recorded"
        assert "intentional SQL error" in results[1].error, f"Wrong error message: {results[1].error}"

    @pytest.mark.asyncio()
    async def test_chained_steps_max_5(self, dbSession) -> None:
        """超过 5 步抛 ValueError。"""
        ds = _make_fake_ds()
        pc = _make_fake_pc(ds)
        dto = _make_fake_dto(ds)
        service = _make_service()

        # 6 步应抛 ValueError
        steps = tuple(
            ChainedStep(f"s{i}", i, "...", "1 AS val", (), f"cte{i}")
            for i in range(6)
        )
        with pytest.raises(ValueError, match=r"<= 5"):
            await service._executeChainedSteps(steps, "test-user", dto, pc, dbSession)
