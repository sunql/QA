"""TurnResult 持久化（基于 ResearchReport.payload 子结构，不加新表）。

TurnResult 是 ResearchReport.payload 内的 turn_results list 子结构：
{
    "turn_id": str,
    "result_ref": str,           # 步骤结果引用
    "chart_config_ref": str,     # 图表配置引用
    "step_results": list[dict],  # 多步执行结果
    "evidence_refs": list[str],  # 假设验证 evidence 引用
    "schema_version": int,       # payload schema 版本
}

ReportRenderer 与历史回放按 result_ref 拉数据，图表/数据/步骤完整恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchReport


@dataclass
class TurnResult:
    """ResearchReport.payload.turn_results 的一条记录。"""
    turn_id: str
    result_ref: str
    chart_config_ref: str | None = None
    step_results: list[dict] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "TurnResult":
        return cls(
            turn_id=d["turn_id"],
            result_ref=d["result_ref"],
            chart_config_ref=d.get("chart_config_ref"),
            step_results=d.get("step_results", []),
            evidence_refs=d.get("evidence_refs", []),
            schema_version=d.get("schema_version", 1),
        )

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "result_ref": self.result_ref,
            "chart_config_ref": self.chart_config_ref,
            "step_results": self.step_results,
            "evidence_refs": self.evidence_refs,
            "schema_version": self.schema_version,
        }


class TurnResultRepository:
    @staticmethod
    async def upsert(
        session: AsyncSession,
        report_id: UUID,
        turn_result: TurnResult,
    ) -> None:
        """把 turn_result 合并到 ResearchReport.payload.turn_results。"""
        stmt = select(ResearchReport).where(ResearchReport.id == report_id)
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()
        if report is None:
            return

        payload = dict(report.payload)
        turn_results: list[dict] = list(payload.get("turn_results", []))
        # 替换或追加
        for i, existing in enumerate(turn_results):
            if existing["turn_id"] == turn_result.turn_id:
                turn_results[i] = turn_result.to_dict()
                break
        else:
            turn_results.append(turn_result.to_dict())

        payload["turn_results"] = turn_results
        report.payload = payload
        await session.flush()

    @staticmethod
    async def get_by_turn_id(
        session: AsyncSession,
        report_id: UUID,
        turn_id: str,
    ) -> TurnResult | None:
        """从 ResearchReport.payload.turn_results 取单条。"""
        stmt = select(ResearchReport).where(ResearchReport.id == report_id)
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()
        if report is None:
            return None

        turn_results: list[dict] = report.payload.get("turn_results", [])
        for d in turn_results:
            if d["turn_id"] == turn_id:
                return TurnResult.from_dict(d)
        return None

    @staticmethod
    async def get_all(session: AsyncSession, report_id: UUID) -> list[TurnResult]:
        """取所有 turn_result，按 turn_id 升序。"""
        stmt = select(ResearchReport).where(ResearchReport.id == report_id)
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()
        if report is None:
            return []

        turn_results: list[dict] = report.payload.get("turn_results", [])
        return sorted(
            [TurnResult.from_dict(d) for d in turn_results],
            key=lambda t: t.turn_id,
        )
