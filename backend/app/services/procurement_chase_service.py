"""催货单服务（feat/delivery-chase-closed-loop，2026-11-27）。

**业务闭环**：采购 Copilot · 到货跟踪 → 勾选未到货订单行 → 按供应商创建催货单 →
validate → submit-approval → approve → send（经 `ProcurementCommandPort`，outbox 留痕）。

**本期口径**（用户 2026-11-27 拍板）：
- 不做审批开关裁剪（链路保留，与询价同构）；
- 供应商答复回流不做：`SENT` 即本期终态；
- 自动升级（超期未答复再催）不做（二期）。

**核心不变式**：

1. **一个供应商一张催货单**：`from-pending` 把选中的行按 `supplierCode` 分组，
   每组生成一张草稿（同批次 `request_id` 关联，可一次追全部结果）。
2. **服务端回查行数据**：`from-pending` 只接受 `{poNo, poLine}` 身份对，
   数量/日期等**一律回查业务库**（只读 SQL，防篡改、防陈旧）——
   前端带来的数量一概不信任。
3. **幂等**：`idempotency_key = sha256(supplier + sorted(lines) + deadline + action)`
   ⇒ 同选行重复提交返回既有草稿（Copilot 重试安全），异 payload 409。
4. **供应商必须在 entity_mapping**：解析不到的行**剔除**并点名（不炸整批）；
   整批解析后没有可用行 ⇒ 422（不静默产出空催货单）。
5. **只读红线**：回查走 `pending_delivery` 同款只读 SQL 管线（本体绑定解析 +
   SQL Guard），催货单本身写**元数据库**，不写业务库。

失败语义与 RFQ 一致：任一环节失败 ⇒ 草稿置 FAILED/NEEDS_CORRECTION 并留痕，
**不写半份**。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import EntityMapping
from app.domain.procurement_chase_models import (
    ProcurementChaseDecision,
    ProcurementChaseDraft,
    ProcurementChaseLine,
)
from app.services.procurement_chase_state_machine import assertChaseTransition
from app.services.procurement_command_port import (
    ProcurementCommandPort,
    defaultCommandPort,
)
from app.services.procurement_query_utils import colValue

logger = logging.getLogger(__name__)

#: 空值计数等业务校验复用询价的行数上限（防一次性塞爆单张催货单）
MAX_CHASE_LINES = 200

#: 默认供应商答复期限（天）
DEFAULT_REPLY_DEADLINE_DAYS = 3


def computeChasePayloadHash(payload: dict[str, Any]) -> str:
    """对催货单 payload 做规范化 JSON 摘要（与 RFQ 同口径：键排序 + 紧凑分隔）。"""
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def chaseIdempotencyKey(
    *,
    supplierEnterpriseKey: int,
    poLines: list[tuple[str, str]],
    expectedReplyDeadline: datetime,
) -> str:
    """幂等键 = sha256(供应商 + 排序后的行身份对 + 期限)。

    同选行 + 同期限 ⇒ 同草稿（重试安全）；改选行或改期限 ⇒ 新草稿（409 由
    调用方在撞键时处理）。
    """
    lines = sorted(poLines)
    raw = (
        f"{supplierEnterpriseKey}|"
        f"{';'.join(f'{po}:{line}' for po, line in lines)}|"
        f"{expectedReplyDeadline.isoformat()}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _assertTransitionOrConflict(current: str, target: str) -> None:
    """状态迁移断言；非法 ⇒ ConflictError（409，与 RFQ 的对外语义一致）。"""
    try:
        assertChaseTransition(current, target)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc


def _asDate(value: Any) -> date | None:
    """EXPECTED_DATE 的列值可能是 str / date / datetime ⇒ 归一为 date（None 保持）。"""
    if value is None or isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


async def resolveSupplierEnterpriseKeys(
    session: AsyncSession, supplierCodes: list[str]
) -> dict[str, int]:
    """ERP 供应商码 → 统一 enterprise_key（批量查询，缺码不在结果里）。"""
    if not supplierCodes:
        return {}
    rows = (
        await session.execute(
            select(EntityMapping.enterprise_key, EntityMapping.enterprise_code).where(
                EntityMapping.entity_type == "SUPPLIER",
                EntityMapping.enterprise_code.in_(supplierCodes),
            )
        )
    ).all()
    return {str(code): int(key) for key, code in rows}


def groupLinesBySupplier(
    lines: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """按 supplierCode 分组（保序；None/空码的行剔除并交由调用方点名）。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        code = colValue(line, "SUPPLIER_CODE")
        if not code:
            continue
        grouped.setdefault(str(code), []).append(line)
    return grouped


