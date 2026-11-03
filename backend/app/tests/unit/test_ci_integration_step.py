"""CI `backend-integration` 分片步骤的**可执行**守卫。

背景
----
`backend-integration` 的分片 job 曾是整套判据里最脆的一环：
没有完成标记 ⇒ job 一被取消（超时/被抢占）它的失败集合就是**空的**
⇒ 「失败集合对差」会得出「净新增 0」的**假结论** ⇒ 把没验证过的回归合进 main。

本文件把该步骤的行为**钉死在仓库里**（而不是只写在变更记录里）：

- **静态部分**：直接读 `.github/workflows/ci.yml`，断言 job 的结构性纪律
  （超时上限 / `fail-fast: false` / 分片总数与矩阵长度一致 / 标记协议 /
  失败路径也必须打标记 …）。
- **行为部分**：把 YAML 里那段 `run:` **原样抽出来**，用假的 `find` / `sort` / `uv`
  在真 bash 里跑一遍，逐条验证五种情形（清单生成失败 / 清单为空 / 空分片 /
  pytest 红 / pytest 绿）。

⚠️ 行为部分需要 bash ≥ 4.4（`mapfile -d ''`）。macOS 自带 bash 3.2 ⇒ 本机跳过，
CI（ubuntu-latest，bash 5）**必跑**。静态部分在任何平台都跑。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# backend/app/tests/unit/test_ci_integration_step.py → parents[4] == 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKFLOW_PATH = _REPO_ROOT / ".github/workflows/ci.yml"
_JOB_NAME = "backend-integration"
_STEP_NAME = "Run integration shard"
_MARKER = "INTEGRATION_SHARD_DONE shard=$shard_index status="

_UNIT_JOB_NAME = "backend-unit"
_UNIT_STEP_NAME = "Run unit shard"
_UNIT_MARKER = "UNIT_SHARD_DONE shard=$shard_index status="

# 行为部分要用 bash ≥ 4.4（`mapfile -d ''`）
_MIN_BASH_MAJOR = 4
_MIN_BASH_MINOR = 4
_SCRIPT_TIMEOUT_SEC = 60


def _bashVersion() -> tuple[int, int] | None:
    """取 bash 版本；没有 bash 则返回 None。"""
    bashPath = shutil.which("bash")
    if bashPath is None:
        return None
    completed = subprocess.run(
        [bashPath, "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    parts = completed.stdout.strip().split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def _bashTooOld() -> bool:
    version = _bashVersion()
    if version is None:
        return True
    return version < (_MIN_BASH_MAJOR, _MIN_BASH_MINOR)


def _bashFunctionBody(script: str, name: str) -> str:
    """抽出 bash 函数体（按行扫描到**同缩进**的结束 `}`）。

    ⚠️ **不能**用 `script.split("fail_shard() {", 1)[1].split("\\n          }", 1)[0]`：
    YAML 的块标量 `run: |` 在解析时会把内容**去缩进** ⇒ 函数结尾的 `}` 落在**第 0 列**
    ⇒ 那个模式**永远匹配不到** ⇒ `split` 返回**整段脚本**
    ⇒ 断言「函数体里有标记」恒成立 ⇒ **空转守卫**（本批评审 MEDIUM-4）。

    ⇒ 改成按行扫描 + **同缩进**的 `}` 收尾；找不到函数 / 找不到结束括号就**报错**
    （而不是默默返回整段脚本 ⇒ 那正是空转的成因）。
    """
    lines = script.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"{name}() {{":
            start = index
            break
    if start is None:
        raise AssertionError(f"脚本里找不到函数 {name}() {{")

    indent = len(lines[start]) - len(lines[start].lstrip())
    body: list[str] = []
    for line in lines[start + 1 :]:
        lineIndent = len(line) - len(line.lstrip())
        if line.strip() == "}" and lineIndent == indent:
            return "\n".join(body)
        body.append(line)
    raise AssertionError(f"函数 {name} 没有找到同缩进的结束括号")


@pytest.fixture(scope="module")
def workflow() -> dict:
    """解析后的 workflow（模块级缓存，避免每个用例重复读盘）。"""
    return yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def integrationJob(workflow: dict) -> dict:
    assert _JOB_NAME in workflow["jobs"], f"workflow 缺少 {_JOB_NAME} job"
    return workflow["jobs"][_JOB_NAME]


@pytest.fixture(scope="module")
def integrationStep(integrationJob: dict) -> dict:
    steps = [step for step in integrationJob["steps"] if step.get("name") == _STEP_NAME]
    assert len(steps) == 1, f"找不到 {_STEP_NAME} 步骤"
    return steps[0]


@pytest.fixture(scope="module")
def runBody(integrationStep: dict) -> str:
    return integrationStep["run"]


@pytest.fixture(scope="module")
def unitStep(workflow: dict) -> dict:
    steps = [step for step in workflow["jobs"][_UNIT_JOB_NAME]["steps"] if step.get("name") == _UNIT_STEP_NAME]
    assert len(steps) == 1, f"找不到 {_UNIT_STEP_NAME} 步骤"
    return steps[0]


@pytest.fixture(scope="module")
def unitRunBody(unitStep: dict) -> str:
    """`Run unit shard` 步骤的 run 块正文（行为部分要原样拿去跑）。"""
    return unitStep["run"]


# --------------------------------------------------------------------------
# 静态部分：结构性纪律
# --------------------------------------------------------------------------


def test_integration_job_keeps_generous_timeout(integrationJob: dict) -> None:
    """超时上限必须 ≥ 90 分钟。

    实测最坏分片 49 分 48 秒 ⇒ 60 分钟上限的余量只有 **1.21 倍**
    ⇒ runner 抖动 12% 就判 `cancelled` ⇒ 失败集合为空 ⇒ 假「净新增 0」。
    """
    assert integrationJob["timeout-minutes"] == 90


def test_integration_job_keeps_fail_fast_disabled(integrationJob: dict) -> None:
    """`fail-fast: false` 必须保留。

    matrix 默认 `fail-fast: true`：一个分片失败会取消其余分片
    ⇒ 被取消分片的失败集合同样是空的 ⇒ 又一次假「净新增 0」。
    """
    assert integrationJob["strategy"]["fail-fast"] is False


def test_shard_count_matches_matrix_length(integrationJob: dict, integrationStep: dict) -> None:
    """`SHARD_COUNT` 必须等于 `matrix.shard` 的**长度**。

    不一致 ⇒ 要么有分片永远轮不到，要么下标越界切出空分片
    ⇒ 空分片 ⇒ pytest 回落到 `testpaths` 收全树 ⇒ 假绿。
    """
    matrixShards = integrationJob["strategy"]["matrix"]["shard"]
    shardCount = integrationStep["env"]["SHARD_COUNT"]

    assert int(shardCount) == len(matrixShards)


def test_shard_index_is_taken_from_env_not_interpolated(integrationStep: dict, runBody: str) -> None:
    """分片号必须走 env，不能把 `${{ matrix.shard }}` 插进脚本文本。

    GH Actions 会先把 `${{ }}` 展开成**脚本文本**再交给 bash 解析 ⇒ 直接插值
    就是注入面。将来矩阵若改成动态来源（`fromJSON(needs...)`），风险会变成现实。
    """
    assert integrationStep["env"]["SHARD"] == "${{ matrix.shard }}"
    # 注释里可以**提到** `${{ }}`（说明为什么不用它），但**可执行行**里不能有。
    executableLines = [
        line for line in runBody.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("${{" in line for line in executableLines)


def test_run_uses_nul_separated_file_list(runBody: str) -> None:
    """清单必须 **NUL 分隔**：`-print0` + `sort -z` + `mapfile -d ''`。

    换行分隔时，文件名里含换行/空格就会被 mapfile 拆开
    ⇒ 静默丢文件 ⇒ 分片内容与预期不符（且丢的是哪几个还没人知道）。
    """
    assert "-print0" in runBody
    assert "sort -z" in runBody
    assert "mapfile -d ''" in runBody


def test_run_does_not_use_process_substitution_for_the_list(runBody: str) -> None:
    """清单**不能**用进程替换：`mapfile` 恒返回 0 ⇒ 生成器的退出码被吞掉。

    退出码被吞 ⇒ 清单生成失败静默变成「空数组」⇒ 空数组喂给 pytest
    ⇒ pytest 回落到 `testpaths=["app/tests"]` ⇒ 收全树 ⇒ 超时 ⇒ job 变 cancelled
    ⇒ 失败集合为空 ⇒ 又一次假「净新增 0」。
    """
    assert "< <(" not in runBody


def test_run_validates_shard_is_decimal(runBody: str) -> None:
    """分片号必须是十进制非负整数（`10#$SHARD` 强制十进制）。

    `$((08))` 在 bash 里是八进制语法错误；分片号若非数字，算术展开会炸掉，
    或者被静默当成 0 ⇒ 分片 0 跑两遍、其它分片空跑。
    上界 3 位数字：防御矩阵改成动态来源后的注入/超位。
    """
    assert "10#$SHARD" in runBody
    assert re.search(r"\[\[.*SHARD.*=~\s*\^\[0-9\]\{1,3\}\$", runBody) is not None


def test_run_prints_completion_marker(runBody: str) -> None:
    """完成标记必须在跑完后打印（且带 pytest 退出码）。"""
    assert _MARKER in runBody


def test_marker_shard_number_is_validated(runBody: str) -> None:
    """完成标记里的分片号必须来自**已校验**的数值，不能直接用 `$SHARD`。

    `SHARD` 来自 `matrix.shard`，而 **workflow 文件本身 PR 可改** ⇒ `SHARD`
    可能是任意文本（含换行 ⇒ 可在日志里伪造一行完成标记）。
    工具的标记正则是 `shard=(\\d+)` ⇒ 非数字会让标记**解析不出来**
    （⇒ 判「缺完成标记」⇒ 不可用 —— 虽然 fail-closed，但原因失真：
    真实原因是「分片号非法」，日志里却像「被取消」）。

    ⇒ 标记必须引用校验后的 `shard_index`（默认 0，校验通过后才覆盖）。
    """
    markerLines = [
        line.strip() for line in runBody.splitlines() if "_SHARD_DONE" in line
    ]
    assert markerLines, "脚本里找不到完成标记行"

    for line in markerLines:
        assert "$SHARD " not in line, f"完成标记直接用了未校验的 $SHARD：{line}"
        assert "$shard_index" in line, f"完成标记未使用校验后的分片号：{line}"


def test_every_failure_path_emits_the_marker(runBody: str) -> None:
    """**所有**失败路径都必须先打标记再退出。

    标记的意义：工具把「有标记且 status∉{0,1}」判为「跑完了但 pytest 没跑成」
    （复跑即可），把「无标记」判为「被取消」（要先解决超时/抢占）。
    两者都要复跑，但原因不同 ⇒ 必须能区分。

    ⇒ 脚本里只允许两个 `exit`：统一失败出口（`fail_shard`，内部打标记）
    与最后那句「按 pytest 退出码退出」。多出任何第三个 `exit` ⇒ 有人绕过
    统一出口 ⇒ 本用例转红。
    """
    exitStatements = {
        line.strip() for line in re.findall(r"^\s*exit .*$", runBody, re.MULTILINE)
    }

    assert exitStatements == {"exit 4", 'exit "$status"'}
    # 统一失败出口里必须打标记
    failShardBody = _bashFunctionBody(runBody, "fail_shard")
    assert _MARKER in failShardBody
    # 反空转（MEDIUM-4）：函数体**不得**包含脚本末尾那句 `exit "$status"`
    # —— 它在函数**外面**。若抽取失败返回了整段脚本，这一行必失败
    # ⇒ 证明边界真的被找到了，而不是「split 没匹配上 ⇒ 整段都算函数体」。
    assert 'exit "$status"' not in failShardBody
    # L10：行内 `exit`（`foo; exit 1` / `&& exit 1`）不算上面的「整行 exit」集合，
    # 但同样绕过统一出口 ⇒ 必须在**可执行行**层面再查一遍。
    executableLines = [
        line.strip()
        for line in runBody.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for line in executableLines:
        if re.search(r"\bexit\b", line):
            assert line in ("exit 4", 'exit "$status"'), f"发现未走统一出口的 exit：{line}"


# --------------------------------------------------------------------------
# 静态部分：backend-unit 同款纪律（LOW-4 —— 与 integration 同一套硬化）
# --------------------------------------------------------------------------


def test_unit_step_marks_failure_paths(unitStep: dict) -> None:
    """unit 分片的失败路径也必须先打 `UNIT_SHARD_DONE status=4` 再退出。

    与 integration 同理：「有标记且 status∉{0,1}」与「无标记」都判不可用，
    但原因不同 ⇒ 必须能区分。
    """
    body = unitStep["run"]
    failUnitBody = _bashFunctionBody(body, "fail_unit")
    assert _UNIT_MARKER in failUnitBody
    # 反空转（MEDIUM-4）：函数体不得包含脚本末尾那句 `exit "$status"`
    # （它在函数外面）⇒ 抽取失败返回整段脚本时，本行必失败。
    assert 'exit "$status"' not in failUnitBody


def test_unit_marker_shard_number_is_validated(unitStep: dict) -> None:
    """unit 的完成标记同样必须用**已校验**的分片号，不能直接用 `$SHARD`。

    理由与 integration 侧相同：`SHARD` 来自 workflow（PR 可改）⇒ 可能是任意文本。
    """
    body = unitStep["run"]
    markerLines = [line.strip() for line in body.splitlines() if "_SHARD_DONE" in line]
    assert markerLines, "脚本里找不到完成标记行"

    for line in markerLines:
        assert "$SHARD " not in line, f"完成标记直接用了未校验的 $SHARD：{line}"
        assert "$shard_index" in line, f"完成标记未使用校验后的分片号：{line}"


def test_unit_step_validates_shard_digits(unitStep: dict) -> None:
    """unit 分片号同样必须校验为不超过 3 位的十进制非负整数。"""
    body = unitStep["run"]
    assert "10#$SHARD" in body
    assert re.search(r"\[\[.*SHARD.*=~\s*\^\[0-9\]\{1,3\}\$", body) is not None


def test_unit_step_has_no_stray_exit(unitStep: dict) -> None:
    """unit 脚本里所有 `exit` 必须走统一出口（含行内 exit 的排查）。"""
    body = unitStep["run"]
    executableLines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for line in executableLines:
        if re.search(r"\bexit\b", line):
            assert line in ("exit 4", 'exit "$status"'), f"发现未走统一出口的 exit：{line}"


# --------------------------------------------------------------------------
# 静态部分：unit 与 integration 的**对称硬化**（L-2 / L-3 / L-9 / L-11）
# --------------------------------------------------------------------------


def test_unit_step_declares_shard_count_matching_matrix(workflow: dict, unitStep: dict) -> None:
    """unit 侧也必须声明 `SHARD_COUNT`，且与 `matrix.shard` 的**长度**一致。

    理由与 integration 侧相同：不一致 ⇒ 要么有分片永远轮不到，
    要么下标越界切出空分片 ⇒ 空分片 ⇒ pytest 回落到 `testpaths` 收全树
    ⇒ 假绿。
    """
    matrixShards = workflow["jobs"][_UNIT_JOB_NAME]["strategy"]["matrix"]["shard"]

    assert "SHARD_COUNT" in unitStep["env"], "unit 步骤必须声明 SHARD_COUNT"
    assert int(unitStep["env"]["SHARD_COUNT"]) == len(matrixShards)


def test_unit_run_passes_shard_count_to_the_shard_tool(unitStep: dict) -> None:
    """分片总数必须**由 `SHARD_COUNT` 传给分片工具**，不能写死字面量。

    写死 ⇒ 矩阵从 4 改成 8 时脚本仍按 4 分片 ⇒ 4 号分片永远空跑
    （空分片 ⇒ pytest 收全树 ⇒ 假绿），或分片内容与预期不符。
    """
    body = unitStep["run"]

    assert re.search(r'--shards\s+"\$SHARD_COUNT"', body) is not None, (
        "分片工具的 --shards 必须传 \"$SHARD_COUNT\"，不能写死字面量"
    )
    assert re.search(r"--shards\s+\d", body) is None, "分片总数不得写死为字面量"


def test_unit_run_uses_nul_separated_file_list(unitStep: dict) -> None:
    """unit 的分片清单也必须 **NUL 分隔**（与 integration 同款）。

    换行分隔 ⇒ 文件名里含换行/空格就被拆开 ⇒ 静默丢文件
    ⇒ 分片内容与预期不符（且丢的是哪几个还没人知道）。
    ⚠️ NUL 清单**只能落临时文件**：命令替换会剥掉 NUL 字节。

    ⚠️ 断言必须锚在**真实调用**上（变异取证 M25 实证）：
    脚本的注释里出现过 `--nul` 字面量（解释「为什么只能落文件」），
    若只断言 `"--nul" in body` ⇒ 把真实调用里的 `--nul` 删掉也照样绿
    ⇒ 恒绿守卫。下面用整条调用形态的正则锚定。
    """
    body = unitStep["run"]

    invocation = re.search(
        r"--root\s+app/tests/unit\s+--shards\s+\"\$SHARD_COUNT\"\s+"
        r"--shard\s+\"\$shard_index\"\s+--nul",
        body,
    )
    assert invocation is not None, "分片工具调用必须带 --nul（NUL 分隔输出）"
    assert re.search(r"mapfile\s+-d\s+''\s+-t\s+shard_tests\s+<\s+\"\$list_file\"", body) is not None, (
        "必须用 mapfile -d '' -t shard_tests < \"$list_file\" 读 NUL 清单"
    )
    assert "list_file=$(mktemp)" in body, "NUL 清单必须落临时文件（命令替换会剥掉 NUL）"
    assert not re.search(r"shard_list=\$\(", body), "不得用命令替换装清单（会剥掉 NUL 字节）"


def test_both_jobs_use_invalid_shard_sentinel(runBody: str, unitStep: dict) -> None:
    """分片号非法时，完成标记必须打**哨兵值 -1**，不能打 0。

    打 0 ⇒ 工具看到的是「shard 0 的正常标记」⇒ 真实原因（分片号非法）
    被掩盖成「标记与 job 不符」；更糟的是「打 0」是在**谎报**本 job 的分片号。

    ⇒ 未校验前一律用 -1 占位，校验通过后才覆盖成真实分片号；
    工具侧 `ci_pytest_log.INVALID_SHARD = -1` 会把这种标记判成
    「CI 脚本判定分片号非法 ⇒ 该 job 一个用例都没跑」。
    """
    for label, body in (("integration", runBody), ("unit", unitStep["run"])):
        assert "shard_index=-1" in body, f"{label} 侧未用 -1 作为非法分片号占位"
        assert "shard_index=0" not in body, f"{label} 侧仍在用 0 占位（会谎报分片号）"


def test_both_jobs_trap_temp_file_cleanup(runBody: str, unitStep: dict) -> None:
    """临时清单文件必须用 `trap ... EXIT` 兜底清理。

    脚本中途被 `set -e` 打断、或被信号杀死时，临时文件会留下；
    `trap` 保证无论走哪条出口（含 `fail_*` 的 `exit`）都清掉。
    """
    for label, body in (("integration", runBody), ("unit", unitStep["run"])):
        assert re.search(r"trap\s+'rm -f \"\$list_file\"'\s+EXIT", body) is not None, (
            f"{label} 侧缺少 trap 清理临时清单文件"
        )


# --------------------------------------------------------------------------
# 行为部分：把 YAML 里那段 run 原样跑一遍
# --------------------------------------------------------------------------


_STUB_FIND = """#!/bin/bash
# 契约检查：调用形态必须是 `find app/tests/integration -name 'test_*.py' -print0`。
# 少任何一部分 ⇒ 清单范围不对（收进别的目录 / 漏 -name 过滤 / 不是 NUL 分隔）
# ⇒ 分片内容与预期不符。这里逐个实参断言，防脚本将来改调用形态时静默漂移。
seen_path=0
seen_name=0
seen_print0=0
prev=""
for arg in "$@"; do
  case "$arg" in
    -print0) seen_print0=1 ;;
    app/tests/integration) seen_path=1 ;;
  esac
  if [ "$prev" = "-name" ] && [ "$arg" = "test_*.py" ]; then seen_name=1; fi
  prev="$arg"
