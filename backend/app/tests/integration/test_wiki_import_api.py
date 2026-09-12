"""Wiki 知识导入 REST 端点集成测试（feat-wiki-knowledge M2）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。
LLM 走 patch 注入假客户端——测试不联外网，但**保留真实调用链**
（ModelConfigService 查配置 → createClient → complete → 计量落库）。

覆盖：选模可用性标注、Markdown 切分、执行落库、机制 1 分类、
fallback 降级、分类失败不丢知识、重复 pageId 的 PARTIAL、计量台账、认证。
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import wiki_import
from app.domain.exceptions import LlmClientError
from app.domain.models import LlmConfig
from app.domain.wiki_learning_models import WikiImportTask, WikiTokenUsage
from app.domain.wiki_models import WikiPage
from app.domain.wiki_schemas import (
    MAX_CONTENT_CHARS,
    MAX_IMPORT_DRAFTS,
    MAX_SOURCE_CHARS,
)
from app.infrastructure.llm.base_client import LlmResponse
from app.services.messages_zh import MSG_WIKI_IMPORT_FILE_PARSE_FAILED
from app.services.wiki_import_service import WikiImportService

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/import"
_PAGES = "/api/v1/wiki/pages"

# admin 头：用户名不命中 DB 用户时回退 stub 默认（含 admin 角色）
_ADMIN_HEADERS = {"X-User-Id": "wiki-test-admin", "X-User-Roles": "admin"}

# patch 目标：invoker 模块级导入的 createClient
_INVOKER_CLIENT = "app.services.learning.llm_invoker.createClient"
# patch 目标：import service 模块级导入的 createClient（选模可用性）
_SERVICE_CLIENT = "app.services.wiki_import_service.createClient"

_CLASSIFY_JSON = json.dumps(
    {
        "primary": "RULE",
        "confidence": 0.92,
        "alternatives": ["POLICY"],
        "reason": "含准入门槛阈值",
    }
)


class _FakeLlmClient:
    """假 LLM 客户端：记录调用次数，返回可配置内容（模拟真实 BaseLlmClient）。

    失败时抛 ``LlmClientError`` 而非裸 ``RuntimeError`` —— 真实客户端
    （openai_client / ollama_client）把网络、超时、解析异常一律包成
    ``LlmClientError``。假客户端若抛裸异常，测的就不是生产会走的那条路。
    """

    def __init__(self, content: str = _CLASSIFY_JSON, *, raiseError: bool = False) -> None:
        self._content = content
        self._raiseError = raiseError
        self.calls = 0

    async def complete(self, messages, **kwargs) -> LlmResponse:
        self.calls += 1
        if self._raiseError:
            raise LlmClientError("LLM 服务不可用")
        return LlmResponse(
            content=self._content,
            modelName="fake-model",
            promptTokens=100,
            completionTokens=50,
            totalTokens=150,
        )


async def _seedModel(
    dbSession: AsyncSession,
    *,
    modelName: str = "wiki-test-model",
    isActive: bool = True,
    costPer1kInput: str = "0.001",
    costPer1kOutput: str = "0.002",
) -> int:
    """插入一条 llm_config，返回其 id。"""
    config = LlmConfig(
        model_name=modelName,
        provider="openai_compatible_proxy",
        is_active=isActive,
        cost_per_1k_input=Decimal(costPer1kInput),
        cost_per_1k_output=Decimal(costPer1kOutput),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config.id


def _drafts(*items: tuple[str, str]) -> list[dict]:
    return [{"title": t, "content": c} for t, c in items]


# ---------------------------------------------------------------------------
# 选模
# ---------------------------------------------------------------------------


async def test_list_models_marks_usable_and_unusable(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """usable 反映「能否真正调用」：无凭据的模型标记为不可用但仍在列表里。"""
    # Arrange
    await _seedModel(dbSession, modelName="can-call")
    await _seedModel(dbSession, modelName="no-credential")

    def _factory(config):
        return _FakeLlmClient() if config.model_name == "can-call" else None

    # Act
    with patch(_SERVICE_CLIENT, side_effect=_factory):
        resp = await client.get(f"{_BASE}/models")

    # Assert
    assert resp.status_code == 200
    byName = {m["modelName"]: m for m in resp.json()}
    assert byName["can-call"]["usable"] is True
    assert byName["no-credential"]["usable"] is False
    # 不可用的也返回，向导才能解释「为什么不能选」
    assert byName["no-credential"]["isActive"] is True


async def test_list_models_inactive_marked_unusable(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _seedModel(dbSession, modelName="disabled", isActive=False)
    with patch(_SERVICE_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.get(f"{_BASE}/models")
    assert resp.json()[0]["usable"] is False


# ---------------------------------------------------------------------------
# 预检（切分）
# ---------------------------------------------------------------------------


async def test_preview_splits_markdown_by_shallowest_heading(
    client: AsyncClient,
) -> None:
    """按最浅标题层级切分；`###` 子标题不参与切分。"""
    source = (
        "## 供应商准入规则\n\n注册资本 >= 1000 万。\n\n"
        "### 例外\n\n战备物资可豁免。\n\n"
        "## 供应商分级规则\n\n按年度采购额分 A/B/C 级。\n"
    )
    resp = await client.post(f"{_BASE}/preview", json={"source": source})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [d["title"] for d in body["drafts"]] == ["供应商准入规则", "供应商分级规则"]
    # 子标题内容归属其父条目，而不是被切成第三篇
    assert "战备物资可豁免" in body["drafts"][0]["content"]


async def test_preview_keeps_intro_before_first_heading(
    client: AsyncClient,
) -> None:
    """首个标题之前的引言归入第一篇，不能被静默丢弃。

    引言常是文档摘要/适用范围，正文价值不比标题段低；切分逻辑若按
    ``source[start:end]`` 直接切，``source[0:start]`` 会人间蒸发。
    """
    source = (
        "本文件规定供应商准入的总体要求与适用范围。\n\n"
        "## 准入规则\n\n注册资本 >= 1000 万。\n\n"
        "## 分级规则\n\n按年度采购额分 A/B/C 级。\n"
    )
    resp = await client.post(f"{_BASE}/preview", json={"source": source})

    body = resp.json()
    assert body["total"] == 2
    assert "适用范围" in body["drafts"][0]["content"]
    # 只进第一篇，不能污染后续条目
    assert "适用范围" not in body["drafts"][1]["content"]


async def test_preview_single_heading_yields_one_draft(client: AsyncClient) -> None:
    """只有一层标题且唯一 → 整篇一条，不切成碎片。"""
    resp = await client.post(
        f"{_BASE}/preview", json={"source": "# 采购管理办法\n\n正文内容。"}
    )
    assert resp.json()["total"] == 1


async def test_preview_without_heading_yields_one_draft(client: AsyncClient) -> None:
    """无标题 → 整篇一条，标题取首个非空行。"""
    resp = await client.post(
        f"{_BASE}/preview", json={"source": "供应商台账说明\n\n这里是正文。"}
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["drafts"][0]["title"] == "供应商台账说明"


async def test_preview_blank_source_returns_422(client: AsyncClient) -> None:
    resp = await client.post(f"{_BASE}/preview", json={"source": "   \n  "})
    assert resp.status_code == 422


async def test_preview_does_not_touch_database(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """预检是纯解析：不建 Page、不建任务。"""
    await client.post(f"{_BASE}/preview", json={"source": "## 甲\n\n内容"})

    assert (await dbSession.execute(select(WikiPage))).scalars().all() == []
    assert (await dbSession.execute(select(WikiImportTask))).scalars().all() == []


async def test_preview_oversized_source_returns_422(client: AsyncClient) -> None:
    """原始素材超上限 → 422（在边界挡住无上限请求体，而不是让它进解析器）。"""
    oversized = "x" * (MAX_SOURCE_CHARS + 1)
    resp = await client.post(f"{_BASE}/preview", json={"source": oversized})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 预检（文件上传）
# ---------------------------------------------------------------------------

# 一份已知有效的最小 PDF（单页、无内容）。用它而不是 reportlab 生成：reportlab
# 并非测试依赖，只是碰巧在环境里，靠它会变成一颗定时炸弹。
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>\nendobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>\nendobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>\nendobj\n"
    b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n0000000058 00000 n\n0000000115 00000 n\n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n194\n%%EOF"
)


def _makeDocxBytes(text: str) -> bytes:
    """用 python-docx 造一个真 DOCX（而不是手搓 zip）——解析链测的是生产路径。"""
    import io

    from docx import Document

    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph(text)
    doc.save(buf)
    return buf.getvalue()


async def test_preview_file_txt_returns_text_and_markdown_type(
    client: AsyncClient,
) -> None:
    """上传 .txt → 纯文本 + 来源类型 MARKDOWN。"""
    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("规则.txt", "## 准入规则\n\n注册资本 >= 1000 万。", "text/plain")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "注册资本 >= 1000 万。" in body["text"]
    assert body["sourceType"] == "MARKDOWN"


async def test_preview_file_md_returns_markdown_type(client: AsyncClient) -> None:
    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("policy.md", "## 甲\n\n正文", "text/markdown")},
    )
    assert resp.status_code == 200
    assert resp.json()["sourceType"] == "MARKDOWN"


async def test_preview_file_docx_extracts_paragraphs(client: AsyncClient) -> None:
    """上传 .docx → 抽出段落文本 + 来源类型 WORD。"""
    resp = await client.post(
        f"{_BASE}/preview-file",
        files={
            "file": (
                "制度.docx",
                _makeDocxBytes("供应商准入需注册资本不少于一千万元。"),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "供应商准入需注册资本不少于一千万元。" in body["text"]
    assert body["sourceType"] == "WORD"


async def test_preview_file_pdf_returns_pdf_type(client: AsyncClient) -> None:
    """上传 .pdf → 200 + 来源类型 PDF。

    这里只断言「解析链路通了、类型判对了」，不断言文本内容：这份最小 PDF
    本身没有文本层。文本抽取的正确性由 ``app/tests/unit/test_document_parser.py``
    在解析器层面覆盖，此处重复断言只会把两个套件绑在一起。
    """
    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("制度.pdf", _MINIMAL_PDF, "application/pdf")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["text"], str)
    assert body["sourceType"] == "PDF"


async def test_preview_file_legacy_doc_returns_unsupported_not_parse_failure(
    client: AsyncClient,
) -> None:
    """老式二进制 .doc → 422 且原因是「不支持的类型」，不是「解析失败」。

    回归点：``parse_document`` 曾把 ``ext == "doc"`` 一并交给 python-docx，
    而后者读不了 .doc（抛 ``Package not found``）。

    这里**必须断言 detail 文案**，只断言 422 是假的守卫：修与不修都是 422，
    差别只在「为什么」——前者误导用户去查文件是否损坏，后者告诉他换格式。
    """
    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("老制度.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/msword")},
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "不支持的文件类型" in detail
    assert "老制度.doc" in detail  # 回显文件名，用户才对得上自己选的文件
    assert MSG_WIKI_IMPORT_FILE_PARSE_FAILED not in detail


async def test_preview_file_unsupported_type_returns_422(client: AsyncClient) -> None:
    """csv 不在支持范围（用户已拍板本次只做 PDF/Word/文本）→ 422。"""
    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("台账.csv", "供应商,等级\n甲,A", "text/csv")},
    )
    assert resp.status_code == 422

    # 与「解析失败」分开：csv 的用户动作是换格式，不是去查文件是否损坏。
    assert "不支持的文件类型" in resp.json()["detail"]


async def test_preview_file_corrupt_returns_neutral_message_and_logs_detail(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """损坏文件 → 422 给**可行动**的通用文案，底层异常只进服务端日志。

    泄漏面：``pypdf`` 的异常消息里带着库内部状态/临时路径，而前端根本不展示
    ``detail``（它有自己的 i18n 文案，且拦截器只 toast 后端原文）。所以回显
    原文是纯泄露——没有用户受益，只有攻击面。详细原因必须留在日志里
    （否则就是另一个极端：静默吞错，线上无从排查）。
    """
    with caplog.at_level(logging.ERROR):
        resp = await client.post(
            f"{_BASE}/preview-file",
            files={"file": ("坏文件.pdf", b"definitely not a pdf", "application/pdf")},
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == MSG_WIKI_IMPORT_FILE_PARSE_FAILED
    assert "PDF parsing failed" not in resp.json()["detail"]
    # 反过来：服务端日志里**要**有底层原因，否则变成静默吞错。
    assert "知识导入文件解析失败" in caplog.text
    assert "坏文件.pdf" in caplog.text


async def test_preview_file_oversized_returns_413(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过字节上限 → 413，且**在读进解析器之前**就被挡住。

    把上限压到 2 MB 而不是真造 10 MB+：要守的是「边界存在且生效」，
    不是「常量恰好等于 10」。压过头会让上限 < 1 MB 时文案里的 MB 数变成 0，
    所以取 2 MB 这个仍能整除出整数 MB 的值。
    """
    monkeypatch.setattr(wiki_import, "MAX_UPLOAD_BYTES", 2 * 1024 * 1024)

    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("大制度.txt", b"x" * (2 * 1024 * 1024 + 1), "text/plain")},
    )

    assert resp.status_code == 413
    assert "文件过大" in resp.json()["detail"]


