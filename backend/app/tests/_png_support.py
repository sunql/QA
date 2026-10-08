"""测试用 PNG 生成器（纯标准库 zlib + struct）。

为什么自己拼 PNG 而不是用 Pillow：Pillow 只是 ``pytesseract`` / ``pdfplumber``
带进来的**传递依赖**，项目从未显式声明。测试依赖传递依赖，等于把「哪天上游换实现」
变成测试红灯 —— 而这张图只是要满足「reportlab 能解出的真 PNG」，用 zlib 拼一个
单色图 15 行就够。

只覆盖测试需要的形状：8bit RGB、无交错、无滤波、单 IDAT。
"""

from __future__ import annotations

import struct
import zlib

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    """PNG chunk = 长度(4) + 类型(4) + 数据 + CRC32(类型+数据)(4)。"""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def solidPng(width: int, height: int, rgb: tuple[int, int, int] = (0, 217, 192)) -> bytes:
    """生成 width×height 的纯色 RGB PNG 字节。"""
    if width < 1 or height < 1:
        raise ValueError("width/height 必须 >= 1")
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit, colorType=2(RGB)
    # 每条扫描线前置一个 filter 字节（0 = None），reportlab/Pillow 都接受
    row = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(row * height)
    return _PNG_MAGIC + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def pngDeclaringSize(width: int, height: int) -> bytes:
    """一张 IHDR **声明**为 width×height、实际像素数据仍是 8×8 的 PNG。

    用来测「解压炸弹」：真实炸弹（12000×12000 纯色）只有 580 KB，但造它要先把
    4.3 亿字节的扫描线在内存里铺开再压缩 —— 测试为验证一个内存闸而先吃掉 400 MB
    是荒唐的。尺寸闸只看 IHDR 里声明的宽高、在把字节交给解码器**之前**就判，所以
    一张「声明得很大、数据很小」的 PNG 是**恰好等价**的输入：闸按声明值拒它，
    与真实炸弹被拒的理由逐字相同。
    """
    if width < 1 or height < 1:
        raise ValueError("width/height 必须 >= 1")
    png = bytearray(solidPng(8, 8))
    png[16:20] = struct.pack(">I", width)
    png[20:24] = struct.pack(">I", height)
    # 改了 IHDR 数据就必须重算它的 CRC，否则连「合法 PNG」都不是，
    # 测出来的就只是「CRC 校验」而不是「尺寸闸」
    png[29:33] = struct.pack(">I", zlib.crc32(bytes(png[12:29])) & 0xFFFFFFFF)
    return bytes(png)