class ProcurementChaseService:
    """催货单全生命周期。"""

    # ------------------------------------------------------------ 查询

    async def getDraft(
        self, session: AsyncSession, chaseDraftId: str
    ) -> dict[str, Any]:
        """详情：草稿 + 行 + 决策时间线（None ⇒ 404）。"""
        draft = (
            await session.execute(
                select(ProcurementChaseDraft).where(
                    ProcurementChaseDraft.chase_draft_id == chaseDraftId
                )
            )
        ).scalars().first()
        if draft is None:
            raise NotFoundError(f"催货单不存在: {chaseDraftId}")
        lines = (
            await session.execute(
                select(ProcurementChaseLine)
                .where(ProcurementChaseLine.chase_draft_id == draft.chase_draft_id)
                .order_by(ProcurementChaseLine.po_no, ProcurementChaseLine.po_line_no)
            )
        ).scalars().all()
        decisions = (
            await session.execute(
                select(ProcurementChaseDecision)
                .where(ProcurementChaseDecision.chase_draft_id == draft.chase_draft_id)
                .order_by(ProcurementChaseDecision.id)
            )
        ).scalars().all()
        return {"draft": draft, "lines": list(lines), "decisions": list(decisions)}

    async def listDrafts(
        self, session: AsyncSession, *, limit: int = 50
    ) -> list[ProcurementChaseDraft]:
        """最新草稿列表（按 id 倒序）。"""
        rows = (
            await session.execute(
                select(ProcurementChaseDraft)
                .order_by(ProcurementChaseDraft.id.desc())
                .limit(max(1, min(limit, 200)))
            )
        ).scalars().all()
        return list(rows)

    # ------------------------------------------------------------ 创建

    async def createDraftFromPending(
        self,
        session: AsyncSession,
        *,
        poLines: list[dict[str, Any]],
        expectedReplyDeadline: datetime,
        createdBy: str,
        notes: str | None = None,
        adapterProvider: Any = None,
        lineRowsByIdentity: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """**核心入口**：从 Copilot 选中的未到货行生成催货单（按供应商分组）。

        - 入参 `poLines` 只取 `{poNo, poLine}` 身份对；其余字段一律以
          `lineRowsByIdentity`（**服务端回查结果**）为准 —— 不在前端传数量。
        - 行按 supplierCode 分组 ⇒ 每组一张草稿；同一批共享 `request_id`。
        - 供应商解析不到 ⇒ 该组剔除并进响应 `unresolvedSuppliers`（不炸整批）。
        - 幂等：同 (supplier, lines, deadline) ⇒ 返回既有草稿（`reused=True`）。

        返回 `{"drafts": [...], "unresolvedSuppliers": [...], "skippedLines": [...]}`
        """
        if not poLines:
            raise ValidationError("未选择任何订单行")
        if len(poLines) > MAX_CHASE_LINES:
            raise ValidationError(f"一次最多催 {MAX_CHASE_LINES} 行（收到 {len(poLines)}）")
        if expectedReplyDeadline <= _now():
            raise ValidationError("供应商答复期限必须晚于当前时间")

        # 身份对 → 回查行（服务端数据，不信前端数量）
        identities: list[tuple[str, str]] = []
        for line in poLines:
            poNo = str(line.get("poNo") or "").strip()
            poLine = str(line.get("poLine") or "").strip()
            if poNo and poLine:
                identities.append((poNo, poLine))
        identities = sorted(set(identities))
        if not identities:
            raise ValidationError("选中行缺少 poNo / poLine 身份对")
        if lineRowsByIdentity is None:
            raise ValidationError("缺少服务端回查的行数据（lineRowsByIdentity 必填）")

        missing = [identity for identity in identities if identity not in lineRowsByIdentity]
        usableLines: list[dict[str, Any]] = []
        for identity in identities:
            row = lineRowsByIdentity.get(identity)
            if row is not None:
                usableLines.append(row)

        supplierCodes = sorted({
            str(colValue(row, "SUPPLIER_CODE"))
            for row in usableLines
            if colValue(row, "SUPPLIER_CODE")
        })
        keyByCode = await resolveSupplierEnterpriseKeys(session, supplierCodes)
        unresolved = [code for code in supplierCodes if code not in keyByCode]

        requestId = f"CHASE-{_now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
        drafts: list[dict[str, Any]] = []
        grouped = groupLinesBySupplier(usableLines)
        for supplierCode, group in sorted(grouped.items()):
            enterpriseKey = keyByCode.get(supplierCode)
            if enterpriseKey is None:
                continue
            draft, reused = await self._createOneDraft(
                session,
                supplierEnterpriseKey=enterpriseKey,
                supplierCode=supplierCode,
                lines=group,
                expectedReplyDeadline=expectedReplyDeadline,
                requestId=requestId,
                createdBy=createdBy,
                notes=notes,
            )
            drafts.append({"draft": draft, "reused": reused, "lineCount": len(group)})

        if not drafts:
            raise ValidationError(
                "选中的行无法生成催货单："
                f"供应商不在统一实体映射 {unresolved or '（无可用行）'}"
            )
        skipped = [
            {"poNo": po, "poLine": line}
            for po, line in missing
        ]
        return {
            "drafts": drafts,
            "unresolvedSuppliers": unresolved,
            "skippedLines": skipped,
            "requestId": requestId,
        }

    async def _createOneDraft(
        self,
        session: AsyncSession,
        *,
        supplierEnterpriseKey: int,
        supplierCode: str,
        lines: list[dict[str, Any]],
        expectedReplyDeadline: datetime,
        requestId: str,
        createdBy: str,
        notes: str | None,
    ) -> tuple[ProcurementChaseDraft, bool]:
        """单供应商单草稿（幂等 + payload 快照 + 行表写入）。"""
        identities = [
            (str(colValue(row, "PO_NO")), str(colValue(row, "PO_LINE_NO")))
            for row in lines
        ]
        idempotencyKey = chaseIdempotencyKey(
            supplierEnterpriseKey=supplierEnterpriseKey,
            poLines=identities,
            expectedReplyDeadline=expectedReplyDeadline,
        )
        payload = self._buildPayload(
            supplierCode=supplierCode,
            supplierEnterpriseKey=supplierEnterpriseKey,
            lines=lines,
            expectedReplyDeadline=expectedReplyDeadline,
            notes=notes,
        )
        payloadHash = computeChasePayloadHash(payload)
        existing = (
            await session.execute(
                select(ProcurementChaseDraft).where(
                    ProcurementChaseDraft.idempotency_key == idempotencyKey
                )
            )
        ).scalars().first()
        if existing is not None:
            if existing.payload_hash != payloadHash:
                raise ConflictError(
                    f"催货单幂等键冲突（同 key 异 payload）：{idempotencyKey[:16]}…"
                )
            return existing, True

        draft = ProcurementChaseDraft(
            chase_draft_id=uuid4(),
            version=1,
            status="DRAFT",
            request_id=requestId,
            supplier_enterprise_key=supplierEnterpriseKey,
            supplier_code=supplierCode,
            expected_reply_deadline=expectedReplyDeadline,
            notes=notes,
            payload_json=payload,
            payload_hash=payloadHash,
            idempotency_key=idempotencyKey,
            created_by=createdBy,
        )
        session.add(draft)
        await session.flush()
        for row in lines:
            session.add(
                ProcurementChaseLine(
                    chase_draft_id=draft.chase_draft_id,
                    po_no=str(colValue(row, "PO_NO")),
                    po_line_no=str(colValue(row, "PO_LINE_NO")),
                    material_code=(
                        str(colValue(row, "ITEM_CODE"))
                        if colValue(row, "ITEM_CODE") is not None
                        else None
                    ),
                    material_desc=(
                        str(colValue(row, "MATERIAL_DESC"))
                        if colValue(row, "MATERIAL_DESC") is not None
                        else None
                    ),
                    ordered_qty=colValue(row, "ORDERED_QTY"),
                    received_qty=colValue(row, "RECEIVED_QTY"),
                    open_qty=colValue(row, "OPEN_QTY"),
                    expected_date=_asDate(colValue(row, "EXPECTED_DATE")),
                    overdue_days=colValue(row, "OVERDUE_DAYS"),
                )
            )
        await session.flush()
        return draft, False

    @staticmethod
    def _buildPayload(
        *,
        supplierCode: str,
        supplierEnterpriseKey: int,
        lines: list[dict[str, Any]],
        expectedReplyDeadline: datetime,
        notes: str | None,
    ) -> dict[str, Any]:
        """外发报文快照（服务端数据 ⇒ 可复算）；外发端口消费这个结构。"""
        return {
            "supplierCode": supplierCode,
            "supplierEnterpriseKey": supplierEnterpriseKey,
            "expectedReplyDeadline": expectedReplyDeadline.isoformat(),
            "notes": notes,
            "lines": [
                {
                    "poNo": str(colValue(row, "PO_NO")),
                    "poLine": str(colValue(row, "PO_LINE_NO")),
                    "materialCode": (
                        str(colValue(row, "ITEM_CODE"))
                        if colValue(row, "ITEM_CODE") is not None
                        else None
                    ),
                    "materialDesc": (
                        str(colValue(row, "MATERIAL_DESC"))
                        if colValue(row, "MATERIAL_DESC") is not None
                        else None
                    ),
                    "orderedQty": (
                        float(colValue(row, "ORDERED_QTY"))
                        if colValue(row, "ORDERED_QTY") is not None
                        else None
                    ),
                    "receivedQty": (
                        float(colValue(row, "RECEIVED_QTY"))
                        if colValue(row, "RECEIVED_QTY") is not None
                        else None
                    ),
                    "openQty": (
                        float(colValue(row, "OPEN_QTY"))
                        if colValue(row, "OPEN_QTY") is not None
                        else None
                    ),
                    "expectedDate": (
                        str(colValue(row, "EXPECTED_DATE"))
                        if colValue(row, "EXPECTED_DATE") is not None
                        else None
                    ),
                    "overdueDays": colValue(row, "OVERDUE_DAYS"),
                }
                for row in sorted(
                    lines,
                    key=lambda row: (
                        str(colValue(row, "PO_NO")),
                        str(colValue(row, "PO_LINE_NO")),
                    ),
                )
            ],
        }

    # ------------------------------------------------------------ 生命周期

    async def _loadDraft(self, session: AsyncSession, chaseDraftId: str) -> ProcurementChaseDraft:
        draft = (
            await session.execute(
                select(ProcurementChaseDraft).where(
                    ProcurementChaseDraft.chase_draft_id == chaseDraftId
                )
            )
        ).scalars().first()
        if draft is None:
            raise NotFoundError(f"催货单不存在: {chaseDraftId}")
        return draft

    async def _decide(
        self,
        session: AsyncSession,
        *,
        chaseDraftId: str,
        target: str,
        decision: str,
        actor: str,
        note: str | None,
    ) -> ProcurementChaseDraft:
        """统一的状态推进 + 决策留痕（caller commit）。

        非法迁移 ⇒ `ConflictError`（409）—— 与 RFQ 同口径：状态机对外的错误
        形态是领域异常，不是裸 ValueError（500）。
        """
        draft = await self._loadDraft(session, chaseDraftId)
        _assertTransitionOrConflict(draft.status, target)
        draft.status = target
        draft.version += 1
        session.add(
            ProcurementChaseDecision(
                chase_draft_id=draft.chase_draft_id,
                decision=decision,
                actor=actor,
                note=note,
                decided_time=_now(),
            )
        )
        await session.flush()
        return draft

    async def validateDraft(
        self, session: AsyncSession, chaseDraftId: str, *, actor: str
    ) -> ProcurementChaseDraft:
        """DRAFT/NEEDS_CORRECTION → VALIDATING → 按行数/期限校验 → PENDING_APPROVAL 或 NEEDS_CORRECTION。"""
        draft = await self._loadDraft(session, chaseDraftId)
        _assertTransitionOrConflict(draft.status, "VALIDATING")
        draft.status = "VALIDATING"
        draft.version += 1
        await session.flush()
        problems = self._validatePayload(draft)
        target = "NEEDS_CORRECTION" if problems else "PENDING_APPROVAL"
        decision = "SUBMIT"
        draft.status = target
        draft.version += 1
        session.add(
            ProcurementChaseDecision(
                chase_draft_id=draft.chase_draft_id,
                decision=decision,
                actor=actor,
                note=("；".join(problems) if problems else None),
                decided_time=_now(),
            )
        )
        await session.flush()
        return draft

    @staticmethod
    def _validatePayload(draft: ProcurementChaseDraft) -> list[str]:
        payload = draft.payload_json or {}
        problems: list[str] = []
        if not payload.get("lines"):
            problems.append("lines 为空（催货至少一行）")
        if draft.expected_reply_deadline <= _now():
            problems.append("供应商答复期限已过期")
        return problems

    async def approveDraft(
        self, session: AsyncSession, chaseDraftId: str, *, actor: str, note: str | None = None
    ) -> ProcurementChaseDraft:
        """PENDING_APPROVAL → READY_TO_SEND。"""
        return await self._decide(
            session, chaseDraftId=chaseDraftId, target="READY_TO_SEND",
            decision="APPROVE", actor=actor, note=note,
        )

    async def rejectDraft(
        self, session: AsyncSession, chaseDraftId: str, *, actor: str, note: str | None = None
    ) -> ProcurementChaseDraft:
        """PENDING_APPROVAL → REJECTED（终态）。"""
        return await self._decide(
            session, chaseDraftId=chaseDraftId, target="REJECTED",
            decision="REJECT", actor=actor, note=note,
        )

    async def cancelDraft(
        self, session: AsyncSession, chaseDraftId: str, *, actor: str, note: str | None = None
    ) -> ProcurementChaseDraft:
        """活跃态 → CANCELLED（终态）。"""
        draft = await self._loadDraft(session, chaseDraftId)
        _assertTransitionOrConflict(draft.status, "CANCELLED")
        draft.status = "CANCELLED"
        draft.version += 1
        session.add(
            ProcurementChaseDecision(
                chase_draft_id=draft.chase_draft_id,
                decision="CANCEL",
                actor=actor,
                note=note,
                decided_time=_now(),
            )
        )
        await session.flush()
        return draft

    async def sendDraft(
        self,
        session: AsyncSession,
        chaseDraftId: str,
        *,
        actor: str,
        port: ProcurementCommandPort | None = None,
    ) -> ProcurementChaseDraft:
        """READY_TO_SEND → SENDING → 经命令端口外发 → SENT（本期终态）。

        外发引用写进决策留痕的 `note`（与 RFQ 同口径）；同幂等键重发 ⇒ 外部
        引用不变（端口保证）。
        """
        draft = await self._loadDraft(session, chaseDraftId)
        _assertTransitionOrConflict(draft.status, "SENDING")
        commandPort = port if port is not None else defaultCommandPort()
        draft.status = "SENDING"
        draft.version += 1
        await session.flush()
        result = await commandPort.sendChase(
            draft.payload_json or {}, idempotencyKey=draft.idempotency_key
        )
        target = "SENT" if result.status == "SENT" else "FAILED"
        _assertTransitionOrConflict(draft.status, target)
        draft.status = target
        draft.version += 1
        session.add(
            ProcurementChaseDecision(
                chase_draft_id=draft.chase_draft_id,
                decision="SEND",
                actor=actor,
                note=result.externalRef,
                decided_time=_now(),
            )
        )
        await session.flush()
        return draft


def defaultReplyDeadline(now: datetime | None = None) -> datetime:
    """默认供应商答复期限 = **今天 + 3 天，取当日 23:59:59（UTC）**。

    为什么不精确到当前时刻：`from-pending` 未显式传期限时，两次相邻请求的
    默认期限必须**相同** —— 幂等键含期限 ⇒ 毫秒级差异会让「同选行重复提交」
    变成两张草稿（幂等失效）。日期粒度 ⇒ 同一天内幂等，跨天自然新键。
    """
    base = (now or _now()) + timedelta(days=DEFAULT_REPLY_DEADLINE_DAYS)
    return datetime(
        base.year, base.month, base.day, 23, 59, 59, tzinfo=UTC
    )
