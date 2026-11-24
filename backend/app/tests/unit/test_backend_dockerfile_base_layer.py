"""后端 Dockerfile 的基础层拆分守卫（2026-11-24 性能回归防护）。

背景：原 `docker/Dockerfile.backend` 的 runtime stage 内含一次 apt 安装
（OCR/PDF/Office/中文字体，约 200 个 deb 包）。业务代码一变，该层的缓存
就失效 ⇒ 改一行 Python 也要重装全部系统包（实测 15–25 分钟，且遇网络抖动
产生大量 `Ign:` 重试）。

已拆成：
- `docker/Dockerfile.backend.base` —— 只含系统包层，预构建为
  `qa-system-backend-base:2026-11`（每个 platform 一次）；
- `docker/Dockerfile.backend` —— runtime stage `FROM ${RUNTIME_BASE}`。

本守卫防止有人把 apt 装回业务 Dockerfile（缓存又会失效）。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.backend"
BASE_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.backend.base"

#: runtime 必备的系统包（base 镜像必须装全）。
REQUIRED_RUNTIME_PACKAGES = (
    "tesseract-ocr",
    "tesseract-ocr-chi-sim",
    "poppler-utils",
    "libreoffice-calc",
    "libreoffice-writer",
    "libreoffice-impress",
    "fonts-noto-cjk",
)


class TestBackendDockerfileBaseLayer:
    def test_runtime_stage_uses_base_image(self) -> None:
        code = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
        assert "ARG RUNTIME_BASE=" in code, "必须声明 RUNTIME_BASE 构造参数"
        assert "FROM ${RUNTIME_BASE}" in code, (
            "runtime stage 必须 FROM ${RUNTIME_BASE}（预构建基础镜像）"
        )

    def test_business_dockerfile_has_no_apt_install(self) -> None:
        """业务 Dockerfile 里出现 apt install ⇒ 缓存又会被打穿（性能回归）。"""
        code = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
        # runtime stage = 最后一个 FROM 之后（builder 的 build-essential 编译层不受影响，
        # 它在 runtime 之前，且只动一次业务代码不会命中它）
        runtime_part = code.rsplit("\nFROM ", 1)[-1]
        assert "apt-get install" not in runtime_part, (
            "runtime stage 不得再 apt install —— 系统包层应在 "
            "docker/Dockerfile.backend.base 里预构建"
        )

    def test_base_image_installs_all_runtime_packages(self) -> None:
        code = BASE_DOCKERFILE.read_text(encoding="utf-8")
        assert "apt-get install" in code, "基础镜像必须装 runtime 系统包"
        for package in REQUIRED_RUNTIME_PACKAGES:
            assert package in code, f"基础镜像缺少系统包：{package}"

    def test_base_image_keeps_aliyun_mirror(self) -> None:
        """国内网络：必须切阿里云源（否则 deb.debian.org 频繁 502）。"""
        code = BASE_DOCKERFILE.read_text(encoding="utf-8")
        assert "mirrors.aliyun.com" in code
