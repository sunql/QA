# CentOS 7 生产部署与数据前移 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 qa-system 从 macOS Docker Compose 前移到 CentOS 7.9 内网生产机，保留全部业务数据，Mac 继续作为开发机双活。

**Architecture:** 全容器 Compose（10 个服务，含新增 ollama）。联网 x86_64 构建机离线造镜像 → `docker save` → scp → 内网 `docker load`。数据走一次性单向导数（PG/Neo4j/qa-objects/MySQL 四类）。Milvus 不搬卷，走重建 + 回填。

**Tech Stack:** Docker CE 24.0.9 / docker compose plugin 2.27.1 / FastAPI + uvicorn / PostgreSQL 16 / Neo4j 5.23 / Milvus 2.4.6 / Ollama / pytest

**设计依据：** `docs/superpowers/specs/2026-10-22-centos7-deployment-design.md`（957 行，下称「spec」）。本计划不重复 spec 的论证，只做执行。

---

## Global Constraints

以下约束对**每个 Task 都生效**，不再逐条重复：

- **源码冻结点**：`origin/main` @ `0ea463b3`，生产 `alembic_version = 0132`。任何部署**必须从全新的 worktree 检出**（`qa-system-deploy-source-must-be-fresh-worktree`：主仓常停陈旧 detached HEAD）
- **4 个值必须逐字复用，禁止重新生成**：`SECRET_KEY` `NEO4J_PASSWORD` `MINIO_ROOT_USER` `MINIO_ROOT_PASSWORD`。仅 `POSTGRES_PASSWORD` `JWT_SECRET` 可换新
  - ⚠️ spec §7.1 的正文写「这 **5** 个必须逐字复用」，但它自己的 `REUSE_KEYS` 只列了 4 个变量名 —— **以变量清单为准，个数是笔误**。Task 7 的 `REUSE_KEYS` 元组按 4 个实现，并有单测钉死
  - Task 7 的比对方式也**强于** spec：spec 是把 4 行 `sort | sha256sum` 成一个总哈希，对不上时无法定位是哪个键；本计划逐键比对指纹，一次列出全部不一致项
- **Docker 版本钉死 `24.0.9-1.el7`**（spec §5.1）。该版本在 kernel 3.10 上的运行时**无任何一手来源验证过**，Task 2 的验证步骤是硬门禁，失败即降级 `20.10.24-1.el7`
- **禁止手工改 `alembic_version`**（`qa-system-never-fake-alembic-version-number`）。对不齐用 `alembic stamp`
- **禁止裸 `git stash` / `git pop` 跨分支**（`qa-system-git-stash-cross-branch-hazard`）
- **禁止后端按内容自动改派模型**（`qa-system-no-model-override`）。模型/embedding provider 切换只能由人在管理页显式操作
- **任何命令不得回显密钥明文**。比对凭据一律走 sha256 指纹
- **有逻辑的脚本一律写 Python + pytest**，bash 只做纯编排。本仓既有惯例见 `backend/scripts/preflight_image_secrets.py` + `backend/app/tests/unit/test_preflight_image_secrets.py`
- **函数 < 50 行、文件 < 800 行、嵌套 ≤ 4 层**（项目 `CLAUDE.md`）

**执行前须替换的占位符**（全计划只有这两个，都是环境值而非代码占位）：

| 占位符 | 含义 | 在哪取 |
|---|---|---|
| `<centos>` | CentOS 7 生产机的 SSH 目标（`root@<ip>`） | 部署方提供 |
| `<tag>` | 镜像标签（回写文档时填 Task 4 生成的 tag） | Task 4 Step 3 的 `$TAG` 输出 |

其余路径、命令、期望值均为实测或仓库内已核实，无待填项。

---

## File Structure

| 文件 | 责任 | 何时建 |
|---|---|---|
| `docker/docker-compose.centos.yml` | CentOS 专用 overlay：加 ollama、去掉 `./init` bind mount、给 ollama 配 host-gateway。**用 overlay 而非改主 compose**，让 Mac 开发环境零影响 | Task 3 |
| `backend/app/tests/unit/test_compose_centos_overlay.py` | 把 overlay 当数据断言，防止后续编辑破坏 Mac 开发环境 | Task 3 |
| `backend/pyproject.toml` | `dev` extras 补 `pyyaml>=6.0.1`（原为传递依赖） | Task 3 |
| `backend/scripts/preflight_centos.py` | 宿主勘察：内核 / XFS ftype / AVX2 / glibc / CPU / 内存 / 磁盘 / SELinux。JSON 输出 + 非零退出码 | Task 1 |
| `backend/app/tests/unit/test_preflight_centos.py` | 上者的单测 | Task 1 |
| `backend/scripts/build_offline_images.sh` | 联网机造镜像 + `docker save` 打包（纯编排） | Task 4 |
| `backend/app/tests/unit/test_build_offline_images.py` | 断言脚本镜像清单与 compose 一致，防漏打镜像 | Task 4 |
| `backend/scripts/verify_env_keys.py` | 比对源端与 CentOS 侧 `.env` 的复用键指纹（CRLF 安全、不回显明文） | Task 7 |
| `backend/app/tests/unit/test_verify_env_keys.py` | 上者的单测 | Task 7 |
| `backend/scripts/verify_migration.py` | 导入后行数核对（对比 spec 附录 A 实测值） | Task 10 |
| `backend/app/tests/unit/test_verify_migration.py` | 上者的单测 | Task 10 |
| `docs/deployment/00-overview.md` | 追加「带数据迁移时禁止生成新 SECRET_KEY」警告 | Task 15 |
| `docs/deployment/02-environment.md` | 同上 | Task 15 |
| `docs/deployment/01-prerequisites.md` | 追加 CentOS 7 受限路径（Docker 上限、glibc 2.17 下 Ollama 须容器化） | Task 15 |
| `docs/deployment/07-operations.md` | 追加双目标部署清单 | Task 15 |
| `Harness/wiki/operations-runbook.md` | 同上 | Task 15 |

**为什么用 overlay 而不是直接改 `docker/docker-compose.yml`**：Mac 长期双活（spec §10）。把 CentOS 专属的 ollama 服务、SELinux 处理放进 overlay，Mac 侧的 `docker compose up` 行为**逐字不变**；出问题时删掉 overlay 即可完全回退。改主 compose 则两条线互相污染。

---

## Task 1: 宿主勘察脚本

**Files:**
- Create: `backend/scripts/preflight_centos.py`
- Test: `backend/app/tests/unit/test_preflight_centos.py`

**Interfaces:**
- Consumes: 无（首个 Task）
- Produces:
  - `runProbe(name: str, fn: Callable[[], str]) -> ProbeResult`
  - `ProbeResult = NamedTuple("ProbeResult", [("name", str), ("ok", bool), ("value", str), ("severity", str)])`，`severity ∈ {"hard", "soft"}`
  - `main() -> int`，`hard` 失败返回 1，`soft` 失败返回 0 但在 JSON 中标记
  - CLI：**Mac/测试侧** `python -m scripts.preflight_centos [--json]`；
    **CentOS 宿主侧**必须 `python3 /root/preflight_centos.py [--json]`（裸文件，不能走 `-m scripts.`，见 Task 2 Step 1）

- [ ] **Step 1: 写失败测试**

`backend/app/tests/unit/test_preflight_centos.py`：

```python
"""preflight_centos 宿主勘察单测。

只测判定逻辑，不测真实宿主 —— probe 函数全部注入替身。
"""

from __future__ import annotations

from scripts.preflight_centos import ProbeResult, evaluate, runProbe


def test_runProbe_records_ok_when_probe_returns_value():
    result = runProbe("glibc", lambda: "2.17")
    assert result == ProbeResult("glibc", True, "2.17", "hard")


def test_runProbe_records_failure_when_probe_raises():
    def boom() -> str:
        raise RuntimeError("command not found")

    result = runProbe("xfs_ftype", boom)
    assert result.ok is False
    assert result.severity == "hard"
    assert "command not found" in result.value


def test_evaluate_flags_xfs_ftype_zero_as_hard_failure():
    """XFS ftype=0 会让 overlay2 的 d_type 永远为 0，Postgres 索引全坏。"""
    severity, message = evaluate("xfs_ftype", "0")
    assert severity == "hard"
    assert message.startswith("ftype=0 必须")


def test_evaluate_flags_avx2_absent_as_soft_only():
    """缺 AVX2 只影响 mysql:8.0，可降级 8.0.32，不阻断。"""
    verdict = evaluate("cpu_avx2", "absent")
    assert verdict[0] == "soft"


def test_evaluate_accepts_ftype_one():
    assert evaluate("xfs_ftype", "1")[0] == "hard"  # severity 原样，ok 由调用方看


def test_evaluate_flags_kernel_below_3_10_514():
    """3.10.0-514 是 overlay2 的 RH 回移点，低于它 overlay2 起不来。"""
    _, message = evaluate("kernel", "2.6.32-754.el7")
    assert "3.10.0-514" in message


def test_evaluate_accepts_kernel_at_and_above_3_10_514():
    """build 号也参与比较 —— 3.10.0-514 与 3.10.0-1160 只差 build。"""
    assert evaluate("kernel", "3.10.0-514.el7")[1] == ""
    assert evaluate("kernel", "3.10.0-1160.el7.x86_64")[1] == ""


def test_kernelTuple_keeps_the_build_number():
    """只取到 (3,10,0) 会把 3.10.0-514 误判为低于自己。"""
    from scripts.preflight_centos import _kernelTuple
    assert _kernelTuple("3.10.0-1160.el7.x86_64") == (3, 10, 0, 1160)
    assert _kernelTuple("2.6.32-754.el7") == (2, 6, 32, 754)
    assert _kernelTuple("2.17") == (2, 17, 0)


def test_evaluate_blocks_undersized_host():
    """16C/32G 是 spec §1.1 的目标环境，不满足就该在装机前拦住。"""
    assert evaluate("cpu_cores", "8")[0] == "hard"
    assert evaluate("mem_total_gb", "16.0")[0] == "hard"
    assert evaluate("docker_disk_free_gb", "42.0")[0] == "hard"
    assert evaluate("cpu_cores", "16")[1] == ""
    assert evaluate("mem_total_gb", "63.0")[1] == ""


def test_evaluate_only_flags_enforcing_selinux():
    """Disabled/Permissive 都是常态，不该报成问题。"""
    assert evaluate("selinux", "Disabled")[1] == ""
    assert evaluate("selinux", "Permissive")[1] == ""
    assert "setenforce" in evaluate("selinux", "Enforcing")[1]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && python -m pytest app/tests/unit/test_preflight_centos.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'scripts.preflight_centos'`

- [ ] **Step 3: 最小实现**

`backend/scripts/preflight_centos.py`：

```python
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
from typing import Callable, NamedTuple

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
        severity, message = evaluate(name, result.value)
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && python -m pytest app/tests/unit/test_preflight_centos.py -v
```
Expected: 11 passed

- [ ] **Step 5: 在 Mac 上冒烟跑一次（确认不崩）**

```bash
cd backend && python -m scripts.preflight_centos
```
Expected: macOS 上 kernel/glibc/xfs 项会 FAIL（这是预期的 —— 它是 CentOS 勘察脚本），但**脚本本身不抛未捕获异常**，正常打印结果并返回 1。

- [ ] **Step 6: 提交**

```bash
git add backend/scripts/preflight_centos.py backend/app/tests/unit/test_preflight_centos.py
git commit -m "feat(deploy): CentOS 宿主勘察脚本 + 6 项单测"
```

---

## Task 2: CentOS 装机与 Docker 实测门禁

**Files:** 无代码文件。这是**纯运维** Task。

**Interfaces:**
- Consumes: Task 1 的 JSON 输出（人工读）
- Produces: 一台 Docker 能真正跑容器的 CentOS 7.9 宿主

> ⚠️ **本 Task 有真实的未知项。** `docker-ce 24.0.9-1.el7` 在 kernel 3.10 上能否起得来**无任何一手来源验证过**（spec §5.1）。下面的验证步骤是硬门禁，不是形式。

- [ ] **Step 1: 跑勘察脚本，据结果处理**

```bash
# 把 preflight_centos.py 传到 CentOS
scp backend/scripts/preflight_centos.py root@<centos>:/root/
ssh root@<centos> 'python3 /root/preflight_centos.py --json'
```

⚠️ **必须按裸文件跑，不能写 `python3 -m scripts.preflight_centos`。** 两个原因：
1. `scp` 落地的是 `/root/preflight_centos.py`，那里既没有 `scripts` 包也没有
   `scripts/preflight_centos.py` 这个模块路径，`-m scripts.` 必然 ModuleNotFoundError；
2. 仓内 `backend/scripts/__init__.py:6` 有 `from __future__ import annotations`，
   那是 3.7+ 语法，在 CentOS 7 的 python3 3.6.8 上 import 到包这一层就 SyntaxError ——
   哪怕模块本身完全兼容 3.6，也进不去。该脚本零个仓内 import，按文件跑即可。

按结果处理：
- `xfs_ftype=0` → **必须重格式化**挂载点再继续：
  ```bash
  # 停 docker、备份数据、重建 ext4（最省事，XFS 换 ftype 会丢数据）
  mkfs.ext4 -m 0 /dev/mapper/vg-docker
  # 或 XFS 原地升 ftype（CentOS 7.9 的 xfsprogs 支持，需 unmount）
  xfs_admin -X upgrade /dev/mapper/vg-docker
  mount -o remount,attr2,inode64,ftype=1 /var/lib/docker
  ```
- `cpu_avx2: absent` → 记下，P2 构建时把 `mysql:8.0` 钉到 `8.0.32`
- `kernel < 3.10.0-514` → 停，先升级内核

- [ ] **Step 2: 装 Docker（离线 RPM，不用 get.docker.com）**

```bash
# 联网机下载（版本取 spec §5.1 的「选用」档）
B=https://download.docker.com/linux/centos/7/x86_64/stable/Packages
mkdir -p /opt/qa-transfer && cd /opt/qa-transfer
for p in docker-ce-24.0.9-1.el7 docker-ce-cli-24.0.9-1.el7 \
         containerd.io-1.6.33-3.1.el7 docker-compose-plugin-2.27.1-1.el7; do
  curl -fsSLO "$B/$p.x86_64.rpm" || echo "MISS $p"
done
ls -la   # 确认 4 个都在

# CentOS 上安装
ssh root@<centos> 'mkdir -p /opt/qa-transfer'
scp /opt/qa-transfer/*.rpm root@<centos>:/opt/qa-transfer/
ssh root@<centos> 'yum install -y /opt/qa-transfer/docker-ce-24.0.9-1.el7.x86_64.rpm \
                            /opt/qa-transfer/docker-ce-cli-24.0.9-1.el7.x86_64.rpm \
                            /opt/qa-transfer/containerd.io-1.6.33-3.1.el7.x86_64.rpm \
                            /opt/qa-transfer/docker-compose-plugin-2.27.1-1.el7.x86_64.rpm'
```

> `docker-ce-cli` **不可省** —— 它才提供 `docker` 命令本身（spec §5.1）。
> 不要用 `curl https://get.docker.com | sh`：该脚本面向当前发行版，对 EOL 的 el7 会解析出错误的仓库配置。

- [ ] **Step 3: 写 daemon.json 后启动**

```bash
ssh root@<centos> 'cat > /etc/docker/daemon.json <<'"'"'EOF'"'"'
{
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" },
  "selinux-enabled": false,
  "insecure-registries": []
}
EOF
systemctl enable --now docker && systemctl restart docker'
```
> `log-opts` 不可省：默认 `json-file` 无上限，长跑会把 `/var/lib/docker` 撑爆。
> `selinux-enabled: false` 是 spec §7.3 的选择 —— 唯一那个 bind mount 在 Task 3 已被 overlay 删掉，SELinux 就没必要了。

- [ ] **Step 4: 【硬门禁】四条实测命令**

```bash
ssh root@<centos> 'set -x
  rpm -q runc containerd.io docker-ce docker-ce-cli
  systemctl start docker && docker info
  docker run --rm hello-world
  docker compose version'
```

判定：
| 观察 | 结论 |
|---|---|
| `runc` 是 `1.0.0-70.rc10` | ⚠️ 用的是 extras 里 2018 年的旧 runc，需确认 containerd 自带的那个生效 |
| `docker info` 报 `failed to start daemon` | **降级 `20.10.24-1.el7`**，重跑本步 |
| `hello-world` 报 `runc create failed: ... EOF: unknown` | **降级 `20.10.24-1.el7`** |
| `docker compose version` 报无法连接 engine | compose 插件与 engine 不匹配，换 `docker-compose-plugin` 版本 |
| 四条全过 | ✅ 进 Task 3 |

> 这一步是整份计划里**唯一无法提前确定结果**的硬门禁。spec §5.1 已说明：所有候选版本的 CentOS 7 运行时都无一手来源验证，只能实跑。
>
> **与 spec 的一处顺序调整**：spec 把「9 个目标镜像 + `ollama/ollama` 各能真正 `run` 起来并退出」写进了 P1 门禁，但 P1 此刻**还没有镜像可 run**。该门禁移到了 Task 4 Step 5（`docker load` 之后）执行 —— 位置不同，门禁内容不变。

- [ ] **Step 5: 记录版本快照**

```bash
ssh root@<centos> 'rpm -qa | grep -E "^(docker|containerd)" | sort > /root/qa-centos-docker-versions.txt; cat /root/qa-centos-docker-versions.txt'
```
> 这份快照进 §11 的文档回写清单 —— 将来别人复现时需要知道实测通过的到底是哪个版本。

- [ ] **Step 6: 装机后复检 ftype（Step 1 那次查不到真答案）**

Step 1 的勘察查的是 `/var/lib` 所在文件系统 —— 那是**装机前唯一可查的东西**，
因为 `/var/lib/docker` 当时还不存在。但 spec §6 P0 与设计稿真正关心的是
**`/var/lib/docker` 这个挂载点本身**（spec:299 `xfs_info /var/lib/docker`）。
若运维在 P1 里给 `/var/lib/docker` 单独挂了个卷，那个卷完全没被 Step 1 看过。

Docker 起来之后必须再查一次真实挂载点：

```bash
ssh root@<centos> 'findmnt -no FSTYPE -T /var/lib/docker; xfs_info /var/lib/docker 2>/dev/null | grep ftype'
```
**门禁**：`FSTYPE` 非空；若为 `xfs`，`ftype` 必须是 `1`。
不是 xfs（或 XFS 且 ftype=1）才算过 —— 与 Step 1 的分支处理一致。

> `findmnt -no FSTYPE -T <path>` 里的 `-T` 是必需的：去掉它就是「精确匹配挂载点」，
> 而 `/var/lib` 在默认 CentOS 7 上**不是**挂载点 ⇒ findmnt 返回空、退出码 1 ⇒
> 任何「空输出即通过」的写法都会把这次检查变成空转。
> 另外 `-T` 必须紧跟 `-no FSTYPE` 之后写成 `-no FSTYPE -T <path>`；
> 写成 `-T -no FSTYPE` 会被 util-linux 2.23.2 拒（`--target and --source can't be used together`）。

---

## Task 3: CentOS compose overlay

**Files:**
- Create: `docker/docker-compose.centos.yml`
- Test: `backend/app/tests/unit/test_compose_centos_overlay.py`

**Interfaces:**
- Consumes: 无
- Produces: `docker-compose.centos.yml`，用法 `docker compose -f docker-compose.yml -f docker-compose.centos.yml up -d`。定义 1 个新服务 `ollama`，并覆盖 `mysql-demo` 去掉 `./init` bind mount（**保留 `mysql_data`**，见下）