done
if [ "$seen_print0" != "1" ] || [ "$seen_path" != "1" ] || [ "$seen_name" != "1" ]; then
  echo "stub-find: 契约不符（path=$seen_path name=$seen_name print0=$seen_print0）" >&2
  exit 64
fi
case "${STUB_FIND_MODE:-ok}" in
  fail) exit 1 ;;
  empty) exit 0 ;;
esac
count="${STUB_FIND_COUNT:-8}"
i=0
while [ "$i" -lt "$count" ]; do
  case "$i" in
    3)
      # 文件名里带空格 ⇒ 换行/空白分隔的实现会在这里把清单拆坏
      printf 'app/tests/integration/test_gen_03 with space.py\\0'
      ;;
    5)
      # 文件名里带换行 ⇒ 只有 NUL 分隔能保住它作为一个完整文件名
      printf 'app/tests/integration/test_gen_05\\nwith newline.py\\0'
      ;;
    *)
      printf 'app/tests/integration/test_gen_%02d.py\\0' "$i"
      ;;
  esac
  i=$((i + 1))
done
"""

_STUB_SORT = """#!/bin/bash
# 契约检查：必须用 -z（NUL 分隔排序）
z=0
for arg in "$@"; do
  if [ "$arg" = "-z" ]; then z=1; fi