async def test_preview_file_at_exact_limit_is_accepted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恰好等于上限 → 放行（`>` 而不是 `>=`）。

    边界测试的另一半：只测「超了被拒」的话，把判断写成 `>=` 也能通过，
    而那样用户上传一个正好卡在上限的文件会被莫名拒绝。
    """
    monkeypatch.setattr(wiki_import, "MAX_UPLOAD_BYTES", 2 * 1024 * 1024)

    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("正好.txt", b"x" * (2 * 1024 * 1024), "text/plain")},
    )

    assert resp.status_code == 200


async def test_preview_file_does_not_touch_database(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """文件解析是纯解析：不建 Page、不建任务、不记计量。"""
    await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("a.txt", "## 甲\n\n内容", "text/plain")},
    )

    assert (await dbSession.execute(select(WikiPage))).scalars().all() == []
    assert (await dbSession.execute(select(WikiImportTask))).scalars().all() == []
    assert (await dbSession.execute(select(WikiTokenUsage))).scalars().all() == []


async def test_preview_file_rejects_when_stub_auth_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉 stub auth（模拟生产）→ 未认证上传必须 403。

    解析是 CPU 密集的，暴露给未认证客户端等于送一个免鉴权的放大器。
    """
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")

    resp = await client.post(
        f"{_BASE}/preview-file",
        files={"file": ("a.txt", "内容", "text/plain")},
    )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 执行：基本落库 + 分类
