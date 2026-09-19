# 运维手册

## 启动

```bash
cp docker/.env.example docker/.env  # 编辑密钥
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d
```

服务端口：
- 前端 5173、后端 8000(/docs)、PostgreSQL **5433**（容器 5432 映射到宿主 5433，避 OpenMetadata 占用；详见下方「迁移与端口映射」）、Neo4j 7474/7687、Milvus 19530、MySQL 3306。

## 本地开发

```bash
cd backend
uv sync --extra dev
uv run alembic upgrade head       # 迁移

# 必须带 docker/.env 的 SECRET_KEY！否则 datasource 解密失败（见下方「SECRET_KEY 必带」）
export SECRET_KEY="$(grep '^SECRET_KEY=' ../docker/.env | cut -d= -f2)"
uv run uvicorn app.main:app --reload --port 8000
uv run pytest --cov=app --cov-fail-under=80

cd frontend
npm install && npm run dev
npm run test:coverage
```

> **SECRET_KEY 必带（2026-08-18 踩坑）**：DB 里 `data_source.password_encrypted`（ZJTH-Oracle 等）是用
> `docker/.env` 的 `SECRET_KEY` 加密的。启动后端**必须**导出同一密钥，否则 datasource 读取时
> `Fernet` 解密抛 `ConfigError: SECRET_KEY 不是合法的 Fernet 密钥`，整个 NL2SQL 链路失败。
> 代码默认值 `development-insecure-key-change-me`（33 字符）不是合法 Fernet key，**不要依赖默认值**。
> `uv run uvicorn` 不会自动读取 `docker/.env`，需显式 export。

## 数据库迁移

```bash
uv run alembic upgrade head      # 应用
uv run alembic revision --autogenerate -m "desc"  # 生成新迁移
uv run alembic downgrade -1     # 回滚一步
```

## 日志

- 后端日志级别由 `LOG_LEVEL` 控制。
- Token 消耗、模型路由决策、SQL 执行审计均记录日志。

## 备份

- PostgreSQL 元数据：`pg_dump`。
- Neo4j：`neo4j-admin dump`。
- Milvus：MinIO 数据卷备份。

## 常见问题

| 现象 | 排查 |
|------|------|
| 后端启动 SECRET_KEY 报错 | 生成合法 Fernet key 填入 |
| 查询报「SECRET_KEY 不是合法的 Fernet 密钥」 | 重启时漏了 `SECRET_KEY` env。`export SECRET_KEY="$(grep '^SECRET_KEY=' ../docker/.env \| cut -d= -f2)"` 后重启（必须与加密 datasource 时同一密钥，不能用新生成的） |
| NL2SQL 生成错误 SQL | 检查本体是否定义、SQL Guard 日志 |
| 模型调用失败 | 检查 API Key、网络、provider 配置 |
| Ollama Token 为 0 | 旧版无 eval_count，标记近似 |
| SQLite 测试自增失败 | 主键须用 BigIntPk（with_variant Integer） |
| 后端 500 `UndefinedTableError: relation "xxx" does not exist` | DB 落后于 ORM（迁移未应用）。`cd backend && DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata alembic upgrade head` |
| 后端 500 `ConnectionRefusedError [Errno 61] Connection refused` | 连了 5432，但 Docker 把宿主 5433 映射到容器 5432（`docker-compose.yml` 注释「避开宿主 5432（被 openmetadata 占用）」）。本机连 PG 全部走 5433 |

## 迁移与端口映射（2026-08-30 踩坑）

### 端口：DB 永远在 5433，不是 5432

`docker-compose.yml` 里 postgres 服务的端口映射是 `5433:5432` —— 宿主 5433 → 容器 5432。原因：宿主 5432 被 OpenMetadata 占用。所以：

```bash
# 元数据库（开发）
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata

# 测试库
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test
```

连 5432 = ConnectionRefused；连 5433 = 正常。

