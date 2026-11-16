"""外部不可信文本的**单一事实源**净化（WP-ONT-AI A1 引入）。

为什么要有这个模块：仓库里有两处会处理「外部系统写来的文本」——

1. `app/services/nl2sql_schema.py`：本体配置的别名 / 描述进 prompt 前的净化；
2. `app/services/discovery_snapshot_service.py`：业务库（表名 / 列名 / 注释）
   采集进快照前的净化。

两处**各写一套正则**正是「承诺与实现不一致」的同类风险：一边补了 U+0085、
另一边漏了 ⇒ 同一段不可信文本在两条路径上得到不同的净化结果。
⇒ 收敛到这里，各调用方按**各自口径**选用。

## 三层防御（由弱到强）

| 层 | 手段 | 挡住什么 |
|---|---|---|
| 1 | 转义 `<` `>` | 标签逃逸（`</instructions>` 拼出闭合标签） |
| 2 | 折叠**全部**行分隔符 | 裸行注入（一条注释里换行写「忽略以上规则」） |
| 3 | 剔除控制字符 / 零宽 / 双向覆写 | 人眼看不见、模型看得见的指令（零宽空格拼接、双向覆写把 "TURN OFF" 显示成 "TURN ON"） |

另有一层**高置信 PII 掩码**（邮箱 / 手机号 / 身份证号），对应设计稿 D-3 的红线
「不把 PII 带进元数据库」。

## 口径差异（刻意保留，不要合并成一个函数）

- `sanitizeSchemaField`：**本体配置**口径（管理员写入，转义 + 折叠换行）。
  `nl2sql_schema` 用它，行为与历史**逐字一致**。
- `sanitizeExternalText`：**业务库**口径（上面三层防御 + PII 掩码）。
  发现域用它 —— 业务库里写注释的是**低权限业务账号**，威胁模型比本体配置更宽。
"""

from __future__ import annotations

import re

# 行分隔符全集：LLM / 日志 / 前端对「换行」的识别并不只认 \r\n ——
# U+2028/U+2029（JS 行终止符）、U+0085（NEL）以及 \v/\f 都能把一行拆成两行。
# （与 `nl2sql_schema` 原有集合逐字一致，收敛到本模块。）
LINE_SEPARATORS = ("\r", "\n", "\u2028", "\u2029", "\u0085", "\v", "\f")

# 控制字符 / 零宽 / 双向覆写：人眼看不见（或看起来正常）但模型看得见。
# - C0（除 \t）与 DEL：终端控制码
# - C1（U+0080–U+009F）：含 NEL（U+0085，行分隔符的另一种写法）
# - U+200B–U+200F：零宽字符（可用来把单词拆开躲过关键词过滤）
# - U+202A–U+202E：双向覆写（可把 "TURN OFF" 显示成 "TURN ON"）
# - U+2066–U+2069：双向隔离符
# - U+FEFF：零宽不换行空格（BOM）
_INVISIBLE_CHARS = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)

# 高置信 PII 模式。刻意只收**形状几乎不会误伤业务数据**的：
# - 邮箱：必须含 `@` 与域名点；
# - 大陆手机号：11 位、1 开头且第二位 3-9，前后不是数字；
# - 大陆身份证号：18 位（末位可为 X），前后不是数字。
# 物料编码 / 订单号这类业务数字串**不在**这些形状里 ⇒ 不会被误掩码。
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9-])"),
        "[邮箱已掩码]",
    ),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号已掩码]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证号已掩码]"),
)

PII_MASK = "***"


def escapeAngleBrackets(value: str) -> str:
    """转义尖括号，使其无法构造任何标签逃逸出包装。

    相比删除标签更彻底：分片拼接、大小写变体或自闭合/带属性变体都无法重组出
    闭合标签——因为所有 `<`/`>` 已被替换为 HTML 实体。
    """

    return value.replace("<", "&lt;").replace(">", "&gt;")


def collapseLineSeparators(value: str) -> str:
    """把**全部**行分隔符折叠成空格（不是删掉 ⇒ 不把两个词粘在一起）。"""

    for separator in LINE_SEPARATORS:
        value = value.replace(separator, " ")
    return value


def sanitizeSchemaField(value: str) -> str:
    """本体配置字段（别名 / 描述）渲染前的净化：转义尖括号 + 折叠全部行分隔符。

    ⚠️ 与 `nl2sql_schema._sanitizeSchemaField` **逐字等价**（历史行为不变），
    这里只是把实现收敛到单一事实源。
    """

    return collapseLineSeparators(escapeAngleBrackets(value))


def maskPii(value: str) -> str:
    """高置信 PII 掩码（邮箱 / 手机号 / 身份证号）。"""

    for pattern, replacement in _PII_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def sanitizeIdentifier(value: str | None) -> str | None:
    """净化**标识符**（表名 / 列名 / 属主 / 类型名）：注入防御，**不做 PII 掩码**。

    刻意与 `sanitizeExternalText` 分开：标识符是**结构身份**，掩码会改掉它的
    语义（表名变了 ⇒ 快照哈希变了 ⇒ 「同一张表被当成两张」）。
    PII 只可能出现在注释这类自由文本里。

    空串归一为 `None`。
    """

    if value is None:
        return None
    cleaned = escapeAngleBrackets(value)
    cleaned = collapseLineSeparators(cleaned)
    cleaned = _INVISIBLE_CHARS.sub("", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def sanitizeExternalText(value: str | None) -> str | None:
    """净化**业务库来的自由文本**（注释）：注入防御 + PII 掩码。

    顺序：转义尖括号 ⇒ 折叠行分隔符 ⇒ 剔除不可见字符 ⇒ PII 掩码 ⇒ 收敛空白。

    空串归一为 `None`（与既有 `sanitizeComment` 口径一致）。
    """

    cleaned = sanitizeIdentifier(value)
    if cleaned is None:
        return None
    return maskPii(cleaned) or None
