"""集成测试：术语词典条目注入 NL2SQL 计划 prompt（供应商报价含税/时间/地点维度）。

用户场景（2026-08-17）：供应商报价存在多档有效数据——按价格表号分 含税 T10 /
不含税 T11 / 含税地点 T20 / 不含税地点 T21，按 生效日期/失效日期 分时间段，
按价格条件分地点。LLM 倾向把所有报价 AVG 平均，且可能把「含税价」误映射为
PPRICCONF.价格表号 的别名（那是价格清单号，不是单价）。

修复（用户选择第 3 项）：把 5 条术语写入术语词典，计划阶段以 <term_dictionary>
块注入 prompt，引导 LLM 按 价格表号+生效期+地点 维度取价、不跨清单/跨期平均。

本测试锁定整条注入链（术语 → renderDictionaryText → 计划 system prompt）：
- 词典非空时，计划阶段 system prompt 出现「含税价」等词典段（渲染格式含
  「term」：definition（类 X、属性 Y、hint））；
- 词典空表时，计划 prompt 无 <term_dictionary> 段（控制组，防误注入）。

说明：与 test_receiptdetail_qty_alias.py 同模式——模型路由 / LLM / 适配器 /
embedding 均以 fake 注入；术语经 POST /api/v1/term-dictionary 写入，走真实 DB。
"""

from __future__ import annotations

from decimal import Decimal

import seed_ontology
from app.domain.models import DataSource, LlmConfig, OntologyClass, OntologyProperty
from app.infrastructure.security.crypto import encryptApiKey

def _supplierProperties() -> list[OntologyProperty]:
    """与 seed_ontology.py PPRICLIST 本体一致的属性子集（聚焦取价相关列）。

    每次调用返回全新实例：OntologyProperty 是 ORM 实例，跨测试复用会被上一
    个测试会话标记持久化（class_id 已赋值），重跑时 flush 因 identity map
    冲突静默跳过属性写入，导致类存在但属性为空（真实回归 2026-08-17）。
    """
    return [
        OntologyProperty(property_name="价格表号", data_type="STRING", source_column="PLI_0"),
        OntologyProperty(
            property_name="物料编码",
            data_type="STRING",
            source_column="PLICRI2_0",
            business_aliases=["价格条件3", "物料编号"],
        ),
        OntologyProperty(
            property_name="单价",
            data_type="DECIMAL",
            source_column="PRI_0",
            business_aliases=["报价", "供应商报价", "采购报价"],
        ),
        OntologyProperty(property_name="生效日期", data_type="DATETIME", source_column="PLISTRDAT_0"),
        OntologyProperty(property_name="失效日期", data_type="DATETIME", source_column="PLIENDDAT_0"),
        # 价格条件4 = 取价地点（工厂）列，值即 FACILITY.FCY_0（T20/T21 清单使用）
        OntologyProperty(
            property_name="价格条件4",
            data_type="STRING",
            source_column="PLICRI3_0",
            business_aliases=["地点", "地点编码", "工厂", "场所", "地点编号"],
        ),
    ]

# 与 dev 环境写入的 5 条术语一致（含税/地点维度 + 总纲），用于注入链断言
TERMS = [
    {
        "term": "含税价",
        "definition": "供应商含税单价。查询取 SupplierPriceDetail 的单价，并须按价格表号筛 T10。",
        "mappedClassName": "SupplierPriceDetail",
        "mappedPropertyName": "单价",
        "formulaHint": "WHERE 价格表号='T10'",
    },
    {
        "term": "不含税价",
        "definition": "供应商不含税单价。查询取 SupplierPriceDetail 的单价，并须按价格表号筛 T11。",
        "mappedClassName": "SupplierPriceDetail",
        "mappedPropertyName": "单价",
        "formulaHint": "WHERE 价格表号='T11'",
    },
    {
        "term": "含税价(地点)",
        "definition": "按地点区分的供应商含税单价，价格清单 T20；地点取价格条件列中的地点值。",
        "mappedClassName": "SupplierPriceDetail",
        "mappedPropertyName": "单价",
        "formulaHint": "WHERE 价格表号='T20'",
    },
    {
        "term": "不含税价(地点)",
        "definition": "按地点区分的供应商不含税单价，价格清单 T21；地点取价格条件列中的地点值。",
        "mappedClassName": "SupplierPriceDetail",
        "mappedPropertyName": "单价",
        "formulaHint": "WHERE 价格表号='T21'",
    },
    {
        "term": "供应商报价",
        "definition": "供应商报价是 SupplierPriceDetail 的单价，可按含税/时间段/地点分多条有效报价。",
        "mappedClassName": "SupplierPriceDetail",
        "mappedPropertyName": "单价",
        "formulaHint": "未明确维度时取当期生效单价，不把所有报价平均",
    },
]


