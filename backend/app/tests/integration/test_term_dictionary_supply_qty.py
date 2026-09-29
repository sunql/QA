"""集成测试：「供货量」术语词典条目注入 NL2SQL 计划 prompt（生产型物料口径）。

用户场景（2026-09-21）：问「3月份供货量最多的三家供应商」时，LLM 偶发把外协
服务商（C079，做电镀）算入供货量 Top3，返回 B125/B019/C079；正确口径应排除
外协、限定生产型物料（TCLCOD_0 IN 'A02','A03','A04','A05'），返回
B125/B019/B153。结果不一致的根因不是幻觉/温度漂移，而是 LLM 缺少业务语义
约束——它不知道"供货量"在你们语境里特指生产型物料。

修复（按用户选择：最小改动）：把口径写入术语词典，计划阶段以 <term_dictionary>
块注入 prompt。词典条目包含：
- term = 「供货量」
- mapped_class_name = DWD_GOODS_RECEIPT_LINE
- mapped_property_name = RECEIVED_QTY
- formula_hint 显式限定 DIM_IMATERIAL.TCLCOD_0 IN ('A02','A03','A04','A05')

本测试锁定整条注入链（术语 → renderDictionaryText → 计划 system prompt）：
- 词典非空时，计划阶段 system prompt 出现「供货量」术语段（渲染格式含
  「term」：definition（类 X、属性 Y、hint））；
- 词典空表时，计划 prompt 无 <term_dictionary> 段（控制组，防误注入）；
- 关联术语「外协供应商」「生产型物料」也应在 prompt 中出现（同次注入）；
- formula_hint 必须引用正确的本体类 DIM_IMATERIAL（不是错的 ITMMASTER），
  防止后续 rebind 误改后 LLM 找不到类；
- formula_hint 必须包含 TCLCOD_0 IN ('A02','A03','A04','A05') 过滤条件。

说明：与 test_term_dictionary_inject.py 同模式——模型路由 / LLM / 适配器 /
embedding 均以 fake 注入；术语经 POST /api/v1/term-dictionary 写入，走真实 DB。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.models import (
    DataSource,
    LlmConfig,
    OntologyClass,
    OntologyProperty,
)
from app.infrastructure.security.crypto import encryptApiKey

pytestmark = pytest.mark.asyncio


# 与 dev 环境实际写入的 3 条术语一致；id 由 DB 自增，测试断言按 term 内容
SUPPLY_QTY_TERMS: list[dict] = [
    {
        "term": "供货量",
        "definition": (
            "生产型物料的供应数量(默认排除外协/委外服务,如电镀等委外加工)。"
            "统计时默认限定 DIM_IMATERIAL.TCLCOD_0 IN "
            "('A02','A03','A04','A05'),即生产型物料类别。"
            "外协服务商(如 C079 做电镀)的服务量默认不算供货量。"
            "【口径例外】以下场景不套用生产型物料过滤:"
            "(1) 用户明确说明「零星物料」「低值易耗」;"
            "(2) 用户明确指定了具体供应商编码或名称"
            "(已知供应商时不限制其物料范围)。"
        ),
        "mappedClassName": "DWD_GOODS_RECEIPT_LINE",
        "mappedPropertyName": "RECEIVED_QTY",
        "formulaHint": (
            "默认 JOIN DIM_IMATERIAL ON DIM_IMATERIAL.ITMREF_0 = "
            "DWD_GOODS_RECEIPT_LINE.MATERIAL_CODE "
            "WHERE DIM_IMATERIAL.TCLCOD_0 IN "
            "('A02','A03','A04','A05') "
            "AND DWD_GOODS_RECEIPT_LINE.RECEIPT_DATE 在统计月份范围内。"
            "【例外】用户已明确指定供应商编码列表时,不应用 TCLCOD_0 过滤。"
        ),
    },
    {
        "term": "外协供应商",
        "definition": (
            "提供外协加工服务的供应商(如电镀、热处理、表面处理等)。"
            "这类供应商的供应量不属于「供货量」,统计供货量时必须排除。"
            "外协供应商的特征:提供的物料类别不在生产型物料范围 A02-A05 内。"
        ),
        "mappedClassName": "DIM_SUPPLIER",
        "mappedPropertyName": "BPSNUM_0",
        "formulaHint": (
            "外协供应商的供货量不计入生产供货量排名;"
            "如果统计中出现外协供应商,需检查是否漏掉 "
            "TCLCOD_0 IN ('A02','A03','A04','A05') 过滤"
        ),
    },
    {
        "term": "生产型物料",
        "definition": (
            "用于生产的物料类别,物料类型代码 TCLCOD_0 IN "
            "('A02','A03','A04','A05')。"
            "这是供货量统计必须限定的物料范围。"
            "TCLCOD_0 取其他值的物料(如外协服务、办公用品、备件等)"
            "不属于生产型物料,不计入生产供货量。"
        ),
        "mappedClassName": "DIM_IMATERIAL",
        "mappedPropertyName": "TCLCOD_0",
        "formulaHint": (
            "生产型物料过滤条件:DIM_IMATERIAL.TCLCOD_0 IN "
            "('A02','A03','A04','A05')"
        ),
    },
]


_DEFAULT_PLAN = (
    '{"target":"查询 3 月份供货量最多的三家供应商",'
    '"selectedClasses":["DWD_GOODS_RECEIPT_LINE","DIM_IMATERIAL"],'
    '"selectedProperties":["SUPPLIER_CODE","RECEIVED_QTY","TCLCOD_0"],'
    '"conditions":["TCLCOD_0 IN ('"'"'A02'"'"','"'"'A03'"'"','"'"'A04'"'"','"'"'A05'"'"')",'
    '"RECEIPT_DATE >= '"'"'2026-03-01'"'"'","RECEIPT_DATE < '"'"'2026-04-01'"'"'"],'
    '"aggregations":[{"alias":"total_qty","function":"SUM","property":"RECEIVED_QTY"}],'
    '"groupBy":["SUPPLIER_CODE"],'
    '"orderBy":[{"column":"total_qty","direction":"DESC"}],'
    '"rowLimit":3}'
)


class _PipelineLlm:
    """按 prompt 内容路由回复：计划 / SQL / 回答；记录每次调用消息。

    计划阶段返回注入的计划（按供货量 Top3 场景，默认限定生产型物料），
    保证完整链路推进到 SQL 阶段。
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
                "```sql\nSELECT SUPPLIER_CODE, SUM(RECEIVED_QTY) AS total_qty "
                "FROM ZJTH.DWD_GOODS_RECEIPT_LINE r "
                "JOIN ZJTH.DIM_IMATERIAL m ON m.ITMREF_0 = r.MATERIAL_CODE "
                "WHERE m.TCLCOD_0 IN ('A02','A03','A04','A05') "
                "AND r.RECEIPT_DATE >= DATE '2026-03-01' "
                "AND r.RECEIPT_DATE < DATE '2026-04-01' "
                "GROUP BY SUPPLIER_CODE "
                "ORDER BY total_qty DESC "
                "FETCH FIRST 3 ROWS ONLY\n```"
            )
        else:
            _Resp.content = "查询完成,3 月份供货量最多的三家供应商是 B125、B019、B153。"
        return _Resp()


