#!/usr/bin/env python3
"""CI 判据守卫的**变异取证**：每条守卫都必须证明「把它改坏 ⇒ 对应用例转红」。

规矩（见 `Harness/rules/测试规范.md`）：守卫不是恒绿 ⇒ 必须拿变异取证。

⚠️ 本文件**只放跑者**（改坏 → 跑用例 → 判三态）；**307 组**变异的清单在
`backend/scripts/ci_mutation_cases.py`（M37 起）+ `ci_mutation_cases_core.py`
（M1–M36，纯数据）+ `ci_mutation_cases_r14/r16/r18/r20/r21/r23/r24/r26/
r28/r30/r32/r34.py`（各轮收口/修复批，纯数据）——拆开是为了文件都不超 800 行上限。

扩展说明：
- 支持 **repo 根路径**（`.github/workflows/ci.yml`）—— 作用域 `repo`；
- 支持 `occurrence`：**锚点第 N 次出现**（同一行在 unit / integration 两段里各出现一次时用）。

用法（在仓库根或任意子目录执行均可，脚本按自身位置自动定位仓库根）：

    python3 backend/scripts/ci_mutation_evidence.py            # 全部 307 组
    python3 backend/scripts/ci_mutation_evidence.py M5 M6      # 只跑指定组

环境要求：`TEST_DATABASE_URL` / `TEST_NEO4J_URI` / `TEST_NEO4J_PASSWORD`
（unit 守卫的环境契约，缺一即拒跑）。
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(os.environ.get("QA_REPO_ROOT", pathlib.Path(__file__).resolve().parents[2]))
BACKEND = REPO / "backend"

# 变异清单在同目录的 `ci_mutation_cases.py` 里；把它所在的 `backend/` 加进
# `sys.path`，才能以包内模块的方式 import（直接以脚本方式运行时 `sys.path[0]`
# 是 `backend/scripts` 本身 ⇒ `scripts` 包不可见）。
sys.path.insert(0, str(BACKEND))

from scripts.ci_mutation_cases import MUTATIONS  # noqa: E402

_REQUIRED_ENV = ("TEST_DATABASE_URL", "TEST_NEO4J_URI", "TEST_NEO4J_PASSWORD")

# 单条守卫用例跑不完就当取证失败（fail-closed：宁可误报也不无限等）
CASE_TIMEOUT_SECONDS = 300

# pytest 的退出码语义：
# 0 = 全部通过；**1 = 有断言失败**；2 = 收尾阶段出错（收集失败等）；3 = 内部错误；
# 4 = 命令行用法错；5 = **没收集到任何用例**。
# ⚠️ 只有 **1** 才是「守卫断言失败（转红）」⇒ 其余非 0 都是「没跑起来」。
_PYTEST_EXIT_ASSERTION_FAILED = 1

# 第二十六轮评审 M-26-3：**崩溃红**不是转红——变异若把分析器改成会抛异常的
# 形态（如杀掉 isinstance 分派臂 ⇒ 下一行 AttributeError），退出码 1 与
# `FAILED <nodeid>` 都满足，但证明的是「变异让分析器崩了」，不是「守卫抓到
# 了逃逸」⇒ 输出里出现**分析器栈帧**即判取证失败。标记取分析器内部函数名
# 的栈帧行（`  File "...", in findImportTimeHooks`）；正常断言失败发生在
# 分析器调用**返回之后**（守卫已拿到结果再 assert）⇒ 栈帧只有测试帧，
# 不会出现这些内部帧。
_ANALYZER_FRAME_MARKERS = (
    "in findImportTimeHooks",
    "in _findImportTimeHooksImpl",
    "in scanExec",
    "in scanCallee",
)


def buildEnv() -> dict[str, str]:
    """继承当前 shell 环境（凭据不入库）；缺必需变量就 fail-fast。"""
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"缺少必需环境变量：{', '.join(missing)}（见文件头环境要求）")
    return dict(os.environ)


def resolve(scope: str, relPath: str) -> pathlib.Path:
    return (BACKEND if scope == "backend" else REPO) / relPath


def anchorProblem(source: str, original: str, occurrence: int) -> str | None:
    """锚点核验：`count == occurrence` **精确相等**，不精确时返回失败说明。

    ⚠️ 旧判据 `total < occurrence` 是**假绿灯**——锚成为更深缩进同文本臂的
    子串时 total 会变大（M169/M218 实证：count=2 vs occurrence=1 杀错臂
    ⇒ 守卫仍绿）。锚点核验的契约是**精确相等**：多了说明锚不独占
    （可能杀错臂），少了说明锚不存在，都判取证失败。
    """
    total = source.count(original)
    if total != occurrence:
        return f"锚点出现 {total} 次（要求恰好 {occurrence} 次）"
    return None


def replaceOccurrence(source: str, original: str, mutated: str, occurrence: int) -> str | None:
    """把锚点的第 `occurrence` 次出现替换掉；找不到返回 None。"""
    index = -1
    for _ in range(occurrence):
        index = source.find(original, index + 1)
        if index < 0:
            return None
    return source[:index] + mutated + source[index + len(original) :]


def _failureMarkerFound(combined: str, case: str) -> bool:
    """输出里必须有**这条用例**的失败标记（不是「文件里随便哪个失败」）。

    ⚠️ 2026-11-07 代码评审 MEDIUM-4：**裸文件名 / 裸用例名都不算数**。
    - 文件级用例（`app/tests/unit/test_ci_surface.py`）原先只判
      「输出里含 `test_ci_surface.py`」⇒ 那是 pytest 的**路径前缀**，
      **任意**上下文（收集进度行、warning）都满足 ⇒ 别的用例失败会被算成
      「这条守卫转红」⇒ **假取证**。
    - 用例级用例原先只判「输出里含用例名」⇒ `test_x` 是 `test_x_extra` 的
      **子串** ⇒ 同名兄弟用例失败会被算成命中 ⇒ 同样是假取证。

    ⇒ 现在要求 pytest 的**短摘要行**里出现 `FAILED <nodeid>` / `ERROR <nodeid>`
    （**整行匹配**：nodeid 后面必须跟空白或行尾 ⇒ 不能是更长 nodeid 的前缀），
    文件级则要求 `FAILED <file>::…`。
    """
    if "::" in case:
        pattern = rf"^(?:FAILED|ERROR) {re.escape(case)}(?:\s|$)"
    else:
        # 文件级：必须是**该文件下的某个 nodeid** 失败（`FAILED <file>::test_x`）
        pattern = rf"^(?:FAILED|ERROR) {re.escape(case)}::\S+(?:\s|$)"
    return re.search(pattern, combined, flags=re.MULTILINE) is not None


def run_case(case: str, env: dict[str, str]) -> tuple[str, str]:
    """跑一条守卫用例 ⇒ `(verdict, detail)`。

    ⚠️ **verdict 必须有三态，不能压成布尔**（2026-11-07 安全评审 HIGH-2）：

    | verdict | pytest 退出码 | 含义 |
    |---|---|---|
    | `"red"` | **1** | **这条用例没通过**（断言失败或报错）⇒ 变异被守卫抓到了 ✔ |
    | `"green"` | 0 | 守卫仍然绿 ⇒ 变异**没**被抓住 ⇒ 取证失败 |
    | `"inconclusive"` | 2 / 3 / 4 / 5 | 用例**根本没跑起来**（收尾错误 / 内部错误 / 用法错 / 没收集到用例） |

    ⚠️ 只看「退出码非 0」会把「**用例没跑**」（例如用例文件还没 `git add`、
    收集阶段就报错）冒充成「守卫抓到了变异」⇒ **假取证** ⇒ 取证报告全绿而
    守卫其实根本没被测过。⇒ 2/3/4/5 一律判 `inconclusive`（**取证失败**，不是转红）。

    ⚠️ 另外必须确认**该用例确实被执行且确实是它没过**（MEDIUM-4）：
    退出码 1 时还要在输出里找到**它自己的**失败标记
    ⇒ 否则「别的用例失败」会被算成「这条用例转红」。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", case, "-q", "--no-header", "-p", "no:randomly"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=CASE_TIMEOUT_SECONDS,
    )
    output = completed.stdout[-800:]
    combined = completed.stdout + completed.stderr
    if completed.returncode == 0:
        return "green", output
    if completed.returncode != _PYTEST_EXIT_ASSERTION_FAILED:
        return (
            "inconclusive",
            f"pytest 退出码 {completed.returncode}（不是 {_PYTEST_EXIT_ASSERTION_FAILED}）"
            f" ⇒ 用例没跑起来 ⇒ 不能算「转红」\n{output}",
        )
    if not _failureMarkerFound(combined, case):
        return (
            "inconclusive",
            f"pytest 退出码 1，但输出里没有 {case!r} 自己的失败标记"
            f" ⇒ 不是它失败\n{output}",
        )
    # 第二十六轮评审 M-26-3：崩溃红取证失败（见 _ANALYZER_FRAME_MARKERS 注释）。
    if any(marker in combined for marker in _ANALYZER_FRAME_MARKERS):
        return (
            "inconclusive",
            "输出含分析器内部栈帧 ⇒ 崩溃红（变异让分析器异常）"
            " ⇒ 不能证明检测能力\n" + output,
        )
    return "red", output


