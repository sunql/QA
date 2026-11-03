"""报告文本的净化与脱敏：两道工序必须**都**在，且顺序不能换。

背景
----
报告里的每一段外部文本（job 名、日志行、gh stderr）都由 PR 作者可控 ⇒

- **净化**（剔控制字符）：防 CSI 序列擦掉已打印的「结论：可合并」行、
  防 OSC 52 改写维护者终端的剪贴板；
- **脱敏**（打码凭据）：防 SAS 签名 URL / token / DSN 落进报告。

顺序不能换：先剔控制字符（否则攻击者可以用控制字符把 `=` 拆开骗过脱敏正则），
再脱敏。
"""

from __future__ import annotations

from app.utils.ci_text import (
    redactSecrets,
    sanitizeForReport,
    stripControlChars,
)

# --------------------------------------------------------------------------
# 净化
# --------------------------------------------------------------------------


def test_sanitizeForReport_strips_control_sequences() -> None:
    """控制字符（含 ESC）必须被剔掉。"""
    raw = "结论：\x1b[2K\r可合并\x07"

    cleaned = sanitizeForReport(raw)

    assert "\x1b" not in cleaned
    assert "\r" not in cleaned
    assert "\x07" not in cleaned
    assert "可合并" in cleaned


def test_sanitizeForReport_truncates_long_fields() -> None:
    """超长字段必须截断（防日志把报告撑爆）。"""
    cleaned = sanitizeForReport("x" * 5000)

    assert len(cleaned) < 5000
    assert "已截断" in cleaned


def test_stripControlChars_does_not_truncate() -> None:
    """`stripControlChars` 只剔控制字符、**不截断**。

    脱敏前必须用它（而不是会截断的 `sanitizeForReport`）——
    否则长 stderr 会在脱敏之前就被砍掉，脱敏看到的是半截凭据。
    """
    long_text = "x" * 5000

    assert len(stripControlChars(long_text)) == 5000


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------


def test_redactSecrets_masks_token_and_sas_signatures() -> None:
    """gh 的 stderr 可能带 SAS 签名 URL / token ⇒ 落进报告前必须打码。"""
    raw = (
        "failed: https://blob.core.windows.net/logs?sig=AbCdEf0123456789XYZ&se=2026-11-02\n"
        "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n"
        "password: hunter2"
    )

    cleaned = redactSecrets(raw)

    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in cleaned
    assert "sig=AbCdEf0123456789XYZ" not in cleaned
    assert "hunter2" not in cleaned


def test_redactSecrets_masks_env_style_credentials() -> None:
    """环境变量形态的凭据必须打码。

    `\\b` 前缀对 `POSTGRES_PASSWORD=` **无效**（`_` 是单词字符，`\\b` 不成立）⇒
    这条曾经漏脱敏。判定器与报告都不得把 `PASSWORD=` / `DATABASE_URL=` 原样印出来。
    """
    raw = (
        "POSTGRES_PASSWORD=qa_pg_dev_2026\n"
        "DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/db\n"
        "GITHUB_TOKEN=github_pat_11ABCDEFG0abcdefghijklmnop\n"
    )

    cleaned = redactSecrets(raw)

    assert "qa_pg_dev_2026" not in cleaned
    assert "github_pat_11ABCDEFG0abcdefghijklmnop" not in cleaned
    assert "127.0.0.1:5434" not in cleaned


def test_redactSecrets_masks_bare_key_colon_value_form() -> None:
    """**不带引号、不带 `=`** 的 `key: value` 形态也必须打码。

    上一版把这条形态整个丢了 ⇒ gh stderr 里的 `password: hunter2`
    原样进了报告（相对旧实现是**倒退**）。
    """
    cleaned = redactSecrets("password: hunter2\nAuthorization: Bearer abc123xyz")

    assert "hunter2" not in cleaned
    assert "abc123xyz" not in cleaned


def test_redactSecrets_keeps_legitimate_failure_ids_intact() -> None:
    """脱敏不能误伤**合法的失败用例 ID**。

    `app/tests/unit/test_password.py::test_x` 里含 `password` ⇒ 宽泛的
    `[=:]` 通配会把 ID 截断 ⇒ 两次 run 的 ID 不一致 ⇒ 对差出假差异。
    """
    failureId = "app/tests/unit/test_password.py::test_x"

    assert failureId in redactSecrets(failureId)


def test_redactSecrets_keeps_failure_ids_containing_url_intact() -> None:
    """含 `url` 的失败用例 ID 也不能被误伤（`url` 是脱敏关键词之一）。"""
    failureId = "app/tests/unit/test_url.py::test_x"

    assert failureId in redactSecrets(failureId)


def test_redactSecrets_masks_whole_dsn_including_host() -> None:
    """`DATABASE_URL=…` 必须**整条**打码（含 host:port）。"""
    cleaned = redactSecrets(
        "DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test"
    )

    assert "127.0.0.1:5434" not in cleaned
    assert "qa_metadata_test" not in cleaned


# --------------------------------------------------------------------------
# 顺序约束
# --------------------------------------------------------------------------


def test_sanitizeForReport_removes_control_chars_before_redacting() -> None:
    """顺序不能换：攻击者用控制字符把 `=` 拆开，必须先剔控制字符再脱敏。"""
    raw = "POSTGRES_PASSWORD\x1b=qa_pg_dev_2026"

    cleaned = sanitizeForReport(raw)

    assert "qa_pg_dev_2026" not in cleaned
    assert "\x1b" not in cleaned


def test_sanitizeForReport_redacts_before_truncating() -> None:
    """脱敏必须在截断**之前** ⇒ 横跨截断边界的凭据不会漏出半截。

    旧实现是「净化 → 截断」：一个 260 字符的凭据会在脱敏**之前**被砍到 200
    字符 ⇒ 脱敏正则看到的是半截凭据（`TOKEN=AAAA…` 里的值被截短）
    ⇒ 打码正则仍会命中，但**凭据前缀会随字段一起进报告**；
    而若凭据形态本身跨边界（值里含空格 ⇒ 匹配不到整条）就彻底漏出去。

    ⇒ 唯一正确的顺序是：剔控制字符 → **脱敏** → 截断。
    """
    secretValue = "s3cr3t " + "A" * 250
    raw = f"TOKEN={secretValue}"

    cleaned = sanitizeForReport(raw)

    assert "s3cr3t" not in cleaned
    assert "TOKEN=" not in cleaned


def test_sanitizeForReport_redacts_secrets() -> None:
    """净化入口**必须自带脱敏** —— 否则调用点漏一处就漏一份凭据。"""
    cleaned = sanitizeForReport("gh stderr: password: hunter2")

    assert "hunter2" not in cleaned