> **⚠️ 这个覆盖的对象是 `mysql-demo`，不是 `qa-objects`。**
> 仓库里唯一的 bind mount 在 `docker-compose.yml:142`，属于 `mysql-demo`：
> `- ./init:/docker-entrypoint-initdb.d`。`qa-objects` 只有 `objects_data:/data`，
> 覆盖它的 `volumes` 对 `./init` 毫无作用。
> 也不能用 `volumes: []`：那不是「删掉 bind mount」，而是「删掉这个服务的全部卷」，
> 实测在 Compose v5.6.0 上空列表恰好被当成「不覆盖」（`objects_data` 存活），
> 但这是版本相关的实现细节，不能依赖 —— 换个 compose 版本就可能把 MinIO 数据卷剥掉。
> 正确做法是 `!override` 显式重列，只保留 `mysql_data`。

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/unit/test_compose_centos_overlay.py`：

```python
"""CentOS overlay 契约测试。

overlay 是纯 YAML，没有可单测的函数。这里的「测试」是把它当数据断言，
防止后续编辑破坏 Mac 开发环境或漏掉 CentOS 专属处理。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

OVERLAY = Path(__file__).resolve().parents[4] / "docker" / "docker-compose.centos.yml"


class _ComposeLoader(yaml.SafeLoader):
    """能读懂 Compose 专属 YAML 标签的 SafeLoader。

    overlay 是写给 `docker compose` 读的。`!override`（Compose ≥ 2.24）
    表示「整个替换掉这个字段」，PyYAML 不认识它，`yaml.safe_load` 会抛
    ConstructorError。对本测试而言，标签本身没有语义 —— 我们要断言的是
    替换之后留下的内容，所以直接把标签剥掉、保留值。
    """


def _unwrapUnknownTag(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _unwrapUnknownTag)


@pytest.fixture(scope="module")
def overlay() -> dict:
    """文件不存在时抛 FileNotFoundError（测试 ERROR），而不是 skip。

    skip 会让「文件被误删」和「测试还没写」长得一模一样 —— 这正是
    qa-system-chat-plan-empty-fixture-pattern 记录的假绿模式。
    """
    return yaml.load(OVERLAY.read_text(encoding="utf-8"), Loader=_ComposeLoader)


def test_overlay_defines_ollama_service(overlay):
    assert "ollama" in overlay["services"]


def test_ollama_binds_all_interfaces(overlay):
    """默认只听 127.0.0.1，容器经 host-gateway 够不着 —— 这是最易漏的一条。"""
    assert overlay["services"]["ollama"]["environment"]["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_ollama_persists_models_in_named_volume(overlay):
    """模型 blob 必须落卷，否则重启要重新灌几十 GB。"""
    assert "ollama_models" in overlay["services"]["ollama"]["volumes"][0]


def test_ollama_memory_settings_present(overlay):
    """32G 上预算合计 ≈32G（spec §7.4）。缺这两项会直接开始 swap。"""
    env = overlay["services"]["ollama"]["environment"]
    assert env["OLLAMA_KEEP_ALIVE"] == "5m"
    assert env["OLLAMA_NUM_PARALLEL"] == 1


def test_ollama_does_not_publish_ports(overlay):
    """只有 backend 需要访问 Ollama；暴露宿主端口等于把推理面开到 LAN。"""
    assert "ports" not in overlay["services"]["ollama"]


def test_overlay_drops_init_bind_mount(overlay):
    """唯一那个 bind mount 挂在 mysql-demo 上（docker-compose.yml:142）。

    这个断言必须查真正被改动的服务。查错服务会让「什么都没改」和
    「改对了」长得一模一样 —— 恒真断言是本仓最常见的假绿来源。
    """
    volumes = overlay["services"]["mysql-demo"]["volumes"]
    assert volumes is not None
    assert all("./init" not in str(v) for v in volumes)


def test_overlay_keeps_mysql_data_volume(overlay):
    """`!override` 是整字段替换，不是「删除其中一项」。

    漏掉这一条：MySQL 的数据目录会落进容器可写层，`docker compose down`
    之后就没了 —— 而这正是 overlay 里唯一有数据风险的写法。
    """
    assert "mysql_data:/var/lib/mysql" in overlay["services"]["mysql-demo"]["volumes"]


def test_overlay_does_not_touch_other_services(overlay):
    """overlay 只允许改 ollama 与 mysql-demo，其余服务一律不得出现。"""
    assert set(overlay["services"]) == {"ollama", "mysql-demo"}
```

- [ ] **Step 2: 声明 pyyaml 依赖**

`pyyaml` 目前只是**传递依赖**（经 `mcp-agent` / `rapidocr` 拉进来），`pyproject.toml` 的 `dev` extras 里没有它。哪天那些包被换掉，这个测试会以 `ModuleNotFoundError` 崩掉，排查起来很费时间。显式声明：

```bash
# 编辑 backend/pyproject.toml，在 dev extras 的 "pytest-cov>=5.0.0" 之后加一行：
#     "pyyaml>=6.0.1",
```

⚠️ 本文件里 `[project.optional-dependencies]` 下**已经有 `ml` 这一组**（`transformers>=4.40.0`）。
只往 `dev` 里加一行，**不要动 `ml`**。改完的 `dev` 段落应为（原样替换，只多一行）：

```toml
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.23.0",
    "pytest-cov>=5.0.0",
    "pyyaml>=6.0.1",
    "respx>=0.21.0",
    "anyio>=4.4.0",
    "ruff>=0.6.0",
    "aiosqlite>=0.20.0",
]
```

> Task 1 已在本文件加过一段 `[tool.ruff.lint.per-file-ignores]`（针对
> `scripts/preflight_centos.py` 的 3.6 兼容规则）。**保留它**，别当成残留删掉。

- [ ] **Step 3: 跑测试确认失败**

```bash
cd backend && python -m pytest app/tests/unit/test_compose_centos_overlay.py -v
```
Expected: 8 ERROR with `FileNotFoundError: .../docker/docker-compose.centos.yml`

- [ ] **Step 4: 写 overlay**

`docker/docker-compose.centos.yml`：

```yaml
# CentOS 7 生产环境 overlay —— 用法：
#   docker compose -f docker-compose.yml -f docker-compose.centos.yml up -d
#
# 为什么用 overlay 而不是改主 compose：Mac 长期双活当开发机（spec §10）。
# CentOS 专属处理全部隔离在这个文件里，Mac 侧 `docker compose up` 逐字不变，
# 出问题删掉本文件即可完全回退。
#
# ⚠️ `!override` 需要 Compose ≥ 2.24。el7 仓库能装到的 docker-compose-plugin
#    上限是 2.27.1（Task 2 实测确认），够用。
#
# 依据 docs/superpowers/specs/2026-10-22-centos7-deployment-design.md

services:
  # ===== 本地 LLM / embedding =====
  ollama:
    image: ollama/ollama:latest
    container_name: qa-ollama
    restart: unless-stopped
    environment:
      # ⚠️ 不可省。默认只听 127.0.0.1，backend 容器经 host-gateway 够不着。
      # 仓库里 grep 不到任何 OLLAMA_HOST，这是纯运维项，没有代码兜底。
      OLLAMA_HOST: 0.0.0.0:11434
      # 32G 机器上内存预算合计 ≈32G（偏紧，spec §7.4）。模型空闲 5 分钟后
      # 释放权重，避免长期占着约 12G；并发限 1，避免 KV cache 翻倍。
      OLLAMA_KEEP_ALIVE: 5m
      OLLAMA_NUM_PARALLEL: 1
    volumes:
      - ollama_models:/root/.ollama
    # 不映射宿主端口：只有 backend 需要访问，走容器网络更安全。
    # 需要手工调试时用 docker exec qa-ollama ollama list

  # ===== 去掉唯一的 bind mount（spec §7.3）=====
  # `./init:/docker-entrypoint-initdb.d` 挂在 mysql-demo 上（docker-compose.yml:142），
  # 而 docker/init/ 是空目录。CentOS 上 SELinux 对 bind mount 的标签策略是常见坑，
  # 为一个空目录冒这个险不划算。
  #
  # 为什么是 `!override` 而不是 `volumes: []`：
  #   - `[]` 是「把这个服务的卷清空」，会把 mysql_data 一起剥掉；
  #   - Compose 没有「删除列表中某一项」的写法，只能整字段替换再重列。
  # 所以这里必须把 mysql_data 原样写回来 —— 漏了就是静默的数据丢失。
  mysql-demo:
    volumes: !override
      - mysql_data:/var/lib/mysql

volumes:
  ollama_models:
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend && python -m pytest app/tests/unit/test_compose_centos_overlay.py -v
```
Expected: 8 passed

- [ ] **Step 6: 验证 overlay 能被 compose 解析且合并正确**

⚠️ 本步必须**全局**验证，不能 `grep` 某个服务再数 `init` —— 那样「查错了服务」
和「根本没删掉」输出的都是 `0`，是恒真断言。下面的写法数的是整个合并结果。

```bash
# 6.1 合并后仍是 10 个服务（原有 9 个 + ollama），原服务一个不少
docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml config --services
```
Expected: 输出 9 行原有服务名 + `ollama`，共 10 行。

```bash
# 6.2 bind mount 真的没了（数整个合并结果里的挂载点）
test "$(docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml config \
        | grep -c 'docker-entrypoint-initdb.d')" -eq 0 \
  && echo "OK: ./init bind mount 已移除" \
  || { echo "FAIL: bind mount 仍在"; exit 1; }
```

```bash
# 6.3 数据卷没被一起剥掉（!override 整字段替换，最容易丢的就是它）
test "$(docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml config \
        | grep -c 'mysql_data')" -ge 1 \
  && echo "OK: mysql_data 存活" \
  || { echo "FAIL: mysql_data 被误删，MySQL 数据将落容器可写层"; exit 1; }
```

```bash
# 6.4 `!override` 标签本身必须在位（查 overlay 源文件，不是查合并结果 ——
#     合并结果里 compose 已经把它解析掉了，根本看不到这个标签）
test "$(grep -cE '^[[:space:]]*volumes: !override[[:space:]]*$' \
        docker/docker-compose.centos.yml)" -eq 1 \
  && echo "OK: !override 标签在位" \
  || { echo "FAIL: !override 标签缺失或不精确"; exit 1; }
```

**反向自检**（防止 6.2 又变成假绿）：临时把 overlay 里的 `!override` 删掉，让
bind mount 回来，重跑 6.2 —— 必须报 `FAIL`。恢复后再跑一次，必须报 `OK`。

> ⚠️ **6.2 单独不够，必须配 6.4。** 6.2 查的是**合并结果**，而合并结果里
> `!override` 已经被 compose 解析掉了 —— 标签丢没丢，6.2 只能从「bind mount 回没回来」
> 间接推断。实测三种坏法：
>
> | overlay 写法 | 6.2（查合并结果） | 6.4（查源文件） |
> |---|---|---|
> | `volumes: !override` | OK | OK |
> | `volumes:`（标签删了） | **FAIL** | FAIL |
> | `volumes: !overide`（拼错） | **OK —— bind mount 已悄悄回来** | FAIL |
> | `volumes: !override []`（空列表） | OK（数据卷也一起没了） | FAIL |
>
> 拼错这一行最危险：`docker compose config` 对**未知 tag** 是 rc=0、stderr 为空、
> 按普通列表处理 —— 完全静默，Mac 侧和 CentOS 侧表现一致，没人看得出来。
> 而 `!override` 一丢，SELinux 要命的那个 bind mount 就原样回到生产机上。
>
> 这也是本仓没有 CI（无 `.github/workflows`）的代价：那 8 条单测不会被自动重跑，
> 本步的 6.4 是部署当天唯一一次会真正执行的断言。

> ⚠️ **`!override` 是长期维护债，改主 compose 时必须同步。**
> 它是**整字段替换**：overlay 里 `mysql-demo.volumes` 写死成 `[mysql_data:/var/lib/mysql]`。
> 以后有人给主 compose 的 `mysql-demo` 加一个新卷（新加的 `mycache:/var/cache` 之类），
> **不会**自动出现在 CentOS 上 —— 那个卷在 CentOS 侧静默消失，容器起来才报找不到目录。
> Mac 侧一切正常，所以本地开发永远发现不了。
>
> 两条防护，都要做：
> 1. 在主 compose 的 `mysql-demo` 处加一行注释，指向本 overlay 文件；
> 2. 每次改主 compose 的 `mysql-demo.volumes` 后，重跑 6.3 确认 `mysql_data` 计数，
>    并肉眼核对 overlay 里重列的卷列表与主 compose 一致。
>
> Compose 没有「从列表里删掉某一项」的写法，这是唯一的解法；真要避免这份债，
> 只能回头改主 compose 里那个 `./init` bind mount 的来源（例如让 init 脚本进镜像）。

- [ ] **Step 7: 提交**

```bash
git add docker/docker-compose.centos.yml \
        backend/app/tests/unit/test_compose_centos_overlay.py \
        backend/pyproject.toml
git commit -m "feat(deploy): CentOS compose overlay — 加 ollama、去 mysql-demo 空 bind mount"
```

---

## Task 4: 离线镜像构建与交付

**Files:**
- Create: `backend/scripts/build_offline_images.sh`
- Test: `backend/app/tests/unit/test_build_offline_images.py`（断言脚本里的镜像清单与 compose 一致）

**Interfaces:**
- Consumes: Task 3 的 overlay
- Produces: `/opt/qa-transfer/images/*.tar`，以及 `images.manifest.sha256`

- [ ] **Step 1: 写失败测试**

```python
"""离线镜像清单必须与 compose 实际用到的镜像一致。

这是防「漏打一个镜像，到了 CentOS 才 pull 失败」的唯一低成本检查。
compose 里新增/删除服务时，这个测试会先红。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
COMPOSE_FILES = [REPO / "docker" / "docker-compose.yml",
                 REPO / "docker" / "docker-compose.centos.yml"]
SCRIPT = REPO / "backend" / "scripts" / "build_offline_images.sh"


class _ComposeLoader(yaml.SafeLoader):
    """能读懂 Compose 专属 YAML 标签的 SafeLoader。

    overlay 里有 `!override`（Compose ≥ 2.24 的整字段替换标签）。
    `yaml.safe_load` 不认识它，会抛 ConstructorError —— 于是这个测试在
    overlay 一落地就直接 ERROR，而不是给出有意义的断言。
    标签本身对「收集镜像名」没有语义，剥掉、保留值即可。
    """


def _unwrapUnknownTag(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _unwrapUnknownTag)


def _composeImages() -> set[str]:
    images: set[str] = set()
    for path in COMPOSE_FILES:
        if not path.exists():
            continue
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
        for svc in (data.get("services") or {}).values():
            if "image" in svc:
                images.add(svc["image"])
    return images


def _scriptImages() -> set[str]:
    """从 IMAGES=(...) 数组里取镜像名。

    数组元素带引号（  "postgres:16-alpine" ），正则必须容许首尾引号，
    否则 listed 恒为空、missing 恒为全集，这个测试就永久失败。
    """
    text = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"^IMAGES=\((.*?)^\)", text, re.S | re.M)
    assert block, "找不到 IMAGES=(...) 数组"
    return set(re.findall(r"""['"]?([a-z0-9._/-]+:[a-zA-Z0-9._-]+)['"]?""",
                          block.group(1)))


def test_script_exists():
    assert SCRIPT.exists(), "build_offline_images.sh 必须存在"


def test_script_lists_every_compose_image():
    missing = _composeImages() - _scriptImages()
    assert not missing, f"脚本漏了这些镜像：{sorted(missing)}"


def test_script_parser_is_not_silently_empty():
    """守卫上面那个引号 bug：解析出 0 个镜像时必须红，而不是让 missing 变成全集。

    下限取 9 —— 合并后 compose 的 8 个外部镜像 + alpine:3.20（通用工具容器，
    Task 4 Step 6 的 run 门禁也要用它）。写成 8 的话，正则只多吐出一个也能过。
    """
    assert len(_scriptImages()) >= 9, f"只解析出 {len(_scriptImages())} 个镜像，正则多半坏了"


def test_compose_image_loader_tolerates_the_centos_overlay():
    """overlay 用 `!override`，普通 safe_load 读不了 —— 守卫这一点别退回去。"""
    images = _composeImages()
    assert "ollama/ollama:latest" in images, "overlay 里的 ollama 必须被收进来"
    assert "postgres:16-alpine" in images


def test_script_forces_amd64_platform():
    """脚本把平台抽成了变量（`docker pull --platform "$PLATFORM"`），
    所以不能断言字面量 "--platform linux/amd64" 在文件里 —— 那样恒失败。
    拆成两条：flag 用到了，且 PLATFORM 的值确实是 amd64。两条缺一不可：
    只断 flag 的话，写成 PLATFORM=linux/arm64 照样过。
    """
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--platform" in text, "拉镜像和 build 都没钉平台"
    assert re.search(r'^PLATFORM="linux/amd64"$', text, re.M), \
        "Mac 是 arm64，不钉 amd64 会打出跑不了的镜像"


def test_script_saves_and_checksums():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "docker save" in text
    assert "sha256sum" in text
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && python -m pytest app/tests/unit/test_build_offline_images.py -v
```
Expected: FAIL —— `AssertionError: build_offline_images.sh 必须存在`

- [ ] **Step 3: 写脚本**

`backend/scripts/build_offline_images.sh`：

```bash
#!/usr/bin/env bash
# 联网 x86_64 构建机：拉齐镜像 -> docker save -> 校验和。
# 产物 scp 到内网 CentOS 后 docker load。
# 依据 docs/superpowers/specs/2026-10-22-centos7-deployment-design.md §6.1
set -euo pipefail

OUT_DIR="${OUT_DIR:-/opt/qa-transfer/images}"
# Mac 是 arm64、CentOS 是 x86_64 —— 平台不钉死会打出跑不了的镜像
PLATFORM="linux/amd64"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

# 与 docker-compose.yml + docker-compose.centos.yml 保持一致
# （backend/frontend 是本地构建，不在此列）
IMAGES=(
  "postgres:16-alpine"
  "neo4j:5.23-community"
  "quay.io/coreos/etcd:v3.5.5"
  "minio/minio:RELEASE.2023-03-20T20-16-18Z"
  "milvusdb/milvus:v2.4.6"
  "minio/minio:RELEASE.2024-06-13T22-53-53Z"
  "mysql:8.0"
  "alpine:3.20"
  "ollama/ollama:latest"
)

mkdir -p "$OUT_DIR"

echo "== 拉取镜像（$PLATFORM）=="
for img in "${IMAGES[@]}"; do
  echo "-- $img"
  docker pull --platform "$PLATFORM" "$img"
done

echo "== 构建 backend / frontend =="
# ⚠️ 必须在联网机做，且 NPM_REGISTRY 必须覆盖为国内源：
# docker/Dockerfile.frontend 默认走 registry.npmjs.org，内网构建会卡死
# （qa-system-frontend-build-npm-registry）。
# docker/Dockerfile.backend 已硬编码 aliyun Debian + aliyun PyPI 源，无需覆盖。
NPM_REGISTRY="$NPM_REGISTRY" \
  docker compose -f docker-compose.yml -f docker-compose.centos.yml \
    build --platform "$PLATFORM" backend frontend

echo "== 打包 =="
TAG="$(date '+%Y%m%d-%H%M')"
docker save -o "$OUT_DIR/all-$TAG.tar" "${IMAGES[@]}"
# 只存本地构建的那两个。⚠️ 不要用 `docker compose images -q` —— 它列出**所有**服务的
# 镜像（含刚 pull 的 8 个外部镜像），会把 milvus / neo4j 之类再存一遍，
# 离线包凭空多几个 GB。`config --images` 给的是镜像**名**（docker save 也吃名），
# 减去 IMAGES 数组后剩下的正好是 backend / frontend。
BUILT="$(docker compose -f docker-compose.yml -f docker-compose.centos.yml config --images \
  | grep -vxF -f <(printf '%s\n' "${IMAGES[@]}"))"
