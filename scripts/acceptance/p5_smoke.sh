#!/usr/bin/env bash
# P5 工作流 / 审批 —— 端到端冒烟验收（本地栈）
#
# 验收的四件事（每一件都同时断言 HTTP 结果 **和** 落库状态，
# 避免「只看状态码」的假绿）：
#   1. 全链路：提交 → 第一步执行 → 停在审批门 → 审批通过 → 第二步执行 → COMPLETED
#   2. 幂等：同 idempotencyKey 重放 ⇒ 200 + 同一个实例，不落新行
#   3. 取消：CANCELLED + 未执行步骤 SKIPPED + 待办审批 CANCELLED
#   4. 四眼：提交人自己批 ⇒ 403（自审批拦截）
#
# 用法：
#   ADMIN_PASSWORD='***' SUBMITTER_USER=wf_submitter SUBMITTER_PASSWORD='***' \
#     ./scripts/acceptance/p5_smoke.sh
#
# 可选环境变量：
#   BASE_URL         后端地址（默认 http://localhost:8000；非本地地址需 ALLOW_NON_LOCAL=1）
#   ADMIN_USER       审批人账号（默认 admin，须与定义体审批矩阵里的 approverUser 一致）
#   PG_CONTAINER     本地 PG 容器名（默认 qa-postgres；密码从容器环境读取，不落盘）
#   POLL_TIMEOUT     等待 worker 推进的秒数（默认 60）
#
# 前置：本地 compose 栈已启动、ENABLE_WORKFLOW=true、worker 容器在跑、迁移已到 0152。

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:?需要 ADMIN_PASSWORD（本地 admin 密码）}"
SUBMITTER_USER="${SUBMITTER_USER:?需要 SUBMITTER_USER（提交人账号，须不是 ADMIN_USER）}"
SUBMITTER_PASSWORD="${SUBMITTER_PASSWORD:?需要 SUBMITTER_PASSWORD}"
PG_CONTAINER="${PG_CONTAINER:-qa-postgres}"
POLL_TIMEOUT="${POLL_TIMEOUT:-60}"
ALLOW_NON_LOCAL="${ALLOW_NON_LOCAL:-0}"
WORKER_CONTAINER="${WORKER_CONTAINER:-qa-workflow-worker}"

DOMAIN_CODE="procurement"
CAPABILITY_CODE="po_write"
VERSION="v1"

PASS=0
FAIL=0
BODY=""
CODE=""

pass() { printf '  \033[32m✅\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31m❌\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
info() { printf '\n\033[36m▶ %s\033[0m\n' "$1"; }

# 只对状态码断言是假绿的源头 ⇒ 断言必须同时看响应体 / 库里的状态。
check() { # description expected actual
  if [[ "$2" == "$3" ]]; then pass "$1（$3）"; else fail "$1：期望 $2，实际 $3"; fi
}

# ---------- 安全闸：默认拒绝对非本地地址运行 ----------
case "$BASE_URL" in
  http://localhost:* | http://127.0.0.1:*) ;;
  *)
    if [[ "$ALLOW_NON_LOCAL" != "1" ]]; then
      printf '拒绝对非本地地址运行：%s\n（确需如此请显式设置 ALLOW_NON_LOCAL=1）\n' "$BASE_URL" >&2
      exit 2
    fi
    ;;
esac

if [[ "$ADMIN_USER" == "$SUBMITTER_USER" ]]; then
  printf 'ADMIN_USER 与 SUBMITTER_USER 必须不同（四眼原则）\n' >&2
  exit 2
fi

# ---------- 工具函数 ----------
req() { # 用法同 curl；结果写入 CODE / BODY
  local raw
  if ! raw=$(curl -sS -w $'\n%{http_code}' "$@"); then
    CODE="000"
    BODY=""
    return 0
  fi
  CODE="${raw##*$'\n'}"
  BODY="${raw%$'\n'*}"
}

psql_q() { # 只读查询（-tA，单值）
  docker exec -i "$PG_CONTAINER" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -v ON_ERROR_STOP=1' <<<"$1"
}