# ---------------------------------------------------------------------------


async def test_execute_creates_pages_with_classification(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """执行导入 → 建 Page（带分类建议）+ 任务台账 + LLM 计量。"""
    # Arrange
    modelId = await _seedModel(dbSession)
    fake = _FakeLlmClient(_CLASSIFY_JSON)

    # Act
    with patch(_INVOKER_CLIENT, return_value=fake):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": _drafts(("供应商准入规则", "注册资本 >= 1000 万")),
                "modelId": modelId,
                "sourceType": "MARKDOWN",
                "sourceRef": "policy.md",
            },
        )

    # Assert：任务
    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "SUCCEEDED"
    assert task["totalPages"] == 1
    assert task["successPages"] == 1
    assert task["failedPages"] == 0
    assert task["selectedModelId"] == modelId
    assert float(task["totalCostUsd"]) > 0
    assert task["finishedTime"] is not None

    # Assert：Page 落库 + 分类建议
    pageId = task["pageIds"][0]
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    assert page.dimension == "RULE"
    assert page.auto_classification["primary"] == "RULE"
    assert page.auto_classification["confidence"] == pytest.approx(0.92)
    assert page.imported_via_task_id == task["id"]
    assert page.processing_model_id == modelId
    assert page.structure_stage == "MARKDOWN"

    # Assert：计量台账（独立于 session_token_usage）
    usages = (await dbSession.execute(select(WikiTokenUsage))).scalars().all()
    assert len(usages) == 1
    assert usages[0].mechanism == "CLASSIFY"
    assert usages[0].prompt_tokens == 100
    assert usages[0].completion_tokens == 50
    assert usages[0].import_task_id == task["id"]
    assert usages[0].model_config_id == modelId
    # 成本 = 100*0.001/1000 + 50*0.002/1000 = 0.0002
    assert usages[0].cost == Decimal("0.000200")