### SSOT 端口契约（2026-08-31 加固）

**问题**：容器内 / 容器外用同一个 `.env` 端口契约不一致——容器内 `postgres:5432`（Docker DNS 解析容器 service）、容器外 `localhost:5433`（宿主端口重映射）。`.env` 一份文件无法同时表达两种契约，过去靠口口相传，新人必然踩坑。

**SSOT 设计**：把容器内 / 容器外的端口契约分离到两个独立 `.env`，并通过 `docker-compose env_file` + `scripts/run_local.sh` 自动注入。**改端口只改一处**。

| 场景 | 配置文件 | URL 写法 | 注入方式 |
|---|---|---|---|
| 容器内（docker compose up） | `docker/.env` | `postgres:5432`（容器 service + 容器端口） | compose `env_file: .env` |
| 容器外（本机 uvicorn / 测试） | `backend/.env` | `localhost:5433`（宿主端口） | `scripts/run_local.sh` 自动 export |

**容器内变更**：把 `docker-compose.yml` 的 backend service 从硬编码 `environment: DATABASE_URL: ...` 改成 `env_file: .env`。`docker/.env` 是容器内端口契约 SSOT。

**容器外入口**：

```bash
# 推荐：用 wrapper 脚本（自动端口可达性预检 + SSOT 防御 + 注入正确端口）
./scripts/run_local.sh --port 8001 \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001

# 跑 alembic（自动用 5433）
./scripts/run_local.sh alembic upgrade head

# 跳过预检（如已知端口 OK）
./scripts/run_local.sh --no-preflight .venv/bin/uvicorn app.main:app --port 8001
```

**双层 fail-fast**：

1. `scripts/run_local.sh` 在启动前检查 `.env` 是否仍写 `localhost:5432` —— 是则拒绝运行并指向 SSOT 说明。
2. `app/main.py` 的 lifespan 启动预检：连不上 PG 时，如果 URL 是 `localhost:5432` / `127.0.0.1:5432`，打印精确端口契约提示（容器外 → 改 5433；容器内 → 改 `postgres:5432`）。

**未来改端口的口径**：

- 改 PG 宿主端口：只改 `docker-compose.yml` 的 `5433:5432` 映射 + `backend/.env` 的 `localhost:5433` + `scripts/run_local.sh` 提示文字。`docker/.env` 容器内端口 `postgres:5432` 不动。
- 改容器内 PG 端口：只改 `docker-compose.yml` 容器端口段 + `docker/.env` 的 `postgres:<新端口>`。宿主端口不动。

### Stub auth 默认 admin（2026-08-31 落地）

dev/test 下默认 stub user 带 `("user", "admin")` 角色，避免未登录访问 `/api/v1/features` 等带 owner-based ACL 的端点必 403。生产安全护栏不变：

- `AUTH_STUB_ENABLED=0` → 拒绝任何 `X-User-*` 头请求（`getCurrentUser` 入口校验）
- `APP_ENV=production` + stub 仍开启 → 启动时 ERROR 日志告警（`main.py`）
- 反向代理剥离 `X-User-*` 头是兜底（与 stub 模式无关）

测试非 admin 路径时显式设 `X-User-Roles: user` 即可（11 处现有集成测试已补）。详见 `Harness/changes/feat-acl-default-admin-stub/summary.md`。

### 迁移：`git pull` 后必须 alembic upgrade head

ORM 模型一旦新增表/列，必须有对应迁移文件。DB 与 ORM 不一致会出现两类故障：

1. **`alembic current` 落后于 head** —— 启动后端任何读取新表的请求 → `UndefinedTableError`，整 500。
2. **`SKIP_SCHEMA_CHECK=1` 静默放行** —— 启动校验被跳过，运行期才暴露，生产事故更难排。

