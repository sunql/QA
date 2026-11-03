"""`gh` 适配层：参数校验必须**下沉到每个会拼 URL 的函数**。

这些函数是模块的公开 API，被复用时未校验的输入即刻变成路径注入
（`--repo ../..` 会被服务端规范化后打到另一个 API 端点）。
"""

from __future__ import annotations

import json

import pytest

from app.utils import ci_gh

# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------


def test_validateRepo_accepts_normal_slug() -> None:
    assert ci_gh.validateRepo("sunql/QA") is None


@pytest.mark.parametrize(
    "repo",
    ["../../other-owner/other-repo", "bad;repo", "owner", "owner/repo/extra", ".owner/repo"],
)
def test_validateRepo_rejects_path_injection_forms(repo: str) -> None:
    assert ci_gh.validateRepo(repo) is not None


def test_validateRunId_rejects_non_numeric() -> None:
    assert ci_gh.validateRunId("111") is None
    assert ci_gh.validateRunId("11a") is not None
    assert ci_gh.validateRunId("../../x") is not None


def test_fetchJobPayloads_rejects_malformed_repo() -> None:
    """`fetchJobPayloads` 自己校验 repo —— 不能只在 CLI 校验。"""
    with pytest.raises(ValueError):
        ci_gh.fetchJobPayloads("../../other-owner/other-repo", "111")


def test_fetchJobLog_rejects_malformed_repo() -> None:
    with pytest.raises(ValueError):
        ci_gh.fetchJobLog("bad;repo", 1)


def test_fetchRunHeadSha_rejects_malformed_sha() -> None:
    """run 返回的 head_sha 形态异常 ⇒ 拒绝（它会被拼进 compare 路径）。"""

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"head_sha": "../../etc/passwd"})
        stderr = ""

    original = ci_gh.subprocess.run
    ci_gh.subprocess.run = lambda *args, **kwargs: FakeResult()
    try:
        with pytest.raises(ValueError):
            ci_gh.fetchRunHeadSha("sunql/QA", "111")
    finally:
        ci_gh.subprocess.run = original


def test_fetchChangedFiles_rejects_malformed_sha() -> None:
    with pytest.raises(ValueError):
        ci_gh.fetchChangedFiles("sunql/QA", "not-a-sha", "b" * 40)


# --------------------------------------------------------------------------
# 分页合并
# --------------------------------------------------------------------------


def test_parseJobs_merges_paginated_pages() -> None:
    """`gh api --paginate --slurp` 返回**页数组** ⇒ 必须把各页的 job 合并。

    不合并就会只看到第一页 ⇒ 少算 job ⇒ 覆盖等价性判定失真。
    """
    payload = json.dumps(
        [
            {"total_count": 3, "jobs": [{"id": 1, "name": "a", "conclusion": "success"}]},
            {"total_count": 3, "jobs": [{"id": 2, "name": "b", "conclusion": "failure"}]},
        ]
    )

    rawItems = ci_gh._parseJobs(payload)

    assert [item["id"] for item in rawItems] == [1, 2]


def test_parseJobs_accepts_plain_object_shape() -> None:
    """单页时 gh 返回对象（`{"jobs": [...]}`）⇒ 也要能解析。"""
    payload = json.dumps({"total_count": 1, "jobs": [{"id": 7, "name": "a", "conclusion": "success"}]})

    rawItems = ci_gh._parseJobs(payload)

    assert [item["id"] for item in rawItems] == [7]


def test_parseChangedFiles_merges_paginated_pages() -> None:
    payload = json.dumps(
        [
            {"files": [{"filename": "a.py"}]},
            {"files": [{"filename": "b.py"}]},
        ]
    )

    assert ci_gh._parseChangedFiles(payload) == ("a.py", "b.py")


# --------------------------------------------------------------------------
# gh 调用本身
# --------------------------------------------------------------------------


def test_runGh_clears_gh_debug_and_disables_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GH_DEBUG` 会让 gh 把**请求头**（含 token）dump 进 stderr ⇒ 必须清掉。

    没有凭据时 gh 会挂交互提示直到超时 ⇒ 必须设 `GH_PROMPT_DISABLED=1`。
    """
    captured: dict[str, object] = {}

    class FakeResult:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fakeRun(args, **kwargs):
        captured["env"] = kwargs.get("env")
        captured["args"] = args
        return FakeResult()

    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setattr(ci_gh.subprocess, "run", fakeRun)

    ci_gh._runGh(["api", "x"])

    env = captured["env"]
    assert isinstance(env, dict)
    assert "GH_DEBUG" not in env
    assert env.get("GH_PROMPT_DISABLED") == "1"


def test_runGh_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """gh 必须走**参数数组**（`shell=False`）⇒ 没有 shell 插值面。"""
    captured: dict[str, object] = {}

    class FakeResult:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fakeRun(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResult()

    monkeypatch.setattr(ci_gh.subprocess, "run", fakeRun)

    ci_gh._runGh(["api", "repos/a b/c"])

    assert captured["args"] == ["gh", "api", "repos/a b/c"]
    assert "shell" not in captured["kwargs"] or captured["kwargs"]["shell"] is False
