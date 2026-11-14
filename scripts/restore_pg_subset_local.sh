#!/usr/bin/env bash
# scripts/restore_pg_subset_local.sh — 把「本体 + 指标 + KPI 目录」子集恢复到**本地 Docker PG**
#
# 行为：
#   1. 守卫：目标库必须匹配 ^qa_metadata(_test.*)?$；**只经 docker exec 操作本地容器**
#      ⇒ 本脚本在结构上无法写生产库（生产库在 192.168.205.82，本机不可达）
#   2. 回滚锚点：先 pg_dump 当前子集到 backups/pg/local_subset_pre_restore_<ts>.dump
#   3. 单事务：SET session_replication_role=replica → 按子→父 DELETE 本地行 → 灌入生产行 → COMMIT
#   4. 校验：逐表行数对账（与生产 manifest 比） + 孤儿审计 + 序列校验
#
# 用法：
#   ./scripts/restore_pg_subset_local.sh --dump backups/pg/prod_subset_20261114_1015.sql.gz --dry-run
#   ./scripts/restore_pg_subset_local.sh --dump <file> --target-db qa_metadata --apply
#
# 参数：
#   --dump FILE        生产侧导出的 .sql.gz（必填）
#   --target-db NAME   目标库（默认 qa_metadata）
#   --container NAME   PG 容器名（默认 qa-postgres）
#   --user NAME        数据库用户（默认取容器内 POSTGRES_USER）
#   --manifest FILE    生产 manifest（默认与 dump 同名 .manifest.txt；用于行数对账）
#   --apply            真执行（默认只打印计划；--dry-run 显式声明不执行）
#   --keep-backend     恢复期间不停止后端容器（默认会停 qa-backend，恢复后拉起）
#   --no-backup        跳过本地回滚锚点（不建议）

set -euo pipefail

CONTAINER="qa-postgres"
TARGET_DB="qa_metadata"
DB_USER=""
DUMP_FILE=""
ROLLBACK_FROM=""
MANIFEST=""
APPLY=0
KEEP_BACKEND=0
NO_BACKUP=0
BACKUP_DIR="$PWD/backups/pg"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TABLES_FILE="$SCRIPT_DIR/pg_subset_tables.txt"

usage() {
  grep '^# ' "$0" | sed 's/^# //'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump)         DUMP_FILE="$2"; shift 2 ;;
    --rollback-from) ROLLBACK_FROM="$2"; shift 2 ;;
    --target-db)    TARGET_DB="$2"; shift 2 ;;
    --container)    CONTAINER="$2"; shift 2 ;;
    --user)         DB_USER="$2"; shift 2 ;;
    --manifest)     MANIFEST="$2"; shift 2 ;;
    --backup-dir)   BACKUP_DIR="$2"; shift 2 ;;
    --apply)        APPLY=1; shift ;;
    --dry-run)      APPLY=0; shift ;;
    --keep-backend) KEEP_BACKEND=1; shift ;;
    --no-backup)    NO_BACKUP=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "[restore] 未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '[restore %s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }

# ---- 守卫 1：目标库名白名单 ----
[[ "$TARGET_DB" =~ ^qa_metadata(_test.*)?$ ]] \
  || die "拒绝执行：目标库 '$TARGET_DB' 不在白名单（只允许 qa_metadata / qa_metadata_test*）"

# ---- 守卫 2：容器存在 ----
command -v docker >/dev/null 2>&1 || die "docker 命令不存在"
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || die "容器 $CONTAINER 未运行"

DB_USER="${DB_USER:-$(docker exec "$CONTAINER" printenv POSTGRES_USER)}"
[[ -n "$DB_USER" ]] || die "无法确定数据库用户"

# ---- 守卫 3：目标库必须存在，且 schema 与清单表齐全 ----
EXISTS="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='$TARGET_DB';" 2>/dev/null | tr -d '[:space:]')"
[[ "$EXISTS" == "1" ]] || die "目标库 $TARGET_DB 不存在（先 createdb，再重跑）"

TABLES=()
while IFS= read -r _line; do
  [[ -n "$_line" ]] && TABLES+=("$_line")
