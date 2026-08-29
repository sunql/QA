"""QueryPlan 生成（ReAct 推理阶段）单元测试。

generateQueryPlan 调 LLM 生成结构化 JSON 计划，解析为 QueryPlan。
覆盖：有效 JSON、fence 包裹、缺失字段容错、解析失败重试。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.domain.exceptions import Nl2SqlError
from app.domain.models import OntologyClass, OntologyProperty
from app.domain.query_plan import Aggregation, QueryPlan
from app.services.nl2sql_service import Nl2SqlService


def _cls(name: str) -> OntologyClass:
    return OntologyClass(class_name=name, source_table=f"T_{name}")


def _clsWithProps(name: str, props: list[str]) -> OntologyClass:
    """带属性的本体类，用于计划校验（props 为 property_name 列表）。"""
    properties = [OntologyProperty(property_name=p, source_column=p) for p in props]
    return OntologyClass(class_name=name, source_table=f"T_{name}", properties=properties)


def _llmConfig() -> SimpleNamespace:
    return SimpleNamespace(model_name="test-model")


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 10
        self.completionTokens = 5


class _FakeLlm:
    """按顺序弹出预置回复的假客户端。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append([(m.role, m.content) for m in messages])
        content = self._responses.pop(0)
        return _Resp(content)


def _validPlanJson() -> str:
    plan = QueryPlan(
        target="各供应商收货数量",
        selectedClasses=("PRECEIPT",),
        selectedProperties=("BPSNUM", "QTY"),
        aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
        groupBy=("BPSNUM",),
    )
    return json.dumps(plan.to_dict(), ensure_ascii=False)


