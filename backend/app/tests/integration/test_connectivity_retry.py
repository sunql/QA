"""连通性校验失败回灌重试（Task 1）。

背景（2026-10-03 真机）：「B019 圣特公司近 12 个月供货量下降的原因」多步第 2 步
（按收货地点拆分）报 `计划校验未通过：以下表无法通过关联路径连通：DIM_FACILITY`。
根因是 `_finalizePlan` 把 `validateConnectivity` 的失败**直接 raise**，
LLM 没有第二次机会 —— 属性归属校验（`validatePlan`）会回灌 `initialErrors`，
连通性校验却不会，同一道闸门两套语义。

本测试锁住修复后的行为：
- 连通性失败必须回灌到下一轮 prompt，模型换类后能通过
- 重试耗尽仍按既有契约抛 `Nl2SqlError`（不静默吞错）
- `supplementJoinPath` 幂等：`_planIssues` 与 `_finalizePlan` 各调一次安全
- `_finalizePlan` 的模块级连通性守卫**未被摘除**（被直接调用时仍生效）

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:pass@localhost:5434/qa_metadata_test \\
        uv run pytest app/tests/integration/test_connectivity_retry.py -v
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.exceptions import Nl2SqlError
from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.domain.query_plan import Aggregation, JoinSpec, PlanResult, QueryPlan
from app.infrastructure.llm.base_client import LlmMessage, LlmResponse
from app.services.nl2sql_service import Nl2SqlService

# 与 validateConnectivity 的报错文案绑定：改了文案这里要同步
_CONNECTIVITY_MARK = "无法通过关联路径连通"


class _FakeLlm:
    """按顺序弹出预置回复的假客户端，记录每次调用的消息。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        self.calls.append([(m.role, m.content) for m in messages])
        return LlmResponse(
            content=self._responses.pop(0),
            modelName=model or "test-model",
            promptTokens=10,
            completionTokens=5,
            totalTokens=15,
        )

    def userPrompts(self) -> list[str]:
        return ["\n".join(c for role, c in call if role == "user") for call in self.calls]


def _llmConfig() -> SimpleNamespace:
    return SimpleNamespace(model_name="test-model", temperature=0.0)


def _prop(name: str) -> OntologyProperty:
    return OntologyProperty(
        property_name=name,
        source_column=f"{name}_0",
        data_type="STRING",
        is_primary_key=False,
        is_foreign_key=False,
    )


def _cls(classId: int, name: str, props: list[str]) -> OntologyClass:
    return OntologyClass(
        id=classId,
        class_name=name,
        source_table=f"T_{name}",
        properties=[_prop(p) for p in props],
    )


def _join(sourceId: int, sourceCols: list[str], targetId: int, targetCols: list[str]) -> OntologyJoin:
    return OntologyJoin(
        source_class_id=sourceId,
        source_columns=sourceCols,
        target_class_id=targetId,
        target_columns=targetCols,
        join_key=f"{sourceId}|{','.join(sourceCols)}->{targetId}|{','.join(targetCols)}",
    )


def _islandClasses() -> list[OntologyClass]:
    """RECEIPT --SUPPLIER 有边；FACILITY 是孤岛（join 目录里零边）。"""
    return [
        _cls(1, "RECEIPT", ["QTY", "SUPPLIER_CODE", "SITE_CODE"]),
        _cls(2, "SUPPLIER", ["SUPPLIER_CODE", "SUPPLIER_NAME"]),
        _cls(3, "FACILITY", ["SITE_CODE", "SITE_NAME"]),
    ]


def _islandJoins() -> list[OntologyJoin]:
    """唯一的边：RECEIPT(1) ↔ SUPPLIER(2)。FACILITY 不与任何表相连。"""
    return [_join(1, ["SUPPLIER_CODE"], 2, ["SUPPLIER_CODE"])]


# 第 1 轮：选了孤岛类 FACILITY —— 属性归属全合法，只有连通性过不去。
_ISLAND_PLAN = QueryPlan(
    target="按收货地点拆分供货量",
    selectedClasses=("RECEIPT", "FACILITY"),
    selectedProperties=("QTY",),
    aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
    joins=(JoinSpec(sourceClass="RECEIPT", targetClass="FACILITY", columns=("SITE_CODE",)),),
)

# 第 2 轮：听反馈换掉孤岛类，改连有边的 SUPPLIER。
_HEALED_PLAN = QueryPlan(
    target="按供应商拆分供货量",
    selectedClasses=("RECEIPT", "SUPPLIER"),
    selectedProperties=("QTY", "SUPPLIER_CODE"),
    aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
    joins=(JoinSpec(sourceClass="RECEIPT", targetClass="SUPPLIER", columns=("SUPPLIER_CODE",)),),
)


