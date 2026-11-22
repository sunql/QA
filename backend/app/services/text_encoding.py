"""文本文件编码探测（预览与入库切块共用）。

背景（2026-11-22）：`.txt` 一律按 UTF-8 解码，而国内 Windows 导出的纯文本
常是 **GBK/GB18030** ⇒ 预览乱码、**入库切块也是乱码**（检索命中一堆乱码片段，
比预览乱码更糟）。

探测顺序（**确定性**，不依赖第三方猜测库——同样字节永远得到同一结果，
测试与线上一致）：

1. **BOM**：``utf-8-sig`` / ``utf-16`` / ``utf-32``（BOM 是最强证据）；
2. **严格 UTF-8**：能严格解码就一定是 UTF-8；
3. **GB18030**：是 GBK/GB2312 的超集，覆盖国内 Windows 文本的绝大多数；
4. **Big5**：繁体中文常见；
5. **latin-1**：兜底（任何字节都能解，不会抛）。

⚠️ 顺序有讲究：GB18030 几乎能"解"很多字节序列 ⇒ 必须**先试 UTF-8**，
否则英文/UTF-8 文本会被错误地当 GBK。
"""

from __future__ import annotations

#: 探测结果（编码名 + 是否命中 BOM）。
BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

#: 无 BOM 时的尝试顺序。
FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gb18030", "big5", "latin-1")


def detectEncoding(content: bytes) -> str:
    """探测最可能的编码（**总是**返回一个可用编码）。"""
    for bom, encoding in BOM_ENCODINGS:
        if content.startswith(bom):
            return encoding
    for encoding in FALLBACK_ENCODINGS:
        try:
            content.decode(encoding)
            return encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"


def decodeTextBytes(content: bytes) -> tuple[str, str]:
    """解码文本字节 → ``(文本, 编码)``；解码失败回退 ``errors="replace"``。"""
    encoding = detectEncoding(content)
    try:
        return content.decode(encoding), encoding
    except (UnicodeDecodeError, LookupError):  # pragma: no cover - 理论上不可达
        return content.decode("utf-8", errors="replace"), "utf-8"


def decodeText(content: bytes) -> str:
    """只要文本（预览/切块用）。"""
    return decodeTextBytes(content)[0]


def charsetFor(encoding: str) -> str:
    """编码名 → HTTP ``charset`` 值（``utf-8-sig`` ⇒ ``utf-8``）。"""
    if encoding == "utf-8-sig":
        return "utf-8"
    return encoding
