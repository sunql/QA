"""比对源端与 CentOS 侧 .env 中的复用键。

4 个值必须逐字复用（spec §7.1）。肉眼在 44 位 base64 上发现不了抄错，
本脚本比对 sha256 指纹，且**任何输出都不含明文**。

⚠️ spec §7.1 正文写「5 个」，但同段 `REUSE_KEYS` 只列 4 个 —— 个数是 spec 笔误，
以 `REUSE_KEYS` 为准（测试 `test_reuse_keys_are_exactly_the_four_from_spec` 钉死）。

用法：
    python -m scripts.verify_env_keys <源端.env> <centos.env>
退出码 0 = 全一致；1 = 有缺失或不一致。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# spec §7.1：只有 POSTGRES_PASSWORD / JWT_SECRET 可以换新
REUSE_KEYS: tuple[str, ...] = (
    "SECRET_KEY",
    "NEO4J_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
)


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
    return extractValues(Path(path).read_text(encoding="utf-8", errors="replace"))


def compare(src: dict[str, str], dst: dict[str, str]) -> list[tuple[str, str, str]]:
    """返回全部不一致项 (key, 源端指纹, 目标端指纹)。缺失也计入。"""
    mismatches: list[tuple[str, str, str]] = []
    for key in REUSE_KEYS:
        a, b = src.get(key), dst.get(key)
        if a is None or b is None or fingerprint(a) != fingerprint(b):
            mismatches.append((key, fingerprint(a or ""), fingerprint(b or "")))
    return mismatches


def render(mismatches: list[tuple[str, str, str]]) -> str:
    lines = ["复用键指纹比对结果（只显示指纹，无明文）："]
    for key, srcFp, dstFp in mismatches:
        lines.append(f"  DIFF  {key}\n          源端={srcFp}\n          目标={dstFp}")
    if not mismatches:
        lines.append("  全部一致 ✓")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法：python -m scripts.verify_env_keys <源端.env> <centos.env>", file=sys.stderr)
        return 2
    mismatches = compare(parseEnvFile(argv[1]), parseEnvFile(argv[2]))
    print(render(mismatches))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))