def _json(plan: QueryPlan) -> str:
    return json.dumps(plan.to_dict(), ensure_ascii=False)


class TestConnectivityRetry:
    """连通性失败 → 回灌 initialErrors → LLM 换类 → 通过。"""

    async def test_connectivity_failure_self_heals(self) -> None:
        """孤岛类 → 第 1 轮连通性失败 → 第 2 轮换类 → 成功返回。

        修复前：`_finalizePlan` 直接 raise，只有 1 次 LLM 调用。
        修复后：2 次 LLM 调用，且第 2 次 prompt 带具体的连通性报错。
        """
        llm = _FakeLlm(responses=[_json(_ISLAND_PLAN), _json(_HEALED_PLAN)])

        result = await Nl2SqlService().generateValidatedPlan(
            "按收货地点拆分供货量",
            _islandClasses(),
            llm,
            _llmConfig(),
            datasourceType="POSTGRESQL",
            joins=_islandJoins(),
            maxPlanAttempts=2,
        )

        assert len(llm.calls) == 2, f"连通性失败应回灌重试一次，实际 LLM 调用 {len(llm.calls)} 次"

        retryPrompt = llm.userPrompts()[1]
        assert _CONNECTIVITY_MARK in retryPrompt, retryPrompt[-400:]
        # 必须点名**孤岛** T_FACILITY。旧实现拿 set 的任意元素当 BFS 起点，
        # 起点落在孤岛上时会反过来点名 T_RECEIPT —— 而这条消息是要回灌给 LLM
        # 让它自愈的，点错名字等于让它砍掉错误的类，把本可自愈的失败变成死局。
        assert "T_FACILITY" in retryPrompt, retryPrompt[-400:]
        assert "T_RECEIPT" not in retryPrompt, retryPrompt[-400:]

        assert result.plan.selectedClasses == ("RECEIPT", "SUPPLIER")

    async def test_connectivity_failure_exhausts_retries(self) -> None:
        """孤岛类不可替代 → 耗尽重试仍抛 Nl2SqlError，detail 含连通性报错。

        与既有契约一致：不静默吞错，错误文本要让运维看得出是连通性问题。
        """
        llm = _FakeLlm(responses=[_json(_ISLAND_PLAN), _json(_ISLAND_PLAN)])

        with pytest.raises(Nl2SqlError) as exc:
            await Nl2SqlService().generateValidatedPlan(
                "按收货地点拆分供货量",
                _islandClasses(),
                llm,
                _llmConfig(),
                datasourceType="POSTGRESQL",
                joins=_islandJoins(),
                maxPlanAttempts=2,
            )

        assert len(llm.calls) == 2, f"应尝试满 2 轮，实际 {len(llm.calls)} 次"
        assert _CONNECTIVITY_MARK in (exc.value.detail or "")


class TestSupplementIdempotency:
    """`_planIssues` 与 `_finalizePlan` 各调一次 supplementJoinPath 的安全性依据。"""

    def test_supplement_idempotent(self) -> None:
        """A→C 有中间路径 B：首次补 hop，第二次 changed=False 返回**同一对象**。

        返回同一对象（而非等价副本）是幂等的强证据 —— 双调用不会重复累加 JOIN。
        """
        from app.services.nl2sql_schema import supplementJoinPath

        classes = [
            _cls(1, "A", ["B_REF"]),
            _cls(2, "B", ["C_REF"]),
            _cls(3, "C", ["A_ID"]),
        ]
        joins = [
            _join(1, ["B_REF"], 2, ["B_ID"]),
            _join(2, ["C_REF"], 3, ["C_ID"]),
        ]
        plan = QueryPlan(
            target="A 与 C 关联",
            selectedClasses=("A", "C"),
            joins=(JoinSpec(sourceClass="A", targetClass="C", columns=()),),
        )

        first = supplementJoinPath(plan, classes, joins)
        assert first is not plan, "首调应真的补出中间 hop"
        assert len(first.joins) == 2, [j for j in first.joins]

        second = supplementJoinPath(first, classes, joins)
        assert second is first, "二调应走 changed=False 原样返回"


class TestFinalizeGuard:
    """`_finalizePlan` 的连通性守卫必须保留（模块级守卫，可被直接调用）。"""

    def test_finalize_still_guards(self) -> None:
        """直接调 `_finalizePlan` 传不连通 plan → 仍抛 Nl2SqlError。"""
        planResult = PlanResult(
            plan=_ISLAND_PLAN, promptTokens=10, completionTokens=5,
        )

        with pytest.raises(Nl2SqlError) as exc:
            Nl2SqlService()._finalizePlan(
                planResult, _islandClasses(), _islandJoins(), "按收货地点拆分供货量",
            )

        assert _CONNECTIVITY_MARK in (exc.value.detail or "")