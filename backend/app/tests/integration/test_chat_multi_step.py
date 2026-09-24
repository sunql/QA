"""多步 NL2SQL 集成测试（真实 PG + 完整 API 链路）。

验证单步优先策略 + 多步执行的 HTTP 契约与数据落库：
- 明确要求分步 → 直接多步（intent=multi_step + steps 数组）
- 无显式分步的对比类问题 → 先单步，成功则不拆步（intent=query，无 steps）
- 单步 SQL 执行失败 → 回退多步拆解
- 流式：step_plan / step_result 事件序列
- 落库：SessionMessage / SessionQueryState / SessionTokenUsage

LLM / 业务库 adapter 为外部依赖，注入 test double；数据层（消息/状态/用量）全真实 PG。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.domain.models import LlmConfig, SessionMessage, SessionQueryState, SessionTokenUsage
from app.services.stream_events import (
    EVENT_CLASS_RECALL,
    EVENT_DONE,
    EVENT_META,
    EVENT_MULTI_STEP_PLAN,
    EVENT_STEP_PLAN,
    EVENT_STEP_RESULT,
    EVENT_TOKEN,
)
from app.tests.integration.test_chat_api import _RouterFor, _StubEmbeddingService, _seed

ROWS = [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]

# 拆步 LLM 返回的多步计划（2 数据步 + 1 汇总步）
_MULTI_STEP_PLAN_JSON = (
    '{"isMultiStep": true, "steps": ['
    '{"description": "2024 年销售额", "subQuestion": "2024年的销售额是多少"}, '
    '{"description": "2025 年销售额", "subQuestion": "2025年的销售额是多少"}'
    '], "aggregationHint": "对比两年销售额给出趋势"}'
)


class _MultiStepLlm:
    """按 system prompt 路由回复：拆步 / 汇总 / 计划 / SQL / 图表 / 回答。"""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> object:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        user = messages[1].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "查询拆分器" in system:
            _Resp.content = _MULTI_STEP_PLAN_JSON
        elif "企业数据分析助手" in system:
            _Resp.content = "2025 年销售额较 2024 年增长 25%。"
        elif "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}'
        elif "解析为查询计划" in system:
            _Resp.content = '{"target":"销售额","selectedClasses":["PRECEIPT"],"selectedProperties":["NAME","QTY"]}'
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT GROUP BY NAME\n```"
            )
        else:
            _Resp.content = "查询完成。"
        return _Resp()


class _OkAdapter:
    """记录每次执行的 SQL，固定返回 ROWS。"""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        return ROWS


class _FlakyThenOkAdapter:
    """对含指定子串的 SQL 前 fail_count 次抛错，之后成功（模拟单步 + 重试均失败）。

    用子串而非调用序号判定：值域采样（SELECT DISTINCT ...）也走同一 adapter，
    按序号会让采样误占失败配额，导致重试意外成功。
    """

    def __init__(self, fail_substring: str, fail_count: int) -> None:
        self.fail_substring = fail_substring
        self.fail_count = fail_count
        self.failed = 0
        self.executed: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        if self.fail_substring in sql and self.failed < self.fail_count:
            self.failed += 1
            raise RuntimeError("ORA-00942: 表或视图不存在")
        return ROWS


def _data_queries(adapter: _OkAdapter | _FlakyThenOkAdapter) -> list[str]:
    """过滤出数据查询 SQL（含 SUM(QTY)），排除值域采样（SELECT DISTINCT ...）。"""
    return [s for s in adapter.executed if "SUM(QTY)" in s]


def _install(monkeypatch, config: LlmConfig, llm: _MultiStepLlm, adapter) -> None:
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: llm)
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: adapter)
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


def _payload(question: str, datasourceId: int) -> dict:
    return {"sessionId": "s1", "question": question, "datasourceId": datasourceId}


class TestMultiStepChatApi:
    async def test_explicit_step_request_executes_multi_step(self, client, dbSession, monkeypatch) -> None:
        """明确要求分步 → 直接多步：intent=multi_step + steps 数组 + 落库。"""
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        assert body["steps"] is not None
        assert len(body["steps"]) == 2  # 2 个数据步骤（汇总步骤不进 steps）
        assert body["steps"][0]["stepIndex"] == 0
        assert body["steps"][0]["sql"] is not None
        assert body["steps"][1]["stepIndex"] == 1
        assert "增长" in body["answer"]
        # 两个数据步骤各执行一次 SQL（另有 1 次值域采样，不计入）
        assert len(_data_queries(adapter)) == 2
        # 拆步 + 2×计划/SQL + 汇总 均已调用 LLM
        assert any("查询拆分器" in m[0][1] for m in llm.calls)

        # 落库：消息 + 查询状态 + 用量（2×nl2sql + 1×answer）
        msgs = list((await dbSession.execute(select(SessionMessage).order_by(SessionMessage.id))).scalars().all())
        assert [m.role for m in msgs] == ["user", "assistant"]
        state = await dbSession.execute(select(SessionQueryState))
        state_rows = list(state.scalars().all())
        assert len(state_rows) == 1
        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        # 拆步判定(step_plan) + 2×计划/SQL(nl2sql) + 汇总(answer) 均计量
        assert sorted(r.purpose for r in usages) == ["answer", "nl2sql", "nl2sql", "step_plan"]

    @pytest.mark.parametrize(
        "question",
        [
            "先查 2024 年的销售额，再查 2025 年的销售额，对比趋势",
            "先查 2024 年的销售额，然后查 2025 年的销售额，对比趋势",
        ],
    )
    async def test_sequential_instruction_triggers_multi_step(
        self, client, dbSession, monkeypatch, question
    ) -> None:
        """顺序指令（先…再/然后…，无「分步」关键词）→ 直接多步，不依赖单步失败回退。"""
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()  # 单步可成功：若未触发多步，会走单步并 intent=query
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat",
            json=_payload(question, ds.id),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        assert body["steps"] is not None
        assert len(body["steps"]) == 2
        assert len(_data_queries(adapter)) == 2  # 2 个数据步骤各执行一次
        assert any("查询拆分器" in m[0][1] for m in llm.calls)

    async def test_single_step_success_does_not_decompose(self, client, dbSession, monkeypatch) -> None:
        """对比类问题（无显式分步）→ 先单步，成功则不拆步（不调用拆步 LLM）。

        2026-08-16：单步也填 steps 字段（前端 MultiStepPlanCard 始终渲染 1 步）。
        """
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("对比 2024 和 2025 年的销售额", ds.id),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "query"  # 非 multi_step
        assert body["steps"] is not None
        assert len(body["steps"]) == 1  # 单步 = 1 元素
        assert body["steps"][0]["sql"] is not None
        assert body["steps"][0]["error"] is None
        assert len(_data_queries(adapter)) == 1  # 仅一次单步查询
        # 拆步 LLM 从未被调用（单步优先，成功即止）
        assert not any("查询拆分器" in m[0][1] for m in llm.calls)

    async def test_single_step_failure_falls_back_to_multi_step(self, client, dbSession, monkeypatch) -> None:
        """单步 SQL 执行失败（含重试）→ 回退多步拆解并成功返回。"""
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        # 单步原 SQL + 回灌重试共 2 次均失败，之后多步各子查询成功
        adapter = _FlakyThenOkAdapter(fail_substring="SUM(QTY)", fail_count=2)
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("对比 2024 和 2025 年的销售额", ds.id),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        assert body["steps"] is not None
        assert len(body["steps"]) == 2
        # 单步原 SQL + 重试均失败，回退多步后 2 个子查询成功
        assert adapter.failed == 2
        assert len(_data_queries(adapter)) == 4  # 2 次单步失败 + 2 次多步成功
        assert any("查询拆分器" in m[0][1] for m in llm.calls)

    async def test_explicit_step_marker_skips_llm_decomposition(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """2026-08-16 修复：用户用「第X步」标号 → 规则快路径，零 step_plan LLM 消耗。

        LLM 拆步对"对比 + 分析趋势"字样倾向返回 isMultiStep=false，会静默吞掉多步。
        本测试用「第X步」标号验证：直接走规则路径，未调拆步 LLM，无 step_plan 用量。
        """
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat",
            json=_payload(
                "第一步查 2024 年各供应商采购金额，第二步查 2025 年同期，第三步对比趋势",
                ds.id,
            ),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        assert body["steps"] is not None
        assert len(body["steps"]) == 3  # 3 数据步（汇总步不进 steps）
        # 规则路径：未调拆步 LLM
        assert not any("查询拆分器" in m[0][1] for m in llm.calls)
        # 用量：3×nl2sql + 1×answer + 1×step_plan(0 token)，与 LLM 拆步路径同 purpose 集合
        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        assert sorted(r.purpose for r in usages) == ["answer", "nl2sql", "nl2sql", "nl2sql", "step_plan"]
        # 规则路径的 step_plan 用量为 0（无 LLM 调用），便于按 purpose 区分规则/LLM 拆步
        step_plan_rows = [u for u in usages if u.purpose == "step_plan"]
        assert len(step_plan_rows) == 1
        assert step_plan_rows[0].prompt_tokens == 0
        assert step_plan_rows[0].completion_tokens == 0

    async def test_ordinal_connector_anchor_not_dropped(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """2026-09-09 回归：'先找出Top3供应商，然后…三种物料，最后分析'。

        序数承接词规则路径此前把首个连接词之前的锚点子句（"先找出公司上半年供货量
        最大的三个供应商"）整段丢弃，第二步引用的"这三个供应商"成为悬空锚点 → SQL
        编造占位符/重查全量。修复后首段补为第一数据步，必须保留进 steps。
        """
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat",
            json=_payload(
                "先找出公司上半年供货量最大的三个供应商，"
                "然后分别看这三个供应商供货量最大的三种物料分别是什么，"
                "最后分析供货的情况",
                ds.id,
            ),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        assert body["steps"] is not None
        # 首段锚点不再被丢弃 → 3 个数据步骤（此前只剩 2 步且首段缺失）
        assert len(body["steps"]) == 3
        assert body["steps"][0]["stepIndex"] == 0
        assert "供货量最大的三个供应商" in body["steps"][0]["subQuestion"]
        assert body["steps"][0]["sql"] is not None
        assert body["steps"][0]["error"] is None
        assert body["steps"][1]["subQuestion"].startswith("分别看这三个供应商")
        assert body["steps"][2]["subQuestion"].startswith("分析供货的情况")
        # 序数承接词规则路径命中 → 未调拆步 LLM
        assert not any("查询拆分器" in m[0][1] for m in llm.calls)
        # 3 个数据步骤各执行一次数据 SQL
        assert len(_data_queries(adapter)) == 3
        # 用量：3×nl2sql + 1×answer + 1×step_plan(0 token)，与「第X步」规则路径同构
        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        assert sorted(r.purpose for r in usages) == [
            "answer", "nl2sql", "nl2sql", "nl2sql", "step_plan",
        ]

    async def test_single_step_renders_execution_plan(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """2026-08-16：单步查询也返回 steps 字段，前端 MultiStepPlanCard 始终渲染 1 步。"""
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("查询 2025 年各供应商采购金额", ds.id),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "query"
        assert body["steps"] is not None
        assert len(body["steps"]) == 1
        assert body["steps"][0]["sql"] is not None
        assert body["steps"][0]["error"] is None


class TestMultiStepChatStreamApi:
    async def test_stream_explicit_step_emits_step_events(self, client, dbSession, monkeypatch) -> None:
        """流式：明确分步 → meta → step_plan/step_result ×2 → token(汇总) → done。"""
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")

        frames: list[tuple[str, dict]] = []
        for block in resp.text.split("\n\n"):
            if not block.strip():
                continue
            event: str | None = None
            data: dict = {}
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event = line[len("event: "):]
                elif line.startswith("data: "):
                    data = json.loads(line[len("data: "):])
            frames.append((event or "", data))

        events = [e for e, _ in frames]
        # 序列：meta → class_recall → multi_step_plan(完整计划) → step_plan/step_result ×2 → step_plan(汇总) → token → done
        assert events[0] == EVENT_META
        assert events[1] == EVENT_CLASS_RECALL
        assert events[2] == EVENT_MULTI_STEP_PLAN
        assert events[3] == EVENT_STEP_PLAN
        assert events[4] == EVENT_STEP_RESULT
        assert events[5] == EVENT_STEP_PLAN
        assert events[6] == EVENT_STEP_RESULT
        assert events[7] == EVENT_STEP_PLAN  # 汇总步骤开始前的 step_plan
        assert events[8] == EVENT_TOKEN
        assert events[-1] == EVENT_DONE
        # multi_step_plan 事件携带完整计划概览（含 aggregationOnly 标记）
        overview = frames[2][1]
        assert len(overview["steps"]) == 3
        assert [s["stepIndex"] for s in overview["steps"]] == [0, 1, 2]
        assert [s["aggregationOnly"] for s in overview["steps"]] == [False, False, True]
        # step_result 事件携带子步骤 SQL
        assert frames[4][1]["sql"] is not None
        # 汇总 step_plan 的 stepIndex 与聚合步一致
        assert frames[7][1]["stepIndex"] == 2
        # done 事件携带 steps 数组
        done = frames[-1][1]
        assert len(done["steps"]) == 2

    async def test_stream_multi_step_done_carries_suggested_agent(self, client, dbSession, monkeypatch) -> None:
        """G4 审查 MEDIUM 修复回归：多步路径 done 帧携带中置信建议卡片。

        中置信语义路由（「表现」单关键词 → SUPPLIER_360_AGENT 0.4）+ 显式分步
        请求同时命中：意图为 query（非 AGENT_RUN），走 _streamMultiStep，
        其 done 帧必须随 suggestedAgent 透传，否则前端默认 streaming UI 下
        建议卡片在多步场景永不渲染。
        """
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_payload("请分步查询供应商 100001 近两年的表现并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        frames: list[tuple[str, dict]] = []
        for block in resp.text.split("\n\n"):
            if not block.strip():
                continue
            event: str | None = None
            data: dict = {}
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event = line[len("event: "):]
                elif line.startswith("data: "):
                    data = json.loads(line[len("data: "):])
            frames.append((event or "", data))

        events = [e for e, _ in frames]
        assert events[-1] == EVENT_DONE
        done = frames[-1][1]
        # 多步完成帧透传中置信建议卡片（camelCase 别名）
        suggestion = done.get("suggestedAgent")
        assert suggestion is not None
        assert suggestion["recommendedAgentCode"] == "SUPPLIER_360_AGENT"
        assert suggestion["confidence"] == 0.4
