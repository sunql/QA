"""语义发布：把当前治理表编译成一份快照并激活（P2 / D12 工具）。

用途
----
发布链路（编译 → 校验 → 激活）此前**从未在真实数据上端到端跑过**：
``semantic_release`` 表 0 行。影子比对（D13）需要一行 ACTIVE 快照才有意义
—— 没有它，「快照侧」是空的，比对结果不是「差异 0」而是「没东西可比」。

本脚本把这条链路做成可复现的一次操作：治理表变了就重跑一次，拿到新的
ACTIVE 快照。

⚠️ **调用契约**（与 ``scripts/kpi_metric_db_guard.py`` + ``scripts/db_snapshot_guard.py`` 同源，
别各写一份）：

- 目标库必须显式给出（``--database-url`` 或 ``--database-url-env``）——
  **不读** ``DATABASE_URL`` / ``TEST_DATABASE_URL``；**口令只走环境变量**
  （``--database-url`` 里带口令 ⇒ 直接拒绝：口令会进 ``ps`` / shell 历史 /
  CI 日志）；
- DSN 的 **query string 不许改连接目标**（``?host=`` / ``?port=`` / ``?database=``
  一律拒绝）：asyncpg 方言会把 query 合并进连接参数 ⇒ 备份与写入会落到
  **不同的服务器** ⇒ 正式库写入没有回滚点；
- 目标库是 ``qa_metadata``（dev 与 prod 同名）⇒ 必须显式 ``--allow-prod-write``；
- 写正式库前**自动完成当日备份**（只备份本脚本会写的表：
  ``semantic_release`` + ``audit_log``），
  备份不齐 ⇒ **拒绝写**（没有回滚点就不写）；
- 真正连上的库会用 ``SELECT current_database()`` **复核**（DSN 的 query string
  能覆盖驱动实际连接的库名，路径上解析出来的库名不作数）；
- ``--dry-run`` 只编译 + 校验并打印摘要，**不写库**。

用法::

    # 隔离测试库（**口令走环境变量**，不进 argv）
    export QA_METADATA_DSN='postgresql+asyncpg://user:pw@localhost:5434/qa_metadata_test_semantic'
    ./.venv/bin/python scripts/semantic_publish_snapshot.py \\
        --database-url-env QA_METADATA_DSN \\
        --version v-p2-d12

    # dev（qa_metadata 同名库）：必须显式 opt-in + 自动备份
    ./.venv/bin/python scripts/semantic_publish_snapshot.py \\
        --allow-prod-write \\
        --database-url-env QA_METADATA_DSN \\
        --version v-p2-d12

退出码：0 = 成功；1 = 发布闸门阻断；2 = 参数 / 守卫 / 备份 / 版本冲突等前置失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.exc import DBAPIError, OperationalError  # noqa: E402

from scripts.db_snapshot_guard import (  # noqa: E402
    assertSameDatabase,
    buildSessionFactory,
    connectionErrorExit,
    ensureBackups,
    maskDatabaseUrl,
    resolveDatabaseUrl,
    scrubSecrets,
)
from scripts.kpi_metric_db_guard import (  # noqa: E402
    PROD_DB_NAME,
    assertSafeTarget,
)

# 本脚本会写的表 ⇒ 备份范围就这几张。
# ⚠️ ``audit_log`` 也要在备份范围内：发布链路每次状态迁移都写一行审计
# （``semantic_release_service._writeAudit``）。只备份 semantic_release ⇒
# 回滚点漏掉了脚本自己写下的那批行 ⇒ 「备份范围 == 写入范围」这条不变量
# 不成立。
_BACKUP_TABLES = ("semantic_release", "audit_log")
_DEFAULT_ACTOR = "semantic-publish-dev"


async def _run(args: argparse.Namespace, databaseUrl: str, targetDb: str) -> int:
    """跑完整链路并打印证据。返回进程退出码。"""
    factory = buildSessionFactory(databaseUrl)
    try:
        async with factory() as session:
            # 守卫放行的库名必须等于会话**真正连的**库。
            await assertSameDatabase(session, targetDb, databaseUrl)

            if args.dry_run:
                return await _dryRun(session)
            return await _publish(session, args, databaseUrl)
    except (OperationalError, OSError, DBAPIError, TypeError, ValueError) as exc:
        # 连接 / 驱动类失败：统一转成打码后的退出，不吐裸 traceback（可能回显 DSN）。
        # TypeError / ValueError 也要接：驱动参数非法（如 SSL 模式不认）时抛的是
        # 这两个，不是 DBAPIError ⇒ 否则裸 traceback 取代打码后的干净退出。
        raise connectionErrorExit(exc, databaseUrl) from exc
    finally:
        await factory.kw["bind"].dispose()


async def _publish(session, args: argparse.Namespace, databaseUrl: str) -> int:
    """发布链路：起草 → 过闸门 → 校验 → 激活。返回进程退出码。"""
    from app.domain.exceptions import ConflictError, DomainError
    from app.services.semantic_release_service import semanticReleaseService

    try:
        operation = await semanticReleaseService.createDraft(
            session,
            version=args.version,
            actor=args.actor,
            departments=tuple(args.department or ()),
            note=args.note,
        )
    except ConflictError as exc:
        print(
            f"❌ 版本 {args.version!r} 已存在（{scrubSecrets(str(exc), databaseUrl)}）"
            " ⇒ 换一个新版本号再发。"
        )
        return 2
    except DomainError as exc:
        # 起草阶段的其它领域失败（体量超限 / 校验不过 / 域不存在…）⇒ 未写库。
        print(
            f"❌ 起草失败（{scrubSecrets(str(exc), databaseUrl)}）"
            " ⇒ 未写库（退出码 2 = 前置失败，与闸门阻断区分）。"
        )
        return 2

    if operation.blockers:
        print(f"❌ 发布闸门阻断（{len(operation.blockers)} 条），不继续激活：")
        for blocker in operation.blockers:
            print(f"   - {blocker}")
        return 1
    for warning in operation.warnings:
        print(f"⚠️ 闸门告警：{warning}")

    releaseId = operation.release.id
    try:
        validated = await semanticReleaseService.markValidated(
            session, releaseId, actor=args.actor,
            departments=tuple(args.department or ()),
        )
        activated = await semanticReleaseService.activate(
            session, releaseId, actor=args.actor,
            departments=tuple(args.department or ()),
        )
    except DomainError as exc:
        # 状态机拒绝（非法转换 / 并发激活冲突 / 校验失败）⇒ **前置失败**，
        # 不是「发布闸门阻断」。两者的退出码必须分开：未捕获异常退出会返回 1，
        # 而 1 在本脚本的定义是「闸门阻断」⇒ 并发冲突会冒充成闸门阻断。
        print(
            "❌ 发布链路被拒绝（"
            f"{scrubSecrets(str(exc), databaseUrl)}"
            "）⇒ 快照未激活（退出码 2 = 前置失败，与闸门阻断区分）。"
        )
        return 2
    _summarize(activated.release, validated.release.manifest_json, databaseUrl)
    return 0


def _summarize(row, manifest: dict, databaseUrl: str) -> None:
    """打印发布证据（快照体量 / 类数 / join 数 / 目标库）。"""
    from app.domain.semantic_manifest_schema import canonicalManifestJson

    canonical = canonicalManifestJson(manifest)
    print("=" * 72)
    print(f"发布完成：id={row.id} version={row.version} status={row.status}")
    print(f"  manifest_hash = {row.manifest_hash}")
    print(f"  manifest bytes= {len(canonical.encode('utf-8'))}")
    print(f"  快照类数      = {len(manifest.get('classes') or [])}")
    print(f"  快照 join 数  = {len(manifest.get('joins') or [])}")
    print(f"  目标库        = {maskDatabaseUrl(databaseUrl)}")
    print("=" * 72)


async def _dryRun(session) -> int:
    """只编译 + 校验：打印快照摘要与闸门结论，不写库。"""
    from app.domain.semantic_manifest_schema import canonicalManifestJson, validateManifest
    from app.services.semantic_compiler import compileManifest
    from app.services.semantic_release_service import evaluatePublishGate

    manifest = await compileManifest(session)
    validateManifest(manifest)
    canonical = canonicalManifestJson(manifest)
    gate = evaluatePublishGate(manifest)
    print("=" * 72)
    print(f"dry-run（不写库）：快照 {len(canonical.encode('utf-8'))} 字节")
    print(f"  类 {len(manifest.get('classes') or [])} / "
          f"属性 {sum(len(c.get('properties') or []) for c in manifest.get('classes') or [])} / "
          f"join {len(manifest.get('joins') or [])} / "
          f"指标 {len(manifest.get('metrics') or [])} / "
          f"KPI {len(manifest.get('kpis') or [])}")
    print(f"  闸门阻断 {len(gate.blockers)} 条 / 告警 {len(gate.warnings)} 条")
    for blocker in gate.blockers:
        print(f"   ⛔ {blocker}")
    for warning in gate.warnings:
        print(f"   ⚠️  {warning}")
    print("=" * 72)
    return 0 if gate.canPublish else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="语义发布：编译治理表并激活一份快照")
    parser.add_argument(
        "--database-url", default=None,
        help="目标库 DSN（显式；口令建议改用 --database-url-env）",
    )
    parser.add_argument(
        "--database-url-env", default=None,
        help="从该环境变量读目标库 DSN（口令不进 argv）",
    )
    parser.add_argument("--version", default="v-p2-d12", help="发布版本号（全局作用域唯一）")
    parser.add_argument("--actor", default=_DEFAULT_ACTOR, help="审计 actor")
    parser.add_argument("--department", action="append", help="审计部门（可重复）")
    parser.add_argument("--note", default=None, help="发布备注")
    parser.add_argument(
        "--allow-prod-write", action="store_true",
        help="允许以正式库 qa_metadata（prod 同源）为目标；写操作仍受 --dry-run 控制",
    )
    parser.add_argument("--dry-run", action="store_true", help="只编译校验，不写库")
    args = parser.parse_args()

    databaseUrl = resolveDatabaseUrl(args.database_url, args.database_url_env)
    # 目标库是正式库（prod 同源）时必须显式 opt-in —— **dry-run 也不例外**：
    # opt-in 是「我承认目标是正式库」的人工闸，不是「允许写」的开关。
    targetDb = assertSafeTarget(databaseUrl, allowProdWrite=args.allow_prod_write)
    print(f"目标库：{targetDb} @ {maskDatabaseUrl(databaseUrl)}")

    if not args.dry_run and targetDb == PROD_DB_NAME:
        backups = ensureBackups(
            databaseUrl,
            _BACKUP_TABLES,
            args.version,
            _backupDirectory(),
        )
        for path in backups:
            print(f"✅ 已备份 {path.name}：{path}（{path.stat().st_size} bytes）")

    try:
        return asyncio.run(_run(args, databaseUrl, targetDb))
    except SystemExit:
        raise
    except (OperationalError, OSError, DBAPIError, TypeError, ValueError) as exc:
        raise connectionErrorExit(exc, databaseUrl) from exc


def _backupDirectory() -> Path:
    """备份目录：系统临时目录（不放仓库内，避免 dump 被顺手提交 / 归档）。"""
    import tempfile

    return Path(tempfile.gettempdir()) / "semantic_release_backups"


if __name__ == "__main__":
    raise SystemExit(main())
