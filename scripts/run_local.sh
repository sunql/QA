#!/usr/bin/env bash
# scripts/run_local.sh - 在容器外运行 uvicorn 的统一入口
#
# 解决的问题：docker-compose 把 PG 宿主端口映射成 5433（避让本机 openmetadata 的 5432），
# 直接 `uvicorn app.main:app` 会拿 backend/.env 的 localhost:5433（已 OK），但其他
# 端口契约（Milvus / Neo4j）和 EMBEDDING_HOST_OVERRIDE 需要明确处理。
#
# 用法：
#   ./scripts/run_local.sh                       # 默认启动 uvicorn :8000
#   ./scripts/run_local.sh --port 8001           # 指定端口
#   ./scripts/run_local.sh --no-preflight        # 跳过端口可达性检查
#   ./scripts/run_local.sh alembic upgrade head  # 跑 alembic（自动用 5433）
#
# 自动行为：
#   1. 检测 .env 是否在容器内运行（/proc/1/cgroup 含 docker / k8s）；容器内拒绝运行
#   2. 检测 PG/Milvus/Neo4j 端口可达性，连不上时给精确提示（哪个端口、哪个服务）
#   3. 自动注入 EMBEDDING_HOST_OVERRIDE=（容器外留空，容器内才需 host.docker.internal）
#
# 详见 Harness/wiki/operations-runbook.md「SSOT 端口契约」段。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[run_local]${NC} $*"; }
warn() { echo -e "${YELLOW}[run_local]${NC} $*" >&2; }
err()  { echo -e "${RED}[run_local]${NC} $*" >&2; }

# ---- 1. 拒绝在容器内运行 ----
if [ -f "/proc/1/cgroup" ] && grep -qE "docker|containerd|kubepods" /proc/1/cgroup 2>/dev/null; then
  err "检测到容器内环境。本脚本仅用于容器外（宿主/本机）运行 uvicorn。"
  err "容器内请用：docker compose -f docker/docker-compose.yml up -d backend"
  exit 1
fi

# ---- 2. 检查 backend/.env ----
if [ ! -f "$BACKEND_DIR/.env" ]; then
  err "缺 $BACKEND_DIR/.env"
  err "复制模板：cp $BACKEND_DIR/.env.example $BACKEND_DIR/.env"
  exit 1
fi

# ---- 3. 端口可达性预检 ----
PREFLIGHT=true
PORT="8000"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-preflight) PREFLIGHT=false; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --help|-h)
      cat <<'EOF'
Usage: scripts/run_local.sh [OPTIONS] COMMAND

OPTIONS:
  --port PORT              uvicorn 监听端口（默认 8000）
  --no-preflight           跳过端口可达性预检
  --help, -h               显示本帮助

EXAMPLES:
  scripts/run_local.sh
  scripts/run_local.sh --port 8001
  scripts/run_local.sh alembic upgrade head
  scripts/run_local.sh --no-preflight .venv/bin/uvicorn app.main:app --port 8001

PORTS（宿主端口，避让本机 openmetadata）：
  PostgreSQL  localhost:5433
  Milvus      localhost:19530
  Neo4j       localhost:7687

详见 Harness/wiki/operations-runbook.md「SSOT 端口契约」段。
EOF
      exit 0
      ;;
    *) break ;;
  esac
done

check_port() {
  local host="$1" port="$2" name="$3"
  if nc -z -G 2 "$host" "$port" 2>/dev/null; then
    log "✓ $name @ $host:$port"
  else
    err "✗ $name @ $host:$port 连不上"
    case "$name" in
      PostgreSQL)
        err "  提示：宿主 PG 端口是 5433（避让本机 openmetadata 的 5432）"
        err "  确认：docker ps | grep qa-postgres"
        err "  启动：docker compose -f docker/docker-compose.yml up -d postgres"
        ;;
      Milvus)
        err "  启动：docker compose -f docker/docker-compose.yml up -d milvus-standalone"
        ;;
      Neo4j)
        err "  启动：docker compose -f docker/docker-compose.yml up -d neo4j"
        ;;
    esac
    return 1
  fi
}

if [ "$PREFLIGHT" = true ]; then
  log "端口可达性预检："
  check_port localhost 5433 "PostgreSQL" || true
  check_port localhost 19530 "Milvus"     || true
  check_port localhost 7687  "Neo4j"      || true
  echo
fi

# ---- 4. 注入宿主环境覆盖 ----
# 容器外不需要 host.docker.internal 改写（resolver 用 localhost 直连本机 Ollama/Embedding）
export EMBEDDING_HOST_OVERRIDE="${EMBEDDING_HOST_OVERRIDE:-}"

# ---- 5. 进入 backend 目录执行 ----
cd "$BACKEND_DIR"

# .env 用 dotenv 自动加载（pydantic-settings 会读 DATABASE_URL 等）
# 这里显式 export 一次，确保 alembic/uvicorn 拿得到
set -a
# shellcheck disable=SC1091
source "$BACKEND_DIR/.env"
set +a

# 端口契约 SSOT 检查（防御性）
if [[ "$DATABASE_URL" == *"localhost:5432"* ]]; then
  err "⚠️  $BACKEND_DIR/.env 仍写 localhost:5432"
  err "   宿主 PG 端口已重映射到 5433，请改为 localhost:5433"
  err "   见 Harness/wiki/operations-runbook.md「SSOT 端口契约」"
  exit 1
fi

log "启动命令：$*"
log "DATABASE_URL=$DATABASE_URL"
echo

exec "$@"