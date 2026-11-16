"""P4 知识检索的**确定性词项匹配**（无 LLM）。

为什么这层值得单独钉住：

1. **可复现**：同一问题 + 同一发布快照 ⇒ 同一批证据。G5 的「引用可回溯」
   建立在「同一问题重跑得到同一结果」之上。
2. **可解释**：每条命中都带**命中的词项**，调用方能看懂为什么命中。
3. **不碰散文**：只匹配**名字与别名**，不匹配 description / businessDefinition
   —— 散文命中噪声太大，且同一段散文会被几十个条目共享 ⇒ 排序失去区分度。

⚠️ 本文件在 `unit/` 下 ⇒ 允许新建（CI 的 unit 分片按路径散列，新文件安全）。
"""

from __future__ import annotations

from app.services.knowledge.matching import (
    TermSpec,
    matchTerms,
    normalizeText,
    sortScored,
    splitIdentifier,
)


def test_cjkAliasMatchesBySubstring() -> None:
    """中文没有词边界 ⇒ 子串包含即命中。"""
    matched = matchTerms("交付延迟最高的供应商？", (TermSpec("供应商", 4),))

    assert [item.text for item in matched] == ["供应商"]


def test_asciiTermMatchesOnWordBoundaryOnly() -> None:
    """ASCII 词项必须卡词边界 ⇒ 'po' 不许命中 'portfolio'（子串但不是词）。"""
    assert matchTerms("portfolio list", (TermSpec("po", 3),)) == ()
    assert matchTerms("PO 交付延迟", (TermSpec("po", 3),)) != ()


def test_camelIdentifierMatchesWhenAllTokensPresent() -> None:
    """复合标识符允许**全词命中**（purchase + order 都在问题里）⇒ 权重降一档。"""
    matched = matchTerms("purchase order delivery", (TermSpec("PurchaseOrder", 3),))

    assert len(matched) == 1
    assert matched[0].weight == 2


def test_singleCharacterTermsAreIgnored() -> None:
    """单字词项噪声太大（'料' 能命中任何含'料'的问题）⇒ 不参与匹配。"""
    assert matchTerms("供应商与物料", (TermSpec("料", 3),)) == ()
    assert matchTerms("供应商与物料", (TermSpec("p", 3),)) == ()


def test_matchedTermsAreDeduplicatedByText() -> None:
    """同一个别名写两遍不加分（否则别名多的条目会虚高）。"""
    matched = matchTerms("供应商", (TermSpec("供应商", 4), TermSpec("供应商", 4)))

    assert len(matched) == 1


def test_emptyQuestionMatchesNothing() -> None:
    assert matchTerms("", (TermSpec("供应商", 4),)) == ()
    assert matchTerms("   ", (TermSpec("供应商", 4),)) == ()


def test_normalizeTextTrimsAndLowercases() -> None:
    assert normalizeText("  Supplier  ") == "supplier"


def test_splitIdentifierSplitsCamelSnakeAndKebab() -> None:
    assert splitIdentifier("PurchaseOrder") == ("purchase", "order")
    assert splitIdentifier("purchase_order") == ("purchase", "order")
    assert splitIdentifier("purchase-order") == ("purchase", "order")
    assert splitIdentifier("采购订单") == ("采购订单",)


def test_sortScoredIsDeterministicOnTie() -> None:
    """同分必须按 key 兜底排序 ⇒ 输出顺序可复现（否则测试与试点报告都会飘）。"""
    items = [
        (5, "B", object()),
        (5, "A", object()),
        (9, "C", object()),
    ]

    ordered = sortScored(items)

    assert [(score, key) for score, key, _ in ordered] == [(9, "C"), (5, "A"), (5, "B")]