async def test_execute_classification_strips_json_fence(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """LLM 用 ```json 围栏包裹（deepseek 默认）时仍能解析。"""
    modelId = await _seedModel(dbSession)
    fake = _FakeLlmClient(f"```json\n{_CLASSIFY_JSON}\n```")

    with patch(_INVOKER_CLIENT, return_value=fake):
        resp = await client.post(
            f"{_BASE}/execute",
            json={"drafts": _drafts(("规则", "内容")), "modelId": modelId},
        )

    assert resp.status_code == 201
    pageId = resp.json()["pageIds"][0]
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    assert page.dimension == "RULE"


async def test_execute_without_auto_classify_skips_llm(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """关闭 autoClassify → 不调模型、不记计量、dimension 留空。"""
    modelId = await _seedModel(dbSession)
    fake = _FakeLlmClient()

    with patch(_INVOKER_CLIENT, return_value=fake):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": _drafts(("仅存档", "纯文本内容")),
                "modelId": modelId,
                "autoClassify": False,
            },
        )

    assert resp.status_code == 201
    assert fake.calls == 0
    assert (await dbSession.execute(select(WikiTokenUsage))).scalars().all() == []

    pageId = resp.json()["pageIds"][0]
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    assert page.dimension is None
    # 仍记录用户选的模型，供 M3 批量重分类复用
    assert page.processing_model_id is None
    assert resp.json()["selectedModelId"] == modelId


