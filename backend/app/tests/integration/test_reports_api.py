"""A8（M4 Report 模板 MVP）集成测试（真实 PG + 完整 API 链路）。

覆盖（brief §验收 integration 部分）：
- monthly-ops-v1 生成 → PENDING_REVIEW 落库 → admin approve → 全员可见
- supplier-360-v1 同链路（含真实 entity_mapping 绑定）
- 非 admin 生成 + 他人 PENDING 不可见（404）+ review 403
- LLM 计数 = 1（fake factory 共享计数）+ purpose="report_summary" 进 token_usage
- LLM 不可用 / 失败 → 总结降级「（总结生成失败）」+ 报告主体照常落库
- 非法 params → 422；未知模板 → 404；供应商不存在 → 该节 renderError，不拖垮报告
- 列表可见性收敛（非 admin 只看自己的 + 全员 APPROVED）

数据层全真实 PG；LLM 走 fake factory monkeypatch（test_chat_api 同模式）。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.v1.reports as reports_module
from app.domain.enums import MatchRule, SourceSystem
from app.domain.models import (
    EntityMapping,
    LlmConfig,
    SessionTokenUsage,
)
from app.infrastructure.security.crypto import encryptApiKey

# 非 admin 用户：必须显式 X-User-Roles 降权（stub 默认角色含 admin，
# 与 test_evidence_acl 同口径）
ALICE_HEADERS = {"X-User-Id": "alice", "X-User-Roles": "user"}
BOB_HEADERS = {"X-User-Id": "bob", "X-User-Roles": "user"}
ADMIN_HEADERS = {"X-User-Id": "admin-user"}  # 不在 DB → stub 默认角色含 admin

MONTHLY_PARAMS = {"month": "2026-09", "supplierKey": "10105"}
SUMMARY_JSON = json.dumps(
    {"lines": ["[事实] 9 月 OTD 达标", "下月预计回升"]}, ensure_ascii=False
)


class _ReportLlm:
    """按共享计数：报告总结恰好 ≤1 次 LLM 的断言载体。

    返回逐行带 [事实]/[推断]/[假设] 前缀的 JSON（含 fence 验证 SSOT 剥离开箱）。
    mode: ok / empty_parse / raise
    """

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls = 0

    async def complete(self, messages, model=None):
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "empty_parse":
            return type("R", (), {"content": "not-json", "promptTokens": 3,
                                  "completionTokens": 2, "cachedTokens": None})()
        return type("R", (), {
            "content": "```json\n" + SUMMARY_JSON + "\n```",
            "promptTokens": 10, "completionTokens": 5, "cachedTokens": None,
        })()


async def _seed_llm_config(dbSession: AsyncSession) -> LlmConfig:
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        is_active=True,
        api_key_encrypted=encryptApiKey("sk-test"),
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
        cost_threshold=Decimal("999"),
        weight=10,
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config


async def _seed_kpi(client: AsyncClient, code: str) -> None:
    resp = await client.post(
        "/api/v1/kpi-catalog",
        json={"kpiCode": code, "kpiName": f"{code} 名称", "formula": "SELECT 1",
              "unit": "%", "owner": "采购部"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text


async def _seed_supplier(dbSession: AsyncSession) -> None:
    dbSession.add(
        EntityMapping(
            entity_type="SUPPLIER",
            enterprise_key=900001,
            enterprise_code="10105",
            source_system=SourceSystem.ERP,
            source_code="V0001",
            source_key="V0001",
            match_rule=MatchRule.MDM_MASTER,
        )
    )
    await dbSession.commit()


async def _generate(client: AsyncClient, headers: dict, **overrides) -> dict:
    payload = {"templateCode": "monthly-ops-v1", "params": MONTHLY_PARAMS}
    payload.update(overrides)
    resp = await client.post("/api/v1/reports/generate", json=payload, headers=headers)
    return resp


@pytest.mark.asyncio
class TestTemplatesEndpoint:
    async def test_lists_two_templates_with_params_schema(self, client) -> None:
        resp = await client.get("/api/v1/reports/templates", headers=ALICE_HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert isinstance(body, list)
        assert {t["code"] for t in body} == {"monthly-ops-v1", "supplier-360-v1"}
        monthly = next(t for t in body if t["code"] == "monthly-ops-v1")
        assert monthly["title"]
        names = {f["name"] for f in monthly["paramsSchema"]}
        assert {"month", "supplierKey"} <= names

    async def test_invalid_status_filter_422(self, client) -> None:
        resp = await client.get(
            "/api/v1/reports", params={"status": "BOGUS"}, headers=ALICE_HEADERS
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
class TestGenerateAndReviewFlow:
    async def test_monthly_ops_generate_approve_then_visible(
        self, client, dbSession, monkeypatch
    ) -> None:
        llm = _ReportLlm()
        monkeypatch.setattr(reports_module._service, "_llmFactory", lambda _c: llm)
        await _seed_llm_config(dbSession)
        await _seed_kpi(client, "KPI_SUPPLIER_OTD")
        await _seed_supplier(dbSession)

        resp = await _generate(client, ALICE_HEADERS)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        body = resp.json()
        instanceId = body["id"]
        assert body["status"] == "PENDING_REVIEW"
        assert body["createdBy"] == "alice"
        assert len(body["sections"]) == 4
        bySection = {s["sectionId"]: s for s in body["sections"]}
        # KPI 卡片：种子行 found=True；未种子的 found=False（不报错）
        overview = bySection["kpi-overview"]["data"]
        assert overview[0]["kpiCode"] == "KPI_SUPPLIER_OTD"
        assert any(row["found"] for row in overview)
        # 供应商 360 绑定成功（entity_mapping 已种）
        assert bySection["supplier-kpis"]["renderError"] is None
        assert bySection["supplier-identity"]["renderError"] is None
        # 总结：fence SSOT 剥离 + JSON 解析 + 无前缀行兜底 [推断]
        assert "[事实] 9 月 OTD 达标" in body["summary"]
        assert "[推断] 下月预计回升" in body["summary"]

        # LLM 预算：恰好 1 次；计量落 token_usage（purpose=report_summary）
        assert llm.calls == 1
        usage_rows = (
            await dbSession.execute(
                select(SessionTokenUsage).where(
                    SessionTokenUsage.purpose == "report_summary"
                )
            )
        ).scalars().all()
        assert len(usage_rows) == 1
        assert usage_rows[0].prompt_tokens == 10
        assert usage_rows[0].completion_tokens == 5

        # 他人 PENDING 不可见（404，不泄露存在性）
        other = await client.get(f"/api/v1/reports/{instanceId}", headers=BOB_HEADERS)
        assert other.status_code == status.HTTP_404_NOT_FOUND

        # admin approve → 全员可见
        review = await client.post(
            f"/api/v1/reports/{instanceId}/review",
            json={"decision": "APPROVE", "note": "数据核对无误"},
            headers=ADMIN_HEADERS,
        )
        assert review.status_code == status.HTTP_200_OK
        assert review.json()["status"] == "APPROVED"
        assert review.json()["reviewedBy"] == "admin-user"

        visible = await client.get(f"/api/v1/reports/{instanceId}", headers=BOB_HEADERS)
        assert visible.status_code == status.HTTP_200_OK
        assert visible.json()["status"] == "APPROVED"

        # 状态机：已审批不可重复审批 → 409
        again = await client.post(
            f"/api/v1/reports/{instanceId}/review",
            json={"decision": "REJECT"},
            headers=ADMIN_HEADERS,
        )
        assert again.status_code == status.HTTP_409_CONFLICT

    async def test_supplier360_template_flow(self, client, dbSession, monkeypatch) -> None:
        llm = _ReportLlm()
        monkeypatch.setattr(reports_module._service, "_llmFactory", lambda _c: llm)
        await _seed_llm_config(dbSession)
        await _seed_supplier(dbSession)

        resp = await client.post(
            "/api/v1/reports/generate",
            json={"templateCode": "supplier-360-v1", "params": {"supplierKey": "10105"}},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        body = resp.json()
        assert body["status"] == "PENDING_REVIEW"
        bySection = {s["sectionId"]: s for s in body["sections"]}
        profile = bySection["supplier-profile"]["data"]
        assert {row["field"] for row in profile} == {
            "enterpriseKey", "enterpriseCode", "entityType",
        }
        assert bySection["supplier-entity-codes"]["data"][0]["sourceSystem"] == "ERP"
        assert bySection["supplier-kpi-values"]["renderError"] is None
        assert llm.calls == 1

    async def test_review_rejected_flow(self, client, monkeypatch) -> None:
        llm = _ReportLlm(mode="empty_parse")
        monkeypatch.setattr(reports_module._service, "_llmFactory", lambda _c: llm)
        await _generate(client, ALICE_HEADERS)
        list_resp = await client.get("/api/v1/reports", headers=ADMIN_HEADERS)
        instanceId = list_resp.json()["rows"][0]["id"]

        review = await client.post(
            f"/api/v1/reports/{instanceId}/review",
            json={"decision": "REJECT", "note": "口径待复核"},
            headers=ADMIN_HEADERS,
        )
        assert review.status_code == status.HTTP_200_OK
        assert review.json()["status"] == "REJECTED"
        # 被拒报告对他人不可见
        other = await client.get(f"/api/v1/reports/{instanceId}", headers=BOB_HEADERS)
        assert other.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
class TestVisibilityGuards:
    async def test_non_admin_review_forbidden(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            reports_module._service, "_llmFactory", lambda _c: _ReportLlm()
        )
        await _generate(client, ALICE_HEADERS)
        list_resp = await client.get("/api/v1/reports", headers=ALICE_HEADERS)
        instanceId = list_resp.json()["rows"][0]["id"]
        resp = await client.post(
            f"/api/v1/reports/{instanceId}/review",
            json={"decision": "APPROVE"},
            headers=ALICE_HEADERS,
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_list_scopes_visibility(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            reports_module._service, "_llmFactory", lambda _c: _ReportLlm()
        )
        await _generate(client, ALICE_HEADERS)  # alice 的 PENDING
        # bob 看不到 alice 的 PENDING
        bob_list = await client.get("/api/v1/reports", headers=BOB_HEADERS)
        assert bob_list.json()["total"] == 0
        # alice 看得到自己的
        alice_list = await client.get("/api/v1/reports", headers=ALICE_HEADERS)
        assert alice_list.json()["total"] == 1
        # admin 全见
        admin_list = await client.get("/api/v1/reports", headers=ADMIN_HEADERS)
        assert admin_list.json()["total"] == 1
        # 状态过滤：admin 按 PENDING_REVIEW 过滤
        pending = await client.get(
            "/api/v1/reports", params={"status": "PENDING_REVIEW"}, headers=ADMIN_HEADERS
        )
        assert pending.json()["total"] == 1
        approved = await client.get(
            "/api/v1/reports", params={"status": "APPROVED"}, headers=ADMIN_HEADERS
        )
        assert approved.json()["total"] == 0


@pytest.mark.asyncio
class TestDegradationAndValidation:
    async def test_no_llm_config_degrades_summary(self, client) -> None:
        # 不种 LLM 配置：不触发不调，报告主体照常落库
        resp = await _generate(client, ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        body = resp.json()
        assert body["status"] == "PENDING_REVIEW"
        assert body["summary"] == "（总结生成失败）"
        assert len(body["sections"]) == 4

    async def test_llm_failure_still_persists_report(self, client, dbSession, monkeypatch) -> None:
        llm = _ReportLlm(mode="raise")
        monkeypatch.setattr(reports_module._service, "_llmFactory", lambda _c: llm)
        await _seed_llm_config(dbSession)
        resp = await _generate(client, ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        assert llm.calls == 1
        assert resp.json()["summary"] == "（总结生成失败）"

    async def test_missing_supplier_renders_error_section(self, client) -> None:
        # 供应商不存在 → 该两节 renderError，其余节照常（异常隔离）
        resp = await _generate(
            client, ADMIN_HEADERS, params={"month": "2026-09", "supplierKey": "NOSUCH"}
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        bySection = {s["sectionId"]: s for s in resp.json()["sections"]}
        assert bySection["supplier-kpis"]["renderError"]
        assert bySection["kpi-overview"]["renderError"] is None

    async def test_invalid_params_422(self, client) -> None:
        resp = await _generate(
            client, ADMIN_HEADERS, params={"month": "not-a-month", "supplierKey": "10105"}
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_unknown_template_404(self, client) -> None:
        resp = await _generate(client, ADMIN_HEADERS, templateCode="nope")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
