"""PDF 中文字体解析（2026-10-31，D5）。

生产要的是微软雅黑：它会被**嵌入子集**，任何阅读器都能直接显示；而
STSong-Light 是 CID 字体，客户端没装 FontPack 时会显示方框。缺字体文件时
必须原样回落到 STSong-Light —— PDF 导出不能因为少一个字体就整体失败。

用 reportlab 自带的 ``Vera.ttf`` 当替身：真实性的关键是「文件路径 → 注册成
家族 → 能画 PDF」这条链，而不是字体里有没有字形。
"""

import importlib
from pathlib import Path

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from app.services import pdf_export_service, pdf_fonts

# reportlab 自带的 TrueType 字体（backend 依赖里一定有）
VERA_TTF = Path(pdfmetrics.__file__).resolve().parent.parent / "fonts" / "Vera.ttf"

# macOS 自带一批 .ttc；用来验证 subfontIndex 路径。Linux CI 上会跳过。
TTC_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Athelas.ttc",
)


def _reload(monkeypatch: pytest.MonkeyPatch, **env: str):
    """重新导入模块以清掉进程级缓存；环境变量按用例重设。"""
    for key in ("PDF_CJK_FONT_PATH", "PDF_CJK_BOLD_FONT_PATH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(pdf_fonts)


def _drawPdf(path: Path, fontName: str) -> bytes:
    c = canvas.Canvas(str(path))
    c.setFont(fontName, 12)
    c.drawString(72, 720, "PDF Test")
    c.save()
    return path.read_bytes()


def test_env_font_path_is_registered_as_family(monkeypatch):
    module = _reload(monkeypatch, PDF_CJK_FONT_PATH=str(VERA_TTF))

    assert module.ensureCjkFont() == module._FAMILY

    font = pdfmetrics.getFont(module._FAMILY)
    assert Path(font.face.filename).name == "Vera.ttf"


def test_bold_file_is_registered_when_configured(monkeypatch):
    module = _reload(
        monkeypatch,
        PDF_CJK_FONT_PATH=str(VERA_TTF),
        PDF_CJK_BOLD_FONT_PATH=str(VERA_TTF),
    )

    family = module.ensureCjkFont()

    assert pdfmetrics.getFont(f"{family}-Bold").face.filename.endswith("Vera.ttf")
    # 家族映射里 <b> 必须指向已注册的字体，否则 Paragraph 渲染时抛 KeyError
    assert pdfmetrics.getFont(family).face.filename.endswith("Vera.ttf")


def test_missing_font_falls_back_to_builtin_cid(monkeypatch, tmp_path):
    module = _reload(monkeypatch, PDF_CJK_FONT_PATH=str(tmp_path / "nope.ttf"))
    monkeypatch.setattr(module, "_DEFAULT_DIRS", ())

    assert module.ensureCjkFont() == module._FALLBACK_FONT_NAME
    assert _drawPdf(tmp_path / "fallback.pdf", module._FALLBACK_FONT_NAME).startswith(
        b"%PDF-1."
    )


def test_default_probe_finds_msyh_in_fonts_dir(monkeypatch, tmp_path):
    fonts_dir = tmp_path / "fonts"
    fonts_dir.mkdir()
    (fonts_dir / "msyh.ttf").write_bytes(VERA_TTF.read_bytes())

    module = _reload(monkeypatch)
    monkeypatch.setattr(module, "_DEFAULT_DIRS", (str(fonts_dir),))

    assert module.ensureCjkFont() == module._FAMILY


def test_configured_family_can_draw_pdf(monkeypatch, tmp_path):
    module = _reload(monkeypatch, PDF_CJK_FONT_PATH=str(VERA_TTF))
    family = module.ensureCjkFont()

    assert _drawPdf(tmp_path / "font.pdf", family).startswith(b"%PDF-1.")


def test_export_builder_uses_resolved_family(monkeypatch):
    """反向守卫：导出服务不得再硬编码字体名（否则配了雅黑也不生效）。"""
    module = _reload(monkeypatch, PDF_CJK_FONT_PATH=str(VERA_TTF))
    service = importlib.reload(pdf_export_service)

    builder = service.ChatExportPdfBuilder()

    assert builder._styles["body"].fontName == module._FAMILY


@pytest.mark.skipif(
    not any(Path(path).is_file() for path in TTC_CANDIDATES),
    reason="本机没有 .ttc 字体文件",
)
def test_ttc_collection_loads_subfont_zero(monkeypatch, tmp_path):
    ttc = next(path for path in TTC_CANDIDATES if Path(path).is_file())
    module = _reload(monkeypatch, PDF_CJK_FONT_PATH=ttc)

    assert module.ensureCjkFont() == module._FAMILY
    assert _drawPdf(tmp_path / "ttc.pdf", module._FAMILY).startswith(b"%PDF-1.")
