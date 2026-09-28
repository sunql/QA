#!/usr/bin/env bash
# scripts/deploy_backend.sh - 后端「原地更新」容器代码（不重建镜像）
#
# 解决的问题：镜像重建（docker compose build backend）是**正确**的部署方式，但
# Docker Hub 不可达时 build 会失败。本脚本是那条路的替代，用 docker cp 把宿主改动
# 灌进运行中的容器。
#
# 为什么不能只 cp 一部分 —— 这是本脚本存在的核心理由：
#   1. **`app/` + `scripts/` + `alembic/` 三者要一起灌。**
#      - 只灌 app/：`app/main.py` 与其依赖（如 app/infrastructure/schema_drift.py）
#        会形成「新一半 + 旧一半」的错配。
#      - 只灌 app/ 不灌 alembic/：容器 CMD 是 `alembic upgrade head && uvicorn`，
#        新迁移的 revision 不在容器里 → alembic 报找不到 rev → uvicorn 起不来。
#   2. **源路径必须带尾斜杠 `/.`**：`docker cp backend/app qa-backend:/app/app/`
#      会嵌套成 `/app/app/app`，代码改了却跑旧的（表现为莫名 404）。
#
# 用法：
#   ./scripts/deploy_backend.sh              # 灌代码 + 重启 + 等启动
#   ./scripts/deploy_backend.sh --no-restart # 只灌代码，不重启
#   ./scripts/deploy_backend.sh --rollback   # 回滚到本次灌代码前的快照
#
# 自动行为：
#   1. 前置检查：容器在跑、宿主要灌的三个目录都存在
#   2. 覆盖前先把容器内这三处打包快照到 backups/container/<时间戳>/（可回滚）
#   3. 依次 cp app/ scripts/ alembic/（均为 `src/.` → `dst/` 形式）
#   4. 重启容器并轮询日志，等到 "Application startup complete." 才算成功；
#      失败则打印最近日志并提示用 --rollback
#
# ⚠️ docker cp 是**临时**手段：容器一旦被重建（compose up / build），灌进去的代码
#    全部丢失并回退到镜像里的版本。网络恢复后请用
#    `docker compose build backend && docker compose up -d backend` 固化。
#
# 设计要点：
#   - 快照先于覆盖：回滚成本极低，比「发现错了但没备份」划算得多
#   - 以容器日志里的启动成功字样作为判据，而不是「cp 没报错就当成功」——
#     启动期有 schema drift 校验与多道 seed，cp 成功但代码有问题时正是靠这步暴露
#   - 三个目录逐一 cp 且逐个校验存在，避免「少灌一个」的静默失败

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
CONTAINER="${QA_BACKEND_CONTAINER:-qa-backend}"
SNAPSHOT_ROOT="$REPO_ROOT/backups/container"
STARTUP_TIMEOUT_SECONDS=90

# 必须一起灌的目录（宿主相对 backend/ → 容器内绝对路径）
SYNC_DIRS=("app:/app/app" "scripts:/app/scripts" "alembic:/app/alembic")

info()  { echo "[deploy] $*"; }
warn()  { echo "[deploy] ⚠️  $*" >&2; }
die()   { echo "[deploy] ❌ $*" >&2; exit 1; }

usage() {
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ── 参数 ────────────────────────────────────────────────────────────
DO_RESTART=1
DO_ROLLBACK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-restart) DO_RESTART=0 ;;
        --rollback)   DO_ROLLBACK=1 ;;
        -h|--help)    usage 0 ;;
        *)            die "未知参数：$1（-h 看用法）" ;;
    esac
    shift
done

# ── 前置检查 ────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"

CONTAINER_STATE="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)"
[[ -n "$CONTAINER_STATE" ]] || die "容器 $CONTAINER 不存在。先 docker compose up -d backend"
[[ "$CONTAINER_STATE" == "running" ]] || die "容器 $CONTAINER 状态是 $CONTAINER_STATE，不是 running"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SNAPSHOT_DIR="$SNAPSHOT_ROOT/$TIMESTAMP"

