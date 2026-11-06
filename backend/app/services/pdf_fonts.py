"""PDF 中文字体解析：优先微软雅黑，缺字体文件时回落 reportlab 内置 STSong-Light。

为什么不再直接用 STSong-Light：它是 CID 字体，**不嵌字形**，阅读器需要额外
装 FontPack，没装的（多数浏览器内置查看器、手机端）会把中文显示成方框。微软
雅黑是 TrueType，reportlab 会按需嵌入子集，任何阅读器都能直接显示。

字体文件**不入库**（微软雅黑不可再分发）：生产由运维把 ``msyh.ttc`` /
``msyhbd.ttc`` 放到 compose 挂载点 ``/app/fonts``；本地直接跑 uvicorn 时探测
仓库内的 ``docker/fonts`` / ``backend/fonts``。可用 ``PDF_CJK_FONT_PATH`` /
``PDF_CJK_BOLD_FONT_PATH`` 显式指定路径。

缺文件、文件损坏都只是回落旧外观，**绝不让 PDF 导出整体失败** —— 导出的主
功能是问答内容，字体是外观。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

logger = logging.getLogger(__name__)

_FONT_PATH_ENV = "PDF_CJK_FONT_PATH"
_BOLD_FONT_PATH_ENV = "PDF_CJK_BOLD_FONT_PATH"

# 家族名：正文样式统一用它，``<b>`` / ``<i>`` 由 registerFontFamily 映射
_FAMILY = "MicrosoftYaHei"
_FALLBACK_FONT_NAME = "STSong-Light"

# backend/app/services/pdf_fonts.py → parents[2] == backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
# 探测顺序：容器挂载点 → 仓库内本地约定目录
_DEFAULT_DIRS = (
    "/app/fonts",
    str(_BACKEND_ROOT.parent / "docker" / "fonts"),
    str(_BACKEND_ROOT / "fonts"),
)
_DEFAULT_BASENAMES = ("msyh.ttc", "msyh.ttf")
_DEFAULT_BOLD_BASENAMES = ("msyhbd.ttc", "msyhbd.ttf")

_lock = threading.Lock()
_family: str | None = None


def ensureCjkFont() -> str:
    """返回可用的中文字体家族名（首次调用时探测并注册）。"""
    global _family
    if _family is not None:
        return _family
    with _lock:
        if _family is None:
            _family = _register()
        return _family


def _register() -> str:
    regular = _resolveFontPath(_FONT_PATH_ENV, _DEFAULT_BASENAMES)
    if regular is None:
        logger.info("未找到 PDF 中文字体文件，回落 %s", _FALLBACK_FONT_NAME)
        _registerCidFallback()
        return _FALLBACK_FONT_NAME

    bold = _resolveFontPath(_BOLD_FONT_PATH_ENV, _DEFAULT_BOLD_BASENAMES)
    try:
        pdfmetrics.registerFont(_loadFont(_FAMILY, regular))
        if bold is not None:
            pdfmetrics.registerFont(_loadFont(f"{_FAMILY}-Bold", bold))
    except Exception:
        # 字体文件可能是别的 TrueType（不是字体集合）、也可能损坏；都回落
        logger.warning(
            "PDF 中文字体加载失败：%s，回落 %s", regular, _FALLBACK_FONT_NAME, exc_info=True
        )
        _registerCidFallback()
        return _FALLBACK_FONT_NAME

    # 雅黑没有斜体变体：斜体/粗斜体一律指向正体（或粗体），否则 <i> 找不到字体
    boldName = f"{_FAMILY}-Bold" if bold is not None else _FAMILY
    pdfmetrics.registerFontFamily(
        _FAMILY, normal=_FAMILY, bold=boldName, italic=_FAMILY, boldItalic=boldName
    )
    logger.info("PDF 中文字体：%s（粗体：%s）", regular, bold or "同正体")
    return _FAMILY


def _resolveFontPath(envName: str, basenames: tuple[str, ...]) -> Path | None:
    """环境变量优先；显式路径无效时继续探测默认目录（只警告，不报错）。"""
    explicit = os.environ.get(envName, "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
        logger.warning("%s 指向的文件不存在：%s", envName, explicit)

    for directory in _DEFAULT_DIRS:
        for basename in basenames:
            candidate = Path(directory) / basename
            if candidate.is_file():
                return candidate
    return None


def _loadFont(name: str, path: Path) -> TTFont:
    """``.ttc`` 是字体集合，雅黑的 regular/bold 分别在第 0 个子字体。"""
    return TTFont(name, str(path), subfontIndex=0)


def _registerCidFallback() -> None:
    # 重复注册不报错，但仍兜一层：字体注册的任何异常都不该让导出失败
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_FALLBACK_FONT_NAME))
    except Exception:
        logger.debug("STSong-Light 注册跳过", exc_info=True)