async def test_execute_auto_classify_without_model_returns_422(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        f"{_BASE}/execute", json={"drafts": _drafts(("标题", "正文"))}
    )
    assert resp.status_code == 422


async def test_execute_too_many_drafts_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """草稿条数超上限 → 422。

    回归点：drafts 是「用户输入 × LLM 调用」的乘数，不设上限等于把成本开关
    交给调用方（10 万条 = 10 万次模型调用 + 10 万次 INSERT）。
    """
    modelId = await _seedModel(dbSession)
    tooMany = [{"title": f"标题{i}", "content": "内容"} for i in range(MAX_IMPORT_DRAFTS + 1)]

    resp = await client.post(
        f"{_BASE}/execute", json={"drafts": tooMany, "modelId": modelId}
    )

    assert resp.status_code == 422
    # 边界校验在服务层之前拦下：不该留下任何任务或条目
    assert (await dbSession.execute(select(WikiImportTask))).scalars().all() == []
    assert (await dbSession.execute(select(WikiPage))).scalars().all() == []


async def test_execute_oversized_content_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """单条正文超上限 → 422（PG 的 TEXT 不截断，需在边界挡）。"""
    modelId = await _seedModel(dbSession)
    oversized = "x" * (MAX_CONTENT_CHARS + 1)

    resp = await client.post(
        f"{_BASE}/execute",
        json={"drafts": [{"title": "标题", "content": oversized}], "modelId": modelId},
    )

    assert resp.status_code == 422


async def test_execute_batch_creates_all_pages(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    modelId = await _seedModel(dbSession)
    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": _drafts(("甲", "内容甲"), ("乙", "内容乙"), ("丙", "内容丙")),
                "modelId": modelId,
            },
        )

    assert resp.status_code == 201
    assert resp.json()["successPages"] == 3
    assert len((await dbSession.execute(select(WikiPage))).scalars().all()) == 3
    assert len((await dbSession.execute(select(WikiTokenUsage))).scalars().all()) == 3