done < <(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$TABLES_FILE" | awk '!seen[$0]++')
TABLE_COUNT=${#TABLES[@]}
[[ $TABLE_COUNT -gt 0 ]] || die "表清单为空（${TABLES_FILE}）"

MISSING=""
for t in "${TABLES[@]}"; do
  GOT="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$TARGET_DB" -tAc \
    "SELECT 1 FROM information_schema.tables WHERE table_name='$t';" 2>/dev/null | tr -d '[:space:]')"
  [[ "$GOT" == "1" ]] || MISSING="${MISSING} ${t}"
done
[[ -z "$MISSING" ]] || die "目标库缺表：${MISSING}（schema 版本不对，先对齐 alembic）"

# ---- 守卫 4：数据源文件存在（正向 dump 或回滚锚点，二选一） ----
if [[ -n "$ROLLBACK_FROM" && -n "$DUMP_FILE" ]]; then
  die "--dump 与 --rollback-from 只能二选一"
fi
if [[ -n "$ROLLBACK_FROM" ]]; then
  [[ -f "$ROLLBACK_FROM" ]] || die "找不到回滚锚点 $ROLLBACK_FROM"
  IS_ROLLBACK=1
else
  [[ -n "$DUMP_FILE" ]] || die "缺少 --dump（或用 --rollback-from 指定回滚锚点）"
  [[ -f "$DUMP_FILE" ]] || die "找不到 dump 文件 $DUMP_FILE"
  IS_ROLLBACK=0
fi

MANIFEST="${MANIFEST:-}"
if [[ -z "$MANIFEST" && $IS_ROLLBACK -eq 0 ]]; then
  # 导出脚本的产物命名：<base>.sql.gz + <base>.manifest.txt
  MANIFEST="${DUMP_FILE%.sql.gz}.manifest.txt"
  [[ -f "$MANIFEST" ]] || MANIFEST="${DUMP_FILE%.gz}.manifest.txt"
fi

log "目标库=${TARGET_DB}（白名单校验通过）容器=${CONTAINER} 用户=${DB_USER} 表数=${TABLE_COUNT}"
if [[ $IS_ROLLBACK -eq 1 ]]; then
  log "模式=回滚（源=本地锚点）源文件=${ROLLBACK_FROM}"
else
  log "模式=正向恢复 源文件=${DUMP_FILE}"
fi
[[ -f "$MANIFEST" ]] && log "manifest=${MANIFEST}" || log "manifest 缺失（跳过行数对账）"

if [[ $APPLY -eq 0 ]]; then
  log "=== DRY-RUN：以下为计划，未做任何改动 ==="
  echo "  1) 回滚锚点：pg_dump -Fc 子集 → $BACKUP_DIR/local_subset_pre_restore_<ts>.dump"
  echo "  2) 停后端容器 qa-backend（--keep-backend 可跳过）"
  echo "  3) 单事务：session_replication_role=replica → DELETE（子→父）→ 灌入源数据 → COMMIT"
  echo "  4) 校验：行数对账 + 孤儿审计 + 序列校验"
  echo "  5) 拉起后端"
  exit 0
fi

# ---- 回滚锚点 ----
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/local_subset_pre_restore_$(date '+%Y%m%d_%H%M%S').dump"
if [[ $NO_BACKUP -eq 0 ]]; then
  BK_ARGS=(--format=custom --data-only --no-owner --no-privileges)
  for t in "${TABLES[@]}"; do BK_ARGS+=(-t "public.${t}"); done
  docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$TARGET_DB" "${BK_ARGS[@]}" > "$BACKUP_FILE"
  [[ -s "$BACKUP_FILE" ]] || die "本地回滚锚点导出失败"
  log "回滚锚点：${BACKUP_FILE}（$(du -h "$BACKUP_FILE" | cut -f1)）"
else
  log "WARNING: 已跳过本地回滚锚点（--no-backup）"
fi

# ---- 停后端（避免恢复过程中读到中间态） ----
BACKEND_STOPPED=0
if [[ $KEEP_BACKEND -eq 0 ]] && docker ps --format '{{.Names}}' | grep -qx "qa-backend"; then
  docker stop qa-backend >/dev/null && BACKEND_STOPPED=1
  log "已停止 qa-backend"
fi

restore_backend() {
  if [[ $BACKEND_STOPPED -eq 1 ]]; then
    docker start qa-backend >/dev/null && log "已拉起 qa-backend"
  fi
}
trap restore_backend EXIT

# ---- 事务：DELETE（子→父）+ 灌入源数据 ----
# 源 SQL：正向 = gunzip 生产 dump；回滚 = pg_restore 把本地锚点转成 SQL 文本
emit_source_sql() {
  if [[ $IS_ROLLBACK -eq 1 ]]; then
    # pg_restore 不允许 -d 与 -f 同用：这里只要 SQL 文本，连接参数由后续 psql 承担
    cat "$ROLLBACK_FROM" \
      | docker exec -i "$CONTAINER" pg_restore --data-only --no-owner --no-privileges -f -
  else
    gunzip -c "$DUMP_FILE"
  fi
}

COMBINED="$(mktemp -t qa_subset_restore_XXXXXX.sql)"
{
  echo "BEGIN;"
  echo "SET session_replication_role = replica;"
  for t in "${TABLES[@]}"; do
    echo "DELETE FROM public.${t};"
  done
  emit_source_sql
  echo "COMMIT;"
} > "$COMBINED"

log "开始灌入（单事务，失败自动回滚）…"
if docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -o /dev/null -U "$DB_USER" -d "$TARGET_DB" < "$COMBINED"; then
  log "灌入成功"
else
  die "灌入失败（事务已回滚，本地数据未变）；请检查 dump 是否与本地 schema 匹配"
fi
rm -f "$COMBINED"

# ---- 校验 ----
echo
log "=== 校验 1：逐表行数（本地 vs 生产 manifest）==="
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$TARGET_DB" -tA <<'SQL'
SELECT t || chr(9) || c FROM (
  SELECT 'ontology_metric_source' t, count(*) c FROM ontology_metric_source
  UNION ALL SELECT 'kpi_catalog_history', count(*) FROM kpi_catalog_history
  UNION ALL SELECT 'ontology_join', count(*) FROM ontology_join
  UNION ALL SELECT 'ontology_relation', count(*) FROM ontology_relation
  UNION ALL SELECT 'ontology_property', count(*) FROM ontology_property
  UNION ALL SELECT 'class_domain_mapping', count(*) FROM class_domain_mapping
  UNION ALL SELECT 'coverage_cell', count(*) FROM coverage_cell
  UNION ALL SELECT 'business_object_domain', count(*) FROM business_object_domain
  UNION ALL SELECT 'kpi_catalog', count(*) FROM kpi_catalog
  UNION ALL SELECT 'ontology_metric', count(*) FROM ontology_metric
  UNION ALL SELECT 'business_object', count(*) FROM business_object
  UNION ALL SELECT 'ontology_class', count(*) FROM ontology_class
  UNION ALL SELECT 'id_mapping', count(*) FROM id_mapping
) s ORDER BY 1;
SQL

if [[ -f "$MANIFEST" ]]; then
  echo
  log "=== 校验 1b：与生产 manifest 逐表对账（diff 非空即不一致）==="
  LOCAL_COUNTS="$(docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$TARGET_DB" -tA <<'SQL'
SELECT t || chr(9) || c FROM (
  SELECT 'ontology_metric_source' t, count(*) c FROM ontology_metric_source
  UNION ALL SELECT 'kpi_catalog_history', count(*) FROM kpi_catalog_history
  UNION ALL SELECT 'ontology_join', count(*) FROM ontology_join
  UNION ALL SELECT 'ontology_relation', count(*) FROM ontology_relation
  UNION ALL SELECT 'ontology_property', count(*) FROM ontology_property
  UNION ALL SELECT 'class_domain_mapping', count(*) FROM class_domain_mapping
  UNION ALL SELECT 'coverage_cell', count(*) FROM coverage_cell
  UNION ALL SELECT 'business_object_domain', count(*) FROM business_object_domain
  UNION ALL SELECT 'kpi_catalog', count(*) FROM kpi_catalog
  UNION ALL SELECT 'ontology_metric', count(*) FROM ontology_metric
  UNION ALL SELECT 'business_object', count(*) FROM business_object
  UNION ALL SELECT 'ontology_class', count(*) FROM ontology_class
  UNION ALL SELECT 'id_mapping', count(*) FROM id_mapping
) s ORDER BY 1;
SQL
)"
  PROD_COUNTS="$(grep -vE '^(#|file:|sha256:|size:|source_|pg_version:|tables:)' "$MANIFEST" | awk -F'\t' '$1!="" {print $1"\t"$2}' | sort)"
  DIFF="$(printf '%s\n' "$LOCAL_COUNTS" | sort > /tmp/.qa_local_counts.$$ ; printf '%s\n' "$PROD_COUNTS" | sort > /tmp/.qa_prod_counts.$$ ; diff /tmp/.qa_local_counts.$$ /tmp/.qa_prod_counts.$$ || true; rm -f /tmp/.qa_local_counts.$$ /tmp/.qa_prod_counts.$$)"
  if [[ -z "$DIFF" ]]; then
    log "行数对账：✅ 全部一致"
  else
    log "行数对账：⚠️ 不一致（本地 vs 生产）"
    printf '%s\n' "$DIFF"
  fi
