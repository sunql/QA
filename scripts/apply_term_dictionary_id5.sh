#!/usr/bin/env bash
# scripts/apply_term_dictionary_id5.sh - 生产环境一次性数据修正
#
# 目标：term_dictionary 中 term='供货量' 的那一行（term 上有唯一约束），改映射到
#       DWD_GOODS_RECEIPT_DTL.RCV_QTY_PUU，并写入完整口径 formula_hint。
#
# 定位方式：**按 term 定位**，不按 id。同一份数据在不同环境 id 不一样
#          （开发库 id=5、生产库 id=7），写死 id 会在另一套环境直接打空。
#          EXPECTED_ID 仅供对账提示，不一致不阻断。
#
# 用法：
#   ./apply_term_dictionary_id5.sh                  # 预览：预检 + 打印现状 + 生成回滚脚本，不改库
#   ./apply_term_dictionary_id5.sh --apply          # 执行（TTY 下要求输入 yes 确认）
#   ./apply_term_dictionary_id5.sh --apply --yes    # 执行，不交互（无人值守）
#   ./apply_term_dictionary_id5.sh --backup-dir /opt/qa/backups --apply
#
# 连接方式（按优先级）：
#   1) PSQL_CMD 环境变量：完整的 psql 前缀命令（非 docker 部署时用）
#        PSQL_CMD="psql -h 10.0.0.5 -p 5432 -U qa_user -d qa_metadata" ./apply_term_dictionary_id5.sh
#   2) 默认走 docker：
#        docker exec -i qa-postgres psql -X -v ON_ERROR_STOP=1 -U qa_user -d qa_metadata
#      可用 PG_CONTAINER / PG_USER / PG_DB 覆盖容器名 / 账号 / 库名。
#
# 安全设计：
#   - 默认只预览，必须显式 --apply 才会写库
#   - 写库前按 term 锁定行并复核唯一性，任何漂移直接中止并打印候选行
#   - 整个 UPDATE 在单个事务内完成，写后校验不通过则抛错回滚，不会留下半改状态
#   - 执行前把旧值生成可直接执行的回滚脚本，落到 --backup-dir（默认当前目录）
#   - 不接触明文口令：容器内 local socket 走 trust；外部连接请用 ~/.pgpass
#
# 说明：应用侧读取术语词典是每次请求现查（无缓存），更新后下一次计划即生效，
#       不需要重启后端。
#
# 退出码：0 成功 / 预览；1 预检或执行失败；2 参数错误

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

# ---- 目标行与目标值（唯一真相，改这里）----
# term 上有唯一约束，用它定位；不同环境 id 不同（开发库 5、生产库 7）
TARGET_TERM='供货量'
# 仅用于对账提示：与脚本编写环境一致的期望 id，不一致时打印提示但不阻断
EXPECTED_ID="${EXPECTED_ID:-5}"
TARGET_CLASS='DWD_GOODS_RECEIPT_DTL'
TARGET_PROPERTY='RCV_QTY_PUU'
# 双引号是刻意的：这段文本里的 ''A02'' 是 SQL 转义后的单引号，
# 落到库里是 IN ('A02','A03','A04','A05')。文本中没有 $ ` \ !，双引号最省事。
TARGET_HINT="默认 JOIN DIM_IMATERIAL ON DIM_IMATERIAL.ITMREF_0 = DWD_GOODS_RECEIPT_DTL.ITEM_CODE WHERE DIM_IMATERIAL.TCLCOD_0 IN (''A02'',''A03'',''A04'',''A05'') AND DWD_GOODS_RECEIPT_DTL.RCV_DATE 在统计月份范围内,统计量用 SUM(DWD_GOODS_RECEIPT_DTL.RCV_QTY_PUU)。【例外】用户已明确指定供应商编码列表时,不应用 TCLCOD_0 过滤。"

# ---- 连接参数 ----
PG_CONTAINER="${PG_CONTAINER:-qa-postgres}"
PG_USER="${PG_USER:-qa_user}"
PG_DB="${PG_DB:-qa_metadata}"

APPLY=0
ASSUME_YES=0
BACKUP_DIR="$PWD"

