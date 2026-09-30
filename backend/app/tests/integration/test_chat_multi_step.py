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
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.domain.models import LlmConfig, SessionMessage, SessionQueryState, SessionTokenUsage
from app.services.messages_zh import MSG_MULTI_STEP_DEGRADE_FAILED, MSG_PLAN_TOO_MANY_STEPS
from app.services.stream_events import (
    EVENT_CHART,
    EVENT_CLASS_RECALL,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_META,
    EVENT_MULTI_STEP_PLAN,
    EVENT_SQL,
    EVENT_STEP_PLAN,
    EVENT_STEP_RESULT,
    EVENT_TOKEN,
)
from app.tests.integration.test_chat_api import _RouterFor, _StubEmbeddingService, _seed

ROWS = [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]

# 第二步 SQL 的专属失败标记（仅 C4 用例用）：让「某一 SQL 定向失败」可构造。
STEP2_FAIL_MARKER = "QTY_STEP2_FAIL"

# 拆步 LLM 返回的多步计划（2 数据步 + 1 汇总步）
_MULTI_STEP_PLAN_JSON = (
    '{"isMultiStep": true, "steps": ['
    '{"description": "2024 年销售额", "subQuestion": "2024年的销售额是多少"}, '
    '{"description": "2025 年销售额", "subQuestion": "2025年的销售额是多少"}'
    '], "aggregationHint": "对比两年销售额给出趋势"}'
)


class _MultiStepLlm:
    """按 system prompt 路由回复：拆步 / 汇总 / 计划 / SQL / 标签分类 / 回答。

    图表阶段**不再产出 option**（决策引擎定 kind、渲染器画图，LLM 只在规则歧义时
    给一个语义标签），故这里没有「图表 JSON」分支：分类调用落到最后的 else，
    回一句自然语言 —— 不是白名单标签，等价于「分类器答非所问」，用于走
    「保留规则原判」那条路径。
    """

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
        elif "解析为查询计划" in system:
            _Resp.content = '{"target":"销售额","selectedClasses":["PRECEIPT"],"selectedProperties":["NAME","QTY"]}'
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT GROUP BY NAME\n```"
            )
        else:
            _Resp.content = "查询完成。"
        return _Resp()


# 6 个数据步（>MAX_PLAN_DATA_STEPS=4）的拆步回复，用于超限拒收用例
_OVERSIZED_PLAN_JSON = (
    '{"isMultiStep": true, "steps": ['
    + ", ".join(
        f'{{"description": "维度{i}", "subQuestion": "维度{i}的金额是多少"}}'
        for i in range(6)
    )
    + '], "aggregationHint": "综合分析"}'
)


class _OversizedLlm(_MultiStepLlm):
    """拆步 LLM 返回 6 步计划（超出 4 步数据步上限），其余阶段与父类一致。"""

    async def complete(self, messages: list, **kwargs) -> object:
        resp = await super().complete(messages, **kwargs)
        if "查询拆分器" in messages[0].content:
            resp.content = _OVERSIZED_PLAN_JSON
        return resp


class _NoDecomposeLlm(_MultiStepLlm):
    """拆步 LLM 一律答「不需要多步」→ 单步失败后无多步回退，异常直接上抛。

    用于构造「单步硬失败」终局（`_detectMultiStep` 返回 None），这是唯一能让
    上抛路径的用量留痕被观测到的形态（见 TestSingleStepRetryMetering）。
    """

    async def complete(self, messages: list, **kwargs) -> object:
        resp = await super().complete(messages, **kwargs)
        if "查询拆分器" in messages[0].content:
            resp.content = '{"isMultiStep": false, "steps": []}'
        return resp


class _RetryGenFailsLlm(_MultiStepLlm):
    """单步首次 SQL 执行失败后，**回灌重试的那次生成**也失败（回复里没有 SQL 围栏）。

    真实语义：`Nl2SqlService.generateSql` 解析不出 SQL 时抛 `Nl2SqlError`，并在异常上
    携带本次调用的 token；`_runQueryWithRetry` 的重试生成分支此前把这些 token 整段
    丢弃（既没落账、也没随异常交回）。识别方式用回灌提示词里的固定句
    （见 `Nl2SqlService._buildUserPrompt` 的 executionError 分支）——比按调用序号判定
    稳，且不影响其它阶段（拆步/计划/汇总）与多步回退后的各步生成。
    """

    async def complete(self, messages: list, **kwargs) -> object:
        resp = await super().complete(messages, **kwargs)
        if "上一次生成的 SQL 在数据库执行时报错" in messages[1].content:
            resp.content = "抱歉，我无法修正这条 SQL。"  # 无 ```sql 围栏 → 解析失败
        return resp


class _PerStepLlm(_MultiStepLlm):
    """第二步（子问题含 2025）的 SQL 带专属标记，供 adapter 定向失败。

    按**子问题原文**而非「含 2025」判定：SQL 阶段 user prompt 里的 `<scope_hint>`
    是原始复合问题（"…2024 和 2025 年的销售额…"），宽匹配会把第一步的 SQL 也打上
    标记，于是第一步也失败，用例就构造不出「一步成功一步失败」。

    其余行为全同 `_MultiStepLlm`（含 calls 记录）。
    """

    async def complete(self, messages: list, **kwargs) -> object:
        resp = await super().complete(messages, **kwargs)
        isSqlStage = "生成 SQL 时必须" in messages[0].content
        isStep2 = "2025年的销售额是多少" in messages[1].content
        if isSqlStage and isStep2:
            resp.content = (
                f"```sql\nSELECT NAME, SUM({STEP2_FAIL_MARKER}) AS TOTAL_QTY "
                "FROM ZJTH.PRECEIPT GROUP BY NAME\n```"
            )
        return resp


