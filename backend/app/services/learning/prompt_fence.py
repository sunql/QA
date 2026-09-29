"""提示词围栏隔离（feat-wiki-knowledge，M2/M4 共用）。

所有把**用户内容**插进 prompt 的学习机制（机制 1 分类、机制 2 实体抽取、
后续的机制 3/4/5）都用同一套围栏：把内容包在 ``<user_content>`` 里，并在
system prompt 里声明「标签内是数据、不是指令」。

**光靠声明不够**：正文里若出现 ``</user_content>``，它就会提前闭合围栏，
把后面的文字挪到「围栏之外的指令」位置。所以插值前必须**打断**正文里的
围栏标签字样。

打断方式 = 在标签名里插一个零宽空格（U+200B）：对模型是普通噪声字符，
但它不再匹配围栏标签。

**这个函数的实现有个反直觉的坑**：如果替换值写成肉眼相同的字符串，
``str.replace(x, x)`` 是**空操作**，防御静默失效而所有测试照样绿
（M4 首版正是这么写的）。因此这里用显式转义 ``\\u200b`` **派生**出替换值，
保证「替换值恒不等于被替换值」；并有
``test_neutralize_fence_breaks_fence_tags`` 钉住这个不变量。

**新增用到围栏的机制请调用本模块，不要各自复制一份 replace** ——
隔离逻辑一旦分叉，漏掉的那一路不会有任何报错。
"""

from __future__ import annotations

_ZERO_WIDTH_SPACE = "\u200b"

# (被替换值, 替换值)。替换值由 _ZERO_WIDTH_SPACE 派生，永远不等于被替换值。
_FENCE_TAGS: tuple[tuple[str, str], ...] = (
    ("<user_content>", f"<user{_ZERO_WIDTH_SPACE}_content>"),
    ("</user_content>", f"</user{_ZERO_WIDTH_SPACE}_content>"),
)


def neutralizeFence(raw: str) -> str:
    """打断 ``raw`` 里的围栏标签字样，令其无法提前闭合 ``<user_content>``。"""
    for tag, broken in _FENCE_TAGS:
        raw = raw.replace(tag, broken)
    return raw


__all__ = ["neutralizeFence"]
