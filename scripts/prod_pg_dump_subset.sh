#!/usr/bin/env bash
# scripts/prod_pg_dump_subset.sh — 只读导出「本体 + 指标 + KPI 目录」子集
#
# 行为（**全程只读**，不写任何库）：
#   1. 从 scripts/pg_subset_tables.txt 读子集表清单（SSOT，与恢复脚本共用）
#   2. 在 PG 容器内跑 pg_dump --data-only（文本格式 ⇒ 本地 psql 可直接灌）
#   3. 产物：<out>/<label>_subset_<ts>.sql.gz + 同名 .manifest.txt（行数 + sha256）
#
# 用法（在生产宿主机执行；本地演练同样可用）：
#   ./scripts/prod_pg_dump_subset.sh
#   ./scripts/prod_pg_dump_subset.sh --out-dir /root/backup --label prod_20261114
#   ./scripts/prod_pg_dump_subset.sh --container qa-postgres --db qa_metadata --user qa_user
#
# 设计要点：
#   - 容器内取 POSTGRES_USER / POSTGRES_DB（不硬编码账号，也不在日志里出现口令）
#   - --data-only：**不带 DDL**，本地 schema 保持本地自己的版本（本地 alembic 0143 不被动）
#   - 文本格式 + gzip：跨版本可直接 psql 灌入；自带 COPY 与 owned 序列的 setval
#   - manifest 记录每张表行数 ⇒ 恢复后逐表对账
#   - 只导出目标子集：**不落业务数据**（业务数据在 Oracle，不在本库）

set -euo pipefail

CONTAINER="qa-postgres"
DB=""
DB_USER=""
OUT_DIR=""
LABEL="prod"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TABLES_FILE="$SCRIPT_DIR/pg_subset_tables.txt"

usage() {
  grep '^# ' "$0" | sed 's/^# //'
  cat <<'EOF'

参数：
  --out-dir DIR     产物目录（默认 $PWD/backups/pg）
  --label NAME      产物名前缀（默认 prod）
  --container NAME  PG 容器名（默认 qa-postgres）
  --db NAME         数据库名（默认取容器内 POSTGRES_DB）
  --user NAME       数据库用户（默认取容器内 POSTGRES_USER）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir)   OUT_DIR="$2"; shift 2 ;;
    --label)     LABEL="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --db)        DB="$2"; shift 2 ;;
    --user)      DB_USER="$2"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "[dump] 未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '[dump %s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '[dump] ERROR: %s\n' "$*" >&2; exit 1; }

# sha256 工具：CentOS 7 只有 sha256sum，macOS 只有 shasum -a 256
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "找不到 sha256sum / shasum"
  fi
}

# ---- 前置检查 ----
command -v docker >/dev/null 2>&1 || die "docker 命令不存在"
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || die "容器 $CONTAINER 未运行"
[[ -f "$TABLES_FILE" ]] || die "找不到表清单 $TABLES_FILE"

# 表清单：去注释、去空行、去重，保持文件顺序（用 while read 兼容 macOS bash 3.2）
TABLES=()
while IFS= read -r _line; do
  [[ -n "$_line" ]] && TABLES+=("$_line")
done < <(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$TABLES_FILE" | awk '!seen[$0]++')
TABLE_COUNT=${#TABLES[@]}
[[ $TABLE_COUNT -gt 0 ]] || die "表清单为空"

DB="${DB:-$(docker exec "$CONTAINER" printenv POSTGRES_DB)}"
DB_USER="${DB_USER:-$(docker exec "$CONTAINER" printenv POSTGRES_USER)}"
[[ -n "$DB" && -n "$DB_USER" ]] || die "无法确定数据库名/用户名（容器 env 缺失，请用 --db/--user 指定）"

OUT_DIR="${OUT_DIR:-$PWD/backups/pg}"
mkdir -p "$OUT_DIR"

# 连通性 + 版本（只读）
PG_VERSION="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB" -tAc 'SHOW server_version;' 2>/dev/null | tr -d '[:space:]')"
[[ -n "$PG_VERSION" ]] || die "无法连接 ${DB}（用户 ${DB_USER}）"
log "容器=$CONTAINER 库=$DB 用户=$DB_USER 服务端版本=$PG_VERSION 表数=$TABLE_COUNT"

TS="$(date '+%Y%m%d_%H%M')"
BASE="${LABEL}_subset_${TS}"
SQL_FILE="${OUT_DIR}/${BASE}.sql"
GZ_FILE="${SQL_FILE}.gz"
MANIFEST="${OUT_DIR}/${BASE}.manifest.txt"

# ---- 逐表行数（生产侧快照，恢复后对账用） ----
{
  echo "# 生产侧子集快照（恢复后逐表对账）"
  echo "# 生成时间: $(date '+%Y-%m-%d %H:%M:%S')  库: $DB  容器: $CONTAINER"
  echo "# 表顺序与 scripts/pg_subset_tables.txt 一致"
} > "$MANIFEST"

for t in "${TABLES[@]}"; do
  CNT="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB" -tAc "SELECT count(*) FROM \"$t\";" 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$CNT" ]] || die "统计 $t 行数失败"
  printf '%s\t%s\n' "$t" "$CNT" >> "$MANIFEST"
done
log "行数快照 → $MANIFEST"

# ---- 导出（只读） ----
DUMP_ARGS=(--data-only --no-owner --no-privileges)
for t in "${TABLES[@]}"; do
  DUMP_ARGS+=(-t "public.${t}")
done

log "开始 pg_dump --data-only（$TABLE_COUNT 张表）…"
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB" "${DUMP_ARGS[@]}" > "$SQL_FILE"
[[ -s "$SQL_FILE" ]] || die "dump 产物为空"
gzip -9 -f "$SQL_FILE"

SHA="$(sha256_of "$GZ_FILE")"
SIZE="$(du -h "$GZ_FILE" | cut -f1)"
{
  echo "file: ${BASE}.sql.gz"
  echo "sha256: $SHA"
  echo "size: $SIZE"
  echo "source_container: $CONTAINER"
  echo "source_db: $DB"
  echo "pg_version: $PG_VERSION"
  echo "tables: $TABLE_COUNT"
} >> "$MANIFEST"

log "导出完成：${GZ_FILE}（${SIZE}, sha256=${SHA}）"
log "传输到本地后必须先比对 sha256，再执行恢复脚本"
cat "$MANIFEST"
