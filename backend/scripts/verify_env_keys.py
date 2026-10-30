"""比对源端与 CentOS 侧 .env 中的复用键。

4 个值必须逐字复用（spec §7.1）。肉眼在 44 位 base64 上发现不了抄错，
本脚本比对 sha256 指纹，且**任何输出都不含明文**（stdout / stderr / 异常消息）。

⚠️ spec §7.1 正文写「5 个」，但同段 `REUSE_KEYS` 只列 4 个 —— 个数是 spec 笔误，
以 `REUSE_KEYS` 为准（测试 `test_reuse_keys_are_exactly_the_four_from_spec` 钉死）。

用法：
    python -m scripts.verify_env_keys <源端.env> <centos.env>

退出码（运维只看这个，四条路径两两可分）：
    0 = 4 个复用键全一致
    1 = 有缺失或不一致
    2 = 参数个数不对（用法错）
    3 = .env 读不到（路径不存在 / 无权限 / 是个目录）

2 与 3 必须与 1 分开：若 `scp` 失败被读成 1，运维会去找一个根本不存在的不一致。
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

# spec §7.1：只有 POSTGRES_PASSWORD / JWT_SECRET 可以换新
REUSE_KEYS: tuple[str, ...] = (
    "SECRET_KEY",
    "NEO4J_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
)

# 缺失侧渲染成这个字样，而不是 fingerprint("")。
# 两者指纹相同（都是 sha256("")[:12]）的话，报告里会出现两行一模一样的值，
# 运维看到「源端=目标端」会以为匹配、是误报。
MISSING_MARKER = "缺失"

# 某个键是空值时给出的建议。只对 SECRET_KEY 有专门的警告 ——
# 它是 Fernet 主密钥，空了会让 5 个数据源密码 + 6 个 LLM key 永久不可解。
# 其余键给通用文案；不要在这里写死 "SECRET_KEY"，否则谈 MINIO_ROOT_USER 时
# 会印出一句跟自己那个键无关的忠告。
_EMPTY_ADVICE: dict[str, str] = {
    "SECRET_KEY": "空 SECRET_KEY 不能当 Fernet 主密钥用",
}

_GENERIC_EMPTY_ADVICE = "空值同样不可用于生产，请确认这不是漏填"

# main() 的退出码
EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_BAD_USAGE = 2
EXIT_UNREADABLE = 3


def fingerprint(value: str) -> str:
    """值的 sha256 前 12 位。仅用于比对，不泄露明文。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def extractValues(text: str) -> dict[str, str]:
    """解析 .env；剥 CR、去首尾引号。

    重复键**后出现者胜**（与 docker compose 一致）：docker/.env 里 NEO4J_PASSWORD
    出现两次，compose 用的是最后那个值，这里若取首个就会比对一个 compose 根本没在
    用的值，报出运维查不出来的「不一致」。测试 `test_extract_last_wins_on_duplicate_key` 钉死。
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def parseEnvFile(path: str | Path) -> dict[str, str]:
    """读一个 .env。读不到就抛 OSError —— 映射成退出码是 main 的职责。"""
    return extractValues(Path(path).read_text(encoding="utf-8", errors="replace"))


def compare(src: dict[str, str], dst: dict[str, str]) -> list[tuple[str, str | None, str | None]]:
    """返回全部不一致项 (key, 源端指纹, 目标端指纹)；缺失侧用 None。

    缺失也计入不一致 —— 缺失的 .env 绝不等于「键一致」。
    """
    mismatches: list[tuple[str, str | None, str | None]] = []
    for key in REUSE_KEYS:
        a, b = src.get(key), dst.get(key)
        if a is None or b is None or fingerprint(a) != fingerprint(b):
            mismatches.append((key, None if a is None else fingerprint(a),
                               None if b is None else fingerprint(b)))
    return mismatches


def findEmptyValues(src: dict[str, str], dst: dict[str, str]) -> list[str]:
    """两端都在但都是空值的复用键。

    空值 == 空值，按「同值」判一致（不因此报 DIFF），但必须告警：
    空 SECRET_KEY 不能当 Fernet 主密钥用，会让 5 个数据源密码 + 6 个 LLM key 不可解。
    """
    return [key for key in REUSE_KEYS
            if src.get(key) == "" and dst.get(key) == ""]


def emptyAdvice(key: str) -> str:
    """空值告警的后半句。谈哪个键就说哪个键的话。"""
    return _EMPTY_ADVICE.get(key, _GENERIC_EMPTY_ADVICE)


def render(mismatches: list[tuple[str, str | None, str | None]],
           emptyKeys: Sequence[str] = ()) -> str:
    lines = ["复用键指纹比对结果（只显示指纹，无明文）："]
    for key, srcFp, dstFp in mismatches:
        lines.append(
            f"  DIFF  {key}\n"
            f"          源端={MISSING_MARKER if srcFp is None else srcFp}\n"
            f"          目标={MISSING_MARKER if dstFp is None else dstFp}"
        )
    for key in emptyKeys:
        lines.append(f"  WARN  {key}：两端都是空值，按同值判一致；{emptyAdvice(key)}")
    if not mismatches:
        lines.append("  全部一致 ✓")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法：python -m scripts.verify_env_keys <源端.env> <centos.env>", file=sys.stderr)
        return EXIT_BAD_USAGE
    try:
        src = parseEnvFile(argv[1])
        dst = parseEnvFile(argv[2])
    except (OSError, ValueError) as exc:
        # 只带 errno 与路径，不含任何文件内容。退出码单列，否则「scp 失败」
        # 会被门禁读成「键不一致」，运维去查一个不存在的问题。
        # ValueError 一并接住：路径含 NUL 时 pathlib 抛的是它而不是 OSError，
        # 漏掉就等于把上面那个问题原样装回来（异常穿出去 → 退出码 1）。
        print(f"读取 .env 失败：{exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    mismatches = compare(src, dst)
    print(render(mismatches, findEmptyValues(src, dst)))
    return EXIT_MISMATCH if mismatches else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
