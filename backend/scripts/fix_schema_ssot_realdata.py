"""Schema SSOT 对齐真实数据门禁（Harness 开发门禁，**只读**）。

站在被修的东西的**下游**：验「ORM 声明 ↔ 真实 PG 结构一致」本身 —— 只跑一遍 drift 工具
就报绿的话，工具的比较盲区（谓词 / 类型 / comment / unique 标志）会把同类回归一起放过。
**只读**：只 SELECT 结构视图，不建不删不改任何对象，可反复执行。

断言：`[db:column]` / `[db:index]` / `[orm:table]` / `[orm:column]` / `[orm:index]` / `[alembic]`
一律为空；`[db:table]` 允许残留但必须落在显式允许名单里（名单外出现 = 失败 ⇒ 以后新增忘了
注册的模型模块会被抓到）。另加两条**正向**断言：`uq_metric_source_triple`（约束支撑的唯一索引
⇒ ORM 侧必须用 UniqueConstraint）与 `ix_evaluation_report_status_running`（partial 索引，谓词
必须含等值形式 `= 'RUNNING'` —— drift **不比较谓词**；PG 把 `IN (...)` 渲染成 `= ANY (ARRAY[...])`，
**不含** `= 'RUNNING'` ⇒ 能把两者区分开）。比较逻辑复用 `app/infrastructure/schema_drift.py`，
与容器启动期判定**同一份 SSOT**；ORM 侧声明形态由 `test_orm_schema_ssot.py` 钉，不在此重复。
另有**防漏断言**：drift 工具的 code 集合是 SSOT，门禁必须覆盖它全部 —— 日后新增一类 code 而
门禁没跟上 ⇒ 静默漏网 ⇒ 直接判失败。

用法（默认目标 = 5434 `qa_metadata_test`；密码只从环境变量读）：
    cd backend
    TEST_DATABASE_URL='postgresql+asyncpg://<user>:<pwd>@127.0.0.1:5434/qa_metadata_test' \
      python3 scripts/fix_schema_ssot_realdata.py
库名白名单 `^qa_metadata_test($|_)`；其他库须显式 `--allow-non-test-db`（默认关闭，只放宽库名
白名单，不放宽断言、不改只读性）。退出码：0 通过 / 1 失败项 / 2 环境错。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

# 以 `python scripts/fix_schema_ssot_realdata.py` 直接执行时 sys.path[0] 是 scripts/，够不到
# app 包（脚本执行的 cwd 不进 sys.path），故显式补上 backend 根。否则会命中 editable install
# 指向的**另一个** checkout 的 app 包（口径就错了）。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.infrastructure import schema_drift as _drift  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_ENV_ERROR = 0, 1, 2

# 库名白名单：只允许测试库（qa_metadata_test 与 qa_metadata_test_* 临时库）
TEST_DB_NAME_PATTERN = re.compile(r"^qa_metadata_test($|_)")

# 允许残留的「DB 多余表」名单，按库名给出。未在表内的库名 ⇒ 名单为空 ⇒ 任何多余表都算失败
ALLOWED_EXTRA_TABLES: dict[str, frozenset[str]] = {
    "qa_metadata_test": frozenset({"po_lines_sandbox"}),
    "qa_metadata": frozenset({
        "BPSUPPLIER", "PORDER", "PORDERQ", "evidence_20260930", "knowledge_claim_20260930",
        "ontology_class_pre_pruning_20260918", "session_message_bak_20260930",
        "session_query_state_20260930", "wiki_page_20260930"}),
}

# 必须为空的漂移类别（code）：覆盖「ORM 漏声明」与「ORM 声明了 DB 没有」两个方向；仅「DB 多余表」
# 允许残留（且要落在允许名单里）。
MUST_BE_EMPTY_CODES = (
    "alembic", "missing_table", "missing_column", "missing_index",
    "index_mismatch", "extra_column", "extra_index",
)
ALL_CODES = MUST_BE_EMPTY_CODES[:3] + ("extra_table",) + MUST_BE_EMPTY_CODES[3:]

# 语义特殊、必须正向断言存在的两条索引
KEY_UNIQUE_INDEX = "uq_metric_source_triple"
KEY_PARTIAL_INDEX = "ix_evaluation_report_status_running"
# partial 索引谓词里必须出现的**等值**子串。不能用裸 "RUNNING"：PG 把 `IN (...)` 渲染成
# `= ANY (ARRAY['PENDING'::text, 'RUNNING'::text])`，里面同样含 "RUNNING" ⇒ 裸子串会放行错误
# 的谓词；等值形式 `= 'RUNNING'` 只在 `status = 'RUNNING'` 时出现。
KEY_PARTIAL_PREDICATE = "= 'RUNNING'"

_KEY_INDEX_NAMES = {"names": [KEY_UNIQUE_INDEX, KEY_PARTIAL_INDEX]}
_KEY_INDEX_DEFS_SQL = text(
    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = ANY(:names)"
)


def _objectsByCode(issues: list[_drift.DriftIssue]) -> dict[str, list[str]]:
    """把 drift 的聚合 issue 还原成 code → 对象名列表。
    `_formatIssues` 按 code 聚合并用 `", "` 连接；只关心**对象名**，而对象名（表名 / `表.列` /
    `表.索引`）不含逗号，按逗号切分是安全的。
    已知偏差：`index_mismatch` 的 detail 内含 list repr（如 `ORM=['actor', 'created_at']`），
    按逗号切分会切成多段 ⇒ 打印出的计数偏高。**不影响判据** —— 判据是「**为空**」而非计数，
    虚增的计数只出现在展示里，不会放过任何漂移。"""
    grouped: dict[str, list[str]] = {}
    for issue in issues:
        parts = [part.strip() for part in issue.detail.split(",") if part.strip()]
        grouped.setdefault(issue.code, []).extend(parts)
    return grouped


async def _checkKeyIndexes(engine) -> list[str]:
    """正向断言：两条关键索引在真实 DB 上确实存在且形状正确。"""
    failures: list[str] = []
    dbIndexes = await _drift._queryDbIndexes(engine)
    byName = {ix.name: ix for ixs in dbIndexes.values() for ix in ixs}
    uniqueIndex = byName.get(KEY_UNIQUE_INDEX)
    if uniqueIndex is None:
        failures.append(f"真实 DB 上没有索引 {KEY_UNIQUE_INDEX}")
    elif not uniqueIndex.isUnique or KEY_UNIQUE_INDEX not in uniqueIndex.constraintNames:
        failures.append(f"{KEY_UNIQUE_INDEX} 必须是约束支撑的唯一索引（ORM 侧才能用 UniqueConstraint）")
    async with engine.connect() as conn:
        rows = (await conn.execute(_KEY_INDEX_DEFS_SQL, _KEY_INDEX_NAMES)).mappings().all()
    defs = {row["indexname"]: row["indexdef"] for row in rows}
    runningDef = defs.get(KEY_PARTIAL_INDEX, "")
    if not runningDef:
        failures.append(f"真实 DB 上没有索引 {KEY_PARTIAL_INDEX}")
    elif KEY_PARTIAL_PREDICATE not in runningDef:
        failures.append(f"{KEY_PARTIAL_INDEX} 的定义里没有 `{KEY_PARTIAL_PREDICATE}` 等值谓词：{runningDef}")
    return failures


async def _runChecks(url: str, dbName: str) -> tuple[list[str], dict[str, list[str]]]:
    """跑 drift 检查 + 关键索引正向断言，返回 (失败项, 分类统计)。"""
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        grouped = _objectsByCode(await _drift._checkDriftAsync(engine))
        failures: list[str] = []
        uncoveredCodes = set(_drift._SEVERITY_BY_CODE) - set(ALL_CODES)
        if uncoveredCodes:
            failures.append(f"drift 工具新增了 code {sorted(uncoveredCodes)}，本门禁未覆盖 ⇒ 会静默漏网")
        for code in MUST_BE_EMPTY_CODES:
            objects = grouped.get(code, [])
            if objects:
                failures.append(f"[{code}] 非空（{len(objects)} 项）：{objects[:10]}")
        allowed = ALLOWED_EXTRA_TABLES.get(dbName, frozenset())
        for table in grouped.get("extra_table", []):
            if table not in allowed:
                failures.append(f"名单外的多余表：{table}（{dbName} 允许名单 = {sorted(allowed) or '空'}）")
        failures += await _checkKeyIndexes(engine)
    finally:
        await engine.dispose()
    return failures, grouped


def _resolveTarget(args: argparse.Namespace) -> tuple[str, str] | None:
    """解析目标库并做库名白名单检查；不合格返回 None（原因已打印）。DSN 来源：CLI 参数 →
    `TEST_DATABASE_URL`（密码只从环境变量读）。"""
    url = args.database_url or os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        print("❌ 参数错误：未指定数据库。请传 --database-url 或设置 TEST_DATABASE_URL（默认 5434 qa_metadata_test）", file=sys.stderr)
        return None
    if not url.startswith("postgresql+asyncpg://"):
        print("❌ 参数错误：DSN 必须是 postgresql+asyncpg://（项目仅 asyncpg）", file=sys.stderr)
        return None
    dbName = make_url(url).database or ""
    if not dbName:
        print("❌ 参数错误：DSN 里没有库名", file=sys.stderr)
        return None
    if TEST_DB_NAME_PATTERN.match(dbName):
        return url, dbName
    if not args.allow_non_test_db:
        print(f"❌ 目标库 {dbName} 不在测试库白名单（{TEST_DB_NAME_PATTERN.pattern}）。确需只读巡检生产同构库时加 --allow-non-test-db。", file=sys.stderr)
        return None
    print(f"⚠️  已显式放行非测试库：{dbName}（只读巡检模式）")
    return url, dbName


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Schema SSOT 对齐真实数据门禁（只读；退出码 0/1/2）")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--allow-non-test-db", action="store_true", help="允许指向非 qa_metadata_test* 的库（如生产同构库 qa_metadata）。默认关闭；只放宽库名白名单，不放宽任何断言，也不改变只读性。")
    args = parser.parse_args(argv)

    target = _resolveTarget(args)
    if target is None:
        return EXIT_ENV_ERROR
    url, dbName = target

    print(f"target.db        = {dbName}")
    print(f"target.allowlist = {sorted(ALLOWED_EXTRA_TABLES.get(dbName, frozenset()))}")

    try:
        failures, grouped = asyncio.run(_runChecks(url, dbName))
    except Exception as exc:  # noqa: BLE001 —— 环境类错误一律归到退出码 2
        print(f"❌ 连接/执行数据库失败：{exc}", file=sys.stderr)
        return EXIT_ENV_ERROR

    for code in ALL_CODES:
        print(f"drift.{code:<16}= {len(grouped.get(code, []))}")

    if failures:
        print("\n❌ 失败项：")
        for failure in failures:
            print(f"  - {failure}")
        return EXIT_FAILED

    print("\n✅ Schema SSOT 对齐真实数据验证通过（只读，未改动任何对象）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
