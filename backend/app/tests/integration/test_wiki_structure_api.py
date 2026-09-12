"""机制 5 渐进结构与规则 dry-run 的集成测试（feat-wiki-knowledge M6）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。规则求值器是
**纯函数**，故那部分直接调用（无 DB 参与，不属于「绕过 API 直连 service」）；
凡落到 DB 的断言一律走 HTTP。

覆盖：
- 阶段**派生**而非递增：产物在则 FULLY，产物删掉就掉回去
- 只有 ACCEPTED 建议算数（PENDING / REJECTED 不算）
- 接受 RULE → 物化 wiki_rule_executable；PROCESS → 物化 process_workflow
- METRIC / CONCEPT 不物化（公式与定义不是可执行判决）
- 重复物化 UPSERT 覆盖而不是撞唯一键报错
- 求值器：**字段缺失判不出 ≠ 条件不成立**（本机制最重要的一条）
- 算子归一化 + 白名单（未知算子报错，不能静默判否）
- dry-run 三类结果分计（PASSED / FAILED / UNDECIDABLE）
- 未认证 403 / 不存在 404 / 删条目级联清产物
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.domain.models import LlmConfig
from app.domain.wiki_learning_models import (
    ProcessWorkflow,
    StructureSuggestion,
    WikiRuleExecutable,
)
from app.domain.wiki_models import KnowledgeClaim
from app.infrastructure.llm.base_client import LlmResponse

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_SUGGESTIONS = "/api/v1/wiki/suggestions"
_RELATIONS = "/api/v1/wiki/relations"

_INVOKER_CLIENT = "app.services.learning.llm_invoker.createClient"

_RULE_CONTENT = "供应商准入要求：注册资本 ≥ 1000 万元，否则不得准入。"
_PROCESS_CONTENT = (
    "供应商准入流程：\n1. 提交资料\n2. 资质审查\n3. 现场考察\n4. 审批发布"
)
_METRIC_CONTENT = "准时交付率 = 准时交付单数 / 总交付单数"

# 与 prompt 约定的形状一致（action 里的 target_entity 是可选键，M6 才消费它）
_RULE_STRUCTURE = {
    "conditions": [
        {"field": "注册资本", "operator": ">=", "value": "1000", "unit": "万元"}
    ],
    "action": {
        "type": "BLOCK",
        "target_entity": "SUPPLIER",
        "description": "不得准入",
    },
}
_PROCESS_STRUCTURE = {
    "steps": [
        {"seq": 1, "name": "提交资料", "actor_role": "供应商", "action": "填表"},
        {"seq": 2, "name": "资质审查", "actor_role": "采购员", "action": "核验"},
        {"seq": 3, "name": "现场考察", "actor_role": "质量工程师", "action": "走访"},
        {"seq": 4, "name": "审批发布", "actor_role": "采购经理", "action": "签批"},
    ],
    "trigger": "新供应商引入",
}
_METRIC_STRUCTURE = {
    "formula": "准时交付单数 / 总交付单数",
    "numerator": "准时交付单数",
    "denominator": "总交付单数",
    "unit": "%",
}


class _FakeLlmClient:
    """假 LLM 客户端（与 M4/M5 测试同形）。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def complete(self, messages, **kwargs) -> LlmResponse:
        self.calls += 1
        return LlmResponse(
            content=self._content,
            modelName="fake-model",
            promptTokens=100,
            completionTokens=50,
            totalTokens=150,
        )


