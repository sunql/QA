"""文本编码探测守卫（预览与入库切块共用）。

判据要点：
1. **BOM 最强证据**（UTF-8-SIG / UTF-16 / UTF-32）；
2. **先试 UTF-8**：GB18030 几乎能"解"很多字节序列，顺序错了会把 UTF-8 误判；
3. 国内 Windows 文本（GBK/GB18030）要能正确解出中文；
4. latin-1 兜底：任何字节都不抛；
5. `utf-8-sig` 的 charset 要归一成 `utf-8`（HTTP 头里没有 sig 这种写法）。
"""

from __future__ import annotations

import pytest

from app.services.text_encoding import (
    charsetFor,
    decodeText,
    decodeTextBytes,
    detectEncoding,
)


def test_utf8PlainText() -> None:
    content = "# 采购制度\n中文正文".encode("utf-8")

    text, encoding = decodeTextBytes(content)

    assert encoding == "utf-8"
    assert "采购制度" in text


def test_utf8WithBom() -> None:
    content = "# 采购制度".encode("utf-8-sig")

    text, encoding = decodeTextBytes(content)

    assert encoding == "utf-8-sig"
    assert "采购制度" in text
    # BOM 不能留在正文里
    assert not text.startswith("﻿")


def test_utf16WithBom() -> None:
    content = "采购制度".encode("utf-16")

    text, encoding = decodeTextBytes(content)

    assert encoding.startswith("utf-16")
    assert "采购制度" in text


def test_gbkChineseTextIsDecoded() -> None:
    """国内 Windows 导出的 txt 常是 GBK ⇒ 不能按 UTF-8 解得乱码。"""
    content = "采购询价管理制度 第一条 询价邀请范围".encode("gbk")

    text, encoding = decodeTextBytes(content)

    assert encoding == "gb18030"
    assert "采购询价管理制度" in text


def test_utf8IsPreferredOverGb18030() -> None:
    """顺序：GB18030 能解很多序列 ⇒ 必须**先试 UTF-8**，否则英文/UTF-8 被误判。"""
    content = "中文 mixed ascii 123".encode("utf-8")

    assert detectEncoding(content) == "utf-8"


def test_latin1FallbackNeverRaises() -> None:
    """任意字节都不许抛（预览不能因为编码探测崩掉）。"""
    content = bytes(range(256))

    text, encoding = decodeTextBytes(content)

    assert isinstance(text, str)
    assert encoding


def test_charsetForNormalizesSig() -> None:
    assert charsetFor("utf-8-sig") == "utf-8"
    assert charsetFor("gb18030") == "gb18030"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("纯英文文本", "utf-8"),
        ("繁體中文測試", "utf-8"),
    ],
)
def test_commonChineseCasesStayUtf8(raw: str, expected: str) -> None:
    assert detectEncoding(raw.encode("utf-8")) == expected


def test_decodeTextReturnsOnlyText() -> None:
    assert "采购" in decodeText("采购".encode("gbk"))