class TestGenerateQueryPlan:
    async def test_injects_few_shot_into_plan_prompt(self) -> None:
        """1-2：few-shot 历史示例注入计划阶段 system prompt。"""
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateQueryPlan(
            "问题", [_cls("PRECEIPT")], fake, _llmConfig(),
            fewShot="示例 1：\n问题：上月销量\nSQL：\nSELECT 1 FROM T_PRECEIPT",
        )
        systemContent = fake.calls[0][0][1]
        assert "历史查询示例" in systemContent
        assert "SELECT 1 FROM T_PRECEIPT" in systemContent

    async def test_omits_few_shot_when_none(self) -> None:
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        assert "历史查询示例" not in fake.calls[0][0][1]

    async def test_plan_prompt_instructs_time_bucket_grouping(self) -> None:
        # 真实回归（2026-08-15）：时间粒度需求下 LLM 把「月份」写进 groupBy 而非日期属性，
        # 计划阶段 prompt 须明确：groupBy 填 DATE/DATETIME 属性名，粒度截断交给 SQL 生成阶段。
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        system = fake.calls[0][0][1]
        assert "时间粒度" in system
        assert "groupBy" in system
        assert "TO_CHAR" in system
        assert "严禁填" in system

    async def test_parses_valid_json_plan(self) -> None:
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        result = await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        assert result.plan.target == "各供应商收货数量"
        assert result.plan.selectedClasses == ("PRECEIPT",)
        assert result.plan.aggregations[0].function == "SUM"
        # system + user 两条消息
        assert len(fake.calls) == 1
        assert fake.calls[0][0][0] == "system"

    async def test_parses_plan_in_json_fence(self) -> None:
        fake = _FakeLlm(["```json\n" + _validPlanJson() + "\n```"])
        service = Nl2SqlService()
        result = await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        assert result.plan.target == "各供应商收货数量"

    async def test_missing_fields_use_defaults(self) -> None:
        fake = _FakeLlm([json.dumps({"target": "只有目标"}, ensure_ascii=False)])
        service = Nl2SqlService()
        result = await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        assert result.plan.selectedClasses == ()

    async def test_retries_on_parse_failure_then_succeeds(self) -> None:
        fake = _FakeLlm(["抱歉，无法解析", _validPlanJson()])
        service = Nl2SqlService()
        result = await service.generateQueryPlan(
            "问题", [_cls("PRECEIPT")], fake, _llmConfig(), maxRetries=1
        )
        assert result.plan.target == "各供应商收货数量"
        assert len(fake.calls) == 2

    async def test_exhausts_retries_raises(self) -> None:
        fake = _FakeLlm(["坏回复", "还是坏"])
        service = Nl2SqlService()
        with pytest.raises(Nl2SqlError):
            await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig(), maxRetries=1)

    async def test_accumulates_tokens_across_attempts(self) -> None:
        fake = _FakeLlm(["坏回复", _validPlanJson()])
        service = Nl2SqlService()
        result = await service.generateQueryPlan(
            "问题", [_cls("PRECEIPT")], fake, _llmConfig(), maxRetries=1
        )
        assert result.promptTokens == 20
        assert result.completionTokens == 10

    async def test_tolerates_extra_field_in_aggregation(self) -> None:
        raw = json.dumps(
            {
                "target": "各供应商收货数量",
                "selectedClasses": ["PRECEIPT"],
                "aggregations": [
                    {"function": "SUM", "property": "QTY", "alias": "TOTAL_QTY", "extra": "oops"}
                ],
            },
            ensure_ascii=False,
        )
        fake = _FakeLlm([raw])
        service = Nl2SqlService()
        result = await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        assert result.plan.aggregations[0].function == "SUM"
        assert result.plan.aggregations[0].property == "QTY"

    async def test_parses_plan_with_formula(self) -> None:
        raw = json.dumps(
            {
                "target": "各物料收货数量占比",
                "selectedClasses": ["PRECEIPT"],
                "selectedProperties": ["物料编号", "收货数量"],
                "aggregations": [
                    {
                        "function": "SUM",
                        "property": "收货数量",
                        "alias": "占比",
                        "formula": "SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
                    }
                ],
                "groupBy": ["物料编号"],
            },
            ensure_ascii=False,
        )
        fake = _FakeLlm([raw])
        service = Nl2SqlService()
        result = await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        agg = result.plan.aggregations[0]
        assert agg.formula == "SUM(收货数量) / SUM(SUM(收货数量)) OVER ()"
        assert agg.alias == "占比"

    async def test_plan_prompt_documents_formula_field(self) -> None:
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateQueryPlan("占比问题", [_cls("PRECEIPT")], fake, _llmConfig())
        systemContent = fake.calls[0][0][1]
        # JSON 示例含 formula 字段
        assert '"formula"' in systemContent
        # 规则说明 formula 用于派生指标
        assert "占比" in systemContent
        assert "OVER ()" in systemContent

    async def test_plan_prompt_marks_zhanshi_formula_mandatory(self) -> None:
        """2026-08-17 真实回归：占比/比率/百分比 formula 必填（占位符显式 + 规则 4 强引导）。

        之前示例用 '数量' 占位 → LLM 照抄 → formula 属性不在本体 → 校验拒绝 →
        重试耗尽 → Step 失败被静默收纳。修复后示例用显式占位 + 规则 4 标"必填"。
        """
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateQueryPlan("占比问题", [_cls("PRECEIPT")], fake, _llmConfig())
        system = fake.calls[0][0][1]
        # 示例占位须显式（不再是裸 '数量'）
        assert "<当前选中类的真实属性名" in system or "当前选中类" in system
        # 规则 4 必须强约束：占比/比率/百分比 出现 + 必填语气
        assert "占比" in system
        assert "必填" in system
        assert "窗口函数" in system