class _FakeAdapter:
    async def execute_read_only(self, sql: str) -> list[dict]:
        return [
            {"SUPPLIER_CODE": "B125", "total_qty": Decimal("1234.50")},
            {"SUPPLIER_CODE": "B019", "total_qty": Decimal("1100.25")},
            {"SUPPLIER_CODE": "B153", "total_qty": Decimal("987.00")},
        ]


def _receiptLineProperties() -> list[OntologyProperty]:
    """与 seed_ontology 真实管线一致的 DWD_GOODS_RECEIPT_LINE 子集。

    每次调用返回全新实例：OntologyProperty 是 ORM 实例,跨测试复用会被
    上一测试会话标记持久化(class_id 已赋值),重跑时 flush 因 identity map
    冲突静默跳过属性写入,导致类存在但属性为空(真实回归 2026-08-17)。
    """
    return [
        OntologyProperty(
            property_name="收货单号", data_type="STRING", source_column="RECEIPT_NO"
        ),
        OntologyProperty(
            property_name="收货行号", data_type="STRING", source_column="RECEIPT_LINE_NO"
        ),
        OntologyProperty(
            property_name="工厂代码", data_type="STRING", source_column="FACILITY_CODE"
        ),
        OntologyProperty(
            property_name="收货日期", data_type="DATETIME", source_column="RECEIPT_DATE"
        ),
        OntologyProperty(
            property_name="供应商代码", data_type="STRING", source_column="SUPPLIER_CODE"
        ),
        OntologyProperty(
            property_name="物料编码", data_type="STRING", source_column="MATERIAL_CODE"
        ),
        OntologyProperty(
            property_name="收货数量",
            data_type="DECIMAL",
            source_column="RECEIVED_QTY",
        ),
    ]


