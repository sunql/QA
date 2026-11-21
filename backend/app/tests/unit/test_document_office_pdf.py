"""Office → PDF 转换的守卫（预览保真的关键路径）。

判据要点：
1. **未安装 LibreOffice ⇒ None**（调用方退回 HTML 渲染，而不是 500）；
2. **超时 ⇒ None**（巨表/坏文件不能把请求卡死）；
3. **没产出 PDF ⇒ None**（LibreOffice 对坏文件会「成功退出但不产出」）；
4. 产出的不是 PDF 魔数 ⇒ None（防止把半截文件当 PDF 返回）；
5. 缓存对象名带**转换器版本**（升级 LibreOffice 后旧缓存自动失效）。
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app.services import document_office_pdf as officePdf
from app.services.document_preview_cache import cacheObjectName


def test_officePdfEnabled_readsFlag(monkeypatch) -> None:
    monkeypatch.delenv("OFFICE_PDF_PREVIEW", raising=False)
    assert officePdf.officePdfEnabled() is True

    for off in ("0", "false", "False"):
        monkeypatch.setenv("OFFICE_PDF_PREVIEW", off)
        assert officePdf.officePdfEnabled() is False

    monkeypatch.setenv("OFFICE_PDF_PREVIEW", "1")
    assert officePdf.officePdfEnabled() is True


def test_convertReturnsNoneWhenSofficeMissing(monkeypatch) -> None:
    monkeypatch.setattr(officePdf.shutil, "which", lambda _name: None)

    assert officePdf.convertToPdf(b"x", "a.xlsx") is None


def test_convertReturnsNoneOnTimeout(monkeypatch) -> None:
    monkeypatch.setattr(officePdf.shutil, "which", lambda _name: "/usr/bin/soffice")

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=1)

    monkeypatch.setattr(officePdf.subprocess, "run", _timeout)

    assert officePdf.convertToPdf(b"x", "a.xlsx", timeoutSeconds=1) is None


def test_convertReturnsNoneWhenNoPdfProduced(monkeypatch) -> None:
    """LibreOffice 对坏文件常常 `rc=0` 但**不产出** PDF。"""
    monkeypatch.setattr(officePdf.shutil, "which", lambda _name: "/usr/bin/soffice")
    monkeypatch.setattr(
        officePdf.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )

    assert officePdf.convertToPdf(b"x", "a.xlsx") is None


def test_convertRejectsNonPdfOutput(monkeypatch, tmp_path) -> None:
    """产出的文件不是 PDF（魔数不对）⇒ None，不把垃圾当 PDF 返回。"""
    monkeypatch.setattr(officePdf.shutil, "which", lambda _name: "/usr/bin/soffice")

    def _fakeRun(command, **_kwargs):
        outDir = command[command.index("--outdir") + 1]
        from pathlib import Path

        (Path(outDir) / "a.pdf").write_bytes(b"NOT-A-PDF")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(officePdf.subprocess, "run", _fakeRun)

    assert officePdf.convertToPdf(b"x", "a.xlsx") is None


def test_convertReturnsPdfBytes(monkeypatch) -> None:
    monkeypatch.setattr(officePdf.shutil, "which", lambda _name: "/usr/bin/soffice")

    def _fakeRun(command, **_kwargs):
        outDir = command[command.index("--outdir") + 1]
        from pathlib import Path

        (Path(outDir) / "a.pdf").write_bytes(b"%PDF-1.7 fake")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(officePdf.subprocess, "run", _fakeRun)

    assert officePdf.convertToPdf(b"x", "a.xlsx") == b"%PDF-1.7 fake"


def test_convertUsesIsolatedProfile(monkeypatch) -> None:
    """必须用独立 UserInstallation —— 否则并发转换会互相锁死。"""
    monkeypatch.setattr(officePdf.shutil, "which", lambda _name: "/usr/bin/soffice")
    seen: dict = {}

    def _fakeRun(command, **_kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(officePdf.subprocess, "run", _fakeRun)

    officePdf.convertToPdf(b"x", "a.xlsx")

    command = seen["command"]
    assert any(str(part).startswith("-env:UserInstallation=") for part in command)
    assert "--headless" in command


def test_cacheKeyCarriesConverterVersion() -> None:
    a = cacheObjectName("abcdef123456", "lo1")
    b = cacheObjectName("abcdef123456", "lo2")

    assert a.startswith("previews/lo1/ab/")
    assert a.endswith("abcdef123456.pdf")
    assert a != b, "升级转换器后必须换键（否则旧缓存永远命中）"


@pytest.mark.asyncio
async def test_convertAsyncReturnsNoneWithoutSoffice(monkeypatch) -> None:
    monkeypatch.setattr(officePdf.shutil, "which", lambda _name: None)

    assert await officePdf.convertToPdfAsync(b"x", "a.xlsx") is None
