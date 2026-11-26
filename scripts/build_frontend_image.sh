#!/usr/bin/env bash
# 构建前端镜像（发版 amd64 / 本地 arm64）
#
# 为什么要这个脚本（2026-11-24 实测）：
#   `docker compose build frontend` 走官方 npm 源会间歇性失败
#   （`npm error Exit handler never called!` ⇒ tsc not found ⇒ 构建中断）。
#   阿里云镜像源稳定，必须显式传 NPM_REGISTRY。
#
# 用法：
#   ./scripts/build_frontend_image.sh                            # 本地 arm64
#   ./scripts/build_frontend_image.sh --platform linux/amd64 \
#       --tag amd64-1124-fix02                                  # 发版 amd64
#
# 发版后：
#   docker save qa-system-frontend:<tag> | gzip > qa-frontend-<tag>.tar.gz
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PLATFORM="linux/arm64"
TAG="latest"
NPM_REGISTRY="https://registry.npmmirror.com"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --npm-registry) NPM_REGISTRY="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

IMAGE_TAG="qa-system-frontend:${TAG}"

echo "[build] platform=${PLATFORM} npm=${NPM_REGISTRY} image=${IMAGE_TAG}"
docker buildx build --platform "$PLATFORM" \
  -f docker/Dockerfile.frontend \
  --build-arg "NPM_REGISTRY=${NPM_REGISTRY}" \
  --build-arg "VITE_API_BASE_URL=" \
  -t "$IMAGE_TAG" --load .

# ---- 构建后自检：防止「镜像时间对、内容旧」（2026-11-24 实战踩过：
#      amd64 构建启动早于某次提交 ⇒ 上下文快照是旧代码，bundle hash 却是新的时间戳）
# 做法：取镜像内 bundle 名，与源码里的关键文案做交叉验证；两者不一致 ⇒ 失败退出。
echo "[build] 自检：镜像内 bundle 与关键文案…"
BUNDLE_NAME="$(docker run --rm --platform "$PLATFORM" --entrypoint sh "$IMAGE_TAG" -c \
  'ls /usr/share/nginx/html/assets/index-*.js | head -1 | xargs basename' | tr -d '\r')"
echo "[build]   bundle=${BUNDLE_NAME}"
if [ -z "$BUNDLE_NAME" ]; then
  echo "[build] ❌ 镜像内未找到 bundle，构建异常" >&2
  exit 1
fi

# 关键文案校验（任一缺失即失败）：这些串出现在 i18n 资源里，源码改动后必然同步。
CHECK_TEXTS=("采购 Copilot" "待到货 / 逾期未到" "分析结果")
MISSING=""
for text in "${CHECK_TEXTS[@]}"; do
  if ! docker run --rm --platform "$PLATFORM" --entrypoint sh "$IMAGE_TAG" -c \
      "grep -qF '$text' /usr/share/nginx/html/assets/index-*.js"; then
    MISSING="${MISSING} '${text}'"
  fi
done
if [ -n "$MISSING" ]; then
  echo "[build] ❌ 镜像内缺少关键文案：${MISSING}" >&2
  echo "[build]    ⇒ 构建上下文可能是旧代码，请确认提交时间晚于构建启动时间" >&2
  exit 1
fi

echo "[build] ✅ 完成：${IMAGE_TAG}（bundle=${BUNDLE_NAME}，关键文案自检通过）"