login() { # username password -> 令牌（失败返回空）
  req -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg u "$1" --arg p "$2" '{username:$u,password:$p}')"
  [[ "$CODE" == "200" ]] || return 0
  jq -r '.accessToken' <<<"$BODY"
}

wait_instance_status() { # instanceId 期望状态 -> 实际状态（不可达则返回 FAILED/TIMEOUT）
  local id="$1" want="$2" deadline=$((SECONDS + POLL_TIMEOUT)) st
  while ((SECONDS < deadline)); do
    req -H "Authorization: Bearer $TOKEN_ADMIN" "$BASE_URL/api/v1/workflow/instances/$id"
    if [[ "$CODE" != "200" ]]; then
      printf 'HTTP %s' "$CODE"
      return 0
    fi
    st=$(jq -r '.instance.status' <<<"$BODY")
    if [[ "$st" == "$want" || "$st" == "FAILED" || "$st" == "CANCELLED" ]]; then
      printf '%s' "$st"
      return 0
    fi
    sleep 1
  done
  printf 'TIMEOUT'
}

submit_instance() { # 令牌 idempotencyKey
  req -X POST "$BASE_URL/api/v1/workflow/instances" \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d "$(jq -n --arg d "$DOMAIN_CODE" --arg c "$CAPABILITY_CODE" --arg v "$VERSION" --arg k "$2" \
      '{domainCode:$d,capabilityCode:$c,version:$v,idempotencyKey:$k,request:{poNo:"PO-SMOKE",amount:100}}')"
}

# ---------- 预检 ----------
info "预检"
req "$BASE_URL/api/v1/health"
check "后端可访问" "200" "$CODE"

# 开关关闭时整组路由不注册 ⇒ /openapi.json 里没有该路径；开启后才有。
# 不用状态码判定：`/instances` 同时注册了 GET（列表）与 POST，未鉴权 GET 在
# AUTH_MODE=stub 下返回 200、real 下返回 403，只有方法不符才是 405 ⇒ 状态码
# 判据天然不稳。OpenAPI 成员判定与鉴权模式无关，直接对应「路由是否注册」。
# 正向对照：未知路径必须 404，否则「路径缺失 ⇒ 已关闭」失去区分力。
req "$BASE_URL/api/v1/definitely-not-a-route"
check "未知路径返回 404（探测器有区分力）" "404" "$CODE"

req "$BASE_URL/openapi.json"
if [[ "$CODE" == "200" ]] && jq -e '.paths | has("/api/v1/workflow/instances")' <<<"$BODY" >/dev/null 2>&1; then
  pass "ENABLE_WORKFLOW 已开启（openapi 含 /api/v1/workflow/instances）"
else
  fail "ENABLE_WORKFLOW 已开启（openapi 缺 /api/v1/workflow/instances，HTTP ${CODE}）"
fi

if docker inspect -f '{{.State.Running}}' "$WORKER_CONTAINER" 2>/dev/null | grep -qx true; then
  pass "worker 容器 $WORKER_CONTAINER 在运行"
else
  fail "worker 容器 $WORKER_CONTAINER 未运行（审批通过后没有进程推进第二步）"
  printf '\n前置不满足，终止。\n' >&2
  exit 1
fi

TOKEN_ADMIN=$(login "$ADMIN_USER" "$ADMIN_PASSWORD")
if [[ -n "$TOKEN_ADMIN" ]]; then pass "admin 登录成功"; else fail "admin 登录失败"; exit 1; fi

TOKEN_SUB=$(login "$SUBMITTER_USER" "$SUBMITTER_PASSWORD")
if [[ -n "$TOKEN_SUB" ]]; then
  pass "提交人登录成功"
else
  req -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "$(jq -n --arg u "$SUBMITTER_USER" --arg p "$SUBMITTER_PASSWORD" '{username:$u,password:$p}')"
  if [[ "$BODY" == *MUST_CHANGE_PASSWORD* ]]; then
    fail "提交人 must_change_password=true（先用 admin 调 PUT /api/v1/users/{id}/password 清掉）"
  else
    fail "提交人登录失败（HTTP ${CODE}）"
  fi
  exit 1
