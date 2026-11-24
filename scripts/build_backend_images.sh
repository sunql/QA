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

# ---- 构建后自检：防止「镜像时间对、内容旧」（2026-11-24 实战踩过：
#      amd64 构建启动早于某次提交 ⇒ 上下文快照是旧代码）
# 做法：进镜像 grep 本批的关键代码标记，缺任一即失败退出。
echo "[build] 自检：镜像内关键代码标记…"
CHECK_MARKERS=(
  "app/services/agent_tools_procurement.py:_PendingDialect"
  "app/api/v1/procurement_analytics.py:该数据源执行待到货查询失败"
  "app/api/v1/procurement_copilot.py:deleteCopilotConversation"
)
MISSING=""
for marker in "${CHECK_MARKERS[@]}"; do
  file="${marker%%:*}"
  pattern="${marker#*:}"
  if ! docker run --rm --platform "$PLATFORM" --entrypoint sh "$IMAGE_TAG" -c \
      "grep -qF '$pattern' /app/$file"; then
    MISSING="${MISSING} '$file:$pattern'"
  fi
done
if [ -n "$MISSING" ]; then
  echo "[build] ❌ 镜像内缺少关键代码：${MISSING}" >&2
  echo "[build]    ⇒ 构建上下文可能是旧代码，请确认提交时间晚于构建启动时间" >&2
  exit 1
fi

echo "[build] ✅ 完成：${IMAGE_TAG}（关键代码自检通过）"
