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


def test_validateRepo_rejects_a_trailing_newline() -> None:
    """结尾锚点必须是 `\\Z` 而不是 `$`。

    Python 的 `$` **允许结尾换行** ⇒ `"sunql/QA\\n"` 能通过
    `^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$`
    ⇒ 校验形同虚设（实测）。
    """
    assert ci_gh.validateRepo("sunql/QA\n") is not None
    assert ci_gh.validateRepo("sunql/QA\n") != ci_gh.validateRepo("sunql/QA")


def test_validateRunId_rejects_non_numeric() -> None:
    assert ci_gh.validateRunId("111") is None
    assert ci_gh.validateRunId("11a") is not None
    assert ci_gh.validateRunId("../../x") is not None


def test_validateRunId_rejects_a_trailing_newline() -> None:
    assert ci_gh.validateRunId("111\n") is not None


def test_fetchJobPayloads_rejects_malformed_repo() -> None:
    """`fetchJobPayloads` 自己校验 repo —— 不能只在 CLI 校验。"""
    with pytest.raises(ValueError):
        ci_gh.fetchJobPayloads("../../other-owner/other-repo", "111")


def test_fetchJobLog_rejects_malformed_repo() -> None:
    with pytest.raises(ValueError):
        ci_gh.fetchJobLog("bad;repo", 1)


def test_fetchJobLog_rejects_non_integer_job_id() -> None:
    """`jobId` 会被拼进 API 路径 ⇒ 必须自己校验（不能只在 CLI 校验）。

    `fetchJobLog` 的模块纪律写得很清楚：「每个会拼 URL 的函数自己校验」，
    但 `jobId` 之前是直接插值的 ⇒ `"../../runs/12345"` 能拼出
    `repos/{repo}/actions/jobs/../../runs/12345/logs`
    ⇒ 服务端规范化后打到**另一个端点**（与 `--repo ../..` 同一族）。
    """
    with pytest.raises(ValueError):
        ci_gh.fetchJobLog("sunql/QA", "../../runs/12345")


def test_fetchJobLog_rejects_non_positive_job_id() -> None:
    for badId in (0, -1):
        with pytest.raises(ValueError):
            ci_gh.fetchJobLog("sunql/QA", badId)


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


def test_fetchRunHeadSha_rejects_a_sha_with_a_trailing_newline() -> None:
    """sha 的结尾锚点必须是 `\\Z`（`$` 允许结尾换行 ⇒ `"a"*40 + "\\n"` 能过校验）。"""

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"head_sha": "a" * 40 + "\n"})
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


# --------------------------------------------------------------------------
# merge-base（三点语义的分叉点）
# --------------------------------------------------------------------------


def test_parseMergeBaseSha_reads_the_merge_base_commit() -> None:
    payload = json.dumps({"merge_base_commit": {"sha": "c" * 40}, "files": []})

    assert ci_gh._parseMergeBaseSha(payload) == "c" * 40


def test_parseMergeBaseSha_accepts_the_slurped_page_array() -> None:
    """`--paginate --slurp` 返回页数组 ⇒ 合并后仍要能找到 merge-base。"""
    payload = json.dumps(
        [
            {"status": "ahead", "total_commits": 0},
            {"merge_base_commit": {"sha": "c" * 40}},
        ]
    )

    assert ci_gh._parseMergeBaseSha(payload) == "c" * 40


def test_parseMergeBaseSha_rejects_a_response_without_merge_base() -> None:
    """拿不到分叉点 ⇒ 无法确定三点语义 ⇒ 必须**报错**（不能当成「无改动」）。"""
    payload = json.dumps({"status": "identical", "files": []})

    with pytest.raises(ValueError):
        ci_gh._parseMergeBaseSha(payload)


def test_parseMergeBaseSha_rejects_a_malformed_merge_base_sha() -> None:
    payload = json.dumps({"merge_base_commit": {"sha": "not-a-sha"}})

    with pytest.raises(ValueError):
        ci_gh._parseMergeBaseSha(payload)


