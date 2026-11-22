"""Office → PDF 转换（让 Excel/Word/PPT 预览**保真**）。

为什么需要这一层：服务端把 Office 渲染成 HTML 表格，**列宽、合并单元格、配色、
条件格式、打印区域全丢**——用户反馈「预览的格式跟实际差别太大」。
转成 PDF 交给浏览器原生查看器，保真度 100%、可缩放、可翻页。

三条工程约束（不做会出事）：

1. **缓存**：LibreOffice 转换是 CPU 密集的（大表几秒到几十秒）⇒ 转换结果按
   ``content_hash`` 存进对象存储，第二次起直接命中。缓存键带转换器版本号，
   升级 LibreOffice 后旧缓存自动失效。
2. **隔离**：``soffice`` 默认用共享 user profile，**并发会互相锁死**（第二个进程
   直接报 "another instance is running"）⇒ 每次转换用独立的
   ``-env:UserInstallation`` 目录。
3. **有界**：转换可能因文件损坏/巨表卡死 ⇒ 超时强杀 + 全局并发上限（默认 2），
   失败返回 None 由调用方**退回 HTML 渲染**（降级不崩）。

开关：``OFFICE_PDF_PREVIEW=0`` 可整体关闭（回到 HTML 渲染）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: 转换器版本（进缓存键：升级 LibreOffice 后旧缓存自动失效）。
CONVERTER_VERSION = "lo1"

#: 单次转换超时（秒）。19MB 的 8-sheet xls 实测在 10-30s 量级。
DEFAULT_TIMEOUT_SECONDS = 120

#: 并发上限：LibreOffice 吃 CPU，并发太多会把整机拖垮（也让所有请求一起超时）。
MAX_CONCURRENT_CONVERSIONS = 2

_semaphore: asyncio.Semaphore | None = None


def _getSemaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)
    return _semaphore


def officePdfEnabled() -> bool:
    """开关（默认开）。``OFFICE_PDF_PREVIEW=0`` ⇒ 回到 HTML 渲染。"""
    return os.environ.get("OFFICE_PDF_PREVIEW", "1").strip() not in {"0", "false", "False"}


def sofficeBinary() -> str | None:
    """找 soffice 可执行文件；未安装 ⇒ None（调用方退回 HTML）。"""
    for candidate in ("soffice", "libreoffice"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def convertToPdf(
    content: bytes,
    filename: str,
    *,
    timeoutSeconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes | None:
    """Office 字节 → PDF 字节；失败/超时/未安装 ⇒ None。

    **同步**函数（跑在 ``asyncio.to_thread`` 里）：LibreOffice 是子进程，
    await 不了，阻塞在事件循环里会让整个服务卡住。
    """
    binary = sofficeBinary()
    if binary is None:
        logger.warning("Office→PDF 不可用：未安装 libreoffice")
        return None

    suffix = Path(filename).suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="officepdf-") as workDir:
        work = Path(workDir)
        source = work / f"source{suffix}"
        source.write_bytes(content)
        outDir = work / "out"
        outDir.mkdir()
        # 独立 profile ⇒ 避免并发时 "another instance is running" 互锁
        profile = work / "profile"
        profile.mkdir()
        command = [
            binary,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nologo",
            "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outDir),
            str(source),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeoutSeconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Office→PDF 超时（%ss）：filename=%s size=%d",
                timeoutSeconds,
                filename,
                len(content),
            )
            return None
        except Exception:  # noqa: BLE001 —— 子进程启动失败也要降级，不炸预览
            logger.exception("Office→PDF 启动失败：filename=%s", filename)
            return None

        produced = list(outDir.glob("*.pdf"))
        if not produced:
            logger.warning(
                "Office→PDF 未产出 PDF：filename=%s rc=%s stderr=%s",
                filename,
                completed.returncode,
                (completed.stderr or b"")[:500],
            )
            return None
        data = produced[0].read_bytes()
        if not data.startswith(b"%PDF"):
            logger.warning("Office→PDF 产出的不是 PDF：filename=%s", filename)
            return None
        return data


async def convertToPdfAsync(
    content: bytes, filename: str, *, timeoutSeconds: float = DEFAULT_TIMEOUT_SECONDS
) -> bytes | None:
    """带并发上限的异步包装。"""
    async with _getSemaphore():
        return await asyncio.to_thread(
            convertToPdf, content, filename, timeoutSeconds=timeoutSeconds
        )