done
if [ "$z" != "1" ]; then
  echo "stub-sort: 缺少 -z" >&2
  exit 64
fi
exec python3 "$(dirname "$0")/sort_nul.py"
"""

_STUB_SORT_PY = """import sys

data = sys.stdin.buffer.read()
items = sorted(item for item in data.split(b"\\0") if item)
sys.stdout.buffer.write(b"".join(item + b"\\0" for item in items))
"""

_STUB_UV = """#!/bin/bash
# 按 NUL 分隔记录每个实参（保留参数边界：含空格/换行的文件名必须能原样读回），
# 并按 STUB_PYTEST_EXIT 退出
printf '%s\\0' "$@" > "$STUB_UV_ARGV_FILE"
exit "${STUB_PYTEST_EXIT:-0}"
"""


def _readNulFile(path: Path) -> tuple[str, ...]:
    """读 NUL 分隔的文件 ⇒ 实参元组（保住含空格/换行的文件名边界）。"""
    if not path.exists():
        return ()
    raw = path.read_bytes()
    return tuple(item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item)


class _RunResult:
    """一次脚本执行的结果。"""

    def __init__(self, completed: subprocess.CompletedProcess[str], argvFile: Path) -> None:
        self.exitCode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.pytestCalled = argvFile.exists()
        self.pytestArgv = _readNulFile(argvFile)


@pytest.fixture
def runStep(runBody: str, tmp_path: Path):
    """跑一次 YAML 里的 run 块（假 find / sort / uv）。"""

    def _run(
        *,
        shard: str = "0",
        shardCount: str = "4",
        findMode: str = "ok",
        findCount: int = 8,
        pytestExit: int = 0,
    ) -> _RunResult:
        stubDir = tmp_path / "bin"
        stubDir.mkdir(exist_ok=True)
        for name, content in (
            ("find", _STUB_FIND),
            ("sort", _STUB_SORT),
            ("uv", _STUB_UV),
        ):
            stubPath = stubDir / name
            stubPath.write_text(content, encoding="utf-8")
            stubPath.chmod(0o755)
        (stubDir / "sort_nul.py").write_text(_STUB_SORT_PY, encoding="utf-8")

        scriptPath = tmp_path / "step.sh"
        scriptPath.write_text(runBody, encoding="utf-8")
        argvFile = tmp_path / "uv_argv.txt"

        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{stubDir}{os.pathsep}{env.get('PATH', '')}",
                "SHARD": shard,
                "SHARD_COUNT": shardCount,
                "STUB_FIND_MODE": findMode,
                "STUB_FIND_COUNT": str(findCount),
                "STUB_PYTEST_EXIT": str(pytestExit),
                "STUB_UV_ARGV_FILE": str(argvFile),
            }
        )
        completed = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(scriptPath)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_SCRIPT_TIMEOUT_SEC,
            cwd=tmp_path,
            env=env,
        )
        return _RunResult(completed, argvFile)

    return _run


requiresBash44 = pytest.mark.skipif(
    _bashTooOld(),
    reason="行为部分需要 bash ≥ 4.4（`mapfile -d ''`）；macOS 自带 bash 3.2 ⇒ 本机跳过，CI 必跑",
)


@requiresBash44
def test_step_fails_hard_when_file_list_generation_fails(runStep) -> None:
    """清单生成失败 ⇒ 退出码 4 + **打了标记** + **pytest 没被调用**。"""
    result = runStep(findMode="fail")

    assert result.exitCode == 4
    assert "INTEGRATION_SHARD_DONE shard=0 status=4" in result.stdout
    assert result.pytestCalled is False


@requiresBash44
def test_step_fails_hard_when_file_list_is_empty(runStep) -> None:
    """清单为空 ⇒ 退出码 4 + 标记 + pytest 没被调用。"""
    result = runStep(findMode="empty")

    assert result.exitCode == 4
    assert "INTEGRATION_SHARD_DONE shard=0 status=4" in result.stdout
    assert result.pytestCalled is False


@requiresBash44
def test_step_fails_hard_when_shard_is_empty(runStep) -> None:
    """空分片 ⇒ 退出码 4 + 标记 + pytest 没被调用。

    空分片若放行，pytest 会不带路径参数 ⇒ 回落到 `testpaths=["app/tests"]`
    ⇒ 把 unit+integration+e2e 全收进来 ⇒ 本 job 没有 unit 那套环境 ⇒ 超时
    ⇒ job 变 cancelled ⇒ 失败集合为空 ⇒ 假「净新增 0」。
    """
    result = runStep(shard="3", findCount=1)

    assert result.exitCode == 4
    assert "INTEGRATION_SHARD_DONE shard=3 status=4" in result.stdout
    assert result.pytestCalled is False


@requiresBash44
def test_step_rejects_non_numeric_shard(runStep) -> None:
    """分片号不是数字 ⇒ 退出码 4 + pytest 没被调用。"""
    result = runStep(shard="abc")

    assert result.exitCode == 4
    assert result.pytestCalled is False


@requiresBash44
def test_step_rejects_shard_index_out_of_range(runStep) -> None:
    """分片号越界（≥ SHARD_COUNT）⇒ 退出码 4 + pytest 没被调用。"""
    result = runStep(shard="4", shardCount="4")

    assert result.exitCode == 4
    assert result.pytestCalled is False


@requiresBash44
def test_step_reports_red_run_with_marker(runStep) -> None:
    """pytest 真红 ⇒ 退出码 1 + 标记 `status=1`（真红的失败集合**可用**）。"""
    result = runStep(pytestExit=1)

    assert result.exitCode == 1
    assert "INTEGRATION_SHARD_DONE shard=0 status=1" in result.stdout
    assert result.pytestCalled is True


@requiresBash44
def test_step_reports_green_run_with_marker(runStep) -> None:
    """pytest 全绿 ⇒ 退出码 0 + 标记 `status=0`。"""
    result = runStep(pytestExit=0)

    assert result.exitCode == 0
    assert "INTEGRATION_SHARD_DONE shard=0 status=0" in result.stdout
    assert result.pytestCalled is True


@requiresBash44
def test_step_slices_files_by_shard_index(runStep) -> None:
    """分片切分必须是「下标 mod SHARD_COUNT」。

    8 个文件 / 4 个分片 ⇒ shard 0 拿到第 0 与第 4 个。
    """
    result = runStep(shard="0", shardCount="4", findCount=8)

    assert result.exitCode == 0
    assert "app/tests/integration/test_gen_00.py" in result.pytestArgv
    assert "app/tests/integration/test_gen_04.py" in result.pytestArgv
    assert "app/tests/integration/test_gen_01.py" not in result.pytestArgv


@requiresBash44
def test_step_preserves_space_containing_filename(runStep) -> None:
    """M2：含空格的文件名必须**原样**作为单个实参传给 pytest。

    8 个文件 / 4 个分片 ⇒ 第 3 个文件（带空格）落在 shard 3。
    换行/空白分隔的实现会把它拆成两个实参 ⇒ 这里立刻转红。
    """
    result = runStep(shard="3", shardCount="4", findCount=8)

    assert result.exitCode == 0
    assert "app/tests/integration/test_gen_03 with space.py" in result.pytestArgv


@requiresBash44
def test_step_preserves_newline_containing_filename(runStep) -> None:
    """L10：含换行的文件名必须**原样**作为一个实参传给 pytest。

    8 个文件 / 4 个分片 ⇒ 第 5 个文件（带换行）落在 shard 1。
    只有 NUL 分隔（`-print0` / `sort -z` / `mapfile -d ''`）能保住它：
    换行分隔的实现会把它拆成两个实参 ⇒ 这里立刻转红。
    """
    result = runStep(shard="1", shardCount="4", findCount=8)

    assert result.exitCode == 0
    assert "app/tests/integration/test_gen_05\nwith newline.py" in result.pytestArgv


# --------------------------------------------------------------------------
# 行为部分：backend-unit 分片步骤（与 integration 同款纪律）
# --------------------------------------------------------------------------

_STUB_UV_UNIT = """#!/bin/bash
# 区分两种调用：
#   `uv run pytest <files> ...`            ⇒ 记录实参，按 STUB_PYTEST_EXIT 退出
#   `uv run python -m app.utils.ci_shard …` ⇒ 输出分片清单（NUL 分隔）
is_pytest=0
for arg in "$@"; do
  if [ "$arg" = "pytest" ]; then is_pytest=1; fi
