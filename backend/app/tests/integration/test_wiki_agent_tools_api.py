"""Wiki 知识工具接 Agent 的集成测试（feat-wiki-knowledge M8）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）：
页面与规则经 HTTP 或真实 ORM 落到 ``qa_metadata_test``，Agent 运行一律走
``POST /api/v1/agents/{agent_code}/run``，断言的是**对外契约**而不是 service 返回值。

覆盖四条刻意的设计约束：

1. **只读**：四个工具都不改库（用取运行前后的行快照对比来证明，而不是宣称）。
2. **指称解析不猜**：0 条 → 404；多条 → 200 + ``resolved=false`` + 候选，绝不
   替调用方挑一条。
3. **规则试跑不臆造 record**：没有样例时返回 ``status=NO_EXAMPLES`` 而不是
   拿空记录跑出一份「全判不了」的假报告。
4. **ACL 跟着工具走**：层无关工具的授权对象必须等于工具自己的 ``data_object``。
   这是 M8 修掉的静默 fail-closed —— 旧 ``_policiesFor`` 对层无关工具一律发
   SUPPLIER 通配策略，WIKI_* 工具会在运行时全量 403，而 seed 看起来「成功」。
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import (
    AgentPermission,
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
)
from app.domain.schemas import AgentAccessPolicyCreate, AgentDefinitionCreate
from app.domain.wiki_learning_models import WikiRuleExecutable
from app.domain.wiki_models import KnowledgeRelation, WikiPage
from app.services.agent_binding_cache import agent_binding_cache
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_tool_config_registry import agent_tool_config_registry

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_ADMIN = CurrentUser(userId="test-admin", roles=("admin",))
AUTH_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

# Agent → 工具绑定（与 scripts.seed_agents._AGENT_DEFAULT_BINDINGS 的 Wiki 段一致）
_WIKI_BINDINGS: dict[str, str] = {
    "WIKI_SEARCH_AGENT": "wiki_search",
    "WIKI_READ_AGENT": "wiki_read",
    "WIKI_RULE_AGENT": "rule_evaluate",
    "WIKI_COVERAGE_AGENT": "coverage_status",
}

# 工具声明的 data_object（ACL 主体）。层无关工具按对象粒度授权的回归依据。
_OBJECT_BY_TOOL: dict[str, str] = {
    "wiki_search": "WIKI_PAGE",
    "wiki_read": "WIKI_PAGE",
    "rule_evaluate": "WIKI_RULE",
    "coverage_status": "WIKI_COVERAGE",
}


# ---------------------------------------------------------------------------
# Arrange 助手
# ---------------------------------------------------------------------------


async def _seedAgent(
    dbSession: AsyncSession,
    code: str,
    *,
    status: AgentStatus = AgentStatus.ACTIVE,
    policies: list[AgentAccessPolicyCreate] | None = None,
) -> None:
    """按 code 注册一个绑定 Wiki 工具的 Agent。

    默认策略**复刻 seed_agents._policiesFor 对层无关工具的产出口径**：一条
    ``data_layer=None``、对象为工具自身 ``data_object`` 的 READ 策略。测试若
    写成「随便给个 SUPPLIER 通配」，就会把要验的 ACL 语义验反。
    """
    tool_name = _WIKI_BINDINGS[code]
    if policies is None:
        policies = [
            AgentAccessPolicyCreate(
                data_object=_OBJECT_BY_TOOL[tool_name],
                permission=AgentPermission.READ,
                data_layer=None,
                notes="集成测试（层无关工具，对象粒度）",
            )
        ]
    service = AgentRegistryService()
    await service.createAgent(
        dbSession,
        AgentDefinitionCreate(
            agent_code=code,
            agent_name=f"{code} 集成测试",
            description="feat-wiki-knowledge M8 集成测试",
            trigger_type=AgentTriggerType.USER_QUESTION,
            response_latency=AgentResponseLatency.REALTIME,
            data_domains=[],
            data_layers=[],
            status=status,
            version="v1.0",
            tool_name=tool_name,
            policies=policies,
        ),
        _ADMIN,
    )
    # 绑定缓存是运行时读 tool_name 的唯一入口，写完必须刷新（lifespan 之外无自动失效）
    agent_binding_cache.invalidate()
    await agent_binding_cache.warmUp(dbSession)


async def _createPage(
    client: AsyncClient,
    *,
    pageId: str,
    title: str,
    content: str,
    dimension: str = "RULE",
) -> None:
    """经 REST 创建知识条目（走完整 API 链路而非直接 INSERT）。"""
    response = await client.post(
        _PAGES,
        json={
            "pageId": pageId,
            "title": title,
            "content": content,
            "dimension": dimension,
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 201, response.text


async def _seedRule(
    dbSession: AsyncSession,
    *,
    pageId: str,
    examples: list[dict] | None,
    kind: str = "THRESHOLD",
) -> None:
    """直接落一条可执行规则（规则由机制 4/5 物化，没有对外的创建端点）。"""
    dbSession.add(
        WikiRuleExecutable(
            page_id=pageId,
            rule_kind=kind,
            rule_expression={
                "conditions": [
                    # 键名是 operator（wiki_rule_engine._evaluateCondition 的读法），
                    # 不是 op —— 写错会以「算子 None 系统不认识」报 422，报错位置
                    # 离真正写错的地方很远。
                    {"field": "注册资本", "operator": ">=", "value": 1000},
                ],
                "action": {"type": "REJECT", "reason": "注册资本不足"},
            },
            target_entity="SUPPLIER",
            dry_run_examples=examples,
            authority_chain=["L4", "L2"],
            version="v1.0",
        )
    )
    await dbSession.commit()


async def _run(client: AsyncClient, agentCode: str, text: str):
    return await client.post(
        f"/api/v1/agents/{agentCode}/run",
        json={"input": text},
        headers=AUTH_HEADERS,
    )


async def _snapshotPages(dbSession: AsyncSession) -> list[tuple]:
    """条目表快照（证明工具只读：运行前后必须一模一样）。"""
    rows = (await dbSession.execute(select(WikiPage).order_by(WikiPage.page_id))).scalars().all()
    return [
        (r.page_id, r.title, r.content, r.dimension, r.status, r.updated_time) for r in rows
    ]


# ---------------------------------------------------------------------------
# wiki_search
# ---------------------------------------------------------------------------


class TestWikiSearch:
    async def test_returns_matching_pages_with_snippet(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        await _seedAgent(dbSession, "WIKI_SEARCH_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="注册资本不低于 1000 万元，且近三年无重大质量事故。",
        )
        await _createPage(
            client,
            pageId="PAGE-QUALITY-0002",
            title="来料检验规范",
            content="按 AQL 抽检，不合格批次退回。",
        )

        response = await _run(client, "WIKI_SEARCH_AGENT", "供应商准入")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tool"] == "wiki_search"
        items = body["result"]["items"]
        assert [i["pageId"] for i in items] == ["PAGE-ADMISSION-0001"]
        assert "注册资本" in items[0]["snippet"]
        assert body["result"]["total"] == 1
        # 无 LLM 调用 → 计量字段恒为 0（有调用就必须落 wiki_token_usage）
        assert body["tokensUsed"] == 0
        assert body["cost"] == 0.0

    async def test_empty_result_is_an_answer_not_an_error(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """检索无命中是**正常的空结果**：报错会让 Agent 把「知识库里没有」
        误当成「工具坏了」，转而去重试或换工具。"""
        await _seedAgent(dbSession, "WIKI_SEARCH_AGENT")

        response = await _run(client, "WIKI_SEARCH_AGENT", "完全不存在的主题")

        assert response.status_code == 200, response.text
        assert response.json()["result"]["items"] == []
        assert "没有检索到" in response.json()["answer"]

    async def test_like_wildcards_in_query_are_literal(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """用户输入里的 % 必须当字面量：不转义就会退化成「匹配任意内容」。"""
        await _seedAgent(dbSession, "WIKI_SEARCH_AGENT")
        await _createPage(
            client,
            pageId="PAGE-RATE-0001",
            title="增长率算法",
            content="同比增长率 = (本期 - 同期) / 同期。",
        )

        response = await _run(client, "WIKI_SEARCH_AGENT", "%")

        assert response.status_code == 200, response.text
        # 若 % 被当成通配符，这里会命中全部条目
        assert response.json()["result"]["items"] == []

    async def test_empty_input_is_422(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        await _seedAgent(dbSession, "WIKI_SEARCH_AGENT")

        response = await _run(client, "WIKI_SEARCH_AGENT", "   ")

        assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# wiki_read
# ---------------------------------------------------------------------------


class TestWikiRead:
    async def test_reads_exact_title_with_confirmed_relations_only(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        await _seedAgent(dbSession, "WIKI_READ_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="注册资本不低于 1000 万元。",
        )
        # 已确认 + 未审核的候选各一条：后者是机器提的猜测，绝不能进答案
        dbSession.add_all(
            [
                KnowledgeRelation(
                    upstream_page_id="PAGE-ADMISSION-0001",
                    downstream_type="ONTOLOGY_CLASS",
                    downstream_id="supplier",
                    relation_type="APPLIES_TO",
                    confirmed=True,
                ),
                KnowledgeRelation(
                    upstream_page_id="PAGE-ADMISSION-0001",
                    downstream_type="ONTOLOGY_CLASS",
                    downstream_id="purchase_order",
                    relation_type="REFERENCES",
                    confirmed=False,
                ),
            ]
        )
        await dbSession.commit()

        response = await _run(client, "WIKI_READ_AGENT", "供应商准入要求")

        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["pageId"] == "PAGE-ADMISSION-0001"
        assert "注册资本不低于 1000 万元" in result["content"]
        assert [r["downstreamId"] for r in result["relations"]] == ["supplier"]

    async def test_ambiguous_ref_returns_candidates_instead_of_guessing(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """两条都含「供应商」→ 不能替用户挑一条，必须把候选摊开。"""
        await _seedAgent(dbSession, "WIKI_READ_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="准入条件说明。",
        )
        await _createPage(
            client,
            pageId="PAGE-SUPPLIER-0002",
            title="供应商分级管理办法",
            content="分级标准说明。",
        )

        response = await _run(client, "WIKI_READ_AGENT", "供应商")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["result"]["resolved"] is False
        assert {c["pageId"] for c in body["result"]["candidates"]} == {
            "PAGE-ADMISSION-0001",
            "PAGE-SUPPLIER-0002",
        }
        assert "请指明" in body["answer"]

    async def test_exact_page_id_beats_fuzzy_hits(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """抄了 ID 却因为别的条目正文里提过这个 ID 而变成「多条命中」，
        会让用户以为指称没生效。精确命中必须唯一解析。"""
        await _seedAgent(dbSession, "WIKI_READ_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="准入条件说明。",
        )
        await _createPage(
            client,
            pageId="PAGE-NOTES-0002",
            title="评审会议纪要",
            content="会上引用了 PAGE-ADMISSION-0001 的条款。",
        )

        response = await _run(client, "WIKI_READ_AGENT", "PAGE-ADMISSION-0001")

        assert response.status_code == 200, response.text
        assert response.json()["result"]["pageId"] == "PAGE-ADMISSION-0001"

    async def test_not_found_is_404(self, client: AsyncClient, dbSession: AsyncSession) -> None:
        await _seedAgent(dbSession, "WIKI_READ_AGENT")

        response = await _run(client, "WIKI_READ_AGENT", "查无此条")

        assert response.status_code == 404, response.text
        # 领域异常走统一错误信封：message 在 error，不在 detail
        assert "查无此条" in response.json()["error"]

    async def test_read_surfaces_structured_rule(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        await _seedAgent(dbSession, "WIKI_READ_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="注册资本不低于 1000 万元。",
        )
        await _seedRule(dbSession, pageId="PAGE-ADMISSION-0001", examples=None)

        response = await _run(client, "WIKI_READ_AGENT", "PAGE-ADMISSION-0001")

        assert response.status_code == 200, response.text
        rule = response.json()["result"]["rule"]
        assert rule["ruleKind"] == "THRESHOLD"
        assert rule["targetEntity"] == "SUPPLIER"
        assert rule["exampleCount"] == 0


# ---------------------------------------------------------------------------
# rule_evaluate
# ---------------------------------------------------------------------------


class TestRuleEvaluate:
    async def test_reports_three_states(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """通过 / 失败 / 判不了 三态必须分别报出 —— 把「判不了」并进失败会把
        「样例缺字段」误导成「规则写错了」。"""
        await _seedAgent(dbSession, "WIKI_RULE_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="注册资本不低于 1000 万元。",
        )
        await _seedRule(
            dbSession,
            pageId="PAGE-ADMISSION-0001",
            examples=[
                # 通过：800 < 1000 → 不命中，与期望一致
                {"input": {"注册资本": 800}, "expectedOutput": {"matched": False}},
                # 失败：3000 >= 1000 → 命中，但期望说不命中
                {"input": {"注册资本": 3000}, "expectedOutput": {"matched": False}},
                # 判不了：缺字段
                {"input": {"公司名称": "X"}, "expectedOutput": {"matched": True}},
            ],
        )

        response = await _run(client, "WIKI_RULE_AGENT", "PAGE-ADMISSION-0001")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["result"]["status"] == "EVALUATED"
        assert body["result"]["summary"] == {
            "total": 3,
            "passed": 1,
            "failed": 1,
            "undecidable": 1,
        }
        statuses = [c["status"] for c in body["result"]["cases"]]
        assert statuses == ["PASSED", "FAILED", "UNDECIDABLE"]
        assert "判不了 1" in body["answer"]
        assert "样例缺字段" in body["answer"]

    async def test_no_examples_is_an_answer_not_a_crash(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """没有样例时**不能**拿空记录跑一份全「判不了」的假报告。"""
        await _seedAgent(dbSession, "WIKI_RULE_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="注册资本不低于 1000 万元。",
        )
        await _seedRule(dbSession, pageId="PAGE-ADMISSION-0001", examples=[])

        response = await _run(client, "WIKI_RULE_AGENT", "PAGE-ADMISSION-0001")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["result"]["status"] == "NO_EXAMPLES"
        assert "dry-run 样例" in body["answer"]
        assert "cases" not in body["result"]

    async def test_page_without_rule_is_404(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        await _seedAgent(dbSession, "WIKI_RULE_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="只是描述，没有可执行规则。",
        )

        response = await _run(client, "WIKI_RULE_AGENT", "PAGE-ADMISSION-0001")

        assert response.status_code == 404, response.text
        assert "没有可执行规则" in response.json()["error"]

    async def test_page_not_found_is_404(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        await _seedAgent(dbSession, "WIKI_RULE_AGENT")

        response = await _run(client, "WIKI_RULE_AGENT", "查无此条")

        assert response.status_code == 404, response.text
        assert "查无此条" in response.json()["error"]


# ---------------------------------------------------------------------------
# coverage_status
# ---------------------------------------------------------------------------


class TestCoverageStatus:
    async def test_no_args_never_422_and_reports_matrix(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """无参工具：任意问句（哪怕只有空白）都必须跑起来。

        ``AgentRunRequest.input`` 是 ``min_length=1`` —— 真正的空串在 schema
        层就 422 了，根本到不了提取器；能到提取器的最弱输入是纯空白，而
        ``wiki_no_args`` 对它返回 ``{}``。覆盖率查询不该因为用户没把问题说
        清楚就被拒。
        """
        await _seedAgent(dbSession, "WIKI_COVERAGE_AGENT")
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="注册资本不低于 1000 万元。",
        )

        response = await _run(client, "WIKI_COVERAGE_AGENT", "   ")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tool"] == "coverage_status"
        assert set(body["result"]) == {"summary", "gaps", "unlinked"}
        # 条目已建但没挂业务对象 → 未挂载计数必须出现（矩阵看不见的那部分）
        unlinked = body["result"]["unlinked"]
        assert unlinked["pageCount"] == 1
        assert "没有挂到任何已确认的业务对象" in body["answer"]


# ---------------------------------------------------------------------------
# 横切约束
# ---------------------------------------------------------------------------


class TestCrossCutting:
    async def test_tools_are_read_only(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """四个工具跑一遍，条目表必须逐字段不变（只读是承诺，要验不要信）。"""
        for code in _WIKI_BINDINGS:
            await _seedAgent(dbSession, code)
        await _createPage(
            client,
            pageId="PAGE-ADMISSION-0001",
            title="供应商准入要求",
            content="注册资本不低于 1000 万元。",
        )
        await _seedRule(
            dbSession,
            pageId="PAGE-ADMISSION-0001",
            examples=[
                {"input": {"注册资本": 800}, "expectedOutput": {"matched": False}},
            ],
        )
        before = await _snapshotPages(dbSession)

        for code, text in (
            ("WIKI_SEARCH_AGENT", "供应商"),
            ("WIKI_READ_AGENT", "PAGE-ADMISSION-0001"),
            ("WIKI_RULE_AGENT", "PAGE-ADMISSION-0001"),
            ("WIKI_COVERAGE_AGENT", "现在的覆盖度如何"),
        ):
            assert (await _run(client, code, text)).status_code == 200

        assert await _snapshotPages(dbSession) == before

    async def test_object_grain_policy_is_what_makes_wiki_tools_reachable(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """ACL 回归：层无关工具必须拿到**自己 data_object** 的策略。

        旧 ``_policiesFor`` 对层无关工具一律发 SUPPLIER 通配策略 —— seed 日志
        显示成功，运行时却因 ``p.data_object == tool.data_object`` 精确比对
        全量 403。这条用例把「授权跟着工具走」钉死。
        """
        await _seedAgent(
            dbSession,
            "WIKI_SEARCH_AGENT",
            policies=[
                AgentAccessPolicyCreate(
                    data_object="SUPPLIER",  # 旧行为的产物
                    permission=AgentPermission.READ,
                    data_layer=None,
                    notes="旧种子遗留：对象错位",
                )
            ],
        )

        response = await _run(client, "WIKI_SEARCH_AGENT", "供应商")

        assert response.status_code == 403, response.text

    async def test_no_policy_at_all_is_403(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """deny-by-default：零策略不是「全放开」，是「全拒绝」。"""
        await _seedAgent(dbSession, "WIKI_SEARCH_AGENT", policies=[])

        response = await _run(client, "WIKI_SEARCH_AGENT", "供应商")

        assert response.status_code == 403, response.text

    async def test_data_object_is_normalized_upper(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """小写策略对象经归一化后仍能命中工具（否则授权会静默失效）。"""
        await _seedAgent(
            dbSession,
            "WIKI_SEARCH_AGENT",
            policies=[
                AgentAccessPolicyCreate(
                    data_object="  wiki_page  ",
                    permission=AgentPermission.READ,
                    data_layer=None,
                    notes="大小写/空白归一化",
                )
            ],
        )

        response = await _run(client, "WIKI_SEARCH_AGENT", "供应商")

        assert response.status_code == 200, response.text

    async def test_seed_policies_for_wiki_agent_are_object_grain(
        self, dbSession: AsyncSession
    ) -> None:
        """直接对 ``_policiesFor`` 的产出口径断言（seed 是这层语义的 SSOT）。

        与上面那条 403 用例互为表里：一条钉「错对象会 403」，这条钉「seed
        不会产出错对象」。
        """
        from scripts.seed_agents import _policiesFor

        policies = await _policiesFor(dbSession, "WIKI_SEARCH_AGENT")

        assert len(policies) == 1
        assert policies[0].data_object == "WIKI_PAGE"
        assert policies[0].data_layer is None
        assert policies[0].permission is AgentPermission.READ

    async def test_supplier_agent_policies_unchanged(self, dbSession: AsyncSession) -> None:
        """分层工具的策略口径不受本次改动影响（只动了层无关那条分支）。"""
        from scripts.seed_agents import _policiesFor

        policies = await _policiesFor(dbSession, "SUPPLIER_360_AGENT")

        assert {(p.data_object, p.data_layer) for p in policies} == {
            ("SUPPLIER", "DIM"),
            ("SUPPLIER", "FEATURE"),
        }

    async def test_seed_bindings_all_resolve_to_registered_tools(
        self, dbSession: AsyncSession
    ) -> None:
        """seed 里的每个绑定都必须能在 registry 里解析到 —— 否则运行时 409，
        而 seed 自己不报错（这正是 Wiki 工具第一次接进来时最可能的坏法）。"""
        from scripts.seed_agents import _AGENT_DEFAULT_BINDINGS

        for agent_code, tool_name in _AGENT_DEFAULT_BINDINGS.items():
            tool = agent_tool_config_registry.get(tool_name)
            assert tool is not None, f"{agent_code} → {tool_name} 未注册"
            assert tool.handler is not None

        # 工具名单与 Wiki 段一致（防止 seed 改名后测试静默失效）
        wiki_tools = {t for t in _WIKI_BINDINGS.values()}
        assert wiki_tools <= set(_AGENT_DEFAULT_BINDINGS.values())
