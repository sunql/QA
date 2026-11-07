"""变异取证脚本的**三态判据**必须守住（2026-11-07 N6 / M54）。

背景
----
`backend/scripts/ci_mutation_evidence.py` 是「守卫不是恒绿」的取证装置：
它把每条守卫的判据**改坏**，然后看对应用例有没有**转红**。
⇒ 这个脚本自己就是判据的一部分 ⇒ 它也在 CODEOWNERS 的授权面里。

攻击路径
--------
把 `run_case` 的「只有退出码 1 才算转红」放宽成「退出码非 0 都算转红」：

    if completed.returncode != 0:
        return "red", output

⇒ pytest 的**收集失败**（退出码 2）、**没收集到用例**（退出码 5）、
**内部错误**（3）、**用法错**（4）全被冒充成「守卫抓到了变异」
⇒ 取证报告全绿 ⇒ **守卫恒绿**且没有人复核。

⇒ 断言必须钉住**每一种退出码**的判定，不能只钉 0 与 1。

三态
----
| verdict | pytest 退出码 | 含义 |
|---|---|---|
| `red` | **1** | 有断言失败 ⇒ 变异被抓到 ✔ |
| `green` | 0 | 守卫仍绿 ⇒ 变异**没**被抓到 |
| `inconclusive` | 2 / 3 / 4 / 5 | 用例**根本没跑起来** ⇒ 取证失败 |
"""

from __future__ import annotations

import pytest

from scripts import ci_mutation_evidence

_CASE = "app/tests/unit/test_ci_surface.py::test_some_guard_case"
_CASE_NAME = "test_some_guard_case"


class _FakeCompleted:
    """`subprocess.run` 的最小替身（只要退出码与输出）。"""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stubRun(monkeypatch: pytest.MonkeyPatch, result: _FakeCompleted) -> None:
    def fakeRun(*args: object, **kwargs: object) -> _FakeCompleted:
        return result

    monkeypatch.setattr(ci_mutation_evidence.subprocess, "run", fakeRun)


def test_run_case_reports_green_when_pytest_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """退出码 0 ⇒ 守卫仍绿 ⇒ `green`（取证失败：变异没被抓住）。"""
    _stubRun(monkeypatch, _FakeCompleted(0, stdout="1 passed"))

    verdict, _ = ci_mutation_evidence.run_case(_CASE, {})

    assert verdict == "green"


def test_run_case_reports_red_when_the_named_case_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出码 1 **且**输出里有该用例名 ⇒ 真的是它失败 ⇒ `red`。"""
    _stubRun(
        monkeypatch,
        _FakeCompleted(1, stdout=f"FAILED {_CASE} - assert 1 == 2"),
    )

    verdict, _ = ci_mutation_evidence.run_case(_CASE, {})

    assert verdict == "red"


def test_run_case_treats_a_non_assertion_exit_code_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出码 2（收尾阶段出错）⇒ **用例没跑起来** ⇒ `inconclusive`。

    ⚠️ 这是 M54 的变异锚点：把「只有 1 才算转红」放宽成「非 0 都算转红」
    ⇒ 收集失败被冒充成「守卫抓到了变异」⇒ 取证报告全绿 ⇒ 守卫恒绿。

    ⇒ 断言的是**具体退出码 2**，不是笼统的「非 0」。
    """
    _stubRun(
        monkeypatch,
        _FakeCompleted(2, stdout="ERROR collecting app/tests/unit/test_ci_surface.py"),
    )

    verdict, detail = ci_mutation_evidence.run_case(_CASE, {})

    assert verdict == "inconclusive", f"退出码 2 被当成了 {verdict!r}"
    assert "2" in detail


def test_run_case_treats_a_missing_collection_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出码 5（没收集到任何用例）⇒ **用例根本没跑** ⇒ `inconclusive`。

    ⚠️ 这是真实踩过的坑：目标用例文件还没 `git add` ⇒ pytest 收不到用例
    ⇒ 退出码 5 ⇒ 若判成「转红」就是**假取证**。
    """
    _stubRun(monkeypatch, _FakeCompleted(5, stdout="no tests ran"))

    verdict, _ = ci_mutation_evidence.run_case(_CASE, {})

    assert verdict == "inconclusive"


def test_run_case_treats_an_internal_error_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出码 3（pytest 内部错误）⇒ 同样**没跑起来** ⇒ `inconclusive`。"""
    _stubRun(monkeypatch, _FakeCompleted(3, stdout="INTERNALERROR"))

    verdict, _ = ci_mutation_evidence.run_case(_CASE, {})

    assert verdict == "inconclusive"


def test_run_case_treats_a_usage_error_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出码 4（命令行用法错）⇒ 同样**没跑起来** ⇒ `inconclusive`。"""
    _stubRun(monkeypatch, _FakeCompleted(4, stdout="ERROR: file or directory not found"))

    verdict, _ = ci_mutation_evidence.run_case(_CASE, {})

    assert verdict == "inconclusive"


def test_run_case_treats_an_exit_code_without_the_case_name_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出码 1 但输出里**没有**该用例名 ⇒ 不是它失败 ⇒ `inconclusive`。

    ⚠️ 退出码 1 也可能来自「**别的**用例失败 / 插件报错」⇒
    把那种情况算成「这条守卫转红」同样是**假取证**。
    """
    _stubRun(
        monkeypatch,
        _FakeCompleted(1, stdout="FAILED app/tests/unit/test_other.py - boom"),
    )

    verdict, detail = ci_mutation_evidence.run_case(_CASE, {})

    assert verdict == "inconclusive"
    assert _CASE_NAME in detail