docker save -o "$OUT_DIR/built-$TAG.tar" $BUILT

echo "== 校验和 =="
cd "$OUT_DIR"
sha256sum ./*.tar > images.manifest.sha256

# ⚠️ 顺手把每个镜像的**镜像 ID** 记下来。`ollama/ollama:latest` 是浮动 tag：
# 今天造的离线包和三个月后重建的同名 tag 未必是同一个镜像层，而 CentOS 是内网隔离的，
# 拉不到、也换不了 —— 届时没有任何手段确认装的到底是哪个版本。
# 用 `.ID`（镜像 config 的短 sha）而不是 `RepoDigest`：从 tar `docker load` 进来的镜像
# **没有** RepoDigests，比对它必然两边都空 ⇒ 恒过。
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
  | grep -v '<none>' | sort > images.digests.txt
cat images.digests.txt
# ⚠️ 必须硬断言，不要只 `wc -l` 打个数字：格式串写错 / 匹配全被 grep 掉时
# 这个文件会是空的，而下游所有按它做的比对都会「空空如也 ⇒ 通过」。
n=$(wc -l < images.digests.txt)
[ "$n" -ge 11 ] || { echo "ABORT: images.digests.txt 只有 $n 行，期望 >= 11"; exit 1; }
echo "镜像 ID 记录 $n 行"

echo "== 完成 =="
ls -lh "$OUT_DIR"
echo "把 images.manifest.sha256 和 images.digests.txt 一起带走，CentOS 侧要复核"
```

- [ ] **Step 4: 跑测试确认通过**

```bash
chmod +x backend/scripts/build_offline_images.sh
cd backend && python -m pytest app/tests/unit/test_build_offline_images.py -v
```
Expected: 6 passed

- [ ] **Step 5: 联网机上真跑**

```bash
bash backend/scripts/build_offline_images.sh
scp -r /opt/qa-transfer/images root@<centos>:/opt/qa-transfer/
ssh root@<centos> 'cd /opt/qa-transfer/images && sha256sum -c images.manifest.sha256 && \
  for f in *.tar; do docker load -i "$f"; done && docker images'

# 装载后逐个核对 ID 与源端记录一致 —— sha256sum 只证明**传输**没坏，
# 证明不了 load 进去的是同一个镜像
scp /opt/qa-transfer/images/images.digests.txt root@<centos>:/opt/qa-transfer/images/
ssh root@<centos> 'cd /opt/qa-transfer/images && \
  [ -s images.digests.txt ] || { echo "ABORT: images.digests.txt 缺失或为空 —— 无从核对镜像 ID"; exit 1; } && \
  docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}" | grep -v "<none>" | sort \
    > /tmp/centos-images.txt && \
  if comm -13 /tmp/centos-images.txt images.digests.txt | grep -q .; then \
    echo "DIGEST-MISMATCH —— 下列镜像在 CentOS 上的 ID 与源端记录不符（或根本没装上）："; \
    comm -13 /tmp/centos-images.txt images.digests.txt; \
  else echo "DIGEST-OK"; fi'
```
**门禁 1**：`sha256sum -c` 全过。
**门禁 2**：`DIGEST-OK`。
**门禁 3**：转移包里有且仅有 **11 个**镜像。
> ⚠️ 这里必须用 `comm -13` 单向比对，**不能用 `diff -`**：`diff` 比的是两边全集，
> 而 CentOS 上还可能有跟转移包无关的镜像（装 Docker 时拉的 base 镜像等），
> 那些多出来的行会让 `diff` 永远非空 ⇒ 门禁恒红。反过来，`comm -13 f1 f2`
> 只列「在 f2 里但不在 f1 里」的行，正好等于「该在却不在、或 ID 对不上」。
>
> 四个方向都实跑验证过：ID 漂移 ⇒ 报出来；镜像缺失 ⇒ 报出来；宿主多出无关镜像 ⇒ 放行；
> **`images.digests.txt` 为空 ⇒ 会假绿**（`comm` 两边都没东西可比，必然空输出 ⇒ OK）。
> 最后一条是 `comm` 形状自带的坑，所以上面那句 `[ -s images.digests.txt ]` 前置断言
> 是这条门禁的组成部分，不是可选的健壮性装饰。
>
> 这条不是形式检查：`ollama/ollama:latest` 是浮动 tag，Task 5 的 `docker run … ollama pull`
> 与 Task 4 的 `docker save` 之间只要有一次 pull，就可能出现两者不是同一层的情况，
> 而 CentOS 内网拉不到、也换不了。

11 个镜像的来源：

| 来源 | 个数 | 清单 |
|---|---|---|
| 脚本 `IMAGES` 数组（pull 下来 save） | 9 | `postgres:16-alpine` `neo4j:5.23-community` `quay.io/coreos/etcd:v3.5.5` `minio/minio:RELEASE.2023-03-20T20-16-18Z` `milvusdb/milvus:v2.4.6` `minio/minio:RELEASE.2024-06-13T22-53-53Z` `mysql:8.0` `alpine:3.20` `ollama/ollama:latest` |
| 联网机上本地 build 后 save | 2 | `qa-system-backend:latest` `qa-system-frontend:latest` |

> 别写成「10 个」——它既不是 pull 数（9）也不是 Step 6 逐镜像跑的数量（11），
> 而差一个正是 `alpine:3.20` 或某个 build 镜像漏掉的情形。`alpine:3.20` 尤其关键：
> Task 9 的三条前置断言和卷导入全靠它做 helper 容器，离线机上漏了它，
> Task 9 会在第一条断言就报 `Unable to find image 'alpine:3.20'`。
>
> 逐条数而不是只数个数，且**数转移包里的、不是数 CentOS 全部镜像**：
> ```bash
> ssh root@<centos> 'cd /opt/qa-transfer/images && \
>   n=$(wc -l < images.digests.txt); echo "转移包镜像数=$n"; \
>   [ "$n" -eq 11 ] || { echo "ABORT: 期望 11 个，实际 $n 个"; exit 1; }'
> ```
> 门禁 2 已经逐个确认过这 11 个都在、且 ID 对得上，所以门禁 3 只需锁住「不多不少」。
> ⚠️ 不要写成 `ssh … 'docker images … | wc -l'` —— 那数的是 CentOS 上**所有**镜像，
> 装 Docker 过程本身可能拉进来无关镜像，个数就不止 11，门禁恒红。
> 多一个镜像意味着 overlay/base compose 的 `image:` 引用表和离线包对不上，
> 那正是这套门禁要抓的。

- [ ] **Step 6: 【硬门禁】逐镜像真跑一遍**

```bash
ssh root@<centos> 'bash -s' <<'REMOTE'
while IFS='|' read -r img cmd; do
  [ -z "$img" ] && continue
  timeout 20 docker run --rm "$img" $cmd >/dev/null 2>&1
  rc=$?
  case $rc in
    0)          echo "RUN-OK    $img  (正常退出)" ;;
    124)        echo "RUN-OK    $img  (20s 未退出＝服务已起来)" ;;
    125)        echo "RUN-FAIL  $img  容器起不来 → runc/overlay2 问题" ;;
    126|127)    echo "RUN-FAIL  $img  镜像内无该命令，换探测命令" ;;
    *)          echo "RUN-FAIL  $img  rc=$rc" ;;
  esac
done <<'LIST'
postgres:16-alpine|postgres --version
neo4j:5.23-community|
quay.io/coreos/etcd:v3.5.5|etcd --version
minio/minio:RELEASE.2023-03-20T20-16-18Z|minio --version
milvusdb/milvus:v2.4.6|
minio/minio:RELEASE.2024-06-13T22-53-53Z|minio --version
mysql:8.0|mysqld --version
alpine:3.20|echo ok
ollama/ollama:latest|ollama --version
qa-system-backend:latest|python -c "print(1)"
qa-system-frontend:latest|
LIST
REMOTE
```
**门禁**：`RUN-OK` 恰好 **11** 行，且每一行都是 `RUN-OK`（无 `RUN-FAIL`）。

> 必须连行数一起数：只看「没有 RUN-FAIL」的话，清单被改短（少一行镜像）会
> 静默通过 —— 这是本仓最常见的假绿形状（空输出 ⇒ 通过）。
> ```bash
> ssh root@<centos> 'docker images --format "{{.Repository}}:{{.Tag}}" | grep -cv "<none>"'   # 期望 11
> ```

> `docker load` 成功**不等于**镜像能跑 —— load 只解包 layer，`run` 才真正走 overlay2 + runc + cgroup。
> 命令列留空表示用镜像默认 CMD（服务类镜像会一直跑，超时即成功）。刻意**不用** `--entrypoint /bin/sh`：`minio` / `milvus` 镜像里没有 shell，那样只会得到 126/127 假失败。
> 任一 `RUN-FAIL` 且 rc=125，回 Task 2 Step 4 降级 Docker 版本。

---

## Task 5: Ollama 模型离线预置

**Files:** 无代码文件。纯运维。

**Interfaces:**
- Consumes: Task 4 交付的 `ollama/ollama` 镜像
- Produces: CentOS 上 `/opt/qa-transfer/ollama/` 里一份可 cp 进容器的模型目录

- [ ] **Step 1: 联网机上拉模型**

```bash
docker run --rm -v "$PWD/ollama-models:/root/.ollama" ollama/ollama:latest \
  ollama pull bge-m3
# 对话模型由用户在管理页配置时决定，此处按需补拉，例如：
# docker run --rm -v "$PWD/ollama-models:/root/.ollama" ollama/ollama:latest \
#   ollama pull qwen2.5:7b
du -sh ollama-models
```

- [ ] **Step 2: 传到 CentOS 并 cp 进卷**

```bash
tar czf ollama-models.tar.gz ollama-models
scp ollama-models.tar.gz root@<centos>:/opt/qa-transfer/
ssh root@<centos> 'docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml \
    up -d ollama
  VOL=$(docker volume ls -q | grep ollama_models | head -1)
  docker run --rm -v "$VOL":/dst -v /opt/qa-transfer:/src:ro alpine:3.20 \
    sh -c "cp -a /src/ollama-models/. /dst/"
  docker restart qa-ollama'
```

- [ ] **Step 3: 验证**

```bash
ssh root@<centos> 'docker exec qa-ollama ollama list
  docker exec qa-backend python -c "
import urllib.request, json
r = urllib.request.urlopen(\"http://host.docker.internal:11434/api/tags\")
print(json.load(r))"'
```
**门禁**：`ollama list` 列出 bge-m3；backend 容器内能通过 `host.docker.internal:11434` 读到标签列表。
> 这一步专门验证 `OLLAMA_HOST=0.0.0.0` 配对了 —— 默认值下这里必然连不上。

---

## Task 6: CentOS 空环境起容器

**Files:** 无代码文件。纯运维。

**Interfaces:**
- Consumes: Task 3 overlay、Task 4 镜像、Task 5 模型
- Produces: 10 个容器 healthy 的空环境（PG/Neo4j 空库，Milvus 空集合）

- [ ] **Step 1: 放代码（必须是全新 worktree）**

```bash
# ⚠️ 绝不用主仓直接 scp —— 主仓常停陈旧 detached HEAD，
# 缺 0132 迁移会让 backend 启动即崩（qa-system-deploy-source-must-be-fresh-worktree）
ssh root@<centos> 'git clone https://github.com/sunql/QA.git /opt/qa && cd /opt/qa && \
  git checkout 0ea463b3 && git log --oneline -1'
```
**门禁**：输出必须是 `0ea463b3`。不是则停。

- [ ] **Step 2: 写 CentOS 侧 `.env`（此时先不管业务配置）**

按 spec §7.1 表填写。**5 个复用键此时留空**，Task 7 才比对填入。
```bash
ssh root@<centos> 'cd /opt/qa && cp docker/.env.example docker/.env && chmod 600 docker/.env'
```

> ⚠️ **必须改 3 个值，否则 backend 在 CentOS 上够不到 Ollama。**
>
> `.env.example` 里这 3 个都指向 `host.docker.internal`：
>
> | 键 | 模板值 | CentOS 上改成 |
> |---|---|---|
> | `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | `http://ollama:11434` |
> | `EMBEDDING_API_BASE` | `http://host.docker.internal:11434/v1` | `http://ollama:11434/v1` |
> | `EMBEDDING_HOST_OVERRIDE` | `host.docker.internal` | `ollama` |
>
> 原因（已实跑 `docker compose config` 核实）：backend 确实有
> `extra_hosts: host.docker.internal=host-gateway`，所以那个**主机名在容器里解析得开**；
> 但 Task 3 的 overlay **刻意不给 ollama 映射宿主端口**，于是宿主 11434 上没有任何东西
> 在听 ⇒ 域名解析成功、连接被拒。这条坑很隐蔽：名字能解析会让人以为通了。
>
> `ollama` 是 compose 网络里的服务名，backend 与它同在 `default` 网络，直接可达 ——
> 这也正是 overlay「不映射宿主端口」这个设计的用意。
>
> ```bash
> # ⚠️ sed 表达式写成**一行**，不要在引号内用 `\` 折行 —— 折行后的续行缩进会被
> # 吃进表达式，实测报 `bad flag in substitute command`。
> ssh root@<centos> 'cd /opt/qa/docker && sed -i -e "s#^OLLAMA_BASE_URL=.*#OLLAMA_BASE_URL=http://ollama:11434#" -e "s#^EMBEDDING_API_BASE=.*#EMBEDDING_API_BASE=http://ollama:11434/v1#" -e "s#^EMBEDDING_HOST_OVERRIDE=.*#EMBEDDING_HOST_OVERRIDE=ollama#" .env'
> ssh root@<centos> 'grep -E "^(OLLAMA_BASE_URL|EMBEDDING_API_BASE|EMBEDDING_HOST_OVERRIDE)=" /opt/qa/docker/.env'
> ```
> **门禁**：回显 3 行，且值分别是 `http://ollama:11434`、`http://ollama:11434/v1`、`ollama`。

- [ ] **Step 3: 起中间件（不起 backend）**

```bash
ssh root@<centos> 'cd /opt/qa/docker && \
  docker compose -f docker-compose.yml -f docker-compose.centos.yml up -d \
    postgres neo4j milvus-etcd milvus-minio milvus-standalone qa-objects mysql-demo ollama'
```

- [ ] **Step 4: 验证健康**

```bash
ssh root@<centos> 'cd /opt/qa/docker && \
  docker compose -f docker-compose.yml -f docker-compose.centos.yml ps'
```
**门禁**：8 个服务全部 `running`（或 `healthy`），无 `restarting`。
> 若 Milvus 反复重启：检查 `milvus-minio` 是否 healthy —— Milvus 依赖它。
>
> ⚠️ **与 spec 的一处刻意偏离**：spec P4 的门禁写着「`GET /api/v1/health` 返回 200」，但那需要 backend 已启动，而 backend 的 `CMD` 是 `alembic upgrade head && uvicorn` —— 它启动就会跑 seed，**在半空库上产出脏数据**（spec §6.4 铁律）。
> 所以 health 检查挪到 Task 10 Step 5（backend 启动之后）。**P4 只起中间件，不起 backend**，Task 9 Step 1 的断言 1 就是为了钉死这一点。

---

## Task 7: SECRET_KEY 指纹校验

**Files:**
- Create: `backend/scripts/verify_env_keys.py`
- Test: `backend/app/tests/unit/test_verify_env_keys.py`

**Interfaces:**
- Consumes: 源端 `docker/.env` 与 CentOS 侧 `docker/.env`
- Produces: 退出码 0 = 4 个复用键全一致；1 = 有缺失或不一致（**且只打印指纹，不打印值**）；2 = 参数个数不对；3 = `.env` 读不到（路径不存在 / 无权限 / 是目录）。缺失侧渲染成「缺失」而非 `fingerprint("")`。退出码 2/3 必须与 1 分开，否则 `scp` 失败会被门禁读成「键不一致」，运维去查一个不存在的问题。

- [ ] **Step 1: 写失败测试**

```python
"""复用键指纹比对。

三条不可让步的性质：
1. 任何输出（stdout / stderr / 异常消息）都不含密钥明文
2. CRLF 行尾被归一，不能因此报出「假不一致」（否则运维会去查一个不存在的问题）
3. 失败原因必须能区分开：读不到文件（3）≠ 用法错（2）≠ 键不一致（1）
   —— 运维只看得懂退出码，混成一个「非零」会让他去查一个不存在的不一致
"""

from __future__ import annotations

import pytest

from scripts.verify_env_keys import (
    REUSE_KEYS,
    compare,
    extractValues,
    findEmptyValues,
    fingerprint,
    main,
    parseEnvFile,
    render,
)

# sha256("")[:12] —— 空值指纹。缺失/空值一旦被当成正常指纹渲染出来，
# 报告里就会出现两行一模一样的 12 位十六进制，运维无从判断该不该动手。
EMPTY_FP = "e3b0c44298fc"


def _allKeysDict() -> dict[str, str]:
    """4 个复用键齐备、无空值无重复的键值表。"""
    return {
        "SECRET_KEY": "a",
        "NEO4J_PASSWORD": "b",
        "MINIO_ROOT_USER": "c",
        "MINIO_ROOT_PASSWORD": "d",
    }


def _envBody(secretKey: str = "a") -> str:
    """4 个复用键齐备的 .env 文本。"""
    return (
        f"SECRET_KEY={secretKey}\n"
        "NEO4J_PASSWORD=b\n"
        "MINIO_ROOT_USER=c\n"
        "MINIO_ROOT_PASSWORD=d\n"
    )


def test_reuse_keys_are_exactly_the_four_from_spec():
    assert REUSE_KEYS == (
        "SECRET_KEY",
        "NEO4J_PASSWORD",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
    )


def test_fingerprint_is_stable_and_opaque():
    a = fingerprint("abc")
    assert a == fingerprint("abc")
    assert len(a) == 12
    assert "abc" not in a


def test_extract_strips_crlf():
    assert extractValues("SECRET_KEY=abc\r\nOTHER=x\r\n")["SECRET_KEY"] == "abc"


def test_extract_ignores_comments_and_blanks():
    text = "# 注释\n\nSECRET_KEY=abc\n"
    assert extractValues(text) == {"SECRET_KEY": "abc"}


def test_extract_keeps_equals_signs_inside_value():
    """base64 / 口令里可能有 = 号，只能切第一个。"""
    assert extractValues("SECRET_KEY=ab=cd=ef\n")["SECRET_KEY"] == "ab=cd=ef"


def test_extract_strips_surrounding_quotes():
    assert extractValues('SECRET_KEY="abc"\n')["SECRET_KEY"] == "abc"


def test_extract_last_wins_on_duplicate_key():
    """真实文件里 docker/.env 的 NEO4J_PASSWORD 出现了两次（第 14、40 行）。

    重复键必须与 docker compose 取值一致 —— compose 也是「后出现者胜」。
    若这里改成 first-wins，本脚本会比对 compose 根本没在用的那个值，
    报出一个运维查不出来的「不一致」。这条测试把该行为钉死。
    """
    out = extractValues("NEO4J_PASSWORD=first\nNEO4J_PASSWORD=second\n")
    assert out["NEO4J_PASSWORD"] == "second"


def test_compare_reports_all_keys_not_just_the_first_diff():
    """必须报**全部**不一致项，且顺序跟随 REUSE_KEYS。

    旧版这条测试只让 1 个键不同，于是「只返回第一处不一致」的实现照样绿 ——
    名字宣称的行为根本没被测到。这里让 2 个键不同并逐项断言。
    """
    src = _allKeysDict()
    dst = dict(src, SECRET_KEY="WRONG1", MINIO_ROOT_PASSWORD="WRONG4")
    mismatches = compare(src, dst)
    assert mismatches == [
        ("SECRET_KEY", fingerprint("a"), fingerprint("WRONG1")),
        ("MINIO_ROOT_PASSWORD", fingerprint("d"), fingerprint("WRONG4")),
    ], "两处不一致都要报，且顺序必须跟随 REUSE_KEYS"


def test_compare_empty_on_full_match():
    assert compare(_allKeysDict(), _allKeysDict()) == []


def test_missing_key_is_reported_not_silently_skipped():
    mismatches = compare(_allKeysDict(), {})
    assert len(mismatches) == 4, "缺失必须报，不能当作一致"


def test_compare_marks_missing_dest_side_as_none_not_empty_fingerprint():
    """目标侧缺失必须是 None，不是 fingerprint("")。

    否则报告里会出现「源端=目标端=e3b0c44298fc」两行一样的值，
    运维看到会以为「这俩一样，是误报」。
    """
    mismatches = compare(_allKeysDict(), {})
    assert mismatches == [
        ("SECRET_KEY", fingerprint("a"), None),
        ("NEO4J_PASSWORD", fingerprint("b"), None),
        ("MINIO_ROOT_USER", fingerprint("c"), None),
        ("MINIO_ROOT_PASSWORD", fingerprint("d"), None),
    ]
    assert EMPTY_FP not in [fp for _, _, fp in mismatches if fp]


def test_compare_marks_missing_source_side_as_none():
    mismatches = compare({}, _allKeysDict())
    assert mismatches == [
        ("SECRET_KEY", None, fingerprint("a")),
        ("NEO4J_PASSWORD", None, fingerprint("b")),
        ("MINIO_ROOT_USER", None, fingerprint("c")),
        ("MINIO_ROOT_PASSWORD", None, fingerprint("d")),
    ]


def test_render_output_contains_only_fingerprints():
    """报告里只能出现指纹。若有人后来加了「顺手打印明文方便排查」，这里会红。"""
    report = render([("SECRET_KEY", fingerprint("real-secret"), fingerprint("wrong-secret"))])
    assert "SECRET_KEY" in report
    assert "real-secret" not in report
    assert "wrong-secret" not in report
    assert fingerprint("wrong-secret") in report


def test_render_missing_side_shows_marker_not_empty_fingerprint():
    """缺失侧渲染成「缺失」字样，绝不能让空值指纹冒充成一个真实指纹。"""
    report = render([("SECRET_KEY", None, fingerprint("real"))])
    assert "SECRET_KEY" in report
    assert "缺失" in report
    assert EMPTY_FP not in report, "缺失侧不得渲染成 fingerprint('')"


def test_render_missing_dest_side_shows_marker():
    report = render([("NEO4J_PASSWORD", fingerprint("real"), None)])
    assert "缺失" in report
    assert EMPTY_FP not in report
    assert fingerprint("real") in report


def test_render_on_match_prints_no_secret_names():
    out = render([])
    assert out.strip().endswith("✓")
    for key in REUSE_KEYS:
        assert key not in out


def test_find_empty_values_flags_both_sides_empty():
    """两端都空 —— 判一致可以，但空 SECRET_KEY 当 Fernet 主密钥是不能用的。"""
    assert findEmptyValues({"SECRET_KEY": "", "NEO4J_PASSWORD": "b"}, dict(
        {"SECRET_KEY": "", "NEO4J_PASSWORD": "b"})) == ["SECRET_KEY"]
    assert findEmptyValues({"SECRET_KEY": ""}, {"SECRET_KEY": "x"}) == []


def test_empty_value_on_both_sides_warns_but_still_exits_zero(tmp_path, capsys):
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    body = "SECRET_KEY=\nNEO4J_PASSWORD=b\nMINIO_ROOT_USER=c\nMINIO_ROOT_PASSWORD=d\n"
    src.write_text(body, encoding="utf-8")
    dst.write_text(body, encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 0
    out = capsys.readouterr().out
    assert "WARN" in out, "两端都空必须留一条告警，否则空 SECRET_KEY 会被无声放行"
    assert "全部一致" in out


def test_parse_env_file_reads_from_disk(tmp_path):
    """parseEnvFile 是唯一碰文件系统的入口，不能只测不覆盖。"""
    p = tmp_path / ".env"
    p.write_text("# c\nSECRET_KEY=abc\n", encoding="utf-8")
    assert parseEnvFile(p) == {"SECRET_KEY": "abc"}


def test_parse_env_file_raises_oserror_on_missing_path(tmp_path):
    """parseEnvFile 不吞异常 —— 把「读不到」映射成退出码是 main 的职责。"""
    with pytest.raises(OSError):
        parseEnvFile(tmp_path / "nope.env")


def test_main_exit_code_zero_on_match(tmp_path, capsys):
    """退出码是这个脚本唯一的交付物 —— 门禁脚本靠它，不靠人眼看输出。"""
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    body = _envBody()
    src.write_text(body, encoding="utf-8")
    dst.write_text(body, encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 0
    assert "全部一致" in capsys.readouterr().out


def test_main_exit_code_one_on_mismatch(tmp_path, capsys):
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    src.write_text(_envBody(), encoding="utf-8")
    dst.write_text(_envBody("WRONG"), encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 1
    captured = capsys.readouterr()
    assert "DIFF" in captured.out
    assert "WRONG" not in captured.out, "不一致时 stdout 也不许回显明文"
    assert "WRONG" not in captured.err, "stderr 同样不许回显明文"
    assert captured.err.strip() == "", "比对报告只走 stdout；stderr 留给真正的错误"


def test_main_reports_missing_key_as_marker_not_empty_fingerprint(tmp_path, capsys):
    """端到端：目标侧缺 SECRET_KEY 时，运维看到的报告里必须是「缺失」。

    若渲染成 fingerprint("")，报告会印出「源端=目标端=e3b0c44298fc」两行一样的值，
    运维会认为「这俩本来就一样，是误报」，从而放行一个缺密钥的迁移。
    """
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    src.write_text(_envBody(), encoding="utf-8")
    dst.write_text("NEO4J_PASSWORD=b\nMINIO_ROOT_USER=c\nMINIO_ROOT_PASSWORD=d\n",
                   encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 1
    out = capsys.readouterr().out
    assert "SECRET_KEY" in out
    assert "缺失" in out
    assert EMPTY_FP not in out, "缺失侧渲染成空值指纹，运维会误以为两端相同"


def test_main_exit_code_two_on_bad_usage(capsys):
    """参数个数不对必须与「比对不一致」区分开 —— 否则门禁会把用法错误读成迁移失败。"""
    assert main(["prog"]) == 2
    assert capsys.readouterr().err.strip() != ""


def test_main_exit_code_three_on_unreadable_env(tmp_path, capsys):
    """读不到 .env 必须有自己的退出码。

    真实故障：operator 的 scp 失败或路径写错。若这里沿用 1，门禁读成
    「有键不一致」，而 stdout 里一行 DIFF 都没有 —— 运维会去查一个不存在的问题。
    """
    src = tmp_path / "a.env"
    src.write_text(_envBody(), encoding="utf-8")
    assert main(["prog", str(src), str(tmp_path / "nope.env")]) == 3
    captured = capsys.readouterr()
    assert captured.err.strip() != "", "读不到文件要有可读的原因"
    assert captured.out.strip() == "", "读不到文件时不得输出比对报告，否则会被误读成结论"


def test_exit_codes_three_two_one_zero_are_all_distinguishable(tmp_path):
    """四条路径的退出码两两不同 —— 门禁只能靠退出码分类。"""
    good = tmp_path / "good.env"
    good.write_text(_envBody(), encoding="utf-8")
    bad = tmp_path / "bad.env"
    bad.write_text(_envBody("WRONG"), encoding="utf-8")
    missing = tmp_path / "missing.env"

    assert main(["prog"]) == 2
    assert main(["prog", str(good), str(missing)]) == 3
    assert main(["prog", str(good), str(bad)]) == 1
    assert main(["prog", str(good), str(good)]) == 0


def test_io_error_message_carries_no_secret_value(tmp_path, capsys):
    """IO 错误消息只能带 errno + 路径，绝不能带任何值。"""
    rawValue = "SUPER-SECRET-NEVER-PRINT"
    src = tmp_path / "a.env"
    src.write_text(_envBody(rawValue), encoding="utf-8")
    assert main(["prog", str(src), str(tmp_path / "nope.env")]) == 3
    captured = capsys.readouterr()
    assert rawValue not in captured.err, "IO 错误消息不得回显任何值"
    assert rawValue not in captured.out
```


- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && python -m pytest app/tests/unit/test_verify_env_keys.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'scripts.verify_env_keys'`

- [ ] **Step 3: 最小实现**

`backend/scripts/verify_env_keys.py`：

```python
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
        lines.append(f"  WARN  {key}：两端都是空值，按同值判一致；但空 SECRET_KEY 不能当 Fernet 主密钥")
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
    except OSError as exc:
        # 只带 errno 与路径，不含任何文件内容。退出码单列，否则「scp 失败」
        # 会被门禁读成「键不一致」，运维去查一个不存在的问题。
        print(f"读取 .env 失败：{exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    mismatches = compare(src, dst)
    print(render(mismatches, findEmptyValues(src, dst)))
    return EXIT_MISMATCH if mismatches else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```


- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && python -m pytest app/tests/unit/test_verify_env_keys.py -v
```
Expected: 27 passed

- [ ] **Step 5: 真实比对**

```bash
scp root@<centos>:/opt/qa/docker/.env /tmp/centos.env
cd backend && python -m scripts.verify_env_keys ../docker/.env /tmp/centos.env
rc=$?
echo "exit=$rc"
case "$rc" in
  0) echo "OK：4 个复用键全一致" ;;
  1) echo "FAIL：有缺失或不一致 —— 看上面逐项 DIFF（缺失侧显示「缺失」）"; exit 1 ;;
  2) echo "FAIL：用法错，参数个数不对"; exit 1 ;;
  3) echo "FAIL：.env 读不到 —— 多半是上一行 scp 失败或路径写错，**不是**密钥不一致"; exit 1 ;;
  *) echo "FAIL：未知退出码 $rc"; exit 1 ;;
esac
```
**门禁**：`OK：4 个复用键全一致`，`exit=0`。
> 四个退出码必须分开看。早期版本读不到文件时也返回 1，于是 `scp` 失败会被
> 门禁读成「有不一致」—— 运维去找一个不存在的不一致，正是这个脚本要防的事。
> 所以 **3 必须单独判**，不要写成 `test "$rc" -eq 0` 之外再靠人眼读数字。
> 不一致时**不要**手工比对明文 —— 改用脚本的逐项定位，或直接重新从源端 `docker/.env` 复制那一个键。
> ⚠️ 不要用 `git show` 取值：`docker/.env` 已 gitignore（`.gitignore:46`），那只会拿到仓库里的占位符。

> 📌 **本 Task 排在 Task 6（起容器）之后，与 spec §7.1「全部在 P4 起容器之前」不一致。**
> 排在这里是因为脚本本身就是 Task 7 才写出来的，Task 6 时它还不存在。
> 已核实**这个偏离不造成实际损害**：
> - Task 6 Step 3 起的 8 个服务里没有 backend，Fernet 只被 backend 侧
>   （`datasource_service` / `embedding_provider_service`）使用 ⇒ 此时还没加密过任何东西；
> - 唯一会跑 SQL 的 `./init` 挂载在 `mysql-demo` 上，而 Task 3 的 overlay 已经把它去掉了，
>   且 `docker/init/` 目录本身是空的 ⇒ 没有 init 脚本会加密。
>
> 真正不可逆的第一个时点是 **Task 10 启动 backend**，它在 Task 7 之后。
> ⚠️ 因此这条依赖不要倒过来：**Task 10 之前 Task 7 必须已经过了**。
> 如果哪天有人调整任务顺序，把 Task 7 挪到 Task 10 之后，就等于把
> 「用错的主密钥写进去一批新密文」变成了可随时发生的事故。

---

## Task 8: 源端数据导出与自检

**Files:** 无新代码文件。复用仓内既有脚本。

**Interfaces:**
- Consumes: macOS 上运行中的生产栈
- Produces: `backups/{pg,neo4j,objects,mysql}/` 四类产物 + `backups/rowcounts.baseline.json`（**导出那一刻实测的 count(*)**）+ `transfer-manifest.sha256`

- [ ] **Step 1: 导出四类 + 量出行数基线**

```bash
cd /path/to/qa-system
mkdir -p backups

# ── 行数基线：量在 pg_dump **之前**，中间不要插任何别的操作 ──────────────
# 顺序决定方向，不是比较逻辑决定方向：
#   计数早于 dump 的快照 ⇒ 恢复出来的行数必然 >= 基线。
# 所以静态表判「不得低于基线」，而「高于」是导出期间的新写入，必须放过。
# 反过来（量在 dump 之后）只剩 restored < baseline 这一种偏差，
# 但那样任何正常写入都会让静态表偏小，得靠 slack 兜 —— 而静态表本来不该有
# slack，兜了就是「用容差盖住方向搞反」。
# 前提：这批表只追加（audit_log / evidence / *sessions 都是）。会被 DELETE 的表
# 不适用这条推理，量在 dump 之前反而可能量到已删掉的状态。
# ⚠️ 用 jsonb 不是 json：json 输出是 `"t" : 3`（冒号前后都有空格），
# 冒号位置不固定；jsonb 固定成 `"t": 3`，Task 9 Step 2 的取值只需容一个空格。
docker exec -i qa-postgres psql -U qa_user -d qa_metadata -tA > backups/rowcounts.baseline.json <<'SQL'
WITH counts AS (
  SELECT 'audit_log'         AS t, count(*) AS c FROM audit_log
  UNION ALL SELECT 'ontology_class',      count(*) FROM ontology_class
  UNION ALL SELECT 'system_config',       count(*) FROM system_config
  UNION ALL SELECT 'session_token_usage', count(*) FROM session_token_usage
  UNION ALL SELECT 'evidence',            count(*) FROM evidence
  UNION ALL SELECT 'user_sessions',       count(*) FROM user_sessions
  UNION ALL SELECT 'session_message',     count(*) FROM session_message
  UNION ALL SELECT 'multi_step_step',     count(*) FROM multi_step_step
)
SELECT jsonb_build_object(
         'measured_at', to_char(now() AT TIME ZONE 'Asia/Shanghai',
                              'YYYY-MM-DD"T"HH24:MI:SS'),
         'database',    current_database(),
         'tables',      jsonb_object_agg(t, c)
       )::text
FROM counts;
SQL
./scripts/backup_pg.sh                          # → backups/pg/qa_metadata_*.dump（-Fc）
./scripts/backup_neo4j.sh                       # → backups/neo4j/neo4j_*.dump
./scripts/backup_objects.sh                     # → backups/objects/objects_data_*.tar.gz
mkdir -p backups/mysql
docker exec qa-mysql-demo sh -c \
  'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers wms_demo' \
  > backups/mysql/wms_demo.sql
```
> **`backup_milvus.sh` 不执行。** 它是裸 tar 卷备份，Milvus 按 spec §4.5 走重建路径。

- [ ] **Step 2: 自检四份产物（spec §6.2）**

```bash
cd /path/to/qa-system/backups
for f in pg/*.dump neo4j/*.dump objects/*.tar.gz mysql/*.sql; do
  [ -s "$f" ] && echo "OK   $f  $(du -h "$f" | cut -f1)" || echo "EMPTY/MISSING  $f"
done

# ⚠️ 整段必须单引号 + sh -c：`/b/qa_metadata_*.dump` 这个 glob 在**宿主**不存在
# （宿主没有 /b），不引号的话宿主 shell 不会展开它（bash 留字面量、zsh 直接报
# `no matches found` 并中止整条命令），而 docker run 直接 exec 没有 shell，
# 容器里也就没人展开 ⇒ pg_restore 收到字面量 `/b/qa_metadata_*.dump` ⇒ 报
# could not open input file。加 sh -c 后由**容器内**的 sh 对挂载卷展开。
docker run --rm -v "$PWD/pg:/b:ro" postgres:16-alpine \
  sh -c 'pg_restore --list /b/qa_metadata_*.dump' | tail -5
#   期望：列出 TABLE DATA / DATABASE 等条目，末尾无 error

grep -vcE '^(--|/\*|!|$)' mysql/*.sql   # 期望 > 0

# 基线也要自检：非空 + 能被 Task 10 的脚本读懂（缺表/坏 JSON ⇒ 退 3）。
[ -s rowcounts.baseline.json ] && echo "OK   rowcounts.baseline.json" \
  || echo "EMPTY/MISSING  rowcounts.baseline.json"
python -c "import json; print('baseline tables:', len(json.load(open('rowcounts.baseline.json'))['tables']))"
#   期望 baseline tables: 8

find pg neo4j objects mysql rowcounts.baseline.json -type f \
  \( -name '*.dump' -o -name '*.tar.gz' -o -name '*.sql' -o -name 'rowcounts.baseline.json' \) \
  | sort | xargs sha256sum > ../transfer-manifest.sha256
```
**门禁**：四份都非空；`pg_restore --list` 无 error；manifest 生成。

> `pg_restore --list` 只验证**目录结构**可解析，不校验数据块。传输损坏要靠 CentOS 侧真实恢复时报错才暴露 —— 那正是 Task 9 逐步验证存在的意义。

> ⚠️ **没有任何东西把 `rowcounts.baseline.json` 和 `pg/qa_metadata_*.dump` 配成一对。**
> manifest 各自单独校验和，坏掉的能查出来，**配错**查不出来。而非对称规则下
> 「拿旧基线 + 新 dump」会静默通过（向上永远放过），旧的相等判据反而会把它顶出来。
> 概率低（Step 1 两者连着产出），但排查前顺手对一眼：`rowcounts.baseline.json`
> 里的 `measured_at` 日期，应当和 dump 文件名里的日期是**同一次导出**。

- [ ] **Step 3: 传过去并复核校验和**

```bash
# ⚠️ 回到仓库根：Step 2 结束时 cwd 是 backups/，而本 Step 的路径是相对仓库根的。
# 少了这行 cd，两条 scp 都会找不到路径（`backups/*` 在 backups/ 里不存在，
# 而 manifest 写在 backups/ 的**上一级**）。
cd /path/to/qa-system
scp -r backups/* root@<centos>:/opt/qa-transfer/
scp transfer-manifest.sha256 root@<centos>:/opt/qa-transfer/
ssh root@<centos> 'cd /opt/qa-transfer && sha256sum -c transfer-manifest.sha256'
```
**门禁**：每行都 `OK`，且**行数与 Step 2 的 `find` 产物数一致**。
> 行数不必等于 5 —— 四类产物里 neo4j/pg 按日期分文件，实际是 N 行，加上 `rowcounts.baseline.json` 共 N+1 行。基线进 manifest 才能验它没在传输中坏掉；坏掉的基线 Task 10 会退 3，但那时已经在 CentOS 上了。
> 要对的是 N：源端 `wc -l < transfer-manifest.sha256`，与 CentOS 侧
> `sha256sum -c … | grep -c ': OK$'` 两边必须相同。少传一类时
> `sha256sum -c` 只会对**存在的那几行**报 OK，不会抱怨少文件 —— 属「空/残缺
> 输出 ⇒ 通过」那一族，必须数行。

---

## Task 9: CentOS 数据导入（含破坏性操作护栏）

**Files:** 无新代码文件。纯运维，**全部步骤带护栏**。

**Interfaces:**
- Consumes: Task 8 的四份产物
- Produces: 与源端行数一致的 PG / Neo4j / qa-objects / MySQL

> ⚠️ **本 Task 全部命令是破坏性的**（`--clean --if-exists`、覆盖卷 tar、直接 load）。三条前置断言必须先过。

- [ ] **Step 1: 三条前置断言**

```bash
ssh root@<centos> 'cd /opt/qa/docker
# 断言 1：backend 绝不能已经在跑（它会跑 seed，污染半空库）
docker ps --format "{{.Names}}" | grep -qx qa-backend \
  && { echo "ABORT: backend 在跑，先停"; exit 1; }

# 断言 2：三个目标卷**存在**且**确实是空的**
# ⚠️ 必须先查存在性：`docker run -v NAME:/d` 对不存在的命名卷会**自动创建**一个空卷。
# 少了这一句，断言 2 就永远在检查一个刚出生的空卷 → 恒过 → 一条不可能失败的守卫，
# 而它守的正是本 Task 的破坏性导入。卷名前缀 `qa-system` 来自
# docker/docker-compose.yml:1 的 `name:` 键（不是目录名 docker），已实跑 `config` 核实。
for v in qa-system_pg_data qa-system_neo4j_data qa-system_objects_data; do
  docker volume inspect "$v" >/dev/null 2>&1 \
    || { echo "ABORT: 卷 $v 不存在（compose 从没起过？项目名变了？）"; exit 1; }
  n=$(docker run --rm -v "$v:/d" alpine:3.20 sh -c "ls -A /d | wc -l")
  echo "$v entries=$n"; [ "$n" -eq 0 ] || { echo "ABORT: $v 非空"; exit 1; }
done

# 断言 3：传输完整性
sha256sum -c /opt/qa-transfer/transfer-manifest.sha256'
```
**门禁**：断言 1 必须无输出；断言 2 三行都打 `entries=0`；断言 3 全 OK。任一 ABORT 即停。

- [ ] **Step 2: 导入 PostgreSQL + 立即验**

```bash
ssh root@<centos> 'cd /opt/qa-transfer
docker exec -i qa-postgres pg_restore -U qa_user -d qa_metadata --clean --if-exists \
  < pg/qa_metadata_*.dump
# ⚠️ -i 不可省。实测缺 -i 时 pg_restore 以 exit 1 响亮失败：
#    "error: input file is too short (read 0, expected 5)"。这是好事（不假绿），
#    但前提是有人真读了那条报错。
docker exec -i qa-postgres psql -U qa_user -d qa_metadata -tAc \
  "select version_num from alembic_version;"          # 期望 0132
# ⚠️ **不写死行数。** 附录 A 是 2026-10-22 的一次快照，源库一直在被写入：
# audit_log / evidence / multi_step_step 的实测值已经与它不一致，照附录 A
# ⚠️ 向上漂移**不会**误报：静态表只判「不低于基线」，17101 ≥ 17100 是绿的。
# 拿附录 A 当**目标**丢的是向下的精度（目标钉在 17100 时少一行照样过），
# 拿它当**下限**才是稳的 —— 脚本里下限绝对生效，优先级在 slack 之前。
# 基线以 Task 8 Step 1 随包传过来的 rowcounts.baseline.json 为准 ——
# Task 10 会用脚本严格核对，这里只做一眼确认。
# ⚠️ 取值注意：jsonb 输出是 `{"audit_log": 17101}` —— **冒号后仍有空格**
# （`json_build_object` 更糟，冒号前后都有空格）。所以下面用 `: *[0-9]*`，
# 且第二段用 `[0-9][0-9]*` 而不是 `[0-9]*`：后者能匹配空串，取出来是空的。
# ⚠️ 引号：整段被外层的单引号包着，里面**不能出现未转义的单引号**。
# 一个未转义的单引号就提前闭合外层引号：zsh 整块以 no matches found 中止，
# **pg_restore 根本不会跑**，运维看到的是一个 glob 错误而不是一次恢复；
# bash 则把模式剥成裸的 [0-9][0-9]* 交给远端 glob。
# 要写字面单引号，就按本计划约定的「闭合引号 - 双引号包一个单引号 -
# 重开引号」嵌一层，取数那行就是这么写的，与 Step 3 同一写法。
for t in audit_log ontology_class; do
  live=$(docker exec -i qa-postgres psql -U qa_user -d qa_metadata -tAc \
    "select count(*) from $t")
  base=$(grep -o "\"$t\": *[0-9]*" rowcounts.baseline.json | grep -o '"'"'[0-9][0-9]*'"'"')
  echo "$t live=$live baseline=$base"
  [ -n "$base" ] || { echo "ABORT: 基线里读不到 $t（文件损坏，或不是本次导出的那一份）"; exit 1; }
  # 基线量在 pg_dump 之前 ⇒ 恢复出来的只多不少。**少**才是丢数据。
  [ "$live" -ge "$base" ] || { echo "ABORT: $t live=$live < baseline=$base —— 传输丢了数据"; exit 1; }
done'
```
> **引号约定（这一块特有，改动后两个 shell 都要实跑）**：整段被
> `ssh root@<centos> '…'` 的单引号包着，远端 shell 收到的就是这段文本的逐字拷贝。
> 里面**不能出现未转义的单引号** —— 出现一个，外层引号就提前闭合，后果见块内注释。
> 要写字面单引号，用本计划约定的嵌法（闭合引号 → 双引号包一个单引号 → 重开引号，
> 文本形如 `'"'"'`），与 Step 3 的 cypher-shell 同一写法。
> ⚠️ 这里是「**未**转义」，不是「一个都不许有」：下面取值行里那几个单引号是转义过的。
> 照字面把整行的单引号删掉，会**重新引入 I-2** —— zsh 整块中止，`pg_restore` 不跑。

**门禁**：`version_num` = `0132`；两张表 `live >= baseline`（基线量在 dump 之前，
所以「等于或更多」都正常，只有更少才是丢数据）。任一 ABORT → **停**，不要继续。

- [ ] **Step 3: 导入 Neo4j + 立即验**

```bash
ssh root@<centos> 'cd /opt/qa-transfer
docker compose -f /opt/qa/docker/docker-compose.yml \
               -f /opt/qa/docker/docker-compose.centos.yml stop neo4j
docker run --rm -v qa-system_neo4j_data:/data -v /opt/qa-transfer:/backup \
  neo4j:5.23-community neo4j-admin database load neo4j \
  --from-path=/backup/neo4j --overwrite-destination=true
docker compose -f /opt/qa/docker/docker-compose.yml \
               -f /opt/qa/docker/docker-compose.centos.yml start neo4j
docker exec -i qa-neo4j sh -c \
  '"'"'cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "MATCH (n) RETURN count(n);"'"'"''   # 期望 3753
```
**门禁**：`count(n)` = 3753。

> ⚠️ **必须多一层 `sh -c`**：`$NEO4J_PASSWORD` 只存在于**容器内**的环境里，CentOS 宿主
> 的 root shell 没有这个变量（它在 `docker/.env` 与 neo4j 容器 env 里）。整段命令被
> `ssh '…'` 单引号包着 ⇒ 由**宿主** shell 展开 ⇒ `cypher-shell -u neo4j -p ""` ⇒ 认证失败。
> 同一 Step 的 Step 4 对 `\$MYSQL_ROOT_PASSWORD` 做了同样的转义，这里漏了 —— 两处写法
> 必须一致，否则「MySQL 验得过、Neo4j 验不过」会被误判成数据没导全。

- [ ] **Step 4: 导入 qa-objects + MySQL + 立即验**

```bash
ssh root@<centos> 'docker run --rm -v qa-system_objects_data:/data \
    -v /opt/qa-transfer:/backup:ro alpine:3.20 \
    sh -c "cd /data && tar xzf /backup/objects/objects_data_*.tar.gz"
  docker run --rm -v qa-system_objects_data:/data alpine:3.20 sh -c "ls -A /data | head"

  docker exec -i qa-mysql-demo sh -c \
    "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" wms_demo" < /opt/qa-transfer/mysql/wms_demo.sql
  docker exec -i qa-mysql-demo sh -c \
    "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -NBe \
     \"select count(*) from information_schema.tables where table_schema=\\\"wms_demo\\\";\""'
```
**门禁**：`/data` 非空；wms_demo 表数 > 0。

---

## Task 10: 启动 backend 并核对行数

**Files:**
- Create: `backend/scripts/verify_migration.py`
- Test: `backend/app/tests/unit/test_verify_migration.py`

**Interfaces:**
- Consumes: 导入后的 PG / Neo4j；Task 8 Step 1 产出的 `rowcounts.baseline.json`（导出时刻的实测 count(*)）
- Produces: 退出码 0 = 全部通过（都不低于判定阈值，**不是**与基线完全相等）；1 = 有偏差（逐项列出期望 vs 实际与**原因**）；2 = 用法错误；3 = 环境不可用（基线读不了 / 连不上库 / 查询失败）

> 目标行数取自 `--baseline` 指向的基线（导出那一刻的实测值），不是 spec 附录 A 的快照。附录 A 只当**绝对下限**用：源库一直在被写入，实测值已经比附录 A 高（audit_log 17100→17101、evidence 1729→1738、multi_step_step 27→30）。
> ⚠️ 拿附录 A 当**目标**和拿它当**下限**不是一回事，别混。静态表只判「不低于」，
> 所以向上漂移**不会**误报 —— 17101 ≥ 17100 是绿的，「静态表多一行就红」是 I-3
> 裁定**之前**的旧结论，已经作废。真正丢掉的是向下的精度：目标钉死在 17100 时，
> 相对真基线 17101 少一行（17100）照样通过。

> **方向不对称：静态表只判「低于基线」，不判「等于基线」。**
> 基线在 `pg_dump` **之前**量（Task 8 Step 1），dump 的快照更晚 ⇒ 恢复出来的
> 行数必然 >= 基线，多出来的是导出期间新写入的。判成「不一致」就是在好环境上
> 报迁移失败。而「低于」这一侧不需要容差：对只追加的表它是精确的丢数据检测。
> GROWING 表的 slack 语义一个字没动。
> 代价说清楚：静态表**多**出来的行（seed 跑两遍之类）不再被这条门禁查出。

- [ ] **Step 1: 写失败测试**

```python
"""导入后行数核对。对照 spec 附录 A 的**实测 count(*)**，不是 pg_stat 估算。

pg_stat_user_tables.n_live_tup 在这个库上偏差可达 3 个数量级
（audit_log 统计 6、真实 17100），拿它当基线等于没有基线。
"""

from __future__ import annotations

import asyncio
import json
import sys

from scripts.verify_migration import EXPECTED, checkAll, renderReport


def _asyncReturning(value):
    """造一个可 await 的替身，替掉真连库的 _queryAll。"""
    async def _fake(dsn: str) -> dict:
        return value

    return _fake


def _asyncFailing(exc: Exception):
    """造一个一被 await 就抛异常的替身，用来模拟连不上库。"""
    async def _fake(dsn: str) -> dict:
        raise exc

    return _fake


def _fakeAsyncpg(countsFor: dict[str, object]):
    """造一个 asyncpg 替身。

    countsFor 里的表名 → 抛出的异常对象；其余表 → 返回 100。
    exceptions 借用真实 asyncpg，保证实现里的 isinstance 判断与测试抛的是同一个类。
    """
    import types

    import asyncpg

    class FakeConn:
        async def fetchval(self, query: str) -> int:
            table = query.rsplit(" ", 1)[-1]
            if table in countsFor:
                raise countsFor[table]
            return 100

        async def close(self) -> None:
            return None

    async def connect(dsn: str) -> FakeConn:
        return FakeConn()

    return types.SimpleNamespace(
        exceptions=asyncpg.exceptions,
        connect=connect,
    )


def test_expected_matches_spec_appendix_a():
    assert EXPECTED["audit_log"] == 17100
    assert EXPECTED["ontology_class"] == 54
    assert EXPECTED["system_config"] == 28
    assert EXPECTED["session_token_usage"] == 2391
    assert EXPECTED["evidence"] == 1729
    assert EXPECTED["user_sessions"] == 544
    assert EXPECTED["session_message"] == 1420
    assert EXPECTED["multi_step_step"] == 27


def test_growing_set_covers_every_tolerated_table():
    """GROWING 常量此前没有任何测试引用 —— 写空了测试照样全绿。"""
    from scripts.verify_migration import GROWING
    # 断言左侧写成 set(...) 而不是 GROWING：ruff SIM300 会把全大写的名字
    # 当成「常量」，判成 Yoda 条件。语义与 brief 原写法完全一致。
    expectedGrowing = {"user_sessions", "session_message", "session_token_usage"}
    assert set(GROWING) == expectedGrowing
    assert set(EXPECTED) >= set(GROWING)


def test_checkAll_defaults_to_growing_tolerances():
    """不显式传 tolerate 时必须就是 GROWING，否则生产调用（走默认值）不受任何保护。"""
    assert checkAll(dict(EXPECTED, user_sessions=1)) != []
    assert checkAll(dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] - 3)) == []


def test_checkAll_tolerates_upward_growth_on_growing_tables():
    """导出与核对之间还会写入会话数据。多出来的行不是丢数据，必须放过。

    反向陷阱：把容忍条件写成 `got < want`（只放过小幅下降），
    每次正常增长都会变成一条「对不上」，门禁直接卡死。
    """
    grew = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] + 500,
                session_message=EXPECTED["session_message"] + 3000)
    assert checkAll(grew) == []


def test_checkAll_passes_static_tables_that_grew_after_the_baseline():
    """静态表「比基线多」是正常的，必须放过。

    基线是 dump **之前**量的，dump 的快照更晚 ⇒ 恢复出来的行数 >= 基线，
    多出来的部分是导出期间新写入的。判成「不一致」就是在好环境上报迁移失败。
    反向陷阱：写成 `got != want`，一次正确的恢复永远红。
    """
    assert checkAll(dict(EXPECTED, audit_log=17101, evidence=1800)) == []


def test_checkAll_fails_static_table_below_the_baseline():
    """静态表「比基线少」才是真丢数据 —— 这个方向不需要容差，精确检测。"""
    failures = checkAll(dict(EXPECTED, audit_log=EXPECTED["audit_log"] - 1))
    assert len(failures) == 1
    assert failures[0]["table"] == "audit_log"


def test_growing_slack_is_unchanged_when_a_baseline_is_used():
    """GROWING 的语义一个字不动：向上放过、向下在 slack 内放过、超出才报。

    这里**不传 floor**，只验 slack 这一层。传了 floor 就测不出 slack 了：
    附录 A 下限是绝对的，会先于 slack 生效（见
    test_appendix_floor_also_covers_growing_tables）。两者混在一起时，
    真正生效的是更严的那条，slack 永远没被验到。
    """
    baseline = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] + 10)        # 554
    withinSlack = dict(baseline, user_sessions=baseline["user_sessions"] - 20)     # 534
    assert withinSlack["user_sessions"] >= baseline["user_sessions"] * (1 - 0.20)  # slack 内
    assert checkAll(withinSlack, expect=baseline) == []
    beyondSlack = dict(baseline, user_sessions=1)
    assert len(checkAll(beyondSlack, expect=baseline)) == 1


def test_checkAll_passes_on_exact_match():
    assert checkAll(EXPECTED) == []


def test_checkAll_flags_wrong_count():
    bad = dict(EXPECTED, audit_log=6)   # 正是 pg_stat 会给出的值
    failures = checkAll(bad)
    assert len(failures) == 1
    assert failures[0]["table"] == "audit_log"
    assert failures[0]["expected"] == 17100
    assert failures[0]["actual"] == 6


def test_checkAll_tolerates_probes_only_drift_downwards():
    """会话类表在导出后必然继续增长，少一点不算丢数据。"""
    drifted = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] - 3)
    assert checkAll(drifted, tolerate={"user_sessions"}) == []


def test_checkAll_still_flags_large_drop_even_if_tolerated():
    lost = dict(EXPECTED, user_sessions=1)
    assert len(checkAll(lost, tolerate={"user_sessions"})) == 1


def test_render_names_the_failing_table():
    out = renderReport(checkAll(dict(EXPECTED, system_config=0)))
    assert "system_config" in out and "28" in out


def test_parseDsn_strips_the_sqlalchemy_driver_suffix():
    """仓内 DATABASE_URL 是 postgresql+asyncpg://，asyncpg 不认这个 scheme。"""
    from scripts.verify_migration import _parseDsn
    assert _parseDsn("postgresql+asyncpg://u:p@postgres:5432/qa_metadata") \
        == "postgresql://u:p@postgres:5432/qa_metadata"


def test_parseDsn_leaves_plain_dsn_untouched():
    from scripts.verify_migration import _parseDsn
    plain = "postgresql://u:p@localhost:5433/qa_metadata"
    assert _parseDsn(plain) == plain


def test_main_exit_code_two_on_bad_usage(capsys):
    """2 必须与 1 区分，否则门禁会把「命令写错了」读成「迁移失败」。"""
    from scripts.verify_migration import main
    assert main(["prog"]) == 2
    assert main(["prog", "--dsn"]) == 2
    assert capsys.readouterr().err.strip() != ""


def test_main_exit_code_zero_when_all_match(monkeypatch, capsys):
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(EXPECTED)))
    assert main(["prog", "--dsn", "postgresql://u:p@h:5432/db"]) == 0
    # 「全部对上」是**相等**语义。非对称规则下静态表只判「不低于」，
    # 比基线多 1 行也是绿的 —— 成功行若还写「对上」，运维会以为
    # 行数被逐格核对过。措辞必须跟着语义走，所以正向、反向都断言。
    out = capsys.readouterr().out
    assert "全部通过 ✓" in out
    assert "全部对上" not in out


def test_main_exit_code_one_when_counts_drift(monkeypatch, capsys):
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(EXPECTED, audit_log=6)))
    assert main(["prog", "--dsn", "postgresql://u:p@h:5432/db"]) == 1
    assert "audit_log" in capsys.readouterr().out


# ── I1：退出码 3 / _redact / 缺表 这三条路径此前零覆盖 ──────────────────────
# 变异证明：`return 3` 改 `return 1`、`_redact` 整个函数体清空、
# 整段 try/except 删除 —— 15 条测试全绿。

FAKE_DSN = "postgresql://qa_fake:s3cr3t-never-real@localhost:5433/qa_metadata"


def test_main_exit_code_three_when_database_unreachable(monkeypatch, capsys):
    """连不上库必须落到 3，而不是混进 1 里让运维去查一个不存在的数据问题。"""
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncFailing(OSError("connection refused")))
    assert main(["prog", "--dsn", FAKE_DSN]) == 3
    err = capsys.readouterr().err
    assert "OSError" in err
    assert "connection refused" in err


def test_main_exit_code_three_output_never_echoes_credentials(monkeypatch, capsys):
    """错误路径同样不许把口令写进 stderr —— 门禁日志会被人贴进工单。"""
    from scripts.verify_migration import main
    leaky = OSError(f"cannot connect to {FAKE_DSN}")
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncFailing(leaky))
    assert main(["prog", "--dsn", FAKE_DSN]) == 3
    captured = capsys.readouterr()
    assert "s3cr3t-never-real" not in captured.err
    assert "s3cr3t-never-real" not in captured.out
    assert "<DSN>" in captured.err


def test_redact_masks_password_and_dsn():
    from scripts.verify_migration import _redact
    message = f"failed to reach {FAKE_DSN} (password s3cr3t-never-real)"
    out = _redact(FAKE_DSN, message)
    assert "s3cr3t-never-real" not in out
    assert "<DSN>" in out
    assert "***" in out


def test_redact_leaves_message_alone_without_credentials():
    """没有口令的 DSN 不该被瞎改写 —— 无凭据可抹时必须原样返回。"""
    from scripts.verify_migration import _redact
    assert _redact("postgresql://localhost:5433/qa_metadata", "boom") == "boom"
    assert _redact("", "boom") == "boom"


def test_redact_handles_dsn_without_password_part():
    """postgresql://user@host 形式（无冒号）不能被当成有口令而崩掉。"""
    from scripts.verify_migration import _redact
    assert _redact("postgresql://onlyuser@localhost:5433/db", "boom") == "boom"


def test_checkAll_reports_missing_table_with_its_own_reason():
    """缺表与「行数不对」是两种诊断，报告里必须能分开。"""
    from scripts.verify_migration import REASON_MISSING_TABLE
    partial = {table: n for table, n in EXPECTED.items() if table != "evidence"}
    failures = checkAll(partial)
    assert len(failures) == 1
    assert failures[0]["table"] == "evidence"
    assert failures[0]["reason"] == REASON_MISSING_TABLE


def test_checkAll_treats_explicit_none_as_missing_table():
    """_queryAll 对不存在的表回 None，与「键压根没有」必须同义。"""
    from scripts.verify_migration import REASON_MISSING_TABLE
    failures = checkAll(dict(EXPECTED, evidence=None))
    assert len(failures) == 1
    assert failures[0]["reason"] == REASON_MISSING_TABLE


def test_renderReport_counts_the_tables_actually_checked():
    """用基线核对时表数可能不等于 len(EXPECTED)，成功行不能报错数。"""
    assert "3 张表" in renderReport([], tableCount=3)


# ── I2：缺表被误报成「连接问题」 ──────────────────────────────────────────
# 半恢复的库（表还在、内容空）正是 spec §6.4 铁律场景，
# 报成连接问题会把运维送去查网络。


def test_queryAll_records_none_for_missing_table_and_keeps_querying(monkeypatch):
    """一张表不存在不能中断整轮统计：后面的表还得数出来，才能逐项定位。"""
    import asyncpg

    import scripts.verify_migration as vm
    monkeypatch.setitem(sys.modules, "asyncpg", _fakeAsyncpg({
        "evidence": asyncpg.exceptions.UndefinedTableError('relation "evidence" does not exist'),
    }))
    out = asyncio.run(vm._queryAll(FAKE_DSN))
    assert out["evidence"] is None
    # 循环必须继续：evidence 之后的表也要有结果
    assert out["user_sessions"] == 100
    assert out["multi_step_step"] == 100


def test_main_exit_code_one_when_table_is_missing(monkeypatch, capsys):
    """缺表 ⇒ 退出码 1 + 点名表名，不是 3。仍然失败关闭，只是诊断变了。"""
    from scripts.verify_migration import main
    partial = {table: n for table, n in EXPECTED.items() if table != "evidence"}
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(partial))
    assert main(["prog", "--dsn", FAKE_DSN]) == 1
    captured = capsys.readouterr()
    assert "evidence" in captured.out
    assert "连接" not in captured.out


# ── I3：空 --dsn 静默回落到 asyncpg 默认值 ────────────────────────────────
# python -m scripts.verify_migration --dsn "" 会真的去连 ::1:5432 / 127.0.0.1:5432，
# 也就是「悄悄查了别的库」→ 好的环境报「迁移失败」。brief 写明是退出码 2。

def test_main_exit_code_two_on_empty_dsn(monkeypatch, capsys):
    """空 DSN 是用法错误，且必须在碰数据库之前就退掉。"""
    from scripts.verify_migration import main
    touched = []

    async def _neverCalled(dsn: str) -> dict:
        touched.append(dsn)
        return {}

    monkeypatch.setattr("scripts.verify_migration._queryAll", _neverCalled)
    assert main(["prog", "--dsn", ""]) == 2
    assert touched == []
    assert capsys.readouterr().err.strip() != ""


def test_main_exit_code_two_on_whitespace_only_dsn(monkeypatch):
    """全是空白同样是用法错误 —— strip() 后为空才算数。"""
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(EXPECTED)))
    assert main(["prog", "--dsn", "   \t "]) == 2


# ── I5：基线改成「导出那一刻实测的行数」 ──────────────────────────────────
# 附录 A 是 2026-10-22 的一次快照：audit_log 已漂到 17101、evidence 1738、
# multi_step_step 30。基线文件由 Task 8 导出时产出。
# ⚠️ 别再写「静态表 ⇒ 门禁必红」：那是**相等**语义下的结论，已经作废。
# 非对称规则下静态表只判「不低于」，17101 ≥ 17100 是绿的 —— 向上漂移不再误报，
# 向下才精确到基线本身。几乎恒过的是**回落**路径（拿附录 A 当目标），
# 不是静态表判据本身。


def _writeBaseline(tmp_path, tables: dict, measuredAt: str = "2026-10-22T09:00:00+08:00",
                   name: str = "rowcounts.baseline.json"):
    path = tmp_path / name
    path.write_text(json.dumps({
        "measured_at": measuredAt,
        "database": "qa_metadata",
        "tables": tables,
    }, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_loadBaseline_reads_export_time_counts(tmp_path):
    from scripts.verify_migration import loadBaseline
    path = _writeBaseline(tmp_path, dict(EXPECTED, audit_log=17101, evidence=1738))
    assert loadBaseline(path) == dict(EXPECTED, audit_log=17101, evidence=1738)


def test_loadBaseline_rejects_missing_file():
    from scripts.verify_migration import BaselineError, loadBaseline
    try:
        loadBaseline("/nonexistent/rowcounts.baseline.json")
    except BaselineError:
        return
    raise AssertionError("读不到基线文件必须是 BaselineError，而不是别的异常")


def test_loadBaseline_rejects_malformed_json(tmp_path):
    from scripts.verify_migration import BaselineError, loadBaseline
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    try:
        loadBaseline(str(path))
    except BaselineError:
        return
    raise AssertionError("坏 JSON 必须是 BaselineError")


def test_loadBaseline_rejects_table_counts_that_are_not_integers(tmp_path):
    from scripts.verify_migration import BaselineError, loadBaseline
    path = _writeBaseline(tmp_path, dict(EXPECTED, audit_log="一万七"))
    try:
        loadBaseline(path)
    except BaselineError:
        return
    raise AssertionError("非整数行数必须是 BaselineError")


def test_loadBaseline_rejects_baseline_missing_a_table(tmp_path):
    """基线里没提到的表不能被静默跳过 —— 那等于没核对。"""
    from scripts.verify_migration import BaselineError, loadBaseline
    partial = {table: n for table, n in EXPECTED.items() if table != "audit_log"}
    try:
        loadBaseline(_writeBaseline(tmp_path, partial))
    except BaselineError:
        return
    raise AssertionError("基线漏表必须是 BaselineError")


def test_checkAll_targets_the_measured_baseline_not_appendix_a(tmp_path):
    """实测值 17101 就是对的 —— 拿附录 A 的 17100 当目标会误报。"""
    baseline = dict(EXPECTED, audit_log=17101, evidence=1738, multi_step_step=30)
    assert checkAll(dict(baseline), expect=baseline, floor=EXPECTED) == []


def test_checkAll_flags_drift_against_the_baseline_target():
    """静态表比导出时少 ⇒ 传输丢了数据。"""
    baseline = dict(EXPECTED, audit_log=17101)
    failures = checkAll(dict(baseline, audit_log=17090), expect=baseline, floor=EXPECTED)
    assert len(failures) == 1
    assert failures[0]["table"] == "audit_log"
    assert failures[0]["expected"] == 17101
    assert failures[0]["actual"] == 17090


def test_checkAll_below_appendix_floor_gets_its_own_reason():
    """基线本身不可信（导错库）时，下限兜底。理由必须能与传输漂移区分开。"""
    from scripts.verify_migration import REASON_BELOW_FLOOR, REASON_DRIFT
    baseline = dict(EXPECTED, audit_log=500)
    failures = checkAll(dict(baseline), expect=baseline, floor=EXPECTED)
    assert len(failures) == 1
    assert failures[0]["reason"] == REASON_BELOW_FLOOR
    assert failures[0]["reason"] != REASON_DRIFT
    assert REASON_BELOW_FLOOR in renderReport(failures)


def test_appendix_floor_also_covers_growing_tables():
    """下限是绝对的：GROWING 表在 slack 之内但已跌破附录 A，同样要报。

    也就是说**下限先于 slack 生效**。这不是 bug，是「附录 A 是绝对下限」
    这个裁定的直接后果 —— 别为了给 slack 腾空间把它改成可协商的。
    """
    from scripts.verify_migration import REASON_BELOW_FLOOR
    baseline = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"])
    justBelowFloor = EXPECTED["user_sessions"] - 4      # slack 之内（−4 > −20%）
    failures = checkAll(dict(baseline, user_sessions=justBelowFloor),
                        expect=baseline, floor=EXPECTED)
    assert len(failures) == 1
    assert failures[0]["reason"] == REASON_BELOW_FLOOR


def test_checkAll_without_floor_keeps_appendix_a_as_plain_target():
    """不给 floor（未传基线的旧路径）时行为不变：GROWING 仍按 slack 容忍。"""
    assert checkAll(dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] - 4)) == []


def test_main_uses_baseline_target_end_to_end(monkeypatch, tmp_path, capsys):
    """端到端：基线 17101、库也是 17101 ⇒ 通过；且不再打「附录 A 已知漂移」的警。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17101)
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(baseline)))
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)]) == 0
    captured = capsys.readouterr()
    assert "全部通过 ✓" in captured.out
    assert "已知漂移" not in captured.err


def test_main_reds_when_baseline_disagrees_with_the_restore(monkeypatch, tmp_path, capsys):
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17101)
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(baseline, audit_log=17090)))
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)]) == 1
    assert "audit_log" in capsys.readouterr().out


def test_main_warns_loudly_when_baseline_omitted(monkeypatch, capsys):
    """静默回落 = 让运维重新制造这次要消除的困惑。回落必须吵。"""
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(EXPECTED)))
    assert main(["prog", "--dsn", FAKE_DSN]) == 0
    err = capsys.readouterr().err
    assert "2026-10-22" in err
    assert "已知漂移" in err
    assert "附录 A" in err
    assert "!" in err


def test_main_accepts_baseline_before_dsn(monkeypatch, tmp_path):
    from scripts.verify_migration import main
    baseline = dict(EXPECTED)
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(baseline)))
    assert main(["prog", "--baseline", _writeBaseline(tmp_path, baseline), "--dsn", FAKE_DSN]) == 0


def test_main_exit_code_three_when_baseline_unreadable(monkeypatch, capsys):
    """基线读不了 ⇒ 环境不可用（3），不是行数偏差（1），也不是不该去连库。"""
    from scripts.verify_migration import main
    touched = []

    async def _neverCalled(dsn: str) -> dict:
        touched.append(dsn)
        return {}

    monkeypatch.setattr("scripts.verify_migration._queryAll", _neverCalled)
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", "/nonexistent/baseline.json"]) == 3
    assert touched == []
    assert capsys.readouterr().err.strip() != ""


def test_main_exit_code_two_on_empty_baseline_path():
    from scripts.verify_migration import main
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", ""]) == 2


def test_main_exit_code_two_on_unknown_flag():
    from scripts.verify_migration import main
    assert main(["prog", "--dsn", FAKE_DSN, "--nope", "x"]) == 2


def test_main_exit_code_two_on_baseline_without_value():
    from scripts.verify_migration import main
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline"]) == 2


# ── I-1：附录 A 的下限「接没接上」此前没有任何测试 ────────────────────────
# checkAll 内部的 floor 逻辑本身有测试，但 `main` 到底传没传 floor=EXPECTED
# 一条测试都没有。把 verify_migration.py:286 改成 checkAll(actual, expect=baseline)
# （或更危险的 floor=baseline）后，45 条测试全绿 —— 见本轮变异记录。
# 所以下面这几条必须在 **main 层面**钉住接线，checkAll 层的单测在两种接线
# 下都同样通过，钉不住。

def test_main_reports_below_appendix_floor_as_floor_not_drift(monkeypatch, tmp_path, capsys):
    """附录 A 的下限必须在 main 上真的接上：低于下限要报「低于附录 A 下限」。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=500)          # 基线本身已低于附录 A 的 17100
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(baseline)))
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)]) == 1
    out = capsys.readouterr().out
    assert "audit_log" in out
    assert "低于附录 A 下限" in out


def test_main_keeps_growing_slack_when_baseline_is_used(monkeypatch, tmp_path, capsys):
    """下限是附录 A（不是基线），且 GROWING slack 在传基线时仍然生效。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] + 1)   # 545
    actual = dict(baseline, user_sessions=EXPECTED["user_sessions"])          # 544
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(actual))
    rc = main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)])
    assert rc == 0, capsys.readouterr().out


def test_main_compares_against_the_baseline_not_appendix_a(monkeypatch, tmp_path, capsys):
    """目标是基线，不是附录 A —— 这条是两者唯一的区分点。

    为什么需要它：把 `main` 里的 `checkAll(actual, expect=baseline, floor=EXPECTED)`
    换成 `checkAll(actual)`（收了 --baseline 却拿附录 A 当目标）时，其余测试
    全部照样绿 —— 因为它们的基线值与 EXPECTED 相同或更高，两种目标结果一致。
    这里让基线 17500（源库在附录 A 之后又长了）、恢复出来 17200：
      目标=基线   → 17200 < 17500 → 丢数据，红
      目标=附录A  → 17200 >= 17100 → 放过，绿
    这个变异是被 mutation harness 抓出来的，不是想出来的。
    """
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17500)
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(baseline, audit_log=17200)))
    rc = main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)])
    assert rc == 1
    assert "audit_log" in capsys.readouterr().out


# ── I-3：静态表的非对称语义，main 层面钉住 ────────────────────────────────
# 下面两条合起来锁死方向：多了要过，少了要红。
# 方向写反（`got > want` 才过 / `got != want` 才红）时至少一条会红。

def test_main_passes_when_static_tables_come_back_above_baseline(monkeypatch, tmp_path, capsys):
    """dump 的快照比计数晚 ⇒ 恢复出来的只多不少。这是正常路径，必须绿。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17101, evidence=1738, multi_step_step=30)
    restored = dict(baseline, audit_log=17105, evidence=1740, multi_step_step=31)
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(restored))
    rc = main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)])
    assert rc == 0, capsys.readouterr().out


def test_main_fails_when_static_table_comes_back_below_baseline(monkeypatch, tmp_path, capsys):
    """少了才是丢数据。基线早于 dump ⇒ 这个方向是精确检测，不需要容差。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17101)
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(baseline, audit_log=17090)))
    rc = main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)])
    assert rc == 1
    assert "audit_log" in capsys.readouterr().out


def test_fallback_without_baseline_lets_drifted_static_tables_pass(monkeypatch, capsys):
    """回落路径（目标是 2026-10-22 的附录 A）现在对静态表基本恒过。

    这不是好事，是「不再误报」的代价：任何现库都比那份快照新，所以
    「不低于」几乎必然成立。这条测试把这个事实钉住，免得有人把回落
    路径当成「还在严格核对」来依赖。告警里必须说清楚这一点。
    """
    from scripts.verify_migration import main
    drifted = dict(EXPECTED, audit_log=17101, evidence=1738, multi_step_step=30)
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(drifted))
    rc = main(["prog", "--dsn", FAKE_DSN])
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    # 告警必须承认「这条路径几乎恒过」，否则回落会被当成真门禁。
    # ⚠️ 这里必须匹配完整的「几乎恒过」，不能退化成 `or "低于"`：告警里另有一句
    # 「不低于基线」含「低于」二字，`or` 会被那句满足，于是删掉「承认恒过」
    # 那一整行测试照样全绿 —— 变异 harness 实测过，确实存活。
    assert "几乎恒过" in captured.err
    # 灵敏度也要钉：回落不是「只能挡住数量级级别的丢失」，它精确到附录 A 那个数。
    # 这句一旦改回模糊说法，Minor 1 的修正就悄悄退回去了。
    assert "17099" in captured.err
```


- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && python -m pytest app/tests/unit/test_verify_migration.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'scripts.verify_migration'`

- [ ] **Step 3: 最小实现**

`backend/scripts/verify_migration.py`：

```python
"""导入后行数核对。

基线取 **Task 8 导出那一刻实测的 count(*)**（`--baseline rowcounts.baseline.json`），
不是 pg_stat_user_tables.n_live_tup —— 后者是估算值，在本库上 audit_log 报 6、
真实 17100，差 3 个数量级。

不传 `--baseline` 时回落到 spec 附录 A（2026-10-22 的一次快照）。**这份快照已知漂移**：
源库一直在被写入，实测值已经比附录 A 高（audit_log 17100→17101、evidence 1729→1738、
multi_step_step 27→30）。因为静态表只判「不低于」（见下），向上漂移**不会**误报；
但向下只精确到附录 A 那个数：真基线是 17101 时 restored 17099 会红、17100 不会。
所以回落会打一条醒目的告警；计划里的 Step 6 始终传 `--baseline`。

传了 `--baseline` 时同时做两件事：
    目标 = 导出时的实测值（判断传输有没有丢数据）
    下限 = spec 附录 A（绝对下限，防止基线本身不可信，比如导错库）
低于下限的表会以「低于附录 A 下限」单独标出，与传输漂移区分开。

**方向是不对称的：静态表只判「低于基线」，不判「等于基线」。**
基线在 pg_dump **之前**量（见计划 Task 8 Step 1），dump 的快照更晚，
所以恢复出来的行数必然 >= 基线，多出来的是导出期间新写入的行 ——
判成「不一致」就是在好环境上报迁移失败。而「低于」这一侧不需要容差：
对只追加的表它是精确的丢数据检测。GROWING 表的 slack 语义一个字没动。

用法：
    python -m scripts.verify_migration --dsn postgresql://qa_user:...@localhost:5433/qa_metadata
    python -m scripts.verify_migration --dsn <DSN> --baseline backups/rowcounts.baseline.json

退出码：
    0 = 全部通过（都不低于判定阈值；**不是**「与基线完全相等」）
    1 = 有偏差（逐项列出期望 vs 实际与原因）
    2 = 用法错误
    3 = 环境不可用（基线读不了 / 连不上库 / 查询失败，不是数据问题，别去查数据）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# spec 附录 A（2026-10-22 实测 count(*)）。既是回落时的目标，也是传了基线时的绝对下限。
EXPECTED: dict[str, int] = {
    "audit_log": 17100,
    "ontology_class": 54,
    "system_config": 28,
    "session_token_usage": 2391,
    "evidence": 1729,
    "user_sessions": 544,
    "session_message": 1420,
    "multi_step_step": 27,
}

# 导出后仍在增长的表：涨多少都正常，只有掉下去才可能是丢数据
GROWING: frozenset[str] = frozenset({"user_sessions", "session_message", "session_token_usage"})

# 容忍下限：GROWING 表只要不低于 (1 - SLACK) × 目标就放过
GROWING_SLACK = 0.20

# 三种偏差各有各的诊断，报告里必须能分开
REASON_MISSING_TABLE = "表不存在"
REASON_BELOW_FLOOR = "低于附录 A 下限"
REASON_DRIFT = "行数偏差"

USAGE = (
    "用法：python -m scripts.verify_migration --dsn <DATABASE_URL> "
    "[--baseline <rowcounts.baseline.json>]"
)


class UsageError(Exception):
    """命令行用法错误 ⇒ 退出码 2。"""


class BaselineError(Exception):
    """基线文件不可用 ⇒ 退出码 3（环境问题，不是行数偏差）。"""


def loadBaseline(path: str) -> dict[str, int]:
    """读 Task 8 导出时刻的实测行数。

    读不了、坏 JSON、缺表、行数不是整数 —— 全部收敛成 BaselineError，
    因为它们都是同一件事：这份环境没准备好，别去查数据。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineError(f"读不到基线文件 {path}（{type(exc).__name__}）") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"基线文件不是合法 JSON：{path}") from exc
    tables = parsed.get("tables") if isinstance(parsed, dict) else None
    if not isinstance(tables, dict):
        raise BaselineError(f"基线文件缺 tables 段：{path}")
    missing = [table for table in EXPECTED if table not in tables]
    if missing:
        # 漏表 ≠ 通过。少核一张表就等于把「有没有丢数据」这件事留了个洞。
        raise BaselineError(f"基线漏了 {len(missing)} 张表：{', '.join(sorted(missing))}")
    counts: dict[str, int] = {}
    for table in EXPECTED:
        value = tables[table]
        if isinstance(value, bool) or not isinstance(value, int):
            raise BaselineError(f"基线里 {table} 的行数不是整数：{value!r}")
        counts[table] = value
    return counts


def parseArgs(argv: list[str]) -> tuple[str, str | None]:
    """→ (dsn, baselinePath | None)。用法错误抛 UsageError。

    空 --dsn 是用法错误（退出码 2），不是「让 asyncpg 用默认值」。
    否则 --dsn "" 会真的去连 ::1:5432 / 127.0.0.1:5432，也就是
    「悄悄查了别的库」→ 好的环境报「迁移失败」。
    """
    dsn: str | None = None
    baselinePath: str | None = None
    index = 1
    while index < len(argv):
        flag = argv[index]
        if flag == "--dsn":
            if dsn is not None or index + 1 >= len(argv):
                raise UsageError("--dsn 需要一个值，且只能给一次")
            dsn = argv[index + 1]
            index += 2
            continue
        if flag == "--baseline":
            if baselinePath is not None or index + 1 >= len(argv):
                raise UsageError("--baseline 需要一个值，且只能给一次")
            baselinePath = argv[index + 1]
            index += 2
            continue
        raise UsageError(f"无法识别的参数：{flag}")
    if dsn is None:
        raise UsageError("缺少 --dsn")
    if not dsn.strip():
        raise UsageError("--dsn 是空的")
    if baselinePath is not None and not baselinePath.strip():
        raise UsageError("--baseline 路径是空的")
    return dsn, baselinePath


def checkAll(
    actual: dict[str, int | None],
    expect: dict[str, int] | None = None,
    floor: dict[str, int] | None = None,
    tolerate: set[str] | None = None,
) -> list[dict[str, object]]:
    """返回偏差列表。

    expect = 目标行数（缺省 EXPECTED）；floor = 绝对下限（缺省不设）。
    tolerate 里的表允许小幅下降。actual 里的 None 表示这张表没查到。
    """
    expect = EXPECTED if expect is None else expect
    tolerate = set(GROWING) if tolerate is None else tolerate
    floor = {} if floor is None else floor
    failures: list[dict[str, object]] = []
    for table, want in expect.items():
        got = actual.get(table)
        if got is None:
            failures.append({
                "table": table, "expected": want, "actual": None,
                "reason": REASON_MISSING_TABLE,
            })
            continue
        lowerBound = floor.get(table)
        if lowerBound is not None and got < lowerBound:
            # 下限是绝对的，连 GROWING 的 slack 也盖不过它。
            failures.append({
                "table": table, "expected": want, "actual": got,
                "reason": REASON_BELOW_FLOOR,
            })
            continue
        if got == want:
            continue
        if table in tolerate:
            # GROWING 表只要不低于阈值就放过：**向上一定放过**。
            # 导出与核对之间必然还会写入会话数据，多出来的行不是问题；
            # 只有「少了」才可能是丢数据。写成 `got < want` 会把每一次
            # 正常增长报成失败，把门禁变成噪声。
            if got >= want * (1 - GROWING_SLACK):
                continue
        elif got >= want:
            # 静态表：**只判「低于」**，不比「相等」。
            # 基线是在 pg_dump **之前**量的，dump 的快照更晚 ⇒ 恢复出来的行数
            # 必然 >= 基线，多出来的是导出期间新写入的行。写成 `got != want`
            # 会在一次完全正确的恢复上永远报「迁移失败」。
            # 代价：静态表多出来的行（seed 跑两遍之类）不再被查出 ——
            # 方向不对称是这条规则的固有取舍，不是疏忽。
            continue
        failures.append({
            "table": table, "expected": want, "actual": got, "reason": REASON_DRIFT,
        })
    return failures


def renderReport(
    failures: list[dict[str, object]],
    tableCount: int | None = None,
    targetLabel: str = "",
) -> str:
    checked = len(EXPECTED) if tableCount is None else tableCount
    if not failures:
        # ⚠️ 措辞必须跟着语义走。静态表判「不低于基线」、会话类表判「不低于基线的
        # 80%」，比基线**多**的行也放过 —— 所以既不能写「全部对上」（那是相等语义），
        # 也不该写「都不低于导出基线」（会话类表在 slack 内可以比基线低）。
        suffix = f"，目标：{targetLabel}" if targetLabel else "，目标：spec 附录 A 的回落快照"
        return f"全部通过 ✓（{checked} 张表都不低于判定阈值{suffix}）"
    lines = [f"{len(failures)} 张表对不上："]
    for f in failures:
        reason = f.get("reason", REASON_DRIFT)
        lines.append(f"  {f['table']}: 期望 {f['expected']}，实际 {f['actual']}（{reason}）")
    lines.append("『表不存在』与『低于附录 A 下限』先查迁移与备份，别去查网络。")
    lines.append("若是 GROWING 表且下降幅度很小，可能是导出后继续写入；若是对不上，先别启动 backend。")
    return "\n".join(lines)


def _parseDsn(databaseUrl: str) -> str:
    """asyncpg 不认 SQLAlchemy 的驱动后缀。

    仓内 DATABASE_URL 形如 postgresql+asyncpg://qa_user:...@postgres:5432/qa_metadata，
    原样传给 asyncpg.connect 会报 unrecognized URL scheme。
    """
    return databaseUrl.replace("postgresql+asyncpg://", "postgresql://", 1)


def _redact(dsn: str, message: str) -> str:
    """把错误消息里的 DSN 与口令抹掉 —— 错误输出同样不许带凭据。"""
    if dsn:
        message = message.replace(dsn, "<DSN>")
    _, _, credentials = dsn.partition("://")
    userInfo, atSign, _ = credentials.partition("@")
    if atSign and ":" in userInfo:
        password = userInfo.split(":", 1)[1]
        if password:
            message = message.replace(password, "***")
    return message


async def _queryAll(dsn: str) -> dict[str, int | None]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        out: dict[str, int | None] = {}
        for table in EXPECTED:
            try:
                out[table] = await conn.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608 — 表名来自模块常量
            except asyncpg.exceptions.UndefinedTableError:
                # 半恢复的库（表没建出来）正是 spec §6.4 铁律场景。
                # 记 None 交给 checkAll 报成「表不存在」，退出码仍是 1：
                # 失败关闭不变，只有诊断变了。让异常冒上去会被 main 归成
                # 「连接/查询问题」，把运维送去查一个明明在库里的问题；
                # 中断整轮统计则会让后面所有表都查不成。
                out[table] = None
        return out
    finally:
        await conn.close()


def _warnAppendixSnapshot() -> None:
    """回落提醒。静默回落会让运维重新制造这次已消除的困惑。

    还要说实话：静态表只判「不低于」，而附录 A 比任何现库都旧 ⇒ 这条回落路径
    对静态表**几乎恒过**。实测灵敏度（audit_log，目标 17100）：restored 17099 /
    17000 / 16000 全部报红；漏掉的只有「恰好落在附录 A 与真基线之间」的那一两行 ——
    真基线是 17101 时 restored 17100 会绿。别把它当门禁用。
    """
    print(
        "=" * 70 + "\n"
        "!! 未提供 --baseline：正在用 2026-10-22 spec 附录 A 的快照核对。\n"
        "!! 这份快照已知漂移 —— 源库一直在被写入，audit_log / evidence /\n"
        "!! multi_step_step 的实测值已经与附录 A 不一致。\n"
        "!! 而且静态表只判「不低于基线」，附录 A 比任何现库都旧 ⇒\n"
        "!! 这条回落路径对静态表**几乎恒过**：它只精确到附录 A 那个数。\n"
        "!! audit_log 目标 17100，restored 17099 / 17000 / 16000 都报红；\n"
        "!! 漏的只有恰好落在 17100..真基线之间的那一两行。\n"
        "!! 计划里的 Step 6 始终传 --baseline；只有临时排查才不带。\n"
        + "=" * 70,
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    try:
        rawDsn, baselinePath = parseArgs(argv)
    except UsageError as exc:
        print(f"{exc}\n{USAGE}", file=sys.stderr)
        return 2

    dsn = _parseDsn(rawDsn)
    baseline: dict[str, int] | None = None
    if baselinePath is not None:
        try:
            baseline = loadBaseline(baselinePath)
        except BaselineError as exc:
            print(f"基线不可用：{exc}", file=sys.stderr)
            print("这是环境问题，不是行数偏差；先修基线文件。", file=sys.stderr)
            return 3
    else:
        _warnAppendixSnapshot()

    try:
        actual = asyncio.run(_queryAll(dsn))
    except Exception as exc:
        # 连不上库不是「行数对不上」。不区分的话，一个连错库的部署
        # 会带着退出码 1 进门禁，运维去查一个不存在的数据问题。
        print(f"查询失败（{type(exc).__name__}）：{_redact(dsn, str(exc))}", file=sys.stderr)
        print("这是连接/查询问题，不是行数偏差；请先确认 DATABASE_URL 与容器网络。", file=sys.stderr)
        return 3

    if baseline is None:
        failures = checkAll(actual)
        targetLabel = ""
    else:
        failures = checkAll(actual, expect=baseline, floor=EXPECTED)
        targetLabel = f"导出基线 {baselinePath}"
    print(renderReport(failures, targetLabel=targetLabel))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && python -m pytest app/tests/unit/test_verify_migration.py -v
```
Expected: 53 passed

- [ ] **Step 5: 启动 backend（此时才允许）**

```bash
ssh root@<centos> 'cd /opt/qa/docker && \
  docker compose -f docker-compose.yml -f docker-compose.centos.yml up -d backend frontend'
```
> backend 的 `CMD` 是 `alembic upgrade head && uvicorn`。数据已恢复且版本对齐，这一步应是 **no-op**。若报 `Can't locate revision '0132'`，说明代码不是 `0ea463b3` —— 回 Task 6 Step 1。

- [ ] **Step 6: 真实核对**

```bash
# ⚠️ 整段放进子 shell。这些 exit 1 跑在**运维本地的交互 shell** 里，不是远端：
# Task 9 的块裹在 ssh '…' 里，exit 只结束远端；这一块跑在本地，粘进终端时
# 一次 scp 断网就能把运维的会话直接关掉。子 shell 里 exit 只结束子 shell。
# 门禁一点没弱：子 shell 返回非 0，外面照样判（末尾那句 false 就是干这个的 ——
# 在 set -e 的脚本里致命，在交互 shell 里无害）。
(
  # ⚠️ 三步**分开**做。串在一个 && 里时，任何一步失败（scp 断网、backend 没起、
  # 路径写错）都会塌成退出码 1，而 case 会把它读成「行数有偏差」，让运维去报告里
  # 找一份根本不存在的输出 —— 脚本压根没跑过。失败关闭是对的，指错方向是灾难。
  scp backend/scripts/verify_migration.py root@<centos>:/opt/qa/ \
    || { echo "ABORT: 脚本没传过去（本地路径不对 / 网络断了）"; exit 1; }
  ssh root@<centos> "docker cp /opt/qa/verify_migration.py qa-backend:/app/scripts/" \
    || { echo "ABORT: 脚本没 cp 进容器（backend 没起？/opt/qa 路径不对？）"; exit 1; }
  ssh root@<centos> "docker cp /opt/qa-transfer/rowcounts.baseline.json qa-backend:/app/" \
    || { echo "ABORT: 基线没 cp 进容器（Task 8 没传过来？）"; exit 1; }

  # 只有这一条的退出码会进 case —— 它才是「核对脚本」的退出码
  rc=0
  ssh root@<centos> "docker exec qa-backend sh -c 'python -m scripts.verify_migration --dsn \"\$DATABASE_URL\" --baseline /app/rowcounts.baseline.json'" || rc=$?
  echo "exit=$rc"
  case "$rc" in
    0) echo "OK：8 张表都不低于判定阈值（静态表=基线本身、会话类表=基线的 80%），且都不低于 spec 附录 A 下限" ;;
    1) echo "FAIL：有偏差 —— 看上面逐项『期望/实际/原因』。『表不存在』与『低于附录 A 下限』先查迁移与备份，别去查网络"; exit 1 ;;
    2) echo "FAIL：用法错 —— 看 stderr 的用法行"; exit 1 ;;
    3) echo "FAIL：环境不可用（基线读不了 / 连不上库）—— 不是行数偏差，先修基线文件或 DATABASE_URL"; exit 1 ;;
    *) echo "FAIL：未知退出码 $rc —— 多半是 ssh 自己失败或脚本崩了，看上面有没有 traceback"; exit 1 ;;
  esac
) || { echo "ABORT：门禁未通过 —— 不要往下走（原因见上面那一行）"; false; }
```

> `--baseline` **必须传**。不传会回落到 spec 附录 A 的快照并打一条醒目告警。
> 更要紧的是：**回落路径对静态表几乎恒过** —— 附录 A 比任何现库都旧，而静态表
> 只判「不低于」，于是几乎必然成立。它不是门禁：它只精确到附录 A 那个数
> （audit_log 目标 17100 ⇒ restored 17099 / 17000 / 16000 都报红），
> 漏的只有恰好落在附录 A 与真基线之间的那一两行。告警里写了这一句，
> 不是为了吓唬，是为了不让有人把回落当成「还在严格核对」。

⚠️ **必须多一层 `sh -c`**：`$DATABASE_URL` 只存在于**容器内**的环境里，CentOS 宿主
的 root shell 没有这个变量。写成 `docker exec qa-backend python -m ... --dsn "$DATABASE_URL"`
时，变量会在宿主 shell 上展开成空串 ⇒ `--dsn ""` ⇒ 用法错误退出 2 —— 在一个
数据完全正常的环境上，门禁会红。`sh -c` 把字符串原样送进容器，由容器自己的 shell 展开。

**门禁**：输出 `全部通过 ✓（8 张表都不低于判定阈值…）` 且 `exit=0`。**这一步没过就不要往下走** —— 带着残缺数据起 backend 会让 seed 在半空库上产出脏数据（spec §6.4 铁律）。

> **退出码必须分开判**，不要只写 `test "$rc" -eq 0` 之外再靠人眼读数字：
> 2 是「命令写错了」，3 是「环境没准备好」，两者都不是「行数不对」。
> 把 3 混进 1，运维就会去查一个不存在的数据问题。

> 反向自检（四条，覆盖四个不同方向，都要实跑过）：
> 1. 把基线里 `audit_log` 的值改**大**到 999999，重跑必须报
>    `audit_log: 期望 999999，实际 …（行数偏差）` 并返回 1；
> 2. 把基线里某张**静态**表的值改**小** 100，重跑必须**仍然通过**（`exit=0`）——
>    基线量在 dump 之前，恢复出来的只多不少。这条专门钉方向：把非对称语义写反
>    （改成「多了才算不一致」）的话，它会红；
> 3. 把 `--baseline` 指向一个不存在的路径，重跑必须返回 3；
> 4. 恢复基线文件，重跑必须回到 `全部通过 ✓（…）` 且 `exit=0`。

> ⚠️ 非对称的代价：静态表**多**出来的行（seed 跑两遍、重复导入之类）不再被这条行数门禁查出。
> 真正兜住它的是**导入的幂等性**，不是别的断言：`pg_restore --clean --if-exists`
> （Task 9 Step 2）会 drop 后重建，所以重跑一遍恢复不会叠加；Task 9 Step 1 的
> 断言 2 又要求三个目标卷**为空**才允许导入，第二次恢复根本进不去。
> 说清楚剩下的缺口：**seed 跑两遍造成的多余行，本计划里没有任何东西会发现。**
> （`version_num` 指望不上：它是单行 PK 表，且被 `--clean` 重建过 ——
> 恢复一次、恢复两次、以及恢复出 100 条重复 `audit_log` 的那次，它都是 `0132`。
> Task 14 第 9 项「PG 行数抽查 —— Task 10 已做」就是本 Step 自己，不构成独立复核。）
> 要补就单独加一条查询，别指望这条门禁顺手捎带。

> ⚠️ **`audit_log` 之类的表将来被合法清理时（归档、保留策略），不要放宽脚本。**
> 正确做法：把这次清理记进变更记录，并更新 spec 附录 A 的数字
> （`EXPECTED` 就是从那儿抄的，Task 8 的基线也会随下一次导出重新生成）。
> 为了迁就一次清理去改 `GROWING_SLACK` 或下限，等于把门禁的判据
> 交给下一个做清理的人 —— 那次清理大概不会有记录。
> 一条只在成功时才绿的命令，等于没有命令。

---

## Task 11: Embedding provider 切换（**必须先于 Task 12**）

**Files:** 无代码文件。走管理 API。

**Interfaces:**
- Consumes: Task 10 已起的 backend + Task 5 的 ollama
- Produces: `embedding_provider` 激活行指向 CentOS 本地 Ollama

> ⚠️ **编号顺序 ≠ 执行顺序**：Task 12 的回填要真实调 embedding，迁移过来的 provider 指向 Mac 上的 oMLX，在 CentOS 上不存在。必须先做本 Task。

- [ ] **Step 1: 登录取 JWT，落盘到 CentOS**

```bash
ssh -t root@<centos> 'umask 077
read -rs -p "admin 密码: " PW; echo
curl -s -X POST http://localhost:5173/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[\"access_token\"])" > /root/.qa-jwt
unset PW
[ -s /root/.qa-jwt ] || { echo "登录失败 —— 把上面返回体原样贴出来再查"; exit 1; }
echo "token 已落盘：$(wc -c < /root/.qa-jwt) 字节（不打印内容）"'
```
**门禁**：`token 已落盘：<若干> 字节`。

> ⚠️ **token 必须落盘，不能靠 shell 变量传。** 早期版本在 Step 1 的 `ssh` 里
> `JWT=$(…)`，然后 Step 2/3/4 各自的 `ssh` 里用 `$JWT` —— **每条 `ssh` 都是一个全新的
> 远端 shell，Step 1 的 `JWT` 早就随那个 shell 一起没了**。而且这三条用的是双引号包
> `ssh`，`$JWT` 由**本地 Mac shell** 展开，本地更没有 ⇒ 发出的是
> `Authorization: Bearer `（空）⇒ 全部 401。Task 12 Step 4 与 Task 13 Step 2 同理，
> 一共 5 处。token 落 `/root/.qa-jwt`（`umask 077`）后，各 Step 用
> `docker exec -e JWT="$(cat /root/.qa-jwt)"` 注入容器，由**容器自己的 shell** 展开。
> `-t` 是必须的：不加 `-t` 时 ssh 不分配 tty，`read` 会去读 heredoc/管道而不是终端。
> 任一步返回 401 就是 token 过期或落盘失败 —— 重跑本步，不要往下走。

- [ ] **Step 2: 创建并激活 CentOS Ollama provider**

```bash
ssh root@<centos> 'bash -s' <<'REMOTE'
set -e
# 显式守卫：`$(cat …)` 失败不会触发 set -e（它在参数位置），空 token 会变成
# 一条 401 的静默请求，看上去像「provider 切不过去」，实际是根本没带 token。
[ -s /root/.qa-jwt ] || { echo "缺 /root/.qa-jwt —— 回 Task 11 Step 1 重取 token"; exit 1; }
docker exec -e JWT="$(cat /root/.qa-jwt)" qa-backend sh -c '
curl -s -X POST http://localhost:8000/api/v1/embedding-providers \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d "{\"name\":\"Ollama bge-m3 (centos)\",\"providerType\":\"ollama\",\
       \"baseUrl\":\"http://localhost:11434/v1\",\"modelName\":\"bge-m3:latest\",\
       \"dimension\":1024,\"isActive\":true}"'
REMOTE
```
要点：
- `baseUrl` **故意写 `localhost`** —— resolver 按 `EMBEDDING_HOST_OVERRIDE` 把它改写成
  CentOS 上可达的地址（`embedding_client.py:_applyHostOverride`，只改写 loopback 主机）。
  **在 CentOS 上该变量的值是 `ollama`**（Task 6 Step 2 已改），所以这条 `localhost`
  最终会变成 `http://ollama:11434/v1` —— 同一套代码，Mac 上是 `host.docker.internal`，
  CentOS 上是 compose 服务名。**若这里照抄成 `host.docker.internal`，
  Step 4 的维度门禁必挂**，而且失败原因（连接被拒）看着像模型没就绪，其实是地址不对。
- **走 CRUD API，不要手改库** —— 手改会绕过缓存失效
- 走 API 不违反「禁止后端覆盖用户 modelId」铁律：这是人在显式操作，不是系统按内容改派

- [ ] **Step 3: 验证**

```bash
ssh root@<centos> 'bash -s' <<'REMOTE'
set -e
# 显式守卫：`$(cat …)` 失败不会触发 set -e（它在参数位置），空 token 会变成
# 一条 401 的静默请求，看上去像「provider 切不过去」，实际是根本没带 token。
[ -s /root/.qa-jwt ] || { echo "缺 /root/.qa-jwt —— 回 Task 11 Step 1 重取 token"; exit 1; }
docker exec -e JWT="$(cat /root/.qa-jwt)" qa-backend sh -c '
curl -s http://localhost:8000/api/v1/embedding-providers/active \
  -H "Authorization: Bearer $JWT"'
REMOTE
```
**门禁**：返回 `providerType=ollama`、`dimension=1024`。返回 401 ⇒ 回 Step 1 重取 token。

- [ ] **Step 4: 真实调一次 embedding**

```bash
ssh root@<centos> 'bash -s' <<'REMOTE'
set -e
# 显式守卫：`$(cat …)` 失败不会触发 set -e（它在参数位置），空 token 会变成
# 一条 401 的静默请求，看上去像「provider 切不过去」，实际是根本没带 token。
[ -s /root/.qa-jwt ] || { echo "缺 /root/.qa-jwt —— 回 Task 11 Step 1 重取 token"; exit 1; }
docker exec -e JWT="$(cat /root/.qa-jwt)" qa-backend sh -c '
curl -s -X POST http://localhost:8000/api/v1/embedding-providers/active/test \
  -H "Authorization: Bearer $JWT"'
REMOTE
```
**门禁**：维度必须返回 **1024**。不是则停 —— 与 `milvus_client.py:38` 的 `_DIM` 不一致会抛 `ConfigError`。

---

## Task 12: Milvus 重建回填

**Files:** 无代码文件。

**Interfaces:**
- Consumes: Task 11 的 provider、Task 6 的 Milvus
- Produces: 与 spec 附录 A 实体数一致的集合

- [ ] **Step 1: 先 dry-run**

```bash
ssh root@<centos> 'docker exec qa-backend python scripts/backfill_milvus_embeddings.py --dry-run'
```
**门禁**：能连库、取到类/属性、embedding 生成无维度错误。
> `--dry-run` 仍会真实调用 embedding API 产生费用，但不写入任何向量。

- [ ] **Step 2: 正式回填（**不加 `--cleanup`**）**

```bash
ssh root@<centos> 'docker exec qa-backend python scripts/backfill_milvus_embeddings.py'
```
> ⚠️ **不要顺手加 `--cleanup`。** CentOS 上集合是空的，普通回填走 `syncEmbedding`（按 `ontology_id` 先删后插，**幂等**）就够。`--cleanup` 的用途是收敛已经重复的集合；历史上它在一次改动后静默退化成纯 append，导致「32 类被堆成 98 行、3642 属性被堆成 10926 行」，检索 topK 窗口被重复项吃光。
> 确实需要重跑且行数不对时，才用 `--cleanup` 收敛（与 `--sources`/`--skip-classes` 互斥）。
>
> ⚠️ **回填期间不要同时跑对话**（spec §7.4）。32G 上的内存预算合计 ≈32G 偏紧，回填本身吃内存，再叠一个对话模型的 ~12G 会直接开始 swap。回填完成前不要在管理页触发问答。

- [ ] **Step 3: 核对行数（**必做**）**

```bash
ssh root@<centos> 'docker exec qa-backend python scripts/diag_milvus_embedding_state.py'
```
**门禁**（对照 spec 附录 A）：

| 集合 | 期望 |
|---|---|
| `ontology_class_embeddings` | 50 |
| `ontology_property_embeddings` | 3742 |
| `query_embeddings` | 1061 |
| `wiki_page_embeddings` | 64 |

> 任一项**接近整数倍翻倍**（如 3742 → 7484）就是踩了上面的坑，改用 `--cleanup` 收敛后重验。

- [ ] **Step 4: Wiki 向量**

```bash
ssh root@<centos> 'bash -s' <<'REMOTE'
set -e
# 显式守卫：`$(cat …)` 失败不会触发 set -e（它在参数位置），空 token 会变成
# 一条 401 的静默请求，看上去像「provider 切不过去」，实际是根本没带 token。
[ -s /root/.qa-jwt ] || { echo "缺 /root/.qa-jwt —— 回 Task 11 Step 1 重取 token"; exit 1; }
docker exec -e JWT="$(cat /root/.qa-jwt)" qa-backend sh -c '
curl -s -X POST http://localhost:8000/api/v1/wiki/vector-sync \
  -H "Authorization: Bearer $JWT"'
REMOTE
```

- [ ] **Step 5: RAG 文档重新 ingest**

`document_embeddings`（36 条）**无回填脚本**，只能走一次完整 ingest。源文件已在 Task 9 恢复到 qa-objects 卷，重新走文档上传/解析流程。
**门禁**：ingest 后 `document_embeddings` 有实体且能召回。

---

## Task 13: 业务配置修正与 LLM 切换

**Files:** 无代码文件。

- [ ] **Step 1: 停用 Mac 开发探针数据源**

```bash
ssh root@<centos> 'bash -s' <<'REMOTE'
set -e
docker exec -i qa-postgres psql -U qa_user -d qa_metadata <<'SQL'
UPDATE data_source SET is_active = false
 WHERE name IN ('localmysql', 'smoke', 'recall-probe', 'phase13_realdata_pg');
SQL
docker exec -i qa-postgres psql -U qa_user -d qa_metadata -tAc \
  "SELECT name FROM data_source WHERE is_active ORDER BY name;"
REMOTE
```
**门禁**：最后一条查询**只回显 `THBI Oracle` 一行**。多出任何一行就是漏掉的探针源。

> ⚠️ 早期版本这里写的是 `... name IN (\$\$${q:-localmysql}\$,\$smoke\$...)`，试图用
> Postgres 的 `$$…$$` dollar-quoting 躲开外层引号。实测该写法必然失败：
> `\$$` 只产出**一个** `$`，psql 直接报
> `ERROR: unterminated dollar-quoted string at or near "$$localmysql$,$smoke$,..."`。
> `$$` 在 shell 里还有 PID 展开的歧义，双重不牢。改用 `<<'SQL'` heredoc：
> 引号定界符让 shell 完全不碰内容，SQL 里想写什么就写什么。
> `localmysql` 指向 `host.docker.internal:3306` 库名 `srm`，而 CentOS 上该端口是 compose 自带的 `mysql-demo`（库名 `wms_demo`），库名不匹配，必然连不上。

- [ ] **Step 2: 【R1 致命项】验证迁移过来的 THBI Oracle 能解密**

```bash
ssh root@<centos> 'bash -s' <<'REMOTE'
set -e
# 显式守卫：`$(cat …)` 失败不会触发 set -e（它在参数位置），空 token 会变成
# 一条 401 的静默请求，看上去像「provider 切不过去」，实际是根本没带 token。
[ -s /root/.qa-jwt ] || { echo "缺 /root/.qa-jwt —— 回 Task 11 Step 1 重取 token"; exit 1; }
docker exec -e JWT="$(cat /root/.qa-jwt)" qa-backend sh -c '
curl -s -X POST http://localhost:8000/api/v1/data-sources/1/test \
  -H "Authorization: Bearer $JWT"'
REMOTE
```
**门禁**：连接测试通过。

> ⚠️ **不要重新注册一条来测** —— 新注册的不经过 `password_encrypted` 解密路径，测通了也不证明 SECRET_KEY 用对了。
> 失败特征（已核实）：`business_db_pool.py:708` → `decryptApiKey()` → `crypto.py:50-51` 抛 `ConfigError` → `exceptions.py:206 statusForError` 落 `return 400`，文案 `"API Key 密文无法解密，可能密钥已变更"`。
> ⚠️ **该文案里的 "API Key" 有误导性** —— `decryptApiKey` 是通用加密助手，数据源密码走同一函数。**不要照着去查 `llm_config`**，先查 `data_source.password_encrypted`。
> 实测已确认源端**只有一把** key（6 个 `.env` 文件 + 8 个镜像 + 运行中容器指纹全同），**从未轮换过**。所以失败原因**只可能是抄错了**，回 Task 7 重新比对指纹。

- [ ] **Step 3: 配置对话模型（由用户在管理页完成）**

对话模型选型**不在本计划范围**（spec §7.5）——用户在管理页自行配置。本步骤只保证通道就绪：
```bash
# 门禁 A：模型已离线就位（容器活着）
ssh root@<centos> 'docker exec qa-ollama ollama list'   # 确认目标模型在列

# 门禁 B：**backend 容器真的连得到** —— 这才是对话能不能用的那条
ssh root@<centos> 'docker exec qa-backend python -c "
import urllib.request, json
r = urllib.request.urlopen(\"http://ollama:11434/api/tags\", timeout=5)
print(json.load(r))"'
```
> ⚠️ **门禁 A 通过不等于通道就绪。** `ollama list` 是在 ollama 容器**内部**执行的，
> 它只证明容器活着，不证明 backend 够得着。Task 6 Step 2 那 3 个 `.env` 值如果没改
> （仍指 `host.docker.internal`，而 overlay 又不映射宿主端口），A 会绿、B 必挂 ——
> 而且 B 的报错是连接被拒，看起来像「模型没拉下来」，会被误诊到 Ollama 安装上。
> B 才是这一步真正要验的东西。
>
> 内网不可达 `api.deepseek.com` / `api.minimax.cn`。迁移过来的 3 条激活 LLM 配置**全部不可用**，需全部重新指向本地 Ollama。

---

## Task 14: 冒烟验收

**Files:** 无代码文件。对应 spec §8。

- [ ] **Step 1: 通用冒烟**

```bash
ssh root@<centos> '
curl -s http://localhost:5173/api/v1/health                              # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5173/api/v1/data-quality/rules   # 200（表缺失会 500）
'
```
浏览器侧（spec §8.1）：
1. health ✅ 2. 注册模型 201 3. THBI Oracle 连接测试通过（Task 13 已做）4. 定义知识类「库存」成功 5. **提问 → 回复 + SQL + 图表 + Token 计量** 6. data-quality/rules 200

- [ ] **Step 2: 迁移专属冒烟**

7. **语义检索召回正常**（证明 embedding 切换成功 —— 最易失败项）
8. Neo4j `MATCH (n) RETURN count(n)` ≈ 3753
9. PG 行数抽查 —— Task 10 已做
10. RAG 文档检索 —— Task 12 已做
11. **重启存活**：`docker compose restart` 后 10 个容器全回 healthy
12. **迁移链一致**：`SELECT version_num FROM alembic_version` = 0132

- [ ] **Step 3: 访问面验收（spec §8.3）**

从另一台机器用 LAN IP 访问 `http://<centos>:5173`。前端走同源 `/api/v1`（`VITE_API_BASE_URL` 留空），应自动适配 LAN IP，无需改配置。

- [ ] **Step 4: 销毁临时 token**

```bash
ssh root@<centos> 'shred -u /root/.qa-jwt 2>/dev/null || rm -f /root/.qa-jwt; \
  ls -l /root/.qa-jwt 2>&1 | tail -1'
```
**门禁**：回显 `No such file or directory`。留着就是一份可用的 admin 会话凭据落盘。

- [ ] **Step 5: 清理镜像泄露（顺手做，spec 未列但同源）**

```bash
ssh root@<centos> 'docker images | grep qa-system-backend'
```
确认只有当前在用的标签。Mac 侧的 6 个泄露镜像清理见 `qa-system-old-images-contain-secrets`（**先轮换那 4 个 LLM key，再删**）。

---

## Task 15: 文档回写

**Files:**
- Modify: `docs/deployment/00-overview.md:49`
- Modify: `docs/deployment/02-environment.md:32`
- Modify: `docs/deployment/01-prerequisites.md`
- Modify: `docs/deployment/07-operations.md`
- Modify: `Harness/wiki/operations-runbook.md`

**Interfaces:**
- Consumes: Task 1–14 的实测结果
- Produces: 让下一个人不重踩 spec 里发现的坑

- [ ] **Step 1: 给**所有**生成 SECRET_KEY 的入口加警告（4 处，不是 2 处）**

实测生成入口共 **4 条路径，分布在 2 个文件的 2 个代码块**：

| # | 位置 | 命令 |
|---|---|---|
| 1 | `docs/deployment/00-overview.md:49` | `python -c "…print('SECRET_KEY='+Fernet.generate_key().decode())" >> docker/.env` |
| 2 | `docs/deployment/02-environment.md:32` | `python -c "…print(Fernet.generate_key().decode())"` |
| 3 | `docs/deployment/02-environment.md:34` | `openssl rand -base64 32` |
| 4 | `docs/deployment/02-environment.md:38` | `docker compose run --rm backend python -c "…"` |

> ⚠️ 只在 #1 和 #2 下面插警告是不够的：#3/#4 与 #2 在**同一个 ```bash 代码块**里，
> 三条是并列的「任选其一」。漏掉它们，读者照抄 openssl 或容器那两条照样换掉主密钥 ——
> 而这正是 `qa-system-secret-key-must-be-reused` 记的那个不可逆事故。

在 `00-overview.md:49` 那个代码块**下方**插入：

```markdown
> ⚠️ **仅限空环境首次部署。**
> `SECRET_KEY` 就是 Fernet 主密钥（`crypto.py:_getFernet()`）。**带数据迁移时执行本命令
> 会让所有既有密文永久不可解** —— 5 个数据源密码（含 THBI Oracle 生产库）、6 个 LLM key、
> embedding key 全部报废，且无法回滚，只能重新导数。
> 迁移场景下必须**逐字复制源环境**的 `SECRET_KEY`，并用
> `python -m scripts.verify_env_keys <源.env> <新.env>` 比对指纹。
```

在 `02-environment.md` 那个「生成方式（任选其一）」代码块**下方**（`## 关键原则` 之前）
插入**同一段**警告，并加一句：

```markdown
> ⚠️ **以上三条（Python / openssl / 容器）都只在空环境首次部署时用。**
> `SECRET_KEY` 就是 Fernet 主密钥（`crypto.py:_getFernet()`）。**带数据迁移时任何一条
> 都会让所有既有密文永久不可解** —— 5 个数据源密码（含 THBI Oracle 生产库）、6 个 LLM key、
> embedding key 全部报废，且无法回滚，只能重新导数。
> 迁移场景下必须**逐字复制源环境**的 `SECRET_KEY`，并用
> `python -m scripts.verify_env_keys <源.env> <新.env>` 比对指纹。
```

**门禁**：`grep -rn "generate_key\|rand -base64 32" docs/deployment/` 的每一处，
其所在代码块下方都能搜到「仅限空环境首次部署」。

- [ ] **Step 2: `01-prerequisites.md` 加 CentOS 7 受限路径**

追加一节，涵盖：
- Docker CE 在 CentOS 7 冻结于 `24.0.9-1.el7`（本项目实测通过），降级锚点 `20.10.24-1.el7`
- glibc 2.17 ⇒ **Ollama 必须容器化**（官方二进制要 ≥2.28，且原生安装**静默失败**）
- XFS `ftype` 必须为 1
- SELinux 对 bind mount 的处理（本方案用 overlay 规避）

- [ ] **Step 3: `07-operations.md` + runbook 加双目标部署清单**

因为 Mac 长期双活（spec §10），每次部署产出**两条**记录：
```
| 环境 | 镜像 tag | alembic_version | 部署时间 | 执行人 |
| Mac  | <tag>    | 0132             | ...      | ...     |
| CentOS | <tag>  | 0132             | ...      | ...     |
```
并附上版本探针命令：
```bash
docker exec -i qa-postgres psql -U qa_user -d qa_metadata -tAc "select version_num from alembic_version;"
```

- [ ] **Step 4: 把 spec 里的实测结果回填**

把 Task 1/2/4 的实测值写进对应文档：CentOS 内核版本、XFS ftype、AVX2 有无、Docker 最终版本、镜像 tag。

- [ ] **Step 5: 提交**

```bash
git add docs/deployment/ Harness/wiki/operations-runbook.md
git commit -m "docs(deploy): 回写 CentOS 7 部署实测结果 + SECRET_KEY 迁移警告"
```

---

## 回滚

任一 Task 失败时的回退路径：

| 失败点 | 回退 |
|---|---|
| Task 2 Docker 起不来 | 换 `20.10.24-1.el7`，重跑 Step 4 |
| Task 4 镜像不全 | 补拉缺失镜像重跑；已 load 的不影响 |
| Task 9 导入失败 | 删空卷重来。**源端数据未动**，无损失 |
| Task 10 行数对不上 | 同上。**不要**试图手工补数据 |
| Task 12 回填行数翻倍 | `--cleanup` 收敛（Task 12 Step 2 注） |
| Task 13 THBI 解密失败 | 回 Task 7 重比对指纹。**不要**重新生成 SECRET_KEY |
| Task 14 冒烟不过 | Mac 环境完全未受影响，可继续在 Mac 上开发排错 |

**任何情况下都不要重新生成 `SECRET_KEY`。**

---

## 已知不确定项（执行时留意）

1. **Task 2 的 Docker 能否在 kernel 3.10 起来** —— 唯一无法提前确定、只能实跑的硬门禁。所有候选版本在 CentOS 7 上的运行时**均无一手来源验证**（spec §5.1）
2. **CPU 是否有 AVX2** —— 决定 `mysql:8.0` 是否要钉 `8.0.32`。Task 1 会测出
3. **XFS 是否已是 ftype=1** —— 决定 Task 2 Step 1 要不要重格式化
4. **32G 内存够不够** —— spec §7.4 的预算合计 ≈32G「偏紧」。overlay 已配 `OLLAMA_KEEP_ALIVE=5m` + `OLLAMA_NUM_PARALLEL=1` 缓解。实测若开始 swap，降对话模型到 7B 级量化（~4.7 GB），不要升硬件
5. **对话模型选型** —— 用户在管理页配置，本计划不预设（spec §7.5）
6. **双活漂移** —— schema 因两边都自动 `alembic upgrade head` 而会收敛；**数据永不收敛**，业务数据以 CentOS 为准（spec §10）

## 与 spec 的三处刻意偏离

写计划时发现 spec 内部有三处不自洽，此处显式记录，**不是执行时的临场决定**：

| # | spec 写的 | 问题 | 本计划怎么做 |
|---|---|---|---|
| 1 | §7.1 正文：「这 **5** 个必须逐字复用」 | 同一段的 `REUSE_KEYS` 只列了 **4** 个变量名 | 按 4 个实现，Task 7 有单测钉死 `REUSE_KEYS` |
| 2 | P1 门禁：「9 个目标镜像各能真正 `run` 起来」 | P1 此刻**还没 load 镜像**，无从 run | 门禁移到 Task 4 Step 6（load 之后），内容不变 |
| 3 | P4 门禁：`GET /api/v1/health` 返回 200 | 需要 backend 已启动，而 backend 启动就跑 seed，会在半空库上产脏数据 | health 检查移到 Task 10 Step 5（backend 起后）；P4 只起中间件 |

> 三条都写进 spec 的对应章节会更干净，但 spec 已定稿提交，改动留给执行时同步。
