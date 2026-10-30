"""镜像密钥泄露 preflight（只读，部署门禁）。

在部署 backend 镜像**之前**运行，确认本地环境文件没被烤进镜像。

**为什么需要**：``.dockerignore`` 排的是 build context，而 ``Dockerfile.backend``
里的 ``COPY backend/ ./`` 会把整个 ``backend/`` 塞进镜像。``backend/.env`` 被
``.gitignore`` 挡住所以不会进版本库，但**它照样会进 build context** —— 一旦
``.dockerignore`` 漏了规则，装 ``localhost:5433`` dev 库凭据的 ``.env`` 就会被
``COPY`` 进镜像，任何拿到镜像的人都能读到。

**这不影响运行时**：``app/config.py`` 的 pydantic ``env_file=".env"`` 只是兜底，
进程环境变量优先；compose 用 ``env_file: - .env``（即 ``docker/.env``）注入配置。
所以镜像里没有 ``/app/.env`` 容器照样正常跑。

用法::

    uv run python scripts/preflight_image_secrets.py [image]   # 默认 qa-system-backend:latest

退出码：无敏感文件 → 0；有 → 1。

两项检查：

1. ``no_secret_env_files`` —— ``/app`` 下没有真 ``.env``（``.env.example`` 放行）
2. ``env_example_present`` —— **反向守卫**：``.env.example`` 必须还在。

第 2 项不是凑数：如果 ``find`` 写错或探针坏掉，第 1 项会因为「没查到东西」
而静默 PASS。第 2 项要求模板文件确实存在，探针坏掉时它会红，从而堵住这个假绿
（对照 memory ``qa-system-same-status-code-false-green``）。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_DEFAULT_IMAGE = "qa-system-backend:latest"

# 判定为「真 .env」的 basename 前缀；模板不在此列
_ENV_PREFIX = ".env"
_TEMPLATE_NAME = ".env.example"
# 依赖树里的同名文件不算泄露：.env 是 pypi 上真实存在的包名
_VENV_MARKERS = ("/.venv/", "/site-packages/", "/node_modules/")


@dataclass(frozen=True)
class CheckResult:
    """单项检查结果（frozen，不可变）。"""

    name: str
    passed: bool
    detail: str


def classifySecretFiles(paths: Iterable[str]) -> list[str]:
    """挑出镜像内不该存在的环境文件，按传入顺序返回。

    放行 ``.env.example``（模板，新人 onboarding 依赖）与 ``.venv`` /
    ``site-packages`` 下的同名文件（``.env`` 在 pypi 上是真实存在的包名）。
    """
    violations: list[str] = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if not name.startswith(_ENV_PREFIX) or name == _TEMPLATE_NAME:
            continue
        if any(marker in path for marker in _VENV_MARKERS):
            continue
        violations.append(path)
    return violations


def listImageEnvFiles(
    image: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> list[str]:
    """列出镜像 ``/app`` 下的 ``.env*`` 文件。

    用镜像自带的 python 而不是 ``find``：``python:3.12-slim`` 不保证装了
    ``findutils``，而 python 是这个镜像必然有的。
    """
    proc = runner(
        [
            "docker", "run", "--rm", "--entrypoint", "python", image,
            "-c",
            "import pathlib;"
            "print('\\n'.join(str(p) for p in pathlib.Path('/app').rglob('.env*')))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def runChecks(
    image: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[CheckResult]:
    """执行两项检查（只读，不改镜像）。"""
    found = listImageEnvFiles(image, runner=runner)
    violations = classifySecretFiles(found)

    return [
        CheckResult(
            name="no_secret_env_files",
            passed=not violations,
            detail=(
                f"镜像 {image} 内未发现敏感 .env"
                if not violations
                else f"镜像 {image} 内烤进了 {len(violations)} 个敏感 .env：{', '.join(violations)}"
            ),
        ),
        CheckResult(
            name="env_example_present",
            passed=any(p.rsplit("/", 1)[-1] == _TEMPLATE_NAME for p in found),
            detail=(
                f"模板 {_TEMPLATE_NAME} 在位，探针有效"
                if any(p.rsplit("/", 1)[-1] == _TEMPLATE_NAME for p in found)
                else f"模板 {_TEMPLATE_NAME} 也不存在 —— 探针可能失效，本检查不可信"
            ),
        ),
    ]


def main() -> int:
    image = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_IMAGE
    try:
        results = runChecks(image)
    except subprocess.CalledProcessError as exc:
        print(f"[FAIL] 无法读取镜像 {image}：docker 返回 {exc.returncode}")
        print("  先确认镜像已构建：docker images | grep " + image.split(":")[0])
        return 1

    failed = False
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")
        if not result.passed:
            failed = True

    if failed:
        print("  修复：确认 .dockerignore 含 **/.env 与 !**/.env.example，"
              "并从干净的 git worktree 构建（主仓的本地 .env 会进 build context）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())