fi

echo
log "=== 校验 2：孤儿审计（子集内部 + 指向未恢复表）==="
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$TARGET_DB" -tA <<'SQL'
SELECT 'orphan_kpi_metric' k, count(*) v FROM kpi_catalog k
  WHERE k.metric_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ontology_metric m WHERE m.id = k.metric_id)
UNION ALL SELECT 'orphan_kpi_datasource', count(*) FROM kpi_catalog k
  WHERE k.datasource_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM data_source d WHERE d.id = k.datasource_id)
UNION ALL SELECT 'orphan_metric_class', count(*) FROM ontology_metric m
  WHERE m.target_class_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ontology_class c WHERE c.id = m.target_class_id)
UNION ALL SELECT 'orphan_property_class', count(*) FROM ontology_property p
  WHERE NOT EXISTS (SELECT 1 FROM ontology_class c WHERE c.id = p.class_id)
UNION ALL SELECT 'orphan_class_parent', count(*) FROM ontology_class c
  WHERE c.parent_class_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ontology_class p WHERE p.id = c.parent_class_id)
UNION ALL SELECT 'orphan_coverage_cell', count(*) FROM coverage_cell c
  WHERE NOT EXISTS (SELECT 1 FROM ontology_class o WHERE o.id = c.ontology_class_id)
