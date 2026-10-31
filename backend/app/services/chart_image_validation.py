"""导出图表位图的校验与归属（0105；2026-10-31 分节插图）。

从 ``session_history_service`` 拆出：会话历史的读/删与「接收用户提交的二进制」
是两回事，放一个类里会让 800 行的服务继续膨胀，也让「边界校验」淹没在 SQL 聚合里。

三条职责，全部围绕 ``(messageId, stepIndex)`` 这个键：

- ``resolveChartImages``  校验并解码 → ``{(messageId, stepIndex): pngBytes}``；
- ``attachChartImages``   把位图挂到 payload 对应轮次/步骤（返回新 payload）；
- ``decodeChartImage`` / ``pngPixelCount`` 是前两者共用的两道底层闸门。

``stepIndex`` 为 ``None`` = 该轮顶层图（0105 行为）；非 None = 多步报告里的某一步。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import replace

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.error_messages import (
    MSG_EXPORT_CHART_COUNT_EXCEEDED,
    MSG_EXPORT_CHART_IMAGE_INVALID,
    MSG_EXPORT_CHART_IMAGE_TOO_LARGE,
    MSG_EXPORT_CHART_IMAGE_TOO_MANY_PIXELS,
    MSG_EXPORT_CHART_MESSAGE_NOT_FOUND,
    MSG_EXPORT_CHART_STEP_NOT_FOUND,
    MSG_EXPORT_CHART_TOTAL_TOO_LARGE,
    MSG_EXPORT_CHART_TOTAL_TOO_MANY_PIXELS,
)
from app.domain.exceptions import ValidationError
from app.domain.export_limits import (
    CHART_IMAGE_DATA_URL_PREFIX,
    MAX_EXPORT_CHART_IMAGES,
    MAX_EXPORT_IMAGE_BYTES,
    MAX_EXPORT_IMAGE_PIXELS,
    MAX_EXPORT_TOTAL_IMAGE_BYTES,
    MAX_EXPORT_TOTAL_IMAGE_PIXELS,
    PNG_MAGIC,
)
from app.domain.models import SessionMessage
from app.domain.schemas import ChatExportChartImage
from app.services.pdf_export_service import ChatExportPayload, ChatExportTurn
from app.services.report_sections import chartSteps

ChartImageKey = tuple[int, int | None]


def decodeChartImage(dataUrl: str) -> bytes:
    """单张 data URL → PNG 字节；前缀/大小/魔数任一不合法抛 ``ValidationError``。"""
    if not dataUrl.startswith(CHART_IMAGE_DATA_URL_PREFIX):
        raise ValidationError(MSG_EXPORT_CHART_IMAGE_INVALID)
    payload = dataUrl[len(CHART_IMAGE_DATA_URL_PREFIX):]
    # 先用 base64 的长度反推解码后大小（4 字符 → 3 字节）再解码：反过来等于
    # 让一个超长字符串先把内存占住，上限就成了摆设。
    if len(payload) * 3 // 4 > MAX_EXPORT_IMAGE_BYTES:
        raise ValidationError(MSG_EXPORT_CHART_IMAGE_TOO_LARGE)
    try:
        imageBytes = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError(MSG_EXPORT_CHART_IMAGE_INVALID) from None
    if len(imageBytes) > MAX_EXPORT_IMAGE_BYTES:
        raise ValidationError(MSG_EXPORT_CHART_IMAGE_TOO_LARGE)
    if not imageBytes.startswith(PNG_MAGIC):
        raise ValidationError(MSG_EXPORT_CHART_IMAGE_INVALID)
    return imageBytes


def pngPixelCount(imageBytes: bytes) -> int | None:
    """PNG 的像素数（宽 × 高），从 IHDR 头直接读；头不完整/不合法返回 ``None``。

    **为什么手读头而不是用 Pillow 量尺寸**：像素数必须在这一层卡住，而
    ``PIL.Image.open`` 一旦打开就已经按解出的尺寸分配过内存了 —— 让它先解码
    再回头问「你多大」，炸弹早响完了。PNG 的尺寸恒在固定偏移（8 字节签名 +
    4 字节块长 + 4 字节块类型 ⇒ 偏移 16 起 8 个字节），读 8 字节就够，
    也不需要为此把 Pillow 变成这个服务的依赖。
    """
    if len(imageBytes) < 24 or imageBytes[12:16] != b"IHDR":
        return None
    width = int.from_bytes(imageBytes[16:20], "big")
    height = int.from_bytes(imageBytes[20:24], "big")
    # 0 是畸形的（PNG 规范要求 ≥1），照 None 处理，别让 0×N 混过像素闸
    if width <= 0 or height <= 0:
        return None
    return width * height


async def assertChartOwnership(
    session: AsyncSession,
    sessionId: str,
    charts: list[ChatExportChartImage],
) -> None:
    """位图的归属必须真实存在；否则 422（不回显任何 id）。

    两次判定：
    1. ``messageId`` 必须属于该 session；
    2. 带 ``stepIndex`` 的位图，该步必须在 ``steps_snapshot`` 里且**确有图**
       （判据与 ``buildReportSections`` 同为 ``chartSteps``）—— 否则前端算错
       归属时图会挂到「没图的一步」上，PDF 里对应块反而变占位框。
    """
    messageIds = {chart.message_id for chart in charts}
    stmt = select(SessionMessage.id, SessionMessage.steps_snapshot).where(
        and_(
            SessionMessage.session_id == sessionId,
            SessionMessage.id.in_(messageIds),
        )
    )
    snapshots = {
        row.id: row.steps_snapshot
        for row in (await session.execute(stmt)).all()
    }
    if len(snapshots) != len(messageIds):
        raise ValidationError(MSG_EXPORT_CHART_MESSAGE_NOT_FOUND)
    chartedSteps = {
        messageId: {step["stepIndex"] for step in chartSteps(snapshot)}
        for messageId, snapshot in snapshots.items()
    }
    for chart in charts:
        if chart.step_index is None:
            continue
        if chart.step_index not in chartedSteps.get(chart.message_id, set()):
            raise ValidationError(MSG_EXPORT_CHART_STEP_NOT_FOUND)


async def resolveChartImages(
    session: AsyncSession,
    sessionId: str,
    charts: list[ChatExportChartImage],
) -> dict[ChartImageKey, bytes]:
    """校验并解码前端回传的图表位图 → ``{(messageId, stepIndex): pngBytes}``。

    ``stepIndex`` 为 None = 该轮顶层图（0105 行为）；非 None = 多步报告的某一
    步（2026-10-31 分节插图）。

    这是本服务第一次接收**用户提交的二进制**，故在边界处显式失败
    （任一条不合法 → ``ValidationError`` ⇒ 422）：

    - 条数 / 单张字节 / 总量字节三重上限（见 ``app.domain.export_limits``）；
    - **单张与总量的像素上限** —— 字节闸管不住解压炸弹；
    - ``data:image/png;base64,`` 前缀 + PNG 魔数（只信前缀等于允许「任意
      二进制贴个 PNG 标签」送进来）；
    - ``messageId`` 必须属于该 session —— **不信任前端传来的 id**，否则可以
      借导出把别人的图拼进来。报错不回显 id（沿用 HIGH-3 口径，否则 422
      就成了「这个 id 属不属于这个会话」的枚举探针）；
    - ``stepIndex`` 必须指向该消息快照里**确有图**的一步（判据与
      ``buildReportSections`` 同一把尺），否则 422。

    重复 ``(messageId, stepIndex)`` 取后者：前端逐条生成不该重复，不值得为
    这点小事拒掉整份导出。
    """
    if len(charts) > MAX_EXPORT_CHART_IMAGES:
        raise ValidationError(MSG_EXPORT_CHART_COUNT_EXCEEDED)
    decoded: dict[ChartImageKey, bytes] = {}
    totalBytes = 0
    totalPixels = 0
    for chart in charts:
        imageBytes = decodeChartImage(chart.image_png)
        totalBytes += len(imageBytes)
        # 逐张累加即判：等全部解完再量体，等于让一个请求先把内存占满
        if totalBytes > MAX_EXPORT_TOTAL_IMAGE_BYTES:
            raise ValidationError(MSG_EXPORT_CHART_TOTAL_TOO_LARGE)
        # 像素同样逐张累加即判，理由同上 —— 只是这条闸拦的是解压炸弹
        pixelCount = pngPixelCount(imageBytes)
        if pixelCount is None:
            # 魔数对但读不出 IHDR（截断/畸形）：与其交给 Pillow 赌它报不报错，
            # 不如在边界上按「不是合法 PNG」拒掉。
            raise ValidationError(MSG_EXPORT_CHART_IMAGE_INVALID)
        if pixelCount > MAX_EXPORT_IMAGE_PIXELS:
            raise ValidationError(MSG_EXPORT_CHART_IMAGE_TOO_MANY_PIXELS)
        totalPixels += pixelCount
        if totalPixels > MAX_EXPORT_TOTAL_IMAGE_PIXELS:
            raise ValidationError(MSG_EXPORT_CHART_TOTAL_TOO_MANY_PIXELS)
        decoded[(chart.message_id, chart.step_index)] = imageBytes
    if decoded:
        await assertChartOwnership(session, sessionId, charts)
    return decoded


def attachChartImages(
    payload: ChatExportPayload,
    imagesByKey: dict[ChartImageKey, bytes],
) -> ChatExportPayload:
    """把位图按 ``(messageId, stepIndex)`` 挂到对应轮次/步骤上（返回新 payload）。

    ``stepIndex is None`` → 该轮顶层图；非 None → 分节里那一步的图。

    没挂上的位图（该轮被 ``_MAX_TURNS_PER_EXPORT`` 截掉、或前端算错了 id）
    **静默忽略**：导出是主功能，多出来一张没人要的图不该让它失败。
    """
    if not imagesByKey:
        return payload

    def withImages(turn: ChatExportTurn) -> ChatExportTurn:
        if turn.message_id is None:
            return turn
        topImage = imagesByKey.get((turn.message_id, None))
        stepCharts = tuple(
            replace(
                step,
                chart_image=imagesByKey[(turn.message_id, step.step_index)],
            )
            if (turn.message_id, step.step_index) in imagesByKey
            else step
            for step in turn.step_charts
        )
        if topImage is None and stepCharts == turn.step_charts:
            return turn
        return replace(
            turn,
            chart_image=topImage if topImage is not None else turn.chart_image,
            step_charts=stepCharts,
        )

    return replace(payload, turns=[withImages(turn) for turn in payload.turns])