# ---------------------------------------------------------------------------
# 执行：失败与降级
# ---------------------------------------------------------------------------


async def test_execute_unusable_model_returns_503(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """模型无凭据（createClient → None）→ 503，而不是 500。"""
    modelId = await _seedModel(dbSession)
    with patch(_INVOKER_CLIENT, return_value=None):
        resp = await client.post(
            f"{_BASE}/execute",
            json={"drafts": _drafts(("标题", "正文")), "modelId": modelId},
        )
    assert resp.status_code == 503


async def test_execute_falls_back_when_primary_fails(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """primary 挂了 → 用 fallback 完成，且 processing_model_id 记的是 fallback。"""
    primaryId = await _seedModel(dbSession, modelName="primary-down")
    fallbackId = await _seedModel(dbSession, modelName="fallback-up")

    def _factory(config):
        return _FakeLlmClient(raiseError=config.model_name == "primary-down")

    with patch(_INVOKER_CLIENT, side_effect=_factory):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": _drafts(("规则", "内容")),
                "modelId": primaryId,
                "fallbackModelId": fallbackId,
            },
        )

    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "SUCCEEDED"
    # 审计上能看出「选了 primary、实际用了 fallback」
    assert task["selectedModelId"] == primaryId

    page = (
        await dbSession.execute(
            select(WikiPage).where(WikiPage.page_id == task["pageIds"][0])
        )
    ).scalar_one()
    assert page.processing_model_id == fallbackId


async def test_execute_classification_parse_failure_keeps_page(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """分类输出不可解析 → 知识照常入库（dimension 空），但状态记 PARTIAL。

    条目本身成功（``successPages == 1``），所以不是 FAILED —— 分类是增强，
    不该让导入失败。但也不能记 SUCCEEDED：用户显式选了模型要求分类，拿到
    「成功」却零分类，等于把「模型没跑成」谎报成「模型认为无维度」。
    """
    modelId = await _seedModel(dbSession)

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient("这不是 JSON")):
        resp = await client.post(
            f"{_BASE}/execute",
            json={"drafts": _drafts(("规则", "正文")), "modelId": modelId},
        )

    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "PARTIAL"
    assert task["successPages"] == 1
    assert "分类" in task["errorMessage"]

    page = (
        await dbSession.execute(
            select(WikiPage).where(WikiPage.page_id == task["pageIds"][0])
        )
    ).scalar_one()
    assert page.dimension is None
    # token 是真花掉的，计量行必须留下
    assert len((await dbSession.execute(select(WikiTokenUsage))).scalars().all()) == 1


async def test_execute_llm_network_error_keeps_page(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """分类时模型网络故障（LlmClientError）不得中止整批导入。

    ``LlmClientError`` 是 ``LLMUnavailableError`` 的**兄弟**类而非子类，
    只捕获后者会让一次瞬时抖动冒到循环外：整批中止 + 任务永远挂在 RUNNING。
    """
    modelId = await _seedModel(dbSession)

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient(raiseError=True)):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": _drafts(("规则", "正文"), ("定义", "正文2")),
                "modelId": modelId,
            },
        )

    assert resp.status_code == 201
    task = resp.json()
    assert task["successPages"] == 2
    assert task["failedPages"] == 0
    assert task["status"] == "PARTIAL"
    assert task["errorMessage"]

    # 不能被留成僵尸 RUNNING
    stored = (
        await dbSession.execute(
            select(WikiImportTask).where(WikiImportTask.id == task["id"])
        )
    ).scalar_one()
    assert stored.status == "PARTIAL"
    assert stored.finished_time is not None


