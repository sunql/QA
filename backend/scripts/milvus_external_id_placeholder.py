"""id_mapping 占位行的三态判定（PG 侧）。

为什么不能只看「INSERT 有没有抛异常」
--------------------------------------
``id_mapping.unified_id`` 是**主键**，而 ``ON CONFLICT (business_object, external_id)``
只兜住 ``uq_id_mapping_bo_ext`` 这条唯一索引 ⇒ **主键冲突会抛 IntegrityError**；
但「(bo, ext) 已被**别的** unified_id 占用」这种情况会被 ``DO NOTHING``
**静默吞掉**（0 行受影响、无异常）。

⇒ 存在两种「INSERT 没报错但结果不对」的情形，**异常信号无法区分**。
所以这里的判定口径是：**先写（或不写），再读回，以读回结果为准**。

三态
----
- ``ok``：占位行存在且一致（本次写的，或本来就有且一致）⇒ 该行可以回填；
- ``conflict``：同槽位被**别的** unified_id 占用，或本 unified_id 已被**别的**
  (bo, ext) 占用 ⇒ 该行**不能**回填（照写会让「Milvus.external_id ↔ PG.unified_id」
  成为悬垂引用，事后按 external_id 反查 PG 查不到）；
- ``failed``：写完（或本该写）之后读回仍找不到占位行 ⇒ PG 状态不明 ⇒ 该行跳过。

⚠️ **``business_object`` 大小写 SSOT 未决**（2026-11-07 仍未决）：本模块按
**字面相等**判定。若现存 PG 行的 ``business_object`` 用的是**小写**而调用方传的是
**大写**（或反之），判定会退化成 conflict ⇒ 该行永远回填不了。
调用方（``scripts/backfill_milvus_external_id.py``）在 SSOT 决议前
**不得对生产执行**。
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PLACEHOLDER_OK = "ok"
PLACEHOLDER_CONFLICT = "conflict"
PLACEHOLDER_FAILED = "failed"

# 内部用：读回时「什么都没找到」——不是三态之一（要靠 apply 语义再翻译）。
_ABSENT = "absent"

# 异常信息里允许出现的最大字符数（SQLAlchemy 的异常文本会带上失败的 SQL 与参数）。
_MAX_ERROR_CHARS = 200

_INSERT_SQL = (
    "INSERT INTO id_mapping "
    "(unified_id, business_object, external_id, milvus_collection, "
    "milvus_id, created_time, updated_time) "
    "VALUES (:uid, :bo, :ext, :mc, :mid, now(), now()) "
    "ON CONFLICT (business_object, external_id) DO NOTHING"
)

# 读回：一次拿齐「同槽位」与「同 unified_id」两边的占用情况。
# unified_id 与 (business_object, external_id) 各有唯一索引 ⇒ OR 走 bitmap OR。
_READ_SQL = (
    "SELECT unified_id, business_object, external_id FROM id_mapping "
    "WHERE (business_object = :bo AND external_id = :ext) OR unified_id = :uid"
)


def _truncate(message: str) -> str:
    """截断异常文本，避免把整条 SQL 与参数写进日志。"""
    if len(message) <= _MAX_ERROR_CHARS:
        return message
    return message[:_MAX_ERROR_CHARS] + "…"


def _errorText(exc: Exception) -> str:
    """取数据库的**原始**错误文本（约束名在这里），而不是只有异常类名。"""
    orig = getattr(exc, "orig", None)
    return _truncate(str(orig) if orig is not None else str(exc))


async def _insertPlaceholder(
    session: AsyncSession,
    *,
    businessObject: str,
    externalId: str,
    expectedUid: str,
    collectionName: str,
    ontologyId: int,
) -> None:
    """尝试写占位行。

    **不抛异常**：写入失败/冲突都不直接决定行的去留 —— 判定一律以读回为准
    （见模块说明）。异常只记日志，且必须带数据库原始错误（约束名）。
    """
    savepoint = await session.begin_nested()
    try:
        await session.execute(
            text(_INSERT_SQL),
            {
                "uid": expectedUid,
                "bo": businessObject,
                "ext": externalId,
                "mc": collectionName,
                "mid": str(ontologyId),
            },
        )
        await savepoint.commit()
    except IntegrityError as exc:
        await savepoint.rollback()
        logger.warning(
            "PG 占位写冲突（以读回结果判定，不直接判失败）: %s (%s: %s)",
            expectedUid,
            type(exc).__name__,
            _errorText(exc),
        )
    except SQLAlchemyError as exc:
        await savepoint.rollback()
        logger.warning(
            "PG 占位写失败: %s (%s: %s)",
            expectedUid,
            type(exc).__name__,
            _errorText(exc),
        )


async def _readPlaceholderRows(
    session: AsyncSession, *, businessObject: str, externalId: str, expectedUid: str
) -> list[dict]:
    """读回占位行（同时覆盖「同槽位」与「同 unified_id」两边）。"""
    result = await session.execute(
        text(_READ_SQL),
        {"bo": businessObject, "ext": externalId, "uid": expectedUid},
    )
    return [dict(row) for row in result.mappings().all()]


def classifyPlaceholder(
    rows: list[dict], *, businessObject: str, externalId: str, expectedUid: str
) -> tuple[str, str]:
    """按读回结果判定占位行状态 ⇒ (状态, 说明)。

    状态 ∈ {ok, conflict, absent}；``absent`` 表示「两边都没占用」。
    """
    byUid = None
    bySlot = None
    for row in rows:
        if byUid is None and row.get("unified_id") == expectedUid:
            byUid = row
        if (
            bySlot is None
            and row.get("business_object") == businessObject
            and row.get("external_id") == externalId
        ):
            bySlot = row

    if byUid is not None and (
        byUid.get("business_object"),
        byUid.get("external_id"),
    ) != (businessObject, externalId):
        return (
            PLACEHOLDER_CONFLICT,
            f"unified_id {expectedUid} 已被 "
            f"({byUid.get('business_object')}, {byUid.get('external_id')}) 占用",
        )
    if bySlot is not None and bySlot.get("unified_id") != expectedUid:
        return (
            PLACEHOLDER_CONFLICT,
            f"({businessObject}, {externalId}) 已被 {bySlot.get('unified_id')} 占用",
        )
    if bySlot is not None or byUid is not None:
        return PLACEHOLDER_OK, "占位行已存在且一致"
    return _ABSENT, "占位行不存在"


async def ensurePlaceholder(
    session: AsyncSession,
    *,
    businessObject: str,
    externalId: str,
    expectedUid: str,
    collectionName: str,
    ontologyId: int,
    apply: bool,
) -> tuple[str, str]:
    """确保占位行可用；返回 (状态, 说明)，状态 ∈ {ok, conflict, failed}。

    - ``apply=False``（预演）：**只读**，不写库；两边都没占用 ⇒ 判 ``ok``（将要写）；
    - ``apply=True``：先写（``ON CONFLICT DO NOTHING``），再读回判定。
    """
    if apply:
        await _insertPlaceholder(
            session,
            businessObject=businessObject,
            externalId=externalId,
            expectedUid=expectedUid,
            collectionName=collectionName,
            ontologyId=ontologyId,
        )

    try:
        rows = await _readPlaceholderRows(
            session,
            businessObject=businessObject,
            externalId=externalId,
            expectedUid=expectedUid,
        )
    except SQLAlchemyError as exc:
        logger.warning(
            "PG 占位行读回失败: %s (%s: %s)",
            expectedUid,
            type(exc).__name__,
            _errorText(exc),
        )
        return PLACEHOLDER_FAILED, f"读回失败：{_errorText(exc)}"

    state, detail = classifyPlaceholder(
        rows,
        businessObject=businessObject,
        externalId=externalId,
        expectedUid=expectedUid,
    )
    if state != _ABSENT:
        return state, detail
    if apply:
        return PLACEHOLDER_FAILED, "写入后仍读不到占位行"
    return PLACEHOLDER_OK, "占位行不存在（预演：将要写入）"