usage() {
  grep '^# ' "$0" | sed 's/^# //'
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

# ---- 参数解析 ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)      APPLY=1; shift ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir 需要一个目录参数}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *)            echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# ---- psql 调用封装 ----
runPsql() {
  if [[ -n "${PSQL_CMD:-}" ]]; then
    # PSQL_CMD 刻意不加引号：它本身是「psql + 连接参数」的多个词
    # shellcheck disable=SC2086
    $PSQL_CMD "$@"
  else
    docker exec -i "$PG_CONTAINER" \
      psql -X -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" "$@"
  fi
}

if [[ -n "${PSQL_CMD:-}" ]]; then
  PSQL_RUN_HINT="$PSQL_CMD"
else
  PSQL_RUN_HINT="docker exec -i $PG_CONTAINER psql -X -v ON_ERROR_STOP=1 -U $PG_USER -d $PG_DB"
fi

# ---- 前置检查 ----
if [[ -z "${PSQL_CMD:-}" ]]; then
  command -v docker >/dev/null 2>&1 || die "找不到 docker 命令，且未设置 PSQL_CMD"
  docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER" \
    || die "容器 $PG_CONTAINER 未运行（docker ps 看不到它）"
fi

TABLE_OK="$(runPsql -t -A -c "SELECT to_regclass('public.term_dictionary') IS NOT NULL;")"
[[ "$TABLE_OK" == "t" ]] || die "当前库里没有 term_dictionary 表（库：${PG_DB}），连接可能指错了"

echo "== 连接信息 =="
runPsql -t -A -F ' | ' -c \
  "SELECT current_database(), coalesce(inet_server_addr()::text, 'local'), current_user;"

# ---- 预检：按 term 定位（term 有唯一约束）----
LOCATOR="$(runPsql -t -A -F ',' -v "term=$TARGET_TERM" <<'SQL'
SELECT coalesce(min(id), 0), count(*)
  FROM term_dictionary
 WHERE term = :'term';
SQL
)"
IFS=',' read -r ACTUAL_ID BY_TERM <<< "$LOCATOR"

# 定位失败时把两个候选行都打出来：只报「匹配 0 行」运维无从下手
showCandidates() {
  runPsql -v "id=$EXPECTED_ID" -v "term=$TARGET_TERM" <<'SQL'
\x on
SELECT id, term, length(term) AS term_len,
       mapped_class_name, mapped_property_name, formula_hint, updated_time
  FROM term_dictionary
 WHERE id = :id OR term = :'term'
 ORDER BY id;
SQL
}

echo
echo "== 预检 =="
echo "  term='$TARGET_TERM'            匹配行数: $BY_TERM"
echo "  实际 id: $ACTUAL_ID"

if [[ "$BY_TERM" != "1" ]]; then
  echo
  echo "== 候选行 =="
  showCandidates
  die "定位失败：term='$TARGET_TERM' 匹配 $BY_TERM 行（期望 1）。已中止，未做任何修改。"
fi

if [[ "$ACTUAL_ID" != "$EXPECTED_ID" ]]; then
  echo "  提示: 实际 id=$ACTUAL_ID 与 EXPECTED_ID=$EXPECTED_ID 不同（不同环境编号不一致），按 term 继续。"
fi

# ---- 现状 ----
echo
echo "== 当前值 =="
runPsql -v "term=$TARGET_TERM" <<'SQL'
\x on
SELECT id, term, mapped_class_name, mapped_property_name, formula_hint, updated_time
  FROM term_dictionary
 WHERE term = :'term';
SQL

# ---- 生成回滚脚本（用旧值）----
ROLLBACK_SQL="$(runPsql -t -A -v "term=$TARGET_TERM" <<'SQL'
SELECT format(
  'UPDATE term_dictionary SET mapped_class_name = %L, mapped_property_name = %L, '
  'formula_hint = %L, updated_time = %L WHERE term = %L;',
  mapped_class_name, mapped_property_name, formula_hint, updated_time, term)
FROM term_dictionary
WHERE term = :'term';
SQL
)"
[[ -n "$ROLLBACK_SQL" ]] || die "回滚语句生成失败（查不到旧值），已中止"

TS="$(date '+%Y%m%d-%H%M%S')"
mkdir -p "$BACKUP_DIR"
ROLLBACK_FILE="$BACKUP_DIR/term_dictionary_supply_qty_rollback_${TS}.sql"
ROLLBACK_BASENAME="$(basename "$ROLLBACK_FILE")"
{
  echo "-- term_dictionary「${TARGET_TERM}」回滚脚本"
  echo "-- 生成时间: $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "-- 生成脚本: $SCRIPT_NAME"
  echo "-- 内容: 本次 UPDATE 之前该行的原值，按需执行即可还原"
  echo "--"
  echo "-- 执行方式："
  echo "--   $PSQL_RUN_HINT < $ROLLBACK_BASENAME"
  echo ""
  echo "BEGIN;"
  echo "$ROLLBACK_SQL"
  echo "COMMIT;"
} > "$ROLLBACK_FILE"

