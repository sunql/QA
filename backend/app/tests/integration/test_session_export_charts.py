"""导出 PDF 携带图表位图的集成测试（真 PG + 完整 API 链路，0105）。

覆盖新端点接收**用户提交的二进制**的四类边界：合法 PNG 嵌进 PDF、无图回落
（向后兼容）、格式非法、跨 session 归属越界。渲染细节（原生 table/kpi 排版、
等比缩放）在 ``app/tests/unit/test_pdf_export_charts.py``，此处只钉 HTTP 契约。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from app.domain.models import SessionMessage
from app.services.session_history_service import (
    _CHART_IMAGE_DATA_URL_PREFIX,
    _MAX_EXPORT_CHART_IMAGES,
    _MAX_EXPORT_IMAGE_PIXELS,
    _MAX_EXPORT_TOTAL_IMAGE_PIXELS,
)
from app.tests._png_support import pngDeclaringSize, solidPng

_T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
_T1 = datetime(2026, 1, 1, 10, 1, tzinfo=UTC)


async def _seedMessage(
    dbSession,
    *,
    sessionId: str,
    role: str,
    content: str,
    chartType: str | None = None,
    chartOption: dict | None = None,
    createdAt: datetime | None = None,
) -> SessionMessage:
    msg = SessionMessage(
        session_id=sessionId,
        role=role,
        content=content,
        chart_type=chartType,
        chart_option=chartOption,
        created_time=createdAt or _T0,
        updated_time=createdAt or _T0,
    )
    dbSession.add(msg)
    await dbSession.commit()
    await dbSession.refresh(msg)
    return msg


async def _seedTurn(
    dbSession,
    sessionId: str,
    *,
    createdAt: datetime | None = None,
    **assistantKwargs,
) -> SessionMessage:
    """一条完整问答轮次，返回 assistant 行（位图按它的 id 归属）。"""
    await _seedMessage(dbSession, sessionId=sessionId, role="user", content="问", createdAt=_T0)
    return await _seedMessage(
        dbSession, sessionId=sessionId, role="assistant", content="答",
        createdAt=createdAt or _T1, **assistantKwargs,
    )


def _dataUrl(png: bytes) -> str:
    return _CHART_IMAGE_DATA_URL_PREFIX + base64.b64encode(png).decode()


class TestExportChartsApi:
    async def test_uploaded_bitmap_is_embedded_in_pdf(self, client, dbSession) -> None:
        """合法 PNG → 200，且 PDF 里真的出现了图像对象（不是只把请求收下了）。"""
        # Arrange
        sid = "s-chart-img"
        asst = await _seedTurn(dbSession, sid, chartType="bar", chartOption={"series": []})

        # Act
        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf",
            json={"charts": [{"messageId": asst.id, "imagePng": _dataUrl(solidPng(400, 300))}]},
        )

        # Assert
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        assert resp.content.startswith(b"%PDF-1.")
        assert b"/Subtype /Image" in resp.content

    async def test_export_without_charts_renders_placeholder(self, client, dbSession) -> None:
        """不传图 = 旧行为：仍 200，图表回落占位框（不是失败、也不是空白）。"""
        sid = "s-chart-none"
        await _seedTurn(dbSession, sid, chartType="bar")

        resp = await client.post(f"/api/v1/sessions/{sid}/export.pdf", json={})

        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-1.")
        assert b"/Subtype /Image" not in resp.content

    async def test_table_chart_option_renders_without_bitmap(self, client, dbSession) -> None:
        """table 是服务端原生画的，前端一张图都不回传也该出表格。"""
        sid = "s-chart-table"
        await _seedTurn(
            dbSession, sid, chartType="table",
            chartOption={"columns": ["地区", "销量"], "rows": [{"地区": "华北", "销量": 100}]},
        )

        resp = await client.post(f"/api/v1/sessions/{sid}/export.pdf", json={})

        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-1.")
        # 原生表格不是位图，所以没有图像对象。但这条断言本身**没有牙**：
        # 表格分支挂掉退回占位框时，PDF 里同样没有图像对象。真正证明「表格分支
        # 赢了」的是下面那条 —— 分派顺序是 table → kpi → **image** → 占位框，
        # 所以只有「给了位图也不嵌」才排除了退回的情形。
        assert b"/Subtype /Image" not in resp.content

    async def test_table_turn_prefers_native_table_over_uploaded_bitmap(
        self, client, dbSession
    ) -> None:
        """table 轮次即便收到位图也不嵌 —— 位图只服务 ECharts 类图型。

        分派顺序（``pdf_export_service._chart_flowables``）是 table → kpi → image
        → 占位框：表格分支一旦解析 `chart_option` 失败就会**落到** image 分支，
        此时位图会被嵌进去。所以「给了位图但仍无图像对象」是表格分支真的赢了的
        证据 —— 这是上一条测试无法给出的。
        """
        sid = "s-chart-table-wins"
        asst = await _seedTurn(
            dbSession, sid, chartType="table",
            chartOption={"columns": ["地区", "销量"], "rows": [{"地区": "华北", "销量": 100}]},
        )

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf",
            json={"charts": [{"messageId": asst.id, "imagePng": _dataUrl(solidPng(120, 80))}]},
        )

        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-1.")
        assert b"/Subtype /Image" not in resp.content, "表格轮次不该嵌位图（分派应命中表格分支）"

    async def test_rejects_bitmap_for_message_of_another_session(
        self, client, dbSession
    ) -> None:
        """跨 session 的 messageId → 422；错误信息不回显任何 id（防归属枚举）。"""
        # Arrange
        ownSid, otherSid = "s-chart-own", "s-chart-other"
        await _seedTurn(dbSession, ownSid)
        foreign = await _seedTurn(dbSession, otherSid)

        # Act
        resp = await client.post(
            f"/api/v1/sessions/{ownSid}/export.pdf",
            json={"charts": [{"messageId": foreign.id, "imagePng": _dataUrl(solidPng(8, 8))}]},
        )

        # Assert
        assert resp.status_code == 422
        body = str(resp.json())
        assert str(foreign.id) not in body
        assert otherSid not in body
        assert ownSid not in body

    async def test_rejects_image_without_png_data_url_prefix(self, client, dbSession) -> None:
        sid = "s-chart-prefix"
        asst = await _seedTurn(dbSession, sid)

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf",
            json={"charts": [{"messageId": asst.id, "imagePng": "aGVsbG8="}]},
        )

        assert resp.status_code == 422

    async def test_rejects_non_png_bytes_wearing_the_png_prefix(self, client, dbSession) -> None:
        """前缀可以伪造，魔数不行。"""
        sid = "s-chart-magic"
        asst = await _seedTurn(dbSession, sid)
        fake = _CHART_IMAGE_DATA_URL_PREFIX + base64.b64encode(b"PK\x03\x04zip").decode()

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf",
            json={"charts": [{"messageId": asst.id, "imagePng": fake}]},
        )

        assert resp.status_code == 422

    async def test_rejects_more_than_image_count_limit(self, client, dbSession) -> None:
        sid = "s-chart-count"
        asst = await _seedTurn(dbSession, sid)
        png = _dataUrl(solidPng(8, 8))
        charts = [
            {"messageId": asst.id, "imagePng": png}
            for _ in range(_MAX_EXPORT_CHART_IMAGES + 1)
        ]

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf", json={"charts": charts}
        )

        assert resp.status_code == 422

    async def test_rejects_decompression_bomb(self, client, dbSession) -> None:
        """解密炸弹：73 字节的文件声明 12000×12000（1.44 亿像素）。

        字节闸（2 MiB）与 PNG 魔数**都会放行**它 —— 只有像素闸能拦。不拦的代价是
        Pillow 按 1.44 亿像素分配内存（实测单张峰值 RSS +2 GB），而 PIL 默认的
        ``MAX_IMAGE_PIXELS`` 要到 2 倍（1.79 亿）才抛异常，这一段完全不设防。
        """
        sid = "s-chart-bomb"
        asst = await _seedTurn(dbSession, sid)
        bomb = _dataUrl(pngDeclaringSize(12000, 12000))
        assert len(bomb) < 2 * 1024 * 1024, "前提：它必须能通过字节闸，否则测的不是像素闸"

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf",
            json={"charts": [{"messageId": asst.id, "imagePng": bomb}]},
        )

        assert resp.status_code == 422

    async def test_rejects_when_total_pixels_exceed_budget(self, client, dbSession) -> None:
        """单张都合规，累计超总量 —— 与字节总量同口径的逐张累加即判。"""
        sid = "s-chart-pixels-total"
        asst = await _seedTurn(dbSession, sid)
        # 每张 2800×2800 = 7.84M px（单张上限 8M 之内），6 张 = 47M > 40M 总量
        each = 2800 * 2800
        assert each < _MAX_EXPORT_IMAGE_PIXELS
        count = _MAX_EXPORT_TOTAL_IMAGE_PIXELS // each + 1
        png = _dataUrl(pngDeclaringSize(2800, 2800))
        charts = [{"messageId": asst.id, "imagePng": png} for _ in range(count)]

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf", json={"charts": charts}
        )

        assert resp.status_code == 422

    async def test_accepts_pixels_just_under_the_total_budget(
        self, client, dbSession
    ) -> None:
        """反向守卫：没超预算的**不能被误拦** —— 否则像素闸就成了「图一多就导不出」。

        这些 PNG 的 IHDR 声明与实际数据不符（``pngDeclaringSize`` 的构造使然），
        会解码失败降级成占位框 —— 那正是设计内的降级，导出本身必须 200。
        """
        sid = "s-chart-pixels-ok"
        asst = await _seedTurn(dbSession, sid)
        each = 2800 * 2800
        count = _MAX_EXPORT_TOTAL_IMAGE_PIXELS // each  # 不放 +1：刚好不超
        png = _dataUrl(pngDeclaringSize(2800, 2800))
        charts = [{"messageId": asst.id, "imagePng": png} for _ in range(count)]

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf", json={"charts": charts}
        )

        assert resp.status_code == 200

    async def test_rejects_png_with_unreadable_ihdr(self, client, dbSession) -> None:
        """魔数对但读不出 IHDR（截断）—— 与其交给 Pillow 赌它报不报错，边界上拒掉。"""
        sid = "s-chart-truncated"
        asst = await _seedTurn(dbSession, sid)

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf",
            json={"charts": [{"messageId": asst.id, "imagePng": _dataUrl(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d")}]},
        )

        assert resp.status_code == 422

    async def test_empty_session_still_404_even_with_charts(self, client, dbSession) -> None:
        """会话本身不存在该报 404 —— 不该被「图片格式对不对」的问题掩盖。"""
        resp = await client.post(
            "/api/v1/sessions/s-chart-ghost/export.pdf",
            json={"charts": [{"messageId": 1, "imagePng": _dataUrl(solidPng(8, 8))}]},
        )

        assert resp.status_code == 404

    async def test_single_turn_export_ignores_bitmap_of_other_turns(
        self, client, dbSession
    ) -> None:
        """单条导出只挂该轮的图；其他轮次的位图静默忽略，不影响导出成败。"""
        sid = "s-chart-single"
        first = await _seedTurn(dbSession, sid, chartType="bar", chartOption={"series": []})
        second = await _seedTurn(
            dbSession, sid, chartType="line", chartOption={"series": []}, createdAt=_T1
        )

        resp = await client.post(
            f"/api/v1/sessions/{sid}/export.pdf",
            json={
                "messageId": first.id,
                "charts": [
                    {"messageId": first.id, "imagePng": _dataUrl(solidPng(120, 80))},
                    {"messageId": second.id, "imagePng": _dataUrl(solidPng(120, 80))},
                ],
            },
        )

        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-1.")
