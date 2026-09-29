#!/usr/bin/env bash
# scripts/backup_pg.sh - PostgreSQL 自动备份（custom format）
#
# 行为：
#   1. 用 docker exec qa-postgres pg_dump -Fc 备份 qa_metadata 库
#   2. 输出 backups/pg/qa_metadata_YYYY-MM-DD_HHMM.dump
#   3. 删除 14 天前的 .dump 文件（保留窗口 14 天）
#   4. 全程写日志到 backups/pg/backup.log
#
# 设计要点：
#   - Custom format (-Fc)：压缩二进制，支持 pg_restore 选择性恢复
#   - 容器内 pg_dump：避免宿主 PG 版本差异；走项目标准 docker exec 路径
#   - PGPASSWORD 注入：docker exec 容器内 psql/pg_dump 默认 peer 认证需 qa_user
#   - 错误可见：pg_dump 失败时 exit non-zero + 日志末尾告警；cron 会捕获到
#   - 锁定文件：避免并发跑（多副本 / 用户手抖）
#
# 用法：
#   ./scripts/backup_pg.sh                   # 默认备份（14 天保留）
#   ./scripts/backup_pg.sh --keep 7          # 自定义保留天数
#   ./scripts/backup_pg.sh --container NAME  # 自定义容器名（默认 qa-postgres）
#   ./scripts/backup_pg.sh --db NAME         # 自定义数据库名（默认 qa_metadata）

set -euo pipefail

# ---- 解析参数 ----
KEEP_DAYS=14
CONTAINER="qa-postgres"
DB_NAME="qa_metadata"
DB_USER="qa_user"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)   KEEP_DAYS="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --db)     DB_NAME="$2"; shift 2 ;;
    --user)   DB_USER="$2"; shift 2 ;;
    -h|--help)
      grep '^# ' "$0" | sed 's/^# //'
      exit 0 ;;
    *)
      echo "[backup_pg] 未知参数: $1" >&2
      exit 2 ;;
  esac
done

# ---- 路径 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/backups/pg"
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
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "ERROR: 容器 $CONTAINER 未运行"
  exit 1
fi

# ---- 备份 ----
TIMESTAMP="$(date '+%Y-%m-%d_%H%M')"
DUMP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.dump"
SIZE_BEFORE=0
SIZE_AFTER=0

log "开始备份 db=$DB_NAME container=$CONTAINER keep=${KEEP_DAYS}d → $DUMP_FILE"

# docker exec 容器内用 qa_user（peer auth 需 pg_hba.conf 允许 local socket + 同名 OS user；
# pg_dump 默认走容器内 postgres user，但 PGPASSWORD + -U 显式更稳；这里保留 peer 简化）
if docker exec "$CONTAINER" pg_dump \
     -U "$DB_USER" \
     -d "$DB_NAME" \
     -Fc \
     --no-owner \
     --no-privileges \
     > "$DUMP_FILE" 2>>"$LOG_FILE"; then
  SIZE_AFTER=$(stat -f%z "$DUMP_FILE" 2>/dev/null || stat -c%s "$DUMP_FILE" 2>/dev/null || echo 0)
  SIZE_BEFORE_HUMAN=$(numfmt --to=iec --suffix=B "$SIZE_AFTER" 2>/dev/null || echo "${SIZE_AFTER}B")
  log "备份成功 size=$SIZE_BEFORE_HUMAN path=$DUMP_FILE"
else
  log "ERROR: pg_dump 失败，详见上方 stderr"
  rm -f "$DUMP_FILE"
  exit 1
fi

# ---- 清理过期 ----
DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${DB_NAME}_*.dump" -mtime +"$KEEP_DAYS" -print -delete | wc -l | tr -d ' ')
log "清理完成：删除 $DELETED 个超过 ${KEEP_DAYS} 天的备份"

# ---- 列出当前快照 ----
COUNT=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${DB_NAME}_*.dump" | wc -l | tr -d ' ')
log "当前保留快照数: $COUNT"
log "---"