# ---- 计划写入的值 ----
echo
echo "== 即将写入 =="
cat <<EOF
  mapped_class_name    = $TARGET_CLASS
  mapped_property_name = $TARGET_PROPERTY
  formula_hint         = $TARGET_HINT
  updated_time         = now()   （ORM onupdate 的等价行为，原始 SQL 不会自动带）
EOF

echo
echo "回滚脚本已生成: $ROLLBACK_FILE"

if [[ "$APPLY" -ne 1 ]]; then
  echo
  echo "== 预览模式，未做任何修改 =="
  echo "确认无误后执行："
  echo "  $0 --apply"
  exit 0
fi

# ---- 二次确认 ----
if [[ "$ASSUME_YES" -ne 1 ]]; then
  if [[ -t 0 ]]; then
    read -r -p "确认写入生产库？输入 yes 继续，其它任意内容取消： " ANSWER
    [[ "$ANSWER" == "yes" ]] || { echo "已取消，未做任何修改。"; exit 0; }
  else
    die "非交互环境必须显式加 --yes（防止误触发）"
  fi
fi

# ---- 执行 ----
echo
echo "== 执行 UPDATE =="
if ! runPsql -v "term=$TARGET_TERM" \
             -v "cls=$TARGET_CLASS" -v "prop=$TARGET_PROPERTY" -v "hint=$TARGET_HINT" <<'SQL'
\set ON_ERROR_STOP on
SET client_encoding = 'UTF8';
BEGIN;
-- 行被别的会话长时间锁住时快速失败，而不是无限等待
SET LOCAL lock_timeout = '5s';

-- 目标值走 psql 变量 + 临时表：DO 块里不能用 :'var'（psql 不在 $$...$$ 内做变量替换），
-- 这样长文本只需在脚本里写一次
CREATE TEMP TABLE _td_target (term text, cls text, prop text, hint text) ON COMMIT DROP;
INSERT INTO _td_target VALUES (:'term', :'cls', :'prop', :'hint');

DO $$
DECLARE
  tgt      record;
  affected int;
  verified int;
BEGIN
  SELECT * INTO tgt FROM _td_target;
  IF tgt IS NULL THEN
    RAISE EXCEPTION '内部错误：目标值临时表为空';
  END IF;

  UPDATE term_dictionary
     SET mapped_class_name    = tgt.cls,
         mapped_property_name = tgt.prop,
         formula_hint         = tgt.hint,
         updated_time         = now()
   WHERE term = tgt.term;

  GET DIAGNOSTICS affected = ROW_COUNT;
  IF affected <> 1 THEN
    RAISE EXCEPTION 'UPDATE 影响 % 行，期望恰好 1 行；事务回滚', affected;
  END IF;

  SELECT count(*) INTO verified
    FROM term_dictionary
   WHERE term = tgt.term
     AND mapped_class_name    IS NOT DISTINCT FROM tgt.cls
     AND mapped_property_name IS NOT DISTINCT FROM tgt.prop
     AND formula_hint         IS NOT DISTINCT FROM tgt.hint;

  IF verified <> 1 THEN
    RAISE EXCEPTION '写后校验失败：符合目标值的行数为 %，期望 1；事务回滚', verified;
  END IF;

  RAISE NOTICE 'UPDATE 成功：「%」已更新', tgt.term;
END
$$;

COMMIT;
SQL
then
  echo "ERROR: 执行失败，事务已回滚（未写入任何数据）。请查看上方 psql 报错。" >&2
  echo "旧值回滚脚本（当前无需执行）: $ROLLBACK_FILE" >&2
  exit 1
fi

# ---- 复核 ----
echo
echo "== 更新后 =="
runPsql -v "term=$TARGET_TERM" <<'SQL'
\x on
SELECT id, term, mapped_class_name, mapped_property_name, formula_hint, updated_time
  FROM term_dictionary
 WHERE term = :'term';
SQL

cat <<EOF

完成。
回滚脚本: $ROLLBACK_FILE
回滚命令: $PSQL_RUN_HINT < $ROLLBACK_FILE

术语词典无缓存，下一次计划请求即生效，无需重启后端。
EOF
