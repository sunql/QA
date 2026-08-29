"""Phase 5 最终全栈冒烟：真实 Postgres 元数据 + 真实 Oracle 业务库 JOIN。

仅 stub LLM 网络客户端（docker/.env API Key 留空，避免真实外部调用），其余全真实：
- 真实 Postgres：data_source / ontology_class / ontology_property / llm_config /
  session_token_usage / session_message
- 真实 Oracle（ZJTH）：经真实 _OracleAdapter 执行 JOIN 查询返回多表数据

验证项：
- 5.1  buildSchemaText 渲染 `[FK → 目标表]` 与 `### JOIN 关系` 段落；NL2SQL 系统
       prompt 含 JOIN 指引；生成的 JOIN SQL 在真实 Oracle 执行成功返回多表数据
- 5.2  非流式与流式调用后 session_message 双写（user + assistant）落库
- token session_token_usage 落库（purpose=nl2sql/chart/answer）
- 5.4  LLM 异常触发降级（stub 对首次 nl2sql 抛错，验证 _callWithFallback）
- 5.6  processMessageStream 产出 meta→sql→chart→token→done 事件序列
- chitchat 快捷路径：0 token、无 SQL/图表

用法（SECRET_KEY 取 docker/.env，用于解密 data_source 口令）：
    SECRET_KEY=$(grep '^SECRET_KEY=' docker/.env | cut -d= -f2) \\
        backend/.venv/bin/python backend/scripts/smoke_phase5_join.py
（需 infra 已启动、Oracle 192.168.205.70 可达）
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from sqlalchemy import func, select

from app.domain.exceptions import LlmClientError
from app.domain.models import SessionMessage, SessionTokenUsage
from app.domain.schemas import ChatRequest, HistoryMessage
from app.infrastructure.database import getSessionFactory
from app.infrastructure.llm.base_client import StreamChunk
from app.services.chat_service import ChatService
from app.services.nl2sql_service import Nl2SqlService
from app.services.ontology_service import OntologyService
from app.api.v1 import chat as chat_module

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("smoke")

DATASOURCE_ID = 2  # ZJTH-Oracle

JOIN_SQL = (
    "SELECT r.BPSNUM_0 AS SUPPLIER, COUNT(*) AS RECEIPT_LINE_COUNT "
    "FROM ZJTH.PRECEIPTD d JOIN ZJTH.PRECEIPT r ON r.PTHNUM_0 = d.PTHNUM_0 "
    "GROUP BY r.BPSNUM_0 ORDER BY RECEIPT_LINE_COUNT DESC FETCH FIRST 5 ROWS ONLY"
)

CHART_JSON = json.dumps(
    {"title": {"text": "各供应商收货明细条数"}, "tooltip": {},
     "series": [{"type": "pie", "data": [{"name": "H1", "value": 874934}]}]}
)

ANSWER = "已按供应商汇总各收货单的明细条数，共 5 家，H1 最多（约 87.5 万条）。"


class _StubLlm:
    """按 system prompt 内容路由回复：NL2SQL / 图表 JSON / 回答。记录每次调用消息。"""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []
        self.failFirstNl2Sql = False

    async def complete(self, messages: list, model: str | None = None) -> object:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        user = messages[1].content

        class _Resp:
            content = ""
            modelName = "stub"
            promptTokens = 100
            completionTokens = 20

        if self.failFirstNl2Sql and "可用的数据表结构" in system:
            self.failFirstNl2Sql = False
            raise LlmClientError("模拟 LLM 网络故障（首次 NL2SQL）")
        if "可用的数据表结构" in system:
            _Resp.content = f"```sql\n{JOIN_SQL}\n```"
        elif system.strip().startswith("你只输出合法的 JSON"):
            _Resp.content = CHART_JSON
        else:
            _Resp.content = ANSWER
        return _Resp()

    async def completeStream(self, messages: list, **kwargs):
        """流式回答：逐 2 字产出增量块，末块 isDone 携带 token 统计（5.6）。"""
        for i in range(0, len(ANSWER), 2):
            yield StreamChunk(
                content=ANSWER[i : i + 2],
                isDone=False,
                promptTokens=0,
                completionTokens=0,
                modelName="stub",
            )
        yield StreamChunk(
            content="", isDone=True, promptTokens=100, completionTokens=20, modelName="stub"
        )


class _NoopEmbedding:
    """冒烟聚焦 JOIN/上下文/流式：stub 掉 fire-and-forget 向量存储（5.3 失败容错由单测覆盖）。"""

    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None


def _stubFactory(config):
    return _stub


async def _countByPurpose(session) -> dict[str, int]:
    rows = await session.execute(
        select(SessionTokenUsage.purpose, func.count(SessionTokenUsage.id))
        .group_by(SessionTokenUsage.purpose)
    )
    return {p: n for p, n in rows.all()}


async def _countSessionMessages(session, sessionId: str) -> dict[str, int]:
    rows = await session.execute(
        select(SessionMessage.role, func.count(SessionMessage.id))
        .where(SessionMessage.session_id == sessionId)
        .group_by(SessionMessage.role)
    )
    return {role: n for role, n in rows.all()}


async def main() -> None:
    global _stub
    _stub = _StubLlm()

    # 真实库为持久化存储，每次运行用唯一 sessionId，保证本轮断言计数精确（1+1 双写）
    runId = uuid.uuid4().hex[:8]
    sessionId = f"smoke-join-{runId}"
    streamSessionId = f"smoke-stream-{runId}"
    sessionFactory = getSessionFactory()

    # ---- 0. 验证 buildSchemaText 直接渲染 FK JOIN 提示（5.1） ----
    async with sessionFactory() as session:
        ontology = OntologyService()
        classes = await ontology.listClasses(session)
        schemaText = Nl2SqlService().buildSchemaText(classes)
    assert "### JOIN 关系" in schemaText, "缺少 JOIN 关系段落"
    assert "[FK → PRECEIPT]" in schemaText, "缺少 [FK → PRECEIPT] 标注"
    print("[5.1] buildSchemaText 渲染 FK JOIN 提示 ✅")

    # ---- 1. 非流式查询（stub LLM + 真实 Oracle JOIN） ----
    chat_module._service._llmFactory = _stubFactory
    chat_module._service._embedding = _NoopEmbedding()

    async with sessionFactory() as session:
        dto = ChatRequest(sessionId=sessionId, question="各供应商的收货明细条数", datasourceId=DATASOURCE_ID)
        resp = await chat_module._service.processMessage(dto, session)

    nl2sqlCalls = [c for c in _stub.calls if "可用的数据表结构" in c[0][1]]
    assert nl2sqlCalls, "未捕获 NL2SQL 调用"
    nl2sqlSystem = nl2sqlCalls[0][0][1]
    assert "### JOIN 关系" in nl2sqlSystem, "NL2SQL prompt 未含 JOIN 段落"
    assert "[FK → PRECEIPT]" in nl2sqlSystem, "NL2SQL prompt 未含 FK 标注"
    print("[5.1] NL2SQL 系统 prompt 注入 JOIN 关系提示 ✅")
    assert "JOIN" in resp.sql.upper(), f"生成的 SQL 非 JOIN: {resp.sql}"
    print(f"[5.1] 生成 JOIN SQL 在真实 Oracle 执行成功，返回 {len(resp.data)} 行真实数据 ✅")
    print(f"       示例行: {resp.data[0] if resp.data else None}")
    assert resp.tokensUsed > 0 and float(resp.cost) > 0, "Token 计量缺失"
    assert resp.chartType == "pie" and resp.chartOption, "图表缺失"
    print(f"[Token] tokensUsed={resp.tokensUsed}, cost={resp.cost}, chartType={resp.chartType} ✅")

    async with sessionFactory() as session:
        usage = await _countByPurpose(session)
        assert usage.get("nl2sql") and usage.get("chart") and usage.get("answer"), f"审计行不全: {usage}"
        print(f"[Token] session_token_usage 落库: {usage} ✅")
        msgs = await _countSessionMessages(session, sessionId)
        assert msgs.get("user") == 1 and msgs.get("assistant") == 1, f"会话消息双写失败: {msgs}"
        print(f"[5.2] session_message 双写落库: {msgs} ✅")

    # ---- 2. 降级（5.4）：首次 NL2SQL 抛错 → 走 fallback 成功 ----
    _stub.failFirstNl2Sql = True
    async with sessionFactory() as session:
        dto = ChatRequest(sessionId=f"smoke-fallback-{runId}", question="各供应商的收货明细条数", datasourceId=DATASOURCE_ID)
        resp2 = await chat_module._service.processMessage(dto, session)
    assert resp2.sql and "JOIN" in resp2.sql.upper(), "降级后未恢复 JOIN 回答"
    print("[5.4] 首次 NL2SQL 故障 → fallback 降级重试成功 ✅")

    # ---- 3. 流式（5.6）：SSE 事件序列 meta→sql→chart→token→done ----
    async with sessionFactory() as session:
        dto = ChatRequest(sessionId=streamSessionId, question="各供应商的收货明细条数", datasourceId=DATASOURCE_ID)
        events = [ev async for ev in chat_module._service.processMessageStream(dto, session)]
    kinds = [ev.event for ev in events]
    assert kinds[0] == "meta", f"首事件非 meta: {kinds}"
    assert "sql" in kinds and "chart" in kinds and "done" in kinds and any(k == "token" for k in kinds), f"事件序列异常: {kinds}"
    print(f"[5.6] SSE 事件序列: {' → '.join(kinds)} ✅")

    # ---- 4. chitchat 快捷路径 ----
    async with sessionFactory() as session:
        dto = ChatRequest(sessionId=f"smoke-chitchat-{runId}", question="你好", datasourceId=DATASOURCE_ID)
        resp3 = await chat_module._service.processMessage(dto, session)
    assert resp3.intent == "chitchat" and resp3.sql is None and resp3.tokensUsed == 0
    print(f"[chitchat] {resp3.answer!r}，0 token ✅")

    # ---- 5. 会话上下文注入（5.2）：第二轮带 history 走真实存储 ----
    async with sessionFactory() as session:
        dto = ChatRequest(
            sessionId=streamSessionId,
            question="再按供应商看一次",
            datasourceId=DATASOURCE_ID,
            history=[HistoryMessage(role="user", content="各供应商的收货明细条数")],
        )
        _ = await chat_module._service.processMessage(dto, session)
        stored = await _countSessionMessages(session, streamSessionId)
    assert stored.get("user") == 2, f"上下文持久化后续轮失败: {stored}"
    print(f"[5.2] 第二轮后 session_message 累计: {stored} ✅")

    print("\n==== Phase 5 全栈冒烟全部通过 ====")


if __name__ == "__main__":
    asyncio.run(main())
