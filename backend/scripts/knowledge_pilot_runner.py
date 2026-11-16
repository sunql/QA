"""P4 D36：试点问题集跑批（**只读** · 真实数据）。

做什么
------
按问题集逐条调用知识 Facade（**真实**权限源 + **真实**发布面 + 真实路由），
用 `pilot_eval.evaluateResponse` 判三条判据（权限结果 / 证据命中 / 引用可回溯），
输出通过率与逐条明细。

只读保证
--------
脚本**只**通过 facade 发查询（facade 内部全是 SELECT）；不写任何表 ⇒ 可以安全
指向隔离测试库。目标库仍走仓内白名单守卫（`scripts/kpi_metric_db_guard.py`），
正式库需显式 `--allow-prod-write`（只读也不该在生产上乱跑）。

采购薄别名的等价性
------------------
采购域的每条问题**跑两遍**：一遍用主接口请求体（显式 `domain`），一遍用
采购薄别名的请求体（**没有 domain**，只固定转发）⇒ 两者响应必须**逐项一致**
（各路状态 + 证据 ref 集合）⇒ 这就是「别名 = 同一逻辑」的机器判据。

用法
----
    cd backend
    PILOT_DATABASE_URL='postgresql+asyncpg://user:pwd@localhost:5434/qa_metadata_test_pilot' \
      uv run python -m scripts.knowledge_pilot_runner \
        --out ../Harness/changes/feat-knowledge-facade/daily/pilot_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from scripts.kpi_metric_db_guard import assertSafeTarget, maskDsn  # noqa: E402

#: 问题集默认位置（仓内 Harness 变更目录）。
_DEFAULT_QUESTION_SET = (
    Path(__file__).resolve().parents[2]
    / "Harness/changes/feat-knowledge-facade/daily/pilot_questions.json"
)

#: 通过率判据：必须 100%（计划 §6）。
PASS_RATE_TARGET = 1.0


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P4 试点问题集跑批（只读）")
    parser.add_argument(
        "--db-url",
        default=os.environ.get("PILOT_DATABASE_URL") or os.environ.get("DATABASE_URL") or "",
        help="目标库 DSN（默认取环境变量 PILOT_DATABASE_URL / DATABASE_URL）",
    )
    parser.add_argument(
        "--question-set",
        default=str(_DEFAULT_QUESTION_SET),
        help="问题集 JSON 路径",
    )
    parser.add_argument("--out", default="", help="JSON 报告输出路径（可选）")
    parser.add_argument("--markdown", default="", help="Markdown 报告输出路径（可选）")
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="允许指向正式库 qa_metadata（只读脚本，仅用于显式确认）",
    )
    return parser.parse_args()


def _repoRelative(path: str) -> str:
    """报告里只写**仓内相对路径**（绝对路径是机器噪声，还会泄本地目录结构）。"""
    target = Path(path).resolve()
    repoRoot = Path(__file__).resolve().parents[2]
    try:
        return str(target.relative_to(repoRoot))
    except ValueError:
        return target.name


def _loadQuestions(path: str) -> tuple[Any, ...]:
    from app.services.knowledge.pilot_eval import parseQuestionSet

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return parseQuestionSet(payload)


def _currentUser(identity: Any) -> Any:
    from app.dependencies import CurrentUser

    return CurrentUser(
        userId="pilot-runner",
        roles=tuple(identity.roles),
        departments=tuple(identity.departments),
    )


def _canonicalRequest(question: Any) -> Any:
    from app.domain.knowledge_schemas import KnowledgeQueryRequest

    return KnowledgeQueryRequest(
        domain=question.domain,
        question=question.question,
        scope=question.scope,
    )


def _aliasRequest(question: Any) -> Any:
    """采购薄别名请求（**不带 domain**，只固定转发）。

    收窄口径原样带上：别名必须与主接口**同一逻辑**，否则「等价」这条判据
    会被「两边收窄不同」噪声掉。
    """
    from app.domain.knowledge_schemas import ProcurementKnowledgeQueryRequest

    return ProcurementKnowledgeQueryRequest(
        question=question.question, scope=question.scope
    ).toCanonicalRequest()


def _signature(response: Any) -> tuple[Any, ...]:
    """响应的「可比签名」：各路状态 + 证据 ref 集合（顺序无关）。"""
    statuses = tuple((row.channel, row.status) for row in response.route_results)
    refs = tuple(sorted(item.ref for item in response.evidence))
    return statuses + refs


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services.knowledge.facade import KnowledgeFacade
    from app.services.knowledge.pilot_eval import PilotCheck, evaluateResponse

    questions = _loadQuestions(args.question_set)
    facade = KnowledgeFacade()
    engine = create_async_engine(args.db_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    records: list[dict[str, Any]] = []
    try:
        async with factory() as session:
            for question in questions:
                user = _currentUser(question.identity)
                response = await facade.query(
                    _canonicalRequest(question), user=user, session=session
                )
                result = evaluateResponse(response, question)

                checks = list(result.checks)
                if question.domain == "PROCUREMENT":
                    aliasResponse = await facade.query(
                        _aliasRequest(question), user=user, session=session
                    )
                    same = _signature(aliasResponse) == _signature(response)
                    checks.append(
                        PilotCheck(
                            name="alias_equivalence",
                            passed=same,
                            detail=(
                                "采购薄别名与主接口响应一致"
                                if same
                                else "采购薄别名与主接口响应不一致"
                            ),
                        )
                    )
                passed = all(check.passed for check in checks)
                records.append(
                    {
                        "id": question.id,
                        "question": question.question,
                        "domain": question.domain,
                        "identity": {
                            "roles": list(question.identity.roles),
                            "departments": list(question.identity.departments),
                        },
                        "passed": passed,
                        "release": {
                            "id": response.release.id,
                            "version": response.release.version,
                            "degraded": response.release.degraded,
                        },
                        "routes": {
                            row.channel: row.status for row in response.route_results
                        },
                        "evidence": [
                            {
                                "channel": item.channel,
                                "kind": item.kind,
                                "ref": item.ref,
                                "title": item.title,
                                "releaseId": item.release_id,
                                "releaseVersion": item.release_version,
                                "source": item.source,
                            }
                            for item in response.evidence
                        ],
                        "checks": [
                            {"name": check.name, "passed": check.passed, "detail": check.detail}
                            for check in checks
                        ],
                    }
                )
    finally:
        await engine.dispose()

    passedCount = sum(1 for record in records if record["passed"])
    total = len(records)
    passRate = (passedCount / total) if total else 0.0
    report = {
        "questionSet": _repoRelative(args.question_set),
        "database": maskDsn(args.db_url),
        "total": total,
        "passed": passedCount,
        "passRate": passRate,
        "target": PASS_RATE_TARGET,
        "ok": passRate >= PASS_RATE_TARGET,
        "records": records,
    }

    print(f"试点跑批：{passedCount}/{total} 通过（通过率 {passRate:.0%}，判据 {PASS_RATE_TARGET:.0%}）")
    for record in records:
        flag = "✅" if record["passed"] else "❌"
        routes = "、".join(f"{k}={v}" for k, v in record["routes"].items())
        print(f"  {flag} {record['id']} {record['question']}｜{routes}")
        if not record["passed"]:
            for check in record["checks"]:
                if not check["passed"]:
                    print(f"      ✗ {check['name']}：{check['detail']}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"JSON 报告：{args.out}")
    if args.markdown:
        Path(args.markdown).write_text(_markdown(report), encoding="utf-8")
        print(f"Markdown 报告：{args.markdown}")
    return 0 if report["ok"] else 1


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 试点跑批报告",
        "",
        f"- 通过率：**{report['passRate']:.0%}**（{report['passed']}/{report['total']}）",
        f"- 判据：{report['target']:.0%}",
        f"- 目标库：`{report['database']}`",
        "",
        "| 问题 | 身份 | 各路状态 | 引用版本 | 结论 |",
        "|---|---|---|---|---|",
    ]
    for record in report["records"]:
        routes = "、".join(f"{k}={v}" for k, v in record["routes"].items())
        identity = "/".join(record["identity"]["departments"]) or "-"
        version = record["release"]["version"] or "降级"
        lines.append(
            f"| {record['id']} {record['question']} | {identity} | {routes} | {version} | "
            f"{'✅ 通过' if record['passed'] else '❌ 不通过'} |"
        )
    failures = [record for record in report["records"] if not record["passed"]]
    if failures:
        lines += ["", "## 未通过明细", ""]
        for record in failures:
            for check in record["checks"]:
                if not check["passed"]:
                    lines.append(f"- {record['id']}：`{check['name']}` {check['detail']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parseArgs()
    if not args.db_url:
        print("缺少目标库：请用 --db-url 或环境变量 PILOT_DATABASE_URL", file=sys.stderr)
        return 2
    assertSafeTarget(args.db_url, allowProdWrite=args.allow_prod_write)
    print(f"目标库：{maskDsn(args.db_url)}")
    print(f"问题集：{args.question_set}")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