UNION ALL SELECT 'orphan_bo_header_class', count(*) FROM business_object b
  WHERE b.header_class_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ontology_class o WHERE o.id = b.header_class_id)
UNION ALL SELECT 'orphan_entity_mapping_bo', count(*) FROM entity_mapping e
  WHERE NOT EXISTS (SELECT 1 FROM business_object b WHERE b.code = e.entity_type)
UNION ALL SELECT 'orphan_feature_definition_bo', count(*) FROM feature_definition f
  WHERE NOT EXISTS (SELECT 1 FROM business_object b WHERE b.code = f.entity_type);
SQL

echo
log "=== 校验 3：序列 vs max(id)（序列必须 ≥ max(id)，否则下次插入会撞主键）==="
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$TARGET_DB" -tA <<'SQL'
SELECT format('%-30s last_value=%-10s max_id=%-10s %s', m.t,
  coalesce(s.last_value::text, '-'), coalesce(m.mx::text, '-'),
  CASE WHEN s.sequencename IS NULL THEN '⚠️ 无序列'
       WHEN m.mx IS NOT NULL AND s.last_value < m.mx THEN '❌ 序列落后（会撞主键）'
       ELSE '✅' END)
FROM (
  SELECT 'ontology_class' t, max(id) mx FROM ontology_class
  UNION ALL SELECT 'ontology_property', max(id) FROM ontology_property
  UNION ALL SELECT 'ontology_metric', max(id) FROM ontology_metric
  UNION ALL SELECT 'ontology_metric_source', max(id) FROM ontology_metric_source
  UNION ALL SELECT 'ontology_relation', max(id) FROM ontology_relation
  UNION ALL SELECT 'ontology_join', max(id) FROM ontology_join
  UNION ALL SELECT 'kpi_catalog', max(id) FROM kpi_catalog
  UNION ALL SELECT 'kpi_catalog_history', max(id) FROM kpi_catalog_history
  UNION ALL SELECT 'class_domain_mapping', max(id) FROM class_domain_mapping
  UNION ALL SELECT 'coverage_cell', max(id) FROM coverage_cell
) m
LEFT JOIN pg_sequences s ON s.schemaname = 'public' AND s.sequencename = m.t || '_id_seq'
ORDER BY m.t;
SQL

restore_backend
BACKEND_STOPPED=0
if [[ $IS_ROLLBACK -eq 1 ]]; then
  log "回滚完成。恢复到锚点状态（${ROLLBACK_FROM}）。如需撤销本次回滚，用锚点 ${BACKUP_FILE}"
else
  log "完成。如需撤销本次恢复，用 ${BACKUP_FILE} 执行："
  log "  bash scripts/restore_pg_subset_local.sh --rollback-from ${BACKUP_FILE} --target-db ${TARGET_DB} --apply"
fi
