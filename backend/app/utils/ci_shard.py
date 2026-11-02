"""CI 测试分片分配：按**路径稳定哈希**分组。

为什么不沿用仓里 integration/ 的「排序后下标 mod N」：

- 下标 mod N 依赖**文件在排序列表里的位置** ⇒ 增删/重命名任何一个文件，都会让它
  之后所有文件的分片归属整体轮转一位。`Harness/rules/测试规范.md` 已记录因此
  爆过两次（Milvus 的 `qa_ci` 库只被 3 个文件懒创建、Neo4j 残留 Class 节点）。
- 路径哈希只依赖**相对扫描根的路径** ⇒ 任何增删改都**不会移动其它文件**的归属
  （注意：把 `a/x.py` 移到 `b/x.py` 确实会改变它的分片 —— 那是「文件换了位置」，
  不是「别的文件被连带移动」，后者才是要防的）。

⚠️ 必须用 `hashlib`，不能用内置 `hash()`：后者受 `PYTHONHASHSEED` 随机化影响，
同一个文件在不同进程/不同机器上会被分到不同分片，分片结果不可复现。

⚠️ 哈希输入是**相对扫描根目录**的路径（不是绝对路径、不是仓库相对路径），
这样同一份代码无论放在哪个 checkout 里，分片结果都一致。

用法（CI 里就是这么用的）：

    uv run python -m app.utils.ci_shard --root app/tests/unit --shards 4 --shard 2
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Iterable
from pathlib import Path

_HASH_ALGORITHM = "sha256"
_TEST_FILE_PATTERN = "test_*.py"


def scanTestFiles(root: str | Path) -> list[str]:
    """扫描 root 下全部测试文件，返回**相对 root** 的 POSIX 路径（已排序）。

    排序保证同一份代码在任何机器、任何文件系统枚举顺序下都得到同一份列表。
    """
    rootPath = Path(root)
    if not rootPath.is_dir():
        raise FileNotFoundError(f"分片根目录不存在：{rootPath}")

    found = [path for path in rootPath.rglob(_TEST_FILE_PATTERN) if path.is_file()]
    return sorted(path.relative_to(rootPath).as_posix() for path in found)


def assignShard(rootRelativePath: str, shardCount: int) -> int:
    """返回该文件所属的分片号（0 .. shardCount-1）。"""
    if shardCount < 1:
        raise ValueError(f"shardCount 必须 >= 1，收到 {shardCount}")

    digest = hashlib.new(_HASH_ALGORITHM, rootRelativePath.encode("utf-8")).hexdigest()
    return int(digest, 16) % shardCount


def assignShards(rootRelativePaths: Iterable[str], shardCount: int) -> dict[str, int]:
    """批量分配：路径 → 分片号。

    结果与输入顺序无关（每个路径独立计算），因此「新增一个文件」不会移动其它文件。
    """
    return {path: assignShard(path, shardCount) for path in rootRelativePaths}


def listShardFiles(root: str | Path, shardCount: int, shardIndex: int) -> list[str]:
    """列出属于该分片的测试文件，路径**相对当前工作目录**（可直接喂给 pytest）。"""
    if shardCount < 1:
        raise ValueError(f"shardCount 必须 >= 1，收到 {shardCount}")
    if not 0 <= shardIndex < shardCount:
        raise ValueError(f"shardIndex 必须在 0..{shardCount - 1}，收到 {shardIndex}")

    rootPath = Path(root)
    prefix = rootPath.as_posix().rstrip("/") + "/"
    files = scanTestFiles(rootPath)

    return [prefix + relative for relative in files if assignShard(relative, shardCount) == shardIndex]


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：打印指定分片的测试文件路径，每行一个。

    失败一律走 `parser.error`（退出码 2 + 一行人话），**不吐 traceback** ——
    CI 直接消费这个 stdout，traceback 会把日志搅浑。
    """
    parser = argparse.ArgumentParser(prog="app.utils.ci_shard", description="按稳定哈希分配 CI 测试分片")
    parser.add_argument("--root", required=True, help="测试目录，例如 app/tests/unit")
    parser.add_argument("--shards", required=True, type=int, help="分片总数")
    parser.add_argument("--shard", required=True, type=int, help="本 job 负责的分片号（0 起）")
    args = parser.parse_args(argv)

    if args.shards < 1:
        parser.error(f"--shards 必须 >= 1，收到 {args.shards}")
    if not 0 <= args.shard < args.shards:
        parser.error(f"--shard 必须在 0..{args.shards - 1}，收到 {args.shard}")

    try:
        paths = listShardFiles(args.root, args.shards, args.shard)
    except FileNotFoundError as error:
        parser.error(str(error))
    except ValueError as error:
        parser.error(str(error))

    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
