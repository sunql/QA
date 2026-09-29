"""authority_department 真实 PG + 完整 API 链路集成测试（v3.1 任务 M5 / 蓝图 §4.13）。

按 Harness/rules/测试规范.md 强制规则：真实 PostgreSQL（qa-pg-a1/qa_metadata_test）
+ 完整 HTTP 链路（路由 → service → ORM → 真实 DB）+ TRUNCATE 清库隔离。

覆盖：
1. **claim 链路落库可读**：ORM 造 claim + authority_department=SALES_MGMT → HTTP
   GET 取回仍是 SALES_MGMT
2. **页面创建带 department**：POST /pages {authorityDepartment: "FINANCE"} → 落库
   → GET 取回
3. **PATCH 显式 null 置空**：PATCH {authorityDepartment: null} → 字段变 NULL
4. **非法值 422**：POST/PATCH 传非法字符串 → ValidationError → 422
5. **alembic 0102 upgrade/downgrade 往返干净**：接 0101 之后，
   addColumn 落库、dropColumn 复原
6. **旧 claim 不带 department 字段**：序列化返 None 不抛（兼容迁移期数据）

设计要点：
- 不直接 ORM 写库造 page；走 POST /pages（要 page_id 才能 FK），claim 直 ORM
  （claim 暂未开放 POST 端点，与 test_wiki_api.py 既有模式一致）。
- 不调 LLM：claim 抽取路径不动，纯手工造行验证 authority_department 透传。

alembic 往返测试单独一个测试函数，套 try/finally 复原 alembic_version，
避免污染同一测试库上后续的 alembic current 断言。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import KNOWLEDGE_AUTHORITY_DEPARTMENTS, KnowledgeClaim

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/pages"


async def _createPage(
    client: AsyncClient,
    *,
    pageId: str | None = None,
    title: str = "供应商准入规则",
    department: str | None = None,
) -> dict:
    """通过 HTTP 真实链路建一条 page；可选带 authorityDepartment。"""
    payload: dict[str, str] = {"title": title, "content": "注册资本 >= 1000 万"}
    if pageId:
        payload["pageId"] = pageId
    if department is not None:
        payload["authorityDepartment"] = department
    resp = await client.post(_BASE, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. 页面级 authority_department：API 透传
# ---------------------------------------------------------------------------


async def test_create_page_with_authority_department_roundtrip(client: AsyncClient) -> None:
    """建 page 时带 FINANCE → GET 取回仍是 FINANCE（camelCase 契约）。

    验：DTO 校验通过、ORM 落库、读路径 camelCase 序列化一致。
    """
    created = await _createPage(
        client,
        pageId="AUTH-DEPT-FINANCE",
        title="财务部准入规则",
        department="FINANCE",
    )
    assert created["authorityDepartment"] == "FINANCE"

    resp = await client.get(f"{_BASE}/AUTH-DEPT-FINANCE")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorityDepartment"] == "FINANCE"


async def test_create_page_authority_department_null_legal(client: AsyncClient) -> None:
    """NULL 合法：默认（不传）= NULL，旧数据可继续落库无 department 字段。"""
    created = await _createPage(client, pageId="AUTH-DEPT-NULL")
    assert created["authorityDepartment"] is None

    resp = await client.get(f"{_BASE}/AUTH-DEPT-NULL")
    assert resp.status_code == 200
    assert resp.json()["authorityDepartment"] is None


async def test_create_page_authority_department_invalid_returns_422(
    client: AsyncClient,
) -> None:
    """非法值 → Pydantic ValidationError → 422（与 authority_level 同形）。"""
    resp = await client.post(
        _BASE,
        json={
            "title": "x",
            "content": "y",
            "authorityDepartment": "NOT_A_DEPARTMENT",
        },
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("department", list(KNOWLEDGE_AUTHORITY_DEPARTMENTS))
async def test_create_page_accepts_every_department(
    client: AsyncClient, department: str
) -> None:
    """11 枚举值全部接受：保证 ORM 元组与 DTO 引用完全同步。"""
    page_id = f"AUTH-DEPT-{department}"
    created = await _createPage(
        client,
        pageId=page_id,
        title=f"测试 {department}",
        department=department,
    )
    assert created["authorityDepartment"] == department


async def test_patch_authority_department_explicit_null_clears(
    client: AsyncClient,
) -> None:
    """PATCH 显式 null = 置空（与 dimension 同语义）。"""
    await _createPage(
        client,
        pageId="AUTH-DEPT-CLEAR",
        department="QA",
    )

    # 显式置空
    resp = await client.patch(
        f"{_BASE}/AUTH-DEPT-CLEAR",
        json={"authorityDepartment": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["authorityDepartment"] is None

    # 再 GET 一次确认落库
    resp2 = await client.get(f"{_BASE}/AUTH-DEPT-CLEAR")
    assert resp2.json()["authorityDepartment"] is None


async def test_patch_authority_department_change_value(client: AsyncClient) -> None:
    """PATCH 切换部门（如 QA → INDUSTRY_STANDARD）。"""
    await _createPage(client, pageId="AUTH-DEPT-SWITCH", department="QA")

    resp = await client.patch(
        f"{_BASE}/AUTH-DEPT-SWITCH",
        json={"authorityDepartment": "INDUSTRY_STANDARD"},
    )
    assert resp.status_code == 200
    assert resp.json()["authorityDepartment"] == "INDUSTRY_STANDARD"


async def test_patch_authority_department_invalid_returns_422(
    client: AsyncClient,
) -> None:
    """PATCH 非法值 422（Create 同款校验）。"""
    await _createPage(client, pageId="AUTH-DEPT-PATCH-INVALID", department="FINANCE")

    resp = await client.patch(
        f"{_BASE}/AUTH-DEPT-PATCH-INVALID",
        json={"authorityDepartment": "FAKE_DEPT"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 2. Claim 链路：authority_department 独立于 page 的归属
# ---------------------------------------------------------------------------


async def test_claim_authority_department_roundtrip(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """claim 级 authority_department 直 ORM 写库 → list endpoint 序列化可读。

    路径选 ORM 直造：claim 创建端点（POST /claims）尚未开放，按
    test_wiki_api.py:304 既有模式（KnowledgeClaim ORM 造数 + HTTP GET 取）。
    验：ORM 字段透传、读模型 KnowledgeClaimRead 序列化、camelCase 契约。
    """
    page = await _createPage(
        client, pageId="AUTH-DEPT-CLAIM-PAGE", department="FINANCE"
    )
    page_id = page["pageId"]

    # 直 ORM 写一条带 authority_department 的 claim（与既有 fixture 一致）
    claim = KnowledgeClaim(
        page_id=page_id,
        claim_text="注册资本 >= 1000 万",
        claim_type="RULE",
        authority_department="SALES_MGMT",
    )
    dbSession.add(claim)
    await dbSession.commit()
    await dbSession.refresh(claim)

    # 通过 HTTP 真实链路 GET 取回
    resp = await client.get(f"{_BASE}/{page_id}/claims")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["authorityDepartment"] == "SALES_MGMT"


async def test_claim_without_authority_department_serializes_none(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """迁移期旧 claim（不带 department 字段）→ 序列化返 None，不抛。

    这是 PATCH 时 drop column / add column 切换窗口期最重要的兼容性保证：
    旧行 authority_department IS NULL → 读路径不抛错。
    """
    page = await _createPage(client, pageId="AUTH-DEPT-LEGACY")
    page_id = page["pageId"]

    # 模拟迁移前旧 claim（无 authority_department 字段）
    legacy = KnowledgeClaim(
        page_id=page_id,
        claim_text="成立 >= 3 年",
        claim_type="RULE",
        # 故意不传 authority_department —— 默认 NULL
    )
    dbSession.add(legacy)
    await dbSession.commit()
    await dbSession.refresh(legacy)

    resp = await client.get(f"{_BASE}/{page_id}/claims")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["authorityDepartment"] is None  # 兼容旧数据


async def test_page_and_claim_authority_department_independent(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """page 与 claim 的 authority_department 是**独立两列**。

    设计意图：claim 可能由不同部门产生，即使 page 归属 QA 部，claim 也可能
    由 SCM 部补充。同一对 (page, claim) 不强制两字段一致。
    """
    page = await _createPage(
        client, pageId="AUTH-DEPT-INDEPENDENT", department="QA"
    )
    page_id = page["pageId"]

    claim = KnowledgeClaim(
        page_id=page_id,
        claim_text="x",
        claim_type="RULE",
        authority_department="SCM",
    )
    dbSession.add(claim)
    await dbSession.commit()

    # page 端仍是 QA
    page_resp = await client.get(f"{_BASE}/{page_id}")
    assert page_resp.json()["authorityDepartment"] == "QA"

    # claim 端是 SCM（独立）
    claim_resp = await client.get(f"{_BASE}/{page_id}/claims")
    assert claim_resp.json()[0]["authorityDepartment"] == "SCM"


# ---------------------------------------------------------------------------
# 3. alembic 0102 upgrade/downgrade 往返（接 0101 之后）
# ---------------------------------------------------------------------------


_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/
_REQUIRED_DB = "qa_metadata_test"
_HEAD = "0102_authority_department"
_PREV = "0101"
_TABLE_PAGE = "wiki_page"
_TABLE_CLAIM = "knowledge_claim"
_COLUMN = "authority_department"
_ALEMBIC_TIMEOUT_SEC = 120


def _assertTestDb(url: str) -> None:
    """写库闸：只认测试库，否则 raise（不执行任何 DDL）。"""
    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    dbName = match.group(1) if match else ""
    if dbName != _REQUIRED_DB:
        raise RuntimeError(
            f"拒绝对非测试库执行迁移：解析到库名 {dbName!r}，要求 {_REQUIRED_DB!r}。"
            "alembic/env.py 只读 DATABASE_URL（TEST_DATABASE_URL 被静默忽略）。"
        )


def _alembic(url: str, *args: str) -> str:
    """在测试库上跑一条 alembic 子命令；先过写库闸。"""
    _assertTestDb(url)
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_ALEMBIC_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} 失败（rc={result.returncode}）：{result.stderr[-2000:]}"
    )
    return result.stdout


async def _columnMeta(
    dbSession: AsyncSession, table: str, column: str
) -> tuple[str, str] | None:
    """读真实 PG information_schema：指定列的 (data_type, is_nullable)。"""
    await dbSession.rollback()
    result = await dbSession.execute(
        text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    row = result.first()
    return (row[0], row[1]) if row else None


async def test_alembic_0102_authority_department_roundtrip(
    dbSession: AsyncSession,
) -> None:
    """alembic 0102 升级/降级往返干净（接 0101 之后）。

    契约：
    - upgrade head 后 wiki_page + knowledge_claim 多出 authority_department 列
      （VARCHAR(30) NULL）
    - downgrade 0101 后两列消失
    - 再 upgrade head 复原 —— 双向对称

    安全闸：_assertTestDb 强制库名为 qa_metadata_test；alembic/env.py 只读
    DATABASE_URL，传错会打到 prod。
    """
    from app.tests._pg_support import resolveTestDatabaseUrl

    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    try:
        # 先升级到 head —— 不论当前在哪都能往上走（alembic 会自动 upgrade head）
        _alembic(url, "upgrade", "head")

        # 两列都已存在
        await dbSession.rollback()
        meta_page = await _columnMeta(dbSession, _TABLE_PAGE, _COLUMN)
        meta_claim = await _columnMeta(dbSession, _TABLE_CLAIM, _COLUMN)
        assert meta_page is not None, f"wiki_page.{_COLUMN} 应已存在"
        assert meta_claim is not None, f"knowledge_claim.{_COLUMN} 应已存在"
        # data_type: VARCHAR(30) 在 PG 里是 character varying
        assert meta_page[0] == "character varying", (
            f"wiki_page.{_COLUMN} 类型应为 character varying，实际 {meta_page[0]}"
        )
        assert meta_claim[0] == "character varying"
        # nullable 必须是 YES（旧数据 / 治理未推动场景要求 NULL）
        assert meta_page[1] == "YES", f"wiki_page.{_COLUMN} 应可空，实际 {meta_page[1]}"
        assert meta_claim[1] == "YES"

        # 降到 0101 —— 验证 downgrade 把两列都 drop 掉
        _alembic(url, "downgrade", _PREV)

        await dbSession.rollback()
        meta_page = await _columnMeta(dbSession, _TABLE_PAGE, _COLUMN)
        meta_claim = await _columnMeta(dbSession, _TABLE_CLAIM, _COLUMN)
        assert meta_page is None, f"降级后 wiki_page.{_COLUMN} 应消失"
        assert meta_claim is None, f"降级后 knowledge_claim.{_COLUMN} 应消失"

        # 再 upgrade head 复原
        _alembic(url, "upgrade", "head")
        await dbSession.rollback()
        meta_page = await _columnMeta(dbSession, _TABLE_PAGE, _COLUMN)
        meta_claim = await _columnMeta(dbSession, _TABLE_CLAIM, _COLUMN)
        assert meta_page is not None, "再升级后 wiki_page 列应复现"
        assert meta_claim is not None, "再升级后 knowledge_claim 列应复现"

        # 验：升完级别参数与 head 配置一致（avoid「同版本号不同结构」漂移）。
        # 注：alembic_version 表存的是短 ID（如 "0102"），与文件 revision 变量
        # 等长；文件名前缀是迁移文件名（grep 用），不是 revision ID。
        version = (
            await dbSession.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        assert version == "0102", f"alembic_version 应为 0102，实际 {version}"
    finally:
        # 兜底：保证下次测试拿到 head 状态（不依赖调用方按顺序跑）
        try:
            _alembic(url, "upgrade", "head")
        except Exception:
            # 已经在 head 时 alembic 不会报错；其他错误吞掉，避免 teardown 噪声
            pass