_DEFAULT_PLAN = (
    '{"target":"查询 A 物料的含税价（按价格表号拆分）",'
    '"selectedClasses":["SupplierPriceDetail"],'
    '"selectedProperties":["价格表号","单价"],'
    '"conditions":["PLI_0 = \'T10\'"],'
    '"aggregations":[],'
    '"groupBy":["价格表号"],'
    '"rowLimit":10}'
)


class _PipelineLlm:
    """按 prompt 内容路由回复：计划 / SQL / 回答；记录每次调用消息。

    计划阶段返回注入的计划（默认「按价格表号拆分供应商含税单价」，可传
    planJson 覆盖，如按地点过滤的 T20 查询），保证完整链路推进到 SQL 阶段。
    """

    def __init__(self, planJson: str | None = None) -> None:
        self.calls: list[list[tuple[str, str]]] = []
        self.planSystemPrompt: str | None = None
        self._planJson = planJson or _DEFAULT_PLAN

    async def complete(self, messages: list, **kwargs) -> object:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "解析为查询计划" in system:
            self.planSystemPrompt = system
            _Resp.content = self._planJson
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT PLI_0, PRI_0 FROM ZJTH.PPRICLIST "
                "WHERE PLI_0 = 'T10' FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            _Resp.content = "查询完成，共 1 条记录，含税价如下。"
        return _Resp()


class _FakeAdapter:
    async def execute_read_only(self, sql: str) -> list[dict]:
        return [{"PLI_0": "T10", "PRI_0": Decimal("12.50")}]


async def _seed(session) -> tuple[LlmConfig, DataSource]:
    """写入模型配置、数据源与 SupplierPriceDetail 本体类，返回 (config, ds)。"""
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    ds = DataSource(
        name="ZJTH",
        type="oracle",
        host="h",
        port=1521,
        database_name="svc",
        username="u",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
        is_default=True,
    )
    cls = OntologyClass(
        class_name="SupplierPriceDetail",
        class_alias="供应商价格明细",
        source_table="ZJTH.PPRICLIST",
        properties=_supplierProperties(),
    )
    session.add_all([config, ds, cls])
    await session.commit()
    await session.refresh(config)
    await session.refresh(ds)
    return config, ds


class _RouterFor:
    """固定返回指定模型配置的假路由。"""

    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:
        return self._config

    def selectFallbackModel(self, configs, excludeId) -> LlmConfig | None:
        return None


class _StubEmbeddingService:
    """embedding 占位：检索返回空、存储 fire-and-forget，不触达真实 API。"""

    async def searchSimilarQueries(self, question: str, *, topK: int, datasourceId: int) -> list:
        return []

    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None


def _installFakes(monkeypatch, config: LlmConfig, *, planJson: str | None = None) -> _PipelineLlm:
    """用 fake 替换 ChatService 的模型路由 / LLM 工厂 / 适配器 / embedding。

    返回 fake LLM 实例，供测试断言其收到的计划 system prompt。
    """
    import app.api.v1.chat as chat_module

    fakeLlm = _PipelineLlm(planJson=planJson)
    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: fakeLlm)
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())
    return fakeLlm


async def _seedTerms(client, terms: list[dict]) -> None:
    """经真实 API 写入术语词典条目。"""
    for t in terms:
        resp = await client.post("/api/v1/term-dictionary", json=t)
        assert resp.status_code == 201, resp.text


def _chat_payload(question: str, datasourceId: int) -> dict:
    return {"sessionId": "s-term-dict", "question": question, "datasourceId": datasourceId}


_QUESTION = "A 物料的含税价是多少，要按价格表号区分，不要平均"


