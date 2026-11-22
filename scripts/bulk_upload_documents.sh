#!/usr/bin/env bash
# 批量上传文档到生产/任意环境（逐个调用 POST /api/v1/documents/upload）。
#
# 为什么是脚本而不是前端：文档中心当前上传弹窗是**单文件**（maxCount=1）。
# 批量场景用本脚本最稳：串行上传（每个文件都要落 MinIO + 解析 + 向量化），
# 遇到重复/失败不中断，最后汇总。
#
# 用法（在生产宿主机执行，后端端口已映射到 127.0.0.1:8000）：
#   USERNAME=admin PASSWORD='***' ./scripts/bulk_upload_documents.sh /data/docs \
#       [documentType=POLICY] [securityLevel=L1] [version=v1.0] [owner=采购部]
# 或直接给 TOKEN：
#   TOKEN='eyJ...' ./scripts/bulk_upload_documents.sh /data/docs
#
# 仅上传扩展名：pdf docx txt md（与 /documents/upload 支持的类型一致）；
# 单文件上限 50 MB（后端 MAX_UPLOAD_BYTES，超限 413）。
# 环境变量：API_BASE（默认 http://127.0.0.1:8000/api/v1）、DOCUMENT_TYPE、SECURITY_LEVEL、VERSION、OWNER、DRY_RUN=1
set -euo pipefail

DIR="${1:-}"
if [[ -z "$DIR" || ! -d "$DIR" ]]; then
  echo "用法: $0 <目录>（环境变量见脚本头部注释）" >&2
  exit 2
fi

API_BASE="${API_BASE:-http://127.0.0.1:8000/api/v1}"
DOCUMENT_TYPE="${DOCUMENT_TYPE:-OTHER}"
SECURITY_LEVEL="${SECURITY_LEVEL:-L1}"
VERSION="${VERSION:-v1.0}"
OWNER="${OWNER:-}"
DRY_RUN="${DRY_RUN:-0}"

TOKEN="${TOKEN:-}"
if [[ "$DRY_RUN" != "1" && -z "$TOKEN" ]]; then
  : "${USERNAME:?需要 TOKEN 或 USERNAME/PASSWORD}"
  : "${PASSWORD:?需要 TOKEN 或 USERNAME/PASSWORD}"
  loginBody=$(printf '{"username":"%s","password":"%s"}' "$USERNAME" "$PASSWORD")
  loginResp=$(curl -sS -X POST "$API_BASE/auth/login" \
    -H 'Content-Type: application/json' -d "$loginBody")
  TOKEN=$(printf '%s' "$loginResp" | grep -o '"accessToken":"[^"]*"' | head -1 | cut -d'"' -f4)
  if [[ -z "$TOKEN" ]]; then
    echo "登录失败：$loginResp" >&2
    exit 3
  fi
fi

ok=0; dup=0; fail=0
files=$(find "$DIR" -maxdepth 1 -type f \( -iname '*.pdf' -o -iname '*.docx' -o -iname '*.txt' -o -iname '*.md' \) | sort)
if [[ -z "$files" ]]; then
  echo "目录里没有可上传文件（pdf/docx/txt/md）：$DIR" >&2
  exit 2
fi

while IFS= read -r f; do
  name=$(basename "$f")
  if [[ "$DRY_RUN" = "1" ]]; then
    echo "[dry-run] $name"
    continue
  fi
  code=$(curl -sS -o /tmp/bulk_upload_resp.json -w '%{http_code}' -X POST \
    "$API_BASE/documents/upload?documentType=${DOCUMENT_TYPE}&securityLevel=${SECURITY_LEVEL}&version=${VERSION}&owner=${OWNER}" \
    -H "Authorization: Bearer $TOKEN" \
    -F "file=@${f};type=application/octet-stream")
  case "$code" in
    201) ok=$((ok+1)); echo "OK   $name" ;;
    409) dup=$((dup+1)); echo "DUP  $name（内容重复）" ;;
    *)   fail=$((fail+1)); echo "FAIL $name（HTTP $code）: $(head -c 200 /tmp/bulk_upload_resp.json)" ;;
  esac
done <<< "$files"

echo "----"
echo "成功=$ok 重复=$dup 失败=$fail  （目录：${DIR}）"
[[ "$fail" -eq 0 ]]