def test_fetchMergeBaseSha_calls_the_three_dot_compare_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compare 必须走**三点**语法 `base...head`（两点 `base..head` 实测 404）。"""
    captured: dict[str, object] = {}

    def fakeRunGh(args: list[str]) -> str:
        captured["args"] = args
        return json.dumps({"merge_base_commit": {"sha": "c" * 40}})

    monkeypatch.setattr(ci_gh, "_runGh", fakeRunGh)

    assert ci_gh.fetchMergeBaseSha("sunql/QA", "a" * 40, "b" * 40) == "c" * 40
    assert captured["args"] == [
        "api",
        "repos/sunql/QA/compare/" + "a" * 40 + "..." + "b" * 40,
        "--paginate",
        "--slurp",
    ]


def test_fetchMergeBaseSha_rejects_malformed_shas() -> None:
    with pytest.raises(ValueError):
        ci_gh.fetchMergeBaseSha("sunql/QA", "not-a-sha", "b" * 40)
    with pytest.raises(ValueError):
        ci_gh.fetchMergeBaseSha("sunql/QA", "a" * 40, "not-a-sha")


# --------------------------------------------------------------------------
# 「PR 相对分叉点改了什么」（三点语义，树对树实现 ⇒ 无 300 项上限）
# --------------------------------------------------------------------------


def test_fetchChangedFiles_computes_the_three_point_diff_from_the_merge_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三点语义 = `diff(merge_base, head)` ⇒ 分叉点从 compare 拿，差异树对树算。"""
    trees = {
        "c" * 40: {"a.py": "1" * 40, "old.py": "2" * 40},
        "b" * 40: {"a.py": "1" * 40, "new.py": "3" * 40},
    }
    monkeypatch.setattr(ci_gh, "fetchMergeBaseSha", lambda repo, base, head: "c" * 40)
    monkeypatch.setattr(ci_gh, "fetchTreePathShas", lambda repo, sha: trees[sha])

    assert ci_gh.fetchChangedFiles("sunql/QA", "a" * 40, "b" * 40) == ("new.py", "old.py")


def test_fetchChangedFiles_has_no_file_count_cap() -> None:
    """⚠️ **没有 300 项上限**（2026-11-07 代码评审 MEDIUM-2）。

    compare API 的 `files` 数组**最多 300 项** ⇒ 大 PR 会被静默截断
    ⇒ 归因集不完整 ⇒ 与净变更取交集时会把 PR 真改的 CI 文件**排除**掉
    ⇒ 守卫静默失效（fail-open）。

    ⇒ 本实现改走 Trees API 树对树 ⇒ 条目数只受 GitHub 的 `truncated` 限制
    ⇒ 400 个改动文件必须**全部**出现在清单里。
    """
    trees = {
        "c" * 40: {f"f{i}.py": "1" * 40 for i in range(400)},
        "b" * 40: {f"f{i}.py": "2" * 40 for i in range(400)},
    }

    original = ci_gh.fetchMergeBaseSha
    originalTree = ci_gh.fetchTreePathShas
    ci_gh.fetchMergeBaseSha = lambda repo, base, head: "c" * 40
    ci_gh.fetchTreePathShas = lambda repo, sha: trees[sha]
    try:
        changed = ci_gh.fetchChangedFiles("sunql/QA", "a" * 40, "b" * 40)
    finally:
        ci_gh.fetchMergeBaseSha = original
        ci_gh.fetchTreePathShas = originalTree

    assert len(changed) == 400


def test_fetchChangedFiles_reports_a_rename_as_delete_plus_add() -> None:
    """**改名**在树对树下 = 旧路径删除 + 新路径新增 ⇒ **两个路径都在清单里**。

    ⚠️ GitHub compare 对一次改名只给**一个**条目（新路径 `filename`、
    旧路径 `previous_filename`）⇒ 只取 `filename` 的话**旧路径从清单里消失**
    ⇒ 攻击路径（2026-11-06 安全审查 H-1 实测）：把
    `.github/ci-known-flaky-tests.json` 改名成 `docs/notes.json`
    ⇒ 白名单数据文件不再出现在改动清单里 ⇒ **静默绕过**退出码 6 的人工仲裁。
    """
    trees = {
        "c" * 40: {".github/ci-known-flaky-tests.json": "1" * 40},
        "b" * 40: {"docs/notes.json": "2" * 40},
    }

    original = ci_gh.fetchMergeBaseSha
    originalTree = ci_gh.fetchTreePathShas
    ci_gh.fetchMergeBaseSha = lambda repo, base, head: "c" * 40
    ci_gh.fetchTreePathShas = lambda repo, sha: trees[sha]
    try:
        changed = ci_gh.fetchChangedFiles("sunql/QA", "a" * 40, "b" * 40)
    finally:
        ci_gh.fetchMergeBaseSha = original
        ci_gh.fetchTreePathShas = originalTree

    assert changed == (".github/ci-known-flaky-tests.json", "docs/notes.json")


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


