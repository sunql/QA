"""merge-gate workflow 的**契约**守卫。

闸门的安全性质有两条是写在 workflow 文件里的（不在 Python 代码里）⇒
一旦被人改掉，Python 侧的守卫一条都不会红 ⇒ 必须用**读 YAML** 的方式钉死。

两条契约
--------
1. **checkout 必须显式指向默认分支** ⇒ 闸门读到的「已知抖动白名单」
   永远是 **main 上的版本** ⇒ PR 不能给自己开豁免。
   （`workflow_run` 语义下 `github.sha` / `github.ref` 本来就指向默认分支，
   但那是 action 的**隐含默认**；显式写出来才成契约。）
2. **故意不传 `--allow-ci-changes`** ⇒ PR 改了 CI 相关面 ⇒ 退出码 6 ⇒ failure
   ⇒ 必须人工仲裁。把它写进 CLI 就能让闸门**自动**放行 CI 相关面改动
   ⇒ 人工仲裁这一层就没了。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.utils.ci_report import MAX_REPORT_BYTES

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "merge-gate.yml"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
    """闸门 workflow 的全部 job。"""
    jobs = _workflow()["jobs"]
    assert "failure-set-diff" in jobs, "闸门 job 改名了？本文件的契约要跟着改"
    return jobs


def _steps() -> list[dict]:
    """闸门里**所有** job 的步骤列表（不只 `failure-set-diff`）。

    ⚠️ 2026-11-06 代码评审 M-4：旧实现只扫**一个** job ⇒ 在 workflow 里加第二个
    job（或给现有 job 加一个后续 step）调用 `--allow-ci-changes` / `--flake-whitelist`
    ⇒ 守卫**全绿** ⇒ 人工仲裁层被自动化掉 / 白名单可读任意路径。
    ⇒ 契约必须覆盖**全部** job。
    """
    steps: list[dict] = []
    for job in _jobs().values():
        steps.extend(job.get("steps") or [])
    return steps


def test_merge_gate_workflow_exists() -> None:
    """workflow 文件必须在（路径写错 ⇒ 后面几条守卫会**假绿**）。"""
    assert _WORKFLOW.is_file(), f"找不到 {_WORKFLOW}"


def test_checkout_targets_the_default_branch_explicitly() -> None:
    """checkout 必须**显式**指向默认分支。

    ⇒ 闸门读到的白名单 = main 上的版本 ⇒ PR 分支里的白名单改动不生效
    ⇒ **PR 不能给自己开豁免**（这是白名单机制的核心安全属性）。

    ⚠️ 断言收紧为**整串等于** `${{ github.event.repository.default_branch }}`：
    只判「含 `default_branch`」会让 `refs/heads/${{ github.event.pull_request.head.ref }}`
    这类混写蒙混过关（它含 `head` 不含 `default_branch`，但换个写法
    `main-${{ github.event.repository.default_branch }}` 就能通过）
    ⇒ 「显式指向默认分支」必须是**唯一**指向。
    """
    checkoutSteps = [step for step in _steps() if str(step.get("uses", "")).startswith("actions/checkout")]
    assert checkoutSteps, "闸门里没有 checkout 步骤？"

    for step in checkoutSteps:
        ref = (step.get("with") or {}).get("ref")
        assert ref is not None, "checkout 没有显式 ref ⇒ 依赖 action 的隐含默认（不可验证的契约）"
        assert str(ref) == "${{ github.event.repository.default_branch }}", (
            f"checkout 的 ref 不是「恰好」默认分支：{ref!r}"
            "⇒ 白名单可能取到 PR 分支的版本 ⇒ PR 能给自己开豁免"
        )


def _executableScript() -> str:
    """闸门里**真正会被执行**的 shell 文本（去掉以 `#` 开头的注释行）。

    只看注释行会误判：文件头就写着「故意不传 `--allow-ci-changes`」这句话。
    """
    lines: list[str] = []
    for step in _steps():
        script = step.get("run")
        if not script:
            continue
        for line in str(script).splitlines():
            if line.strip().startswith("#"):
                continue
            lines.append(line)
    return "\n".join(lines)


def test_merge_gate_never_passes_allow_ci_changes() -> None:
    """闸门**故意不传** `--allow-ci-changes`。

    传了它 ⇒ PR 改了 CI 相关面（workflow / conftest / pytest 配置 / 判定工具）
    也会被判「可合并」⇒ **人工仲裁这一层消失** ⇒ 失败集合的可比性无人把关。
    """
    script = _executableScript()

    assert "--allow-ci-changes" not in script, (
        "闸门不得传 --allow-ci-changes ⇒ CI 相关面改动必须人工仲裁（退出码 6）"
    )


def test_merge_gate_is_triggered_by_workflow_run_not_pull_request() -> None:
    """闸门必须由 `workflow_run` 触发 ⇒ 闸门代码与白名单**永远取自默认分支**。

    若改成 `pull_request` 触发 ⇒ PR 就能改闸门代码与白名单 ⇒ 自我豁免。
    """
    workflow = _workflow()
    # YAML 1.1 把裸 `on` 解析成布尔 True ⇒ 两种键名都要试。
    triggers = workflow.get("on", workflow.get(True))

    assert "workflow_run" in triggers, f"闸门触发方式变了：{sorted(triggers)}"
    assert triggers["workflow_run"]["workflows"] == ["CI"]


def test_merge_gate_never_passes_flake_whitelist() -> None:
    """闸门**不传** `--flake-whitelist` ⇒ 白名单路径只能是 CLI 的**默认值**。

    默认值 = 仓库根 `.github/ci-known-flaky-tests.json`（`ci_flakes.defaultWhitelistPath`）
    ⇒ 由 checkout 的默认分支版本决定 ⇒ PR 不能把路径指到自己分支上的文件。

    ⚠️ 传了就等于允许闸门读**任意路径**的白名单 ⇒ 只要那个路径在 PR 分支上
    （或可被 PR 写），PR 就能给自己开豁免 ⇒ 「精确 node ID + CODEOWNER 维护」
    这两条硬约束全部落空。
    """
    script = _executableScript()

    assert "--flake-whitelist" not in script, (
        "闸门不得传 --flake-whitelist ⇒ 白名单路径只能是 CLI 默认值（默认分支上的文件）"
    )


def test_merge_gate_reads_the_gate_title_from_the_tool_file() -> None:
    """闸门必须从**独立文件**里取标题（不靠从人读报告正文里 grep）。

    对称豁免的代价：PR 侧**确定性**弄红白名单用例也不拦 ⇒ 唯一的提示若只躺在
    check summary 正文里（要展开才看得到），GitHub UI 上人先看到的仍是
    「绿色对勾 + 净新增 0 ⇒ 可合并」⇒ **真相被盖住**。
    ⇒ 标题必须带上豁免条数（PR 侧命中时额外点明）。

    ⚠️ 2026-11-06 安全审查 L-2：旧实现让闸门 `grep '^gate-title: '` 报告正文
    ⇒ 报告是**给人读**的文本 ⇒ 改一下渲染（加缩进、换前缀、把标题挪到被截断的
    尾部）就静默失效 ⇒ **豁免提示从标题里消失**。
    ⇒ 标题走**专用文件** ⇒ 报告怎么改都不影响它。
    """
    script = _executableScript()

    assert "--gate-title-file" in script, "闸门没有让工具把标题写进独立文件"
    assert "gate_title=" in script, "闸门没有回显工具给的标题"
    assert "grep -E '^gate-title: '" not in script, (
        "闸门不得从人读报告正文里 grep 标题 ⇒ 报告正文一改就静默失效"
    )


def test_merge_gate_wires_the_tool_title_into_the_check_payload() -> None:
    """工具给的标题必须**真的流进 check 的 title 字段**（不只 echo 出来）。

    ⚠️ 2026-11-06 代码评审 M-5：旧守卫只断言 `"gate_title=" in script`
    ⇒ 那被 **`echo "gate_title=${TOOL_TITLE}"`** 这一行满足
    ⇒ 而真正喂给 check payload 的是 `title="${TOOL_TITLE}"`
    ⇒ 删掉**那一行**（改名 / 重构 `if` 块时很容易发生）⇒ 守卫照样绿
    ⇒ check 标题静默回退成「净新增 0 ⇒ 可合并」⇒ **豁免提示消失**
    ⇒ 正是 SR-L-2 要堵的那个失败模式（真相被盖住且无人察觉）。

    ⇒ 这条守卫沿**数据流**追：`jq --arg title "<var>"` ⇒ `<var>` 必须由
    `TOOL_TITLE` 赋值 ⇒ 改名变量不会误报，改**赋值来源**才会红。
    """
    script = _executableScript()

    argMatch = re.search(r'--arg title "\$\{([A-Za-z_][A-Za-z0-9_]*)\}"', script)
    assert argMatch is not None, "jq 没有把某个变量作为 check 的 title 传入"
    titleVar = argMatch.group(1)

    assignmentPattern = rf'^\s*{re.escape(titleVar)}="\$\{{TOOL_TITLE\}}"'
    assert re.search(assignmentPattern, script, re.MULTILINE) is not None, (
        f"check 的 title 变量 {titleVar!r} 不是从 TOOL_TITLE 赋来的"
        " ⇒ 工具给的标题不会出现在 check 标题里 ⇒ 豁免提示被盖住"
    )


def test_merge_gate_is_triggered_by_workflow_run_only() -> None:
    """触发器集合必须**恰好**是 `{workflow_run}`（不能多出一条别的）。

    ⚠️ 2026-11-06 代码评审 M-3：旧守卫只断言 `"workflow_run" in triggers`
    ⇒ 在保留 `workflow_run` 的同时再加一条 `pull_request:` 触发器，守卫照样绿。

    ⇒ 而 `pull_request` 事件用的是 **PR 分支上的 workflow 文件**
    ⇒ PR 就能运行自己改过的闸门，并 `POST /check-runs` 造一个**同名**的
    `merge-gate / failure-set-diff` success check ⇒ 必需检查按名字匹配
    ⇒ 「撞最新的一个是竞态」（workflow 自己的注释就承认了）⇒ 闸门可被绕过。

    ⇒ 断言必须落在**集合相等**上。
    """
    workflow = _workflow()
    # YAML 1.1 把裸 `on` 解析成布尔 True ⇒ 两种键名都要试。
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"workflow_run"}, (
        f"闸门的触发器集合变了：{sorted(triggers)}"
        "⇒ 多出来的触发器（例如 pull_request）会让 PR 用自己分支上的闸门文件"
        "⇒ 可伪造同名 success check ⇒ 闸门被绕过"
    )


def test_merge_gate_never_passes_dangerous_flags_in_any_job() -> None:
    """**每一个** job 的脚本都不得出现 `--allow-ci-changes` / `--flake-whitelist`。

    ⚠️ 2026-11-06 代码评审 M-4：旧守卫只扫 `failure-set-diff` 这**一个** job
    ⇒ 在 workflow 里加第二个 job（或给现有 job 加一个后续 step）调用
    `--allow-ci-changes` / `--flake-whitelist`，守卫**全绿**。

    ⇒ `--allow-ci-changes` = 把「人工仲裁」自动化掉；`--flake-whitelist` = 让闸门读
    **任意路径**的白名单（PR 分支上的文件）⇒ 直接击穿白名单机制的两条硬约束。
    """
    script = _executableScript()

    assert "--allow-ci-changes" not in script, (
        "闸门的**任何** job 都不得传 --allow-ci-changes ⇒ CI 相关面改动必须人工仲裁"
    )
    assert "--flake-whitelist" not in script, (
        "闸门的**任何** job 都不得传 --flake-whitelist ⇒ 白名单路径只能是 CLI 默认值"
    )


def test_merge_gate_base_run_query_only_picks_push_runs() -> None:
    """基线必须限定 **event=push**（2026-11-07 安全评审 HIGH-1）。

    GitHub 的 `runs?branch=<name>` 过滤匹配的是 `head_branch`、**不区分事件**
    （实测：`branch=feat/merge-gate-flake-whitelist` 返回的是 `event=pull_request`
    的 run）⇒ 一个**名叫 main 的 fork 分支**发起的 PR 也会命中 `branch=main`
    ⇒ 它的 run 可能被当成「main 的基线」⇒ 基线 = 攻击者自己的代码
    ⇒ 失败集合对差被中和 ⇒ 闸门失去意义。

    ⇒ 查询串必须带 `event=push`，**且** jq 必须再筛一次 `.event == "push"`
    （双保险：查询参数可能被服务端忽略 ⇒ jq 侧独立把关）。
    """
    script = _executableScript()

    assert "event=push" in script, (
        "基线查询没有限定 event=push ⇒ PR run 可能被当成 main 的基线"
    )
    assert re.search(r"select\(\s*\.event\s*==\s*[\"']push[\"']", script) is not None, (
        "基线查询的 jq 没有再筛一次 .event == 'push' ⇒ 只靠查询参数不够"
    )


def test_merge_gate_base_run_query_rechecks_the_run_fields_in_jq() -> None:
    """jq 侧必须**独立复核** `head_branch` / `head_repository` / `status`。

    ⚠️ 2026-11-07 代码评审 LOW-3：查询串里的 `branch=main&status=completed`
    **不能**当防线 ——
    - `runs?branch=<name>` 匹配的是 `head_branch`、**不区分事件**
      ⇒ 一个**名叫 main 的 fork 分支**也会命中；
    - `status=completed` 是**查询参数**，服务端可能忽略 ⇒ 拿一个**还在跑**的 run
      当基线 ⇒ 它的 job 没有完成标记 ⇒ 失败集合为空 ⇒ 对差出假「净新增 0」。

    ⇒ 这三个字段必须在 jq 的 `select(...)` 里**再筛一遍**（双保险）。
    """
    script = _executableScript()

    assert re.search(r"\.head_branch\s*==\s*[\"']main[\"']", script) is not None, (
        "jq 没有独立复核 head_branch ⇒ 一个叫 main 的 fork 分支会被当成基线"
    )
    assert re.search(r"\.status\s*==\s*[\"']completed[\"']", script) is not None, (
        "jq 没有独立复核 status ⇒ 还在跑的 run 可能被当成基线"
    )
    assert "head_repository" in script, (
        "jq 没有独立复核 head_repository ⇒ 基线可能来自 fork 仓库"
    )


def test_merge_gate_summary_window_is_wider_than_the_report_cap() -> None:
    """摘要的字节窗口必须**大于**工具自己的报告上限 ⇒ 工具不会被 workflow 二次截断。

    ⚠️ 2026-11-06 代码评审 M-1：闸门把报告 `head -c 8000` 塞进 check 摘要
    ⇒ 工具以为「我没超上限」，workflow 却按**字节**砍尾 ⇒ 砍掉的部分**没有任何标记**
    ⇒ 净新增失败清单（证据）在白名单变长后被整段挤出可见范围。

    ⇒ 报告的截断必须由**工具**负责（它会明示），workflow 只能原样搬运。
    """
    script = _executableScript()

    match = re.search(r"head\s+-c\s+(\d+)", script)
    assert match is not None, "闸门没有用 head -c 截断摘要 ⇒ 找不到字节窗口"
    windowBytes = int(match.group(1))

    assert windowBytes > MAX_REPORT_BYTES, (
        f"摘要窗口 {windowBytes} 字节 ≤ 报告上限 {MAX_REPORT_BYTES} 字节"
        "⇒ workflow 会二次截断工具的报告 ⇒ 截断变成静默的"
    )


def test_merge_gate_base_run_is_bounded_by_the_pr_run_dispatch_time() -> None:
    """基线必须是「**PR 的 CI 事件派发之前**」的 main run（2026-11-07 N6）。

    ⚠️ PR 的 CI 跑的是 `refs/pull/N/merge`（= main 与 PR 头合并出来的 commit）
    ⇒ 它执行的是**PR 事件派发那一刻的 main**（合并 commit 在派发时就被冻结）。
    ⇒ 基线若取「闸门跑的那一刻 main 上最新的 run」⇒ 基线树含 main 的后续改动
    （尤其是 CI 文件）⇒ 两次 run 跑的代码**天然不同**
    ⇒ **系统性假拦**（实测 PR #92/#94/#95/#107 的净变更 CI 命中 1/29/29/17，
      而它们真正的三点归因命中是 **0**）。

    ⇒ 查询必须带「`created_at` ≤ PR run 的 `created_at`」这个条件。
    """
    script = _executableScript()

    assert "PR_RUN_CREATED_AT" in script, (
        "闸门没有把 PR run 的 created_at 传进基线查询 ⇒ 基线可能取到 PR CI 之后的 main"
    )
    assert re.search(r"created_at\s*<=", script) is not None, (
        "基线查询没有用 `created_at <=` 限定时间窗 ⇒ 基线可能取到 PR CI 之后的 main"
    )


def test_merge_gate_exposes_the_pr_run_created_at_to_the_diff_step() -> None:
    """PR run 的 `created_at` 必须**注入到**跑工具的那一步的环境变量里。

    ⚠️ 只在别的步骤里出现（哪怕是注释）都不算 ⇒ 断言的是**这一步的 env**。
    """
    steps = [
        step
        for step in _steps()
        if "ci_failure_set" in str(step.get("run", ""))
    ]
    assert steps, "闸门里没有跑工具的步骤？"

    env = steps[0].get("env") or {}
    assert "PR_RUN_CREATED_AT" in env, (
        "跑工具的步骤没有注入 PR_RUN_CREATED_AT ⇒ 基线时间窗无从限定"
    )


def test_merge_gate_fails_closed_when_the_pr_run_start_time_is_missing() -> None:
    """拿不到 PR run 的开跑时间 ⇒ 必须**判不出基线**（fail-closed）。

    ⚠️ 空字符串会让 jq 的 `.created_at <= ""` **恒真**（任何时间戳都 > ""）
    ⇒ 基线退化成「main 上最新的 run」⇒ 又变回**系统性假拦**
    ⇒ 所以必须显式判空，而不是把空值直接喂给 jq。
    """
    script = _executableScript()

    assert re.search(r'if\s+\[\s*-z\s+"\$\{PR_RUN_CREATED_AT\}"\s*\]', script) is not None, (
        "闸门没有判 PR_RUN_CREATED_AT 为空 ⇒ 空值会让时间窗恒真 ⇒ 基线退化成最新 main run"
    )


def test_merge_gate_uses_created_at_not_run_started_at() -> None:
    """基线时间窗的上界必须取 **`created_at`**（事件派发时刻），不是 `run_started_at`。

    ⚠️ 2026-11-07 代码评审 CRITICAL-1：`pull_request` 事件的**合并 commit
    在事件派发那一刻就被冻结**了 ⇒ 复跑（re-run）**不会**重新计算它
    ⇒ 复跑检出的仍是**同一个**合并 commit ⇒ 它的 main 侧底座是
    **派发那一刻**的 main。

    实测（PR #105 run `41908498247`）：`created_at=2026-11-04T20:38:19Z`、
    `run_started_at=2026-11-04T22:24:11Z`（attempt=2）；合并 commit `57a267a9`
    的第一父 = `83581e10`（= 派发那一刻的 main）；attempt-2 的 job 日志写着
    `git fetch … +57a267a93f…:refs/remotes/pull/105/merge`。

    ⇒ 上界锚 `run_started_at` ⇒ 基线取到**派发之后**的 main
    ⇒ 基线树 ≠ 执行树（执行树用的是派发那一刻的 main）
    ⇒ 两次 run 的 CI 定义可能不同 ⇒ 失败集合不可比 ⇒ **fail-open**。

    ⇒ `workflow_run` 事件载荷里必须取 `created_at`。
    """
    steps = [
        step
        for step in _steps()
        if "ci_failure_set" in str(step.get("run", ""))
    ]
    assert steps, "闸门里没有跑工具的步骤？"

    env = steps[0].get("env") or {}
    assert "PR_RUN_CREATED_AT" in env, (
        "跑工具的步骤没有注入 PR_RUN_CREATED_AT ⇒ 基线时间窗无从限定"
    )
    value = str(env["PR_RUN_CREATED_AT"])
    assert "created_at" in value, (
        f"基线时间窗的上界没有取 created_at：{value!r}"
        "⇒ 基线可能取到 PR 事件派发之后的 main ⇒ 基线树 ≠ 执行树 ⇒ fail-open"
    )
    assert "run_started_at" not in value, (
        f"基线时间窗的上界用了 run_started_at：{value!r}"
        "⇒ 复跑时取到的是更晚的 main ⇒ 两次 run 的 CI 定义可能不同却判「可合并」"
        "⇒ fail-open"
    )


def test_merge_gate_leaves_the_main_tip_to_the_tool() -> None:
    """`M_T`（PR run 的 main 侧底座）必须由**工具**独立取。

    ⚠️ 闸门的 `BASE_RUN_ID`（基线 run）与工具要的 `M_T`（PR run 开跑那一刻的
    main tip）是**两件不同的事**：前者是「拿来对差的 run」，后者是「PR 那次
    合并用的 main 侧底座」。
    ⇒ 闸门若把 `M_T` 代取后传给工具，工具侧就**没有独立复核的机会**
    ⇒ 「基线 = fork 上同名 main 分支」那个洞会重新打开。

    ⇒ 契约：闸门**不传** `M_T`，由工具自己走 main 的 push run 列表取。
    """
    steps = [
        step
        for step in _steps()
        if "ci_failure_set" in str(step.get("run", ""))
    ]
    assert steps, "闸门里没有跑工具的步骤？"

    env = steps[0].get("env") or {}
    offending = [key for key in env if "MAIN_TIP" in key.upper()]
    assert not offending, (
        f"闸门把 M_T 传给了工具（{offending}）⇒ 工具失去独立复核 M_T 的机会"
    )

    from app.utils import ci_gh, ci_surface

    assert hasattr(ci_gh, "fetchMainTipShaAt"), "工具侧没有取 M_T 的入口"
    assert hasattr(ci_surface, "fetchMainTipShaAt"), (
        "ci_surface 没有引用取 M_T 的入口 ⇒ M_T 的来源不受工具侧复核"
    )