**强制流程（每次 `git pull` 后 / 每次新增迁移文件后）：**

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  alembic upgrade head
```

**CI / 本地启动前置**（建议加进 pre-commit 或 docker-compose 的 backend 启动入口）：

```bash
if [ "$(alembic current 2>/dev/null | head -1)" != "0025_audit_immutability (head)" ]; then
  alembic upgrade head
fi
```

**新增迁移文件 checklist**（开发自检）：

- [ ] `alembic upgrade head` 在干净 DB 上能跑通
- [ ] `alembic downgrade -1` 能回滚（验证对称性）
- [ ] 集成测试用真实 PG（`localhost:5433/qa_metadata_test`）跑迁移 — 见 `Harness/rules/测试规范.md`
- [ ] 更新本 runbook 的「迁移与端口映射」段（如端口或流程变更）

### 真实事故回放（2026-08-30）

| 时间 | 现象 | 根因 |
|---|---|---|
| T+0 | 前端 F12 报 `/kpi-catalog` 404、`/entity-mappings` 500 | 上一台 uvicorn 进程（PID 34319）启动时间早于 `kpi_catalog.router` 注册，是个**陈旧进程** |
| T+5min | 杀掉陈旧进程并重启 → 两个端点都 500 | 重启用了 `localhost:5432`，Docker 没在那开端口 |
| T+10min | 端口改回 5433 → `UndefinedTableError: relation "kpi_catalog" does not exist` | DB 停在 Alembic 0020；后续 0021-0025（entity_mapping、kpi_catalog、audit_log、history、immutability trigger）从未应用 |
| T+12min | `alembic upgrade head` | 5 条迁移跑完，两个端点恢复 200 |

**事后动作（已落地）**：

1. 写本节「迁移与端口映射」，避免下次重复踩。
2. 启动 backend 之前必跑 `alembic upgrade head`（见上文强制流程）。
3. 排查端点 500 时，先看 `uvicorn` 日志的首个 traceback，再换端点 —— **不要只换端点不查日志**。

---

## 并发与韧性备忘（2026-09-19）

> **目的**：下次遇到「50/100 人并发降级」「LLM 持续故障」「要不要多 worker」等同类问题时，
> 先看本节确认当时是怎么定的、为什么不动、什么时候该动。
> 详细 SSOT 在 `Harness/changes/feat-chat-concurrency/summary.md`。

### 当前实测基线（2026-09-19，feat-chat-concurrency 落地后）

| 维度 | 配置 | 实测 |
|---|---|---|
| DB pool | `pool_size=20, max_overflow=10`（max 30） | 50 并发 GET /menu-config → 50/50 200, 0.38s |
| PG | `max_connections=200` | 50 并发后 idle 22 连接，自然保留 |
| LLM 闸 | `LLM_CONCURRENCY_LIMIT=20`（asyncio.Semaphore） | 50 人 → 20 in-flight + 30 排队等闸 |
| 限流 key | `RATE_LIMIT_KEY_STRATEGY=ip`（默认）/ `user_id`（50 人同一 NAT 改这个） | admin PUT 立即生效 |
| Fallback 退避 | tenacity `stop=2 wait_exponential(1,4)` + 429→retry / 401/403/400→直接抛 | 详见 `feat-chat-concurrency/summary.md` §chat_service |

### 决策一：多 worker 部署 → **暂不动**

**何时触发重新评估**：日活并发 ≥ **100 人** 或单 worker 进程 CPU/内存饱和。

**为什么现在不动**：

- 当前单 worker 跑 50 人实测 50/50 200 + 0.19s，还有 ~3 倍余量
- 多 worker → 进程内 `asyncio.Semaphore` **不再共享**（每 worker 独立），需要外部限流（Redis semaphore 或网关层限流）
- 多 worker → Alembic migration 并发抢锁风险（要加 advisory lock 或排他升级）
- 多 worker → 日志/指标/middleware 单例失效（如 `_bootstrapChatConcurrencyConfig` 跑 N 次）
- 多 worker → 内存占用 ×N（每个 worker ~150MB）

**如未来需要多 worker，步骤**：

1. 引入 Redis semaphore（如 `redis.asyncio.Semaphore` 或 `aiocache`），替换 `app/infrastructure/llm/concurrency.py` 的 `LLMConcurrencyManager`
2. DB pool 同步缩到 `pool_size / worker_count`（PG max_connections 上限保护）
3. alembic migration 加 advisory lock 或单独 worker 跑迁移
4. 日志格式加 worker_id 字段（uvicorn `--worker-id` + `logging` filter）
5. nginx 改 upstream 为 `least_conn` 或 `ip_hash`（已有 resolver 变量，见 `qa-system-nginx-upstream-ip-stale`）

**不要做**：
- ❌ 把 uvicorn workers 加到 4 但仍用进程内 Semaphore → 上限虚高 4 倍、LLM provider 必 429
- ❌ 加 gunicorn `--worker-class=uvicorn.workers.UvicornWorker` 但忘了设 `worker_tmpdir` → macOS /tmp 太小报错

### 决策二：熔断器 → **暂不引入**

**何时触发重新评估**：

- LLM provider 持续 5xx / 429 超过 **30 秒**
- fallback 也连挂（多层全断）
- 需要「保护上游」「自动恢复（半开探针）」而不是仅「延迟重试」

**为什么现在不动**：

- 当前 fallback 退避（tenacity 指数 1s~4s × 2 次）已覆盖「请求级瞬时故障」
- 熔断器（pybreaker / resilience4j / 自实现）解决的是「进程级状态机」（closed/open/half-open），需要持续观察 LLM 失败率
- 当前没有 LLM 失败率指标 → 熔断阈值（多少 % 失败开闸）拍脑袋没意义
- 多 worker 部署会让熔断状态机同步问题复杂化（先解 worker 再解 breaker）

**如未来需要熔断，步骤**：

1. 先加指标：`llm_request_total`, `llm_request_failed_total{provider,status}`（prometheus client）
2. 写一个滑动窗口失败率计算器（如 30s 内失败率 ≥ 50% 开启）
3. 用 `pybreaker` 包装 `_callWithFallback` 入口；或自实现 `CircuitBreaker`（避免额外依赖）
4. 状态持久化到 Redis（多 worker 共享）；单 worker 阶段用内存即可
5. 半开探针：开闸 30s 后放 1 个请求，成功 → 关闭，失败 → 重新开闸

**当前已有什么**：

- ✅ tenacity 退避（429/5xx 重试 + 指数退避 + reraise）
- ✅ fallback（按 weight 选备选模型，已在 `chat_service._callWithFallback`）
- ✅ Semaphore（LLM 并发上限）
- ❌ 熔断器（无）
- ❌ 失败率指标（无）

### 决策三：DB pool 调参 → **已动**（2026-09-19）

详见 `Harness/changes/feat-chat-concurrency/summary.md` §follow-up 1。

- `DB_POOL_SIZE`: 5 → **20**
- `DB_MAX_OVERFLOW`: 10 → **10**（不变）
- PG `max_connections`: 100 → **200**（docker-compose.yml `postgres.command`）
- migration `0081` 幂等 UPDATE 已 seed 的旧默认 5 → 20

### 出问题时的排查顺序

1. **看 `docker logs qa-backend 2>&1 | grep -E "并发配置|LLM_CONCURRENCY|max_conn"`** → 确认启动期读到的实际值
2. **`pg_stat_activity`** 看连接池是否打满（active 数 ≥ pool_size+max_overflow = 30 → 间隙）
3. **`curl /api/v1/admin/system-config`** 看 system_config 当前值与启动期值是否一致
4. **50 并发复现**（`python3 -c "import asyncio, aiohttp..."` 见 `feat-chat-concurrency/summary.md`）
5. **不要先调 LLM_CONCURRENCY_LIMIT** — 当前不是 LLM 闸问题，先看 DB pool / fallback 是否抛错
