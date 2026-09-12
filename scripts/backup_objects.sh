#!/usr/bin/env bash
# 备份 MinIO 源文件卷（qa-knowledge-sources）。
#
# ⚠️ 与 backup_pg.sh 一样是**手动**脚本：本机 PG 的备份 cron 已确认静默失效
# （launchd 契约断裂），用户 2026-09-12 决定维持手动。此缺口记录在
# Harness/changes/feat-wiki-provenance/summary.md 第 10 段第 1 条。
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(cd "$(dirname "$0")/.." && pwd)/backups/objects}"
STAMP="$(date +%Y-%m-%d_%H%M)"
mkdir -p "$BACKUP_DIR"

docker run --rm \
  -v qa-system_objects_data:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine:3.20 \
  tar czf "/backup/objects_data_${STAMP}.tar.gz" -C /data .

echo "已备份到 $BACKUP_DIR/objects_data_${STAMP}.tar.gz"