class TestGenerateValidatedPlan:
    """generateValidatedPlan：计划生成 + schema 校验 + 差异反馈重试。"""

    def _class(self) -> OntologyClass:
        return _clsWithProps("PRECEIPT", ["BPSNUM", "QTY"])

    async def test_threads_few_shot_through_common(self) -> None:
        """1-2：few-shot 经 common 参数透传到 generateQueryPlan 的 system prompt。"""
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateValidatedPlan(
            "问题", [self._class()], fake, _llmConfig(),
            fewShot="示例 1：\n问题：上月销量\nSQL：\nSELECT 1 FROM DUAL",
        )
        assert "历史查询示例" in fake.calls[0][0][1]

    async def test_threads_drift_warning_through_common(self) -> None:
        """2-4：driftWarning 经 common 参数透传到 generateQueryPlan 的 system prompt。"""
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        warning = "警告：以下本体表/字段在当前数据源中已不存在（与数据库 schema 不一致），请勿在 SQL 中引用：\n- 表 GONE_TBL"
        await service.generateValidatedPlan(
            "问题", [self._class()], fake, _llmConfig(), driftWarning=warning,
        )
        assert warning in fake.calls[0][0][1]

    async def test_returns_plan_when_validation_passes(self) -> None:
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        result = await service.generateValidatedPlan("问题", [self._class()], fake, _llmConfig())
        assert result.plan.selectedClasses == ("PRECEIPT",)
        assert len(fake.calls) == 1

    async def test_regenerates_with_feedback_when_validation_fails(self) -> None:
        bad = json.dumps({"target": "x", "selectedClasses": ["GHOST"]}, ensure_ascii=False)
        fake = _FakeLlm([bad, _validPlanJson()])
        service = Nl2SqlService()
        result = await service.generateValidatedPlan("问题", [self._class()], fake, _llmConfig())
        assert result.plan.selectedClasses == ("PRECEIPT",)
        assert len(fake.calls) == 2
        # 第二次调用的 user prompt 携带具体校验差异（"GHOST 不在本体"）
        secondUser = fake.calls[1][1][1]
        assert "GHOST" in secondUser

    async def test_raises_when_plan_never_validates(self) -> None:
        alwaysBad = json.dumps({"target": "x", "selectedClasses": ["GHOST"]}, ensure_ascii=False)
        fake = _FakeLlm([alwaysBad, alwaysBad])
        service = Nl2SqlService()
        with pytest.raises(Nl2SqlError):
            await service.generateValidatedPlan("问题", [self._class()], fake, _llmConfig())

    async def test_threads_dictionary_text_through_common(self) -> None:
        """B3：dictionaryText 经 common 参数透传到 generateQueryPlan 的 system prompt。"""
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateValidatedPlan(
            "问题", [self._class()], fake, _llmConfig(),
            dictionaryText="- 「占比」：某值占总量的比例",
        )
        assert "术语词典" in fake.calls[0][0][1]
        assert "占比" in fake.calls[0][0][1]

    # ---- 范围感知行数限制（scope-aware row limit）端到端 ----

    async def test_scoped_question_clears_row_limit(self) -> None:
        """14：假 LLM 返回 rowLimit=100 的计划 + 问题含 2025 年 → 计划 rowLimit 被清成 None。"""
        plan = QueryPlan(
            target="收货明细",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            rowLimit=100,
        )
        fake = _FakeLlm([json.dumps(plan.to_dict(), ensure_ascii=False)])
        service = Nl2SqlService()
        result = await service.generateValidatedPlan(
            "2025年的收货明细", [self._class()], fake, _llmConfig(),
        )
        assert result.plan.rowLimit is None

    async def test_unscoped_question_keeps_null_when_default_disabled(self) -> None:
        """15：默认 NL2SQL_NO_SCOPE_ROW_LIMIT=0 → 问题无范围词 + 计划 rowLimit=None → 保持 None。"""
        plan = QueryPlan(
            target="收货明细",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            rowLimit=None,
        )
        fake = _FakeLlm([json.dumps(plan.to_dict(), ensure_ascii=False)])
        service = Nl2SqlService()
        result = await service.generateValidatedPlan(
            "列出收货明细", [self._class()], fake, _llmConfig(),
        )
        assert result.plan.rowLimit is None

    async def test_plan_conditions_count_as_scope(self) -> None:
        """16：问题无时间词但计划带 conditions → 同样判为有范围，rowLimit 清成 None。"""
        plan = QueryPlan(
            target="查询",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            conditions=("供应商 = 'S1'",),
            rowLimit=100,
        )
        fake = _FakeLlm([json.dumps(plan.to_dict(), ensure_ascii=False)])
        service = Nl2SqlService()
        result = await service.generateValidatedPlan(
            "采购量", [self._class()], fake, _llmConfig(),
        )
        assert result.plan.rowLimit is None

    async def test_scope_question_unions_main_question(self) -> None:
        """17：多步场景下主问题的范围并入子问题：主问"2025 年..." + 子问"查各供应商..." → None。

        模拟 chat_service 把 dto.question 作为 scopeQuestion 透传，
        即使子问题文本不含时间词，整体 scopeText 也带主问题年份。
        """
        plan = QueryPlan(
            target="各供应商采购额",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("BPSNUM", "QTY"),
            aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
            groupBy=("BPSNUM",),
            rowLimit=100,
        )
        fake = _FakeLlm([json.dumps(plan.to_dict(), ensure_ascii=False)])
        service = Nl2SqlService()
        result = await service.generateValidatedPlan(
            "查各供应商采购额",  # 子问题（无时间词）
            [self._class()], fake, _llmConfig(),
            scopeQuestion="2025年的采购情况",  # 主问题（含年份）
        )
        assert result.plan.rowLimit is None

    async def test_zhanshi_alias_without_formula_self_heals_on_retry(self) -> None:
        """2026-08-17 真实回归端到端：alias='占比' 无 formula → 校验拒绝 → 重试带 hint
        → LLM 加 formula → 通过。

        修复前：占比 alias 无 formula 被静默放过 → SQL 不算百分比 → Step 失败被收纳。
        修复后：占比 alias 无 formula 立即报可操作 hint（窗口函数 + property 用真实属性），
        LLM 重试时正确加 formula，整步成功。
        """
        # 第 1 次：alias='占比' 无 formula → 校验拒绝
        badPlan = {
            "target": "4 月份主要 top10 采购物料的占比",
            "selectedClasses": ["PRECEIPT"],
            "selectedProperties": ["BPSNUM", "QTY"],
            "aggregations": [{"function": "SUM", "property": "QTY", "alias": "占比"}],
            "groupBy": ["BPSNUM"],
            "sortBy": [{"property": "占比", "direction": "desc"}],
        }
        # 第 2 次：alias='占比' + 正确 formula → 通过
        goodPlan = {
            "target": "4 月份主要 top10 采购物料的占比",
            "selectedClasses": ["PRECEIPT"],
            "selectedProperties": ["BPSNUM", "QTY"],
            "aggregations": [
                {
                    "function": "SUM",
                    "property": "QTY",
                    "alias": "占比",
                    "formula": "SUM(QTY) / SUM(SUM(QTY)) OVER ()",
                }
            ],
            "groupBy": ["BPSNUM"],
            "sortBy": [{"property": "占比", "direction": "desc"}],
        }
        fake = _FakeLlm(
            [json.dumps(badPlan, ensure_ascii=False),
             json.dumps(goodPlan, ensure_ascii=False)]
        )
        service = Nl2SqlService()
        result = await service.generateValidatedPlan(
            "4 月份主要 top10 采购物料的占比", [self._class()], fake, _llmConfig(),
        )
        # 重试了 1 次
        assert len(fake.calls) == 2
        # 最终计划带 formula
        assert result.plan.aggregations[0].formula == "SUM(QTY) / SUM(SUM(QTY)) OVER ()"
        # 第二次 user prompt 须携带"占比"+"formula"+"窗口函数"提示（引导 LLM 修）
        secondUser = fake.calls[1][1][1]
        assert "占比" in secondUser
        assert "formula" in secondUser or "窗口" in secondUser