def test_fetchRunHeadInfo_exposes_the_trigger_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """run 的**触发事件**必须取得到 ⇒ 工具侧才能判断「基线是不是 push run」。

    GitHub 的 `runs?branch=<name>` **不区分事件** ⇒ 基线可能是 `pull_request` run
    ⇒ 工具侧必须独立复核（不能只靠 workflow 的查询串）。
    """
    payload = json.dumps(
        {
            "head_sha": "a" * 40,
            "head_branch": "main",
            "event": "push",
            "head_repository": {"full_name": "sunql/QA"},
        }
    )
    monkeypatch.setattr(ci_gh, "_runGh", lambda args: payload)

    headInfo = ci_gh.fetchRunHeadInfo("sunql/QA", "111")

    assert headInfo.event == "push"
    assert headInfo.headBranch == "main"
    assert headInfo.headRepo == "sunql/QA"


# --------------------------------------------------------------------------
# 树对树净变更（净 diff）
# --------------------------------------------------------------------------


def _treePayload(entries: list[dict], truncated: bool = False) -> str:
    return json.dumps({"truncated": truncated, "tree": entries})


def test_parseTreePathShas_keeps_only_file_like_entries() -> None:
    """递归树里混着三种条目：blob（文件）、tree（目录）、commit（子模块）。

    - 目录（`type=tree`）必须**排除** ⇒ 否则「一边多一个空目录」会被当成
      「改了一个文件」⇒ 改动清单里出现根本不是文件的路径；
    - 子模块（`type=commit`）要**保留** ⇒ 它是一个路径级的指针变化；
    - 值里带 `mode` ⇒ 只改权限位（chmod）的改动**也能看见**（blob sha 不变）。
    """
    payload = _treePayload(
        [
            {"path": "a.py", "type": "blob", "sha": "1" * 40, "mode": "100644"},
            {"path": "dir", "type": "tree", "sha": "2" * 40, "mode": "040000"},
            {"path": "dir/child.py", "type": "blob", "sha": "5" * 40, "mode": "100644"},
            {"path": "sub", "type": "commit", "sha": "3" * 40, "mode": "160000"},
        ]
    )

    assert ci_gh._parseTreePathShas(payload) == {
        "a.py": f"100644:{'1' * 40}",
        "dir/child.py": f"100644:{'5' * 40}",
        "sub": f"160000:{'3' * 40}",
    }


def test_parseTreePathShas_rejects_truncated_tree() -> None:
    """递归树被服务端截断 ⇒ 清单不可信 ⇒ 必须**报错**（fail-closed）。

    Trees API 的递归树有上限（条目数 / 字节数）⇒ 命中上限时 GitHub 会
    把 `truncated` 置真并**少给**一部分条目 ⇒ 少给的那部分里的 CI 相关面
    文件就**看不见** ⇒ 守卫放行 ⇒ 假「净新增 0」。
    """
    payload = _treePayload(
        [{"path": "a.py", "type": "blob", "sha": "1" * 40, "mode": "100644"}],
        truncated=True,
    )

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)


def test_parseTreePathShas_rejects_a_missing_truncated_flag() -> None:
    """`truncated` 键**缺失** ⇒ 按「不完整」处理，不能当「未截断」。

    它是**唯一**能证明清单完整的字段 ⇒ 缺失时必须报错（fail-closed）。

    ⚠️ 载荷里**必须带嵌套路径**（`dir/` + `dir/a.py`）⇒ 否则「递归没生效」
    那条不变式会先报错 ⇒ 本用例就测不到 `truncated` 这一条了
    （变异取证：去掉 `truncated` 检查后本用例必须转红）。
    """
    payload = json.dumps(
        {
            "tree": [
                {"path": "dir", "type": "tree", "sha": "2" * 40, "mode": "040000"},
                {"path": "dir/a.py", "type": "blob", "sha": "1" * 40, "mode": "100644"},
            ]
        }
    )

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)


