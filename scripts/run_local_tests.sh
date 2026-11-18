#!/usr/bin/env bash
# scripts/run_local_tests.sh —— 本地跑后端测试的**唯一入口**
#
# 解决什么（2026-11-18 实测，本仓真实事故链）：
#
#   1. **测试库落错**：不设 `TEST_DATABASE_URL` 时，
#      `app/tests/_pg_support.resolveTestDatabaseUrl()` 会把 `DATABASE_URL` 的
#      **库名**改写成 `qa_metadata_test`、**端口原样保留** ⇒ 本地 `DATABASE_URL`
#      指向 5433（开发库端口）⇒ 测试落到 **`5433/qa_metadata_test`** ——
#      那个库实测停在 **0139**（schema 缺列、且带脏列）⇒ `_ensureSchema()` 里的
#      `alembic upgrade head` 报 `DuplicateColumnError` ⇒ **本地测试整体跑不起来**
#      （连纯 unit 都 ERROR，因为 unit 的 conftest 也会建引擎）。
#   2. **Neo4j fail-fast**：不设 `TEST_NEO4J_URI` ⇒ integration 夹具**故意**报错
#      （2026-10-03 生产图谱被清空事故后的守卫，**不要关**）。
#      独立测试实例 `qa-neo4j-test`（7688）本机已在跑。
#
# 本脚本把三个测试依赖**一次性接线**，让本地与 CI 跑在**同一套隔离栈**上：
#
#   | 依赖 | 本地 | CI |
#   |---|---|---|
#   | PG | `qa-pg-a1` **5434** / `qa_metadata_test` | 5434 / `qa_metadata_test` |
#   | Neo4j | `qa-neo4j-test` **7688** | 7688 |
#   | Milvus | 本机 milvus 19530，测试库名 `qa_test`（与应用默认库隔离） | `qa_ci` |
#
# 用法：
#   ./scripts/run_local_tests.sh                      # 跑全部（unit + integration + services）
#   ./scripts/run_local_tests.sh app/tests/unit       # 跑指定路径
#   ./scripts/run_local_tests.sh -k "workflow"        # 透传 pytest 参数
#
# 可覆盖的环境变量：
#   QA_TEST_PG_PORT     测试 PG 端口（默认 5434）
#   QA_TEST_NEO4J_PORT  测试 Neo4j 端口（默认 7688）
#   QA_TEST_PG_USER / QA_TEST_PG_PASSWORD
#   QA_TEST_NEO4J_USER / QA_TEST_NEO4J_PASSWORD
#   QA_TEST_DB_NAME     测试库名（默认 qa_metadata_test）
#
# ⚠️ **不要**用 `docker compose ... down -v` 或删 `qa-pg-a1` 容器来"重置测试库"：
# 测试库重建的正确命令见 `docs/update/1118/M0/A1-本地栈同构核对表.md`。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

RED='\033[0;31m'
GREEN='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${CYAN}[run_local_tests]${NC} $*"; }
err() { echo -e "${RED}[run_local_tests]${NC} $*" >&2; }

# ---- 1. 从 .env 取本地开发口令（口令不写死在本脚本里）----
# 读取顺序：backend/.env（宿主机开发态）→ docker/.env（compose 栈 SSOT）。
_readEnvValue() {
    local key="$1"
    local file value
    for file in "$BACKEND_DIR/.env" "$REPO_ROOT/docker/.env"; do
        [ -f "$file" ] || continue
        value="$(grep -E "^${key}=" "$file" | head -1 | cut -d= -f2-)"
        if [ -n "$value" ]; then
            printf '%s' "$value"
            return 0
        fi
    done
    return 0
}

PG_PORT="${QA_TEST_PG_PORT:-5434}"
PG_USER="${QA_TEST_PG_USER:-$(_readEnvValue POSTGRES_USER)}"
PG_USER="${PG_USER:-qa_user}"
PG_PASSWORD="${QA_TEST_PG_PASSWORD:-$(_readEnvValue POSTGRES_PASSWORD)}"
DB_NAME="${QA_TEST_DB_NAME:-qa_metadata_test}"

NEO4J_PORT="${QA_TEST_NEO4J_PORT:-7688}"
NEO4J_USER="${QA_TEST_NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${QA_TEST_NEO4J_PASSWORD:-$(_readEnvValue NEO4J_PASSWORD)}"

# 5433 是**开发** PG（容器 `qa-postgres`）。测试库 SSOT 在 **5434**（`qa-pg-a1`）；
# 指到 5433 ⇒ 落到那个停在旧版本的 `qa_metadata_test` 上 ⇒ 整套测试 ERROR（见文件头 D1）。
if [ "$PG_PORT" = "5433" ]; then
    err "QA_TEST_PG_PORT=5433 已退役：5433 是开发库端口（qa-postgres）。"
    err "测试库 SSOT 在 5434（qa-pg-a1）；请去掉该覆盖或改成 5434。"
    exit 2
fi

# 口令**不得**写死在本脚本里（本仓库安全规则）：找不到就 fail-fast，
# 让使用者去 .env 补，而不是回退到一个「抄来的默认口令」。
if [ -z "$PG_PASSWORD" ]; then
    err "找不到测试 PG 口令：请在 backend/.env（或 docker/.env）设置 POSTGRES_PASSWORD，"
    err "或显式传入 QA_TEST_PG_PASSWORD=..."
    exit 2
fi
if [ -z "$NEO4J_PASSWORD" ]; then
    err "找不到测试 Neo4j 口令：请在 backend/.env（或 docker/.env）设置 NEO4J_PASSWORD，"
    err "或显式传入 QA_TEST_NEO4J_PASSWORD=..."
    exit 2
fi

# ---- 2. 导出测试环境 ----
export TEST_DATABASE_URL="postgresql+asyncpg://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${DB_NAME}"
export TEST_NEO4J_URI="bolt://localhost:${NEO4J_PORT}"
export NEO4J_USER NEO4J_PASSWORD
# Milvus：测试库与应用默认库隔离（conftest 默认即 qa_test；这里显式写出以免被继承）
export MILVUS_TEST_DB_NAME="${MILVUS_TEST_DB_NAME:-qa_test}"

# 日志**打码**：口令不进终端回滚缓冲 / CI 日志（排障只需要 host:port/db）
log "TEST_DATABASE_URL = postgresql+asyncpg://${PG_USER}:***@localhost:${PG_PORT}/${DB_NAME}"
log "TEST_NEO4J_URI    = ${TEST_NEO4J_URI}"

# ---- 3. 前置检查：端口可达 ----
if ! nc -z localhost "$PG_PORT" 2>/dev/null; then
    err "测试 PG 端口 ${PG_PORT} 不可达。期望容器 'qa-pg-a1' 在跑："
    err "  docker start qa-pg-a1"
    err "（重建测试库：见 docs/update/1118/M0/A1-本地栈同构核对表.md）"
    exit 1
fi
if ! nc -z localhost "$NEO4J_PORT" 2>/dev/null; then
    err "测试 Neo4j 端口 ${NEO4J_PORT} 不可达。期望容器 'qa-neo4j-test' 在跑："
    err "  docker start qa-neo4j-test"
    exit 1
fi

# ---- 4. 跑 pytest ----
cd "$BACKEND_DIR"
if [ "$#" -eq 0 ]; then
    set -- app/tests
fi
log "pytest $*"
exec uv run pytest "$@"
