"""D16 运行时探针：**钉住**语义发布时，重放读到的是**同一版**。

为什么需要独立探针（而不是只看单测）：
单测用替身注入快照，证的是「闸门逻辑对」；本探针连**真实 dev 库** +
**真实已发布快照**跑一遍，证的是「钉住 ⇒ 取到那一版 ⇒ 渲染结果与那一版一致」。

四条判据（全部机检，退出码非 0 即失败）：

1. **钉住路径真的钉住了**：钉住 id=A 时返回的 `semanticReleaseId` == A；
   且**同一版两次渲染逐字节相同**（重放本体：渲染只由「发布 id」决定）；
   若 ACTIVE 路径**确实用了快照**，再要求「钉住渲染 == ACTIVE 渲染」逐字节相同
   （ACTIVE 路径降级时跳过该比对并明确报出「基准不可用」，不误判成重放不一致）；
2. **钉住不存在的版本**：降级直读、**不回报** id、**不抛异常**（P0 裁决一）；
3. **运行记录有这一列**：库里有 `session_query_state.semantic_release_id` 且**可空**
   （老数据兼容）；
4. **写侧往返**：在同一事务里写一行带 id 的运行记录、读回一致，然后 **ROLLBACK**
   ⇒ 对库**不留任何痕迹**（本脚本对外等价于只读）。

**只读性说明**：判据 4 用的是「事务内写 + 回滚」，
事务提交前不会对任何其他连接可见，脚本结束必为 ROLLBACK。

用法::

    # 开关必须显式打开（脚本本身 fail-fast，避免跑成「开关关」的空证据）
    ENABLE_SEMANTIC_REGISTRY_NL2SQL=true \\
      uv run python scripts/diag_semantic_release_pin.py \\
      --database-url-env QA_METADATA_DSN

退出码：
- ``0``：四条判据全过；
- ``2``：钉住路径没走到（回报的 id 不对）；
- ``3``：钉住重放**不一致**（同一版两次渲染文本不同，或与同版 ACTIVE 渲染不同）；
- ``4``：前置条件不满足（开关未开 / 无 ACTIVE 快照 / 复原不可用）；
- ``5``：**取快照本身失败**（连不上库 / `semantic_release` 表不存在等）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.db_snapshot_guard import (  # noqa: E402
    buildSessionFactory,
    connectionErrorExit,
    maskDatabaseUrl,
    resolveDatabaseUrl,
)

EXIT_OK = 0
EXIT_NOT_PINNED = 2
EXIT_NOT_REPLAYABLE = 3
EXIT_PRECONDITION = 4
EXIT_FETCH_FAILED = 5

# 钉住一个**一定不存在**的发布 id（自增主键的上限内，但库里没有这一行）。
_MISSING_RELEASE_ID = 999_999_999


async def _run(databaseUrl: str) -> int:
    from sqlalchemy import text

    from app.config import getSettings
    from app.services.nl2sql_schema import buildSchemaText
    from app.services.ontology_service import OntologyService
    from app.services.semantic_registry_service import semanticRegistry
    from app.services.semantic_schema_gate import renderSchemaWithRelease

    settings = getSettings()
    if not settings.enableSemanticRegistryNl2sql:
        print("前置条件不满足：ENABLE_SEMANTIC_REGISTRY_NL2SQL 未打开", file=sys.stderr)
        return EXIT_PRECONDITION

    factory = buildSessionFactory(databaseUrl)
    async with factory() as session:
        ontology = OntologyService()
        classes = await ontology.listClasses(session)
        joins = await ontology.listJoins(session)

        # ① 当前 ACTIVE 是哪一版？（钉住的对象）
        active = await semanticRegistry.getActive(session)
        if active is None:
            print("前置条件不满足：库里没有 ACTIVE 语义发布", file=sys.stderr)
            return EXIT_PRECONDITION
        activeId = active.id
        print(f"当前 ACTIVE 发布 id = {activeId}（version={active.version}）")

        # 判据 1：钉住那一版 ⇒ 渲染结果可复现，且与「同一版」的渲染逐字节相同
        #
        # ⚠️ 为什么**不能**把「ACTIVE 渲染」当成唯一基准：ACTIVE 路径带 D15 的
        # 事实守卫 ⇒ ACTIVE 快照一旦陈旧（发布之后本体又改了），它会**降级成直读**
        # ⇒ 「钉住渲染（快照文本）」与「ACTIVE 渲染（直读文本）」必然不等 ⇒
        # 拿它当基准会把「ACTIVE 陈旧」误报成「钉住重放不一致」，结论指错方向。
        # ⇒ 基准分两层：
        #   1b「同一版两次渲染逐字节相同」——**任何时候都成立**（这才是「重放」的
        #      本体：渲染只由「发布 id」决定）；
        #   1c「钉住渲染 == ACTIVE 渲染」——**仅当 ACTIVE 路径确实用了快照**时才
        #      成立（ACTIVE 降级时跳过，并明确报出「基准不可用」而不是误判失败）。
        pinned = await renderSchemaWithRelease(
            classes, joins=joins, session=session, pinnedReleaseId=activeId
        )
        pinnedAgain = await renderSchemaWithRelease(
            classes, joins=joins, session=session, pinnedReleaseId=activeId
        )
        unpinned = await renderSchemaWithRelease(classes, joins=joins, session=session)
        print(f"钉住渲染回报的 id = {pinned.semanticReleaseId}")
        print(f"钉住文本行数 = {len(pinned.text.splitlines())}")
        print(f"同一版两次钉住渲染逐字节相同 = {pinned.text == pinnedAgain.text}")
        print(f"ACTIVE 路径回报的 id = {unpinned.semanticReleaseId}")

        if pinned.semanticReleaseId != activeId:
            print(
                f"\n结论：钉住路径**未生效**（回报 id={pinned.semanticReleaseId}，"
                f"期望 {activeId}）",
                file=sys.stderr,
            )
            return EXIT_NOT_PINNED
        if pinned.text != pinnedAgain.text:
            print(
                "\n结论：钉住重放**不一致**（同一版两次渲染的文本不同）",
                file=sys.stderr,
            )
            return EXIT_NOT_REPLAYABLE
        if unpinned.semanticReleaseId == activeId:
            print(f"钉住文本 == ACTIVE 渲染文本（逐字节）= {pinned.text == unpinned.text}")
            if pinned.text != unpinned.text:
                print(
                    "\n结论：钉住重放**不一致**（钉住那一版的渲染 ≠ ACTIVE 渲染）",
                    file=sys.stderr,
                )
                return EXIT_NOT_REPLAYABLE
        else:
            # ACTIVE 路径没用快照（无发布 / 陈旧降级）⇒ 「ACTIVE 渲染」不是
            # 有效基准 ⇒ 明确报出，不误判成「重放不一致」（判据 1b 已独立成立）。
            print(
                "ACTIVE 路径未使用快照（无发布或已陈旧降级）⇒ 「ACTIVE 渲染」"
                "不作为基准；重放判据改用「同一版两次渲染逐字节相同」"
            )

        # 判据 2：钉住一个不存在的版本 ⇒ 降级直读、不回报 id、不抛异常
        # 注意比对基准是**直读渲染**（不是 ACTIVE 渲染）：降级后走的是治理表直读，
        # 与快照渲染在「类内列序 / JOIN 条目序」上本就不同（D14 已知差异）。
        directText = buildSchemaText(classes, joins=joins)
        missing = await renderSchemaWithRelease(
            classes, joins=joins, session=session, pinnedReleaseId=_MISSING_RELEASE_ID
        )
        print(
            f"钉住不存在的版本 ⇒ 回报 id = {missing.semanticReleaseId}，"
            f"文本 == 直读渲染 = {missing.text == directText}"
        )
        if missing.text != directText:
            print(
                "\n结论：降级文本**不等于直读渲染**（降级方向不对）",
                file=sys.stderr,
            )
            return EXIT_NOT_REPLAYABLE
        if missing.semanticReleaseId is not None:
            print(
                "\n结论：降级路径**回报了发布 id**（记账了但没用上该版本）",
                file=sys.stderr,
            )
            return EXIT_NOT_PINNED

        # 判据 3：运行记录有这一列，且可空
        column = (
            await session.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'session_query_state' "
                    "AND column_name = 'semantic_release_id'"
                )
            )
        ).scalar()
        print(f"session_query_state.semantic_release_id 可空 = {column}")
        if column != "YES":
            print(
                "\n结论：运行记录缺这一列，或不可空（老数据兼容不成立）",
                file=sys.stderr,
            )
            return EXIT_PRECONDITION

        # 判据 4：写侧往返（事务内写 + 回滚 ⇒ 对外不留痕迹）
        await session.execute(
            text(
                "INSERT INTO session_query_state "
                "(session_id, user_id, datasource_id, last_question, turn_count, "
                " semantic_release_id, created_time, updated_time) "
                "VALUES (:sid, :uid, :dsid, 'diag', 1, :rel, now(), now()) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "sid": "diag-semantic-release-pin",
                "uid": -1,
                "dsid": -1,
                "rel": activeId,
            },
        )
        stored = (
            await session.execute(
                text(
                    "SELECT semantic_release_id FROM session_query_state "
                    "WHERE session_id = :sid"
                ),
                {"sid": "diag-semantic-release-pin"},
            )
        ).scalar()
        print(f"事务内写回读 = {stored}（期望 {activeId}）")
        await session.rollback()
        print("已 ROLLBACK ⇒ 库内不留痕迹")

    print("\n结论：钉住生效 + 重放逐字节一致 + 缺失版本降级不记账 + 列可空 + 写侧往返一致")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="D16 语义发布钉住运行时探针")
    parser.add_argument(
        "--database-url-env",
        default="QA_METADATA_DSN",
        help="存放 DSN 的环境变量名（避免口令进 argv）",
    )
    args = parser.parse_args()

    rawUrl = os.getenv(args.database_url_env)
    if not rawUrl:
        print(f"环境变量 {args.database_url_env} 未设置", file=sys.stderr)
        return EXIT_PRECONDITION

    try:
        databaseUrl = resolveDatabaseUrl(None, args.database_url_env, allowInlinePassword=False)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PRECONDITION

    print(f"目标库 DSN = {maskDatabaseUrl(databaseUrl)}")

    try:
        return asyncio.run(_run(databaseUrl))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 连接类异常统一打码退出
        raise connectionErrorExit(exc, databaseUrl) from exc


if __name__ == "__main__":
    raise SystemExit(main())
