"""semantic_release 的库侧约束行为（P1a / D4，评审 M3）。

**为什么必须是真实 PG**：`uq_semantic_release_one_active` / `uq_semantic_release_global_version`
是**部分唯一索引**，两个 CHECK 是 JSONB / 正则表达式。ORM 元数据里写对了名字，不等于
PostgreSQL 真正按预期拒绝写入 —— 谓词写错（比如漏了 `COALESCE`）、约束被迁移漏建，
只有真的 INSERT 一次才会暴露。单元测试里断言 `__table_args__` 的元数据只能证「声明存在」。

全部用裸 SQL 写入，刻意**绕过 ORM**：这层约束的职责就是拦住「绕过 ORM 的脚本」，
用 ORM 插入等于让被测对象先替数据库做一遍判断。

断言落到**具体约束名**（从异常消息里解析，见 `_constraintName`），不只看
`IntegrityError`：否则任何一条别的约束（NOT NULL、外键）先炸都会让用例假绿。

**分片影响**：本文件在 `integration/` 里新增会让 `sort` 后下标 ≥ 它的文件整体
轮转一位（`Harness/rules/测试规范.md`「目录分片稳定性」）。本文件自包含——每个
用例自己 `TRUNCATE` 全库并自造所需行，不依赖同分片邻居，故轮转安全。
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_MANIFEST_JSON = '{"schemaVersion":"v1"}'
_HASH = "a" * 64

_UNIQUE_ACTIVE = "uq_semantic_release_one_active"
_UNIQUE_GLOBAL_VERSION = "uq_semantic_release_global_version"
_CHECK_MANIFEST_IS_OBJECT = "ck_semantic_release_manifest_is_object"
_CHECK_HASH_SHA256 = "ck_semantic_release_manifest_hash_sha256"
_CHECK_STATUS = "ck_semantic_release_status"
_FK_DOMAIN = "fk_semantic_release_domain"


async def _insertRelease(
    session: AsyncSession,
    *,
    version: str = "2026.11.09-1",
    status: str = "DRAFT",
    domainCode: str | None = None,
    manifestJson: str = _MANIFEST_JSON,
    manifestHash: str = _HASH,
) -> None:
    """裸 SQL 插入一条发布快照（不 commit；由调用方决定成败）。"""
    await session.execute(
        text(
            "INSERT INTO semantic_release "
            "(domain_code, version, status, manifest_json, manifest_hash) "
            "VALUES (:domain, :version, :status, CAST(:manifest AS jsonb), :hash)"
        ),
        {
            "domain": domainCode,
            "version": version,
            "status": status,
            "manifest": manifestJson,
            "hash": manifestHash,
        },
    )


async def _insertDomain(session: AsyncSession, code: str) -> None:
    """域可能已由应用启动时的 seed 写入（LOGISTICS/PROCUREMENT/QUALITY），故幂等。"""
    await session.execute(
        text(
            "INSERT INTO data_domain (code, name, enabled) VALUES (:code, :name, true) "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {"code": code, "name": f"域 {code}"},
    )


async def _expectRejected(session: AsyncSession, **kwargs: Any) -> str:
    """插入并期望被**某条具体约束**拒绝，返回约束名。

    注意 `session.execute` 就会抛（约束不是 deferrable，asyncpg 在语句级拒绝），
    所以 `pytest.raises` 必须同时包住 execute 与 commit。
    """
    with pytest.raises(IntegrityError) as caught:
        await _insertRelease(session, **kwargs)  # type: ignore[arg-type]
        await session.commit()
    await session.rollback()
    return _constraintName(caught.value)


_CONSTRAINT_IN_MESSAGE = re.compile(
    r'violates (?:check|unique|foreign key) constraint "([^"]+)"'
)


def _constraintName(exc: IntegrityError) -> str:
    """取 PostgreSQL 报的约束名。

    SQLAlchemy 的 asyncpg 适配层把原始异常**字符串化**塞进 `orig.args[0]`
    （`<class 'asyncpg.exceptions.CheckViolationError'>: new row ... violates
    check constraint "ck_..."`），没有 `.constraint_name` 属性 ⇒ 只能从消息里取。
    """
    matched = _CONSTRAINT_IN_MESSAGE.search(str(exc.orig))
    # 解析不来就直接炸，别返回 None 让调用方报「约束名不对」——那会把
    # 「依赖升级改了消息格式」误诊成「约束没生效」。
    assert matched, f"无法从异常解析约束名（可能依赖升级改了消息格式）：{exc.orig!r}"
    return matched.group(1)


# ---------------------------------------------------------------------------
# 部分唯一索引：同域最多一条 ACTIVE
# ---------------------------------------------------------------------------


async def test_two_global_active_rows_are_rejected(dbSession: AsyncSession) -> None:
    """全局快照（domain_code IS NULL）的 ACTIVE 唯一性靠 `COALESCE(domain_code, '')`。

    PostgreSQL 唯一约束对 NULL 互不相等 —— 谓词里漏掉 COALESCE，这条就会静默放行。
    """
    await _insertRelease(dbSession, version="2026.11.09-1", status="ACTIVE")
    await dbSession.commit()

    assert (
        await _expectRejected(dbSession, version="2026.11.09-2", status="ACTIVE")
        == _UNIQUE_ACTIVE
    )


async def test_two_active_rows_in_same_domain_are_rejected(dbSession: AsyncSession) -> None:
    await _insertDomain(dbSession, "PROCUREMENT")
    await dbSession.commit()

    await _insertRelease(dbSession, version="1", status="ACTIVE", domainCode="PROCUREMENT")
    await dbSession.commit()

    assert (
        await _expectRejected(
            dbSession, version="2", status="ACTIVE", domainCode="PROCUREMENT"
        )
        == _UNIQUE_ACTIVE
    )


async def test_active_rows_in_different_domains_are_allowed(dbSession: AsyncSession) -> None:
    """反向守卫：按域发布上线后每个域各有一条 ACTIVE，约束不得把它们互相挤掉。"""
    await _insertDomain(dbSession, "PROCUREMENT")
    await _insertDomain(dbSession, "QUALITY")
    await dbSession.commit()

    await _insertRelease(dbSession, version="1", status="ACTIVE", domainCode="PROCUREMENT")
    await _insertRelease(dbSession, version="1", status="ACTIVE", domainCode="QUALITY")
    await _insertRelease(dbSession, version="1", status="ACTIVE", domainCode=None)
    await dbSession.commit()

    total = await dbSession.scalar(
        text("SELECT count(*) FROM semantic_release WHERE status = 'ACTIVE'")
    )
    assert total == 3


async def test_multiple_non_active_rows_are_allowed(dbSession: AsyncSession) -> None:
    """只有 ACTIVE 受部分索引约束：历史 ARCHIVED 快照可以有很多条。"""
    for index in range(3):
        await _insertRelease(
            dbSession, version=f"2026.10.0{index}", status="ARCHIVED"
        )
    await dbSession.commit()

    total = await dbSession.scalar(text("SELECT count(*) FROM semantic_release"))
    assert total == 3


# ---------------------------------------------------------------------------
# 部分唯一索引：全局快照版本号唯一
# ---------------------------------------------------------------------------


async def test_two_global_rows_with_same_version_are_rejected(
    dbSession: AsyncSession,
) -> None:
    await _insertRelease(dbSession, version="2026.11.09-1", status="ARCHIVED")
    await dbSession.commit()

    assert (
        await _expectRejected(dbSession, version="2026.11.09-1", status="DRAFT")
        == _UNIQUE_GLOBAL_VERSION
    )


async def test_same_version_across_domains_is_allowed(dbSession: AsyncSession) -> None:
    """版本号唯一性只覆盖全局快照；域内版本号唯一性「届时随迁移一起做」。"""
    await _insertDomain(dbSession, "PROCUREMENT")
    await _insertDomain(dbSession, "QUALITY")
    await dbSession.commit()

    await _insertRelease(dbSession, version="2026.11.09-1", domainCode="PROCUREMENT")
    await _insertRelease(dbSession, version="2026.11.09-1", domainCode="QUALITY")
    await _insertRelease(dbSession, version="2026.11.09-1", domainCode=None)
    await dbSession.commit()

    total = await dbSession.scalar(text("SELECT count(*) FROM semantic_release"))
    assert total == 3


# ---------------------------------------------------------------------------
# CHECK：快照形状与哈希格式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("manifestJson", ["[]", '"v1"', "null", "123", "true"])
async def test_manifest_json_must_be_object(
    dbSession: AsyncSession, manifestJson: str
) -> None:
    """'null' / '[1,2]' 这类能过 JSONB 列、却无法被回放成 Manifest。"""
    assert (
        await _expectRejected(dbSession, manifestJson=manifestJson)
        == _CHECK_MANIFEST_IS_OBJECT
    )


@pytest.mark.parametrize(
    "manifestHash",
    [
        "A" * 64,  # 大写十六进制：跨语言复算的口径是小写
        "a" * 63,  # 少一位
        # 多一位的情况由列类型 VARCHAR(64) 先拒（StringDataRightTruncationError，
        # 不是 IntegrityError），CHECK 与列宽是两层，这里只测 CHECK 层。
        "g" * 64,  # 非十六进制字符
        "",
    ],
)
async def test_manifest_hash_must_be_sha256_hex(
    dbSession: AsyncSession, manifestHash: str
) -> None:
    assert (
        await _expectRejected(dbSession, manifestHash=manifestHash)
        == _CHECK_HASH_SHA256
    )


async def test_status_literal_is_constrained(dbSession: AsyncSession) -> None:
    """`one_active` 的谓词是字面量 `status = 'ACTIVE'`；大小写变体会让兜底静默失效。"""
    assert await _expectRejected(dbSession, status="active") == _CHECK_STATUS


# ---------------------------------------------------------------------------
# 外键：快照只能挂真实存在的域，且域不可删
# ---------------------------------------------------------------------------


async def test_unknown_domain_code_is_rejected(dbSession: AsyncSession) -> None:
    assert (
        await _expectRejected(dbSession, domainCode="NO_SUCH_DOMAIN") == _FK_DOMAIN
    )


async def test_valid_snapshot_is_accepted(dbSession: AsyncSession) -> None:
    """反向守卫：合法写入必须放行 —— 否则上面所有「拒绝」都可能只是整体写不进去。"""
    await _insertDomain(dbSession, "PROCUREMENT")
    await dbSession.commit()

    await _insertRelease(dbSession, status="ACTIVE", domainCode="PROCUREMENT")
    await dbSession.commit()

    row = (
        await dbSession.execute(
            text(
                "SELECT version, status, jsonb_typeof(manifest_json) AS kind, "
                "manifest_hash FROM semantic_release"
            )
        )
    ).one()
    assert row.version == "2026.11.09-1"
    assert row.status == "ACTIVE"
    assert row.kind == "object"
    assert row.manifest_hash == _HASH


# ===========================================================================
# 采购域源绑定（M1-1.1，0154）：`procurement_source_binding` /
# `procurement_property_binding` 的库侧约束行为
# ===========================================================================
#
# 为什么写在本文件而不是新建 integration 文件：CI 集成测试按「排序后下标 mod 4」
# 轮转分片（`Harness/rules/测试规范.md`），新建文件会让后续分片整体错位。这两张表
# 是发布 manifest 的输入，约束语义与上面的发布快照同一层，放这里语义上也成立。
#
# 同样用**裸 SQL**：这层约束的职责就是拦住绕过 ORM 的脚本。

_PSB = "procurement_source_binding"
_PPB = "procurement_property_binding"

_CK_PSB_ROLE = "ck_procurement_source_binding_role"
_CK_PSB_CANDIDATE = "ck_procurement_source_binding_candidate"
_CK_PSB_CONFIRMED = "ck_procurement_source_binding_confirmed"
_CK_PSB_RANGE = "ck_procurement_source_binding_valid_range"
_UQ_PSB_ACTIVE = "uq_procurement_source_binding_active"
_UQ_PPB_ACTIVE = "uq_procurement_property_binding_active"
_UQ_PPB_KEY = "uq_procurement_property_binding_key_ordinal"
_FK_PPB_PROPERTY = "fk_procurement_property_binding_property"


async def _insertDataSource(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO data_source "
            "(name, type, host, port, database_name, username, password_encrypted, "
            " created_time, updated_time) "
            "VALUES ('src-test', 'oracle', '127.0.0.1', 1521, 'DB', 'u', 'x', now(), now()) "
            "RETURNING id"
        )
    )
    return row.scalar_one()


async def _insertClass(session: AsyncSession, class_name: str) -> int:
    row = await session.execute(
        text(
            "INSERT INTO ontology_class "
            "(class_name, version, valid_from, created_time, updated_time) "
            "VALUES (:name, 1, now(), now(), now()) RETURNING id"
        ),
        {"name": class_name},
    )
    return row.scalar_one()


async def _insertProperty(session: AsyncSession, class_id: int, name: str) -> int:
    row = await session.execute(
        text(
            "INSERT INTO ontology_property "
            "(class_id, property_name, data_type, created_time, updated_time) "
            "VALUES (:class_id, :name, 'VARCHAR', now(), now()) RETURNING id"
        ),
        {"class_id": class_id, "name": name},
    )
    return row.scalar_one()


async def _insertSourceBinding(
    session: AsyncSession,
    *,
    class_id: int,
    datasource_id: int,
    role: str = "BASE",
    source_table: str = "DWD_X",
    schema_name: str = "THBI",
    is_candidate: bool = True,
    confirmed_by: str | None = None,
    confirmed_time: str | None = None,
    valid_from: str = "now()",
    valid_to: str | None = None,
) -> int:
    row = await session.execute(
        text(
            "INSERT INTO procurement_source_binding "
            "(class_id, datasource_id, schema_name, source_table, binding_role, "
            " is_candidate, confirmed_by, "
            " confirmed_time, valid_from, valid_to) "
            f"VALUES (:class_id, :ds, :schema, :tbl, :role, :cand, :cby, "
            f"({confirmed_time or 'NULL'})::timestamptz, "
            f"({valid_from})::timestamptz, "
            f"({valid_to or 'NULL'})::timestamptz) "
            "RETURNING id"
        ),
        {
            "class_id": class_id,
            "ds": datasource_id,
            "schema": schema_name,
            "tbl": source_table,
            "role": role,
            "cand": is_candidate,
            "cby": confirmed_by,
        },
    )
    return row.scalar_one()


async def _expectBindingRejected(session: AsyncSession, coro) -> str:
    with pytest.raises(IntegrityError) as caught:
        await coro
        await session.commit()
    await session.rollback()
    return _constraintName(caught.value)


async def test_source_binding_two_active_rows_for_same_key_are_rejected(
    dbSession: AsyncSession,
) -> None:
    """同一 (class, datasource, schema, table, role) 只允许一条 `valid_to IS NULL`。"""
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    await dbSession.commit()

    await _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds)
    await dbSession.commit()

    assert (
        await _expectBindingRejected(
            dbSession, _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds)
        )
        == _UQ_PSB_ACTIVE
    )


async def test_source_binding_same_table_two_classes_are_allowed(
    dbSession: AsyncSession,
) -> None:
    """反向守卫：唯一键**含 `class_id`** —— 同表双类（实测 PRICE_LIST）必须放行。"""
    ds = await _insertDataSource(dbSession)
    cls_a = await _insertClass(dbSession, "DWD_SUPPLIER_PRICE_LIST")
    cls_b = await _insertClass(dbSession, "DWD_SUPPLIER_PRICE_LIST_DEL")
    await dbSession.commit()

    await _insertSourceBinding(
        dbSession, class_id=cls_a, datasource_id=ds, source_table="DWD_SUPPLIER_PRICE_LIST"
    )
    await _insertSourceBinding(
        dbSession, class_id=cls_b, datasource_id=ds, source_table="DWD_SUPPLIER_PRICE_LIST"
    )
    await dbSession.commit()

    count = (
        await dbSession.execute(text(f"SELECT count(*) FROM {_PSB}"))
    ).scalar_one()
    assert count == 2


async def test_source_binding_new_active_row_allowed_after_expiry(
    dbSession: AsyncSession,
) -> None:
    """部分唯一索引的谓词是 `valid_to IS NULL` ⇒ 旧行失效后可写新行。"""
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    await dbSession.commit()

    first = await _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds)
    # 不能用 now()：PostgreSQL 的 now() 是**事务开始时刻**，与插入时的 valid_from
    # 完全相同 ⇒ 会撞 ck_..._valid_range（要求 valid_to > valid_from）。
    await dbSession.execute(
        text(f"UPDATE {_PSB} SET valid_to = valid_from + interval '1 second' WHERE id = :id"),
        {"id": first},
    )
    await dbSession.commit()

    await _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds)
    await dbSession.commit()
    count = (
        await dbSession.execute(text(f"SELECT count(*) FROM {_PSB}"))
    ).scalar_one()
    assert count == 2


async def test_source_binding_candidate_with_confirmer_is_rejected(
    dbSession: AsyncSession,
) -> None:
    """D-3：候选行不得带确认人（库层钉死，不靠文档措辞）。"""
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    await dbSession.commit()

    assert (
        await _expectBindingRejected(
            dbSession,
            _insertSourceBinding(
                dbSession, class_id=cls, datasource_id=ds, confirmed_by="someone"
            ),
        )
        == _CK_PSB_CANDIDATE
    )


async def test_source_binding_confirmed_without_confirmer_is_rejected(
    dbSession: AsyncSession,
) -> None:
    """反向：非候选必须同时有确认人与确认时刻，否则「确认」只是改了个布尔。"""
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    await dbSession.commit()

    assert (
        await _expectBindingRejected(
            dbSession,
            _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds, is_candidate=False),
        )
        == _CK_PSB_CONFIRMED
    )


async def test_source_binding_role_literal_is_constrained(
    dbSession: AsyncSession,
) -> None:
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    await dbSession.commit()

    assert (
        await _expectBindingRejected(
            dbSession, _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds, role="MAIN")
        )
        == _CK_PSB_ROLE
    )


async def test_source_binding_valid_to_must_be_after_valid_from(
    dbSession: AsyncSession,
) -> None:
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    await dbSession.commit()

    assert (
        await _expectBindingRejected(
            dbSession,
            _insertSourceBinding(
                dbSession,
                class_id=cls,
                datasource_id=ds,
                valid_from="now() - interval '1 day'",
                valid_to="now() - interval '2 day'",
            ),
        )
        == _CK_PSB_RANGE
    )


async def test_property_binding_key_ordinal_is_unique_per_binding(
    dbSession: AsyncSession,
) -> None:
    """同一绑定内键序号不得重复 —— 4 列复合主键靠它表达。"""
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    prop_a = await _insertProperty(dbSession, cls, "PO_NO")
    prop_b = await _insertProperty(dbSession, cls, "PO_LINE_NO")
    binding = await _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds)
    await dbSession.commit()

    await dbSession.execute(
        text(
            f"INSERT INTO {_PPB} (source_binding_id, property_id, source_column, key_ordinal) "
            "VALUES (:b, :p, 'PO_NO', 1)"
        ),
        {"b": binding, "p": prop_a},
    )
    await dbSession.commit()

    with pytest.raises(IntegrityError) as caught:
        await dbSession.execute(
            text(
                f"INSERT INTO {_PPB} (source_binding_id, property_id, source_column, key_ordinal) "
                "VALUES (:b, :p, 'PO_LINE_NO', 1)"
            ),
            {"b": binding, "p": prop_b},
        )
        await dbSession.commit()
    await dbSession.rollback()
    assert _constraintName(caught.value) == _UQ_PPB_KEY


async def test_property_binding_duplicate_property_is_rejected(
    dbSession: AsyncSession,
) -> None:
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    prop = await _insertProperty(dbSession, cls, "PO_NO")
    binding = await _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds)
    await dbSession.commit()

    await dbSession.execute(
        text(
            f"INSERT INTO {_PPB} (source_binding_id, property_id, source_column) "
            "VALUES (:b, :p, 'PO_NO')"
        ),
        {"b": binding, "p": prop},
    )
    await dbSession.commit()

    with pytest.raises(IntegrityError) as caught:
        await dbSession.execute(
            text(
                f"INSERT INTO {_PPB} (source_binding_id, property_id, source_column) "
                "VALUES (:b, :p, 'PO_NO_ALT')"
            ),
            {"b": binding, "p": prop},
        )
        await dbSession.commit()
    await dbSession.rollback()
    assert _constraintName(caught.value) == _UQ_PPB_ACTIVE


async def test_property_binding_cascades_when_source_binding_deleted(
    dbSession: AsyncSession,
) -> None:
    """属性绑定的生命周期**从属**于源绑定 ⇒ CASCADE 是设计语义，不是笔误。"""
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    prop = await _insertProperty(dbSession, cls, "PO_NO")
    binding = await _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds)
    await dbSession.execute(
        text(
            f"INSERT INTO {_PPB} (source_binding_id, property_id, source_column) "
            "VALUES (:b, :p, 'PO_NO')"
        ),
        {"b": binding, "p": prop},
    )
    await dbSession.commit()

    await dbSession.execute(text(f"DELETE FROM {_PSB} WHERE id = :id"), {"id": binding})
    await dbSession.commit()

    count = (await dbSession.execute(text(f"SELECT count(*) FROM {_PPB}"))).scalar_one()
    assert count == 0


async def test_property_binding_property_delete_is_restricted(
    dbSession: AsyncSession,
) -> None:
    """本体属性是治理证据 ⇒ 被绑定时不允许删除。"""
    ds = await _insertDataSource(dbSession)
    cls = await _insertClass(dbSession, "DWD_X")
    prop = await _insertProperty(dbSession, cls, "PO_NO")
    binding = await _insertSourceBinding(dbSession, class_id=cls, datasource_id=ds)
    await dbSession.execute(
        text(
            f"INSERT INTO {_PPB} (source_binding_id, property_id, source_column) "
            "VALUES (:b, :p, 'PO_NO')"
        ),
        {"b": binding, "p": prop},
    )
    await dbSession.commit()

    with pytest.raises(IntegrityError) as caught:
        await dbSession.execute(
            text("DELETE FROM ontology_property WHERE id = :id"), {"id": prop}
        )
        await dbSession.commit()
    await dbSession.rollback()
    assert _constraintName(caught.value) == _FK_PPB_PROPERTY
