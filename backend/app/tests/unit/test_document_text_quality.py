"""入库文本质量判定守卫（「文档注入 LLM」的前置闸）。

判据要点（**按内容判，不看文件名**——实测英文文件名可能装纯中文正文）：
1. 空文本/解析失败 ⇒ PARSE_FAILED；
2. **ASCII 占比 ≥10% 且全大写词占比 > 35%** ⇒ LOW（PDF CID 缺 ToUnicode）；
   两个条件缺一不可——中文文档 ASCII 词只占 2%–8%（都是编码），只看大写比例
   会把正常中文文档全部误杀（本批实测踩到）；
3. 纯中文文档（无 ASCII 词）不会被大写规则误伤；
4. 替换字符（U+FFFD）是确定性乱码信号；
5. 既非中文也非可读 ASCII ⇒ LOW（二进制被当文本）；
6. `isSuspect` **只用于体检报告高亮**——是否参与 AI 上下文由**人工开关**决定。
"""

from __future__ import annotations

import pytest

from app.services.document_text_quality import (
    QUALITY_LOW,
    QUALITY_OK,
    QUALITY_PARSE_FAILED,
    asciiTextRatio,
    cjkRatio,
    garbledWordRatio,
    hasCjk,
    isSuspect,
    judgeTextQuality,
)

_NORMAL_CN = (
    "第一条 询价邀请范围：单笔采购金额超过五万元的，应当向不少于三家合格供应商"
    "发出询价邀请。第二条 报价期限：报价截止时间自发询价之日起不得少于三个工作日。"
)
#: 实测样本形态：中文标题里混入大写乱码 token（PDF CID 缺 ToUnicode）
#: 实测形态：中文标题里混入大写乱码 token。
#: 样本要**够长**（真实 PDF 1300+ 词）——短样本会因「词数不足」跳过判定。
_MIXED_GARBLED = (
    "浙江泰鸿万立科技股份有限公司 管理标准之 程序文件 QMS (Iso9001&IATF16949)文件 "
    "=r 程序 文件编号: THO1-QP-40 文件版本: V1 标准化: it Ve FB 2026年6月发布 "
    "秘密等级: 内部公开 oR OU CH 。 盖有红色印章为发行受控文件，请您遵守公司保密规定。"
) * 6
_PURE_GARBLED = "RR REINS 34APaP 多 Res Ea ORN, ROR RERTAl ines " * 4


def test_emptyTextIsParseFailed() -> None:
    assert judgeTextQuality("", documentName="制度.pdf")[0] == QUALITY_PARSE_FAILED
    assert judgeTextQuality("   ", documentName="制度.pdf")[0] == QUALITY_PARSE_FAILED


def test_normalChineseDocumentIsOk() -> None:
    quality, _reason = judgeTextQuality(_NORMAL_CN)

    assert quality == QUALITY_OK


def test_englishFilenameWithChineseContentIsOk() -> None:
    """实测：sample_policy.txt 是英文名但纯中文正文 —— 不能被文件名推断误杀。"""
    quality, _reason = judgeTextQuality(_NORMAL_CN, documentName="sample_policy.txt")

    assert quality == QUALITY_OK


def test_mixedGarbledDocumentIsLow() -> None:
    """实测样本：中文正文里混入大写乱码 token ⇒ LOW（不注入 LLM）。"""
    quality, reason = judgeTextQuality(_MIXED_GARBLED)

    assert quality == QUALITY_LOW
    assert "全大写" in reason


def test_chineseDocWithManyCodesIsNotKilled() -> None:
    """中文文档里的编码（TH-MDM-01 / SUS304）不该触发乱码判定。"""
    chineseWithCodes = (
        "主数据表 TH-MDM-01（V1.0）由 MDM 系统维护；物料 SUS304 的不锈钢板，"
        "编码 AJE0000781，供应商 B024；质量体系 ISO9001 与 IATF16949 均适用，"
        "相关 ERP/QMS 字段见附录 A 至 C 部分。"
    )

    quality, _ = judgeTextQuality(chineseWithCodes)

    assert quality == QUALITY_OK


def test_pureAsciiGarbageIsLow() -> None:
    quality, reason = judgeTextQuality(_PURE_GARBLED)

    assert quality == QUALITY_LOW
    assert "全大写" in reason


def test_englishDocumentIsNotKilled() -> None:
    """英文文档判 ASCII 可读率，不能被「中文占比」误杀。"""
    english = (
        "The supplier shall deliver the goods within thirty (30) days after "
        "receipt of the purchase order, unless otherwise agreed in writing."
    )

    quality, _ = judgeTextQuality(english)

    assert quality == QUALITY_OK


def test_binaryLookingContentIsLow() -> None:
    quality, _reason = judgeTextQuality("\x00\x01\x02" * 40)

    assert quality == QUALITY_LOW


def test_replacementCharsAreLow() -> None:
    quality, reason = judgeTextQuality("文档�" * 40)

    assert quality == QUALITY_LOW
    assert "替换字符" in reason


def test_shortTextSkipsRatioCheck() -> None:
    quality, _ = judgeTextQuality("附件一")

    assert quality == QUALITY_OK


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        (None, False),
        (QUALITY_OK, False),
        (QUALITY_LOW, True),
        (QUALITY_PARSE_FAILED, True),
    ],
)
def test_isSuspect_marksOnlyBadOnes(quality: str | None, expected: bool) -> None:
    assert isSuspect(quality) is expected


def test_garbledWordRatio_discriminates() -> None:
    """判别指标本身：干净中英文 0%，乱码样本 > 35%。"""
    english = "The supplier shall deliver the goods within thirty days."
    assert garbledWordRatio(english) == 0.0
    assert garbledWordRatio(_MIXED_GARBLED) > 0.35
    assert garbledWordRatio(_PURE_GARBLED) > 0.35


def test_helpers() -> None:
    assert hasCjk("采购制度") is True
    assert hasCjk("policy") is False
    assert cjkRatio("采购ab") == pytest.approx(0.5)
    assert asciiTextRatio("abc 123") == pytest.approx(1.0)
