#!/usr/bin/env bash
# scripts/backup_neo4j.sh - Neo4j 本体图自动备份
#
# 行为：
#   1. 临时停止 qa-neo4j 容器（neo4j-admin dump 要求数据库 NOT running）
#   2. 用 temp 容器（neo4j:5.23-community）挂载同名 volume 跑 neo4j-admin database dump
#   3. 输出 backups/neo4j/neo4j_YYYY-MM-DD_HHMM.dump
#   4. 重启 qa-neo4j
#   5. 删除 14 天前的 .dump 文件（保留窗口 14 天）
#   6. 全程写日志到 backups/neo4j/backup.log
#
# 设计要点：
#   - **必须停服** dump：neo4j-admin database dump 强制要求 DB not in use
#   - **temp 容器挂同 volume**：避免把 neo4j 镜像整套装到 qa-neo4j；只 dump 不启动 server
#   - **停服窗口 ~30s**：开发环境可接受；生产建议挂从节点 / 走 HA
#   - **失败必重启**：用 trap 兜底，dump 失败 / 脚本异常退出都重启 Neo4j
#   - **--overwrite-destination=true**：同时间戳重复跑会覆盖
#   - **错误可见**：dump 失败时 exit non-zero + 日志末尾告警；cron 会捕获到
#   - **锁定文件**：避免并发跑（多副本 / 用户手抖）
#
# 用法：
#   ./scripts/backup_neo4j.sh                   # 默认备份（14 天保留）
#   ./scripts/backup_neo4j.sh --keep 7          # 自定义保留天数
#   ./scripts/backup_neo4j.sh --container NAME  # 自定义容器名（默认 qa-neo4j）
#   ./scripts/backup_neo4j.sh --db NAME         # 自定义数据库名（默认 neo4j）
#   ./scripts/backup_neo4j.sh --image IMAGE     # 自定义镜像（默认 neo4j:5.23-community）
#   ./scripts/backup_neo4j.sh --skip-stop       # 跳过停服（仅供已在外部停服的高级用户）

set -euo pipefail

# ---- 解析参数 ----
KEEP_DAYS=14
CONTAINER="qa-neo4j"
DB_NAME="neo4j"
VOLUME_NAME="qa-system_neo4j_data"
IMAGE="neo4j:5.23-community"
SKIP_STOP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)   KEEP_DAYS="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --db)     DB_NAME="$2"; shift 2 ;;
    --volume) VOLUME_NAME="$2"; shift 2 ;;
    --image)  IMAGE="$2"; shift 2 ;;
    --skip-stop) SKIP_STOP=1; shift 1 ;;
    -h|--help)
      grep '^# ' "$0" | sed 's/^# //'
      exit 0 ;;
    *)
      echo "[backup_neo4j] 未知参数: $1" >&2
      exit 2 ;;
  esac
done

# ---- 路径 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/backups/neo4j"
LOG_FILE="$BACKUP_DIR/backup.log"
LOCK_FILE="$BACKUP_DIR/.backup.lock"

mkdir -p "$BACKUP_DIR"

# ---- 日志 ----
log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$msg" | tee -a "$LOG_FILE"
}

# ---- 锁定 ----
cleanup_lock() { rm -f "$LOCK_FILE"; }
trap cleanup_lock EXIT
if [[ -e "$LOCK_FILE" ]]; then
  log "ERROR: 检测到另一备份进程在跑（lock=$LOCK_FILE），退出"
  exit 1
fi
touch "$LOCK_FILE"

# ---- 前置检查 ----
if ! command -v docker >/dev/null 2>&1; then
  log "ERROR: docker 命令不存在"
  exit 1
fi
if ! docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "ERROR: 容器 $CONTAINER 不存在（请确认 docker-compose.yml 服务名）"
  exit 1
fi
if ! docker volume ls --format '{{.Name}}' | grep -qx "$VOLUME_NAME"; then
  log "ERROR: volume $VOLUME_NAME 不存在（请确认 docker-compose.yml volume 名）"
  exit 1
fi

# ---- 状态追踪：保证失败时重启 Neo4j ----
WAS_RUNNING=0
restart_neo4j() {
  if [[ "$WAS_RUNNING" -eq 1 ]]; then
    log "确保 qa-neo4j 容器恢复运行"
    docker start "$CONTAINER" >/dev/null 2>&1 || log "WARN: 启动 $CONTAINER 失败，需手动处理"
  fi
}
trap restart_neo4j EXIT

# ---- 备份 ----
TIMESTAMP="$(date '+%Y-%m-%d_%H%M')"
DUMP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.dump"

log "开始备份 db=$DB_NAME container=$CONTAINER volume=$VOLUME_NAME keep=${KEEP_DAYS}d → $DUMP_FILE"

# 1) 停服（若容器原本在跑）
if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]]; then
  WAS_RUNNING=1
  log "停止 $CONTAINER 以便 dump（约 5~10s）"
  docker stop "$CONTAINER" >/dev/null 2>>"$LOG_FILE"
else
  log "容器 $CONTAINER 已未运行，跳过 stop"
fi

# 2) temp 容器挂同 volume 跑 neo4j-admin database dump
if docker run --rm \
     -v "${VOLUME_NAME}:/data" \
     -v "${BACKUP_DIR}:/backup" \
     "$IMAGE" \
     neo4j-admin database dump "$DB_NAME" \
       --to-path=/backup \
       --overwrite-destination=true \
     >>"$LOG_FILE" 2>&1; then
  # neo4j-admin 输出固定文件名 = <db_name>.dump，rename 为时间戳
  GENERATED="$BACKUP_DIR/${DB_NAME}.dump"
  if [[ -f "$GENERATED" ]]; then
    mv "$GENERATED" "$DUMP_FILE"
    SIZE_AFTER=$(stat -f%z "$DUMP_FILE" 2>/dev/null || stat -c%s "$DUMP_FILE" 2>/dev/null || echo 0)
    SIZE_HUMAN=$(numfmt --to=iec --suffix=B "$SIZE_AFTER" 2>/dev/null || echo "${SIZE_AFTER}B")
    log "备份成功 size=$SIZE_HUMAN path=$DUMP_FILE"
  else
    log "ERROR: dump 报告成功但未生成文件 $GENERATED"
    exit 1
  fi
else
  log "ERROR: neo4j-admin database dump 失败，详见上方日志"
  exit 1
fi

# 3) 重启 Neo4j（在 trap 之前显式启，避免锁占用到下一次运行）
if [[ "$WAS_RUNNING" -eq 1 ]]; then
  log "启动 $CONTAINER"
  docker start "$CONTAINER" >/dev/null 2>>"$LOG_FILE" || log "WARN: 启动失败，trap 会重试"
  WAS_RUNNING=0  # trap 跳过
fi

# 4) 清理过期
DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${DB_NAME}_*.dump" -mtime +"$KEEP_DAYS" -print -delete | wc -l | tr -d ' ')
log "清理完成：删除 $DELETED 个超过 ${KEEP_DAYS} 天的备份"

# 5) 列出当前快照
COUNT=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${DB_NAME}_*.dump" | wc -l | tr -d ' ')
log "当前保留快照数: $COUNT"
log "---"
