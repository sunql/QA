"""L2 占比歧义示警端到端（feat-nl2sql-share-denominator-guard）。

场景复刻 2026-10-02 真机问题：Top-N 占比查询执行成功、结果全组恒 100% —— 数学上
无法判错（也可能是组内明细 ≤ N），但必须向用户示警，而不是看似正常地返回答案。

覆盖：
- 单步非流式：答案以 ⚠️ 示警开头，正常回答文本仍在其后；
- 违规（占比 > 100%）路径在 unit 层已测（TestRunQueryShareCheckWiring）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.tests.integration.test_chat_api import _RouterFor, _StubEmbeddingService, _seed

_SHARE_PLAN_JSON = (
    '{"target":"各供应商Top3物料占比","selectedClasses":["PRECEIPT"],'
    '"selectedProperties":["NAME","QTY"],'
    '"aggregations":[{"function":"SUM","property":"QTY","alias":"TOP3_SHARE",'
    '"formula":"SUM(QTY) / SUM(SUM(QTY)) OVER ()"}],'
    '"groupBy":["NAME"],"partitionBy":["NAME"],"perGroupLimit":3,'
    '"sortBy":[{"property":"TOP3_SHARE","direction":"desc"}]}'
)

# 正确形态 SQL（独立 CTE 分母）—— L1 守卫必须放行，让流程走到执行后的 L2 示警
_SHARE_SQL = (
    "WITH sup_total AS ("
    "SELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT GROUP BY NAME) "
    "SELECT r.NAME, SUM(r.QTY) / NULLIF(s.TOTAL_QTY, 0) AS TOP3_SHARE "
    "FROM ZJTH.PRECEIPT r JOIN sup_total s ON s.NAME = r.NAME "
    "GROUP BY r.NAME, s.TOTAL_QTY"
)

# 全组恒 100%：陷阱签名，但数学上不可判错 → 示警而非报错
_ALL_ONES_ROWS = [
    {"NAME": "B019", "TOP3_SHARE": Decimal("1.0")},
    {"NAME": "B125", "TOP3_SHARE": Decimal("1.0")},
]


class _ShareAllOnesLlm:
    """按阶段路由：计划（Top-N 占比）/ SQL（正确形态）/ 回答 / 图表标签。"""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> object:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "解析为查询计划" in system:
            _Resp.content = _SHARE_PLAN_JSON
        elif "生成 SQL 时必须" in system:
            _Resp.content = f"```sql\n{_SHARE_SQL}\n```"
        elif "企业数据分析助手" in system:
            _Resp.content = "三家供应商的 Top3 物料占比均为 100%。"
        else:
            _Resp.content = "查询完成。"
        return _Resp()


class _AllOnesAdapter:
    """所有执行返回恒 100% 占比行（含值域采样，采样失败为 best-effort 不阻断）。"""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        return _ALL_ONES_ROWS


@pytest.mark.integration
class TestShareAmbiguityWarningSingleStep:
    async def test_answer_starts_with_warning_when_all_shares_are_one(
        self, client, dbSession, monkeypatch,
    ) -> None:
        config, ds = await _seed(dbSession)
        llm = _ShareAllOnesLlm()
        adapter = _AllOnesAdapter()
        import app.api.v1.chat as chat_module

        monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
        monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: llm)
        monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, d: adapter)
        monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())

        resp = await client.post(
            "/api/v1/chat",
            json={"sessionId": "s1", "question": "各供应商Top3收货数量占比", "datasourceId": ds.id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 示警在最前（用户第一眼看到），正常回答仍在其后
        assert body["answer"].startswith("⚠️")
        assert "分母错误" in body["answer"]
        assert "三家供应商的 Top3 物料占比均为 100%。" in body["answer"]
        # 业务 SQL 确实执行过（不是被 L1 拦掉后的空转）
        assert any("TOP3_SHARE" in sql for sql in adapter.executed)