class TestInterpretationAndDictionary:
    """interpretation 解析 + 计划 prompt 文档字段 + dictionaryText 注入。"""

    async def test_parses_plan_with_interpretation(self) -> None:
        raw = json.dumps(
            {"target": "占比", "interpretation": "用户想知道占比", "selectedClasses": ["PRECEIPT"]},
            ensure_ascii=False,
        )
        fake = _FakeLlm([raw])
        service = Nl2SqlService()
        result = await service.generateQueryPlan("占比问题", [_cls("PRECEIPT")], fake, _llmConfig())
        assert result.plan.interpretation == "用户想知道占比"

    async def test_plan_prompt_documents_interpretation_field(self) -> None:
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        systemContent = fake.calls[0][0][1]
        assert '"interpretation"' in systemContent

    async def test_injects_dictionary_text_into_plan_prompt(self) -> None:
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateQueryPlan(
            "问题", [_cls("PRECEIPT")], fake, _llmConfig(),
            dictionaryText="- 「占比」：某值占总量的比例",
        )
        systemContent = fake.calls[0][0][1]
        assert "术语词典" in systemContent
        assert "占比" in systemContent

    async def test_omits_dictionary_text_when_none(self) -> None:
        fake = _FakeLlm([_validPlanJson()])
        service = Nl2SqlService()
        await service.generateQueryPlan("问题", [_cls("PRECEIPT")], fake, _llmConfig())
        assert "术语词典" not in fake.calls[0][0][1]
