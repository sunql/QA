"""从 ontology_class 回填 id_mapping 类级映射（M0-P0.2 · A1 ③）。

为每个本体类生成 `obj:CLASS:{class_id}` —— 这是 A2（Neo4j 节点统一 ID）与
A4（ontology_service 三库同步）的输入。`business_object` 用 `CLASS`，与
`ontology_service.py` 的既有约定一致（不按类名建命名空间）。

**只读源表，纯新增**：不动 ontology_class 一行。

冲突路径用 `ON CONFLICT ... DO UPDATE`（不是 DO NOTHING）：DO NOTHING 会让
「先前插过但 PG 侧列不全」的行永远补不回来。但 `set_` **只更新本脚本拥有的两列**
（pg_table / pg_id，见 `_OWNED_COLUMNS`），绝不触碰 A2/A3 回填的 neo4j_* /
milvus_* —— 否则重跑会把图与向量的落点抹掉。

**单事务**：所有批次的 upsert 在同一个事务里提交。分批 commit 会让中途失败留下
「前几批已写、后几批没写」的半状态，对账口径会被破坏好一阵（重跑能修，但在那之前
门禁是红的）。幂等：重复执行行数不变。

**动手前体检**（任何写入之前）：

1. `unified_id` 与自身两段不一致的行 ⇒ `CorruptMappingError`，点名最多 5 条。
   否则重算出的 ID 会撞上第二条唯一约束 `uq_id_mapping_bo_ext`，报错抛在写入
   中途且不可执行。
2. `business_object` 与本脚本的规范值只差大小写的行（`class` / `Class`）⇒
   `CaseVariantMappingError`。已知来源是 `reconcile_milvus_id_mapping.py` 写的
   小写占位行；放任会让同一个类拥有两条统一 ID，A2/A4 按大小写查会对不上。

**默认只预演**（dry-run）：`--apply` 才写库。目标库必须命中仓库统一的
deny-by-default 白名单（`qa_metadata` / `qa_metadata_test*`，见
`scripts/kpi_metric_db_guard.py`）；且 `APP_ENV` 是生产或库名恰为 `qa_metadata`
时，`--apply` 还必须加 `--allow-prod`。

用法：
    cd backend
    uv run python -m scripts.backfill_id_mapping                    # 预演
    uv run python -m scripts.backfill_id_mapping --apply            # 执行
    uv run python -m scripts.backfill_id_mapping --apply --include-deleted
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.domain.id_mapping import (
    UNIFIED_ID_PREFIX,
    UNIFIED_ID_SEPARATOR,
    buildUnifiedId,
)
from app.domain.models import IdMapping, OntologyClass
from scripts.kpi_metric_db_guard import assertSafeTarget

BUSINESS_OBJECT_CLASS = "CLASS"
# 「存活类」的唯一 SQL 口径：回填与门禁共用，写两处会各自漂移、各自自洽
ALIVE_CLASS_PREDICATE = "valid_to IS NULL"
# 报错里最多点名多少条坏行（够定位即可，不把整表倒进日志）
CORRUPT_SAMPLE_LIMIT = 5
_ONTOLOGY_CLASS_TABLE = "ontology_class"
# 单批 upsert 行数：本体类是几十行量级，一批足够；分批只为将来类数上千时不爆参数
_BATCH_SIZE = 500
# 回填**拥有**的列。DO UPDATE 的 set_ 由它生成 —— 测试把它放宽来模拟
# 「有人顺手把 neo4j_* / milvus_* 加进 set_」的回归（门禁必须能抓到）。
_OWNED_COLUMNS: tuple[str, ...] = ("pg_table", "pg_id")
_PRODUCTION_APP_ENVS = frozenset({"production", "prod"})


class CorruptMappingError(RuntimeError):
    """`id_mapping` 存在「`unified_id` 与自身两段不一致」的行 —— 回填拒绝执行。

    这类行必然是手工 SQL 或历史 bug 造出来的：所有写入路径（`register` 与本脚本）
    都经 `buildUnifiedId` 派生，写不出一致的组合。
    """


class CaseVariantMappingError(CorruptMappingError):
    """`id_mapping` 存在大小写不同的同一命名空间（如 `class` vs `CLASS`）。

    继承 `CorruptMappingError`：对「回填拒绝执行、先体检」的调用方语义相同，
    只是文案要给出合并指引。
    """


@dataclass(frozen=True)
class BackfillSummary:
    """回填结果。

    - `aliveClasses`：库中存活本体类总数（`valid_to IS NULL`）
    - `scannedClasses`：本次实际扫描的类数（`includeDeleted=True` 时含墓碑）
    - `affectedRows`：PG 报告的受影响行数（DO UPDATE 语义下含值未变的冲突行）
    """

    aliveClasses: int
    scannedClasses: int
    affectedRows: int


@dataclass(frozen=True)
class BackfillPreview:
    """预演结果：只读，不写任何行。"""

    aliveClasses: int
    scannedClasses: int
    plannedRows: int


def assertSafeWriteTarget(
    *, appEnv: str, databaseUrl: str, allowProdWrite: bool
) -> str:
    """写库目标双闸：库名白名单（deny-by-default）+ 环境名。拒绝时 `SystemExit`。

    库名白名单复用 `kpi_metric_db_guard.assertSafeTarget` —— 本仓刻意只有一份
    正则。但**白名单挡不住同名 dev/prod**（两者都叫 `qa_metadata`），所以再加
    `APP_ENV` 这一路：环境变量说是生产就必须显式 opt-in，哪怕库名像测试库。
    """
    try:
        databaseName = assertSafeTarget(databaseUrl, allowProdWrite=allowProdWrite)
    except SystemExit as exc:
        # 守卫模块的开关名是 KPI 工具链的 --allow-prod-write；本脚本叫 --allow-prod。
        # 保留守卫原文（好文案），只替换开关名 —— 两个名字并排出现会让人照抄错的。
        raise SystemExit(
            str(exc).replace("--allow-prod-write", "--allow-prod")
        ) from None
    if (appEnv or "").strip().lower() in _PRODUCTION_APP_ENVS and not allowProdWrite:
        raise SystemExit(
            f"拒绝执行：APP_ENV={appEnv!r} 判定为生产环境。"
            "确认后加 --allow-prod（并先完成当日备份）。"
        )
    return databaseName


async def _scalar(session: AsyncSession, sql: str, **params: object) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


async def countAliveClasses(session: AsyncSession) -> int:
    """存活本体类数（`ALIVE_CLASS_PREDICATE`）；门禁与回填共用。"""
    return await _scalar(
        session, f"SELECT count(*) FROM ontology_class WHERE {ALIVE_CLASS_PREDICATE}"
    )


async def _loadClassRows(
    session: AsyncSession, *, includeDeleted: bool
) -> list[tuple[int, str]]:
    """取 `(class_id, class_name)`，按 id 升序。class_name 仅用于日志可读性。"""
    stmt = select(OntologyClass.id, OntologyClass.class_name).order_by(
        OntologyClass.id
    )
    if not includeDeleted:
        stmt = stmt.where(OntologyClass.valid_to.is_(None))
    rows = (await session.execute(stmt)).all()
    return [(int(classId), str(className)) for classId, className in rows]


def _buildRows(classRows: list[tuple[int, str]]) -> list[dict[str, str]]:
    """构造 upsert 载荷。external_id 用 str(class_id) —— id 稳定且天然唯一。"""
    return [
        {
            "unified_id": buildUnifiedId(BUSINESS_OBJECT_CLASS, str(classId)),
            "business_object": BUSINESS_OBJECT_CLASS,
            "external_id": str(classId),
            "pg_table": _ONTOLOGY_CLASS_TABLE,
            "pg_id": str(classId),
        }
        for classId, _className in classRows
    ]


async def _upsertBatch(session: AsyncSession, rows: list[dict[str, str]]) -> int:
    """一批 upsert；冲突时只更新本脚本拥有的列。**不 commit**（调用方拥有事务）。"""
    stmt = pg_insert(IdMapping).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["unified_id"],
        set_={column: getattr(stmt.excluded, column) for column in _OWNED_COLUMNS},
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def findCorruptMappings(
    session: AsyncSession,
) -> tuple[int, list[tuple[str, str, str]]]:
    """找出 `unified_id` 与自身两段不一致的行。

    返回 `(总坏行数, 最多 CORRUPT_SAMPLE_LIMIT 条样例)`。总数单独查：只报样例条数
    会在坏行多于取样上限时低报，运维据此判断影响面就会判断错。

    为什么必须在动手前查：回填按 `unified_id` 做 conflict target，但表上还有第二条
    唯一约束 `uq_id_mapping_bo_ext (business_object, external_id)`。一行若
    `unified_id` 与自身两段对不上，重算出的 ID 会撞上**第二条**约束 —— 而
    `on_conflict_do_update` 只能指定一个 conflict target，于是抛 `IntegrityError`。
    体检后点名，比撞约束再炸好。
    """
    whereClause = (
        "unified_id <> :prefix || :sep || business_object || :sep || external_id"
    )
    params = {
        "prefix": UNIFIED_ID_PREFIX,
        "sep": UNIFIED_ID_SEPARATOR,
    }
    total = await _scalar(
        session, f"SELECT count(*) FROM id_mapping WHERE {whereClause}", **params
    )
    if total == 0:
        return 0, []
    rows = (
        await session.execute(
            text(
                "SELECT unified_id, business_object, external_id FROM id_mapping "
                f"WHERE {whereClause} ORDER BY unified_id LIMIT :limit"
            ),
            {**params, "limit": CORRUPT_SAMPLE_LIMIT},
        )
    ).all()
    return total, [(str(uid), str(bo), str(ext)) for uid, bo, ext in rows]


def _renderExpected(bo: str, ext: str) -> str:
    """派生值用于报错文案；坏行本身可能含非法分段，不能让它再抛一次。"""
    try:
        return repr(buildUnifiedId(bo, ext))
    except ValidationError:
        return f"<无法派生：{bo!r} + {ext!r} 不是合法分段>"


def _describeCorrupt(total: int, samples: list[tuple[str, str, str]]) -> str:
    detail = "；".join(
        f"{uid!r} 应为 {_renderExpected(bo, ext)}" for uid, bo, ext in samples
    )
    more = "" if total <= len(samples) else f"（另有 {total - len(samples)} 行未列出）"
    return (
        f"id_mapping 有 {total} 行的 unified_id 与自身两段不一致，回填会撞 "
        f"uq_id_mapping_bo_ext 中途崩溃：{detail}{more}。"
        "请先修正这些行（或删除后由本脚本重建）再跑回填。"
    )


async def findCaseVariantMappings(
    session: AsyncSession,
) -> tuple[int, list[tuple[str, str, str]]]:
    """找出 `business_object` 与本脚本规范值只差大小写的行。

    来源：`reconcile_milvus_id_mapping.py` 用 Milvus 的 `type`（小写 `class`）
    写占位行，而 `ontology_service` 用 `CLASS`。两条记录对同一类 ⇒ 同一类两个
    统一 ID、`uq_id_mapping_bo_ext` 也拦不住（两段不同），A2/A4 按大小写查会
    各自看到一半。
    """
    whereClause = (
        "lower(business_object) = lower(:bo) AND business_object <> :bo"
    )
    params = {"bo": BUSINESS_OBJECT_CLASS}
    total = await _scalar(
        session, f"SELECT count(*) FROM id_mapping WHERE {whereClause}", **params
    )
    if total == 0:
        return 0, []
    rows = (
        await session.execute(
            text(
                "SELECT unified_id, business_object, external_id FROM id_mapping "
                f"WHERE {whereClause} ORDER BY unified_id LIMIT :limit"
            ),
            {**params, "limit": CORRUPT_SAMPLE_LIMIT},
        )
    ).all()
    return total, [(str(uid), str(bo), str(ext)) for uid, bo, ext in rows]


def _describeCaseVariants(total: int, samples: list[tuple[str, str, str]]) -> str:
    detail = "；".join(f"{uid!r}（business_object={bo!r}）" for uid, bo, _ in samples)
    more = "" if total <= len(samples) else f"（另有 {total - len(samples)} 行未列出）"
    return (
        f"id_mapping 有 {total} 行以大小写不同的 business_object 记录了同一命名空间"
        f"（规范值 {BUSINESS_OBJECT_CLASS!r}）：{detail}{more}。"
        "同一类会有两个统一 ID，A2/A4 按大小写查会对不上。"
        "已知来源是 scripts/reconcile_milvus_id_mapping.py 写入的小写占位行；"
        "请先把这些行的跨库落点合并到规范行并删除小写行，再跑回填。"
    )


async def assertNoCaseVariantMappings(session: AsyncSession) -> None:
    """大小写漂移前置体检：回填与只读门禁共用同一判定与文案。"""
    total, samples = await findCaseVariantMappings(session)
    if total:
        raise CaseVariantMappingError(_describeCaseVariants(total, samples))


async def _loadAndCheck(
    session: AsyncSession, *, includeDeleted: bool
) -> tuple[int, list[dict[str, str]]]:
    """体检在**任何写入之前**：撞到约束才炸的话，半状态已经写进去了。"""
    aliveClasses = await countAliveClasses(session)
    rows = _buildRows(await _loadClassRows(session, includeDeleted=includeDeleted))
    if not includeDeleted and aliveClasses != len(rows):
        # 存活口径有两份实现（SQL 常量 vs ORM `valid_to.is_(None)`）：
        # 一旦漂移，摘要里的「存活类」与「扫描」会各说各话，先在这里钉住。
        raise RuntimeError(
            f"存活类口径不一致：SQL 计数 {aliveClasses}，ORM 扫描 {len(rows)}。"
        )
    corruptTotal, corruptSamples = await findCorruptMappings(session)
    if corruptTotal:
        raise CorruptMappingError(_describeCorrupt(corruptTotal, corruptSamples))
    await assertNoCaseVariantMappings(session)
    return aliveClasses, rows


async def previewBackfill(
    session: AsyncSession, *, includeDeleted: bool = False
) -> BackfillPreview:
    """只读预演：体检 + 报计划行数，不写任何行。"""
    aliveClasses, rows = await _loadAndCheck(session, includeDeleted=includeDeleted)
    return BackfillPreview(
        aliveClasses=aliveClasses,
        scannedClasses=len(rows),
        plannedRows=len(rows),
    )


async def backfillIdMappings(
    session: AsyncSession, *, includeDeleted: bool = False, commit: bool = True
) -> BackfillSummary:
    """回填 id_mapping 类级映射；幂等，可安全重跑。

    `commit=False` 时把事务交给调用方（真实数据门禁的探针用它把哨兵与回填放进
    同一个未提交事务）。`includeDeleted=False`（默认）只回填存活类。
    """
    aliveClasses, rows = await _loadAndCheck(session, includeDeleted=includeDeleted)

    affected = 0
    for start in range(0, len(rows), _BATCH_SIZE):
        affected += await _upsertBatch(session, rows[start : start + _BATCH_SIZE])

    if commit:
        await session.commit()

    return BackfillSummary(
        aliveClasses=aliveClasses,
        scannedClasses=len(rows),
        affectedRows=affected,
    )


def _parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填 id_mapping 类级统一 ID")
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="连同软删墓碑（valid_to IS NOT NULL）一起回填",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加只预演（默认只读）",
    )
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help="目标判定为生产时，--apply 必须显式带上（先完成当日备份）",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """CLI 入口：连默认 DB（读 DATABASE_URL）。默认预演，--apply 才写。"""
    args = _parseArgs(argv)

    from app.config import getSettings
    from app.infrastructure.database import getSessionFactory

    settings = getSettings()
    if args.apply:
        try:
            assertSafeWriteTarget(
                appEnv=settings.appEnv,
                databaseUrl=settings.databaseUrl,
                allowProdWrite=args.allow_prod,
            )
        except SystemExit as exc:
            print(f"[backfill_id_mapping] {exc}", file=sys.stderr)
            return 2

    sessionMaker = getSessionFactory()
    async with sessionMaker() as session:
        try:
            if args.apply:
                summary = await backfillIdMappings(
                    session, includeDeleted=args.include_deleted
                )
            else:
                preview = await previewBackfill(
                    session, includeDeleted=args.include_deleted
                )
        except CorruptMappingError as exc:
            print(f"[backfill_id_mapping] FAIL: {exc}", file=sys.stderr)
            return 1
        except SQLAlchemyError as exc:
            # 单事务保证：异常时整个事务已回滚，不会留半状态
            print(f"[backfill_id_mapping] FAIL: 数据库错误：{exc}", file=sys.stderr)
            return 1

    if args.apply:
        print(
            f"[backfill_id_mapping] 已写入：存活类 {summary.aliveClasses}，"
            f"扫描 {summary.scannedClasses}，受影响行 {summary.affectedRows}"
        )
    else:
        print(
            f"[backfill_id_mapping] 预演（未写库）：存活类 {preview.aliveClasses}，"
            f"计划 upsert {preview.plannedRows} 行；确认后加 --apply"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
