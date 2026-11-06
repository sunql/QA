"""A1 取证探针：**故意失败**的测试。

用途：证明 merge-gate 在 PR 侧**真的有新增失败**时，会判 `NEW_FAILURES`（退出码 1）
并把 PR 挡下来。此前只有「净新增 0」方向的证据（B7 批，PR #108）。

⚠️ 这个文件**只存在于探针分支** ⇒ 绝不合并；取证后即删分支。
⚠️ 文件名**不能**是 `test_ci_*.py` —— 那会落进「CI 相关面」
⇒ 闸门会先判 `CI_SURFACE_CHANGED`（退出码 6）⇒ 混淆成「另一个失败模式」。
"""

from __future__ import annotations


def test_a1_probe_intentional_new_failure() -> None:
    """故意失败：让 PR 侧比基线多一条失败，用例名一眼可辨。"""
    assert False, "A1 探针：故意失败 —— 用于证明 merge-gate 会拦截含真失败的 PR"
