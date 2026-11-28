"""copilot 场景配置服务单测（fake session，只测校验逻辑——不碰真实 Agent 表）。

Agent 存在性校验在集成测试覆盖；这里用假 session（链式 execute 返回固定行）
来测校验分支：未知 Agent / 未知渲染器 / 未知槽位类型 / 缺 key 或 label。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.exceptions import NotFoundError, ValidationError
from app.services.copilot_scenario_service import CopilotScenarioService

VALID_ARGS = {
    "scenarioCode": "pending_delivery",
    "name": "待到货 / 逾期未到",
    "description": "汇总欠交与逾期，给催货建议",
    "iconCode": "car",
    "agentCode": "PROCUREMENT_SOURCING",
    "slotSchema": [{"key": "monthsBack", "type": "number", "label": "时间窗"}],
    "questionTemplate": "分析待到货情况（时间窗=近{monthsBack}个月）。",
    "chips": [{"label": "全部供应商概览", "monthsBack": 12}],
    "resultRenderer": "pending_delivery",
    "requiredRole": None,
    "sortOrder": 10,
}


class _AgentRow:
    agent_code = "PROCUREMENT_SOURCING"


class _ChainStmt:
    """假 stmt：where/order_by/limit 链式返回自身；scalars().first() 返回固定行。"""

    def __init__(self, first: Any) -> None:
        self._first = first

    def where(self, *_a: Any, **_k: Any) -> _ChainStmt:
        return self

    def order_by(self, *_a: Any, **_k: Any) -> _ChainStmt:
        return self

    def limit(self, *_a: Any, **_k: Any) -> _ChainStmt:
        return self

    def scalars(self) -> Any:
        return SimpleNamespace(first=lambda: self._first)


class _FakeSession:
    """假 session：execute 总返回 _ChainStmt(first)；支持 add/flush。"""

    def __init__(self, first: Any) -> None:
        self._first = first
        self.added: list[Any] = []

    async def execute(self, _stmt: Any) -> _ChainStmt:
        return _ChainStmt(self._first)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _service(first: Any) -> tuple[CopilotScenarioService, _FakeSession]:
    session = _FakeSession(first)
    return CopilotScenarioService(session=session), session


class TestSaveValidation:
    @pytest.mark.asyncio
    async def test_unknown_agent_rejected(self, monkeypatch) -> None:
        async def _fakeById(_session, _dsId):
            return None

        monkeypatch.setattr(
            "app.services.agent_tools_procurement._loadActiveDatasource", _fakeById
        )
        svc, session = _service(None)
        with pytest.raises(NotFoundError, match="Agent 不存在"):
            await svc.save(**VALID_ARGS)

    @pytest.mark.asyncio
    async def test_unknown_renderer_rejected(self) -> None:
        svc, _session = _service(_AgentRow())
        with pytest.raises(ValidationError, match="result_renderer"):
            await svc.save(**{**VALID_ARGS, "resultRenderer": "magic"})

    @pytest.mark.asyncio
    async def test_unknown_slot_type_rejected(self) -> None:
        svc, _session = _service(_AgentRow())
        with pytest.raises(ValidationError, match="slot type"):
            await svc.save(
                **{
                    **VALID_ARGS,
                    "slotSchema": [{"key": "x", "type": "magic", "label": "X"}],
                }
            )

    @pytest.mark.asyncio
    async def test_slot_missing_key_or_label_rejected(self) -> None:
        svc, _session = _service(_AgentRow())
        with pytest.raises(ValidationError, match="缺少 key"):
            await svc.save(
                **{**VALID_ARGS, "slotSchema": [{"type": "number", "label": "X"}]}
            )
        with pytest.raises(ValidationError, match="缺少 label"):
            await svc.save(
                **{**VALID_ARGS, "slotSchema": [{"key": "x", "type": "number"}]}
            )

    @pytest.mark.asyncio
    async def test_invalid_code_pattern_rejected(self) -> None:
        svc, _session = _service(_AgentRow())
        with pytest.raises(ValidationError, match="codePattern 非法"):
            await svc.save(
                **{
                    **VALID_ARGS,
                    "slotSchema": [
                        {
                            "key": "supplierKeys",
                            "type": "entityMulti",
                            "label": "供应商",
                            "codePattern": "(?<![0-9A-Za-z)",
                        }
                    ],
                }
            )

    @pytest.mark.asyncio
    async def test_non_numeric_slot_range_rejected(self) -> None:
        """min/max 写成字符串/对象 ⇒ 保存时 422（不是运行期 500）。"""
        svc, _session = _service(_AgentRow())
        for bound in ("minimum", "maximum"):
            with pytest.raises(ValidationError, match="必须是数值"):
                await svc.save(
                    **{
                        **VALID_ARGS,
                        "slotSchema": [
                            {
                                "key": "periodIndex",
                                "type": "number",
                                "label": "周期期序",
                                bound: "0",
                            }
                        ],
                    }
                )

    @pytest.mark.asyncio
    async def test_numeric_slot_range_accepted(self) -> None:
        """合法数值范围照常保存（不抛 ValidationError）。"""
        svc, _session = _service(_AgentRow())
        await svc.save(
            **{
                **VALID_ARGS,
                "slotSchema": [
                    {
                        "key": "periodIndex",
                        "type": "number",
                        "label": "周期期序",
                        "minimum": -12,
                        "maximum": 0,
                    }
                ],
            }
        )
