"""P4 知识服务 Facade —— 契约层单测（D32）。

钉住的是**对外契约**，不是实现细节：

- 请求字段的形状与边界（域 / 问题 / 通道 / topK / 钉住 / scope）；
- 通道枚举的**封闭性**（非法通道必须在**请求解析层**被拒，不许漏到编排层）；
- 响应字段的 **camelCase 别名**（前端按 camelCase 取；snake_case 出参 = 前端拿到 undefined）；
- 响应对「发布面是否真的被用到」的**自证**（`release.degraded` + evidence 的 `source`）。

⚠️ 本文件在 `unit/` 下 ⇒ **允许新建**（unit 分片按路径稳定哈希，文件集合变化不影响分片归属）；
`integration/` **禁止新建文件**（分片按「排序下标 mod 4」轮转 ⇒ 新文件会挪动既有文件归属
⇒ 假「净新增失败」）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNELS,
    KnowledgeEntityHint,
    KnowledgeEvidenceRead,
    KnowledgeQueryRequest,
    KnowledgeQueryResponse,
    KnowledgeReleaseRef,
    KnowledgeRouteResultRead,
    KnowledgeScopeRequest,
)

# ---------------------------------------------------------------------------
# 请求形状
# ---------------------------------------------------------------------------


def test_request_defaultsToAllChannelsAndTopK8() -> None:
    """默认行为：通道缺省 = 四路全开；topK 缺省 8。"""
    request = KnowledgeQueryRequest(domain="PROCUREMENT", question="哪些供应商交付延迟最高？")

    assert request.channels is None, "通道缺省必须是 None（编排层据此展开成四路），不是空元组"
    assert request.top_k == 8
    assert request.release_id is None
    assert request.scope is None
    assert request.entity_hints == ()


def test_request_rejectsBlankDomain() -> None:
    """坏输入被拦：空域 / 空白域都不许进编排层。"""
    with pytest.raises(PydanticValidationError):
        KnowledgeQueryRequest(domain="", question="q")
    with pytest.raises(PydanticValidationError):
        KnowledgeQueryRequest(domain="   ", question="q")


def test_request_rejectsEmptyQuestion() -> None:
    """坏输入被拦：空问题。"""
    with pytest.raises(PydanticValidationError):
        KnowledgeQueryRequest(domain="PROCUREMENT", question="")


def test_request_rejectsTooLongQuestion() -> None:
    """坏输入被拦：超长问题（防 prompt 注入面与 token 炸裂）。"""
    with pytest.raises(PydanticValidationError):
        KnowledgeQueryRequest(domain="PROCUREMENT", question="x" * 501)


def test_request_rejectsUnknownChannel() -> None:
    """坏输入被拦：非法通道必须在**请求解析层**被拒。

    漏到编排层的后果：编排层只认四路，第五路会被静默忽略 ⇒ 调用方以为跑了第五路。
    """
    with pytest.raises(PydanticValidationError) as excinfo:
        KnowledgeQueryRequest(
            domain="PROCUREMENT", question="q", channels=("GRAPH", "DOCUMENT")
        )

    message = str(excinfo.value)
    assert "GRAPH" in message and "DOCUMENT" in message, (
        f"报错信息必须点名允许的通道与非法值，实际：{message}"
    )


def test_request_rejectsTopKOutOfRange() -> None:
    """坏输入被拦：topK 必须落在 1..50。"""
    with pytest.raises(PydanticValidationError):
        KnowledgeQueryRequest(domain="PROCUREMENT", question="q", top_k=0)
    with pytest.raises(PydanticValidationError):
        KnowledgeQueryRequest(domain="PROCUREMENT", question="q", top_k=51)


def test_request_rejectsInvalidReleaseId() -> None:
    """坏输入被拦：releaseId 必须为正整数（0 / 负数都不是合法主键）。"""
    with pytest.raises(PydanticValidationError):
        KnowledgeQueryRequest(domain="PROCUREMENT", question="q", release_id=0)
    with pytest.raises(PydanticValidationError):
        KnowledgeQueryRequest(domain="PROCUREMENT", question="q", release_id=-1)


def test_request_acceptsAllFourChannels() -> None:
    """正确输入不被拦：四路枚举全部可传。"""
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT", question="q", channels=KNOWLEDGE_CHANNELS
    )

    assert tuple(request.channels or ()) == KNOWLEDGE_CHANNELS


def test_requestAcceptsEntityHintsAndScope() -> None:
    """正确输入不被拦：实体提示 + 收窄 scope。"""
    request = KnowledgeQueryRequest(
        domain="PROCUREMENT",
        question="q",
        entity_hints=(KnowledgeEntityHint(type="SUPPLIER", code="SUP000001"),),
        scope=KnowledgeScopeRequest(
            org_codes=("procurement",), entity_types=("SUPPLIER",), max_security_level="L2"
        ),
    )

    assert request.entity_hints[0].type == "SUPPLIER"
    assert request.scope is not None
    assert request.scope.max_security_level == "L2"


# ---------------------------------------------------------------------------
# 响应形状
# ---------------------------------------------------------------------------


def test_responseSerializesToCamelCase() -> None:
    """响应字段必须是 camelCase。

    仓内约定（`app/domain/schemas.py`）：DTO 字段名 snake_case、JSON 别名 camelCase。
    出参写成 snake_case ⇒ 前端 `data.routeResults` 拿到 undefined ⇒ 表现为「接口通了但页面空的」。
    """
    response = KnowledgeQueryResponse(
        question="q",
        domain="PROCUREMENT",
        release=KnowledgeReleaseRef(id=7, version="v1.2", manifest_hash="abc", degraded=False),
        answer_draft="草稿",
        evidence=(
            KnowledgeEvidenceRead(
                channel="GRAPH",
                kind="CLASS",
                ref="Supplier",
                title="供应商",
                release_id=7,
                release_version="v1.2",
                source="RELEASE",
            ),
        ),
        route_results=(
            KnowledgeRouteResultRead(
                channel="GRAPH", status="OK", item_count=1, degraded_reason=None, warnings=()
            ),
        ),
    )

    payload = response.model_dump(by_alias=True)

    assert payload["routeResults"][0]["itemCount"] == 1
    assert payload["answerDraft"] == "草稿"
    assert payload["release"]["manifestHash"] == "abc"
    assert payload["release"]["degraded"] is False
    assert payload["evidence"][0]["releaseVersion"] == "v1.2"
    assert payload["evidence"][0]["source"] == "RELEASE"
    assert payload["evidence"][0]["evidenceId"], "M2-1：evidenceId 必须自动生成（调用方逐条引用）"
    assert "item_count" not in payload["routeResults"][0], "snake_case 键不许出现在 JSON 里"


def test_evidenceContractAdditiveFieldsSerializeCamelCase() -> None:
    """M2-1 契约补全（加性可选字段）：sourceRef / security 嵌套模型 camelCase 线格式。

    - 旧字段（channel/kind/ref/title/source）原样 —— 旧调用方零破坏；
    - 新字段全部可选（缺省 None/空）⇒ 既有构造点不必改动；
    - 命名偏差登记：设计稿的嵌套 `source{...}` 改名 `sourceRef`（现仓
      `source: str` 是发布面出处，二者并存）。
    """
    from datetime import UTC, datetime

    from app.domain.knowledge_schemas import (
        KnowledgeEvidenceSecurity,
        KnowledgeEvidenceSourceRef,
    )

    evidence = KnowledgeEvidenceRead(
        channel="SQL",
        kind="QUERY",
        ref="q_01",
        title="供应商收货量",
        evidence_id="ev-1",
        confidence=0.87,
        observed_at=datetime(2026, 11, 20, tzinfo=UTC),
        valid_at=datetime(2026, 11, 20, tzinfo=UTC),
        rule_version="v3",
        security=KnowledgeEvidenceSecurity(level="L2", scope=("PROCUREMENT",)),
        source_ref=KnowledgeEvidenceSourceRef(
            system="THBI",
            object="PRECEIPT",
            record_key="SUP001",
            document_id=None,
            query_id="q_01",
        ),
    )

    payload = evidence.model_dump(by_alias=True)
    assert payload["channel"] == "SQL" and payload["source"] == "RELEASE", "旧字段原样"
    assert payload["evidenceId"] == "ev-1"
    assert payload["confidence"] == 0.87
    assert payload["ruleVersion"] == "v3"
    assert payload["security"] == {"level": "L2", "scope": ("PROCUREMENT",)}
    # I2-B 扩了 8 个 sourceRef 字段（默认 None）⇒ 断言「包含」而非全等
    sourceRef = payload["sourceRef"]
    assert {
        key: sourceRef[key]
        for key in ("system", "object", "recordKey", "documentId", "queryId")
    } == {
        "system": "THBI",
        "object": "PRECEIPT",
        "recordKey": "SUP001",
        "documentId": None,
        "queryId": "q_01",
    }
    assert sourceRef["page"] is None and sourceRef["syncBatch"] is None
    # 缺省构造（既有调用点形态）：新字段全空 ⇒ 不破坏
    minimal = KnowledgeEvidenceRead(
        channel="GRAPH", kind="CLASS", ref="Supplier", title="供应商"
    )
    minimalDump = minimal.model_dump(by_alias=True)
    assert minimalDump["sourceRef"] is None and minimalDump["security"] is None
    assert minimalDump["evidenceId"], "evidenceId 缺省也自动生成"
    assert minimalDump["confidence"] is None


def test_degradedReleaseCarriesNoVersion() -> None:
    """降级读必须**自证**：没有发布版本可引 ⇒ id/version 为空且 degraded=True。

    这条是 G5「引用可回溯到发布版本」的反面守卫：
    降级时若还带一个版本号 ⇒ 调用方会以为这条引用来自某个发布版本 ⇒ 口径骗人。
    """
    response = KnowledgeQueryResponse(
        question="q",
        domain="PROCUREMENT",
        release=KnowledgeReleaseRef(id=None, version=None, manifest_hash=None, degraded=True),
        answer_draft="",
    )

    payload = response.model_dump(by_alias=True)
    assert payload["release"]["id"] is None
    assert payload["release"]["version"] is None
    assert payload["release"]["degraded"] is True


def test_evidenceContractI2BFieldsSerializeCamelCase() -> None:
    """I2-B：`summary` + sourceRef 八字段（page/chunkId/syncBatch/sqlHash/
    paramsSummary/executedAt/period/grain）按 camelCase 序列化，缺省全空不破旧调用方。"""
    from datetime import UTC, datetime

    from app.domain.knowledge_schemas import (
        KnowledgeEvidenceRead,
        KnowledgeEvidenceSourceRef,
    )

    evidence = KnowledgeEvidenceRead(
        channel="SQL",
        kind="CLASS",
        ref="DIM_SUPPLIER",
        title="供应商主数据",
        summary="类 DIM_SUPPLIER 的物理 schema（240 列）",
        source_ref=KnowledgeEvidenceSourceRef(
            object="DIM_SUPPLIER",
            page=3,
            chunk_id="DOC-1#2",
            sync_batch="batch-7",
            sql_hash="ab" * 32,
            params_summary="supplier=D135",
            executed_at=datetime(2026, 11, 21, tzinfo=UTC),
            period="2026-10",
            grain="month",
        ),
    )
    dumped = evidence.model_dump(mode="json", by_alias=True)
    assert dumped["summary"] == "类 DIM_SUPPLIER 的物理 schema（240 列）"
    ref = dumped["sourceRef"]
    assert ref["page"] == 3 and ref["chunkId"] == "DOC-1#2"
    assert ref["syncBatch"] == "batch-7" and ref["sqlHash"] == "ab" * 32
    assert ref["paramsSummary"] == "supplier=D135" and ref["period"] == "2026-10"
    assert ref["grain"] == "month" and ref["executedAt"].startswith("2026-11-21")

    minimal = KnowledgeEvidenceRead(
        channel="GRAPH", kind="CLASS", ref="X", title="X",
    ).model_dump(mode="json", by_alias=True)
    assert minimal["summary"] is None and minimal["sourceRef"] is None