class TestTermDictionaryInject:
    async def test_terms_injected_into_plan_prompt(
        self, client, dbSession, monkeypatch
    ) -> None:
        """词典非空：计划阶段 system prompt 注入 <term_dictionary> 段（含渲染格式）。"""
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _seedTerms(client, TERMS)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        assert "<term_dictionary>" in prompt
        # 渲染格式：- 「term」：definition（类 X、属性 Y、formula_hint）
        assert "「含税价」" in prompt
        assert "类 SupplierPriceDetail" in prompt
        assert "属性 单价" in prompt
        assert "价格表号='T10'" in prompt
        # 地点维度条目与总纲也在词典段内
        assert "「不含税价(地点)」" in prompt
        assert "「供应商报价」" in prompt

    async def test_no_dictionary_when_table_empty(
        self, client, dbSession, monkeypatch
    ) -> None:
        """词典空表：计划 prompt 无 <term_dictionary> 段（控制组）。"""
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        assert "<term_dictionary>" not in prompt

    async def test_plan_referencing_location_alias_passes(
        self, client, dbSession, monkeypatch
    ) -> None:
        """价格条件4 补「地点」别名后：计划引用「地点」（T20 按工厂过滤）通过类作用域校验。

        真实回归 2026-08-17：T20/T21 的取价地点存在 PLICRI3_0（价格条件4），
        但该列无语义、无别名，LLM 写「地点」会在 validatePlan 被拒。补齐业务别名
        ["地点","工厂","场所"] 后，地点维度的 T20/T21 报价查询可正常落地。
        """
        config, ds = await _seed(dbSession)
        locationPlan = (
            '{"target":"查询 C1 工厂 A 物料的含税价（T20）",'
            '"selectedClasses":["SupplierPriceDetail"],'
            '"selectedProperties":["价格表号","单价","地点"],'
            '"conditions":["PLI_0 = \'T20\' AND 地点 = \'C1\'"],'
            '"aggregations":[],'
            '"rowLimit":10}'
        )
        fakeLlm = _installFakes(monkeypatch, config, planJson=locationPlan)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        # 计划确实引用了「地点」，且 schema 渲染已带该别名
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        assert "地点" in prompt

    def test_seed_price_condition4_keeps_location_aliases(self) -> None:
        """锁定种子源：seed_ontology.PROPERTIES 的 价格条件4 必须仍带地点别名。

        本文件其余用例的 _supplierProperties 是手写子集，只证明「validatePlan 接受
        地点 这个别名」，不证明 seed 里还有它。若 seed 的别名被删/改名，本用例立即
        失败，防止「测试用自己的副本通过、真实管线已回归」。
        """
        props = seed_ontology.PROPERTIES["PPRICLIST"]
        pc4 = next(p for p in props if p["name"] == "价格条件4")
        for expected in ("地点", "地点编码", "工厂", "场所", "地点编号"):
            assert expected in pc4["aliases"], f"价格条件4 缺别名 {expected}"
        assert pc4["alias"] == "PLICRI3_0"

    def test_seed_effective_dates_keep_aliases_and_desc(self) -> None:
        """锁定种子源：生效日期/失效日期 必须带业务别名与时间维度 desc（第 2 项）。

        时间维度是供应商报价多档取价的关键约束（当期价须带生效期过滤，不能把
        不同时间段的报价平均）。若 seed 的别名/desc 被删，本用例立即失败。
        """
        props = {p["name"]: p for p in seed_ontology.PROPERTIES["PPRICLIST"]}
        for name, col, expected_alias in (
            ("生效日期", "PLISTRDAT_0", "起始日期"),
            ("失效日期", "PLIENDDAT_0", "截止日期"),
        ):
            prop = props[name]
            assert prop["alias"] == col
            assert expected_alias in prop["aliases"], f"{name} 缺别名 {expected_alias}"
            assert prop.get("desc"), f"{name} 缺 desc"
            assert "查询日" in prop["desc"] or "有效时间" in prop["desc"]

    def test_seed_price_condition_redundants_removed(self) -> None:
        """锁定种子源：价格条件1/5/6 已删除（第 5 项），不得重新加回。

        实证：价格条件1（PLICRI_0）是稀疏 legacy 复合键 `供应商~物料~地点~~~`，
        与 价格条件2/物料编码/价格条件4 重复；价格条件5/6（PLICRI4_0/PLICRI5_0）
        为全空列。留着只会让 LLM 误选，且徒增 schema token。
        """
        names = {p["name"] for p in seed_ontology.PROPERTIES["PPRICLIST"]}
        for gone in ("价格条件1", "价格条件5", "价格条件6"):
            assert gone not in names, f"{gone} 不应存在于 seed，否则 schema 又出现冗余列"
        # 语义化的取价维度列必须仍在
        assert "价格条件2" in names  # 供应商码
        assert "价格条件4" in names  # 地点（含税/不含税地点清单）