# ── 回滚模式 ────────────────────────────────────────────────────────
if [[ "$DO_ROLLBACK" == "1" ]]; then
    LATEST="$(ls -1d "$SNAPSHOT_ROOT"/*/ 2>/dev/null | sort | tail -1 || true)"
    [[ -n "$LATEST" ]] || die "没有可用快照（$SNAPSHOT_ROOT 为空）"
    LATEST="${LATEST%/}"
    info "回滚自快照：${LATEST##*/}"
    for entry in "${SYNC_DIRS[@]}"; do
        src="${entry%%:*}"
        dst="${entry##*:}"
        [[ -d "$LATEST/$src" ]] || die "快照缺少 $src/，无法回滚"
        docker cp "$LATEST/$src/." "$CONTAINER:$dst/" \
            || die "回滚 $src 失败"
    done
    info "回滚完成。重启容器使其生效：docker restart $CONTAINER"
    exit 0
fi

# ── 快照（先于覆盖）─────────────────────────────────────────────────
mkdir -p "$SNAPSHOT_DIR"
info "快照容器内现状 → $SNAPSHOT_DIR"
for entry in "${SYNC_DIRS[@]}"; do
    src="${entry%%:*}"
    dst="${entry##*:}"
    mkdir -p "$SNAPSHOT_DIR/$src"
    docker cp "$CONTAINER:$dst/." "$SNAPSHOT_DIR/$src/" \
        || warn "快照 $dst 失败（继续；但这部分将无法回滚）"
done

# ── 同步 ────────────────────────────────────────────────────────────
for entry in "${SYNC_DIRS[@]}"; do
    src="${entry%%:*}"
    dst="${entry##*:}"
    src_dir="$BACKEND_DIR/$src"
    [[ -d "$src_dir" ]] || die "宿主目录不存在：$src_dir"
    # 尾斜杠 `/.` 是硬要求：写成 `backend/app` 会嵌套成 /app/app/app
    info "cp $src/. → $CONTAINER:$dst/"
    docker cp "$src_dir/." "$CONTAINER:$dst/" || die "cp $src 失败"
done

# ── 校验：三处都到位 ────────────────────────────────────────────────
for entry in "${SYNC_DIRS[@]}"; do
    src="${entry%%:*}"
    dst="${entry##*:}"
    docker exec "$CONTAINER" test -d "$dst" \
        || die "容器内 $dst 不存在 —— 灌入未生效"
done
docker exec "$CONTAINER" test -f /app/app/main.py \
    || die "容器内 /app/app/main.py 不存在 —— app/ 被灌坏了，请 --rollback"

if [[ "$DO_RESTART" == "0" ]]; then
    warn "已灌代码但未重启（--no-restart）。重启后才生效：docker restart $CONTAINER"
    exit 0
fi

# ── 重启 + 等启动完成 ───────────────────────────────────────────────
info "重启 $CONTAINER ..."
docker restart "$CONTAINER" >/dev/null

info "等待启动（最多 ${STARTUP_TIMEOUT_SECONDS}s）..."
elapsed=0
while (( elapsed < STARTUP_TIMEOUT_SECONDS )); do
    logs="$(docker logs "$CONTAINER" 2>&1 | tail -40 || true)"
    if grep -q "Application startup complete." <<<"$logs"; then
        info "✅ 启动成功"
        echo
        warn "docker cp 是临时手段：容器重建即回退到镜像版本。"
        warn "网络恢复后请固化： docker compose build backend && docker compose up -d backend"
        exit 0
    fi
    if grep -qE "RuntimeError|Traceback|Application startup failed" <<<"$logs"; then
        echo "$logs" | tail -30 >&2
        die "启动失败。回滚： ./scripts/deploy_backend.sh --rollback && docker restart $CONTAINER"
    fi
    sleep 3
    elapsed=$(( elapsed + 3 ))
done

docker logs "$CONTAINER" 2>&1 | tail -30 >&2
die "等待启动超时（${STARTUP_TIMEOUT_SECONDS}s）。回滚： ./scripts/deploy_backend.sh --rollback"
