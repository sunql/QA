#!/usr/bin/env bash
# .claude/hooks/protect_volumes.sh - PreToolUse hook: 阻断 docker volume 销毁命令
#
# 背景：2026-09-10 qa-system_pg_data 被无声清空事故后设置的项目级 hook。
# 行为：
#   1. 从 stdin JSON 提取 Bash tool 的 command 字段
#   2. 检测 docker volume 销毁命令（含 -v / --volumes / --rmi all / -a 旗标）
#   3. 命中即 stderr 输出红框警告 + exit 2（Claude Code 协议 = 阻断）
#   4. 仅 project-scope 生效（其他项目目录不受影响）
#
# 旁路（仅紧急情况）：
#   QA_SYSTEM_VOLUME_GUARD_BYPASS=1 <command>   # 仍打印 WARNING 但 exit 0
#
# 退出码：
#   0 - 放行（安全命令 / 已旁路）
#   2 - 阻断（命中销毁命令 + 未旁路）

set -uo pipefail

# ---- 读取 stdin JSON ----
INPUT="$(cat)"

# 提取 command 字段值（用 grep -o + sed，不依赖 python/json 工具）
COMMAND="$(printf '%s' "$INPUT" \
  | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | head -1 \
  | sed 's/^"command"[[:space:]]*:[[:space:]]*"//; s/"$//')"

if [[ -z "${COMMAND:-}" ]]; then
  exit 0
fi

# ---- 旁路 ----
if [[ "${QA_SYSTEM_VOLUME_GUARD_BYPASS:-}" == "1" ]]; then
  printf '[protect_volumes] WARNING: Volume guard BYPASSED (QA_SYSTEM_VOLUME_GUARD_BYPASS=1)\n' >&2
  printf '[protect_volumes] 放行命令: %s\n' "$COMMAND" >&2
  exit 0
fi

# ---- 判定是否危险命令 ----
# step 1：基命令必须命中危险前缀
BASE_HIT=0
if printf '%s' "$COMMAND" | grep -qE '(^|[[:space:]]|;|&&|\|)(docker[[:space:]]+compose[[:space:]]+down|docker-compose[[:space:]]+down|docker[[:space:]]+volume[[:space:]]+rm|docker[[:space:]]+volume[[:space:]]+remove|docker[[:space:]]+system[[:space:]]+prune|docker[[:space:]]+rm)([[:space:]]|$)'; then
  BASE_HIT=1
fi

if [[ "$BASE_HIT" -eq 0 ]]; then
  exit 0
fi

# step 2：判断是否带销毁性旗标
DANGEROUS=0

# docker volume rm/remove 始终危险
if printf '%s' "$COMMAND" | grep -qE 'docker[[:space:]]+volume[[:space:]]+(rm|remove)([[:space:]]|$)'; then
  DANGEROUS=1
fi

# docker compose down / docker-compose down 需 -v / --volumes / --rmi all / -v\*
if printf '%s' "$COMMAND" | grep -qE 'docker(-compose|[[:space:]]+compose)[[:space:]]+down'; then
  if printf '%s' "$COMMAND" | grep -qE '(-v([[:space:]]|$|=)|--volumes|--rmi([[:space:]]+all|=all)|--rmi=all)'; then
    DANGEROUS=1
  fi
fi

# docker system prune 需 -a / --all / --volumes
if printf '%s' "$COMMAND" | grep -qE 'docker[[:space:]]+system[[:space:]]+prune'; then
  if printf '%s' "$COMMAND" | grep -qE '(-a([[:space:]]|$|=)|--all|--volumes)'; then
    DANGEROUS=1
  fi
fi

# docker rm 需 -v / --volumes（同时要非 docker rm -f 这种危险但非 volume 操作场景不命中）
if printf '%s' "$COMMAND" | grep -qE '(^|[[:space:]])docker[[:space:]]+rm[[:space:]]'; then
  if printf '%s' "$COMMAND" | grep -qE '(-v([[:space:]]|$|=)|--volumes)'; then
    DANGEROUS=1
  fi
fi

if [[ "$DANGEROUS" -eq 0 ]]; then
  exit 0
fi

# ---- 阻断 + 警告 ----
# 用 printf + ANSI 颜色，避免依赖 tput
RED='\033[1;31m'
YEL='\033[1;33m'
RST='\033[0m'

{
  printf '%b╔══════════════════════════════════════════════════════════════════════════════╗%b\n' "$RED" "$RST"
  printf '%b║  🔴  VOLUME GUARD: 检测到 Docker volume 销毁命令                              ║%b\n' "$RED" "$RST"
  printf '%b╠══════════════════════════════════════════════════════════════════════════════╣%b\n' "$RED" "$RST"
  printf '%b║%b                                                                              %b║%b\n' "$RED" "$RST" "$RED" "$RST"
  printf '%b║  即将执行的命令:%b\n' "$RED" "$RST"
  printf '%b║    %s%b\n' "$YEL" "$COMMAND" "$RST"
  printf '%b║%b                                                                              %b║%b\n' "$RED" "$RST" "$RED" "$RST"
  printf '%b║  此命令将销毁以下数据:%b\n' "$RED" "$RST"
  printf '%b║    • qa-system_pg_data          qa_metadata (42 表, ~35万 entity_mapping)%b\n' "$RED" "$RST"
  printf '%b║    • qa-system_neo4j_data       本体图谱%b\n' "$RED" "$RST"
  printf '%b║    • qa-system_milvus_etcd      Milvus 元数据%b\n' "$RED" "$RST"
  printf '%b║    • qa-system_milvus_minio     Milvus 存储%b\n' "$RED" "$RST"
  printf '%b║    • qa-system_milvus_data      Milvus 向量%b\n' "$RED" "$RST"
  printf '%b║    • qa-system_mysql_data       WMS demo 库%b\n' "$RED" "$RST"
  printf '%b║%b                                                                              %b║%b\n' "$RED" "$RST" "$RED" "$RST"
  printf '%b║  事故参考: 2026-09-10 qa_metadata 静默清空 → 30 分钟从备份恢复%b\n' "$YEL" "$RST"
  printf '%b║%b                                                                              %b║%b\n' "$RED" "$RST" "$RED" "$RST"
  printf '%b║  真的要执行，请设置旁路环境变量后重跑:%b\n' "$YEL" "$RST"
  printf '%b║    export QA_SYSTEM_VOLUME_GUARD_BYPASS=1%b\n' "$YEL" "$RST"
  printf '%b║    # <原命令>%b\n' "$YEL" "$RST"
  printf '%b║%b                                                                              %b║%b\n' "$RED" "$RST" "$RED" "$RST"
  printf '%b║  如非本意销毁，请按 Ctrl+C 取消当前操作%b\n' "$YEL" "$RST"
  printf '%b║%b                                                                              %b║%b\n' "$RED" "$RST" "$RED" "$RST"
  printf '%b╚══════════════════════════════════════════════════════════════════════════════╝%b\n' "$RED" "$RST"
} >&2

exit 2
