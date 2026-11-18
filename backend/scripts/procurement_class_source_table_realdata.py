"""采购域本体类**物理表绑定**（迁移 0153 校准）真实数据门禁。

## 为什么需要这个门禁（2026-11-18 实测事故链）

迁移 ``0039``/``0040`` 把采购域 5 个本体类的 ``source_table`` 绑到了 **DWD_* 词表**
（``DWD_SUPPLIER`` / ``DWD_MATERIAL`` / ``DWD_PURCHASE_ORDER`` /
``DWD_GOODS_RECEIPT`` / ``DWD_INCOMING_INSPECTION``），而本体 SSOT
（``backend/seed_ontology.py``）绑的是 **ERP 业务表**。两个种子在同一个
``class_name`` 上写不同的 ``source_table`` ⇒ 三条实测后果：

1. NL2SQL 按 ``class.source_table`` 取物理表 ⇒ 指到一张**不存在的表**
   （本地栈只有 ``BPSUPPLIER`` / ``PORDER`` / ``PORDERQ`` 三张真实 ERP 表）；
2. ``seed_ontology.seed()`` 在干净库上**直接崩**：唯一约束
   ``uq_ontology_class_name_version`` = UNIQUE(class_name, version)，
   而 ``_seedClasses`` 按 **source_table** 查重 ⇒ 迁移已占用 class_name
   ⇒ seed 插同名类 ⇒ 唯一约束违例；
3. 测试侧两个夹具种子分叉 ⇒ 用例 verdict 取决于夹具顺序。

迁移 ``0153`` 把绑定校准到 ERP 词表。**本门禁证明它真的生效**。

## 本门禁证明什么（都必须在**干净库**上跑完整迁移链才作数）

1. **迁移链真值** —— ``alembic upgrade head`` 之后：
   - 4 个类的 ``source_table`` 是 ERP 业务表词表；
   - ``IncomingInspection`` 的 ``source_table`` 是 ``NULL``（来料检验无物理表）；
   - ``object_type`` / ``version`` 也必须对（类级内容契约）。
2. **seed_ontology 不再撞唯一约束** —— 在同一干净库上跑
   ``seed_ontology._seedClasses``（**只跑类种子**，不跑全量 seed ⇒ 不碰 Neo4j）：
   - 不抛唯一约束违例（校准前实测抛
     ``duplicate key ... (class_name, version)=(Supplier, 1)``）；
   - 4 个类**复用**迁移留下的行（行数不增、id 不变）⇒
     按 ``source_table`` 查重能命中。

共享测试库**证明不了**这两条：它每个用例前 TRUNCATE 全库 ⇒ 迁移种子行必被清掉
⇒ 只有本脚本（自建干净库）能作数。

## 只写一次性干净库

脚本自己 ``CREATE DATABASE`` → 跑迁移链 → 断言 → ``DROP DATABASE``。
**不碰任何既有库** ⇒ 可反复执行。

用法（目标 PG 默认取 ``TEST_DATABASE_URL``；密码只从环境变量读）：
    cd backend
    TEST_DATABASE_URL='postgresql+asyncpg://<user>:<pwd>@127.0.0.1:5434/qa_metadata_test' \
      python3 scripts/procurement_class_source_table_realdata.py

退出码：0 通过 / 1 断言失败 / 2 环境错（含：建库失败、清理失败 —— 后者即使断言
全过也报 2，避免一行「[OK]」掩盖「库里还留着 scratch 库」）。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import URL, make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_ENV_ERROR = 0, 1, 2

# 一次性干净库名；须落在测试库白名单内（^qa_metadata_test($|_)）
SCRATCH_DB_NAME = "qa_metadata_test_purcclass"
TEST_DB_NAME_PATTERN = re.compile(r"^qa_metadata_test($|_)")


def _scratchDbNameError() -> str | None:
    """自校验 SCRATCH_DB_NAME（本脚本会对它 `DROP DATABASE`）。

    白名单在这里**再查一遍**、而不是只查 TEST_DATABASE_URL：这个常量才是
    真正被 DROP/CREATE 的标识符，一旦被改成 `qa_metadata` 之类，
    下一个跑门禁的人会删掉一个正式库。
    """
    if not TEST_DB_NAME_PATTERN.match(SCRATCH_DB_NAME):
        return (
            f"SCRATCH_DB_NAME={SCRATCH_DB_NAME!r} 不在测试库白名单内"
            f"（{TEST_DB_NAME_PATTERN.pattern}）—— 拒绝执行（本脚本会 DROP 它）"
        )
    return None


# 规范绑定 = seed_ontology.CLASSES 的 source_table（本体 SSOT）
CANONICAL: dict[str, tuple[str | None, str]] = {
    "Supplier": ("BPSUPPLIER", "Master"),
    "ItemMaster": ("ITMMASTER", "Master"),
    "PurchaseOrder": ("PORDER", "Transaction"),
    "Receipt": ("PRECEIPT", "Transaction"),
    "IncomingInspection": (None, "Transaction"),
}


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}", file=sys.stderr)
    return EXIT_FAILED


def _envError(msg: str) -> int:
    print(f"[ENV ] {msg}", file=sys.stderr)
    return EXIT_ENV_ERROR


def _resolveTarget() -> URL:
    """从 TEST_DATABASE_URL（回退 DATABASE_URL）解析**已校验**的目标 URL。

    返回 `URL` 对象而不是拼好的字符串：后续用 `URL.set()/render_as_string()`
    派生连接串 —— 口令里的 `@` / `/` / `:` 会由 SQLAlchemy 正确转义，
    手拼 f-string 会把它们当成 DSN 语法（口令含 `@` 时连错主机）。
    """
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError(
            "未设置 TEST_DATABASE_URL（回退 DATABASE_URL）："
            "需要目标测试 PG 的连接串（含库名 qa_metadata_test*）"
        )
    url = make_url(raw)
    dbName = url.database or ""
    if not TEST_DB_NAME_PATTERN.match(dbName):
        raise RuntimeError(
            f"目标库名 {dbName!r} 不在测试库白名单内（^qa_metadata_test($|_)）"
        )
    if not url.username:
        raise RuntimeError("TEST_DATABASE_URL 缺用户名")
    return url.set(host=url.host or "localhost", port=url.port or 5432)


def _scratchUrl(target: URL) -> URL:
    """同主机/端口/凭据 + 一次性库名。"""
    return target.set(database=SCRATCH_DB_NAME)


def _adminUrl(target: URL) -> URL:
    """同主机/端口/凭据 + `postgres` 维护库（建库/删库只能从这里发起）。"""
    return target.set(database="postgres")


async def _terminateScratchConnections(conn) -> None:  # noqa: ANN001 —— AsyncConnection
    """掐断 scratch 库上的连接：DROP DATABASE 会因「其他会话仍连着」失败。"""
    await conn.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :name AND pid <> pg_backend_pid()"
        ),
        {"name": SCRATCH_DB_NAME},
    )


async def _createScratchDb(target: URL) -> None:
    """在目标 PG 上建一次性干净库（先 DROP 掉同名残留，保证可反复执行）。"""
    engine = create_async_engine(_adminUrl(target), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # 上次跑崩留下的连接会让 DROP 失败 ⇒ 先掐连接。
            await _terminateScratchConnections(conn)
            # 标识符不能 bind；已由 `_scratchDbNameError()` 钉在白名单内。
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB_NAME}"'))
            await conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB_NAME}"'))
    finally:
        await engine.dispose()


async def _dropScratchDb(target: URL) -> None:
    engine = create_async_engine(_adminUrl(target), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await _terminateScratchConnections(conn)
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB_NAME}"'))
    finally:
        await engine.dispose()


def _runAlembicUpgradeHead(scratchUrl: URL) -> None:
    """在干净库上真跑迁移链（``alembic/env.py`` 只读 DATABASE_URL）。"""
    env = dict(os.environ)
    # `str(URL)` 会隐藏口令 ⇒ 必须显式 render（否则 alembic 连不上）
    env["DATABASE_URL"] = scratchUrl.render_as_string(hide_password=False)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head 失败：\n" + (result.stderr or result.stdout)[-3000:]
        )


async def _assertMigrationChainState(scratchUrl: URL) -> list[str]:
    """断言迁移链落在规范绑定上；返回失败描述列表（空 = 全过）。"""
    failures: list[str] = []
    engine = create_async_engine(scratchUrl)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT class_name, source_table, object_type, version
                        FROM ontology_class
                        WHERE class_name = ANY(:names)
                        """
                    ),
                    {"names": list(CANONICAL)},
                )
            ).all()
        found = {r[0]: r for r in rows}
        for className, (sourceTable, objectType) in CANONICAL.items():
            row = found.get(className)
            if row is None:
                failures.append(f"{className}: 迁移链没落下这个类")
                continue
            if row[1] != sourceTable:
                failures.append(
                    f"{className}.source_table = {row[1]!r}，期望 {sourceTable!r}"
                    "（DWD_* 词表 ⇒ 迁移 0153 没生效）"
                )
            if row[2] != objectType:
                failures.append(f"{className}.object_type = {row[2]!r}，期望 {objectType!r}")
            if row[3] != 1:
                failures.append(f"{className}.version = {row[3]!r}，期望 1")
        # 反向断言：不允许残留 DWD_* 词表
        async with engine.connect() as conn:
            legacy = (
                await conn.execute(
                    text(
                        """
                        SELECT class_name, source_table FROM ontology_class
                        WHERE source_table LIKE 'DWD\\_%'
                        """
                    )
                )
            ).all()
        for className, sourceTable in legacy:
            failures.append(
                f"{className}.source_table 仍是 {sourceTable!r}（DWD_* 旧词表）"
            )
    finally:
        await engine.dispose()
    return failures


