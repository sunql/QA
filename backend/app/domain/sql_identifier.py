"""SQL 标识符白名单（表名 / 列名 / 属主）—— **单一事实源**。

背景（2026-11-27，feat/discovery-skip-unprofilable-tables）：

- `services/discovery_profile_sql.py` 的 `assertIdentifier` 是**拼 SQL 前的护栏**
  （防注入），它按白名单 `^[A-Za-z_][A-Za-z0-9_$#]*$` 拒绝非法标识符；
- Oracle 的**回收站对象**（`BIN$W81vJmasuczgY0bNqMBvog==$0`）含 `=` ⇒ 必然被拒 ⇒
  一次发现运行会因为「快照里混进一张回收站表」而**整轮失败**。

两边若各写一份判定，就会出现「一边说合法、另一边说不合法」的漂移
⇒ 本模块把白名单与判定提出来，两个调用方共用。

⚠️ 白名单**故意窄**：它只允许「能安全拼进 SQL 的标识符」，不是「数据库允许的
全部名字」。含 `.`/`=`/`"`/`空格` 等字符的名字一律判为不可剖析对象。

⚠️ 为什么不用「加引号就能拼」：`discovery_profile_sql` 的 `_quote` 只做包裹、
不做转义（注释里写明「严格白名单里没有任何引号字符 ⇒ 无需转义」）⇒ 放宽白名单
会把转义漏洞一起打开。本模块**只做分类，不放宽护栏**。
"""

from __future__ import annotations

import re

#: 标识符最大长度（与 Oracle / PG 的命名上限同量级）。
MAX_IDENTIFIER_LENGTH = 128

#: 合法标识符白名单：首字符字母或下划线，其余字母/数字/下划线/`$`/`#`。
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


def isValidIdentifier(value: object) -> bool:
    """判断 `value` 能否作为 SQL 标识符（表名 / 列名 / 属主）。

    非字符串、空串、超长、含白名单外字符 ⇒ 一律 False（**不抛错**）：
    调用方要的是「分类」而不是「拒绝」——被分类为不可剖析的对象应当被跳过或隐藏，
    而不是让整条链路失败。
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value) > MAX_IDENTIFIER_LENGTH:
        return False
    return IDENTIFIER_PATTERN.match(value) is not None
