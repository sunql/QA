"""CentOS 7 宿主勘察。

在 P1 装机之前跑，把「能不能装、装了能不能跑」的宿主侧前置条件一次问清。
设计依据 docs/superpowers/specs/2026-10-22-centos7-deployment-design.md §6 P0。

判定分两级：
- hard：不满足就必须停下来（ftype=0 会静默损坏 PG 索引；内核太老 overlay2 起不来）
- soft：可降级继续（缺 AVX2 只影响 mysql:8.0）
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import NamedTuple

MIN_KERNEL = (3, 10, 0, 514)   # overlay2 的 RH 回移点
MIN_CORES = 16                 # spec §1.1 目标环境
MIN_MEM_GB = 32.0
MIN_DISK_FREE_GB = 100.0       # 镜像 903MB 卷 + PG/Neo4j 数据 + 日志余量
BIND_MOUNT_REMOVED_NOTE = "./init bind mount 必须在 CentOS 侧删掉（docker/init 为空）"


class ProbeResult(NamedTuple):
    name: str
    ok: bool
    value: str
    severity: str


def runProbe(name: str, fn: Callable[[], str]) -> ProbeResult:
    """执行单个探测；异常不外泄，转成失败结果。"""
    try:
        return ProbeResult(name, True, fn(), "hard")
    except Exception as exc:  # noqa: BLE001 — 探测失败本身就是要报告的结论
        return ProbeResult(name, False, f"{type(exc).__name__}: {exc}", "hard")


def _kernelTuple(value: str) -> tuple[int, ...]:
    """把 '3.10.0-1160.el7.x86_64' 解析成 (3, 10, 0, 1160)。

    build 号必须带上：只切 '.' 会得到 (3,10,0)，那样 3.10.0-514 会被
    误判为低于 MIN_KERNEL=(3,10,0,514)。
    """
    version, _, build = value.strip().partition("-")
    numbers = [int(p) for p in version.split(".") if p.isdigit()]
    head = build.split(".")[0]
    numbers.append(int(head) if head.isdigit() else 0)
    return tuple(numbers)


def evaluate(name: str, value: str) -> tuple[str, str]:
    """返回 (severity, message)。message 非空即表示该探测不达标。"""
    if name == "xfs_ftype" and value.strip() != "1":
        return ("hard", "ftype=0 必须在挂载时加 ftype=1 重新格式化（xfs_admin -X upgrade / mkfs.xfs -m ftype=1）")
    if name == "kernel":
        if _kernelTuple(value) < MIN_KERNEL:
            return ("hard", f"内核 {value} 低于要求的 3.10.0-514")
        return ("hard", "")
    if name == "cpu_avx2" and value.strip() == "absent":
        return ("soft", "缺 AVX2：mysql:8.0 需降级钉 8.0.32")
    if name == "glibc" and _kernelTuple(value) < (2, 17, 0):
        return ("hard", f"glibc {value} < 2.17，el7 原生包装不上")
    if name == "selinux":
        if value.strip().lower() == "enforcing":
            return ("soft", "SELinux=enforcing：本方案用 overlay 规避 bind mount，可 setenforce 0")
        return ("hard", "")
    if name == "cpu_cores":
        if int(value) < MIN_CORES:
            return ("hard", f"{value} 核低于要求的 {MIN_CORES}")
        return ("hard", "")
    if name == "mem_total_gb":
        if float(value) < MIN_MEM_GB:
            return ("hard", f"{value} GB 内存低于要求的 {MIN_MEM_GB} GB")
        return ("hard", "")
    if name == "docker_disk_free_gb":
        if float(value) < MIN_DISK_FREE_GB:
            return ("hard", f"/var/lib/docker 仅剩 {value} GB，低于要求的 {MIN_DISK_FREE_GB} GB")
        return ("hard", "")
    return ("hard", "")


def _probeKernel() -> str:
    return platform.release()


def _probeGlibc() -> str:
    # ldd --version 首行形如 "ldd (GNU libc) 2.17" —— 取最后一个字段
    out = subprocess.run(["ldd", "--version"], capture_output=True, text=True, check=True).stdout
    fields = out.split()
    if not fields:
        raise RuntimeError("ldd --version 无输出")
    return fields[-1]


def _probeAvx2() -> str:
    with open("/proc/cpuinfo", encoding="utf-8") as fh:
        return "present" if " avx2 " in fh.read() else "absent"


def _probeCpuCores() -> str:
    return str(os.cpu_count() or 0)


def _probeMemTotalGb() -> str:
    """从 /proc/meminfo 读 MemTotal，返回 GiB 保留一位小数。"""
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return f"{kb / 1024 / 1024:.1f}"
    raise RuntimeError("/proc/meminfo 无 MemTotal")


def _probeDockerDiskFreeGb() -> str:
    target = "/var/lib/docker" if os.path.isdir("/var/lib/docker") else "/var/lib"
    return f"{shutil.disk_usage(target).free / 1024 ** 3:.1f}"


def _probeSelinux() -> str:
    if shutil.which("getenforce") is None:
        return "absent"
    return subprocess.run(["getenforce"], capture_output=True, text=True, check=True).stdout.strip()


def _probeXfsFtype() -> str:
    mount = subprocess.run(["findmnt", "-no", "FSTYPE", "/var/lib/docker"],
                           capture_output=True, text=True)
    if "xfs" not in mount.stdout:
        return "1"  # 非 XFS，无需判定
    ft = subprocess.run(["xfs_info", "/var/lib/docker"], capture_output=True, text=True)
    for line in ft.stdout.splitlines():
        if "ftype=" in line:
            return line.split("ftype=")[1].split()[0]
    raise RuntimeError("xfs_info 未返回 ftype 字段")


PROBES: tuple[tuple[str, Callable[[], str]], ...] = (
    ("kernel", _probeKernel),
    ("glibc", _probeGlibc),
    ("cpu_cores", _probeCpuCores),
    ("cpu_avx2", _probeAvx2),
    ("mem_total_gb", _probeMemTotalGb),
    ("docker_disk_free_gb", _probeDockerDiskFreeGb),
    ("selinux", _probeSelinux),
    ("xfs_ftype", _probeXfsFtype),
    ("init_bind_mount", lambda: "present — " + BIND_MOUNT_REMOVED_NOTE),
)


def main() -> int:
    results: list[ProbeResult] = []
    for name, fn in PROBES:
        result = runProbe(name, fn)
        # 探测本身抛异常时 value 是 "TypeError: ..." 文本，不是可判定的量；
        # 再喂给 evaluate 的 int()/float() 分支会抛未捕获的 ValueError。
        # 此时保持 runProbe 记的 ok=False / severity="hard" 即可。
        severity, message = evaluate(name, result.value) if result.ok else ("hard", "")
        if result.ok and message:
            result = result._replace(ok=False)
        results.append(result._replace(severity=severity))

    hardFails = [r for r in results if r.severity == "hard" and not r.ok]
    softFails = [r for r in results if r.severity == "soft" and not r.ok]

    if "--json" in sys.argv:
        print(json.dumps([r._asdict() for r in results], ensure_ascii=False, indent=2))
    else:
        for r in results:
            mark = "OK  " if r.ok else "FAIL"
            print(f"[{mark}] {r.name}: {r.value}")

    if softFails:
        print(f"\n警告：{len(softFails)} 项 soft 未满足，可降级继续", file=sys.stderr)
    if hardFails:
        print(f"\n阻断：{len(hardFails)} 项 hard 未满足，必须先修", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())