fi

# ---------- 定义种子（幂等） ----------
info "审批定义"
existing=$(psql_q "SELECT count(*) FROM workflow_definition
  WHERE domain_code='$DOMAIN_CODE' AND capability_code='$CAPABILITY_CODE'
    AND version='$VERSION' AND status='PUBLISHED';")
if [[ "$existing" == "0" ]]; then
  psql_q "INSERT INTO workflow_definition
      (domain_code, capability_code, version, definition_json, status, created_by)
    VALUES ('$DOMAIN_CODE', '$CAPABILITY_CODE', '$VERSION',
      '{\"riskLevel\":\"MEDIUM\",\"steps\":[{\"code\":\"collect\",\"order\":0,\"commandType\":\"po.write\",\"payload\":{\"poNo\":\"PO-SMOKE-1\",\"amount\":100}},{\"code\":\"gate\",\"order\":1,\"requiresApproval\":true},{\"code\":\"submit\",\"order\":2,\"commandType\":\"po.write\",\"payload\":{\"poNo\":\"PO-SMOKE-2\",\"amount\":100}}],\"approvalMatrix\":[{\"matrixCode\":\"po_medium_$ADMIN_USER\",\"subjectType\":\"workflow_instance\",\"capabilityCode\":\"$CAPABILITY_CODE\",\"riskLevel\":\"MEDIUM\",\"approverUser\":\"$ADMIN_USER\",\"timeoutHours\":24}]}',
      'PUBLISHED', '$ADMIN_USER');" >/dev/null
  pass "已写入 PUBLISHED 定义 $DOMAIN_CODE/$CAPABILITY_CODE@$VERSION"
else
  pass "已存在 PUBLISHED 定义（复用）"
fi

# ---------- 场景 1：全链路 ----------
info "场景 1/4：全链路（提交 → 审批 → 完成）"
KEY_DONE="smoke-$(date +%s)-done"
submit_instance "$TOKEN_SUB" "$KEY_DONE"
check "提交返回 201" "201" "$CODE"
ID_DONE=$(jq -r '.id' <<<"$BODY")
[[ "$ID_DONE" != "null" ]] || { fail "未能取到实例 id：$BODY"; exit 1; }

check "提交后停在审批门" "WAITING_APPROVAL" "$(wait_instance_status "$ID_DONE" WAITING_APPROVAL)"

req -H "Authorization: Bearer $TOKEN_ADMIN" "$BASE_URL/api/v1/workflow/instances/$ID_DONE"
TASK_ID=$(jq -r '[.approvals[] | select(.status=="PENDING")][0].id' <<<"$BODY")
if [[ "$TASK_ID" != "null" ]]; then pass "生成 1 条待审批任务（id=${TASK_ID}）"; else fail "没有待审批任务"; exit 1; fi

# 提交人去批：对象级不可见 ⇒ 404（与「任务不存在」同形，没有存在性预言机）
req -X POST "$BASE_URL/api/v1/workflow/approvals/$TASK_ID/decision" \
  -H "Authorization: Bearer $TOKEN_SUB" -H 'Content-Type: application/json' \
  -d '{"decision":"APPROVE"}'
check "非审批人决策被拒" "404" "$CODE"

req -X POST "$BASE_URL/api/v1/workflow/approvals/$TASK_ID/decision" \
  -H "Authorization: Bearer $TOKEN_ADMIN" -H 'Content-Type: application/json' \
  -d '{"decision":"APPROVE","note":"冒烟验收"}'
check "审批通过返回 200" "200" "$CODE"

check "审批后实例 COMPLETED" "COMPLETED" "$(wait_instance_status "$ID_DONE" COMPLETED)"

req -H "Authorization: Bearer $TOKEN_ADMIN" "$BASE_URL/api/v1/workflow/instances/$ID_DONE"
check "三个步骤全部 SUCCEEDED" "3" "$(jq -r '[.steps[] | select(.status=="SUCCEEDED")] | length' <<<"$BODY")"
check "审批任务 APPROVED" "APPROVED" "$(jq -r '.approvals[0].status' <<<"$BODY")"
check "审批人为 $ADMIN_USER" "$ADMIN_USER" "$(jq -r '.approvals[0].approverUser' <<<"$BODY")"

check "审计已留痕（提交 + 审批发起 + 决策 = 3 条）" "3" \
  "$(psql_q "SELECT count(*) FROM audit_log WHERE entity_id='$ID_DONE' AND entity_type='workflow_instance'
      OR (entity_type='approval_task' AND entity_id='$TASK_ID');")"

# ---------- 场景 2：幂等重放 ----------
info "场景 2/4：幂等重放"
submit_instance "$TOKEN_SUB" "$KEY_DONE"
check "同 key 重放返回 200" "200" "$CODE"
check "重放指向同一实例" "$ID_DONE" "$(jq -r '.id' <<<"$BODY")"
check "库中该 key 只有 1 行" "1" \
  "$(psql_q "SELECT count(*) FROM workflow_instance WHERE idempotency_key='$KEY_DONE';")"

# ---------- 场景 3：取消 ----------
info "场景 3/4：取消"
KEY_CANCEL="smoke-$(date +%s)-cancel"
submit_instance "$TOKEN_SUB" "$KEY_CANCEL"
check "提交返回 201" "201" "$CODE"
ID_CANCEL=$(jq -r '.id' <<<"$BODY")
check "提交后停在审批门" "WAITING_APPROVAL" "$(wait_instance_status "$ID_CANCEL" WAITING_APPROVAL)"

req -X POST "$BASE_URL/api/v1/workflow/instances/$ID_CANCEL/cancel" \
  -H "Authorization: Bearer $TOKEN_SUB" -H 'Content-Type: application/json' \
  -d '{"reason":"冒烟验收取消"}'
check "取消返回 200" "200" "$CODE"
check "取消后状态 CANCELLED" "CANCELLED" "$(jq -r '.status' <<<"$BODY")"

req -H "Authorization: Bearer $TOKEN_ADMIN" "$BASE_URL/api/v1/workflow/instances/$ID_CANCEL"
check "未执行步骤被跳过（2 个）" "2" "$(jq -r '[.steps[] | select(.status=="SKIPPED")] | length' <<<"$BODY")"
check "待办审批被取消" "CANCELLED" "$(jq -r '.approvals[0].status' <<<"$BODY")"
check "取消原因落库" "冒烟验收取消" \
  "$(psql_q "SELECT error_message FROM workflow_instance WHERE id=$ID_CANCEL;")"

# ---------- 场景 4：四眼（提交人 = 审批人 ⇒ 403） ----------
info "场景 4/4：四眼原则（admin 自己提交自己批）"
KEY_SELF="smoke-$(date +%s)-self"
submit_instance "$TOKEN_ADMIN" "$KEY_SELF"
check "admin 提交返回 201" "201" "$CODE"
ID_SELF=$(jq -r '.id' <<<"$BODY")
check "停在审批门" "WAITING_APPROVAL" "$(wait_instance_status "$ID_SELF" WAITING_APPROVAL)"

req -H "Authorization: Bearer $TOKEN_ADMIN" "$BASE_URL/api/v1/workflow/instances/$ID_SELF"
TASK_SELF=$(jq -r '[.approvals[] | select(.status=="PENDING")][0].id' <<<"$BODY")
req -X POST "$BASE_URL/api/v1/workflow/approvals/$TASK_SELF/decision" \
  -H "Authorization: Bearer $TOKEN_ADMIN" -H 'Content-Type: application/json' \
  -d '{"decision":"APPROVE"}'
check "自审批被拒（403）" "403" "$CODE"

# ---------- 汇总 ----------
info "汇总"
printf '实例：完成=%s 取消=%s 自审批=%s\n' "$ID_DONE" "$ID_CANCEL" "$ID_SELF"
printf '通过 %d 项，失败 %d 项\n' "$PASS" "$FAIL"
if ((FAIL > 0)); then
  printf '\033[31m验收未通过\033[0m\n'
  exit 1
fi
printf '\033[32m验收通过\033[0m\n'
