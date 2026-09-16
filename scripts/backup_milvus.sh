#!/usr/bin/env bash
# 备份 Milvus 向量库三个卷：
#   - qa-system_milvus_minio  向量数据文件（bucket: a-bucket）
#   - qa-system_milvus_data   Milvus 主卷（含内嵌元数据 rdb_data / binlog 缓存）
#   - qa-system_milvus_etcd   外置 etcd 元数据（当前 4K，兜底备上）
#
# ⚠️ 与 backup_pg.sh / backup_objects.sh 一样是**手动**脚本：本机 cron 已确认
#    静默失效（launchd 契约断裂），用户 2026-09-12 决定维持手动。
#
# ⚠️ 热备语义：容器运行中直接 tar 数据卷，抓到的是某一瞬间的镜像，若备份时
#    正在写入（ingest/upsert）可能包含中间态。开发环境数据量小（~300M）可接受；
#    需要强一致快照时先 `docker compose stop milvus` 再备份。
#
# 恢复：解压对应 tar 到同名 docker volume（docker run --rm -v <vol>:/data -v $PWD:/backup alpine tar xzf ...）

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(cd "$(dirname "$0")/.." && pwd)/backups/milvus}"
STAMP="$(date +%Y-%m-%d_%H%M)"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

backup_volume() {
  local vol="$1" label="$2"
  echo "[$(date '+%F %T')] 备份卷 $vol → ${label}_${STAMP}.tar.gz"
  docker run --rm \
    -v "qa-system_${vol}":/data:ro \
    -v "$BACKUP_DIR":/backup \
    alpine:3.20 \
    tar czf "/backup/${label}_${STAMP}.tar.gz" -C /data .
}

backup_volume milvus_minio milvus_minio
backup_volume milvus_data milvus_data
backup_volume milvus_etcd milvus_etcd

# 清理超过 KEEP_DAYS 的旧备份
deleted=$(find "$BACKUP_DIR" -name "*.tar.gz" -mtime +"$KEEP_DAYS" -print -delete | wc -l | tr -d ' ')
echo "[$(date '+%F %T')] 清理完成：删除 $deleted 个超过 ${KEEP_DAYS} 天的备份"
ls -lh "$BACKUP_DIR" | grep "$STAMP"
