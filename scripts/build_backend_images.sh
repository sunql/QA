#!/usr/bin/env bash
# 构建后端镜像（基础层预构建 + 业务层快速重建）
#
# 为什么需要这个脚本（2026-11-24 实测）：
#   原 Dockerfile 的 runtime stage 里有一次 apt 安装（约 200 个 deb 包），
#   耗时 15–25 分钟；业务代码一变，这层的缓存就失效 ⇒ 改一行 Python 也要
#   重装全部系统包。拆成「基础镜像 + 业务镜像」后：
#     - 基础镜像每个 platform 只构建一次（之后零成本）；
#     - 业务镜像重建只跑最后一层 ⇒ 1–2 分钟。
#
# 用法：
#   ./scripts/build_backend_images.sh                       # 本地 arm64（开发用）
#   ./scripts/build_backend_images.sh --platform linux/amd64 \
#       --tag amd64-1124-fix02                             # 发版 amd64
#   ./scripts/build_backend_images.sh --skip-base           # 基础镜像已存在时跳过
#
# 说明：
#   - 业务镜像 tag 默认 qa-system-backend:latest（compose 本地引用名）；
#   - 发版时另存一份 amd64-<标识> 并 docker save（tar 自带基础层，生产只需一个包）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PLATFORM="linux/arm64"
TAG="latest"
SKIP_BASE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --skip-base) SKIP_BASE=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

# 基础镜像 tag：与平台绑定，避免 arm64/amd64 互相覆盖
case "$PLATFORM" in
  linux/amd64) BASE_TAG="qa-system-backend-base:2026-11-amd64" ;;
  *)           BASE_TAG="qa-system-backend-base:2026-11" ;;
esac

IMAGE_TAG="qa-system-backend:${TAG}"

echo "[build] platform=${PLATFORM} base=${BASE_TAG} image=${IMAGE_TAG}"

if [[ "$SKIP_BASE" -eq 0 ]]; then
  if docker image inspect "$BASE_TAG" >/dev/null 2>&1; then
    echo "[build] 基础镜像已存在，跳过：${BASE_TAG}"
  else
    echo "[build] 构建基础镜像（一次，约 15–25 分钟）→ ${BASE_TAG}"
    docker buildx build --platform "$PLATFORM" \
      -f docker/Dockerfile.backend.base \
      -t "$BASE_TAG" --load .
  fi
else
  docker image inspect "$BASE_TAG" >/dev/null 2>&1 || {
    echo "[build] --skip-base 但基础镜像不存在：${BASE_TAG}" >&2
    exit 1
  }
fi

echo "[build] 构建业务镜像（约 1–2 分钟）→ ${IMAGE_TAG}"
docker buildx build --platform "$PLATFORM" \
  -f docker/Dockerfile.backend \
  --build-arg "RUNTIME_BASE=${BASE_TAG}" \
  -t "$IMAGE_TAG" --load .

echo "[build] ✅ 完成：${IMAGE_TAG}"