async def _seedModel(
    dbSession: AsyncSession, *, modelName: str = "wiki-structure-model"
) -> int:
    config = LlmConfig(
        model_name=modelName,
        provider="openai_compatible_proxy",
        is_active=True,
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config.id


async def _createPage(
    client: AsyncClient, *, title: str, content: str = "正文"
) -> str:
    resp = await client.post(_PAGES, json={"title": title, "content": content})
    assert resp.status_code == 201, resp.text
    return resp.json()["pageId"]


async def _acceptSuggestionFor(
    client: AsyncClient,
    dbSession: AsyncSession,
    *,
    content: str,
    structure: dict,
    title: str = "一条待结构化的知识",
) -> tuple[str, int]:
    """走完整链路造一条已接受的结构化建议：建页 → 机制 4 → 接受。

    返回 ``(pageId, suggestionId)``。
    """
    modelId = await _seedModel(dbSession)
    pageId = await _createPage(client, title=title, content=content)
    fake = _FakeLlmClient(json.dumps(structure, ensure_ascii=False))
    with patch(_INVOKER_CLIENT, return_value=fake):
        resp = await client.post(
            f"{_PAGES}/{pageId}/suggestions", json={"modelId": modelId}
        )
    assert resp.status_code == 200, resp.text
    suggestionId = resp.json()["suggestions"][0]["id"]

    accepted = await client.post(f"{_SUGGESTIONS}/{suggestionId}/accept")
    assert accepted.status_code == 200, accepted.text
    return pageId, suggestionId


async def _stage(client: AsyncClient, pageId: str) -> str:
    resp = await client.get(f"{_PAGES}/{pageId}")
    assert resp.status_code == 200, resp.text
    return resp.json()["structureStage"]


async def _rules(dbSession: AsyncSession) -> list[WikiRuleExecutable]:
    result = await dbSession.execute(
        select(WikiRuleExecutable).order_by(WikiRuleExecutable.id)
    )
    return list(result.scalars().all())


async def _workflows(dbSession: AsyncSession) -> list[ProcessWorkflow]:
    result = await dbSession.execute(
        select(ProcessWorkflow).order_by(ProcessWorkflow.id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 规则求值器（纯函数）
# ---------------------------------------------------------------------------


def _twoConditionRule() -> dict:
    return {
        "conditions": [
            {"field": "注册资本", "operator": ">=", "value": "1000", "unit": "万元"},
            {
                "field": "行业",
                "operator": "IN",
                "value": ["制造业", "批发业"],
                "unit": None,
            },
        ],
        "action": {"type": "BLOCK", "description": "不得准入"},
    }


def _evaluate(record: dict) -> object:
    from app.services.learning.wiki_rule_engine import evaluateRule

    return evaluateRule(_twoConditionRule(), record)


def test_all_conditions_matched() -> None:
    """全部条件成立 → 命中。"""
    evaluation = _evaluate({"注册资本": 1500, "行业": "制造业"})

    assert evaluation.matched is True
    assert evaluation.undecidable is False
    assert [c.matched for c in evaluation.conditions] == [True, True]


def test_single_failing_condition_blocks_match() -> None:
    """第二条不成立就必须整体不命中（防止「只有第一条说了算」）。"""
    evaluation = _evaluate({"注册资本": 1500, "行业": "餐饮业"})

    assert evaluation.matched is False
    assert evaluation.undecidable is False
    assert [c.matched for c in evaluation.conditions] == [True, False]


def test_missing_field_is_undecidable_not_unmatched() -> None:
    """**本机制最重要的一条**：记录里缺字段 → 判不了，不是「不成立」。

    把「缺字段」当成「条件不成立」，规则在生产里就会对每一条缺字段的记录静默
    放行（或静默拦截），而 dry-run 全绿看不出任何异常 —— 这是最难查的一类规则
    事故。故缺失单列一态 ``matched=None`` + ``undecidable=True``，让调用方
    必须显式回答「这条记录我判不了」，而不是收下一个含义模糊的 False。
    """
    evaluation = _evaluate({"行业": "制造业"})  # 缺 注册资本

    assert evaluation.matched is False
    assert evaluation.undecidable is True
    first = evaluation.conditions[0]
    assert first.matched is None
    assert first.field == "注册资本"
    assert first.undecidableReason is not None
    # 缺字段的那个条件不该被记成「不成立」
    assert evaluation.conditions[1].matched is True


def test_non_numeric_value_in_numeric_comparison_is_undecidable() -> None:
    """数值算子遇上不可转数的值 → 判不了，而不是按字符串比出个大小来。"""
    evaluation = _evaluate({"注册资本": "很多", "行业": "制造业"})

    assert evaluation.undecidable is True
    assert evaluation.conditions[0].matched is None
    assert evaluation.conditions[0].undecidableReason is not None


def test_string_equality_is_decidable() -> None:
    """``=`` / ``!=`` 对字符串是能判的 —— 别把「非数值」一律当判不了。"""
    from app.services.learning.wiki_rule_engine import evaluateRule

    rule = {
        "conditions": [{"field": "资质等级", "operator": "=", "value": "A"}],
        "action": {},
    }

    assert evaluateRule(rule, {"资质等级": "A"}).matched is True
    assert evaluateRule(rule, {"资质等级": "B"}).matched is False


def test_operator_alias_is_normalized() -> None:
    """中文算子归一化。

    M5 只把归一化写进了 prompt，**代码里并未强制**（``_validateRule`` 仅 strip）
    —— 模型不听话时库里落的就是「不低于」。故 M6 求值前必须自己归一，否则一条
    完全正常的规则会因为算子写法不同而被判成未知算子。
    """
    from app.services.learning.wiki_rule_engine import evaluateRule

    rule = {
        "conditions": [{"field": "注册资本", "operator": "不低于", "value": "1000"}],
        "action": {},
    }

    assert evaluateRule(rule, {"注册资本": 1200}).matched is True
    assert evaluateRule(rule, {"注册资本": 900}).matched is False


def test_unknown_operator_raises_instead_of_silently_denying() -> None:
    """未知算子必须报错。

    若求值器把不认识的算子当成「不成立」，一条写错算子的规则会在生产里静默
    永不触发，而所有 dry-run 样例都会被判成「期望不命中」而全绿。
    """
    from app.services.learning.wiki_rule_engine import evaluateRule

    rule = {
        "conditions": [{"field": "注册资本", "operator": "约等于", "value": "1000"}],
        "action": {},
    }

    with pytest.raises(ValidationError):
        evaluateRule(rule, {"注册资本": 1000})


def test_not_in_operator() -> None:
    """``NOT IN`` 与 ``IN`` 是同一集合运算的两面。"""
    from app.services.learning.wiki_rule_engine import evaluateRule

    rule = {
        "conditions": [{"field": "状态", "operator": "NOT IN", "value": ["停用"]}],
        "action": {},
    }

    assert evaluateRule(rule, {"状态": "启用"}).matched is True
    assert evaluateRule(rule, {"状态": "停用"}).matched is False


def test_in_with_scalar_rule_value_is_undecidable() -> None:
    """``IN`` 的取值一侧不是数组 → 规则本身写坏了，判不了而不是判否。"""
    from app.services.learning.wiki_rule_engine import evaluateRule

    rule = {
        "conditions": [{"field": "行业", "operator": "IN", "value": "制造业"}],
        "action": {},
    }

    evaluation = evaluateRule(rule, {"行业": "制造业"})

    assert evaluation.conditions[0].matched is None
    assert evaluation.undecidable is True


@pytest.mark.parametrize(
    "badExpression",
    [
        {"conditions": [], "action": {}},
        {"action": {}},
        {"conditions": "注册资本 >= 1000", "action": {}},
    ],
    ids=["empty-list", "missing-key", "not-a-list"],
)
def test_rule_without_conditions_is_rejected(badExpression: dict) -> None:
    """无条件的规则必须拒绝求值。

    ``all([])`` 是 ``True`` —— 一条 ``conditions`` 为空的规则会命中**全部**记录，
    落在 BLOCK 动作上就是「拦下一切」。这种形状多半来自抽取失败，绝不能被当成
    「无条件成立」放行。
    """
    from app.services.learning.wiki_rule_engine import evaluateRule

    with pytest.raises(ValidationError):
        evaluateRule(badExpression, {"注册资本": 1500})


def test_dry_run_reports_passed_case() -> None:
    """dry-run：实际与期望一致 → PASSED。"""
    from app.services.learning.wiki_rule_engine import dryRun

    report = dryRun(
        _twoConditionRule(),
        [
            {
                "input": {"注册资本": 1500, "行业": "制造业"},
                "expectedOutput": {"matched": True},
            }
        ],
    )

    assert report.total == 1
    assert report.passed == 1
    assert report.failed == 0
    assert report.undecidable == 0
    assert report.results[0].status == "PASSED"
    assert report.results[0].actualMatched is True


def test_dry_run_reports_failed_case_with_diverging_condition() -> None:
    """dry-run：实际与期望不符 → FAILED，并指出是哪一条条件岔开的。"""
    from app.services.learning.wiki_rule_engine import dryRun

    report = dryRun(
        _twoConditionRule(),
        [
            {
                "input": {"注册资本": 1500, "行业": "餐饮业"},
                "expectedOutput": {"matched": True},
            }
        ],
    )

    assert report.failed == 1
    assert report.passed == 0
    assert report.results[0].status == "FAILED"
    assert report.results[0].actualMatched is False
    assert report.results[0].expectedMatched is True


def test_dry_run_counts_undecidable_separately_from_failed() -> None:
    """判不了的样例单计一档，**不算失败**。

    混进 FAILED 会让「样例没给全字段」看起来像「规则错了」，把审核人推去改一条
    本来正确的规则 —— 而真正该做的是把样例补全。
    """
    from app.services.learning.wiki_rule_engine import dryRun

    report = dryRun(
        _twoConditionRule(),
        [{"input": {"行业": "制造业"}, "expectedOutput": {"matched": False}}],
    )

    assert report.undecidable == 1
    assert report.failed == 0
    assert report.passed == 0
    assert report.results[0].status == "UNDECIDABLE"


def test_dry_run_with_no_examples_is_not_an_error() -> None:
    """没给样例 = 没跑，不是错误（``total=0`` 的合法报告）。"""
    from app.services.learning.wiki_rule_engine import dryRun

    report = dryRun(_twoConditionRule(), [])

    assert report.total == 0
    assert report.results == ()


@pytest.mark.parametrize(
    "badExample",
    [
        {"input": {"注册资本": 1500}},                              # 缺 expectedOutput
        {"input": {"注册资本": 1500}, "expectedOutput": {}},        # expectedOutput 缺 matched
        {"input": "注册资本=1500", "expectedOutput": {"matched": True}},  # input 不是对象
        {"input": {"注册资本": 1500}, "expectedOutput": {"matched": "yes"}},  # matched 不是布尔
    ],
    ids=[
        "missing-expected-output",
        "missing-matched-key",
        "input-not-an-object",
        "matched-not-a-boolean",
    ],
)
def test_dry_run_rejects_malformed_example(badExample: dict) -> None:
    """样例形状不对 → 422，而不是当成「期望不命中」跑出一个绿报告。"""
    from app.services.learning.wiki_rule_engine import dryRun

    with pytest.raises(ValidationError):
        dryRun(_twoConditionRule(), [badExample])


def test_dry_run_example_cannot_smuggle_extra_shape() -> None:
    """样例只认 input / expectedOutput 两个键，多给的键被忽略而非透传。

    透传会让调用方以为「我传的 extra 生效了」，而它其实从没参与判定。
    """
    from app.services.learning.wiki_rule_engine import dryRun

    report = dryRun(
        _twoConditionRule(),
        [
            {
                "input": {"注册资本": 1500, "行业": "制造业"},
                "expectedOutput": {"matched": True, "action": {"type": "ALLOW"}},
            }
        ],
    )

    assert report.passed == 1


# ---------------------------------------------------------------------------
# 阶段派生
# ---------------------------------------------------------------------------


async def test_new_page_is_markdown(client: AsyncClient) -> None:
    """新建条目停在 MARKDOWN。"""
    pageId = await _createPage(client, title="一条纯正文")

    assert await _stage(client, pageId) == "MARKDOWN"


async def test_claim_moves_page_to_semi_structured(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """有事实原子 → SEMI_STRUCTURED。

    claim 目前没有写入接口（由导入链路的抽取产出），故此处直接种库 —— 断言的是
    「阶段推导读到了 claim 表」，不是 claim 的写入路径。
    """
    pageId = await _createPage(client, title="有条目的知识")
    dbSession.add(
        KnowledgeClaim(page_id=pageId, claim_text="注册资本须不低于 1000 万元")
    )
    await dbSession.commit()

    resp = await client.post(f"{_PAGES}/{pageId}/structure/recompute")

    assert resp.status_code == 200, resp.text
    assert resp.json()["current"] == "SEMI_STRUCTURED"
    assert resp.json()["previous"] == "MARKDOWN"
    assert resp.json()["changed"] is True


async def test_pending_suggestion_does_not_move_stage(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """**待处置**的建议不算数：还没人确认过它，不能算结构已升级。

    必须先显式重算再断言。生成建议**不会**触发阶段同步（那是「有人处置了」
    才发生的事），所以不重算的话阶段恒是初始的 MARKDOWN，断言无论如何都成立 ——
    测的是「没算」，而不是「算出来是 MARKDOWN」。
    """
    modelId = await _seedModel(dbSession)
    pageId = await _createPage(client, title="有建议未处置", content=_RULE_CONTENT)
    fake = _FakeLlmClient(json.dumps(_RULE_STRUCTURE, ensure_ascii=False))
    with patch(_INVOKER_CLIENT, return_value=fake):
        resp = await client.post(
            f"{_PAGES}/{pageId}/suggestions", json={"modelId": modelId}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1

    recomputed = await client.post(f"{_PAGES}/{pageId}/structure/recompute")
    assert recomputed.status_code == 200, recomputed.text
    assert recomputed.json()["current"] == "MARKDOWN"


async def test_rejected_suggestion_does_not_move_stage(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """被打回的建议同样不算数（REJECTED 是终态，不是「待办」）。"""
    modelId = await _seedModel(dbSession)
    pageId = await _createPage(client, title="建议被打回", content=_RULE_CONTENT)
    fake = _FakeLlmClient(json.dumps(_RULE_STRUCTURE, ensure_ascii=False))
    with patch(_INVOKER_CLIENT, return_value=fake):
        resp = await client.post(
            f"{_PAGES}/{pageId}/suggestions", json={"modelId": modelId}
        )
    suggestionId = resp.json()["suggestions"][0]["id"]

    rejected = await client.post(f"{_SUGGESTIONS}/{suggestionId}/reject")
    assert rejected.status_code == 200, rejected.text

    # 同 pending 那条：拒绝**不会**触发阶段同步，必须显式重算才测得到「算出来是
    # MARKDOWN」而不是「从没算过」。
    recomputed = await client.post(f"{_PAGES}/{pageId}/structure/recompute")
    assert recomputed.json()["current"] == "MARKDOWN"


async def test_accepted_metric_suggestion_reaches_semi_structured_only(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """METRIC 建议被接受 → 只到 SEMI_STRUCTURED，**不产物**。

    公式和定义不是可执行的准入判决；把它们塞进 ``wiki_rule_executable`` 会让
    Agent 对着一条没有条件的「规则」没法执行，也会污染规则的准确率统计。
    """
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_METRIC_CONTENT, structure=_METRIC_STRUCTURE
    )

    assert await _stage(client, pageId) == "SEMI_STRUCTURED"
    assert await _rules(dbSession) == []


async def test_accepted_rule_suggestion_materializes_rule(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """接受 RULE 建议 → 物化出一条可执行规则 + 升到 FULLY_STRUCTURED。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )

    rules = await _rules(dbSession)
    assert len(rules) == 1
    assert rules[0].page_id == pageId
    assert rules[0].rule_kind == "THRESHOLD"
    assert rules[0].rule_expression["conditions"][0]["field"] == "注册资本"
    assert await _stage(client, pageId) == "FULLY_STRUCTURED"


async def test_rule_materialization_carries_target_entity(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """规则的目标实体从 ``action.target_entity`` 带过来（Agent 侧据此筛规则）。"""
    await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )

    rules = await _rules(dbSession)
    assert rules[0].target_entity == "SUPPLIER"


async def test_rule_without_action_target_entity_has_none(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """没给目标实体就留空 —— 不猜。猜错会让 Agent 把规则套到错的实体上。"""
    structure = {
        "conditions": _RULE_STRUCTURE["conditions"],
        "action": {"type": "BLOCK", "description": "不得准入"},
    }
    await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=structure
    )

    rules = await _rules(dbSession)
    assert rules[0].target_entity is None


async def test_rule_materialization_carries_page_authority(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """权威链取自条目自身的 ``authority_level``（产物不比来源更权威）。

    条目**先定级再物化**，否则断言到的永远是「没定级」那一路，等于没测。
    """
    modelId = await _seedModel(dbSession)
    pageId = await _createPage(client, title="已定级的规则", content=_RULE_CONTENT)
    patched = await client.patch(f"{_PAGES}/{pageId}", json={"authorityLevel": "L4"})
    assert patched.status_code == 200, patched.text

    fake = _FakeLlmClient(json.dumps(_RULE_STRUCTURE, ensure_ascii=False))
    with patch(_INVOKER_CLIENT, return_value=fake):
        generated = await client.post(
            f"{_PAGES}/{pageId}/suggestions", json={"modelId": modelId}
        )
    suggestionId = generated.json()["suggestions"][0]["id"]
    await client.post(f"{_SUGGESTIONS}/{suggestionId}/accept")

    rules = await _rules(dbSession)
    assert rules[0].authority_chain == ["L4"]


async def test_materialization_normalizes_operator_before_persisting(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """中文算子归一化**后**才落库。

    M5 只把归一化写进了 prompt，代码并未强制，所以库里完全可能落到「不低于」。
    求值器的别名表是给这类旧行的兼容层；新写入的行必须是规范形态，否则每个消费方
    （Agent 工具、规则工作台）都得各自再实现一份别名表 —— 漏掉的那个不会报错。

    断言「落库后的算子」而不是「求值能过」：后者靠别名表也能过，绑不住归一化。
    """
    structure = {
        "conditions": [
            {"field": "注册资本", "operator": "不低于", "value": "1000", "unit": "万元"}
        ],
        "action": {"type": "BLOCK"},
    }
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=structure
    )

    rules = await _rules(dbSession)
    assert rules[0].rule_expression["conditions"][0]["operator"] == ">="

    resp = await client.post(
        f"{_PAGES}/{pageId}/rules/dry-run",
        json={
            "examples": [
                {"input": {"注册资本": 1500}, "expectedOutput": {"matched": True}}
            ]
        },
    )
    assert resp.json()["summary"]["passed"] == 1


async def test_materialization_rejects_unknown_operator(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """认不出的算子 → 422，且**建议不被盖成终态**。

    一条算子写错的规则进库就是一条永不触发的规则，事后无从发现（dry-run 会
    把它判成「期望不命中」而全绿）。故宁可在审核这一步拒掉。

    更关键的是后半句：物化失败必须让整个接受动作一起回滚。建议是终态不可逆的，
    若状态落了、产物没落，就再也没有入口能补上这个产物。
    """
    modelId = await _seedModel(dbSession)
    pageId = await _createPage(client, title="算子写错的规则", content=_RULE_CONTENT)
    structure = {
        "conditions": [{"field": "注册资本", "operator": "约等于", "value": "1000"}],
        "action": {"type": "BLOCK"},
    }
    fake = _FakeLlmClient(json.dumps(structure, ensure_ascii=False))
    with patch(_INVOKER_CLIENT, return_value=fake):
        generated = await client.post(
            f"{_PAGES}/{pageId}/suggestions", json={"modelId": modelId}
        )
    suggestionId = generated.json()["suggestions"][0]["id"]

    accepted = await client.post(f"{_SUGGESTIONS}/{suggestionId}/accept")

    assert accepted.status_code == 422
    assert await _rules(dbSession) == []
    dbSession.expire_all()
    listed = await client.get(f"{_PAGES}/{pageId}/suggestions")
    assert listed.json()["rows"][0]["status"] == "PENDING"


async def test_membership_rule_survives_extraction_and_can_match(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """``IN`` 的取值是**数组**，抽取阶段不能把它压成标量。

    M5 的 ``_stringOrNone`` 只放行字符串与数值，数组会被归一化成 ``None``；而求值器
    要求 ``IN`` / ``NOT IN`` 的取值必须是数组。两者相撞产出的是一条**永远判不了**的
    规则：它通过了物化的全部校验（算子合法、条件非空）安然进库，dry-run 却把每条
    样例都报成 UNDECIDABLE。

    断言两件事，缺一不可：
    1. 落库的取值仍是数组（抽取阶段没有丢形状）
    2. 这条规则**真的能判命中**（端到端跑 dry-run，两个样例都 PASSED 且无一 UNDECIDABLE）
    —— 只断言第 1 条会被「存了数组但求值仍判不了」的实现骗过。
    """
    structure = {
        "conditions": [
            {"field": "行业", "operator": "IN", "value": ["制造业", "批发业"]}
        ],
        "action": {"type": "BLOCK", "description": "不得准入"},
    }
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=structure
    )

    rules = await _rules(dbSession)
    assert rules[0].rule_expression["conditions"][0]["value"] == ["制造业", "批发业"]

    resp = await client.post(
        f"{_PAGES}/{pageId}/rules/dry-run",
        json={
            "examples": [
                {"input": {"行业": "制造业"}, "expectedOutput": {"matched": True}},
                {"input": {"行业": "餐饮业"}, "expectedOutput": {"matched": False}},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()["summary"]
    assert summary["passed"] == 2, resp.text
    assert summary["undecidable"] == 0, resp.text


async def test_materialization_rejects_membership_operator_without_list_value(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """集合算子的取值不是数组 → 422，建议不被盖成终态。

    这是**物化阶段的类型裁决点**：抽取阶段对取值形状是宽松的（与它对待算子同款态度
    ——只 strip 不校验），严格判定统一收在这里。放一条标量取值的 ``IN`` 进库，得到的
    就是一条永远判不了却看不出坏的规则。
    """
    modelId = await _seedModel(dbSession)
    pageId = await _createPage(
        client, title="取值形状错的集合规则", content=_RULE_CONTENT
    )
    structure = {
        "conditions": [{"field": "行业", "operator": "IN", "value": "制造业"}],
        "action": {"type": "BLOCK"},
    }
    fake = _FakeLlmClient(json.dumps(structure, ensure_ascii=False))
    with patch(_INVOKER_CLIENT, return_value=fake):
        generated = await client.post(
            f"{_PAGES}/{pageId}/suggestions", json={"modelId": modelId}
        )
    suggestionId = generated.json()["suggestions"][0]["id"]

    accepted = await client.post(f"{_SUGGESTIONS}/{suggestionId}/accept")

    assert accepted.status_code == 422, accepted.text
    assert await _rules(dbSession) == []
    dbSession.expire_all()
    listed = await client.get(f"{_PAGES}/{pageId}/suggestions")
    assert listed.json()["rows"][0]["status"] == "PENDING"


async def test_materialization_truncates_overlong_target_entity(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """超长的 ``target_entity`` 截到列宽，不能让它一路撞到 Postgres。

    ``target_entity`` 是 ``VARCHAR(100)``，而 SQLAlchemy **不做**长度校验 —— 超长值会
    到 Postgres 抛 ``DataError``，该异常没有领域异常映射，最终表现为用户可见的 500，
    而接受动作是终态不可逆的，用户只会看到「点了没反应」。

    截断而非 422：完整取值仍保留在 ``rule_expression.action.target_entity``（读模型
    也把它一并返回），截的只是这个索引列，没有信息真正丢失；而 422 会让用户卡在一条
    他无从修改的建议上（action 不由前端编辑）。
    """
    longEntity = "S" * 150
    structure = {
        "conditions": [
            {"field": "注册资本", "operator": ">=", "value": "1000", "unit": "万元"}
        ],
        "action": {"type": "BLOCK", "target_entity": longEntity},
    }
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=structure
    )

    rules = await _rules(dbSession)
    assert len(rules[0].target_entity) == 100
    assert rules[0].target_entity == "S" * 100
    # 完整取值仍在 JSONB 里（截断只作用于索引列）
    assert rules[0].rule_expression["action"]["target_entity"] == longEntity
    assert pageId


async def test_dry_run_rejects_excessive_example_count(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """样例条数有上限，不能凭一个请求让服务端跑无界的比对。

    求值是 ``O(样例数 × 条件数)`` 的纯 Python 循环，且响应会把 ``input`` 与每条条件的
    明细原样回显 —— 不设上限就是一个放大入口（全局 30 req/min 兜不住单请求的放大）。
    """
    structure = {
        "conditions": [
            {"field": "注册资本", "operator": ">=", "value": "1000", "unit": "万元"}
        ],
        "action": {"type": "BLOCK"},
    }
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=structure
    )

    examples = [
        {"input": {"注册资本": 1500}, "expectedOutput": {"matched": True}}
        for _ in range(101)
    ]
    resp = await client.post(
        f"{_PAGES}/{pageId}/rules/dry-run", json={"examples": examples}
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    ("operators", "expectedKind"),
    [
        ((">=",), "THRESHOLD"),
        ((">=", "<="), "THRESHOLD"),
        ((">=", "="), "THRESHOLD"),
        (("IN",), "SET_MEMBERSHIP"),
        (("NOT IN",), "SET_MEMBERSHIP"),
        ((">=", "IN"), "SET_MEMBERSHIP"),  # 集合判定优先于大小判定
        (("=",), "LOOKUP"),
        (("!=",), "LOOKUP"),
    ],
    ids=[
        "order-only",
        "order-pair",
        "mixed-eq-first",
        "in",
        "not-in",
        "membership-wins",
        "equality",
        "inequality",
    ],
)
def test_derive_rule_kind(operators: tuple[str, ...], expectedKind: str) -> None:
    """算子组合 → 规则形态（纯函数，无需 DB）。"""
    from app.services.learning.progressive_upgrader import deriveRuleKind

    expression = {
        "conditions": [
            {"field": "f", "operator": operator, "value": "1"}
            for operator in operators
        ],
        "action": {},
    }

    assert deriveRuleKind(expression) == expectedKind


async def test_accepted_process_suggestion_materializes_workflow(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """接受 PROCESS 建议 → 物化出流程（步骤 + 触发条件）+ FULLY_STRUCTURED。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_PROCESS_CONTENT, structure=_PROCESS_STRUCTURE
    )

    workflows = await _workflows(dbSession)
    assert len(workflows) == 1
    assert workflows[0].page_id == pageId
    assert [s["name"] for s in workflows[0].steps] == [
        "提交资料",
        "资质审查",
        "现场考察",
        "审批发布",
    ]
    assert workflows[0].trigger_condition == "新供应商引入"
    assert await _stage(client, pageId) == "FULLY_STRUCTURED"


async def test_stage_drops_back_when_artifact_row_is_removed(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """删掉产物行 → 掉回 SEMI_STRUCTURED。

    阶段是**派生**的而不是「只增不减的计数器」：产物没了阶段还留在 FULLY 的话，
    看板会长期高报结构化进度，且没人能发现。派生让它自愈。
    """
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )
    assert await _stage(client, pageId) == "FULLY_STRUCTURED"

    await dbSession.execute(
        text("DELETE FROM wiki_rule_executable WHERE page_id = :p"), {"p": pageId}
    )
    await dbSession.commit()

    resp = await client.post(f"{_PAGES}/{pageId}/structure/recompute")
    assert resp.json()["current"] == "SEMI_STRUCTURED"  # 已接受建议仍在
    assert resp.json()["changed"] is True


async def test_recompute_reports_no_change_when_stage_is_current(
    client: AsyncClient,
) -> None:
    """阶段没变时如实报 ``changed=false``（调用方能区分「跑了」与「改了」）。"""
    pageId = await _createPage(client, title="无变化")

    resp = await client.post(f"{_PAGES}/{pageId}/structure/recompute")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "pageId": pageId,
        "previous": "MARKDOWN",
        "current": "MARKDOWN",
        "changed": False,
    }


async def _discoverReferenceCandidate(
    client: AsyncClient, *, title: str = "供应商管理"
) -> tuple[str, int]:
    """造一条确定性的引用关系候选（不给 modelId，引用检测是纯规则路径）。

    返回 ``(source 页 pageId, 候选 id)``。
    """
    targetId = await _createPage(client, title="供应商准入规则", content="目标页")
    sourceId = await _createPage(
        client, title=title, content="见供应商准入规则。"
    )
    discovered = await client.post(
        f"{_PAGES}/{sourceId}/relations/discover", json={"modelId": None}
    )
    assert discovered.status_code == 200, discovered.text
    candidates = discovered.json()["candidates"]
    assert len(candidates) == 1, discovered.text
    assert candidates[0]["downstreamId"] == targetId
    return sourceId, candidates[0]["id"]


async def test_confirmed_relation_moves_page_to_semi_structured(
    client: AsyncClient,
) -> None:
    """走真实链路确认一条关系候选 → 阶段升到 SEMI_STRUCTURED。

    这条是「阶段随时反映现实」的活体证明：关系是 M4 的产物，而它的确认动作会
    顺带把阶段推进，用户不需要额外点一次「重算」。
    """
    pageId, relationId = await _discoverReferenceCandidate(client)

    confirmed = await client.post(f"{_RELATIONS}/{relationId}/confirm")

    assert confirmed.status_code == 200, confirmed.text
    assert await _stage(client, pageId) == "SEMI_STRUCTURED"


async def test_rejected_relation_does_not_move_stage(client: AsyncClient) -> None:
    """被打回的关系不算结构升级（同建议的 PENDING/REJECTED 口径）。"""
    pageId, relationId = await _discoverReferenceCandidate(client)

    rejected = await client.post(f"{_RELATIONS}/{relationId}/reject")

    assert rejected.status_code == 200, rejected.text
    assert await _stage(client, pageId) == "MARKDOWN"


async def test_repeat_materialization_upserts_single_row(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同一条知识第二次被接受为规则 → **覆盖**，不是撞唯一键报 500。

    建议是终态不可逆的，但「改完正文重新抽一条新建议再接受」是合法路径。表上
    ``page_id`` 唯一约束表达「一条知识一份产物」，写入就得走 UPSERT。
    """
    modelId = await _seedModel(dbSession)
    pageId = await _createPage(client, title="会被改的规则", content=_RULE_CONTENT)

    for threshold in ("1000", "2000"):
        structure = {
            "conditions": [
                {
                    "field": "注册资本",
                    "operator": ">=",
                    "value": threshold,
                    "unit": "万元",
                }
            ],
            "action": {"type": "BLOCK", "target_entity": "SUPPLIER"},
        }
        fake = _FakeLlmClient(json.dumps(structure, ensure_ascii=False))
        with patch(_INVOKER_CLIENT, return_value=fake):
            resp = await client.post(
                f"{_PAGES}/{pageId}/suggestions", json={"modelId": modelId}
            )
        assert resp.status_code == 200, resp.text
        suggestionId = resp.json()["suggestions"][0]["id"]
        accepted = await client.post(f"{_SUGGESTIONS}/{suggestionId}/accept")
        assert accepted.status_code == 200, accepted.text

    rules = await _rules(dbSession)
    assert len(rules) == 1
    assert rules[0].rule_expression["conditions"][0]["value"] == "2000"


async def test_reject_does_not_materialize(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """拒绝不产物（否则「打回」也能改生产规则）。"""
    modelId = await _seedModel(dbSession)
    pageId = await _createPage(client, title="被拒的规则", content=_RULE_CONTENT)
    fake = _FakeLlmClient(json.dumps(_RULE_STRUCTURE, ensure_ascii=False))
    with patch(_INVOKER_CLIENT, return_value=fake):
        resp = await client.post(
            f"{_PAGES}/{pageId}/suggestions", json={"modelId": modelId}
        )
    suggestionId = resp.json()["suggestions"][0]["id"]

    await client.post(f"{_SUGGESTIONS}/{suggestionId}/reject")

    assert await _rules(dbSession) == []
    assert await _workflows(dbSession) == []


# ---------------------------------------------------------------------------
# 读取与 dry-run 接口
# ---------------------------------------------------------------------------


async def test_get_rule_404_when_absent(client: AsyncClient) -> None:
    """没有规则的条目 → 404（而不是空对象，空对象读起来像「有一条空规则」）。"""
    pageId = await _createPage(client, title="没有规则")

    resp = await client.get(f"{_PAGES}/{pageId}/rule")

    assert resp.status_code == 404


async def test_get_rule_returns_materialized(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """物化后能读回来（Agent 侧直读入口）。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )

    resp = await client.get(f"{_PAGES}/{pageId}/rule")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ruleKind"] == "THRESHOLD"
    assert body["targetEntity"] == "SUPPLIER"
    assert body["ruleExpression"]["conditions"][0]["operator"] == ">="


async def test_get_workflow_404_when_absent(client: AsyncClient) -> None:
    """没有流程的条目 → 404。"""
    pageId = await _createPage(client, title="没有流程")

    resp = await client.get(f"{_PAGES}/{pageId}/workflow")

    assert resp.status_code == 404


async def test_get_workflow_returns_materialized(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """物化后能读回流程（含步骤顺序）。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_PROCESS_CONTENT, structure=_PROCESS_STRUCTURE
    )

    resp = await client.get(f"{_PAGES}/{pageId}/workflow")

    assert resp.status_code == 200, resp.text
    assert [s["seq"] for s in resp.json()["steps"]] == [1, 2, 3, 4]


async def test_dry_run_uses_request_examples(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """dry-run 走请求带的样例。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )

    resp = await client.post(
        f"{_PAGES}/{pageId}/rules/dry-run",
        json={
            "examples": [
                {
                    "input": {"注册资本": 1500},
                    "expectedOutput": {"matched": True},
                },
                {
                    "input": {"注册资本": 500},
                    "expectedOutput": {"matched": True},
                },
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ruleKind"] == "THRESHOLD"
    assert body["summary"] == {"total": 2, "passed": 1, "failed": 1, "undecidable": 0}
    assert [r["status"] for r in body["results"]] == ["PASSED", "FAILED"]


async def test_dry_run_without_examples_uses_stored_examples(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """不传样例时用规则里存的 ``dry_run_examples``（审核当时给的期望）。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )
    # 直接写入存的样例（M6 没有编辑规则的接口，编辑入口留到 M8 的规则工作台）
    await dbSession.execute(
        text(
            "UPDATE wiki_rule_executable SET dry_run_examples = "
            "CAST(:j AS jsonb) WHERE page_id = :p"
        ),
        {
            "j": json.dumps(
                [{"input": {"注册资本": 1500}, "expectedOutput": {"matched": True}}]
            ),
            "p": pageId,
        },
    )
    await dbSession.commit()

    resp = await client.post(f"{_PAGES}/{pageId}/rules/dry-run", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"]["total"] == 1


async def test_dry_run_with_no_request_body_uses_stored_examples(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """**完全不传 body** 也走「用存的样例」，与传 ``{}`` 同义。

    body 是可选的：``examples`` 本身就可选，而「不传 body」与「body 里没有
    examples」在语义上是同一件事（都用存的）。若把 body 声明成必填，不传 body
    会先撞 422 请求校验，调用方拿不到「用存的」这条默认路径 —— 契约文档说的
    三态（不传 / 传空数组 / 传样例）在入口就被砍掉一态。
    """
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )
    await dbSession.execute(
        text(
            "UPDATE wiki_rule_executable SET dry_run_examples = "
            "CAST(:j AS jsonb) WHERE page_id = :p"
        ),
        {
            "j": json.dumps(
                [{"input": {"注册资本": 1500}, "expectedOutput": {"matched": True}}]
            ),
            "p": pageId,
        },
    )
    await dbSession.commit()

    resp = await client.post(f"{_PAGES}/{pageId}/rules/dry-run")

    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"]["total"] == 1


async def test_dry_run_404_when_no_rule(client: AsyncClient) -> None:
    """没有规则就没什么可 dry-run 的 → 404。"""
    pageId = await _createPage(client, title="没有规则")

    resp = await client.post(f"{_PAGES}/{pageId}/rules/dry-run", json={})

    assert resp.status_code == 404


async def test_dry_run_404_when_page_absent(client: AsyncClient) -> None:
    """条目不存在 → 404。"""
    resp = await client.post(
        "/api/v1/wiki/pages/no-such-page/rules/dry-run", json={}
    )

    assert resp.status_code == 404


async def test_dry_run_rejects_malformed_example_with_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """样例形状不对 → 422（形状校验闭环到 HTTP 层）。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )

    resp = await client.post(
        f"{_PAGES}/{pageId}/rules/dry-run",
        json={"examples": [{"input": {"注册资本": 1500}}]},
    )

    assert resp.status_code == 422


async def test_recompute_404_when_page_absent(client: AsyncClient) -> None:
    """重算不存在的条目 → 404。"""
    resp = await client.post("/api/v1/wiki/pages/no-such-page/structure/recompute")

    assert resp.status_code == 404


async def test_stage_endpoints_require_auth(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """认证关闭时整组结构化接口 403（router 级依赖，同 wiki 其他路由）。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")

    assert (await client.get(f"{_PAGES}/{pageId}/rule")).status_code == 403
    assert (await client.get(f"{_PAGES}/{pageId}/workflow")).status_code == 403
    assert (
        await client.post(f"{_PAGES}/{pageId}/rules/dry-run", json={})
    ).status_code == 403
    assert (
        await client.post(f"{_PAGES}/{pageId}/structure/recompute")
    ).status_code == 403


async def test_delete_page_cascades_artifacts(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """删条目 → 产物被 FK 级联清掉（不留孤儿规则）。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )
    assert len(await _rules(dbSession)) == 1

    deleted = await client.delete(f"{_PAGES}/{pageId}")
    assert deleted.status_code == 204, deleted.text
    dbSession.expire_all()

    assert await _rules(dbSession) == []


async def test_orphan_suggestion_does_not_block_accept(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """建议行仍在（级联清理的对象是产物与建议，此处验证外键链不互相打架）。"""
    pageId, _ = await _acceptSuggestionFor(
        client, dbSession, content=_RULE_CONTENT, structure=_RULE_STRUCTURE
    )

    result = await dbSession.execute(
        select(StructureSuggestion).where(StructureSuggestion.page_id == pageId)
    )

    assert result.scalars().all() != []
