"""ILIKE 通配符转义（跨 service 共用）。

不转义的后果不是注入（参数仍走绑定），而是**语义失控**：一个 ``%`` 就让
条件退化成「匹配任意内容」。用户搜「增长率 % 的算法」本意是找那个百分号，
结果拿到全表。

调用方必须把返回值放进 ``f"%{...}%"`` 并给 ``.ilike(..., escape=LIKE_ESCAPE)``
显式传转义符 —— 只转义不传 ``escape=`` 是无效的（Postgres 默认转义符虽是
反斜杠，但显式传递才能防 ``standard_conforming_strings`` 变化）。
"""
from __future__ import annotations

LIKE_ESCAPE = "\\"


def escapeLike(raw: str) -> str:
    """把用户输入里的 ILIKE 通配符（``%`` / ``_`` / 转义符本身）转成字面量。"""
    return (
        raw.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