def main() -> int:
    only = set(sys.argv[1:])
    env = buildEnv()
    results: list[tuple[str, bool, str]] = []
    for name, scope, relPath, original, mutated, case, occurrence in MUTATIONS:
        if only and name not in only:
            continue
        path = resolve(scope, relPath)
        source = path.read_text()
        # 第二十七轮评审 M-27-2（第三十轮 M-29-11 提取成可单测的 helper）：
        # `count == occurrence` 精确契约，不精确即取证失败。
        problem = anchorProblem(source, original, occurrence)
        if problem is not None:
            print(f"✗ {name}: 变异锚点在 {relPath} 里{problem}")
            results.append((name, False, "锚点不精确"))
            continue
        changed = replaceOccurrence(source, original, mutated, occurrence)
        assert changed is not None
        path.write_text(changed)
        try:
            verdict, output = run_case(case, env)
        finally:
            path.write_text(source)
        wentRed = verdict == "red"
        results.append((name, wentRed, case))
        mark = "✓" if wentRed else "✗"
        label = {
            "red": "转红（守卫有效）",
            "green": "仍然绿（守卫无效！）",
            "inconclusive": "取证失败（用例没跑起来 ⇒ 不算转红）",
        }[verdict]
        print(f"{mark} {name}: {case} ⇒ {label}")
        if not wentRed:
            print(output)

    bad = [name for name, ok, _ in results if not ok]
    print(f"\n合计 {len(results)} 组变异，净漏 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
