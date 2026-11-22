"""文本质量**提示**指标（仅供人工参考，**不参与任何自动判罚**）。

⚠️ 设计变更（2026-11-22，用户裁定）：是否参与 AI 上下文由**人工开关**决定
（`document_catalog.ai_context_enabled`）。本模块只给体检脚本提供"可疑"信号，
**不写库、不拦截检索**——自动判定实测反复误判（中文文档里的编码会让大写词
占比冲到 90%+）。
实测（2026-11-22）：`DOC-203B7FEB25B4` 的 chunk 是 `RR REINS 34APaP 多 Res`
这种乱码 —— 注入 prompt 会让模型当真引用，**比检索不到更糟**。

判定口径（**按内容判，不看文件名**——实测英文文件名可能是纯中文正文，
中文文件名也可能是乱码）：

1. 解析失败 / 无文本 ⇒ ``PARSE_FAILED``；
2. 含 Unicode 替换字符（U+FFFD，解码失败痕迹）⇒ ``LOW``；
3. **全大写无元音词占比 > 20%** ⇒ ``LOW``——PDF 内嵌 CID 字体缺 ToUnicode 时，
   抽出来正是这种"大写乱码词"（实测样本 44%–62%，而正常中英文文档均为 **0%**）；
4. 既无中文（< 5%）又非可读 ASCII（< 60%）⇒ ``LOW``（二进制被当文本）；
5. 其余 ``OK``。

实测对照（2026-11-22，dev 8 份）：

=========================  =========  ==============  ======
文档                       中文占比    全大写词占比     判定
=========================  =========  ==============  ======
采购询价管理制度.txt         81.9%      0%（无 ASCII 词） OK
framework_agreement_en     0%         0%              OK
采购与供应商控制程序.pdf     47.1%      61.5%           LOW（混入乱码 token）
纯 ASCII 乱码样本            2.2%      44.4%           LOW
=========================  =========  ==============  ======
"""

from __future__ import annotations

import re

QUALITY_OK = "OK"
QUALITY_LOW = "LOW"
QUALITY_PARSE_FAILED = "PARSE_FAILED"

#: 可写入 catalog 的合法取值（迁移 0165 的 CHECK 同源）。
TEXT_QUALITIES: frozenset[str] = frozenset(
    {QUALITY_OK, QUALITY_LOW, QUALITY_PARSE_FAILED}
)

#: 中文文档的中文占比下限（实测正常文档 27%–82%）。
MIN_CJK_RATIO = 0.05
#: 全大写词（`RR` / `REINS` / `ORN` 这类 token）占比上限。
#: 干净英文实测 0%，乱码样本 44%–62%。
MAX_UPPERCASE_WORD_RATIO = 0.35
#: 判大写比例前的**前置条件**：ASCII 字母占正文比例 ≥ 此值。
#: 实测中文文档只有 2%–8%（都是编码），真乱码文档 15%+。
MIN_LETTERS_SHARE_FOR_CASE_CHECK = 0.10
#: 参与大写判定的最少 ASCII 词数（短文本/缩写密集文本不算）。
MIN_WORDS_FOR_CASE_CHECK = 20
#: ASCII 文档的「字母数字 + 空白」占比下限。
MIN_ASCII_RATIO = 0.60
#: 短于这个长度不做比例判定（一句话的标题不具代表性）。
MIN_SAMPLE_LENGTH = 40

_CJK_RE = re.compile(r"[一-鿿]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_ASCII_TEXT_RE = re.compile(r"[A-Za-z0-9\s\.,;:!?()\[\]{}\-'\"%&/\\<>+=*#@]")
_MOJIBAKE_RE = re.compile(r"[�]")


def hasCjk(value: str) -> bool:
    return bool(_CJK_RE.search(value or ""))


def cjkRatio(text: str) -> float:
    if not text:
        return 0.0
    return len(_CJK_RE.findall(text)) / len(text)


def asciiTextRatio(text: str) -> float:
    if not text:
        return 0.0
    return len(_ASCII_TEXT_RE.findall(text)) / len(text)


def garbledWordRatio(text: str) -> float:
    """**全大写 ASCII 词**占比（PDF CID 乱码的典型形态）。

    为什么不再要求"无元音"：实测 `Res` / `ORN` / `ROR` 这类 token 含元音，
    加上那条会把 44% 漏判成 11%。纯用 ``isupper()`` 才稳：干净英文里 `The`
    是句首 Title Case（不是全大写），正常文档该比例接近 0。
    """
    words = _ASCII_WORD_RE.findall(text)
    if not words:
        return 0.0
    upper = [word for word in words if len(word) >= 2 and word.isupper()]
    return len(upper) / len(words)


def judgeTextQuality(text: str, *, documentName: str = "") -> tuple[str, str]:
    """判定文本质量，返回 ``(quality, reason)``。

    ``documentName`` **不参与判定**（只保留在签名里便于以后做按类型阈值）；
    实测英文文件名可能装纯中文正文，中文文件名也可能装乱码。
    """
    if not text or not text.strip():
        return QUALITY_PARSE_FAILED, "解析无文本（空文档或抽取失败）"
    # Unicode 替换字符是**确定性**的乱码信号（解码失败留下的 U+FFFD）
    replacementRatio = len(_MOJIBAKE_RE.findall(text)) / max(1, len(text))
    if replacementRatio > 0.01:
        return (
            QUALITY_LOW,
            f"含 {replacementRatio:.1%} 的替换字符（解码失败痕迹）",
        )
    if len(text) < MIN_SAMPLE_LENGTH:
        return QUALITY_OK, "文本过短，不做比例判定"

    words = _ASCII_WORD_RE.findall(text)
    lettersShare = sum(len(word) for word in words) / len(text)
    if (
        len(words) >= MIN_WORDS_FOR_CASE_CHECK
        and lettersShare >= MIN_LETTERS_SHARE_FOR_CASE_CHECK
    ):
        upperRatio = garbledWordRatio(text)
        if upperRatio > MAX_UPPERCASE_WORD_RATIO:
            return (
                QUALITY_LOW,
                f"ASCII 占 {lettersShare:.0%} 且全大写词占 {upperRatio:.0%}"
                "（PDF 字体缺 ToUnicode 的典型乱码）",
            )

    cjk = cjkRatio(text)
    asciiRatio = asciiTextRatio(text)
    if cjk < MIN_CJK_RATIO and asciiRatio < MIN_ASCII_RATIO:
        return QUALITY_LOW, f"既非中文（{cjk:.1%}）也非可读 ASCII（{asciiRatio:.1%}）"
    return QUALITY_OK, f"中文占比 {cjk:.1%}／可读字符占比 {asciiRatio:.1%}"


def isSuspect(quality: str | None) -> bool:
    """是否**可疑**（供体检报告高亮）；**不用于拦截检索**。"""
    return quality is not None and quality != QUALITY_OK