async def test_execute_invalid_dimension_discards_suggestion(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """LLM 给了白名单外的维度 → 丢弃建议，但条目照常入库。

    与「调用失败」同属「未获得分类」：模型答了，只是答案不可用。状态同样
    记 PARTIAL —— 用户要的是分类结果，拿到一个被丢弃的建议等于没分类。
    """
    modelId = await _seedModel(dbSession)
    bogus = json.dumps({"primary": "NOT_A_DIMENSION", "confidence": 0.9})

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient(bogus)):
        resp = await client.post(
            f"{_BASE}/execute",
            json={"drafts": _drafts(("规则", "正文")), "modelId": modelId},
        )

    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "PARTIAL"
    assert task["successPages"] == 1
    page = (
        await dbSession.execute(
            select(WikiPage).where(WikiPage.page_id == task["pageIds"][0])
        )
    ).scalar_one()
    assert page.dimension is None
    assert page.auto_classification is None


async def test_execute_duplicate_page_id_yields_partial(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """一条 pageId 撞号不影响其余：任务记 PARTIAL。"""
    modelId = await _seedModel(dbSession)
    # 先占位
    await client.post(
        _PAGES, json={"pageId": "DUP-001", "title": "已存在", "content": "x"}
    )

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [
                    {"pageId": "DUP-001", "title": "撞号", "content": "x"},
                    {"title": "正常条目", "content": "y"},
                ],
                "modelId": modelId,
            },
        )

    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "PARTIAL"
    assert task["successPages"] == 1
    assert task["failedPages"] == 1


