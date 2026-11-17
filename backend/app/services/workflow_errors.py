"""P5 命令执行错误的**净化与归类**（落库前必须过这一关）。

两条纪律（都来自代码审查）：

1. **净化**：适配器抛出的异常消息里可能带**凭据 / PII**（连接串里的密码、
   token、下游返回的整段报文）⇒ 直接落 ``business_command.error_message``
   与审计表 ⇒ 凭据进库、进日志、进导出 ⇒ **不可逆**（落库即须按泄露处理）。
   ⇒ 落库前**先净化**：抹值留键（保留可读性）、控长度、压控制字符。
2. **归类**：**基础设施错误**（DB / 网络 / 超时）与**业务适配器错误**
   （下游明确拒绝）必须可区分 —— 前者「等一等就好」，后者「多半要改代码或
   数据」。混成一句话 ⇒ 运维分不清该等还是该改 ⇒ 处置动作反了。

净化口径（**保守**：宁可多遮，不可漏遮）：

- **控制字符**（换行 / 回车 / 制表 / 不可见字符）⇒ 压成空格：否则错误消息能
  撑成多行 ⇒ **伪造审计行**（破坏审计的可解析性）。
- **URL userinfo**（``scheme://user:pass@host``）⇒ ``scheme://***@host``。
- **键值对形态的凭据**（``password=xxx`` / ``token: xxx``）⇒ 值替换为 ``***``，
  **键名保留**（保留「哪里出的问题」这一信息）。
- **超长** ⇒ 截断并标注（让人知道「被截了」，而不是以为就这么多）。
"""

from __future__ import annotations

import re

from sqlalchemy.exc import SQLAlchemyError

# 落库错误消息的长度上限（``error_message`` 是 ``Text``，但**不设上限**等于
# 让一条异常报文把审计表撑大 ⇒ 必须有界）。
MAX_ERROR_MESSAGE_LEN = 1000

# 错误种类。
ERROR_KIND_ADAPTER = "ADAPTER"
ERROR_KIND_INFRA = "INFRA"

# 错误种类的中文标签（落库消息前缀 ⇒ 巡检时一眼可辨）。
ERROR_LABEL_ADAPTER = "适配器错误"
ERROR_LABEL_INFRA = "基础设施错误"

# 「键 = 值」形态的凭据：键名保留、值抹掉。
# ⚠️ 键名集合刻意偏保守（宁可多遮）：宁可把 ``secret`` / ``authorization``
#    这类无害字段也遮掉，也不能漏掉一个真凭据。
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b("
    r"password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|authorization|auth[_-]?token|"
    r"credential|credentials|session[_-]?id"
    r")(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)

# URL 里的 userinfo（``scheme://user:pass@host``）。
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+(:[^/\s@]*)?@")

# 控制字符（保留 ``\t`` / ``\n`` / ``\r`` 之外的都压掉 ⇒ 其实这三个也要压：
# 多行消息能伪造审计行）。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitizeErrorMessage(
    message: str | None,
    *,
    maxLength: int = MAX_ERROR_MESSAGE_LEN,
) -> str:
    """把错误消息净化成「可落库」的形态。

    口径：控制字符压平 ⇒ URL userinfo 抹值 ⇒ 键值对凭据抹值 ⇒ 截断。
    ``None`` / 空串 ⇒ 空串（调用方据此不写 ``error_message``）。
    """

    if not message:
        return ""
    text = _CONTROL_CHARS.sub(" ", str(message))
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}***@", text)
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = text.strip()
    if len(text) > maxLength:
        text = text[: maxLength - len("…（已截断）")] + "…（已截断）"
    return text


def classifyError(exc: BaseException) -> str:
    """归类异常：基础设施错误 vs 适配器错误。

    - ``SQLAlchemyError`` ⇒ **基础设施**（DB 抖动 / 连接断了 / 锁超时）
      ⇒ 重试有意义（服务恢复即可）。
    - ``TimeoutError``（含 ``asyncio.TimeoutError``）⇒ **基础设施**
      （下游没在期限内应答）。
    - 其余 ⇒ **适配器**（业务拒绝 / 参数错 / 契约不符）。
    """

    if isinstance(exc, (SQLAlchemyError, TimeoutError)):
        return ERROR_KIND_INFRA
    return ERROR_KIND_ADAPTER


def describeError(exc: BaseException, *, maxLength: int = MAX_ERROR_MESSAGE_LEN) -> tuple[str, str]:
    """把异常变成 ``(错误种类, 已净化的消息)``。

    消息形态：``<种类标签>: <异常类型>: <净化后的正文>`` ⇒ 落库后一眼能分出
    「基础设施问题」与「适配器问题」。
    """

    kind = classifyError(exc)
    label = ERROR_LABEL_INFRA if kind == ERROR_KIND_INFRA else ERROR_LABEL_ADAPTER
    body = sanitizeErrorMessage(str(exc), maxLength=maxLength)
    return kind, f"{label}: {type(exc).__name__}: {body}"


def sanitizeAndLabel(
    message: str | None,
    kind: str,
    *,
    maxLength: int = MAX_ERROR_MESSAGE_LEN,
) -> str:
    """给**已有**的裸消息（适配器自己抛的 ``CommandExecutionError``）加标签 + 净化。

    ⚠️ 适配器自抛的消息**不含异常类型名** ⇒ 形态与 ``describeError`` 对齐，
    便于巡检时统一按前缀分辨。
    """

    label = ERROR_LABEL_INFRA if kind == ERROR_KIND_INFRA else ERROR_LABEL_ADAPTER
    body = sanitizeErrorMessage(message, maxLength=maxLength)
    if not body:
        return f"{label}: （无详细信息）"
    return f"{label}: {body}"
