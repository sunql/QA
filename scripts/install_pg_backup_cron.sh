#!/usr/bin/env bash
# scripts/install_pg_backup_cron.sh - 安装/校验 PostgreSQL 备份 cron
#
# 行为：
#   1. 检测用户 crontab 中已有 backup_pg.sh 任务数（marker 计数）
#   2. 没有则注入两行：0 8 * * * 与 0 20 * * *（早晚各一次，保留 14 天）
#   3. 已有一行则补齐缺失那一行（升级兼容：旧版只有 8:00 的用户会自动补 20:00）
#   4. 已有两行则跳过（幂等，可重复执行）
#
# 用法：
#   ./scripts/install_pg_backup_cron.sh                        # 默认 8:00 + 20:00，14 天保留
#   ./scripts/install_pg_backup_cron.sh --time "0 8 * * *"     # 自定义早晨时间
#   ./scripts/install_pg_backup_cron.sh --evening-time "0 20 * * *"  # 自定义晚间时间
#   ./scripts/install_pg_backup_cron.sh --keep 7               # 自定义保留天数（写入脚本默认）
#   ./scripts/install_pg_backup_cron.sh --uninstall            # 移除已装的所有 backup_pg cron 行
#
# 注：
#   - 走用户 crontab（crontab -l/-r），不需要 sudo
#   - macOS 需 System Settings → General → Login Items 允许 cron；或登录 SSH 触发 launchd
#   - Docker exec 需要 cron 进程能访问 docker socket；用户级 crontab 默认有 ~/.docker 权限
#     （Docker Desktop macOS 已把 /var/run/docker.sock 暴露给用户进程）
#   - 历史事故：2026-09-10 qa_metadata 被无声清空，仅靠 cron 备份恢复。详见 Harness 规则。

set -euo pipefail

MORNING_TIME="0 8 * * *"
EVENING_TIME="0 20 * * *"
MARKER="# qa-system-pg-backup"
UNINSTALL=false
SCRIPT_PATH="/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/scripts/backup_pg.sh"
LOG_PATH="/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backups/pg/backup.log"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --time)          MORNING_TIME="$2"; shift 2 ;;
    --evening-time)  EVENING_TIME="$2"; shift 2 ;;
    --keep)          KEEP_DAYS="$2"; shift 2 ;;
    --uninstall)     UNINSTALL=true; shift ;;
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

# 保证 EXISTING 以换行结尾（command substitution 会吃掉尾随换行，导致后续 append 拼接错位）
if [[ -n "$EXISTING" && "${EXISTING: -1}" != $'\n' ]]; then
  EXISTING="${EXISTING}
"
fi

# 卸载模式：grep -vF 一行清掉所有带 marker 的行
if $UNINSTALL; then
  if echo "$EXISTING" | grep -qF "$MARKER"; then
    NEW=$(echo "$EXISTING" | grep -vF "$MARKER" || true)
    echo "$NEW" | crontab -
    echo "[install_cron] 已卸载 backup_pg cron 任务（含所有 marker 行）"
  else
    echo "[install_cron] 未检测到 backup_pg cron 任务，无需卸载"
  fi
  exit 0
fi

# 计算 marker 行数（兼容升级场景）
# 用 grep -F + wc -l 而不是 grep -c，避免无匹配时 exit 1 触发 pipefail 提早退出
# （需临时关 pipefail，否则 grep 无匹配时整个管道返回非零）
set +o pipefail
MARKER_COUNT=$(echo "$EXISTING" | grep -F "$MARKER" | wc -l | tr -d ' ')
set -o pipefail

make_line() {
  local time="$1"
  echo "$time $SCRIPT_PATH >> $LOG_PATH 2>&1 $MARKER"
}

MORNING_LINE="$(make_line "$MORNING_TIME")"
EVENING_LINE="$(make_line "$EVENING_TIME")"

has_line() {
  local line="$1"
  echo "$EXISTING" | grep -qF "$line"
}

if [[ "$MARKER_COUNT" -ge 2 ]]; then
  echo "[install_cron] 已检测到 $MARKER_COUNT 行 backup_pg cron 任务，跳过安装（幂等）"
  echo "$EXISTING" | grep -F "$MARKER"
  exit 0
fi

NEW_CRON="$EXISTING"

if [[ "$MARKER_COUNT" -eq 1 ]]; then
  # 升级兼容：已有 1 行（morning 或 evening），补齐缺失的那一行
  if has_line "$MORNING_LINE"; then
    echo "[install_cron] 检测到 1 行 cron 任务（仅早晨），正在补齐晚间行"
    NEW_CRON="${NEW_CRON}${EVENING_LINE}
"
  elif has_line "$EVENING_LINE"; then
    echo "[install_cron] 检测到 1 行 cron 任务（仅晚间），正在补齐早晨行"
    NEW_CRON="${NEW_CRON}${MORNING_LINE}
"
  else
    # 不太可能：marker 存在但内容不匹配 — 清空重装（保留其他 cron）
    echo "[install_cron] WARNING: 检测到 1 行 cron 任务但内容不匹配，清除并重装"
    NEW_CRON=$(echo "$EXISTING" | grep -vF "$MARKER" || true)
    # 同样补回尾随换行
    if [[ -n "$NEW_CRON" && "${NEW_CRON: -1}" != $'\n' ]]; then
      NEW_CRON="${NEW_CRON}
"
    fi
    NEW_CRON="${NEW_CRON}${MORNING_LINE}
${EVENING_LINE}
"
  fi
else
  # 全新安装：两行
  echo "[install_cron] 未检测到 backup_pg cron 任务，正在安装早 + 晚两个时段"
  NEW_CRON="${NEW_CRON}${MORNING_LINE}
${EVENING_LINE}
"
fi

echo "$NEW_CRON" | crontab -
echo "[install_cron] 安装成功："
crontab -l | grep -F "$MARKER"
