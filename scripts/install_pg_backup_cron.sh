#!/usr/bin/env bash
# scripts/install_pg_backup_cron.sh - 安装/校验 PostgreSQL 备份 cron
#
# 行为：
#   1. 检测用户 crontab 中是否已有 backup_pg.sh 任务
#   2. 没有则注入：0 8 * * *  每天 8:00 跑 backup_pg.sh（输出重定向到 backup.log）
#   3. 有则仅打印已存在（幂等，可重复执行）
#
# 用法：
#   ./scripts/install_pg_backup_cron.sh                    # 默认 8:00，3 天保留
#   ./scripts/install_pg_backup_cron.sh --time "0 8 * * *" # 自定义时间
#   ./scripts/install_pg_backup_cron.sh --keep 7           # 自定义保留天数（写入脚本默认）
#   ./scripts/install_pg_backup_cron.sh --uninstall        # 移除已装的 cron 行
#
# 注：
#   - 走用户 crontab（crontab -l/-r），不需要 sudo
#   - macOS 需 System Settings → General → Login Items 允许 cron；或登录 SSH 触发 launchd
#   - Docker exec 需要 cron 进程能访问 docker socket；用户级 crontab 默认有 ~/.docker 权限
#     （Docker Desktop macOS 已把 /var/run/docker.sock 暴露给用户进程）

set -euo pipefail

CRON_TIME="0 8 * * *"
MARKER="# qa-system-pg-backup"
UNINSTALL=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --time)        CRON_TIME="$2"; shift 2 ;;
    --keep)        KEEP_DAYS="$2"; shift 2 ;;
    --uninstall)   UNINSTALL=true; shift ;;
    -h|--help)
      grep '^# ' "$0" | sed 's/^# //'
      exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_SCRIPT="$REPO_ROOT/scripts/backup_pg.sh"

if [[ ! -x "$BACKUP_SCRIPT" ]]; then
  echo "[install_cron] ERROR: $BACKUP_SCRIPT 不可执行，请先 chmod +x" >&2
  exit 1
fi

EXISTING=$(crontab -l 2>/dev/null || true)

# 卸载模式
if $UNINSTALL; then
  if echo "$EXISTING" | grep -qF "$MARKER"; then
    NEW=$(echo "$EXISTING" | grep -vF "$MARKER" | grep -v "backup_pg.sh" || true)
    echo "$NEW" | crontab -
    echo "[install_cron] 已卸载 backup_pg cron 任务"
  else
    echo "[install_cron] 未检测到 backup_pg cron 任务，无需卸载"
  fi
  exit 0
fi

# 已存在则跳过（幂等）
if echo "$EXISTING" | grep -qF "$MARKER"; then
  echo "[install_cron] 检测到已存在的 backup_pg cron 任务，跳过安装"
  echo "$EXISTING" | grep -F "$MARKER"
  exit 0
fi

# 安装：拼接新行
NEW_LINE="$CRON_TIME /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/scripts/backup_pg.sh >> /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backups/pg/backup.log 2>&1 $MARKER"
NEW_CRON="${EXISTING}${NEW_LINE}
"

echo "$NEW_CRON" | crontab -
echo "[install_cron] 安装成功：$CRON_TIME 跑 backup_pg.sh"
crontab -l | grep -F "$MARKER"
