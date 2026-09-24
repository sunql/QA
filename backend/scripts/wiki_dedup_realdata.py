"""P1 去重真实数据验证脚本（feat-wiki-dedup）。

交付门禁：每特性必须 ship 一个 ``backend/scripts/<feature>_realdata.py``，
摘要文档引用它的**真实输出**。本脚本站在修复的下游 —— 用**真实 PostgreSQL +
完整 HTTP API 链路**重演「8 次导入 × 同一份 75 页文档」的生产故障形状，再逐项
断言去重结果真的落库了。

为什么必须走真链路而不是只调 service：P1 要修的是「重跑同一份文件不再产生副本」，
而旧实现的副本恰好是**走完整导入路径**才产生的（每次导入都生成新 page_id）。
只调 ``generatePageId`` 验证不了「8 次导入后 wiki_page 恰好 75 行」这个信号。

七个步骤（与测试 spec §十 对齐，详见 Harness/changes/feat-wiki-dedup-p1）：

  1. 生成一份中文 Markdown 制度汇编（约 75 个 ``##`` 章节、≥300 行、含全角标点 /
     表格 / 代码块 / 一个超长标题），落到临时目录，文件是唯一事实源
  2. 真实 HTTP 导入 ×8：POST /wiki/import/preview → POST /wiki/import/execute
     （autoClassify=false，不烧 LLM），8 次都用同一份文件内容 + 同一 sourceRef
  3. 直接真实 PG 查询：本脚本拥有的行（按内容派生 page_id 圈定）= 75（旧实现会得到
     600）、distinct title = 75、content_hash 为 NULL 的行数 = 0；全局计数仅作上下文打印
  4. 真冲突（同 page_id、内容不同）：PATCH 改一篇正文 → 显式 pageId + 旧正文再导入
     → failedPages=1 / skipped=0（证明哈希会重算、冲突不会被误判成重跑）
  5. 幂等重放（改后）：显式 pageId + 新正文再导入 → skippedPages=1
  6. 台账无关性：DELETE FROM wiki_import_task 后重放 → 仍 skipped=75、本脚本拥有行数仍 75
  7. 每步打印步骤名 / 真实 SQL / 真实计数 / PASS-FAIL，末行恒为
     ``REALDATA RESULT: PASS`` 或 ``REALDATA RESULT: FAIL``

幂等：每次运行**先**清掉上一次写入的行 —— 按 ``source_ref`` 前缀（``realdata://``）
精确识别任务与挂账页，另按**内容派生的确定性 page_id** 清掉「上次跑到第 6 步、
台账已删、页成了孤儿」的情况。绝不 TRUNCATE 全库，也不碰非本脚本的数据。

写库闸：只允许打到 ``qa_metadata_test``（端口 5433）。生产库 ``qa_metadata`` 曾
发生过整库误删事故（见 memory: qa-system-pg-wipe-incident），本脚本**拒绝**写入
任何非测试库。注意 Alembic/settings 只认 ``DATABASE_URL``，``TEST_DATABASE_URL``
会被 Alembic 静默忽略（memory: qa-system-alembic-targets-prod），故运行命令必须
显式传 ``DATABASE_URL``。

运行（宿主机，需先确保 schema 已 ``alembic upgrade head``，即至少到 `0061_wiki_dedup`
—— 这里刻意不写死具体 head：唯一索引拆到 0062 之后 head 会继续往后走，本脚本的
前置条件始终是「0061 的那两列在」）：

    DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \\
        .venv/bin/python -m scripts.wiki_dedup_realdata
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

# 允许从 backend/ 直接运行（与同级脚本一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.infrastructure import database as dbModule  # noqa: E402
from app.services.wiki_page_service import (  # noqa: E402
    contentHashOf,
    generatePageId,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_HEADING_COUNT = 75
_TEST_DB_NAME = "qa_metadata_test"

# source_ref 前缀：本次运行的台账与页都靠它识别。``realdata://`` 后跟真实文件名，
# 既满足「同一真实文件名作为 sourceRef」的要求，又给清理一个精确锚点。
_FILENAME = "供应商管理制度汇编.md"
_SOURCE_REF = f"realdata://{_FILENAME}"
_REALDATA_PREFIX = "realdata://"

_API = "/api/v1"
_PREVIEW = f"{_API}/wiki/import/preview"
_EXECUTE = f"{_API}/wiki/import/execute"
_PAGES = f"{_API}/wiki/pages"

# 与 wiki_import_service._HEADING_RE 的 ``##`` 分支同形：匹配行首两个井号 + 标题。
_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# 造文件（步骤 1）
# ---------------------------------------------------------------------------


def buildMarkdownSource() -> str:
    """生成 75 个 ``##`` 章节的中文制度汇编（≥300 行，含全角标点/表格/代码块）。

    每条标题与正文都内嵌序号，保证 75 条**两两不同** —— 若任两条正文相同，
    第 3 步「distinct title = 75」的算术就变了（这正是脚本要钉死的不变量）。
    内容行刻意不写行首 ``## ``，避免被切分正则误吞成新标题。
    """
    lines: list[str] = []
    for i in range(1, _HEADING_COUNT + 1):
        if i == 42:
            # 一个刻意超长的标题（仍 < 200 字符），验证长标题在 page_id 派生与
            # 哈希里的稳定性，不会撞上 DTO/DB 的长度截断。
            title = (
                "第42章 " + "供应商" * 40 + "管理制度（本节标题刻意极长，用于验证超长标题在 page_id 派生与哈希中的稳定性）"
            )
        else:
            title = f"第{i}章 供应商管理制度汇编·条款{i}"
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"本条第{i}条是供应商管理制度汇编的第{i}章正文，收录于《采购与供应链管理制度汇编》。")
        lines.append("适用范围：注册资本、资质审查、质量协议、交付验收（含全角括号）与考核退出等事项。")
        lines.append("")
        if i % 3 == 0:
            lines.append("| 项目 | 阈值 | 说明 |")
            lines.append("| --- | --- | --- |")
            lines.append(f"| 注册资本 | ≥ {i * 100} 万元 | 准入底线 |")
            lines.append(f"| 质量协议 | 每年复核 | 第{i}章 |")
            lines.append("")
        if i % 5 == 0:
            lines.append("```")
            lines.append(f"RULE-{i}: 若 注册资本 < {i * 100} 万元 则 拒绝准入")
            lines.append(f"CHECK: 质量协议有效期 >= 第{i}章 复核周期")
            lines.append("```")
            lines.append("")
        lines.append(f"第{i}条强调：供应商须「资质齐全、按期交付、账实相符」；考核不合格者进入退出流程。")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def parseMarkdownDrafts(source: str) -> list[dict[str, str]]:
    """按 ``##`` 切出草稿（文件是唯一事实源，不是手写清单）。

    与 ``wiki_import_service.parseDrafts`` 对齐：正文**含**它自己的标题行，
    ``title`` 取标题文本去空白。生成器保证无 ``#`` 一级标题、无前言，故两套
    切分结果完全一致（第 2 步会用 ``/preview`` 交叉核对）。
    """
    matches = [(m.group(1).strip(), m.start()) for m in _HEADING_RE.finditer(source)]
    drafts: list[dict[str, str]] = []
    for index, (title, start) in enumerate(matches):
        end = matches[index + 1][1] if index + 1 < len(matches) else len(source)
        drafts.append({"title": title, "content": source[start:end].strip()})
    return drafts


# ---------------------------------------------------------------------------
# 写库闸 / 清理
# ---------------------------------------------------------------------------


def _guardDatabaseUrl() -> str:
    """写库闸：只允许打到 ``qa_metadata_test``，其余一律拒绝并退出。"""
    databaseUrl = os.environ.get("DATABASE_URL", "").strip()
    if not databaseUrl:
        raise SystemExit(
            "未设置 DATABASE_URL。本脚本会写数据，只允许打到 qa_metadata_test，请显式指定：\n"
            "  DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/"
            "qa_metadata_test' \\\n"
            "      .venv/bin/python -m scripts.wiki_dedup_realdata"
        )
    url = make_url(databaseUrl)
    if url.get_backend_name() != "postgresql":
        raise SystemExit(
            f"拒绝执行：DATABASE_URL 不是 PostgreSQL（{url.get_backend_name()}）。本脚本禁止 sqlite/其他库。"
        )
    dbName = url.database or ""
    if dbName != _TEST_DB_NAME:
        raise SystemExit(
            f"拒绝执行：目标库 `{dbName}` 不是测试库 `{_TEST_DB_NAME}`。"
            "生产库 qa_metadata 曾发生过整库误删事故，本脚本绝不写入非测试库。"
        )
    return databaseUrl


async def _purgeRealdataRows(factory, expectedPageIds: list[str]) -> int:
    """按精确标识清掉本脚本（上一次运行）写入的行，返回删除的页数。

    两道闸互补，覆盖两种残留形状：
    - **确定性 page_id**：上次若跑到第 6 步（``DELETE FROM wiki_import_task``），
      页的 ``imported_via_task_id`` 已被 ON DELETE SET NULL，``source_ref`` 前缀
      找不回来 —— 但 page_id 是 (sourceRef, title, content) 的纯函数，可重算出来。
    - **source_ref 前缀**：上次若中断在任务还没删的阶段，任务还在，靠前缀识别，
      再沿 FK 先删页/计量、后删任务。

    删除顺序是关键：``wiki_page.imported_via_task_id`` 指向 ``wiki_import_task``，
    必须先删页再删任务，否则页因 SET NULL 变成孤儿而漏删。
    """
    removedPages = 0
    async with factory() as session:
        if expectedPageIds:
            result = await session.execute(
                text("DELETE FROM wiki_page WHERE page_id = ANY(:ids)"),
                {"ids": expectedPageIds},
            )
            removedPages += result.rowcount or 0

        taskIds = list(
            (
                await session.execute(
                    text("SELECT id FROM wiki_import_task WHERE source_ref LIKE :prefix"),
                    {"prefix": f"{_REALDATA_PREFIX}%"},
                )
            ).scalars().all()
        )
        if taskIds:
            # 1) 页（FK imported_via_task_id，必须在任务之前删）
            result = await session.execute(
                text("DELETE FROM wiki_page WHERE imported_via_task_id = ANY(:ids)"),
                {"ids": taskIds},
            )
            removedPages += result.rowcount or 0
            # 2) 计量行（autoClassify=false 时通常没有，防御性清理）
            await session.execute(
                text("DELETE FROM wiki_token_usage WHERE import_task_id = ANY(:ids)"),
                {"ids": taskIds},
            )
            # 3) 任务
            await session.execute(
                text("DELETE FROM wiki_import_task WHERE id = ANY(:ids)"),
                {"ids": taskIds},
            )
        await session.commit()
    return removedPages


# ---------------------------------------------------------------------------
# 输出助手
# ---------------------------------------------------------------------------


def _banner(databaseUrl: str) -> str:
    url = make_url(databaseUrl)
    return (
        "\n" + "=" * 72 + "\n"
        f"P1 去重真实数据验证 —— 目标库 {url.database}@{url.host}:{url.port}\n"
        "=" * 72
    )


def _stepTitle(num: str, title: str) -> None:
    print(f"\n{'-' * 72}\n[步骤 {num}] {title}\n{'-' * 72}")


def _check(condition: bool, label: str, failures: list[str]) -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"      [{mark}] {label}")
    if not condition:
        failures.append(label)


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    expect: int = 200,
) -> dict:
    """发 HTTP 请求并强校验状态码 —— 非预期状态码时带响应体炸出来，绝不静默解析。"""
    resp = await client.request(method, path, json=body)
    if resp.status_code != expect:
        raise RuntimeError(
            f"{method} {path} 期望 HTTP {expect}，实际 {resp.status_code}：{resp.text}"
        )
    return resp.json() if resp.content else {}


async def _scalar(factory, sql: str, params: dict | None = None) -> Any:
    """跑一条返回标量的裸 SQL（打印 SQL 本身，作为第 7 步「真实 SQL」的证据）。"""
    print(f"      SQL: {sql.strip()}")
    async with factory() as session:
        return (await session.execute(text(sql), params or {})).scalar_one()


# ---------------------------------------------------------------------------
# 七个步骤
# ---------------------------------------------------------------------------


async def _step1File(markdown: str, failures: list[str]) -> None:
    _stepTitle("1", "生成真实源文件（中文制度汇编，约 75 个 ## 章节）")
    tmpdir = tempfile.mkdtemp(prefix="wiki_dedup_realdata_")
    path = Path(tmpdir) / _FILENAME
    path.write_text(markdown, encoding="utf-8")
    # 读回文件 —— 让「文件是事实源」成立，而不是直接拿内存字符串充数。
    fileText = path.read_text(encoding="utf-8")
    lineCount = fileText.count("\n") + 1
    headingCount = len(_HEADING_RE.findall(fileText))
    print(f"      文件: {path}")
    print(f"      行数: {lineCount}（要求 ≥ 300）")
    print(f"      标题数: {headingCount}（要求 ≈ 75）")
    _check(lineCount >= 300, f"文件 ≥ 300 行（实际 {lineCount}）", failures)
    _check(headingCount == _HEADING_COUNT, f"标题数 = {_HEADING_COUNT}（实际 {headingCount}）", failures)
    # 供后续步骤复用：把临时目录挂在闭包外（main 里统一清理）。
    global _TMPDIR
    _TMPDIR = tmpdir


async def _step2Import(
    client: httpx.AsyncClient,
    drafts: list[dict[str, str]],
    ctx: dict[str, Any],
    failures: list[str],
) -> None:
    _stepTitle("2", "真实 HTTP 导入 ×8（preview → execute，autoClassify=false）")
    preview = await _request(client, "POST", _PREVIEW, body={"source": _buildSourceText(drafts)})
    # 用 /preview 交叉核对：真解析管线切出来的标题必须与本脚本切的一致，
    # 证明文件确实可被导入、且本脚本的切分没有漂移。
    previewTitles = [d["title"] for d in preview["drafts"]]
    myTitles = [d["title"] for d in drafts]
    _check(preview["total"] == _HEADING_COUNT, f"preview.total = {_HEADING_COUNT}", failures)
    _check(previewTitles == myTitles, "preview 标题与本脚本切分一致", failures)

    for run in range(1, 9):
        body = {"drafts": drafts, "sourceRef": _SOURCE_REF, "autoClassify": False}
        result = await _request(client, "POST", _EXECUTE, body=body, expect=201)
        s, k, f = result["successPages"], result["skippedPages"], result["failedPages"]
        print(
            f"      导入第 {run} 次: status={result['status']} "
            f"success={s} skipped={k} failed={f}"
        )
        if run == 1:
            _check(s == _HEADING_COUNT, f"第 1 次 successPages = {_HEADING_COUNT}（实际 {s}）", failures)
            _check(k == 0, f"第 1 次 skippedPages = 0（实际 {k}）", failures)
            _check(f == 0, f"第 1 次 failedPages = 0（实际 {f}）", failures)
            ctx["pageIds"] = list(result["pageIds"] or [])
            _check(
                len(ctx["pageIds"]) == _HEADING_COUNT,
                f"第 1 次落库 pageIds = {_HEADING_COUNT}（实际 {len(ctx['pageIds'])}）",
                failures,
            )
        else:
            _check(s == 0, f"第 {run} 次 successPages = 0（实际 {s}）", failures)
            _check(k == _HEADING_COUNT, f"第 {run} 次 skippedPages = {_HEADING_COUNT}（实际 {k}）", failures)
            _check(f == 0, f"第 {run} 次 failedPages = 0（实际 {f}）", failures)


async def _step3Counts(factory, expectedPageIds: list[str], failures: list[str]) -> None:
    _stepTitle("3", "直接真实 PG 查询（旧实现会得到 600 行）")
    # 全局计数只作上下文打印、**不作断言**：测试库是共享的，长期躺着别的测试/
    # 脚本写入的外来行（实测 2 行，全局 total=77、distinct title=76），绝对
    # ``count(*)`` 永不等于 75。之前这里 FAIL 不是产品 bug，而是断言作用域错了。
    total = await _scalar(factory, "SELECT count(*) FROM wiki_page")
    distinct = await _scalar(factory, "SELECT count(DISTINCT title) FROM wiki_page")
    print(f"      全局上下文（含外来行，不作断言）：total={total} distinct_title={distinct}")
    # 断言只落在本脚本拥有的行上：内容派生的确定性 page_id 集合。
    owned = await _scalar(
        factory,
        "SELECT count(*) FROM wiki_page WHERE page_id = ANY(:ids)",
        {"ids": expectedPageIds},
    )
    ownedDistinct = await _scalar(
        factory,
        "SELECT count(DISTINCT title) FROM wiki_page WHERE page_id = ANY(:ids)",
        {"ids": expectedPageIds},
    )
    ownedNullHash = await _scalar(
        factory,
        "SELECT count(*) FROM wiki_page WHERE page_id = ANY(:ids) AND content_hash IS NULL",
        {"ids": expectedPageIds},
    )
    _check(owned == _HEADING_COUNT, f"本脚本拥有行数 = {_HEADING_COUNT}（实际 {owned}）", failures)
    _check(ownedDistinct == _HEADING_COUNT, f"本脚本拥有行 distinct title = {_HEADING_COUNT}（实际 {ownedDistinct}）", failures)
    _check(ownedNullHash == 0, f"本脚本拥有行 content_hash 为 NULL 数 = 0（实际 {ownedNullHash}）", failures)


async def _step4Conflict(
    client: httpx.AsyncClient,
    factory,
    ctx: dict[str, Any],
    failures: list[str],
) -> None:
    _stepTitle("4", "真冲突：同 page_id、内容不同 → 计失败（不被误判成重跑）")
    if not ctx["pageIds"]:
        failures.append("步骤 4 跳过：步骤 2 未产生 pageIds")
        print("      [FAIL] 前置缺失，跳过")
        return
    pageId = ctx["pageIds"][0]
    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT title, content, content_hash FROM wiki_page WHERE page_id = :pid"),
                {"pid": pageId},
            )
        ).mappings().one()
    title, originalContent = row["title"], row["content"]
    newContent = originalContent + "\n\n（2026-09-13 修订：新增条款）"

    patched = await _request(client, "PATCH", f"{_PAGES}/{pageId}", body={"content": newContent})
    _check(patched["pageId"] == pageId, "PATCH 后 page_id 不变", failures)

    # 哈希必须重算：不重算会把「同 ID 不同内容」的冲突误判成重跑而静默丢知识。
    storedHash = await _scalar(
        factory, "SELECT content_hash FROM wiki_page WHERE page_id = :pid", {"pid": pageId}
    )
    _check(
        storedHash == contentHashOf(newContent),
        "PATCH 后 content_hash 已重算（与 contentHashOf 同口径）",
        failures,
    )

    result = await _request(
        client,
        "POST",
        _EXECUTE,
        body={
            "drafts": [{"pageId": pageId, "title": title, "content": originalContent}],
            "sourceRef": _SOURCE_REF,
            "autoClassify": False,
        },
        expect=201,
    )
    status = result["status"]
    failed = result["failedPages"]
    skipped = result["skippedPages"]
    print(f"      旧正文再导入: status={status} failed={failed} skipped={skipped}")
    _check(result["failedPages"] == 1, f"failedPages = 1（实际 {failed}）", failures)
    _check(result["skippedPages"] == 0, f"skippedPages = 0（实际 {skipped}）", failures)
    _check(result["status"] == "FAILED", f"status = FAILED（实际 {result['status']}）", failures)

    ctx["editPage"] = {"pageId": pageId, "title": title, "original": originalContent, "new": newContent}


async def _step5IdempotentReplay(
    client: httpx.AsyncClient,
    factory,
    ctx: dict[str, Any],
    failures: list[str],
) -> None:
    _stepTitle("5", "幂等重放（改后）：同 page_id、同内容 → 跳过")
    edit = ctx.get("editPage")
    if not edit:
        failures.append("步骤 5 跳过：步骤 4 未产出编辑页")
        print("      [FAIL] 前置缺失，跳过")
        return
    result = await _request(
        client,
        "POST",
        _EXECUTE,
        body={
            "drafts": [{"pageId": edit["pageId"], "title": edit["title"], "content": edit["new"]}],
            "sourceRef": _SOURCE_REF,
            "autoClassify": False,
        },
        expect=201,
    )
    status = result["status"]
    failed = result["failedPages"]
    skipped = result["skippedPages"]
    print(f"      新正文再导入: status={status} failed={failed} skipped={skipped}")
    _check(result["skippedPages"] == 1, f"skippedPages = 1（实际 {skipped}）", failures)
    _check(result["failedPages"] == 0, f"failedPages = 0（实际 {failed}）", failures)


async def _step6LedgerIndependence(
    client: httpx.AsyncClient,
    factory,
    drafts: list[dict[str, str]],
    ctx: dict[str, Any],
    expectedPageIds: list[str],
    failures: list[str],
) -> None:
    _stepTitle("6", "台账无关性：DELETE FROM wiki_import_task 后重放")
    edit = ctx.get("editPage")
    if edit:
        # 先把第 4 步改过的页还原成原正文：否则整份重放时那一篇是「同 ID 不同内容」的
        # 冲突（failed=1），skipped 就到不了 75。还原是步骤 4/5 编辑的必然收尾。
        await _request(client, "PATCH", f"{_PAGES}/{edit['pageId']}", body={"content": edit["original"]})

    print("      SQL: DELETE FROM wiki_import_task")
    async with factory() as session:
        await session.execute(text("DELETE FROM wiki_import_task"))
        await session.commit()

    result = await _request(
        client,
        "POST",
        _EXECUTE,
        body={"drafts": drafts, "sourceRef": _SOURCE_REF, "autoClassify": False},
        expect=201,
    )
    status = result["status"]
    success = result["successPages"]
    skipped = result["skippedPages"]
    failed = result["failedPages"]
    print(f"      删台账后重放: success={success} skipped={skipped} failed={failed}")
    _check(skipped == _HEADING_COUNT, f"skippedPages = {_HEADING_COUNT}（实际 {skipped}）", failures)
    _check(success == 0, f"successPages = 0（实际 {success}）", failures)

    total = await _scalar(factory, "SELECT count(*) FROM wiki_page")
    print(f"      全局上下文（含外来行，不作断言）：total={total}")
    owned = await _scalar(
        factory,
        "SELECT count(*) FROM wiki_page WHERE page_id = ANY(:ids)",
        {"ids": expectedPageIds},
    )
    _check(owned == _HEADING_COUNT, f"删台账后本脚本拥有行数仍 = {_HEADING_COUNT}（实际 {owned}）", failures)


# ---------------------------------------------------------------------------
# 编排 / 入口
# ---------------------------------------------------------------------------


def _buildSourceText(drafts: list[dict[str, str]]) -> str:
    """由草稿反拼出源文（预览用）。草稿正文已含标题行，顺序拼接即还原。

    这里不另存一份 markdown 字符串，而是从草稿反推，保证预览吃到的源文与
    本脚本切分来自同一事实（文件），避免两处各存一份源文漂移。
    """
    return "\n\n".join(d["content"] for d in drafts)


async def _runAllSteps(
    client: httpx.AsyncClient,
    factory,
    drafts: list[dict[str, str]],
    expectedPageIds: list[str],
    failures: list[str],
) -> None:
    ctx: dict[str, Any] = {"pageIds": [], "editPage": None}
    steps = [
        ("1 源文件", lambda: _step1File(_buildSourceText(drafts), failures)),
        ("2 导入×8", lambda: _step2Import(client, drafts, ctx, failures)),
        ("3 计数", lambda: _step3Counts(factory, expectedPageIds, failures)),
        ("4 冲突", lambda: _step4Conflict(client, factory, ctx, failures)),
        ("5 幂等重放", lambda: _step5IdempotentReplay(client, factory, ctx, failures)),
        ("6 台账无关性", lambda: _step6LedgerIndependence(client, factory, drafts, ctx, expectedPageIds, failures)),
    ]
    for name, coro in steps:
        try:
            await coro()
        except Exception as exc:
            print(f"      [FAIL] 步骤「{name}」异常：{type(exc).__name__}: {exc}")
            failures.append(f"步骤「{name}」异常: {type(exc).__name__}: {exc}")


_TMPDIR: str | None = None


async def main() -> int:
    databaseUrl = _guardDatabaseUrl()
    markdown = buildMarkdownSource()
    drafts = parseMarkdownDrafts(markdown)
    # 内容派生的确定性 page_id：既用于清理孤儿页，也证明「同内容必得同 ID」。
    expectedPageIds = [generatePageId(d["title"], _SOURCE_REF, d["content"]) for d in drafts]

    failures: list[str] = []
    engine = create_async_engine(databaseUrl, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    oldFactory = dbModule._sessionFactory
    oldEngine = dbModule._engine
    client: httpx.AsyncClient | None = None

    print(_banner(databaseUrl))
    try:
        # 沿用 pgApiClient 的接线方式：替换全局工厂，把 getDb 依赖指向真实 PG。
        dbModule._sessionFactory = factory
        dbModule._engine = engine

        removed = await _purgeRealdataRows(factory, expectedPageIds)
        if removed:
            print(f"已清理上一次运行写入的 {removed} 行（source_ref 前缀 realdata://）")

        from app.tests._testapp import buildTestApp

        transport = httpx.ASGITransport(app=buildTestApp(factory))
        client = httpx.AsyncClient(transport=transport, base_url="http://test")

        await _runAllSteps(client, factory, drafts, expectedPageIds, failures)
    except Exception as exc:
        failures.append(f"脚本级异常：{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            await client.aclose()
        # 收尾自清：不管成败都清掉本次写入，测试库不留痕。
        try:
            await _purgeRealdataRows(factory, expectedPageIds)
        except Exception as exc:
            print(f"  收尾清理失败（不影响结论）：{exc}")
        dbModule._sessionFactory = oldFactory
        dbModule._engine = oldEngine
        await engine.dispose()
        if _TMPDIR:
            import shutil

            shutil.rmtree(_TMPDIR, ignore_errors=True)

    if failures:
        print("\n失败项：")
        for f in failures:
            print(f"  - {f}")
        print("\nREALDATA RESULT: FAIL")
        return 1
    print("\nREALDATA RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
