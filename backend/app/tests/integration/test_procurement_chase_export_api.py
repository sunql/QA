"""催货单导出端点契约（真实 PostgreSQL + 完整 HTTP 链路）。

覆盖（每条都是会咬人的失败模式）：

1. **200 + 附件**：xlsx / docx 两种格式都真的产出文件（不是空 body）；
2. **404**：催货单不存在（含**非法单号** —— 不能打成 500）；
3. **400**：``format`` 非法；
4. **401**：凭证无效（token 无效 ⇒ 身份无法确认）；
5. **403**：非 admin 登录（``requireAdmin`` 生效）；
6. **公司法定名称未配置 ⇒ 仍可导出**（记 warning，不阻塞）。

夹具**现场建**：一个 admin 账号（``qa_export_admin``）+ 一个普通账号 + 一张催货单
（+2 行明细）。**不重置 / 不覆盖任何已有账号**（项目规则「测试身份新建账号」）。

关于 401：平台口径是「**身份无法确认 ⇒ 401**（token 无效/过期/已吊销）；
「身份确认但权限不足 ⇒ 403」。匿名 stub 兜底（``AUTH_MODE=stub`` 的开发期
便利）会让无凭证请求拿到默认角色 ⇒ 与其它催货端点同口径，故此处用
**无效 Bearer token** 断言 401。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import Column, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base

from app.domain.procurement_chase_models import (
    ProcurementChaseDraft,
    ProcurementChaseLine,
)
from app.models.rbac import Role, User, UserRole

ADMIN_USERNAME = "qa_export_admin"
ADMIN_PASSWORD = "QaExport@2026"
VIEWER_USERNAME = "qa_export_viewer"
VIEWER_PASSWORD = "QaExport@2026"

#: bcrypt cost（与生产同档；建账号只在本文件内，不碰任何已有账号）
BCRYPT_ROUNDS = 12

SUPPLIER_CODE = "Q1630"
COMPANY_NAME_CONFIG_KEY = "chase.letter.company_name"


# ---------------------------------------------------------------------------
# 夹具：账号 / 催货单
# ---------------------------------------------------------------------------


async def _ensureRole(dbSession: AsyncSession, code: str) -> Role:
    role = (
        await dbSession.execute(select(Role).where(Role.code == code))
    ).scalar_one_or_none()
    if role is None:
        role = Role(code=code, name=code, description=None)
        dbSession.add(role)
        await dbSession.flush()
    return role


async def _createUser(
    dbSession: AsyncSession, *, username: str, password: str, roleCode: str
) -> User:
    """**新建**测试账号（不重置/覆盖任何已有账号）。"""
    role = await _ensureRole(dbSession, roleCode)
    user = User(
        username=username,
        display_name=username,
        email=None,
        enabled=True,
        password_hash=bcrypt.hashpw(
            password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
        ).decode(),
        # 必须为 False：mustChangePasswordGuard 会把非白名单端点一律 403
        must_change_password=False,
    )
    dbSession.add(user)
    await dbSession.flush()
    dbSession.add(UserRole(user_id=user.id, role_id=role.id))
    await dbSession.commit()
    return user


@pytest.fixture()
async def exportUsers(dbSession: AsyncSession) -> None:
    """现场建两个测试账号：admin + 普通用户。"""
    await _createUser(
        dbSession,
        username=ADMIN_USERNAME,
        password=ADMIN_PASSWORD,
        roleCode="admin",
    )
    await _createUser(
        dbSession,
        username=VIEWER_USERNAME,
        password=VIEWER_PASSWORD,
        roleCode="user",
    )


@pytest.fixture()
async def seededChaseDraft(dbSession: AsyncSession) -> tuple[str, str]:
    """造一张 DRAFT 催货单 + 两行明细（真实 ORM 行）。"""
    chaseDraftId = str(uuid.uuid4())
    draft = ProcurementChaseDraft(
        chase_draft_id=uuid.UUID(chaseDraftId),
        version=1,
        status="DRAFT",
        request_id=f"CHASE-{chaseDraftId[:8]}",
        supplier_enterprise_key=900123456,
        supplier_code=SUPPLIER_CODE,
        expected_reply_deadline=datetime.now(UTC) + timedelta(days=7),
        notes="催货单导出测试",
        payload_json={"lines": []},
        payload_hash="0" * 64,
        idempotency_key=f"export-test-{chaseDraftId}",
        created_by=ADMIN_USERNAME,
    )
    dbSession.add(draft)
    await dbSession.flush()
    dbSession.add_all(
        [
            ProcurementChaseLine(
                chase_draft_id=uuid.UUID(chaseDraftId),
                po_no="PO-EXP-1",
                po_line_no="1",
                material_code="M001",
                material_desc="测试物料一",
                ordered_qty=Decimal("12"),
                received_qty=Decimal("2"),
                open_qty=Decimal("10"),
                expected_date=date(2026, 11, 20),
                overdue_days=5,
                is_dirty=False,
            ),
            ProcurementChaseLine(
                chase_draft_id=uuid.UUID(chaseDraftId),
                po_no="PO-EXP-2",
                po_line_no="1",
                material_code="M002",
                material_desc="测试物料二",
                ordered_qty=Decimal("8"),
                received_qty=Decimal("3"),
                open_qty=Decimal("5"),
                expected_date=date(2026, 11, 22),
                overdue_days=3,
                is_dirty=False,
            ),
        ]
    )
    await dbSession.commit()
    return chaseDraftId, SUPPLIER_CODE


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["accessToken"]


def _exportUrl(chaseDraftId: str, format: str = "xlsx") -> str:
    return f"/api/v1/procurement/chase/drafts/{chaseDraftId}/export?format={format}"


def _missingConfigModel():
    """declarative 替身：映射到一张**不存在**的表。

    用途：让「读配置」这一步产生**真正的 PG 错误**（UndefinedTable）——
    只有真错误才会把 PG 事务置为 aborted；Python 层异常（如属性访问失败）
    根本不会发 SQL，事务毫发无损 ⇒ 用例**区分不出有没有 savepoint**。
    """
    base = declarative_base()

    class BrokenConfig(base):
        __tablename__ = "system_config_definitely_not_exist"
        key = Column(String(64), primary_key=True)
        value = Column(Text)

    return BrokenConfig


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestChaseExport:
    async def test_export_xlsx_returns_attachment(
        self, client: AsyncClient, exportUsers, seededChaseDraft, caplog
    ) -> None:
        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        chaseDraftId, _supplierCode = seededChaseDraft
        with caplog.at_level(logging.WARNING, logger="app.api.v1.procurement_chase"):
            resp = await client.get(
                _exportUrl(chaseDraftId), headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 200, resp.text
        disposition = resp.headers.get("content-disposition", "")
        assert "attachment" in disposition
        # RFC 5987：ASCII 回退名 + 中文名的 UTF-8 百分号编码
        assert "filename*=UTF-8''" in disposition
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml"
        )
        assert len(resp.content) > 0
        # Excel 的信息 Sheet 没有公司名行 ⇒ xlsx 不该读配置、更不该打「抬头留空」warning
        assert COMPANY_NAME_CONFIG_KEY not in caplog.text, (
            f"xlsx 导出不应产生公司名 warning，实际日志：{caplog.text}"
        )

    async def test_export_docx_returns_attachment(
        self, client: AsyncClient, exportUsers, seededChaseDraft
    ) -> None:
        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        chaseDraftId, _supplierCode = seededChaseDraft
        resp = await client.get(
            _exportUrl(chaseDraftId, "docx"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml"
        )
        assert resp.content[:2] == b"PK", "docx 是 zip"

    async def test_export_unknown_draft_404(
        self, client: AsyncClient, exportUsers
    ) -> None:
        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = await client.get(
            _exportUrl("00000000-0000-0000-0000-000000000000"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, resp.text

    async def test_export_malformed_draft_id_404(
        self, client: AsyncClient, exportUsers
    ) -> None:
        """单号不是 UUID ⇒ 404（不是 500：语义上就是「不存在」）。"""
        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = await client.get(
            _exportUrl("does-not-exist"), headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404, resp.text

    async def test_both_404_shapes_are_identical(
        self, client: AsyncClient, exportUsers
    ) -> None:
        """两条 404（不存在 / 非法单号）走**同一个异常类型** ⇒ 响应体形状一致。

        否则前端会看到两种错误包络（`{"detail": ...}` vs 统一包络）⇒ 分支代码。
        """
        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        missing = await client.get(
            _exportUrl("00000000-0000-0000-0000-000000000000"),
            headers={"Authorization": f"Bearer {token}"},
        )
        malformed = await client.get(
            _exportUrl("does-not-exist"), headers={"Authorization": f"Bearer {token}"}
        )
        assert missing.status_code == malformed.status_code == 404
        assert set(missing.json()) == set(malformed.json()), (
            f"两条 404 的响应体形状不一致：{missing.json()} vs {malformed.json()}"
        )

    async def test_export_bad_format_400(
        self, client: AsyncClient, exportUsers, seededChaseDraft
    ) -> None:
        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        chaseDraftId, _supplierCode = seededChaseDraft
        resp = await client.get(
            _exportUrl(chaseDraftId, "pdf"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400, resp.text

    async def test_export_requires_valid_credentials_401(
        self, client: AsyncClient, exportUsers, seededChaseDraft
    ) -> None:
        """无效 Bearer token ⇒ 401（身份无法确认）。"""
        chaseDraftId, _supplierCode = seededChaseDraft
        resp = await client.get(
            _exportUrl(chaseDraftId),
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401, resp.text

    async def test_export_non_admin_403(
        self, client: AsyncClient, exportUsers, seededChaseDraft
    ) -> None:
        """非 admin 登录 ⇒ 403（requireAdmin 生效）。"""
        token = await _login(client, VIEWER_USERNAME, VIEWER_PASSWORD)
        chaseDraftId, _supplierCode = seededChaseDraft
        resp = await client.get(
            _exportUrl(chaseDraftId), headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403, resp.text

    async def test_export_without_company_name_still_succeeds(
        self, client: AsyncClient, exportUsers, seededChaseDraft, caplog
    ) -> None:
        """公司法定名称**未配置** ⇒ 仍可导出，且服务端记 warning（不阻塞）。"""
        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        chaseDraftId, _supplierCode = seededChaseDraft
        with caplog.at_level(logging.WARNING, logger="app.api.v1.procurement_chase"):
            resp = await client.get(
                _exportUrl(chaseDraftId, "docx"),
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200, resp.text
        assert len(resp.content) > 0
        assert COMPANY_NAME_CONFIG_KEY in caplog.text, (
            f"未配置公司名时应记 warning，实际日志：{caplog.text}"
        )

    async def test_export_uses_configured_company_name(
        self, client: AsyncClient, exportUsers, seededChaseDraft, dbSession
    ) -> None:
        """配置了公司法定名称 ⇒ 出现在催货函抬头（读 system_config）。"""
        from app.models.system_config import SystemConfig

        dbSession.add(
            SystemConfig(
                key=COMPANY_NAME_CONFIG_KEY,
                value="测试采购有限公司",
                description="催货函抬头",
            )
        )
        await dbSession.commit()

        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        chaseDraftId, _supplierCode = seededChaseDraft
        resp = await client.get(
            _exportUrl(chaseDraftId, "docx"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        import io

        from docx import Document

        document = Document(io.BytesIO(resp.content))
        texts = [paragraph.text for paragraph in document.paragraphs]
        assert "测试采购有限公司" in texts

    async def test_export_falls_back_to_supplier_code(
        self, client: AsyncClient, exportUsers, seededChaseDraft
    ) -> None:
        """MDM 查不到供应商名 ⇒ 回落编码，导出**不被阻塞**。"""
        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        chaseDraftId, supplierCode = seededChaseDraft
        resp = await client.get(
            _exportUrl(chaseDraftId, "docx"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        import io

        from docx import Document

        document = Document(io.BytesIO(resp.content))
        texts = [paragraph.text for paragraph in document.paragraphs]
        assert any(supplierCode in text for text in texts)

    async def test_export_uses_mdm_supplier_name(
        self, client: AsyncClient, exportUsers, seededChaseDraft, dbSession
    ) -> None:
        """MDM 目录命中 ⇒ 催货函抬头用供应商全名（不是编码）。"""
        from app.domain.models import EntityMapping

        dbSession.add(
            EntityMapping(
                entity_type="SUPPLIER",
                enterprise_key=900123456,
                enterprise_code=SUPPLIER_CODE,
                source_system="ERP",
                source_key=SUPPLIER_CODE,
                source_code=SUPPLIER_CODE,
                match_rule="MAPPING",
                name="测试供应商有限公司",
            )
        )
        await dbSession.commit()

        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        chaseDraftId, _supplierCode = seededChaseDraft
        resp = await client.get(
            _exportUrl(chaseDraftId, "docx"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        import io

        from docx import Document

        document = Document(io.BytesIO(resp.content))
        texts = [paragraph.text for paragraph in document.paragraphs]
        assert any("测试供应商有限公司" in text for text in texts)

    async def test_export_survives_config_read_failure(
        self, client: AsyncClient, exportUsers, seededChaseDraft, dbSession, monkeypatch
    ) -> None:
        """配置读取炸了 ⇒ 导出**不被阻塞**，且后续查询仍可用（savepoint 隔离）。

        故障必须是**真正的 PG 错误**（读一张不存在的表 ⇒ UndefinedTable）——
        只有真错误才会把 PG 事务置为 aborted；Python 层异常（如属性访问失败）
        根本不会发 SQL，事务毫发无损 ⇒ 这样的用例**区分不出有没有 savepoint**。

        若配置读取失败把事务打成 aborted，后续的 MDM 查询会连环失败 ⇒
        供应商名会静默退化成编码。此用例用「供应商名仍然解析得到」守住这一点。
        """
        import app.api.v1.procurement_chase as chaseApi
        from app.domain.models import EntityMapping

        dbSession.add(
            EntityMapping(
                entity_type="SUPPLIER",
                enterprise_key=900123456,
                enterprise_code=SUPPLIER_CODE,
                source_system="ERP",
                source_key=SUPPLIER_CODE,
                source_code=SUPPLIER_CODE,
                match_rule="MAPPING",
                name="测试供应商有限公司",
            )
        )
        await dbSession.commit()

        monkeypatch.setattr(chaseApi, "SystemConfig", _missingConfigModel())

        token = await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        chaseDraftId, _supplierCode = seededChaseDraft
        resp = await client.get(
            _exportUrl(chaseDraftId, "docx"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        import io

        from docx import Document

        document = Document(io.BytesIO(resp.content))
        texts = [paragraph.text for paragraph in document.paragraphs]
        assert any("测试供应商有限公司" in text for text in texts)