done
if [ "$is_pytest" = "1" ]; then
  printf '%s\\0' "$@" > "$STUB_UV_ARGV_FILE"
  exit "${STUB_PYTEST_EXIT:-0}"
fi

# ---- 分片清单调用：契约检查 ----
seen_nul=0
seen_root=0
shards=""
shard=""
prev=""
for arg in "$@"; do
  case "$arg" in
    --nul) seen_nul=1 ;;
    app/tests/unit) seen_root=1 ;;
  esac
  if [ "$prev" = "--shards" ]; then shards="$arg"; fi
  if [ "$prev" = "--shard" ]; then shard="$arg"; fi
  prev="$arg"
done
if [ "$seen_nul" != "1" ] || [ "$seen_root" != "1" ]; then
  echo "stub-uv: ci_shard 调用形态不符（nul=$seen_nul root=$seen_root）" >&2
  exit 64
fi
printf '%s\\0' "$shards" "$shard" > "$STUB_SHARD_ARGV_FILE"
case "${STUB_SHARD_MODE:-ok}" in
  fail) exit 1 ;;
  empty) exit 0 ;;
esac
count="${STUB_SHARD_COUNT:-3}"
i=0
while [ "$i" -lt "$count" ]; do
  case "$i" in
    1) printf 'app/tests/unit/test_gen_01 with space.py\\0' ;;
    2) printf 'app/tests/unit/test_gen_02\\nwith newline.py\\0' ;;
    *) printf 'app/tests/unit/test_gen_%02d.py\\0' "$i" ;;
  esac
  i=$((i + 1))