async def _assertSeedOntologyReuses(scratchUrl: URL) -> list[str]:
    """在干净库上跑 ``seed_ontology._seedClasses``：不崩 + 复用迁移留下的行。

    只跑类种子（不跑全量 ``seed()``）⇒ **不碰 Neo4j**（全量 seed 会同步图谱）。
    """
    failures: list[str] = []
    import seed_ontology

    engine = create_async_engine(scratchUrl)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.connect() as conn:
            before = (
                await conn.execute(
                    text(
                        "SELECT class_name, id FROM ontology_class "
                        "WHERE class_name = ANY(:names)"
                    ),
                    {"names": list(CANONICAL)},
                )
            ).all()
        idsBefore = {r[0]: r[1] for r in before}

        # monkeypatch seed_ontology 的引擎入口（与 app/tests/unit/conftest.py 同手法）
        seed_ontology.getEngine = lambda: engine  # type: ignore[assignment]
        seed_ontology.getSessionFactory = lambda: factory  # type: ignore[assignment]
        try:
            async with factory() as session:
                cid = await seed_ontology._seedClasses(session)
        except Exception as exc:  # noqa: BLE001 —— 唯一约束违例正是本门禁要抓的
            failures.append(
                f"seed_ontology._seedClasses 抛异常（校准前实测："
                f"duplicate key ... (class_name, version)=(Supplier, 1)）：{exc!r}"
            )
            return failures

        # 复用校验：4 个类应命中迁移留下的行（id 不变、行数不增）
        for sourceTable in ("BPSUPPLIER", "ITMMASTER", "PORDER", "PRECEIPT"):
            if sourceTable not in cid:
                failures.append(f"_seedClasses 返回的映射里没有 {sourceTable}")
        async with engine.connect() as conn:
            after = (
                await conn.execute(
                    text(
                        "SELECT class_name, id FROM ontology_class "
                        "WHERE class_name = ANY(:names)"
                    ),
                    {"names": list(CANONICAL)},
                )
            ).all()
        idsAfter = {r[0]: r[1] for r in after}
        for className, oldId in idsBefore.items():
            newId = idsAfter.get(className)
            if newId != oldId:
                failures.append(
                    f"{className}: seed 前 id={oldId}，后 id={newId}"
                    "（未复用迁移留下的行 ⇒ _seedClasses 按 source_table 查重没命中）"
                )
    finally:
        await engine.dispose()
    return failures


