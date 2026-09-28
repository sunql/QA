"""内容派生 page_id 的纯函数测试（feat-wiki-dedup P1）。

不触 DB / 不触网络，按 Harness/rules/测试规范.md「例外（仅限无 IO 的纯逻辑）」
条款可直接单测。真实 PG + 完整 API 链路的验证在
``app/tests/integration/test_wiki_dedup_api.py``。

这里**必须**用字面量锁死派生公式（而不是只断言「两次调用相等」）：只断言相等
挡不住把 sha256 换成 md5 之类的「确定但不同」的改写 —— 那会让改造前已落库的
page_id 与改造后算出的不一致，重复项照样入库，而测试全绿。

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
        .venv/bin/pytest app/tests/unit/test_wiki_page_id.py -q
"""

from __future__ import annotations

from app.infrastructure.object_storage import hashContent
from app.services.wiki_page_service import (
    contentHashOf,
    generatePageId,
)

# 夹具取值与 test_wiki_api.py 的 _createPage 默认值一致，便于两个套件对照
_TITLE = "供应商准入规则"
_CONTENT = "注册资本 >= 1000 万"


def test_same_inputs_produce_same_page_id() -> None:
    """同 (sourceRef, title, content) → 同 ID：这是本次去重的全部依据。"""
    assert generatePageId(_TITLE, "", _CONTENT) == generatePageId(_TITLE, "", _CONTENT)


def test_page_id_is_not_random() -> None:
    """回归：旧实现拼 ``uuid.uuid4().hex[:8]``，同一标题每次不同 → 冲突检查永不命中。

    20 次调用必须收敛到 1 个值。旧实现在这里会拿到 20 个不同的 ID。
    """
    ids = {generatePageId("同一份知识", "", "同样的正文") for _ in range(20)}
    assert len(ids) == 1


def test_page_id_formula_is_pinned_by_literal() -> None:
    """钉死派生公式的**字面输出**（换哈希算法/换分隔符都会打红）。"""
    assert generatePageId(_TITLE, "", _CONTENT) == "PAGE-UNTITLED-7048C5E6"
    assert (
        generatePageId("Supplier Qualification", "", "x")
        == "PAGE-SUPPLIER-QUALIFICATION-C719B4B7"
    )


def test_content_change_changes_page_id() -> None:
    """正文变 = 另一条知识（不再撞号，各建一条）。"""
    assert generatePageId(_TITLE, "", _CONTENT) != generatePageId(
        _TITLE, "", "注册资本 > 2000 万"
    )


def test_source_ref_change_changes_page_id() -> None:
    """来源参与派生。

    同一份文件重跑时 sourceRef 也相同（导入台账记的是同一个来源），故不影响
    去重；两个部门各写一份同样条款则算两条知识。spec §5.2 的公式如此规定，
    边界与代价见 summary 风险段。
    """
    assert generatePageId(_TITLE, "", _CONTENT) != generatePageId(
        _TITLE, "policy.md", _CONTENT
    )


def test_chinese_title_folds_to_untitled_slug() -> None:
    """纯中文标题的 slug 折叠成 ``UNTITLED``。

    钉住既有 ASCII 字符集契约（``_PAGE_ID_SANITIZE`` 只放行
    ASCII 字母/数字/连字符/下划线，中文一个字符都不剩）的后果：中文场景下
    可读性由哈希后缀兜底、slug 恒定。这是 P1 明确接受的代价，不是 bug。
    """
    assert generatePageId(_TITLE, "", _CONTENT).startswith("PAGE-UNTITLED-")
    # 两个不同的中文标题 slug 相同，但整体 ID 不同（标题参与哈希）
    assert generatePageId(_TITLE, "", _CONTENT) != generatePageId(
        "供应商 准入 规则", "", _CONTENT
    )


def test_overlong_title_still_fits_page_id_column() -> None:
    """超长标题截断到 40 字符后，整体长度 = 5 + 40 + 1 + 8 = 54 ≤ VARCHAR(64)。"""
    pageId = generatePageId("x" * 200, "", "c")
    assert len(pageId) == 54
    assert pageId == f"PAGE-{'X' * 40}-D8379DE3"


def test_content_hash_is_sha256_lowercase_hex() -> None:
    """content_hash 写库的原值：64 位小写十六进制（口径与 document_catalog 一致）。"""
    assert (
        contentHashOf(_CONTENT)
        == "5dd995a8688226c1fc01cc593b6bce29b2b1b96fcde0feca24ddaab3a13d4041"
    )
    assert contentHashOf("x") == (
        "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
    )
    assert contentHashOf("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_content_hash_delegates_to_hashContent() -> None:
    """``contentHashOf`` 必须转调 ``hashContent``，等价性是结构保证而非约定。

    两处 sha256 若各自演化不会报错、只会让「同 ID 同内容 → 跳过」的比对永远
    不等、重复项静默入库；故 ``contentHashOf`` 退化为 ``hashContent`` 的薄包装，
    本断言钉死二者逐字节等价（str → utf-8 bytes）。
    """
    assert contentHashOf(_CONTENT) == hashContent(_CONTENT.encode("utf-8"))


def test_content_hash_is_not_the_page_id_suffix() -> None:
    """``content_hash`` 与 page_id 后缀**不同源**：全量 sha256(content) vs
    身份哈希（长度前缀拼接 source_ref/title/content 后 sha256）的前 8 位。

    回归点：把两者混用不会报错，但会让「同 ID 同内容 → 跳过」的判定永远不
    成立（或永远成立），重复项静默入库。
    """
    assert not generatePageId(_TITLE, "", _CONTENT).endswith(
        contentHashOf(_CONTENT)[:8].upper()
    )
