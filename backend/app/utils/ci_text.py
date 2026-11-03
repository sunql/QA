"""CI 报告文本的**净化**与**脱敏**（纯函数，无网络、无 IO）。

为什么要单独一个模块：这两道工序被 `ci_gh`（gh 适配层）、`ci_surface`
（CI 相关面判定）、`ci_failure_set`（对差工具）共用 ⇒ 放在一起才能保证
**脱敏规则只有一份**（两处各写一份必然漂移 ⇒ 漏脱敏）。

两道工序的**顺序不能换**：

1. 先剔控制字符（`sanitizeAndRedact` / `renderReport` 的逐字段净化）——
   否则攻击者可以用控制字符把 `=` 或 `:` 拆开、骗过脱敏正则；
2. 再脱敏。

⚠️ 关键词类脱敏正则**不能**用 `\\b` 起头：`_` 是单词字符 ⇒ `POSTGRES_PASSWORD=`
的 `PASSWORD` 前面是 `_`，`\\b` 匹配不到 ⇒ 实测 `POSTGRES_PASSWORD=ci-postgres-only`
原样通过（= 假脱敏）。

⚠️ 也不能用 `[=:]` 无差别通配：`app/tests/unit/test_password.py::test_x` 这类
**合法失败用例 ID** 里的 `::` 会被当成「键值分隔符」⇒ 把用例 ID 截断掉
⇒ 两次 run 的 ID 不一致 ⇒ 对差出假差异。
⇒ 冒号形态用 `(?!:)` 排除 `::`，并要求分隔符前不是路径分隔符。
"""

from __future__ import annotations

import re

# C0 / C1 控制字符（含 ESC）。报告里的一切外部字符串都要过这道净化。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# 报告里单个字段的最大长度
_MAX_REPORT_FIELD_LEN = 200

# 会出现在凭据名里的关键词。
# ⚠️ `url` / `dsn` 也在列：`DATABASE_URL=postgresql://user:pw@host` 整条都该打码，
# 只打码 userinfo 会把 host:port 留在报告里。
_SECRET_KEYWORDS = (
    r"token|secret|password|passwd|pwd|api[_-]?key|access[_-]?key|authorization|url|dsn"
)

# gh stderr / 报告正文里可能出现的凭据形态（按「形态」分条，不做宽泛通配）。
_SECRET_PATTERNS = (
    # GitHub token / PAT
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    # 赋值形态：`KEY=value`（含 `POSTGRES_PASSWORD=…` 这类带前缀的）
    re.compile(
        rf"(?i)[A-Za-z0-9_]*(?:{_SECRET_KEYWORDS})[A-Za-z0-9_]*[ \t]*=[ \t]*\S+"
    ),
    # 冒号形态：`KEY: value`（含 `Authorization: Bearer …`）
    # ⚠️ `(?!:)` 排除 pytest nodeid 的 `::`；`[^\n]*` 打到行尾，避免只打码
    # `Bearer` 而把后面的 token 留在报告里。
    re.compile(
        rf"(?i)[A-Za-z0-9_]*(?:{_SECRET_KEYWORDS})[A-Za-z0-9_]*[ \t]*:(?!:)[^\n]*"
    ),
    # JSON 形态：`"token": "…"`
    re.compile(
        rf"(?i)\"(?:{_SECRET_KEYWORDS})\"[ \t]*:[ \t]*\"?[^\"\s,}}]+"
    ),
    # URL 内嵌凭据：`scheme://user:password@host`
    re.compile(r"://[^\s/:@]+:[^\s/@]+@"),
    # Azure Blob SAS 签名
    re.compile(r"(?i)\bsig=[A-Za-z0-9%+/=_-]{10,}"),
)


def stripControlChars(text: str) -> str:
    """只剔控制字符（**不截断**）。

    需要「不截断」的场景：脱敏前先剔控制字符，否则长 stderr 会在脱敏前
    就被砍掉（先截断会让脱敏看到半截凭据）。
    """
    return _CONTROL_CHARS.sub("", text)


def sanitizeForReport(text: str) -> str:
    """把来自日志 / workflow 的字符串净化后再打印。

    日志内容由 PR 作者可控（测试里的 `print`）⇒ 可能含 ANSI / CSI / OSC 序列：
    - OSC 52 可改写维护者终端的剪贴板；
    - CSI 序列（`\\x1b[2K\\r`）可擦除报告里已打印的行 ⇒ **伪造出「结论：可合并」**。

    ⇒ 报告里的一切外部字符串都必须过这道净化。
    """
    cleaned = stripControlChars(text)
    if len(cleaned) > _MAX_REPORT_FIELD_LEN:
        cleaned = cleaned[:_MAX_REPORT_FIELD_LEN] + "…（已截断）"
    return cleaned


def redactSecrets(text: str) -> str:
    """把可能出现的凭据打码。

    job-logs 端点会 302 到 Azure Blob 的 **SAS 签名 URL**（`?sig=…`，有效期内的
    临时凭据）；`GH_DEBUG=api` 时 gh 还会 dump 请求头；
    日志里也常见 `POSTGRES_PASSWORD=…` / `DATABASE_URL=postgresql://user:pw@host`。
    ⇒ 落进报告前统一打码。
    """
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[已脱敏]", cleaned)
    return cleaned


def sanitizeAndRedact(text: str) -> str:
    """净化（剔控制字符）**再**脱敏。

    两道工序的顺序不能换：先剔控制字符（否则攻击者可以用控制字符把
    `=` 或 `:` 拆开、骗过脱敏正则），再脱敏。
    """
    return redactSecrets(stripControlChars(text))