async def test_execute_unexpected_error_marks_task_failed(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """逃出单条 catch 的异常 → 任务落成 FAILED，不能留下僵尸 RUNNING。

    这是兜底路径：异常若只上抛不落库，台账里会永远挂着一条「进行中」的
    任务（用户看到 RUNNING，实际早就没人跑了）。
    """
    modelId = await _seedModel(dbSession)

    # 预检要先过（否则 503 根本走不到循环），异常由 _importOne 制造
    with (
        patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()),
        patch.object(WikiImportService, "_importOne", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        await client.post(
            f"{_BASE}/execute",
            json={"drafts": _drafts(("标题", "正文")), "modelId": modelId},
        )

    # 用裸 SQL 读，绕开 SQLAlchemy 2.x 的 identity map 缓存
    row = (
        await dbSession.execute(
            text("SELECT status, finished_time FROM wiki_import_task")
        )
    ).one()
    assert row.status == "FAILED"
    assert row.finished_time is not None


async def test_execute_all_failed_marks_failed(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    modelId = await _seedModel(dbSession)
    await client.post(
        _PAGES, json={"pageId": "DUP-ALL", "title": "已存在", "content": "x"}
    )

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [{"pageId": "DUP-ALL", "title": "撞号", "content": "x"}],
                "modelId": modelId,
            },
        )

    task = resp.json()
    assert task["status"] == "FAILED"
    assert task["errorMessage"]


# ---------------------------------------------------------------------------
# 任务台账
# ---------------------------------------------------------------------------


async def test_list_tasks_empty(client: AsyncClient) -> None:
    resp = await client.get(f"{_BASE}/tasks")
    assert resp.status_code == 200
    assert resp.json() == {"rows": [], "total": 0}


async def test_list_tasks_pagination_and_order(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """倒序 + total 独立于 limit。"""
    modelId = await _seedModel(dbSession)
    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        for i in range(3):
            await client.post(
                f"{_BASE}/execute",
                json={"drafts": _drafts((f"标题{i}", "内容")), "modelId": modelId},
            )

    resp = await client.get(f"{_BASE}/tasks", params={"limit": 2})
    body = resp.json()
    assert len(body["rows"]) == 2
    assert body["total"] == 3
    # 倒序：最新（id 最大）在前
    assert body["rows"][0]["id"] > body["rows"][1]["id"]


async def test_list_tasks_hides_other_users_jobs(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """非 admin 的台账只含自己发起的作业。

    回归点：``/tasks`` 曾无归属过滤，任何登录用户都能枚举他人的导入作业
    （含用户自填的 sourceRef 与花费）。
    """
    # Arrange：两个真实 DB 用户（stub 头命中 DB 用户名时以 DB 角色为准，
    # 新用户没有角色 → 非 admin），外加 alice 的一次导入
    from scripts.seed_rbac import seedRbacBaseline

    await seedRbacBaseline(dbSession)
    for username in ("alice", "bob"):
        resp = await client.post(
            "/api/v1/users",
            headers=_ADMIN_HEADERS,
            json={"username": username, "display_name": username, "email": None},
        )
        assert resp.status_code == 201, resp.text

    modelId = await _seedModel(dbSession)
    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_BASE}/execute",
            headers={"X-User-Id": "alice"},
            json={
                "drafts": _drafts(("alice 的制度", "内容")),
                "modelId": modelId,
                "sourceRef": "alice-private.md",
            },
        )
    assert resp.status_code == 201

    # Act + Assert：bob 看不到 alice 的作业
    bob = await client.get(f"{_BASE}/tasks", headers={"X-User-Id": "bob"})
    assert bob.status_code == 200
    assert bob.json() == {"rows": [], "total": 0}

    # alice 能看到自己的
    alice = await client.get(f"{_BASE}/tasks", headers={"X-User-Id": "alice"})
    assert alice.json()["total"] == 1

    # admin 看全量（运维排查失败批次需要）
    admin = await client.get(f"{_BASE}/tasks", headers=_ADMIN_HEADERS)
    assert admin.json()["total"] == 1


# ---------------------------------------------------------------------------
# 认证与授权（Harness 权限与安全规范：所有路由默认 Depends(getCurrentUser)）
# ---------------------------------------------------------------------------


async def test_wiki_routes_reject_when_stub_auth_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉 stub auth（模拟生产）→ 无凭据的 wiki 写操作必须 403。

    回归点：wiki 写接口曾漏挂认证依赖，导致未认证客户端可改删条目。
    """
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")

    assert (await client.get(_PAGES)).status_code == 403
    assert (await client.delete(f"{_PAGES}/ANY")).status_code == 403
    assert (
        await client.post(f"{_BASE}/execute", json={"drafts": [{"title": "a", "content": "b"}]})
    ).status_code == 403


async def test_execute_inactive_model_returns_503(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """已停用的模型即使有凭据也不能用 → 503。

    回归点：``/models`` 宣称 ``usable = is_active 且凭据可用``，但 execute
    只验凭据，调用方可以传已停用模型的 id 继续烧钱。
    """
    modelId = await _seedModel(dbSession, isActive=False)

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_BASE}/execute",
            json={"drafts": _drafts(("标题", "正文")), "modelId": modelId},
        )

    assert resp.status_code == 503
    # 预检失败不该留下半截任务
    assert (await dbSession.execute(select(WikiImportTask))).scalars().all() == []


async def test_execute_unknown_task_type_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """taskType 不在白名单 → 422（避免库里散落拼错的类型，无法聚合）。"""
    modelId = await _seedModel(dbSession)
    resp = await client.post(
        f"{_BASE}/execute",
        json={
            "drafts": _drafts(("标题", "正文")),
            "modelId": modelId,
            "taskType": "NOT_A_TYPE",
        },
    )
    assert resp.status_code == 422


async def test_execute_unknown_source_type_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    modelId = await _seedModel(dbSession)
    resp = await client.post(
        f"{_BASE}/execute",
        json={
            "drafts": _drafts(("标题", "正文")),
            "modelId": modelId,
            "sourceType": "NOT_A_SOURCE",
        },
    )
    assert resp.status_code == 422


async def test_execute_sanitizes_explicit_page_id(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """显式传入的 pageId 与自动生成的走同一套字符集收敛。

    回归点：含 `/` 的 pageId 能落库，但 ``GET /wiki/pages/{pageId}`` 的路径
    参数匹配不到它 —— 条目建出来就不可达。
    """
    modelId = await _seedModel(dbSession)

    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [
                    {"pageId": "a/b c", "title": "带斜杠", "content": "内容"}
                ],
                "modelId": modelId,
                "autoClassify": False,
            },
        )

    assert resp.status_code == 201
    pageId = resp.json()["pageIds"][0]
    assert "/" not in pageId and " " not in pageId
    # 建出来的条目必须真的能按 id 取回（这才是收敛的目的）
    assert (await client.get(f"{_PAGES}/{pageId}")).status_code == 200