def _imaterialProperties() -> list[OntologyProperty]:
    """DIM_IMATERIAL 子集：ITMREF_0 + TCLCOD_0(物料类型代码)。"""
    return [
        OntologyProperty(
            property_name="物料编号", data_type="STRING", source_column="ITMREF_0", is_primary_key=True
        ),
        OntologyProperty(
            property_name="物料类型代码", data_type="STRING", source_column="TCLCOD_0"
        ),
    ]


async def _seed(session) -> tuple[LlmConfig, DataSource]:
    """写入模型配置、数据源、DWD_GOODS_RECEIPT_LINE + DIM_IMATERIAL 本体类。"""
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
    receipt = OntologyClass(
        class_name="DWD_GOODS_RECEIPT_LINE",
        class_alias="收货明细",
        source_table="ZJTH.DWD_GOODS_RECEIPT_LINE",
        properties=_receiptLineProperties(),
    )
    imaterial = OntologyClass(
        class_name="DIM_IMATERIAL",
        class_alias="物料主数据",
        source_table="ZJTH.DIM_IMATERIAL",
        properties=_imaterialProperties(),
    )
    session.add_all([config, ds, receipt, imaterial])
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
    """embedding 占位:检索返回空、存储 fire-and-forget,不触达真实 API。"""

    async def searchSimilarQueries(self, question: str, *, topK: int, datasourceId: int) -> list:
        return []

    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None


