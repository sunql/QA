"""研究会话证据契约真实数据验证脚本（feat-research-evidence-contract，环节一）。

交付门禁：每特性必须 ship 一个 ``backend/scripts/<feature>_realdata.py``，摘要文档
（``Harness/changes/feat-research-evidence-contract/summary.md``）引用它的**真实输出**。
本脚本在**真实 PostgreSQL** 上端到端跑一遍环节一最承重的**读取**路径，逐项打印真实
计数 / PASS-FAIL，末行恒为 ``REALDATA RESULT: PASS`` 或 ``FAIL``。

为什么走真库而不是 mock：本环节改的就是**证据契约的落库语义**（0118/0119 两张表 +
跨版本读），要验的正是「写下去的行经报告装配回来长什么样」，mock 会话说服不了任何人。
三条承重路径：

  1. **跨版本聚合**（Task 8 / D25）：``research_run_state`` 会**版本化**（resume ⇒
     ``updateRunState`` 新 UUID），步结果挂在**写入当时**的版本下 ⇒ 只按单个
     ``run_state_id`` 读会漏掉早前版本（这正是 Task 6 留下的洞）。本脚本给**同一
     ``turn_id`` 落两个 run_state 版本**、各挂 3 行步结果，再断言
     ``loadStepResultsByTurn`` 返回 6 行、两版本都在、序为 ``[0,1,2,0,1,2]``。
  2. **报告证据透传**（Task 8）：``ReportPlanner.compose`` 装配出的
     ``payload["evidence"]["stepResults"]`` 字段集恰为契约集（**顶层**不含 ``rows`` /
     ``raw_result_ref`` 体积字段），且与**独立直查**的 DB 真值逐行对齐（行数 +
     ``step_index`` 序列）—— 证明报告透传的是**真落库**数据，不是重算。
     注：顶层无原始行**不等于**整条目无原始行 —— ``summary.sampleRows`` 仍带
     ``DEFAULT_MAX_SAMPLE_ROWS`` 行样例（真实业务行，D14 有意设计）。
  3. **序列化口径**（D14）：``summary`` 的数值标量端到端是 JSON **字符串**
     （``research_json`` 的 ``Decimal -> str``），不是 float —— 本仓「两套 JSON 安全
     映射」的经典坑（见 memory ``qa-system-two-json-safe-mappings``）。

幂等：每次运行**先**清掉上一次写入的行（按 ``research_session.input_seed`` 前缀
``REALDATA-RESEARCH-EVIDENCE`` 精确识别，turn / run_state / step_result 逐级清理）。
绝不 TRUNCATE 全库，也不碰非本脚本的数据。

写库闸：只允许打到 ``qa_metadata_test``，且**显式拒绝生产端口 5433**（生产库
``qa_metadata`` 曾发生整库误删事故，见 memory ``qa-system-pg-wipe-incident``）。
Alembic/settings 只认 ``DATABASE_URL``（``TEST_DATABASE_URL`` 会被静默忽略，
memory ``qa-system-alembic-targets-prod``），故运行命令必须显式传 ``DATABASE_URL``。

前置条件：目标库里 0118/0119 已生效（``research_run_state`` / ``research_step_result``
在）。本脚本**不**跑 ``alembic upgrade head``（本仓 alembic 默认指向生产 5433，明令禁止）。

运行（宿主机，backend/ 下）：

    DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' \\
        .venv/bin/python -m scripts.research_evidence_realdata
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

# 允许从 backend/ 直接运行（与同级脚本一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)

from app.domain.research_models import ResearchRunState  # noqa: E402
from app.services import research_step_result as repo  # noqa: E402
from app.services.report_planner import ReportPlanner  # noqa: E402
from app.services.research_session_service import ResearchSessionService  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_TEST_DB_NAME = "qa_metadata_test"
_PRODUCTION_PORT = 5433  # 生产库 qa_metadata 的端口，显式拒绝
_MARKER = "REALDATA-RESEARCH-EVIDENCE"  # input_seed 前缀，用于幂等清理
_QUESTION = f"{_MARKER} 供应商收货量为什么下降"
_STEP_RESULT_SCHEMA_VERSION = 1  # buildStepResult 落 raw_result_ref["schemaVersion"]
_EVIDENCE_FIELDS = frozenset(
    {"index", "type", "sql", "status", "error", "summary", "schemaVersion"}
)
_NUMERIC_SUMMARY_FIELDS = ("total", "baseline", "current", "delta", "deltaRatio")

_results: list[tuple[str, bool]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok))
    mark = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{mark}] {name}{suffix}")


def _guardDatabaseUrl() -> str:
    """写库闸：只允许 ``qa_metadata_test``，且显式拒绝生产端口 5433。"""
    databaseUrl = os.environ.get("DATABASE_URL", "").strip()
    if not databaseUrl:
        raise SystemExit(
            "未设置 DATABASE_URL。本脚本会写数据，只允许打到 qa_metadata_test，请显式指定：\n"
            "  DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/"
            "qa_metadata_test' \\\n"
            "      .venv/bin/python -m scripts.research_evidence_realdata"
        )
    url = make_url(databaseUrl)
    if url.get_backend_name() != "postgresql":
        raise SystemExit(
            f"拒绝执行：DATABASE_URL 不是 PostgreSQL（{url.get_backend_name()}）。"
            "本脚本禁止 sqlite/其他库。"
        )
    if url.port == _PRODUCTION_PORT:
        raise SystemExit(
            f"拒绝执行：端口 {url.port} 是**生产端口**（生产库 qa_metadata）。测试库在 5434。"
        )
    dbName = url.database or ""
    if dbName != _TEST_DB_NAME:
        raise SystemExit(
            f"拒绝执行：目标库 `{dbName}` 不是测试库 `{_TEST_DB_NAME}`。"
            "生产库 qa_metadata 曾发生过整库误删事故，本脚本绝不写入非测试库。"
        )
    print(f"[guard] 目标库 = {dbName} @ {url.host}:{url.port}（测试库，写库闸放行）\n")
    return databaseUrl


async def _purgeOwnRows(factory: Any) -> int:
    """清掉本脚本上一次运行写入的行（幂等的第一步）。返回删除的会话行数。

    只按 ``research_session.input_seed`` 前缀精确圈定本脚本的行，再**逐级**清理
    step_result → run_state → turn → session（不依赖 CASCADE 的隐式行为）。
    绝不 TRUNCATE 全库、不碰非本脚本的数据。
    """
    async with factory() as session:
        before = (
            await session.execute(
                text(
                    "SELECT count(*) FROM research_step_result s "
                    "JOIN research_run_state r ON s.run_state_id = r.id "
                    "JOIN research_session c ON r.session_id = c.id "
                    "WHERE c.input_seed LIKE :p"
                ),
                {"p": f"{_MARKER}%"},
            )
        ).scalar_one()
        sidStmt = "SELECT id FROM research_session WHERE input_seed LIKE :p"
        await session.execute(
            text(
                "DELETE FROM research_step_result WHERE run_state_id IN "
                "(SELECT r.id FROM research_run_state r JOIN research_session c "
                " ON r.session_id = c.id WHERE c.input_seed LIKE :p)"
            ),
            {"p": f"{_MARKER}%"},
        )
        await session.execute(
            text(
                "DELETE FROM research_run_state WHERE session_id IN "
                f"({sidStmt})"
            ),
            {"p": f"{_MARKER}%"},
        )
        await session.execute(
            text(f"DELETE FROM research_turn WHERE session_id IN ({sidStmt})"),
            {"p": f"{_MARKER}%"},
        )
        deleted = (
            await session.execute(
                text("DELETE FROM research_session WHERE input_seed LIKE :p"),
                {"p": f"{_MARKER}%"},
            )
        ).rowcount
        await session.commit()
    print(
        f"[purge] 上次运行的残留：删会话 {deleted} 行、连同 step_result {before} 行（逐级清理）"
    )
    return int(deleted or 0)


async def _readStepRowsViaNewSession(
    factory: Any, turnId: uuid.UUID
) -> list[dict[str, Any]]:
    """从**新会话**直查真实列值 —— 只读本会话内存里的 ORM 实例证明不了已落库。

    这条查询**独立于**报告用的 ``loadStepResultsByTurn`` 读取器（自带 join + 排序），
    故「报告内容 == DB 真值」是一条真交叉检验。
    """
    async with factory() as fresh:
        rows = (
            await fresh.execute(
                text(
                    "SELECT s.step_index, s.status, s.sql_text, s.evidence_summary, "
                    "s.raw_result_ref "
                    "FROM research_step_result s "
                    "JOIN research_run_state r ON s.run_state_id = r.id "
                    "WHERE r.turn_id = :t "
                    "ORDER BY r.created_at ASC, r.id ASC, s.step_index ASC"
                ),
                {"t": turnId},
            )
        ).all()
    return [dict(row._mapping) for row in rows]


# ---------------------------------------------------------------------------
# 步骤 1：落 1 会话 + 1 turn + 2 个 run_state 版本 × 3 行步结果
# ---------------------------------------------------------------------------


def _summary(baseline: str, current: str) -> dict[str, Any]:
    """证据摘要（数值标量**串化**：D14 ``Decimal -> str`` 口径）。"""
    return {
        "total": current,
        "baseline": baseline,
        "current": current,
        "delta": str(int(current) - int(baseline)),
        "deltaRatio": "0.2",
    }


async def _seedEvidence(
    factory: Any,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """建会话 + turn + 两个 run_state 版本，各挂 3 行步结果。

    返回 ``(sessionId, turnId, v1, v2)``。三行分别覆盖 ``success`` / ``empty`` /
    ``failed`` 三条 status 派生路径（D23）。
    """
    svc = ResearchSessionService()
    async with factory() as session:
        sessionRow = await svc.createSession(
            session, userId=None, question=_QUESTION, mode="research"
        )
        turn = await svc.appendTurn(
            session, sessionId=sessionRow.id, role="user", content={"question": _QUESTION}
        )
        await session.commit()
        sessionId, turnId = sessionRow.id, turn.id

        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        v1 = uuid.uuid4()
        v2 = uuid.uuid4()
        for versionId, parentId, createdAt in (
            (v1, None, base),
            (v2, v1, base + timedelta(minutes=1)),
        ):
            await svc.createRunState(
                session,
                ResearchRunState(
                    id=versionId,
                    session_id=sessionId,
                    turn_id=turnId,
                    mode="research",
                    current_question=_QUESTION,
                    parent_run_id=parentId,
                    created_at=createdAt,
                ),
            )
            await session.commit()
            for idx, kind in enumerate(("success", "empty", "failed")):
                error = "守卫拒绝" if kind == "failed" else None
                rawRows = (
                    [{"year": 2026, "amount": Decimal("120")}] if kind == "success" else []
                )
                await repo.saveStepResult(
                    session,
                    repo.buildStepResult(
                        run_state_id=versionId,
                        step_index=idx,
                        step_type=f"{kind}步",
                        sql_text=f"SELECT {idx} AS amount FROM DUAL",
                        raw_rows=rawRows,
                        evidence_summary=_summary("100", "120"),
                        error=error,
                    ),
                )
                await session.commit()
    return sessionId, turnId, v1, v2


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def _run() -> int:
    databaseUrl = _guardDatabaseUrl()
    engine = create_async_engine(databaseUrl)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _purgeOwnRows(factory)
        sessionId, turnId, v1, v2 = await _seedEvidence(factory)
        print(f"[seed] 会话 {sessionId} / turn {turnId} / 版本 v1={v1} v2={v2}（各 3 步）\n")

        # ── 路径 1：跨版本聚合（loadStepResultsByTurn） ──
        print("── 路径 1：跨版本聚合（Task 8 / D25）")
        async with factory() as session:
            rows = await repo.loadStepResultsByTurn(session, turnId)
            unrelated = await repo.loadStepResultsByTurn(session, uuid.uuid4())
        _check("跨版本聚合返回 6 行（两个版本各 3 行）", len(rows) == 6, f"实际 {len(rows)}")
        _check(
            "两个 run_state 版本的行都在（只按单版本读会漏）",
            {r.run_state_id for r in rows} == {v1, v2},
        )
        _check(
            "排序 = (版本时序, step_index)：v1 在前、版本内按步序",
            [r.run_state_id for r in rows] == [v1, v1, v1, v2, v2, v2],
        )
        _check(
            "step_index 序列 = [0,1,2,0,1,2]",
            [r.step_index for r in rows] == [0, 1, 2, 0, 1, 2],
        )
        _check(
            "status 派生正确（success/empty/failed 三路）",
            {r.step_index: r.status for r in rows}
            == {0: "success", 1: "empty", 2: "failed"},
        )
        _check("无关 turn 读取为空（不串号）", unrelated == [])

        # ── 路径 2：报告证据透传（compose） ──
        print("\n── 路径 2：报告 payload.evidence 透传（Task 8）")
        svc = ResearchSessionService()
        async with factory() as session:
            planner = ReportPlanner(sessionService=svc)
            payload, renderedMd = await planner.compose(
                session, sessionId=sessionId, turnId=turnId, mode="research"
            )
        # 独立直查放在 compose 会话关闭**之后**：避免两条连接在同一 async with 里叠加，
        # 也确保回读用全新会话（不共享 compose 的会话缓存）。
        dbRows = await _readStepRowsViaNewSession(factory, turnId)
        evidence = payload["evidence"]["stepResults"]
        _check("报告 evidence.stepResults 6 条", len(evidence) == 6, f"实际 {len(evidence)}")
        _check(
            "字段集恰为契约集（无 rows / raw_result_ref）",
            all(set(item) == _EVIDENCE_FIELDS for item in evidence)
            and all("rows" not in item and "raw_result_ref" not in item for item in evidence),
        )
        _check(
            "报告透传 == DB 真值（行数 + step_index 序列，独立直查）",
            len(evidence) == len(dbRows)
            and [item["index"] for item in evidence]
            == [row["step_index"] for row in dbRows],
            f"报告 {[i['index'] for i in evidence]} vs DB {[r['step_index'] for r in dbRows]}",
        )
        _check("报告照常产出 markdown", bool(renderedMd))

        # ── 路径 3：序列化口径（summary 数值标量 = JSON 字符串，D14） ──
        print("\n── 路径 3：summary 数值标量序列化口径（D14）")
        asserted = 0
        allStr = True
        for item in evidence:
            summary = item.get("summary") or {}
            for field in _NUMERIC_SUMMARY_FIELDS:
                value = summary.get(field)
                if value is None:
                    continue
                asserted += 1
                allStr = allStr and isinstance(value, str)
        _check(
            "summary 数值标量端到端是字符串（非 float）",
            allStr,
        )
        _check("至少断言到一个数值标量（否则本断言形同虚设）", asserted > 0, f"计 {asserted}")
        _check(
            "首步 baseline == '100'（Decimal->str 逐字）",
            evidence[0]["summary"].get("baseline") == "100",
            f"实际 {evidence[0]['summary'].get('baseline')!r}",
        )
        _check(
            "首步 schemaVersion == 1（raw_result_ref 带的 schema 版本）",
            evidence[0]["schemaVersion"] == _STEP_RESULT_SCHEMA_VERSION,
        )
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(_run())
    except SystemExit as exc:
        print(f"\n{exc}")
        return 2
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n断言合计 {total} 条，通过 {passed} 条")
    ok = passed == total and total > 0
    print(f"\nREALDATA RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
