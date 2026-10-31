"""A1 统一 ID 映射真实数据验证（Harness 开发 / 上线门禁）。

在**真实库**上逐项断言 A1 的四条不变式 —— 不是「回填写完了」，而是「回填的
结果满足 A1 承诺的语义」：

  1. **覆盖**（集合，不是计数）：每个存活本体类都有对应的 `CLASS` 映射。
     `id_mapping` 不是 CLASS 专用的表（`PROPERTY` / `METRIC` 行由 ontology API
     写入），软删墓碑也留着行 —— 所以「总行数 == 存活类数」在真实库上恒假。
     反过来计数相等也抓不到「漏了 102、多了 999」这种换集：集合覆盖能抓。
  2. **无悬空**：每个 `CLASS` 映射的外部 ID 都对得上一个真实本体类。
  3. **派生一致**：每一行的 `unified_id` 都等于 `obj:{business_object}:{external_id}`
     且能被 `parseUnifiedId` 反解回同样的两段。ID 是三库对齐的唯一锚，破了不会
     报错，只会让 A2/A3 查不到东西。
  4. **重跑存活（survivorship，仅 `--full`）**：给一行打上跨库落点哨兵，重跑回填，
     断言哨兵还在。只断言「重跑不报错、行数不变」时，`on_conflict_do_update` 的
     `set_` 若被改成默认，哨兵会被抹成 NULL 而所有断言依旧全绿。
  5. **幂等（仅 `--full`）**：连跑两次回填，行数不变。

探针的**事务纪律**：哨兵与重跑都在同一个**未提交**事务里，结束直接 rollback。

- 哨兵从不落盘 ⇒ 进程被杀、容器被 stop 都不会在生产留下假坐标；
- 未提交的行也读不到 ⇒ 并发的对账脚本不会把哨兵当真实落点；
- 探针持行锁 ⇒ 并发写入者等待而不是被还原覆盖；
- 失败时 rollback 一定可执行 ⇒ 不会出现「先 commit 再还原、还原失败」的死局。

默认只读（不变式 1-3），可在生产直接跑；`--full` 会写库（未提交事务），目标
判定为生产时必须加 `--allow-prod`。

为什么断言用 raw SQL 而不是 ORM：ORM identity map 在跨 commit 后可能返回旧值。
这是本仓库已付过学费的坑（`seed_business_objects` 的 `header_class_id` 回填事故）。

用法：
    cd backend
    uv run python -m scripts.id_mapping_realdata                 # 只读门禁（可上生产）
    uv run python -m scripts.id_mapping_realdata --full          # 含重跑存活 + 幂等
    uv run python -m scripts.id_mapping_realdata --full --allow-prod
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.domain.id_mapping import buildUnifiedId, parseUnifiedId
from scripts.backfill_id_mapping import (
    ALIVE_CLASS_PREDICATE,
    BUSINESS_OBJECT_CLASS,
    CORRUPT_SAMPLE_LIMIT,
    CorruptMappingError,
    assertNoCaseVariantMappings,
    assertSafeWriteTarget,
    backfillIdMappings,
    countAliveClasses,
    findCaseVariantMappings,
)

_SENTINEL = "__a1_survivorship_probe__"


class VerifyFailure(Exception):
    """一项断言不成立。"""


async def _scalar(session: AsyncSession, sql: str, **params: object) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


async def countClassMappings(session: AsyncSession) -> int:
    return await _scalar(
        session,
        "SELECT count(*) FROM id_mapping WHERE business_object = :bo",
        bo=BUSINESS_OBJECT_CLASS,
    )


async def _caseVariantHint(session: AsyncSession) -> str:
    """覆盖失败时补一句最可能的成因，省掉运维翻表。"""
    total, samples = await findCaseVariantMappings(session)
    if not total:
        return ""
    return (
        f" 另有 {total} 行 business_object 大小写不规范（如 {samples[0][0]!r}）——"
        "它们按小写命名空间存着，规范查询看不到。"
    )


async def _checkCoverage(session: AsyncSession) -> None:
    """不变式 1：每个存活本体类都有 CLASS 映射。"""
    whereClause = (
        "NOT EXISTS ("
        "SELECT 1 FROM id_mapping m "
        "WHERE m.business_object = :bo AND m.external_id = c.id::text)"
    )
    params = {"bo": BUSINESS_OBJECT_CLASS}
    missing = await _scalar(
        session,
        f"SELECT count(*) FROM ontology_class c "
        f"WHERE c.{ALIVE_CLASS_PREDICATE} AND {whereClause}",
        **params,
    )
    if missing == 0:
        return
    samples = (
        await session.execute(
            text(
                "SELECT c.id, c.class_name FROM ontology_class c "
                f"WHERE c.{ALIVE_CLASS_PREDICATE} AND {whereClause} "
                "ORDER BY c.id LIMIT :limit"
            ),
            {**params, "limit": CORRUPT_SAMPLE_LIMIT},
        )
    ).all()
    detail = "；".join(f"{row[0]}:{row[1]}" for row in samples)
    more = "" if missing <= len(samples) else f"（另有 {missing - len(samples)} 个未列出）"
    raise VerifyFailure(
        f"漏类：{missing} 个存活本体类没有 CLASS 映射（{detail}{more}）。"
        "A2/A4 拿不到这些类的统一 ID。" + await _caseVariantHint(session)
    )


async def _checkNoDangling(session: AsyncSession) -> None:
    """不变式 2：不存在指向已消失本体类的 CLASS 映射。"""
    whereClause = (
        "NOT EXISTS (SELECT 1 FROM ontology_class c WHERE c.id::text = m.external_id)"
    )
    params = {"bo": BUSINESS_OBJECT_CLASS}
    dangling = await _scalar(
        session,
        f"SELECT count(*) FROM id_mapping m "
        f"WHERE m.business_object = :bo AND {whereClause}",
        **params,
    )
    if dangling == 0:
        return
    samples = (
        await session.execute(
            text(
                "SELECT m.unified_id FROM id_mapping m "
                f"WHERE m.business_object = :bo AND {whereClause} "
                "ORDER BY m.unified_id LIMIT :limit"
            ),
            {**params, "limit": CORRUPT_SAMPLE_LIMIT},
        )
    ).all()
    detail = "；".join(repr(row[0]) for row in samples)
    more = "" if dangling <= len(samples) else f"（另有 {dangling - len(samples)} 行未列出）"
    raise VerifyFailure(
        f"悬空映射：{dangling} 个 CLASS 映射没有对应的本体类（{detail}{more}）。"
        "统一 ID 指向不存在的对象，三库对齐会查出一堆幽灵。"
    )


async def _checkDerivedIds(session: AsyncSession) -> int:
    """不变式 3：每行 unified_id 都可由其两段派生/反解。"""
    rows = (
        await session.execute(
            text("SELECT unified_id, business_object, external_id FROM id_mapping")
        )
    ).all()
    for unifiedId, businessObject, externalId in rows:
        unifiedId, businessObject, externalId = (
            str(unifiedId),
            str(businessObject),
            str(externalId),
        )
        try:
            expected = buildUnifiedId(businessObject, externalId)
            parsed = parseUnifiedId(unifiedId)
        except ValidationError as exc:
            # 老数据可能含非法分段；门禁要报 FAIL，不能自己抛校验异常
            raise VerifyFailure(
                f"派生不一致：{unifiedId!r} 的分段不合法（{exc}）。"
                "统一 ID 是三库唯一锚，破了不会报错，只会让 A2/A3 查不到东西。"
            ) from exc
        if unifiedId != expected:
            raise VerifyFailure(
                f"派生不一致：库里 {unifiedId!r}，但由两段派生应为 {expected!r}。"
                "统一 ID 是三库唯一锚，破了不会报错，只会让 A2/A3 查不到东西。"
            )
        if parsed != (businessObject, externalId):
            raise VerifyFailure(f"反解不一致：{unifiedId!r} 反解不回原两段。")
    return len(rows)


async def verifyReadOnly(session: AsyncSession) -> dict[str, int]:
    """只读门禁：不变式 1-3 + 大小写漂移。

    漂移必须在这里挂掉：小写占位行与规范行并存时，覆盖/悬空/派生三项都自洽，
    只读门禁会 PASS，直到 `--apply` 才被回填的体检拒绝 —— 白跑一轮上线窗口。
    """
    derived = await _checkDerivedIds(session)
    await assertNoCaseVariantMappings(session)
    await _checkCoverage(session)
    await _checkNoDangling(session)
    return {
        "alive_classes": await countAliveClasses(session),
        "class_mappings": await countClassMappings(session),
        "derived_ok": derived,
    }


async def _discardProbe(session: AsyncSession) -> None:
    """丢弃探针事务。

    - 正常路径回滚失败 ⇒ 会话多半已坏，后续断言会报出无关错误；转成 `VerifyFailure`
      让门禁短路，运维看到的是「回滚失败」而不是某个晦涩的接口异常。
    - 异常在途时只告警：不能拿回滚失败遮蔽真正的根因。哨兵从未 commit，
      连接断了服务端事务也随之终止，不可能落盘。
    """
    # 在 try 之前取：进到 except 里 sys.exc_info() 已变成刚捕获的 rollback 异常
    inFlight = sys.exc_info()[0] is not None
    try:
        await session.rollback()
    except Exception as exc:  # noqa: BLE001 —— 见 docstring 的两条分支
        if inFlight:
            print(
                f"[id_mapping_realdata] 警告：探针事务回滚失败：{exc}", file=sys.stderr
            )
            return
        raise VerifyFailure(f"探针事务回滚失败，会话不可用：{exc}") from exc


async def _checkRerunSurvives(session: AsyncSession) -> None:
    """不变式 4：重跑不得抹掉 A2/A3 写入的跨库落点列。

    探针必须是**这次重跑真的会 upsert 的行**：取 id 最小的**存活**本体类。取错行
    （墓碑、非 CLASS 行、或只能由 `--include-deleted` 覆盖的行）时回填根本不碰它，
    `set_` 被放宽的回归照样全绿。

    哨兵不 commit：`finally` 里 rollback 即还原，且只读方看不到。
    """
    classId = (
        await session.execute(
            text(
                "SELECT id FROM ontology_class "
                f"WHERE {ALIVE_CLASS_PREDICATE} ORDER BY id LIMIT 1"
            )
        )
    ).scalar_one_or_none()
    if classId is None:
        raise VerifyFailure(
            "id_mapping 为空（或没有存活本体类），无法验证重跑存活："
            "先确认回填真的写入了行。"
        )

    unifiedId = buildUnifiedId(BUSINESS_OBJECT_CLASS, str(classId))
    sentinelParams = {"n": _SENTINEL, "c": _SENTINEL, "m": _SENTINEL, "uid": unifiedId}
    try:
        probe = await session.execute(
            text(
                "UPDATE id_mapping SET neo4j_node_id = :n, milvus_collection = :c, "
                "milvus_id = :m WHERE unified_id = :uid"
            ),
            sentinelParams,
        )
        if probe.rowcount != 1:
            raise VerifyFailure(
                f"探针行 {unifiedId!r} 不在 id_mapping 里（受影响 {probe.rowcount} 行）——"
                "门禁无法验证重跑存活，先跑回填。"
            )

        # commit=False：哨兵与回填同属一个未提交事务，SELECT 仍能看到哨兵
        await backfillIdMappings(session, commit=False)

        after = (
            await session.execute(
                text(
                    "SELECT neo4j_node_id, milvus_collection, milvus_id "
                    "FROM id_mapping WHERE unified_id = :uid"
                ),
                {"uid": unifiedId},
            )
        ).one()
        if tuple(after) != (_SENTINEL, _SENTINEL, _SENTINEL):
            raise VerifyFailure(
                f"重跑抹掉了跨库落点：{unifiedId} 的 (neo4j_node_id, milvus_collection, "
                f"milvus_id) 由哨兵变成了 {tuple(after)!r}。回填的 on_conflict set_ "
                "必须只含它自己拥有的列（pg_table/pg_id），否则 A2/A3 的成果会被抹掉。"
            )
    finally:
        await _discardProbe(session)


async def _checkIdempotent(session: AsyncSession) -> None:
    """不变式 5：再跑一次回填，CLASS 映射与总行数都不变（**不落盘**）。"""
    beforeRows = await _scalar(session, "SELECT count(*) FROM id_mapping")
    beforeClass = await countClassMappings(session)
    try:
        # 门禁承诺「结束即回滚」：与探针同理，回填不得自己 commit
        await backfillIdMappings(session, commit=False)
        afterRows = await _scalar(session, "SELECT count(*) FROM id_mapping")
        afterClass = await countClassMappings(session)
    finally:
        await _discardProbe(session)
    if (beforeRows, beforeClass) != (afterRows, afterClass):
        raise VerifyFailure(
            f"回填不幂等：重跑前 {beforeRows} 行（CLASS {beforeClass}），"
            f"重跑后 {afterRows} 行（CLASS {afterClass}）。"
        )


async def verifyFull(session: AsyncSession) -> dict[str, int]:
    """全量门禁：不变式 1-5（会开未提交事务）。"""
    report = await verifyReadOnly(session)
    await _checkRerunSurvives(session)
    await _checkIdempotent(session)
    return report


def _parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A1 统一 ID 映射真实数据门禁")
    parser.add_argument(
        "--full",
        action="store_true",
        help="跑重跑存活 + 幂等（会写库：未提交事务，结束即回滚）",
    )
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help="目标判定为生产时，--full 必须显式带上（先完成当日备份）",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parseArgs(argv)

    from app.config import getSettings
    from app.infrastructure.database import getSessionFactory

    settings = getSettings()
    if args.full:
        try:
            assertSafeWriteTarget(
                appEnv=settings.appEnv,
                databaseUrl=settings.databaseUrl,
                allowProdWrite=args.allow_prod,
            )
        except SystemExit as exc:
            print(f"[id_mapping_realdata] {exc}", file=sys.stderr)
            return 2

    sessionMaker = getSessionFactory()
    async with sessionMaker() as session:
        try:
            report = (
                await verifyFull(session) if args.full else await verifyReadOnly(session)
            )
        except (VerifyFailure, CorruptMappingError) as exc:
            # CorruptMappingError 来自全量模式的重跑（体检）：它是 FAIL 的一项，
            # 不该以 traceback 形态冒出去
            print(f"[id_mapping_realdata] FAIL: {exc}", file=sys.stderr)
            return 1
        except SQLAlchemyError as exc:
            # 探针与幂等检查都在未提交事务里，异常即回滚，哨兵不会落盘
            print(f"[id_mapping_realdata] FAIL: 数据库错误：{exc}", file=sys.stderr)
            return 1

    mode = "全量（重跑存活 ✓，幂等 ✓）" if args.full else "只读"
    print(
        f"[id_mapping_realdata] PASS（{mode}）: "
        f"存活类 {report['alive_classes']}，CLASS 映射 {report['class_mappings']} 行"
        f"（覆盖 ✓ 无悬空 ✓），派生一致 {report['derived_ok']} 行"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