def test_parseTreePathShas_rejects_unexpected_payload() -> None:
    """gh 返回的结构不是预期形状 ⇒ 报错（不能当成「空树」）。"""
    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(json.dumps(["not", "an", "object"]))


def test_parseTreePathShas_rejects_a_malformed_entry() -> None:
    """条目缺字段 ⇒ **报错**，不静默跳过。

    跳过 = 该路径从清单里消失 = 少一个 CI 相关面文件 = 守卫静默失效。
    """
    payload = _treePayload(
        [{"path": "a.py", "type": "blob", "sha": "1" * 40, "mode": "100644"}]
    )
    payload = payload.replace('"mode": "100644"', '"mode": 100644')

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)


def test_parseTreePathShas_rejects_an_unknown_entry_type() -> None:
    """不认识的条目类型 ⇒ 报错（不能当成普通文件收下）。

    ⚠️ 载荷里**必须带嵌套路径**（`dir/` + `dir/a.py`）⇒ 否则「递归没生效」
    那条不变式会**先**报错 ⇒ 本用例就测不到「未知类型」这一条
    （变异取证：把类型检查改成放行后本用例必须转红）。
    """
    payload = _treePayload(
        [
            {"path": "dir", "type": "tree", "sha": "2" * 40, "mode": "040000"},
            {"path": "dir/a.py", "type": "weird", "sha": "1" * 40, "mode": "100644"},
        ]
    )

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)


def test_parseTreePathShas_rejects_a_non_recursive_tree() -> None:
    """**递归没生效** ⇒ 必须报错。

    不带 `recursive=1` 时 GitHub 只返回**根级**条目（文件 + 各子目录的 tree 条目）
    ⇒ 所有 CI 相关面文件（都在子目录里）**一个都看不见** ⇒ 守卫静默失效
    ⇒ 而 `truncated` 仍是 false ⇒ 不看目录不变式就会当成「完整清单」放行。

    ⇒ 不变式：**每个目录条目都必须有子条目**（git 不存空目录 ⇒ 恒成立）。
    """
    payload = _treePayload(
        [
            {"path": "README.md", "type": "blob", "sha": "1" * 40, "mode": "100644"},
            {"path": ".github", "type": "tree", "sha": "2" * 40, "mode": "040000"},
        ]
    )

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)


def test_diffTreePathShas_reports_added_removed_and_modified() -> None:
    """净变更 = 两棵树按「路径 → 对象 sha」求差：新增 / 删除 / 内容变化都要报。"""
    base = {"same.py": "1" * 40, "gone.py": "2" * 40, "mod.py": "3" * 40}
    head = {"same.py": "1" * 40, "new.py": "4" * 40, "mod.py": "5" * 40}

    assert ci_gh._diffTreePathShas(base, head) == ("gone.py", "mod.py", "new.py")


def test_fetchTreePathShas_rejects_malformed_sha() -> None:
    """sha 会被拼进 API 路径 ⇒ 自己校验（不能只在 CLI 校验）。"""
    with pytest.raises(ValueError):
        ci_gh.fetchTreePathShas("sunql/QA", "../../etc/passwd")


def test_fetchTreePathShas_rejects_malformed_repo() -> None:
    with pytest.raises(ValueError):
        ci_gh.fetchTreePathShas("../../other-owner/other-repo", "a" * 40)


def test_fetchTreePathShas_calls_the_recursive_trees_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """必须走 `git/trees/{sha}?recursive=1` ⇒ 一次拿到整棵树。"""
    captured: dict[str, object] = {}

    def fakeRunGh(args: list[str]) -> str:
        captured["args"] = args
        return json.dumps(
            {
                "truncated": False,
                "tree": [
                    {"path": "dir", "type": "tree", "sha": "2" * 40, "mode": "040000"},
                    {"path": "dir/a.py", "type": "blob", "sha": "1" * 40, "mode": "100644"},
                ],
            }
        )

    monkeypatch.setattr(ci_gh, "_runGh", fakeRunGh)

    ci_gh.fetchTreePathShas("sunql/QA", "a" * 40)

    assert captured["args"] == ["api", "repos/sunql/QA/git/trees/" + "a" * 40 + "?recursive=1"]