def _installFakes(monkeypatch, config: LlmConfig, *, planJson: str | None = None) -> _PipelineLlm:
    """用 fake 替换 ChatService 的模型路由 / LLM 工厂 / 适配器 / embedding。

    返回 fake LLM 实例,供测试断言其收到的计划 system prompt。
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


async def _clearTerms(client) -> None:
    """清理术语词典：测试间隔离,确保控制组生效。"""
    resp = await client.get("/api/v1/term-dictionary")
    assert resp.status_code == 200
    for term in resp.json():
        delResp = await client.delete(f"/api/v1/term-dictionary/{term['id']}")
        assert delResp.status_code == 204, delResp.text


def _chat_payload(question: str, datasourceId: int) -> dict:
    return {"sessionId": "s-supply-qty", "question": question, "datasourceId": datasourceId}


_QUESTION = "3 月份供货量最多的三家供应商是哪些"


class TestTermDictionarySupplyQtyInject:
    """「供货量」术语词典条目注入链回归测试。

    锁定 nl2sql_service._renderFewShotPart → renderDictionaryText →
    plan stage system prompt 的完整注入链。词典被误删/误改会立即挂测试。
    """

    async def test_supply_qty_term_injected_into_plan_prompt(
        self, client, dbSession, monkeypatch
    ) -> None:
        """词典非空:计划阶段 system prompt 注入 <term_dictionary> 段,含「供货量」渲染。"""
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _clearTerms(client)
        await _seedTerms(client, SUPPLY_QTY_TERMS)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        # 注入段标记
        assert "<term_dictionary>" in prompt
        # 渲染格式:- 「term」:definition(类 X、属性 Y、hint)
        assert "「供货量」" in prompt
        assert "类 DWD_GOODS_RECEIPT_LINE" in prompt
        assert "属性 RECEIVED_QTY" in prompt
        # formulaHint 关键过滤条件必须在 prompt 中(防 LLM 漏过滤)
        assert "TCLCOD_0 IN ('A02','A03','A04','A05')" in prompt
        # 关联术语(外协供应商 / 生产型物料)同次注入
        assert "「外协供应商」" in prompt
        assert "「生产型物料」" in prompt

    async def test_no_dictionary_when_table_empty(
        self, client, dbSession, monkeypatch
    ) -> None:
        """词典空表:计划 prompt 无 <term_dictionary> 段(控制组,防误注入)。"""
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _clearTerms(client)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        assert "<term_dictionary>" not in prompt
        # 反向断言:无词典时,口径过滤条件不应被「硬塞」进 prompt
        assert "TCLCOD_0 IN ('A02','A03','A04','A05')" not in prompt

    async def test_formula_hint_uses_correct_class_name(
        self, client, dbSession, monkeypatch
    ) -> None:
        """formulaHint 必须引用 DIM_IMATERIAL(不是错的 ITMMASTER)。

        真实回归 2026-09-21:首次写入时 formula_hint 用了「ITMMASTER.TCLCOD_0」,
        但本体中并无 ITMMASTER 类(实际类名是 DIM_IMATERIAL)。LLM 看到错误的
        类名会在 validatePlan 阶段被拒,口径过滤失效。本用例锁定:
        - prompt 中不能出现 ITMMASTER(防再次误写)
        - 必须出现 DIM_IMATERIAL(类名拼写正确)
        """
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _clearTerms(client)
        await _seedTerms(client, SUPPLY_QTY_TERMS)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        # 锁:不要回到错误的 ITMMASTER 类名
        assert "ITMMASTER" not in prompt
        # 锁:必须使用正确的 DIM_IMATERIAL
        assert "DIM_IMATERIAL" in prompt
        # 锁:JOIN 表达式必须出现(否则 LLM 无法落地过滤)
        assert "JOIN DIM_IMATERIAL" in prompt
        assert "MATERIAL_CODE" in prompt

    async def test_supply_qty_term_not_outsourced_service(
        self, client, dbSession, monkeypatch
    ) -> None:
        """「供货量」定义必须包含「外协/委外」排除语义。

        如果定义被改成「所有物料的供应量」,LLM 就会把 C079(电镀外协)
        算入 Top3。本用例锁定定义文本含排除关键词,防回归。
        """
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _clearTerms(client)
        await _seedTerms(client, SUPPLY_QTY_TERMS)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        # 「供货量」条目定义里必须出现外协/委外等排除关键词
        # 取「供货量」条目段单独校验(避免被其他 term 干扰)
        supplyQtyStart = prompt.index("「供货量」")
        nextTermStart = prompt.find("「", supplyQtyStart + 1)
        # 同一 term 的字段都在下一个「 之前
        supplyQtyBlock = prompt[supplyQtyStart:nextTermStart if nextTermStart > 0 else None]
        assert "外协" in supplyQtyBlock or "委外" in supplyQtyBlock, (
            f"「供货量」定义缺失外协/委外排除语义:{supplyQtyBlock!r}"
        )

    async def test_supply_qty_term_contains_explicit_supplier_exception(
        self, client, dbSession, monkeypatch
    ) -> None:
        """「供货量」定义必须包含「明确指定供应商」例外（2026-09-21 修复）。

        真实回归：用户问「B019、B125、D1 这三家供应商的供货情况」(明确指定
        了 3 个供应商编码),LLM 不该套用 DIM_IMATERIAL.TCLCOD_0 IN
        ('A02','A03','A04','A05') 过滤。本用例锁定定义文本含「例外」语义,
        防回归到「必须限定」的硬口径表述。

        实现注意：term 内部可能嵌套「」（如「零星物料」），所以直接用
        ``in prompt`` 在全 prompt 上断言即可——既锁定「供货量」条目携带
        例外语义，又不会被邻近 term 截断干扰。
        """
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _clearTerms(client)
        await _seedTerms(client, SUPPLY_QTY_TERMS)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        # 「供货量」条目必须出现且其后跟随「例外」语义
        assert "「供货量」" in prompt
        # 例外语义必须在 prompt 中出现,且明确提及「供应商」(防回归到仅
        # 写「零星物料」单一例外的旧版本)
        assert "例外" in prompt
        # 锁定「明确指定了具体供应商编码或名称」表述(用户业务规则原文)
        assert "明确指定了具体供应商" in prompt, (
            f"「供货量」例外应明确提及「指定具体供应商」作为条件:{prompt[:1500]!r}"
        )
        # 锁定 TCLCOD_0 过滤条件也出现在 formula_hint(供 LLM 直接抄)
        assert "TCLCOD_0 IN" in prompt


# 2026-09-21 第二轮修复:同 TCLCOD_0 模式,补 INTER_COM/SITE 的「明确供应商」例外
# 用户场景:问「B019、B125、D1 这三家供应商的供货情况」,LLM 把
# INTER_COM_CODE=1(外部供应商)当默认过滤,导致已锁定供应商的数据仍被截掉。
_EXTERNAL_TRADE_TERM: dict = {
    "term": "外部供应商贸易",
    "definition": (
        "供应商与外部公司间的贸易(默认排除内部公司间交易)。"
        "默认统计时,只取 INTER_COM_CODE=1 且 INTER_SITE_CODE=1 的数据,"
        "即只算外部供应商贸易,排除内部公司交易/内部调拨。"
        "【口径例外】以下场景不套用外部过滤:"
        "(1) 用户明确说明「公司内部交易」「内部贸易」「内部调拨」;"
        "(2) 用户明确指定了具体供应商编码或名称"
        "(已知供应商时不限制内部/外部范围)。"
    ),
    "mappedClassName": "DWD_GOODS_RECEIPT_LINE",
    "mappedPropertyName": "INTER_COM_CODE",
    "formulaHint": (
        "默认 WHERE INTER_COM_CODE = '1' AND INTER_SITE_CODE = '1' "
        "(外部公司间贸易)。"
        "【例外】用户已明确指定供应商编码/名称,或者明确声明统计内部交易时,"
        "不应用 INTER_COM_CODE=1 过滤(保留全部交易)。"
    ),
}


class TestExternalTradeExplicitSupplierException:
    """「外部供应商贸易」术语注入 + 明确供应商例外锁定（2026-09-21）。

    用户业务规则:
    - 默认:外部供应商贸易 (INTER_COM_CODE=1 且 INTER_SITE_CODE=1)
    - 例外:用户明确指定具体供应商编码/名称,或明确声明统计内部交易

    与 TCLCOD_0 同模式修复:TCLCOD_0 之前只写了"声明内部交易"单层例外,
    缺「明确指定供应商」例外。本测试锁定词典条目 wording,防回归到单层。
    """

    async def test_external_trade_term_injected_with_exception(
        self, client, dbSession, monkeypatch
    ) -> None:
        """「外部供应商贸易」条目注入后,prompt 必须携带双层例外语义。"""
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _clearTerms(client)
        await _seedTerms(client, [_EXTERNAL_TRADE_TERM])

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        # 注入段标记
        assert "<term_dictionary>" in prompt
        # term 标题必须出现
        assert "「外部供应商贸易」" in prompt
        # 映射类与属性标签必须出现（LLM 据此找到对应过滤条件）
        assert "类 DWD_GOODS_RECEIPT_LINE" in prompt
        assert "属性 INTER_COM_CODE" in prompt
        # formula_hint 必须含默认过滤 + 例外条款
        assert "INTER_COM_CODE = '1'" in prompt
        assert "INTER_SITE_CODE = '1'" in prompt

    async def test_external_trade_term_contains_explicit_supplier_exception(
        self, client, dbSession, monkeypatch
    ) -> None:
        """「外部供应商贸易」条目必须含「明确指定供应商」例外条款。

        真实回归（2026-09-21）:用户问「B019/B125/D1 供货情况」(明确指定
        3 个供应商),LLM 仍套用 INTER_COM_CODE=1 过滤,致查询为空。
        本用例锁定 definition 与 formula_hint 同时含双层例外:
        - 「声明内部交易」(原有)
        - 「明确指定具体供应商」(本次补)
        """
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _clearTerms(client)
        await _seedTerms(client, [_EXTERNAL_TRADE_TERM])

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        # term 必须出现
        assert "「外部供应商贸易」" in prompt
        # 双层例外必须同时出现
        assert "例外" in prompt
        assert "公司内部交易" in prompt or "内部贸易" in prompt, (
            "应保留「声明内部交易」作为第一层例外"
        )
        assert "明确指定了具体供应商" in prompt, (
            f"「外部供应商贸易」应明确提及「指定具体供应商」作为第二层例外:"
            f"{prompt[:1500]!r}"
        )

    async def test_external_trade_term_distinct_from_supply_qty(
        self, client, dbSession, monkeypatch
    ) -> None:
        """「外部供应商贸易」与「供货量」必须作为独立条目同时注入。

        两个口径都受「明确指定供应商」例外影响,但作用维度不同:
        - 供货量 → 物料维度 (TCLCOD_0)
        - 外部供应商贸易 → 供应商维度 (INTER_COM_CODE)

        必须分别建条目,不能合并（LLM 在 plan 阶段会按映射属性找规则）。
        """
        config, ds = await _seed(dbSession)
        fakeLlm = _installFakes(monkeypatch, config)
        await _clearTerms(client)
        # 同时写入供货量 + 外部供应商贸易
        await _seedTerms(client, SUPPLY_QTY_TERMS + [_EXTERNAL_TRADE_TERM])

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200, resp.text
        prompt = fakeLlm.planSystemPrompt
        assert prompt is not None
        # 两个 term 必须同时出现
        assert "「供货量」" in prompt
        assert "「外部供应商贸易」" in prompt
        # 各自过滤条件都必须在 prompt
        assert "TCLCOD_0 IN" in prompt
        assert "INTER_COM_CODE = '1'" in prompt