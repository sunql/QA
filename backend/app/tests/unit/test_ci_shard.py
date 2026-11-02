"""CI 测试分片分配：必须「稳定」（增删文件不移动既有文件）。

背景：`.github/workflows/ci.yml` 的 `backend-unit` 原本把整个 unit 套件塞进**一个**
job，`timeout-minutes: 45`，而套件基线要跑 **42 分钟** ⇒ 余量仅 ~7% ⇒ runner 抖动
就判 `cancelled`。而 `cancelled` 的 job 失败集合是**空的** ⇒ 「失败集合对差」会得出
「净新增 0」的**假结论**（见 Harness/changes/fix-ci-unit-sharding/summary.md）。

修法是拆成分片 job。但仓里 integration/ 现有分片用的是「排序后下标 mod 4」，
`Harness/rules/测试规范.md` 已记录它因此爆过两次（加一个文件 ⇒ 其后所有文件分片
归属整体轮转一位 ⇒ 既有顺序依赖测试突然红）。

⇒ unit/ 分片改用**路径稳定哈希**：增删改任何一个文件都不会移动其它文件的分片归属。
本文件把这条性质钉死，并保证它不是恒绿（改算法即转红）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from app.utils.ci_shard import assignShard, assignShards, listShardFiles, scanTestFiles

# backend/app/tests/unit/test_ci_shard.py → parents[3] == backend/
_BACKEND_DIR = Path(__file__).resolve().parents[3]
# 相对 backend/ 的路径（CLI 子进程用它，因为子进程的 cwd 是 backend/）
_UNIT_ROOT_REL = Path("app/tests/unit")
# 绝对路径（进程内调用用它 ⇒ 不受 pytest 进程 CWD 影响）
_UNIT_ROOT_ABS = _BACKEND_DIR / _UNIT_ROOT_REL
_SUBPROCESS_TIMEOUT_SEC = 60


def _runCli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """跑一次分片 CLI。固定 argv、无 shell=True、限时。"""
    return subprocess.run(
        [sys.executable, "-m", "app.utils.ci_shard", *args],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )


def _relativeToUnitRoot(path: str) -> str:
    """把 `app/tests/unit/xxx` 形式的路径转成相对 unit 根的 POSIX 路径。"""
    return Path(path).relative_to(_UNIT_ROOT_REL).as_posix()


def test_scan_finds_every_unit_test_file() -> None:
    """扫描结果必须覆盖 unit/ 下全部 test_*.py（含子目录），且是 POSIX 相对路径。

    `scanTestFiles` 返回**相对扫描根**的路径（哈希输入），不含根目录前缀 ——
    这样同一份代码换个 checkout 位置，分片结果不变。

    覆盖面用**独立的 `os.walk` 枚举**做交叉验证，而不是钉死某个文件名
    （钉死文件名会让「那个文件被改名」变成 CI 红）。
    """
    files = scanTestFiles(_UNIT_ROOT_ABS)

    expected = sorted(
        Path(os.path.relpath(os.path.join(dirPath, name), _UNIT_ROOT_ABS)).as_posix()
        for dirPath, _, names in os.walk(_UNIT_ROOT_ABS)
        for name in names
        if name.startswith("test_") and name.endswith(".py")
    )

    assert files, "unit/ 下必须能扫到测试文件"
    assert files == expected, "扫描结果与独立枚举不一致（有漏扫或多扫）"
    assert files == sorted(files), "必须排序，保证同一份代码在任何机器上分片一致"
    assert all(not path.startswith("/") for path in files), files[:5]
    assert all(".." not in path for path in files), files[:5]
    assert all(Path(path).name.startswith("test_") for path in files), files[:5]
    assert all(path.endswith(".py") for path in files), files[:5]


def test_scanner_pattern_matches_pytest_python_files() -> None:
    """扫描用的 glob 必须与 pytest 的 `python_files` 一致。

    两者是**各自独立**的定义。若 pytest 改成 `["test_*.py", "*_test.py"]` 而扫描器
    没跟上，被漏掉的文件会**静默地不再被任何分片收集** ⇒ 既不失败也不报警
    ⇒ 典型的假绿（本批要消灭的正是这一类）。
    """
    config = tomllib.loads((_BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    pytestPatterns = config["tool"]["pytest"]["ini_options"]["python_files"]

    from app.utils import ci_shard

    assert pytestPatterns == [ci_shard._TEST_FILE_PATTERN], (
        f"pytest 的 python_files={pytestPatterns} 与扫描器 pattern="
        f"{ci_shard._TEST_FILE_PATTERN} 不一致 ⇒ 会有测试文件被静默漏掉"
    )


def test_shards_partition_the_whole_tree() -> None:
    """各分片必须是全集的一个**划分**：无遗漏、无重复。"""
    files = scanTestFiles(_UNIT_ROOT_ABS)
    prefix = _UNIT_ROOT_ABS.as_posix().rstrip("/") + "/"
    expected = sorted(prefix + path for path in files)

    for shardCount in (2, 3, 4, 5):
        collected: list[str] = []
        for shardIndex in range(shardCount):
            collected.extend(listShardFiles(_UNIT_ROOT_ABS, shardCount, shardIndex))

        assert sorted(collected) == expected, f"shards={shardCount} 不是全集划分"
        assert len(collected) == len(set(collected)), f"shards={shardCount} 有重复归属"


def test_assignment_does_not_move_existing_files_when_a_file_is_added() -> None:
    """新增文件**不得**移动任何既有文件的分片归属。

    这是选「路径哈希」而非「排序后下标 mod N」的唯一理由：后者在新增一个**排序靠前**
    的文件时会让其后所有文件的分片归属整体轮转一位（integration/ 因此爆过两次）。
    """
    files = scanTestFiles(_UNIT_ROOT_ABS)
    before = assignShards(files, 4)

    # 故意取一个排序靠前的名字：下标 mod N 的实现会因此整体轮转。
    after = assignShards([*files, "aaa_brand_new_test.py"], 4)

    for path in files:
        assert after[path] == before[path], path


def test_listShardFiles_membership_is_stable_when_a_file_is_added(tmp_path: Path) -> None:
    """同一条稳定性性质，必须在 **CI 真正调用的 `listShardFiles`** 上成立。

    ⚠️ 只断言 `assignShards` 是不够的：那是单行字典推导，性质几乎白送；
    真正会被 CI 消费的是 `listShardFiles` / CLI。若有人把「下标 mod N」搬进
    `listShardFiles`，只测 `assignShards` 的守卫会**全绿**，
    而 CI 已经悄悄退回到「加一个文件就整体轮转」的不稳定分片。
    """
    for index in range(20):
        (tmp_path / f"test_file_{index}.py").write_text("", encoding="utf-8")

    before = {shard: set(listShardFiles(tmp_path, 4, shard)) for shard in range(4)}

    # 排序靠前的文件名 —— 「下标 mod N」的实现会让其后所有文件轮转一位。
    # （名字必须匹配扫描器的 `test_*.py`，否则它根本不会被扫到。）
    (tmp_path / "test_aaa_brand_new.py").write_text("", encoding="utf-8")

    after = {shard: set(listShardFiles(tmp_path, 4, shard)) for shard in range(4)}

    for shard in range(4):
        left = before[shard] - after[shard]
        assert not left, f"shard {shard} 有文件被新增文件挪走：{sorted(left)}"

    arrivals = {shard: after[shard] - before[shard] for shard in range(4)}
    newcomers = set().union(*arrivals.values())
    assert len(newcomers) == 1, f"新增文件必须只落进一个分片，实际 {newcomers}"


def test_listShardFiles_matches_per_path_hash_assignment(tmp_path: Path) -> None:
    """`listShardFiles` 的成员必须**恰好**等于「按路径哈希逐个判定」的结果。

    这条把 CI 实际消费的那一层钉在哈希语义上：任何绕过 `assignShard` 的实现
    （例如直接按下标取模）都会在这里露馅。
    """
    for name in ("x_test.py", "y_test.py", "z_test.py", "w_test.py"):
        (tmp_path / name).write_text("", encoding="utf-8")

    files = scanTestFiles(tmp_path)
    prefix = tmp_path.as_posix().rstrip("/") + "/"

    for shard in range(4):
        expected = sorted(prefix + path for path in files if assignShard(path, 4) == shard)
        assert sorted(listShardFiles(tmp_path, 4, shard)) == expected


def test_assignment_does_not_depend_on_input_order() -> None:
    """分配结果与输入顺序无关（否则又是「下标 mod N」那类实现）。"""
    files = scanTestFiles(_UNIT_ROOT_ABS)
    forward = assignShards(files, 4)
    backward = assignShards(list(reversed(files)), 4)

    assert forward == backward


def test_assignment_is_stable_across_processes() -> None:
    """跨进程、跨 `PYTHONHASHSEED` 必须完全一致。

    ⚠️ 内置 `hash()` 受 PYTHONHASHSEED 随机化影响 ⇒ 用它做哈希会让每次 CI
    分到的分片都不一样。本条用两个**显式指定不同 seed** 的子进程实测守住。

    ⚠️ 必须**显式**指定 seed：子进程默认继承父进程环境，若 CI 哪天设了
    `PYTHONHASHSEED=0`，两个子进程会拿到同一个 seed ⇒ 内置 `hash()` 的输出也相同
    ⇒ 本守卫会变成恒绿的空转。
    """
    outputs = []
    for seed in ("0", "1"):
        result = _runCli(
            "--root",
            _UNIT_ROOT_REL.as_posix(),
            "--shards",
            "4",
            "--shard",
            "2",
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)

    assert outputs[0] == outputs[1], "不同 PYTHONHASHSEED 下分片结果不同 ⇒ 用到了随机化哈希"
    assert outputs[0].strip(), "分片 2 不应为空"

    cliPaths = outputs[0].splitlines()
    expected = sorted(
        path for path in scanTestFiles(_UNIT_ROOT_ABS) if assignShard(path, 4) == 2
    )
    assert sorted(_relativeToUnitRoot(path) for path in cliPaths) == expected


def test_shards_are_reasonably_balanced() -> None:
    """单个分片不得吃掉绝大多数文件（否则分片等于没分）。

    阈值取「理想占比 + 20 个百分点」：哈希分配的理想占比是 1/shardCount，
    留足余量给「某个文件特别大」的情况，同时仍能挡住退化实现（例如全部落进 0 号分片）。
    """
    files = scanTestFiles(_UNIT_ROOT_ABS)

    for shardCount in (2, 3, 4, 5):
        sizes = [len(listShardFiles(_UNIT_ROOT_ABS, shardCount, i)) for i in range(shardCount)]
        worst = max(sizes) / len(files)
        limit = 1 / shardCount + 0.20
        assert worst <= limit, f"shards={shardCount} 最重分片占比 {worst:.2%} > {limit:.2%}"


def test_cli_rejects_out_of_range_shard() -> None:
    """越界的 --shard 必须非零退出，而不是静默跑空分片。"""
    result = _runCli(
        "--root",
        _UNIT_ROOT_REL.as_posix(),
        "--shards",
        "4",
        "--shard",
        "9",
    )

    assert result.returncode != 0
    assert result.stdout.strip() == ""