def test_fetchNetChangedFiles_compares_the_two_trees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """净变更 = 两棵树之差。

    ⚠️ **不能**用 compare API 的三点 diff 代替：三点从 merge-base 起算 ⇒
    分支若含「重建 main 的 tree 的合成提交」⇒ main 的**自己的** CI 文件
    会被算成「PR 改的」（2026-11-05 实证：三点 158 个文件 vs 净变更 11 个
    ⇒ 假 `CI_SURFACE_CHANGED`）。
    """
    trees = {
        "a" * 40: {"same.py": "1" * 40, "mod.py": "2" * 40},
        "b" * 40: {"same.py": "1" * 40, "mod.py": "3" * 40},
    }
    monkeypatch.setattr(ci_gh, "fetchTreePathShas", lambda repo, sha: trees[sha])

    assert ci_gh.fetchNetChangedFiles("sunql/QA", "a" * 40, "b" * 40) == ("mod.py",)


def test_parseTreePathShas_rejects_a_response_missing_ancestor_entries() -> None:
    """响应缺少**祖先目录条目** ⇒ 不是完整递归树 ⇒ 报错（fail-closed）。

    实测本仓的递归列举必然带上每一级目录条目（git 的树里目录本身就是条目）
    ⇒ 缺祖先 = 响应被裁剪过 ⇒ 清单可能缺文件 ⇒ CI 相关面文件会隐身。
    """
    payload = _treePayload(
        [{"path": "dir/sub/a.py", "type": "blob", "sha": "1" * 40, "mode": "100644"}]
    )

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)


def test_parseTreePathShas_rejects_a_fully_flat_response() -> None:
    """响应里**一个带目录前缀的条目都没有** ⇒ 递归没生效 ⇒ 报错。

    ⚠️ 这条是「前两条不变式会真空通过」时的兜底：响应只剩根级条目 ⇒
    所有 CI 相关面文件（都在子目录里）一个都看不见 ⇒ 守卫**静默失效**
    而 `truncated=false` 不报错。
    """
    payload = _treePayload(
        [{"path": "README.md", "type": "blob", "sha": "1" * 40, "mode": "100644"}]
    )

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)


def test_parseTreePathShas_rejects_a_zero_truncated_flag() -> None:
    """`truncated` 是数字 `0` ⇒ **不能**当「未截断」放行。

    ⚠️ 真值判断（`if parsed["truncated"]:`）会把 `0` 当成「没截断」⇒ **fail-open**
    ⇒ 清单可能已被服务端截断却看不出来 ⇒ 守卫静默失效。
    ⇒ 判据必须是 `is not False`（唯一放行的形态是**布尔假**）。
    """
    payload = json.dumps(
        {
            "truncated": 0,
            "tree": [
                {"path": "dir", "type": "tree", "sha": "2" * 40, "mode": "040000"},
                {"path": "dir/a.py", "type": "blob", "sha": "1" * 40, "mode": "100644"},
            ],
        }
    )

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)




def test_parseTreePathShas_rejects_a_string_false_truncated_flag() -> None:
    """`truncated` 是字符串 `"false"` ⇒ **不能**当「未截断」放行。

    判据必须是 `is not False`（唯一放行的形态是**布尔假**）⇒
    真值判断会把 `"false"` 当成「已截断」（白拦），反过来若服务端改成别的
    形态又可能当成「未截断」（fail-open）⇒ 两种都不接受。
    """
    payload = json.dumps(
        {
            "truncated": "false",
            "tree": [
                {"path": "dir", "type": "tree", "sha": "2" * 40, "mode": "040000"},
                {"path": "dir/a.py", "type": "blob", "sha": "1" * 40, "mode": "100644"},
            ],
        }
    )

    with pytest.raises(ValueError):
        ci_gh._parseTreePathShas(payload)
