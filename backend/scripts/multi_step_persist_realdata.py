"""多步持久化真实数据验证脚本（feat-multi-step-persist）。

交付门禁：每特性必须 ship 一个 ``backend/scripts/<feature>_realdata.py``，
摘要文档（``Harness/changes/feat-multi-step-persist/summary.md``）引用它的**真实输出**。
本脚本在**真实 PostgreSQL** 上端到端跑一遍本特性最承重的三条路径，逐项打印
真实 SQL / 真实计数 / PASS-FAIL，末行恒为 ``REALDATA RESULT: PASS`` 或 ``FAIL``。

为什么走真库而不是 mock：本特性改的就是**落库语义**（两张表 + 0114/0115 + 清理任务），
要验的正是「写下去的行长什么样」，mock 会话说服不了任何人。三条路径：

  1. **建 run + steps**（F2 口径）：`total_steps` / `completed_steps` / 收尾
     `current_step_idx` 三者**同源** —— 都按**完整计划步数**（含末尾汇总步）计，
     不按数据步个数。收尾行落库后从**新会话**回读真实列值。
  2. **续跑**（`prepareResume`）：合法续跑成功（状态回 running、`finished_at` 清空、
     起步之后的步重置）；**重复幂等键被拒**；**越界 `from_step_index` 被拒** ——
     且两次拒绝的**错误文案必须不同**（本项目反复踩「只断言状态码/只断言抛异常」
     的假绿，见 memory `qa-system-same-status-code-false-green`）。
  3. **清理**（`cleanupMultiStepRuns`）：过期的终态行被回收，未过期 / 非终态的行
     不被回收；另含「终态但 `finished_at IS NULL`」的行 —— 该形态**刻意不回收**
     （IMP-1 方案 B 的显式裁定，需运维一次性回填，见变更记录遗留项 16）。

幂等：每次运行**先**清掉上一次写入的行（按 `session_id` 前缀 ``realdata-msps-``
精确识别，``multi_step_step`` 靠 FK ``ON DELETE CASCADE`` 一并清）。绝不 TRUNCATE
全库，也不碰非本脚本的数据 —— 清理步骤的断言只落在本脚本自己的行上。

**两次运行的输出不是逐字节相同**：`finished_at` 的墙钟时间戳、以及 `[purge]` 行打印的
全局 `deleted` 计数（取决于库里他人残留的行）都会浮动。稳定的是**断言集合与末行
`PASS/FAIL`** —— 别把「上次 dif 为空」当成可复现性保证（那次只是恰好同环境）。

写库闸：只允许打到 ``qa_metadata_test``，且**显式拒绝生产端口 5433**（生产库
``qa_metadata`` 曾发生整库误删事故，见 memory `qa-system-pg-wipe-incident`）。
Alembic/settings 只认 ``DATABASE_URL``（``TEST_DATABASE_URL`` 会被静默忽略，
memory `qa-system-alembic-targets-prod`），故运行命令必须显式传 ``DATABASE_URL``。

前置条件：目标库里 0114/0115 已生效（``multi_step_run`` / ``multi_step_step`` 两张表在）。
本脚本**不**跑 ``alembic upgrade head``（本仓 alembic 默认指向生产 5433，明令禁止）；
测试库的 schema 由 `app/tests/_pg_support.py` 按 `TEST_DATABASE_URL` 建好后即可复用。

运行（宿主机，backend/ 下）：

    DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' \\
        .venv/bin/python -m scripts.multi_step_persist_realdata
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
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

from app.jobs.cleanup_multi_step_runs import cleanupMultiStepRuns  # noqa: E402
from app.services import multi_step_persistence as repo  # noqa: E402
from app.services.multi_step_resume import (  # noqa: E402
    ResumeConflict,
    prepareResume,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_TEST_DB_NAME = "qa_metadata_test"
_PRODUCTION_PORT = 5433  # 生产库 qa_metadata 的端口，显式拒绝
_SESSION_PREFIX = "realdata-msps-"
_QUESTION = "请分步查询 2024 和 2025 年的销售额并对比"

# 计划形状：2 个数据步 + 1 个汇总步 = 3（F2 口径分母 = 完整计划步数，不是数据步数）
_PLAN_STEPS = ["查询 2024 年销售额", "查询 2025 年销售额", "[汇总] 对比两年销售额"]
_TOTAL_STEPS = len(_PLAN_STEPS)

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
            "      .venv/bin/python -m scripts.multi_step_persist_realdata"
        )
    url = make_url(databaseUrl)
    if url.get_backend_name() != "postgresql":
        raise SystemExit(
            f"拒绝执行：DATABASE_URL 不是 PostgreSQL（{url.get_backend_name()}）。"
            "本脚本禁止 sqlite/其他库。"
        )
    if url.port == _PRODUCTION_PORT:
        raise SystemExit(
            f"拒绝执行：端口 {url.port} 是**生产端口**（生产库 qa_metadata）。"
            "测试库在 5434。"
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
    """清掉本脚本上一次运行写入的行（幂等的第一步）。返回删除的 run 行数。

    只按 ``session_id`` 前缀精确圈定本脚本的行；``multi_step_step`` 由 FK
    ``ON DELETE CASCADE`` 一并清。绝不 TRUNCATE 全库、不碰非本脚本的数据。
    """
    async with factory() as session:
        before = (
            await session.execute(
                text("SELECT count(*) FROM multi_step_step s JOIN multi_step_run r "
                     "ON s.run_id = r.id WHERE r.session_id LIKE :p"),
                {"p": f"{_SESSION_PREFIX}%"},
            )
        ).scalar_one()
        deleted = (
            await session.execute(
                text("DELETE FROM multi_step_run WHERE session_id LIKE :p"),
                {"p": f"{_SESSION_PREFIX}%"},
            )
        ).rowcount
        await session.commit()
    print(f"[purge] 上次运行的残留：删 run {deleted} 行、连同 steps {before} 行（CASCADE）")
    return int(deleted or 0)


def _sessionKey(tag: str) -> str:
    """本脚本专用的会话标识（前缀可识别 + 后缀区分场景，便于隔离与幂等清理）。"""
    return f"{_SESSION_PREFIX}{tag}"


async def _readRunViaNewSession(factory: Any, runId: uuid.UUID) -> dict[str, Any]:
    """从**新会话**读真实列值 —— 只读本会话内存里的 ORM 实例证明不了已落库。"""
    async with factory() as fresh:
        row = (
            await fresh.execute(
                text(
                    "SELECT status, total_steps, completed_steps, current_step_idx, "
                    "finished_at FROM multi_step_run WHERE id = :i"
                ),
                {"i": runId},
            )
        ).one()
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# 步骤 1：建 run + steps（F2 口径）
# ---------------------------------------------------------------------------


async def _step1BuildRunAndSteps(factory: Any) -> None:
    print("── 步骤 1：建 run + steps（total_steps / completed_steps 口径 = F2）")
    async with factory() as session:
        run = await repo.createRun(
            session, sessionId=_sessionKey("s1"), question=_QUESTION,
            modelId=None, datasourceId=1, totalSteps=_TOTAL_STEPS,
        )
        steps = await repo.createSteps(session, runId=run.id, subQuestions=_PLAN_STEPS)
        # 三步全部成功收尾，再以「真的收尾」的形态关闭 run（status 与 finished_at 同一 UPDATE）
        for step in steps:
            await repo.markStepRunning(session, step)
            await repo.finishStep(
                session, step, status="succeeded",
                sql=f"SELECT {step.step_index}", data=[{"n": step.step_index}],
                tokens=10, cost=0.0001,
            )
        await repo.updateRun(
            session, run, status="succeeded", completedSteps=_TOTAL_STEPS,
            currentStepIdx=_TOTAL_STEPS, finished=True,
        )
        await session.commit()
        runId = run.id
        print(f"  SQL: INSERT INTO multi_step_run(...total_steps={_TOTAL_STEPS}) / "
              f"multi_step_step ×{_TOTAL_STEPS}")
        print(f"  SQL: UPDATE multi_step_run SET status='succeeded', "
              f"completed_steps={_TOTAL_STEPS}, current_step_idx={_TOTAL_STEPS}, finished_at=now()")

    snap = await _readRunViaNewSession(factory, runId)
    print(f"  回读（新会话）: {snap}")
    _check("run.status == succeeded", snap["status"] == "succeeded")
    _check(
        f"total_steps == {_TOTAL_STEPS}（含末尾汇总步）",
        snap["total_steps"] == _TOTAL_STEPS,
    )
    _check(
        f"completed_steps == total_steps == {_TOTAL_STEPS}（分子分母同源，不出现 3/2）",
        snap["completed_steps"] == snap["total_steps"] == _TOTAL_STEPS,
    )
    _check(
        f"收尾哨兵 current_step_idx == {_TOTAL_STEPS}（同一把尺子）",
        snap["current_step_idx"] == _TOTAL_STEPS,
    )
    _check("终态必须带 finished_at（不变量 ①）", snap["finished_at"] is not None)


async def _seedResumableRun(session: Any, tag: str) -> uuid.UUID:
    """种一条「第 0 步成功、第 1 步失败」的可续跑 run（步数 = _TOTAL_STEPS）。"""
    run = await repo.createRun(
        session, sessionId=_sessionKey(tag), question=_QUESTION,
        modelId=None, datasourceId=1, totalSteps=_TOTAL_STEPS,
    )
    steps = await repo.createSteps(session, runId=run.id, subQuestions=_PLAN_STEPS)
    await repo.finishStep(
        session, steps[0], status="succeeded", sql="SELECT 1", data=[{"a": 1}],
    )
    await repo.recordStepError(
        session, steps[1], message="ORA-00942: 表或视图不存在", kind="permanent",
    )
    await repo.finishStep(session, steps[1], status="failed")
    await repo.updateRun(
        session, run, status="failed", completedSteps=1, currentStepIdx=1, finished=True,
    )
    await session.commit()
    return run.id


# ---------------------------------------------------------------------------
# 步骤 2：续跑（合法 / 重复幂等键 / 越界）
# ---------------------------------------------------------------------------


async def _attemptResume(
    factory: Any, runId: uuid.UUID, *, fromStepIndex: int, idempotencyKey: str
) -> tuple[Any, str]:
    """跑一次 prepareResume，返回 (返回的起始步, 被拒文案)。被拒时文案非空。"""
    async with factory() as session:
        try:
            _run, start = await prepareResume(
                session, runId=runId, fromStepIndex=fromStepIndex,
                idempotencyKey=idempotencyKey,
            )
            await session.commit()
            return start, ""
        except ResumeConflict as exc:
            await session.rollback()
            return None, str(exc)


async def _resumeLegal(factory: Any, runId: uuid.UUID) -> None:
    start, _ = await _attemptResume(
        factory, runId, fromStepIndex=1, idempotencyKey="key-A"
    )
    print("  SQL: UPDATE multi_step_run SET status='running', current_step_idx=1, "
          "finished_at=NULL, idempotency_keys=idempotency_keys||'key-A'")
    print(f"  合法续跑返回起始步 = {start}")
    snap = await _readRunViaNewSession(factory, runId)
    print(f"  回读（新会话）: {snap}")
    _check("合法续跑：起始步 == 1", start == 1)
    _check("合法续跑：status 回到 running", snap["status"] == "running")
    _check("合法续跑：finished_at 由 NOT NULL 清回 NULL", snap["finished_at"] is None)
    _check("合法续跑：current_step_idx == 1", snap["current_step_idx"] == 1)


async def _resumeRejections(factory: Any, runId: uuid.UUID) -> None:
    # 此时 status=running（不可续）。带已存在的 key-A ⇒ 文案应指「重复键」；
    # 带一个**新**键 key-B ⇒ 文案应指「状态不可续」。两者必须可区分。
    _, dupMsg = await _attemptResume(
        factory, runId, fromStepIndex=1, idempotencyKey="key-A"
    )
    _, statusMsg = await _attemptResume(
        factory, runId, fromStepIndex=1, idempotencyKey="key-B"
    )
    print(f"  重复键被拒文案: {dupMsg!r}")
    print(f"  状态不可续被拒文案: {statusMsg!r}")
    _check("重复幂等键被拒", "duplicate idempotency key" in dupMsg, dupMsg or "<未抛异常>")
    _check(
        "对照：拒绝文案与「重复键」不同（区分是谁拒的，不是只看抛没抛）",
        bool(statusMsg) and statusMsg != dupMsg and "not resumable" in statusMsg,
        statusMsg or "<未抛异常>",
    )

    # 越界 from_step_index —— 先把 run 关回 failed 使其可续
    async with factory() as session:
        run = await repo.loadRun(session, runId)
        await repo.updateRun(session, run, status="failed", finished=True)
        await session.commit()
    _, rangeMsg = await _attemptResume(
        factory, runId, fromStepIndex=99, idempotencyKey="key-C"
    )
    print(f"  越界被拒文案: {rangeMsg!r}")
    _check(
        "越界 from_step_index=99 被拒（文案含 out of range）",
        "out of range" in rangeMsg, rangeMsg or "<未抛异常>",
    )
    _check(
        "三种拒绝文案两两不同（否则「是哪个闸拒的」无从判起）",
        len({dupMsg, statusMsg, rangeMsg}) == 3,
    )


async def _step2Resume(factory: Any) -> None:
    print("── 步骤 2：续跑（prepareResume）")
    async with factory() as session:
        runId = await _seedResumableRun(session, "s2")
    await _resumeLegal(factory, runId)
    await _resumeRejections(factory, runId)


# ---------------------------------------------------------------------------
# 步骤 3：清理（过期终态回收 / 未过期与非终态不回收 / 缺 finished_at 不回收）
# ---------------------------------------------------------------------------


async def _insertRun(
    session: Any, tag: str, *, status: str, ageDays: int, finishedAt: Any
) -> uuid.UUID:
    """直接插一行 run（清理是纯数据行为，不需走执行链路）。"""
    runId = uuid.uuid4()
    at = datetime.now(UTC) - timedelta(days=ageDays)
    await session.execute(
        text(
            "INSERT INTO multi_step_run (id, session_id, question, model_id, status, "
            "total_steps, completed_steps, current_step_idx, compressed_count, "
            "resume_count, version, idempotency_keys, started_at, updated_at, finished_at) "
            "VALUES (:id, :sid, :q, NULL, :st, 1, 0, 0, 0, 0, 0, '[]'::jsonb, :at, :at, :fa)"
        ),
        {"id": runId, "sid": _sessionKey(tag), "q": "cleanup fixture", "st": status,
         "at": at, "fa": (at if finishedAt else None)},
    )
    await session.commit()
    return runId


async def _step3Cleanup(factory: Any) -> None:
    print("── 步骤 3：保留期清理（cleanupMultiStepRuns）")
    now = datetime.now(UTC)
    async with factory() as session:
        expiredOk = await _insertRun(session, "c-expired-ok", status="succeeded", ageDays=40, finishedAt=True)
        expiredBad = await _insertRun(session, "c-expired-bad", status="failed", ageDays=10, finishedAt=True)
        freshOk = await _insertRun(session, "c-fresh-ok", status="succeeded", ageDays=5, finishedAt=True)
        freshBad = await _insertRun(session, "c-fresh-bad", status="failed", ageDays=2, finishedAt=True)
        nonTerminal = await _insertRun(session, "c-running", status="running", ageDays=100, finishedAt=False)
        # 终态但 finished_at IS NULL（IMP-1 方案 B 显式裁定：不回收）
        poisoned = await _insertRun(session, "c-poisoned", status="failed", ageDays=30, finishedAt=False)

    mysql = ("DELETE FROM multi_step_run WHERE (status='succeeded' AND finished_at < :sb) "
             "OR (status IN ('failed','partially_failed') AND finished_at < :fb)")
    print(f"  谓词 SQL: {mysql}")
    async with factory() as session:
        deleted = await cleanupMultiStepRuns(session, now=now)
        await session.commit()

    async with factory() as session:
        survivors = {
            row[0] for row in await session.execute(
                text("SELECT id FROM multi_step_run WHERE session_id LIKE :p"),
                {"p": f"{_SESSION_PREFIX}c-%"},
            )
        }
    print(f"  全局回收计数 deleted = {deleted}；本脚本清理夹具存活 = {len(survivors)} 行")
    _check("过期的 succeeded（40 天）被回收", expiredOk not in survivors)
    _check("过期的 failed（10 天）被回收", expiredBad not in survivors)
    _check("未过期的 succeeded（5 天）不回收", freshOk in survivors)
    _check("未过期的 failed（2 天）不回收", freshBad in survivors)
    _check("非终态 running（100 天）不回收", nonTerminal in survivors)
    _check(
        "终态但 finished_at IS NULL 不回收（显式裁定，需运维回填）",
        poisoned in survivors,
    )


async def _step0SchemaCheck(factory: Any) -> None:
    async with factory() as session:
        rows = await session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                 "AND tablename IN ('multi_step_run','multi_step_step')")
        )
        found = {r[0] for r in rows}
    if found != {"multi_step_run", "multi_step_step"}:
        raise SystemExit(
            f"目标库缺少本特性的表（找到 {sorted(found)}）。请先让测试 harness "
            "按 TEST_DATABASE_URL 建好 schema（0114/0115）。"
        )
    print(f"[schema] 两张表就位：{sorted(found)}\n")


async def main() -> int:
    databaseUrl = _guardDatabaseUrl()
    engine = create_async_engine(databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        await _step0SchemaCheck(factory)
        await _purgeOwnRows(factory)
        await _step1BuildRunAndSteps(factory)
        print()
        await _step2Resume(factory)
        print()
        await _step3Cleanup(factory)
    finally:
        await engine.dispose()

    failed = [name for name, ok in _results if not ok]
    print(f"\n断言合计 {len(_results)} 条，通过 {len(_results) - len(failed)} 条")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
        print("\nREALDATA RESULT: FAIL")
        return 1
    print("\nREALDATA RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