async def _runChecks(scratchUrl: URL) -> tuple[int, str | None]:
    """跑全部门禁检查；返回 `(退出码, 失败消息)`（成功时消息为 None）。"""
    try:
        _runAlembicUpgradeHead(scratchUrl)
    except RuntimeError as exc:
        return EXIT_ENV_ERROR, str(exc)

    failures = await _assertMigrationChainState(scratchUrl)
    if failures:
        return EXIT_FAILED, "迁移链绑定不合规范：\n  - " + "\n  - ".join(failures)

    failures = await _assertSeedOntologyReuses(scratchUrl)
    if failures:
        return EXIT_FAILED, "seed_ontology 与迁移链未对齐：\n  - " + "\n  - ".join(failures)
    return EXIT_OK, None


async def _main() -> int:
    nameError = _scratchDbNameError()
    if nameError:
        return _envError(nameError)
    try:
        target = _resolveTarget()
    except RuntimeError as exc:
        return _envError(str(exc))

    scratchUrl = _scratchUrl(target)
    print(f"[INFO] 目标 PG      = {target.host}:{target.port}")
    print(f"[INFO] 一次性干净库 = {SCRATCH_DB_NAME}")

    try:
        await _createScratchDb(target)
    except Exception as exc:  # noqa: BLE001
        return _envError(f"创建一次性干净库失败：{exc!r}")

    cleanupError: Exception | None = None
    try:
        result, message = await _runChecks(scratchUrl)
    finally:
        try:
            await _dropScratchDb(target)
        except Exception as exc:  # noqa: BLE001
            cleanupError = exc

    # 清理失败必须**非零退出**：否则干净的一行「[OK]」会掩盖「库里还留着一个
    # 建好的 scratch 库」，下一个人会以为环境是干净的。
    if cleanupError is not None:
        print(
            f"[ERROR] 清理一次性干净库失败（可手工 DROP DATABASE "
            f'"{SCRATCH_DB_NAME}"）：{cleanupError!r}',
            file=sys.stderr,
        )
        if result == EXIT_OK:
            return EXIT_ENV_ERROR

    if message is not None:
        if result == EXIT_FAILED:
            return _fail(message)
        return _envError(message)

    print("[OK] 采购域本体类物理表绑定 = ERP 词表；seed_ontology 复用迁移行，无唯一约束冲突")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
