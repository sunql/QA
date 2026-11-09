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