done
"""


class _UnitRunResult:
    """一次 unit 步骤执行的结果。"""

    def __init__(
        self,
        completed: subprocess.CompletedProcess[str],
        argvFile: Path,
        shardArgvFile: Path,
    ) -> None:
        self.exitCode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.pytestCalled = argvFile.exists()
        self.pytestArgv = _readNulFile(argvFile)
        # 分片工具的实参（`--shards` / `--shard` 的值）⇒ 验证参数真的传下去了
        self.shardArgv = _readNulFile(shardArgvFile)


@pytest.fixture
def runUnitStep(unitRunBody: str, tmp_path: Path):
    """跑一次 YAML 里 `Run unit shard` 的 run 块（假 uv）。"""

    def _run(
        *,
        shard: str = "0",
        shardCount: str = "4",
        shardMode: str = "ok",
        shardEntries: int = 3,
        pytestExit: int = 0,
    ) -> _UnitRunResult:
        stubDir = tmp_path / "bin"
        stubDir.mkdir(exist_ok=True)
        stubPath = stubDir / "uv"
        stubPath.write_text(_STUB_UV_UNIT, encoding="utf-8")
        stubPath.chmod(0o755)

        scriptPath = tmp_path / "unit_step.sh"
        scriptPath.write_text(unitRunBody, encoding="utf-8")
        argvFile = tmp_path / "uv_argv.txt"
        shardArgvFile = tmp_path / "shard_argv.txt"

        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{stubDir}{os.pathsep}{env.get('PATH', '')}",
                "SHARD": shard,
                "SHARD_COUNT": shardCount,
                "STUB_SHARD_MODE": shardMode,
                "STUB_SHARD_COUNT": str(shardEntries),
                "STUB_PYTEST_EXIT": str(pytestExit),
                "STUB_UV_ARGV_FILE": str(argvFile),
                "STUB_SHARD_ARGV_FILE": str(shardArgvFile),
            }
        )
        completed = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(scriptPath)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_SCRIPT_TIMEOUT_SEC,
            cwd=tmp_path,
            env=env,
        )
        return _UnitRunResult(completed, argvFile, shardArgvFile)

    return _run


@requiresBash44
def test_unit_step_reports_green_run_with_marker(runUnitStep) -> None:
    """unit 分片全绿 ⇒ 退出码 0 + 标记 `status=0`。"""
    result = runUnitStep(pytestExit=0)

    assert result.exitCode == 0
    assert "UNIT_SHARD_DONE shard=0 status=0" in result.stdout
    assert result.pytestCalled is True


@requiresBash44
def test_unit_step_reports_red_run_with_marker(runUnitStep) -> None:
    """unit 分片真红 ⇒ 退出码 1 + 标记 `status=1`（真红的失败集合**可用**）。"""
    result = runUnitStep(pytestExit=1)

    assert result.exitCode == 1
    assert "UNIT_SHARD_DONE shard=0 status=1" in result.stdout
    assert result.pytestCalled is True


@requiresBash44
def test_unit_step_rejects_non_numeric_shard_with_sentinel(runUnitStep) -> None:
    """分片号非法 ⇒ 退出码 4 + 标记 `shard=-1`（哨兵）+ pytest 没被调用。

    ⚠️ 标记必须是 **-1** 而不是 0：打 0 等于谎报「我是 0 号分片」
    ⇒ 工具会把真实原因（分片号非法）掩盖成「标记与 job 名不符」。
    """
    result = runUnitStep(shard="abc")

    assert result.exitCode == 4
    assert "UNIT_SHARD_DONE shard=-1 status=4" in result.stdout
    assert result.pytestCalled is False


@requiresBash44
def test_unit_step_rejects_shard_index_out_of_range(runUnitStep) -> None:
    """分片号越界（≥ SHARD_COUNT）⇒ 退出码 4 + pytest 没被调用。"""
    result = runUnitStep(shard="4", shardCount="4")

    assert result.exitCode == 4
    assert result.pytestCalled is False


@requiresBash44
def test_unit_step_fails_hard_when_shard_list_generation_fails(runUnitStep) -> None:
    """分片清单生成失败 ⇒ 退出码 4 + **打了标记** + **pytest 没被调用**。"""
    result = runUnitStep(shardMode="fail")

    assert result.exitCode == 4
    assert "UNIT_SHARD_DONE shard=0 status=4" in result.stdout
    assert result.pytestCalled is False


@requiresBash44
def test_unit_step_fails_hard_when_shard_is_empty(runUnitStep) -> None:
    """空分片 ⇒ 退出码 4 + 标记 + pytest 没被调用。

    空分片若放行，pytest 会不带路径参数 ⇒ 回落到 `testpaths=["app/tests"]`
    ⇒ 把 unit+integration+e2e 全收进来 ⇒ 本 job 没有那套环境 ⇒ 超时
    ⇒ job 变 cancelled ⇒ 失败集合为空 ⇒ 假「净新增 0」。
    """
    result = runUnitStep(shardEntries=0)

    assert result.exitCode == 4
    assert "UNIT_SHARD_DONE shard=0 status=4" in result.stdout
    assert result.pytestCalled is False


@requiresBash44
def test_unit_step_passes_shard_count_to_shard_tool(runUnitStep) -> None:
    """`SHARD_COUNT` 必须真的传给分片工具（脚本里写死 4 的话本例会转红）。"""
    result = runUnitStep(shard="2", shardCount="4")

    assert result.exitCode == 0
    assert result.shardArgv == ("4", "2")


@requiresBash44
def test_unit_step_preserves_space_containing_filename(runUnitStep) -> None:
    """含空格的文件名必须**原样**作为单个实参传给 pytest（NUL 分隔才保得住）。"""
    result = runUnitStep()

    assert result.exitCode == 0
    assert "app/tests/unit/test_gen_01 with space.py" in result.pytestArgv


@requiresBash44
def test_unit_step_preserves_newline_containing_filename(runUnitStep) -> None:
    """含换行的文件名必须**原样**作为一个实参传给 pytest。

    换行分隔的实现会把它拆成两个实参 ⇒ 这里立刻转红。
    """
    result = runUnitStep()

    assert result.exitCode == 0
    assert "app/tests/unit/test_gen_02\nwith newline.py" in result.pytestArgv