def _parseFrames(resp) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析为 (event, data) 帧列表。"""
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
    return frames


@dataclass(frozen=True)
class _SqlCall:
    """一次 generateSql 调用的上下文参数（C4 断言用）。"""

    question: str
    priorState: str | None
    scopeQuestion: str | None
    executionError: str | None


def _spyGenerateSql(monkeypatch) -> list[_SqlCall]:
    """包装 Nl2SqlService.generateSql：记录调用参数后委托真实实现。

    在类上打补丁（而非实例）——monkeypatch 对实例打补丁时 teardown 会把原 bound
    method 落成实例属性，虽然行为等价但会残留。
    """
    from app.services.nl2sql_service import Nl2SqlService

    original = Nl2SqlService.generateSql
    calls: list[_SqlCall] = []

    async def _spy(self, question, classes, llmClient, modelConfig, **kwargs):
        calls.append(_SqlCall(
            question=question,
            priorState=kwargs.get("priorState"),
            scopeQuestion=kwargs.get("scopeQuestion"),
            executionError=kwargs.get("executionError"),
        ))
        return await original(self, question, classes, llmClient, modelConfig, **kwargs)

    monkeypatch.setattr(Nl2SqlService, "generateSql", _spy)
    return calls


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


class _DataQueryFailAdapter:
    """第 `fail_from_query` 个数据查询起全部失败（含回灌重试那一次）。

    按「数据查询序号」而非 SQL 内容判定：回灌重试会**重新生成**一条 SQL，内容随
    C4 是否修复而变（修复前退回原始复合问题，内容与第一步相同），内容匹配无法同时
    覆盖两种情形；序号判定与测试无关的 SQL 内容解耦。

    数据查询用 `SUM(QTY)` 识别（值域采样是 SELECT DISTINCT，不占序号）。
    """

    def __init__(
        self,
        *,
        fail_from_query: int,
        exc_factory: Callable[[], Exception] | None = None,
    ) -> None:
        self.fail_from_query = fail_from_query
        # 失败异常可注入：默认裸 RuntimeError；脱敏用例注入真实 SQLAlchemy 语句异常
        # （`str()` 会在驱动原因后追加 `[SQL: ...]` / `[parameters: ...]`）
        self.exc_factory = exc_factory or (lambda: RuntimeError("ORA-00942: 表或视图不存在"))
        self.data_queries = 0
        self.failed = 0
        self.executed: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        if "SUM(QTY)" in sql:
            self.data_queries += 1
            if self.data_queries >= self.fail_from_query:
                self.failed += 1
                raise self.exc_factory()
        return ROWS


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
        # 拆步判定(step_plan) + 2×计划/SQL(nl2sql) + 汇总(answer) + 全局过滤抽取
        # (multistep_global_filter) 均计量。全局过滤抽取是 B 层的独立 LLM 调用
        # （H1 后才把 token 如实落台账，此前写 0-token 假审计行），故它是第 5 行。
        # chart 那一行 = 第一步出图时的语义标签分类（两步都歧义，但一轮只允许
        # 问一次，故只有 1 行）——每步各出一张图是决策 3，标签调用必须落账。
        assert sorted(r.purpose for r in usages) == [
            "answer", "chart", "multistep_global_filter", "nl2sql", "nl2sql", "step_plan",
        ]
        # 抽取确实花了 token（不是 0/0 占位行）——H1 的验收点
        gf_rows = [u for u in usages if u.purpose == "multistep_global_filter"]
        assert gf_rows[0].prompt_tokens > 0
        assert gf_rows[0].completion_tokens > 0

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
        # 用量：3×nl2sql + 1×answer + 1×step_plan(0 token) + 1×multistep_global_filter
        # + 1×chart（首步出图的语义标签分类；三步只有一次，见 budget 用例），
        # 与 LLM 拆步路径同 purpose 集合（规则路径省掉的是拆步 LLM，全局过滤抽取照跑）
        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        assert sorted(r.purpose for r in usages) == [
            "answer", "chart", "multistep_global_filter",
            "nl2sql", "nl2sql", "nl2sql", "step_plan",
        ]
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
        # 用量：3×nl2sql + 1×answer + 1×step_plan(0 token) + 1×multistep_global_filter
        # + 1×chart（首步出图），与「第X步」规则路径同构
        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        assert sorted(r.purpose for r in usages) == [
            "answer", "chart", "multistep_global_filter",
            "nl2sql", "nl2sql", "nl2sql", "step_plan",
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


class TestOversizedPlanRejected:
    """A6：拆步超限 → 拒收 + 固定提示，**不执行任何数据步**。

    此前超限被 planner 静默截断到 4 步：用户拿到「12 问里的 4 问」却看不出少了
    什么。现改为 planner 如实上报 + 执行缝拒收，「宁可不答，不给残缺的答案」。
    """

    _QUESTION = "请分步查询华东销售下降的所有原因并逐项分析"

    async def test_oversized_plan_is_rejected_with_hint(self, client, dbSession, monkeypatch) -> None:
        """6 步计划 → 提示含真实步数与上限，steps 为**一张**拒收卡，一条 SQL 都没执行。"""
        config, ds = await _seed(dbSession)
        adapter = _OkAdapter()
        _install(monkeypatch, config, _OversizedLlm(), adapter)

        resp = await client.post("/api/v1/chat", json=_payload(self._QUESTION, ds.id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["answer"] == MSG_PLAN_TOO_MANY_STEPS.format(steps=6, limit=4)
        assert body["intent"] == "multi_step"
        # steps 必须给出**一张**卡，且与流式路径同型（流式下发同一份 step_result）。
        # 留空则前端 MultiStepPlanCard 不渲染（只在 steps 非空时挂载），用户看到的
        # 是一段没有归属的裸文字；而非流式「无法回答」分支（chat_service
        # _unanswerableResponse）正是为同一理由填了一张卡。两条路径一个形状。
        assert len(body["steps"]) == 1
        assert body["steps"][0]["description"] == "超出步数上限"
        assert body["steps"][0]["sql"] is None
        # 没有执行任何**数据步** SQL。注意不能断言 `adapter.executed == []`：
        # 值域采样（SELECT DISTINCT …）是流水线构造 _PipelineContext 时做的准备，
        # 先于多步决策，且由模块级 _VALUE_SAMPLE_CACHE 决定是否真落到 adapter——
        # 那样断言会随「本用例是不是进程里第一个碰 RECEIPT.NAME 的」而时绿时红。
        assert _data_queries(adapter) == []

    async def test_oversized_plan_burns_no_data_step_tokens(self, client, dbSession, monkeypatch) -> None:
        """拒收必须发生在数据步之前：台账里不得有 nl2sql / answer 行。

        这是与「截断后照常执行」的分水岭——退化成执行前 4 步的话，用户仍会拿到
        不完整答案，且白烧 4 次 SQL 生成 + 1 次汇总（核心约束 #3 的成本口径）。
        """
        config, ds = await _seed(dbSession)
        _install(monkeypatch, config, _OversizedLlm(), _OkAdapter())

        resp = await client.post("/api/v1/chat", json=_payload(self._QUESTION, ds.id))
        assert resp.status_code == 200, resp.text

        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        # 只该剩两次**前置**调用：拆步判定（产出计划）与全局约束抽取（在
        # chat_service 直接多步入口里先于 _resolveExplicitMultiStep 发生，
        # 故拒收时它已经花了钱）。数据步与汇总一律未发生。
        assert sorted(r.purpose for r in usages) == [
            "multistep_global_filter", "step_plan",
        ]

    async def test_rule_path_oversized_is_rejected_too(self, client, dbSession, monkeypatch) -> None:
        """规则快路径（「第X步」标号）同样受上限约束——它此前**完全无上限**。

        五个标号 → 5 个数据步 → 拒收。规则路径不经过拆步 LLM，超限此前无从拦截。
        """
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _OkAdapter()
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post("/api/v1/chat", json=_payload(
            "第一步查华东金额，第二步查华南金额，第三步查华北金额，"
            "第四步查西南金额，第五步查东北金额",
            ds.id,
        ))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["answer"] == MSG_PLAN_TOO_MANY_STEPS.format(steps=5, limit=4)
        assert [s["description"] for s in body["steps"]] == ["超出步数上限"]
        assert _data_queries(adapter) == []
        # 规则路径本就零拆步 LLM 消耗，拒收不改变这一点
        assert not any("查询拆分器" in m[0][1] for m in llm.calls)

    async def test_four_data_steps_at_limit_still_executes(self, client, dbSession, monkeypatch) -> None:
        """边界反向验证：恰好 4 个数据步（== 上限）必须放行。

        只测「坏的被拦」会让上限被写成 `>=` 也照样绿（守卫类断言要双向测）。
        """
        config, ds = await _seed(dbSession)
        adapter = _OkAdapter()
        _install(monkeypatch, config, _MultiStepLlm(), adapter)

        resp = await client.post("/api/v1/chat", json=_payload(
            "第一步查华东金额，第二步查华南金额，第三步查华北金额，第四步查西南金额",
            ds.id,
        ))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        assert len(body["steps"]) == 4
        assert len(_data_queries(adapter)) == 4

    async def test_streaming_oversized_yields_hint_without_data_steps(self, client, dbSession, monkeypatch) -> None:
        """流式同口径：只下发固定提示，不出现任何数据步事件。

        与非流式共用 `_rejectOversizedPlan`，否则两条路径必然漂移（本文件的
        TestNoAggregationStepDegrade / TestStepFailureIsolation 就是为此存在）。
        前端若收到 6 个 step_plan，会渲染出 6 张「待执行」卡片。
        """
        config, ds = await _seed(dbSession)
        adapter = _OkAdapter()
        _install(monkeypatch, config, _OversizedLlm(), adapter)

        resp = await client.post(
            "/api/v1/chat/stream", json=_payload(self._QUESTION, ds.id),
        )
        assert resp.status_code == 200, resp.text

        frames = _parseFrames(resp)
        events = [e for e, _ in frames]
        assert EVENT_ERROR not in events
        assert events[-1] == EVENT_DONE
        tokenText = "".join(
            str(d.get("content", "")) for e, d in frames if e == EVENT_TOKEN
        )
        assert tokenText == MSG_PLAN_TOO_MANY_STEPS.format(steps=6, limit=4)
        # 没有任何数据步被执行：不生成 SQL、不执行 SQL
        assert EVENT_SQL not in events
        assert _data_queries(adapter) == []
        # 计划概览只有 1 步（拒收说明），不是 6 步
        overview = [d for e, d in frames if e == EVENT_MULTI_STEP_PLAN]
        assert len(overview) == 1
        assert len(overview[0]["steps"]) == 1
        assert overview[0]["steps"][0]["description"] == "超出步数上限"
        # 概览下发过就必须收成终态，否则该步永远停在「待执行」
        stepResults = [d for e, d in frames if e == EVENT_STEP_RESULT]
        assert len(stepResults) == 1
        assert stepResults[0]["stepIndex"] == 0
        assert stepResults[0]["sql"] is None


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

        frames = _parseFrames(resp)

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

        frames = _parseFrames(resp)

        events = [e for e, _ in frames]
        assert events[-1] == EVENT_DONE
        done = frames[-1][1]
        # 多步完成帧透传中置信建议卡片（camelCase 别名）
        suggestion = done.get("suggestedAgent")
        assert suggestion is not None
        assert suggestion["recommendedAgentCode"] == "SUPPLIER_360_AGENT"
        assert suggestion["confidence"] == 0.4


class TestMultiStepStepCharts:
    """决策 3：**每个 step 各出一张图**，且分类调用一轮最多一次。

    服务端只发结构（`chartType` + 不含颜色的 `chartOption`），前端透传 + 套主题。
    每步的图来自该步**自己的** columns/data/plan —— 多步此前完全不出图
    （`chartType` 恒为 null），用户看到三个步骤只有表格。

    预算：每步都可能落进歧义分支（R12 分类比较 vs 占比），若 N 步就 N 次额外
    往返，token 与延迟都白花。故一轮只允许一次语义标签调用，其余步骤按规则原判
    出图（最坏情况是「不如意但画得出」，不会是空白图）。
    """

    # 语义标签分类器 prompt 的固定字样（`chart_label._SYSTEM_PROMPT` 的负面约束句
    # 「不输出图表类型名称，不输出任何 ECharts 配置或代码」）。**在 system 段**，
    # 故判 system 而不是 user —— 旧版「让 LLM 写 option」的 prompt 里该字样出现在
    # user 段，那条路径已删除，现在只有分类器会带这个字样的 system prompt。
    LABEL_PROMPT_MARKER = "图表类型"

    @classmethod
    def _labelCalls(cls, llm: _MultiStepLlm) -> int:
        return sum(1 for call in llm.calls if cls.LABEL_PROMPT_MARKER in call[0][1])

    async def test_each_step_carries_its_own_chart(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        _install(monkeypatch, config, llm, _OkAdapter())

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        steps = resp.json()["steps"]
        assert len(steps) == 2
        for step in steps:
            # 每步都是 1 维（NAME）+ 1 指标（QTY）、无 formula → R12 分类比较
            assert step["chartType"] == "bar"
            assert step["chartOption"]["series"][0]["type"] == "bar"
            assert step["chartOption"]["xAxis"]["data"] == ["A", "B"]

    async def test_failed_step_carries_no_chart(self, client, dbSession, monkeypatch) -> None:
        """失败的步骤没有数据可画 —— 不许发一个渲染不出来的 kind。"""
        config, ds = await _seed(dbSession)
        _install(monkeypatch, config, _MultiStepLlm(), _DataQueryFailAdapter(fail_from_query=2))

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        step2 = resp.json()["steps"][1]
        assert step2["sql"] is None  # 失败标记
        assert step2["chartType"] is None
        assert step2["chartOption"] is None

    async def test_classifier_is_called_at_most_once_per_turn(
        self, client, dbSession, monkeypatch
    ) -> None:
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        _install(monkeypatch, config, llm, _OkAdapter())

        await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )

        # 两步都歧义，但预算只够一次（第一步用掉）
        assert self._labelCalls(llm) == 1

    async def test_step_chart_tokens_are_metered(self, client, dbSession, monkeypatch) -> None:
        """分类那一次调用的 token 必须进总量与台账（核心约束 #3）。"""
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        _install(monkeypatch, config, llm, _OkAdapter())

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        chart_rows = [u for u in usages if u.purpose == "chart"]
        assert len(chart_rows) == 1
        # 假 LLM 每次回 10/5 —— 落了真实值，不是 0/0 占位行
        assert (chart_rows[0].prompt_tokens, chart_rows[0].completion_tokens) == (10, 5)
        # 图表 token 已计入响应用量（否则用户看到的消耗比实际花的少）
        assert resp.json()["tokensUsed"] > 10 + 5

    async def test_stream_step_result_event_carries_chart(
        self, client, dbSession, monkeypatch
    ) -> None:
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        _install(monkeypatch, config, llm, _OkAdapter())

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        stepResults = [payload for event, payload in _parseFrames(resp) if event == EVENT_STEP_RESULT]
        assert len(stepResults) == 2
        assert stepResults[0]["chartType"] == "bar"
        assert stepResults[0]["chartOption"]["series"][0]["type"] == "bar"
        # done 帧的 steps 数组同样带图（前端刷新/回放时用）
        donePayload = _parseFrames(resp)[-1][1]
        assert donePayload["steps"][0]["chartType"] == "bar"

    # ------------------------------------------------------------------
    # 最终报告也要有图（用户反馈）：图此前只活在计划卡的每个步骤里，
    # 最后那条汇总回答是纯文字 —— 而用户看的是回答，不是折叠着的计划。
    # 取「最后一个成功数据步骤」的图：它是整条链的终点，服务端本来就把它当作
    # 追问锚点（last_plan/last_sql/last_data），复用同一份，不另算一张。
    # ------------------------------------------------------------------

    async def test_final_answer_carries_last_step_chart(
        self, client, dbSession, monkeypatch
    ) -> None:
        config, ds = await _seed(dbSession)
        _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        body = resp.json()
        steps = body["steps"]
        assert len(steps) == 2
        # 顶层图就是最后一步那张（同一份，不是各算一份 —— 否则两处会漂移）
        assert body["chartType"] == steps[-1]["chartType"] == "bar"
        assert body["chartOption"] == steps[-1]["chartOption"]
        # 报告图要带数据：TABLE 类 kind 的前端渲染与 CSV 导出都读它
        assert len(body["data"]) == len(ROWS)

    async def test_final_answer_chart_skips_failed_last_step(
        self, client, dbSession, monkeypatch
    ) -> None:
        """最后一步失败 → 顶层图退到最后一个**成功**步骤，而不是消失或指向失败步。"""
        config, ds = await _seed(dbSession)
        _install(monkeypatch, config, _MultiStepLlm(), _DataQueryFailAdapter(fail_from_query=2))

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        body = resp.json()
        assert body["steps"][1]["sql"] is None  # 失败标记
        assert body["chartType"] == "bar"
        assert body["chartOption"] == body["steps"][0]["chartOption"]

    async def test_final_answer_has_no_chart_when_all_steps_fail(
        self, client, dbSession, monkeypatch
    ) -> None:
        """全部数据步骤失败 → 顶层不出图。空图比没图更糟（前端渲染门会画出空白）。"""
        config, ds = await _seed(dbSession)
        _install(monkeypatch, config, _MultiStepLlm(), _DataQueryFailAdapter(fail_from_query=1))

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        body = resp.json()
        assert all(step["sql"] is None for step in body["steps"])
        assert body["chartType"] is None
        assert body["chartOption"] is None

    async def test_stream_emits_one_chart_event_before_done(
        self, client, dbSession, monkeypatch
    ) -> None:
        """流式：最终回答的图走**一次** chart 事件，排在 done 之前。

        每步的图仍走各自的 step_result —— 这条守卫的就是「别每步都补发一次」。
        """
        config, ds = await _seed(dbSession)
        _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

        resp = await client.post(
            "/api/v1/chat/stream",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        frames = _parseFrames(resp)
        chartFrames = [i for i, (event, _) in enumerate(frames) if event == EVENT_CHART]
        assert len(chartFrames) == 1
        assert frames[-1][0] == EVENT_DONE
        assert chartFrames[0] < len(frames) - 1

        chart = frames[chartFrames[0]][1]
        assert chart["chartType"] == "bar"
        assert chart["chartOption"]["series"][0]["type"] == "bar"
        assert len(chart["data"]) == len(ROWS)


class TestNoAggregationStepDegrade:
    """防御分支：计划里没有汇总步（aggregation_only）时的降级收尾。

    两处 `MultiStepPlan` 构造点（step_query_planner.rule_based_split 与 LLM 拆步）
    目前**都**会补一个汇总步，故该分支在生产路径上不可达——它是防「计划被上层
    改坏 / 未来新增构造点漏补汇总步」的兜底。既有的兜底只返回文案、**不写状态**：
    assistant 消息缺失（历史出现悬空 user 轮），`last_question`/`last_sql` 停在
    上一轮 → 下一轮追问锚到更早的问题（静默答错）或退化成无锚点的单轮查询。
    两条路径（流式 / 非流式）此前实现还不对称，这里一并钉住。
    """

    @staticmethod
    def _planWithoutAggregation(question: str):
        """规则拆步的替身：两个数据步骤，**不补**汇总步。"""
        from app.domain.multi_step_plan import MultiStepPlan, StepPlan

        return MultiStepPlan(
            steps=(
                StepPlan(index=0, description="2024 年销售额", sub_question="2024年的销售额是多少"),
                StepPlan(index=1, description="2025 年销售额", sub_question="2025年的销售额是多少"),
            ),
            aggregation_hint="对比两年销售额",
            original_question=question,
        )

    async def test_non_stream_saves_state_and_reports_progress(
        self, client, dbSession, monkeypatch
    ) -> None:
        """非流式：数据步已成功 → 落 assistant 消息 + 保存查询状态。"""
        from app.services.step_query_planner import StepQueryPlanner

        config, ds = await _seed(dbSession)
        adapter = _OkAdapter()
        _install(monkeypatch, config, _MultiStepLlm(), adapter)
        monkeypatch.setattr(
            StepQueryPlanner, "rule_based_split", staticmethod(self._planWithoutAggregation)
        )

        question = "第一步，查 2024 年销售额；第二步，查 2025 年销售额"
        resp = await client.post("/api/v1/chat", json=_payload(question, ds.id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        # 两个数据步都真的执行了 SQL（不是「什么都没做成」）
        assert len(_data_queries(adapter)) == 2
        # 文案如实反映「数据步完成、汇总失败」，而不是笼统的「执行异常」
        assert "2/2" in body["answer"]

        # 落库：user + assistant 双写（此前 assistant 缺失 → 历史悬空）
        msgs = list(
            (await dbSession.execute(select(SessionMessage).order_by(SessionMessage.id)))
            .scalars()
            .all()
        )
        assert [m.role for m in msgs] == ["user", "assistant"]
        # 查询状态锚定本轮问题 + 最后一个成功数据步的 SQL（追问可继续级联）
        state = (
            await dbSession.execute(select(SessionQueryState))
        ).scalar_one()
        assert state.last_question == question
        assert state.last_sql is not None

    async def test_stream_saves_state_and_reports_progress(
        self, client, dbSession, monkeypatch
    ) -> None:
        """流式：同一分支也要落库 + 保存状态（两条路径此前实现不对称）。"""
        from app.services.step_query_planner import StepQueryPlanner

        config, ds = await _seed(dbSession)
        adapter = _OkAdapter()
        _install(monkeypatch, config, _MultiStepLlm(), adapter)
        monkeypatch.setattr(
            StepQueryPlanner, "rule_based_split", staticmethod(self._planWithoutAggregation)
        )

        question = "第一步，查 2024 年销售额；第二步，查 2025 年销售额"
        resp = await client.post("/api/v1/chat/stream", json=_payload(question, ds.id))
        assert resp.status_code == 200, resp.text

        frames = _parseFrames(resp)

        assert frames[-1][0] == EVENT_DONE
        token_text = "".join(
            str(d.get("content", "")) for e, d in frames if e == EVENT_TOKEN
        )
        assert "2/2" in token_text

        msgs = list(
            (await dbSession.execute(select(SessionMessage).order_by(SessionMessage.id)))
            .scalars()
            .all()
        )
        assert [m.role for m in msgs] == ["user", "assistant"]
        state = (
            await dbSession.execute(select(SessionQueryState))
        ).scalar_one()
        assert state.last_question == question
        assert state.last_sql is not None


class TestStepFailureIsolation:
    """C3：单个数据步骤硬失败（SQL 执行 + 回灌重试均失败）不再中止整条多步序列。

    此前异常从 `_executeMultiStep` / `_streamMultiStep` 穿透到 API 层：已完成步骤的
    数据与用量全部作废，对外是 500（非流式）/ internal 错误事件（流式）。
    """

    _QUESTION = "请分步查询 2024 和 2025 年的销售额并对比"

    @staticmethod
    async def _post(
        client, dbSession, monkeypatch, *, stream: bool, fail_from_query: int,
        exc_factory: Callable[[], Exception] | None = None,
    ):
        """注入「第 N 个数据查询起全部失败」的依赖后发一轮请求。

        步骤 2 的 SQL 执行 + 回灌重试 = 第 2、3 个数据查询，故 fail_from_query=2
        恰好表达「第二步执行与重试均失败」；=1 则所有数据步骤均失败。
        """
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _DataQueryFailAdapter(
            fail_from_query=fail_from_query, exc_factory=exc_factory,
        )
        _install(monkeypatch, config, llm, adapter)
        path = "/api/v1/chat/stream" if stream else "/api/v1/chat"
        resp = await client.post(path, json=_payload(TestStepFailureIsolation._QUESTION, ds.id))
        return resp, llm, adapter

    async def test_non_stream_step_failure_isolated(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """非流式：第二步硬失败 → 200 + 该步 error + 第一步与汇总照常完成。"""
        resp, llm, adapter = await self._post(
            client, dbSession, monkeypatch, stream=False, fail_from_query=2,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        assert len(body["steps"]) == 2
        # 第一步照常完成（SQL + 数据都在）——已完成步骤不因后续步骤失败而作废
        assert body["steps"][0]["error"] is None
        assert body["steps"][0]["sql"] is not None
        assert body["steps"][0]["data"]
        # 第二步记步骤级错误（sql/data 双双为空），序列继续
        assert body["steps"][1]["error"] is not None
        assert body["steps"][1]["sql"] is None
        assert body["steps"][1]["data"] is None
        assert adapter.failed == 2  # 首次执行 + 回灌重试各失败一次
        # 汇总步骤照常执行（拿到 1 成功 + 1 失败）
        assert any("企业数据分析助手" in m[0][1] for m in llm.calls)
        # 三个 nl2sql 行 = 两步首次生成 + 第二步的**回灌重试生成**。后者是本用例的关键：
        # 重试生成成功、重试执行又失败，那次生成同样花了钱，不能随异常丢失（核心约束 #3）。
        # chart 只有 1 行：第一步（成功）出了图，第二步失败没有数据可画、不出图。
        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        assert sorted(r.purpose for r in usages) == [
            "answer", "chart", "multistep_global_filter",
            "nl2sql", "nl2sql", "nl2sql", "step_plan",
        ]
        assert all(r.prompt_tokens > 0 for r in usages if r.purpose == "nl2sql")
        # 落库 + 查询状态锚定**成功**的第一步（失败步骤没有 SQL，不能成为追问锚点）
        msgs = list(
            (await dbSession.execute(select(SessionMessage).order_by(SessionMessage.id)))
            .scalars()
            .all()
        )
        assert [m.role for m in msgs] == ["user", "assistant"]
        state = (await dbSession.execute(select(SessionQueryState))).scalar_one()
        assert state.last_sql == body["steps"][0]["sql"]

    async def test_step_error_text_hides_sql_and_parameters(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """用户可见的步骤错误只留驱动原因：SQL 全文与查询参数不得外泄。

        `StepResult.error` 会原样进非流式响应与流式 `step_result` 事件（前端直接渲染），
        而 DB 驱动异常经 SQLAlchemy 包装后 `str()` 会追加 `[SQL: ...]`（内部表/列名）与
        `[parameters: ...]`（查询字面量，可能含业务数据）。
        """
        from sqlalchemy.exc import ProgrammingError

        def _leaky() -> Exception:
            # 真实语句异常的格式：驱动原因 + [SQL: ...] + [parameters: ...]
            return ProgrammingError(
                "SELECT SECRET_COL FROM APP.SECRET_TABLE WHERE CUST_NAME=:n",
                {"n": "ACME-机密客户"},
                RuntimeError("ORA-00942: 表或视图不存在"),
            )

        calls = _spyGenerateSql(monkeypatch)
        resp, _, _ = await self._post(
            client, dbSession, monkeypatch, stream=False, fail_from_query=1,
            exc_factory=_leaky,
        )
        assert resp.status_code == 200, resp.text
        error = resp.json()["steps"][0]["error"]
        assert error is not None
        # 驱动给的原因保留（用户据此才能自查/反馈），其余一律不出现
        assert "ORA-00942" in error
        for leaked in (
            "SELECT SECRET_COL", "SECRET_TABLE", "ACME-机密客户", "[SQL:", "[parameters:",
        ):
            assert leaked not in error, f"用户可见文案泄漏了 {leaked}：{error}"
        # 正向对照：回灌给 LLM 的重试反馈仍带细节（脱敏只针对用户可见出口，不是一刀切）
        feedback = [c.executionError for c in calls if c.executionError]
        assert feedback and all("SECRET_TABLE" in f for f in feedback)

    async def test_step_error_text_carries_retry_failure(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """M7：步骤文案要同时给出「首次」与「重试后仍失败」两段原因。

        只报首次错误会让用户（和排查的人）以为「这一步的 SQL 一上来就写错了」，
        而真相是首次错了、回灌重试**同样**错 —— 后者才是「为什么没救回来」的答案。
        两段用不同 ORA 码以便区分「都出现了」与「只出现了首次那条」。
        """
        seen = {"n": 0}

        def _sequential() -> Exception:
            seen["n"] += 1
            code = "ORA-00942: 表或视图不存在" if seen["n"] == 1 else "ORA-00904: 标识符无效"
            return RuntimeError(code)

        resp, _, adapter = await self._post(
            client, dbSession, monkeypatch, stream=False, fail_from_query=2,
            exc_factory=_sequential,
        )
        assert resp.status_code == 200, resp.text
        assert adapter.failed == 2  # 首次执行 + 回灌重试各失败一次
        error = resp.json()["steps"][1]["error"]

        assert "ORA-00942" in error, f"首次失败原因缺失：{error}"
        assert "重试后仍执行失败" in error, f"未交代重试这一步：{error}"
        assert "ORA-00904" in error, f"重试的失败原因缺失（M7 的整段丢失）：{error}"

    async def test_stream_step_failure_isolated(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """流式：同一分支（两条路径此前实现不对称，这里一并钉住）。"""
        resp, llm, adapter = await self._post(
            client, dbSession, monkeypatch, stream=True, fail_from_query=2,
        )
        assert resp.status_code == 200, resp.text
        frames = _parseFrames(resp)
        events = [e for e, _ in frames]
        # 关键：不是 error 事件（此前异常穿透 → internal 错误事件）
        assert "error" not in events
        assert events[-1] == EVENT_DONE
        stepResults = [d for e, d in frames if e == EVENT_STEP_RESULT]
        assert len(stepResults) == 2
        assert stepResults[0]["error"] is None
        assert stepResults[0]["sql"] is not None
        assert stepResults[1]["error"] is not None
        assert stepResults[1]["sql"] is None
        # 第三步（汇总）的 step_plan 事件照常下发 → 汇总未被跳过
        aggPlan = [d for e, d in frames if e == EVENT_STEP_PLAN and d["stepIndex"] == 2]
        assert len(aggPlan) == 1
        assert any("企业数据分析助手" in m[0][1] for m in llm.calls)

    async def test_non_stream_all_steps_failed_skips_aggregation(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """所有数据步骤都失败 → 不进汇总（汇总 LLM 只见错误行，会编造结论）。"""
        resp, llm, _ = await self._post(
            client, dbSession, monkeypatch, stream=False, fail_from_query=1,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "multi_step"
        assert [s["error"] is not None for s in body["steps"]] == [True, True]
        # 汇总 LLM 未被调用（调用即意味着它只能基于错误行作答）
        assert not any("企业数据分析助手" in m[0][1] for m in llm.calls)
        assert body["answer"] == MSG_MULTI_STEP_DEGRADE_FAILED

    async def test_stream_all_steps_failed_skips_aggregation(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """流式同分支：文案与「不调汇总 LLM」两条都要成立。"""
        resp, llm, _ = await self._post(
            client, dbSession, monkeypatch, stream=True, fail_from_query=1,
        )
        assert resp.status_code == 200, resp.text
        frames = _parseFrames(resp)
        assert [e for e, _ in frames][-1] == EVENT_DONE
        assert "error" not in [e for e, _ in frames]
        tokenText = "".join(str(d.get("content", "")) for e, d in frames if e == EVENT_TOKEN)
        assert MSG_MULTI_STEP_DEGRADE_FAILED in tokenText
        assert not any("企业数据分析助手" in m[0][1] for m in llm.calls)

        # 汇总步骤虽被跳过，但计划概览已把它下发过（初始「待执行」）⇒ 必须补一个终态事件，
        # 否则前端汇总步永远停在「待执行」（末帧 steps 只含数据步骤，不会自愈）。
        assert [d for e, d in frames if e == EVENT_STEP_PLAN and d["stepIndex"] == 2] == []
        stepResults = [d for e, d in frames if e == EVENT_STEP_RESULT]
        assert len(stepResults) == 3
        assert stepResults[2]["stepIndex"] == 2
        assert stepResults[2]["error"] is not None
        assert stepResults[2]["sql"] is None


class TestStepRetryContext:
    """C4：多步场景下执行失败重试必须用子问题 + 跨步注入，而非原始复合问题。

    此前 `_runQueryWithRetry` 恒用 `dto.question`（原始复合问题）且 `priorState=None`：
    重试生成的 SQL 会丢掉子问题范围与「前序步骤结果」约束（如第二步引用的
    「这三个供应商」），与首次生成（子问题 + 注入）口径不一致。
    """

    _QUESTION = "请分步查询 2024 和 2025 年的销售额并对比"

    async def test_retry_uses_sub_question_and_prior_injection(
        self, client, dbSession, monkeypatch,
    ) -> None:
        config, ds = await _seed(dbSession)
        llm = _PerStepLlm()
        # 只让第二步的 SQL 失败一次 → 触发一次回灌重试（重试后成功）
        adapter = _FlakyThenOkAdapter(fail_substring=STEP2_FAIL_MARKER, fail_count=1)
        _install(monkeypatch, config, llm, adapter)
        calls = _spyGenerateSql(monkeypatch)

        resp = await client.post(
            "/api/v1/chat", json=_payload(self._QUESTION, ds.id),
        )
        assert resp.status_code == 200, resp.text
        steps = resp.json()["steps"]
        assert steps[1]["error"] is None  # 重试后成功
        assert adapter.failed == 1

        retries = [c for c in calls if c.executionError is not None]
        assert len(retries) == 1
        retry = retries[0]
        # 子问题（而非原始复合问题）：重试若不带上「2025 年」范围，SQL 会重新对齐成
        # 「对比 2024 和 2025」，与已生成的第一步结果口径不一致
        assert retry.question == steps[1]["subQuestion"]
        assert retry.question != self._QUESTION
        # 跨步注入文本（第一步结果）必须随重试带上
        assert "前序步骤结果" in (retry.priorState or "")
        # 主问题仍作为 scopeQuestion 透传（范围感知行数限制的并集判定）
        assert retry.scopeQuestion == self._QUESTION


class TestSingleStepRetryMetering:
    """M7：单步路径下「重试生成」的 token 也必须落账。

    核心约束 #3 的失败路径同样是计量路径。此前只有多步 `_executeDataStep` 落了账，
    单步两条路径（流式/非流式）都漏：重试生成的那次调用的 token 白花 —— 请求继续
    （回退多步）时响应总额少算且台账缺行。两个分支各漏一次：
    ①重试生成成功、重试执行又失败（`_attachRetryGenTokens`）；
    ②重试生成**自己**就失败（`Nl2SqlError.tokens`，此前既没落账也没随异常交回）。

    **关于非流式硬失败**（此前本文档写「异常穿透触发 getDb 整体回滚、故不断言」，那是
    **错的**）：`TokenUsageService.recordUsage` 每次调用都 `session.add()` +
    `await session.commit()`（见 `token_usage_service.py`），台账行在每次 LLM 调用后
    即已提交；`getDb` 的 `rollback()` 只能回滚未提交的工作，撤不掉已提交的行。因此
    四种组合（流式/非流式 × 回退多步/硬失败）**都会**留下台账行，全部断言。
    """

    _QUESTION = "对比 2024 和 2025 年的销售额"

    @staticmethod
    async def _nl2sqlRows(dbSession) -> list[SessionTokenUsage]:
        usages = list((await dbSession.execute(select(SessionTokenUsage))).scalars().all())
        return [u for u in usages if u.purpose == "nl2sql"]

    async def test_non_stream_fallback_records_single_step_retry_gen(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """单步（首次 + 回灌重试）双失败 → 回退多步：单步那次重试生成也要有台账行。"""
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _FlakyThenOkAdapter(fail_substring="SUM(QTY)", fail_count=2)
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post("/api/v1/chat", json=_payload(self._QUESTION, ds.id))
        assert resp.status_code == 200, resp.text
        assert resp.json()["intent"] == "multi_step"

        rows = await self._nl2sqlRows(dbSession)
        # 4 行 = 单步首次生成 + **单步回灌重试生成** + 多步两个数据步骤的首次生成
        # 不变量：每次 `generateSql` 恰好落一行 nl2sql（无论该次调用成功与否，
        # 失败路径由异常携带 token 交回）⇒ 行数 == 本次请求实际发生的生成调用次数
        assert len(rows) == 4, f"nl2sql 台账行数不符：{[(r.prompt_tokens, r.completion_tokens) for r in rows]}"
        assert all(r.prompt_tokens > 0 for r in rows)

    async def test_retry_generation_failure_records_its_tokens(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """重试**生成**自己失败：那次调用的 token 同样必须落账。

        `generateSql` 解析不出 SQL 时抛 `Nl2SqlError`，token 挂在异常上；重试生成分支
        此前把它整段丢弃 —— 请求随后照常回退多步并成功收尾（HTTP 200），于是这些
        token 既不在台账里、也不在响应总额里，是一次「查不到的费用」。
        """
        config, ds = await _seed(dbSession)
        llm = _RetryGenFailsLlm()
        # 首次数据查询失败一次即够：重试不会执行（生成就失败了），多步回退的各步可用
        adapter = _FlakyThenOkAdapter(fail_substring="SUM(QTY)", fail_count=1)
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post("/api/v1/chat", json=_payload(self._QUESTION, ds.id))
        assert resp.status_code == 200, resp.text
        assert resp.json()["intent"] == "multi_step"  # 重试生成失败 → 回退多步

        rows = await self._nl2sqlRows(dbSession)
        # 4 行 = 单步首次生成 + **单步重试生成（解析失败的那次）** + 多步两步的首次生成
        assert len(rows) == 4, f"nl2sql 台账行数不符：{[(r.prompt_tokens, r.completion_tokens) for r in rows]}"
        assert all(r.prompt_tokens > 0 for r in rows)

    async def test_stream_fallback_records_single_step_retry_gen(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """流式同分支（两条路径此前实现不对称，这里一并钉住）。"""
        config, ds = await _seed(dbSession)
        llm = _MultiStepLlm()
        adapter = _FlakyThenOkAdapter(fail_substring="SUM(QTY)", fail_count=2)
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat/stream", json=_payload(self._QUESTION, ds.id),
        )
        assert resp.status_code == 200, resp.text
        events = [e for e, _ in _parseFrames(resp)]
        assert EVENT_ERROR not in events  # 回退多步成功，用户看不到错误
        assert events[-1] == EVENT_DONE

        rows = await self._nl2sqlRows(dbSession)
        assert len(rows) == 4, f"nl2sql 台账行数不符：{[(r.prompt_tokens, r.completion_tokens) for r in rows]}"
        assert all(r.prompt_tokens > 0 for r in rows)

    async def test_stream_hard_failure_records_single_step_retry_gen(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """单步双失败且无多步回退（拆步 LLM 判定不需要多步）：用量同样要留下。

        流式路径把异常收敛成 error 事件后正常收尾（HTTP 200），事务提交 —— 这一刻
        台账就是这次请求唯一的成本记录，重试生成那行不能丢。
        """
        config, ds = await _seed(dbSession)
        llm = _NoDecomposeLlm()
        adapter = _DataQueryFailAdapter(fail_from_query=1)
        _install(monkeypatch, config, llm, adapter)

        resp = await client.post(
            "/api/v1/chat/stream", json=_payload(self._QUESTION, ds.id),
        )
        assert resp.status_code == 200, resp.text
        frames = _parseFrames(resp)
        assert [e for e, _ in frames][-1] == EVENT_ERROR  # 单步失败直接上抛 → error 事件

        rows = await self._nl2sqlRows(dbSession)
        # 2 行 = 单步首次生成 + **单步回灌重试生成**（后者此前整段丢失）
        assert len(rows) == 2, f"nl2sql 台账行数不符：{[(r.prompt_tokens, r.completion_tokens) for r in rows]}"
        assert all(r.prompt_tokens > 0 for r in rows)

    async def test_non_stream_hard_failure_records_single_step_retry_gen(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """非流式硬失败：异常穿透到 ASGI 层，但已提交的台账行不受影响。

        非流式把异常原样上抛（`_processQuery` 末尾 `raise`，非 DomainError ⇒ 无 500
        处理器 ⇒ `ASGITransport` 默认把异常重新抛给调用方）。`getDb` 的 `rollback()`
        只回滚**未提交**的工作：`recordUsage` 在每次 LLM 调用后即 `commit()`，因此
        这里的两行是提交过的、撤不掉（此前本文档误以为它们会随回滚消失）。
        """
        config, ds = await _seed(dbSession)
        llm = _NoDecomposeLlm()
        adapter = _DataQueryFailAdapter(fail_from_query=1)
        _install(monkeypatch, config, llm, adapter)

        with pytest.raises(RuntimeError, match="ORA-00942"):
            await client.post("/api/v1/chat", json=_payload(self._QUESTION, ds.id))

        rows = await self._nl2sqlRows(dbSession)
        # 2 行 = 单步首次生成 + 单步回灌重试生成（均已在各自调用后提交）
        assert len(rows) == 2, f"nl2sql 台账行数不符：{[(r.prompt_tokens, r.completion_tokens) for r in rows]}"
        assert all(r.prompt_tokens > 0 for r in rows)
