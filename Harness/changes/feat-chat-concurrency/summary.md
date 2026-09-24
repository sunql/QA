# feat-chat-concurrency: LLM 并发治理

## 触发

step 1（`feat-chat-concurrency-params`）已把 DB pool / 限流 key 迁入 system_config。
本 step 解决 LLM 并发层的根因：

1. **无并发上限**：`factory.py` 全程无 `asyncio.Semaphore`，50 人并发瞬间全打 LLM → 429 → fallback 立刻重打 → retry storm。
2. **fallback 无退避**：`_callWithFallback` 在 `LlmClientError` 后立刻调 fallback，等于同时把所有失败请求重打备选模型。
3. **fallback 重试判定过严**：原实现对任何 `LlmClientError` 立刻降级，无法过滤 4xx 永久错误（401/403/400）造成无谓浪费。

## 变更内容

### 1. Alembic migration `0080_llm_concurrency_config.py`

新增 `system_config` key：

| key | 默认 | 合法值 | 生效方式 |
|---|---|---|---|
| `LLM_CONCURRENCY_LIMIT` | `"20"` | 正整数 | **立即**（reload Semaphore） |

幂等 `ON CONFLICT DO NOTHING`。

### 2. `app/config.py`

新增 `llmConcurrencyLimit` Settings 字段（env 覆盖，默认 20）。

### 3. `app/infrastructure/llm/concurrency.py`（新建独立模块）

`LLMConcurrencyManager` 类 + `acquire_llm_concurrency` async context manager。
**为什么拆到独立模块**：`factory.py` ↔ `openai_client.py` ↔ `ollama_client.py` 三方互相依赖
（`factory` 创建客户端 → 客户端的 HTTP 调用又需要 `acquire_llm_concurrency`）。
把 Semaphore 单独放 `concurrency.py` 避开循环 import。
`factory.py` re-export 公共 API，旧 `from app.infrastructure.llm.factory import acquire_llm_concurrency`
调用路径不变。

### 4. `factory.py`（仅 re-export）

```python
from app.infrastructure.llm.concurrency import (  # noqa: F401
    LLMConcurrencyManager, acquire_llm_concurrency,
    get_llm_concurrency_manager, reload_llm_concurrency_limit,
    reset_llm_concurrency_manager,
)
```

### 5. `openai_client.py` / `ollama_client.py`

5 个 HTTP 调用点全部包 `async with acquire_llm_concurrency():`：
- `OpenAiClient.complete` / `completeStream` / `complete_with_tools`
- `OllamaClient.complete` / `completeStream`

异常路径由 `async with` 语义保证 release，无计数泄漏。

### 6. `chat_service.py` `_callWithFallback` 退避

- 新增 `_isRetryableLlmError(exc) -> bool`：默认 True（兼容旧 fallback 行为），仅当 `__cause__` 携带**确定性** 4xx status_code（401/403/400）时返回 False；429 视为 rate limit（4xx 但属临时故障）仍 retry。
- 新增 `_callWithRetryBackoff(caller, fallback)`：tenacity `@AsyncRetrying` 包装，最多 2 次尝试，指数退避 1s~4s，retry_if_exception_type=LlmClientError，reraise=True。
- `_callWithFallback` 改用新判定：永久错误直接抛（保留原始 traceback），可重试错误走 fallback + 退避。

### 7. `main.py` lifespan bootstrap

`_bootstrapChatConcurrencyConfig` 追加读取 `LLM_CONCURRENCY_LIMIT` 行 + 调 `reload_llm_concurrency_limit`。
启动日志改为 4 字段拼接：
```
chat 并发配置（system_config 覆盖生效）: DB_POOL_SIZE=20 DB_MAX_OVERFLOW=30 RATE_LIMIT_KEY_STRATEGY=user_id LLM_CONCURRENCY_LIMIT=15
```

### 8. `system_config.py` PUT 钩子

新增 `LLM_CONCURRENCY_LIMIT` 分支：合法值校验（正整数）后调 `reload_llm_concurrency_limit`；非法值仅 warning 不抛错。

---

## 测试

### 单元（新建 2 个 + 扩展 1 个）

- `test_llm_concurrency_manager.py`（16 个测试）：
  - LLMConcurrencyManager 构造校验（正整数）
  - acquire/release/异常路径 release
  - reload 原子替换 + 旧引用保留
  - 单例模式 + Settings 默认值
  - 接线契约：OpenAI/Ollama 5 个 HTTP 入口都包了 `acquire_llm_concurrency`
- `test_fallback_backoff.py`（14 个测试）：
  - `_isRetryableLlmError` 判定矩阵（429→True，401/403/400→False，Nl2SqlError→True，无关异常→False）
  - `_callWithRetryBackoff` 行为（首次成功、首败后成功、退避耗尽仍失败）
- `test_system_config_side_effect.py` 扩展 4 个测试：
  - LLM_CONCURRENCY_LIMIT 合法/非法/None/0 各种边界

**结果**：34 个新单测全过；完整 unit 套件 **2187 passed, 1 failed**（预存的 ADS 加权测试，与本次无关，memory 已记录）。

### 真机冒烟（test DB）

```
INSERT 4 行 (DB_POOL_SIZE=20, DB_MAX_OVERFLOW=30, RATE_LIMIT_KEY_STRATEGY=user_id, LLM_CONCURRENCY_LIMIT=15)
AFTER bootstrap:
  pool= {'pool_size': 20, 'max_overflow': 30}
  strategy= {'strategy': 'user_id', 'fetched_at': 104377.966829}
  llm_limit= 15
```

4 个 key 全部从 DB 正确读取并注入到对应 module-level 缓存。

---

## 50 人并发预估（更新）

| 机制 | 效果 |
|---|---|
| `LLM_CONCURRENCY_LIMIT=20` Semaphore | 50 请求 → 20 in-flight + 30 排队等闸，不会瞬间打 50 个 LLM 调用 |
| fallback 退避 1s~4s | 429 失败不再立即重打备选模型；failback 自身仍失败时退避耗尽 reraise |
| 默认 retryable | 旧 fallback 行为不变：合成错误（如测试）仍触发降级 |
| DB pool 5/10 = 15 | **仍未调大**——本次只动 LLM 层；DB pool 调大留 follow-up（见下） |

**50 人预估**：LLM 层不再 retry storm，延迟更可预测。剩余瓶颈在 DB pool（35 请求排队等池），需要把 `DB_POOL_SIZE` 调到 20+ 配合 PG `max_connections` 调高（不在本 step 范围）。

---

## 部署

1. `cd backend && DATABASE_URL=... python -m alembic upgrade head`
2. `git commit`（你提交）+ `git push`
3. 后端镜像重建 + 容器 recreate
4. 启动日志确认 4 字段：
   ```
   chat 并发配置（system_config 覆盖生效）: DB_POOL_SIZE=5 DB_MAX_OVERFLOW=10 RATE_LIMIT_KEY_STRATEGY=ip LLM_CONCURRENCY_LIMIT=20
   ```

---

## 不做（明示）

- **DB pool 调大（5/10 → 20/10）**：本次不动，留作 follow-up；同步需要 PG `max_connections` 调高（docker-compose.yml 加 `command: ["-c", "max_connections=200"]`），需重启 PG 容器（数据不丢）
- **Ollama TCPConnector**：本机推理保护，与 Semaphore 是两层独立机制
- **pybreaker 引入**：tenacity 已有，轻量退避 + reraise 已覆盖 90% 场景
- **多 worker LLM 共享闸**：进程内 Semaphore，多 worker 部署需外部限流（Redis semaphore）——留作后续

---

## 风险

| 风险 | 缓解 |
|---|---|
| `LLM_CONCURRENCY_LIMIT` 过大（>LLM provider RPM 上限）仍会被 429 | fallback 退避 + 503/429 触发限流时降级到备选模型 |
| `LLM_CONCURRENCY_LIMIT` 过小（<5）并发响应慢 | admin 可在线调大，无需重启 |
| `_isRetryableLlmError` 默认 True → fallback 在 4xx 错误时浪费一次 | 仅当 `__cause__` 有 status_code 才拦截；SDK/aiohttp 异常都带 status_code，生产误伤罕见 |
| bootstrap 读 DB 失败 | Settings/env 默认值兜底，不阻塞启动 |
| 旧调用方 `from app.infrastructure.llm.factory import acquire_llm_concurrency` | factory re-export，旧路径仍可用 |

---

## 后续（follow-up）

1. **DB pool 调大**：production 50 并发时 35 请求排队，调 `DB_POOL_SIZE=20/DB_MAX_OVERFLOW=10`，PG `max_connections=200`。需要用户拍板 PG 容器重启。 ✅ **2026-09-19 落地**（见下文 follow-up 1 落地段）
2. **多 worker 部署评估**：当前单 worker 跑 50 人靠 async I/O 多路复用；如需垂直扩展到 2-4 worker，需 Redis semaphore 共享 LLM 闸。
3. **熔断器**（pybreaker 或自实现）：当前 tenacity 仅做退避，未做半开/全开状态机；可观察 LLM 持续故障 30s 后短路所有请求直接 503。

---

## follow-up 1 落地（2026-09-19，DB pool + PG max_connections）

### 触发
50 并发场景下旧配置 `pool_size=5, max_overflow=10`（max 15 连接）会排队 35 个请求，
latency spike。PG 默认 `max_connections=100`，元库池满后还可能撞 `remaining connection slots` 错。

### 变更

#### `docker/docker-compose.yml`
- `postgres` service 增 `command: ["postgres", "-c", "max_connections=200"]`
- 注释明示 PG 容器需重启生效 + 数据保留 + 回退方法（删 command 行）

#### `backend/app/config.py`
- `dbPoolSize` 默认 5 → **20**
- 注释追加「2026-09-19 bump」说明 + 配合 `0081` + `docker-compose` 协同

#### `backend/alembic/versions/0081_db_pool_size_tune.py`（新建）
- 幂等 UPDATE：`system_config WHERE key='DB_POOL_SIZE' AND value='5'` → 设为 '20'
- 同时把 description 字面量「默认: 5」刷成「默认: 20」（`replace()` 文本替换）
- 对称 downgrade：value='20' → '5'，description 反向
- 仅当行还是旧默认时才动 → 不覆盖任何手动调整

#### `backend/alembic/versions/0079_chat_concurrency_params.py`（**未改**）
历史 seed 不动；只动 `0081` 的 UPDATE 路径

### 部署步骤

```bash
# 1. PG 容器重建（max_connections 必须重启生效；数据保留）
docker compose -f docker/docker-compose.yml up -d postgres

# 2. 备份（手动跑）
./scripts/backup_pg.sh
./scripts/backup_pg.sh --db qa_metadata_test

# 3. 迁移（prod + test 都跑；幂等）
cd backend
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata' python -m alembic upgrade head
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' python -m alembic upgrade head

# 4. 重建后端镜像（config.py 默认变了）
docker compose -f docker/docker-compose.yml up -d --build backend
```

### 验证

- `SHOW max_connections;` → `200` ✓
- 启动日志：`DB_POOL_SIZE=20 DB_MAX_OVERFLOW=10 RATE_LIMIT_KEY_STRATEGY=ip LLM_CONCURRENCY_LIMIT=20` ✓
- `app.infrastructure.database.get_db_pool_config()` → `{'pool_size': 20, 'max_overflow': 10}` ✓
- `LLMConcurrencyManager._semaphore._value` → 20 ✓
- 50 并发 GET `/api/v1/menu-config` → **50/50 200, 0.38s** ✓
- 50 并发 GET `/api/v1/admin/system-config` → **50/50 200, 0.19s** ✓
- 50 并发后 `pg_stat_activity WHERE datname='qa_metadata'` → 22 idle connections（pool 20 + 2 overflow 自然保留）✓

### 风险

| 风险 | 缓解 |
|---|---|
| PG `max_connections=200` 占用更多 shared memory | 默认 `shared_buffers` + `max_connections=200` 资源充足；如未来再调高，需同步调 `shared_buffers` |
| 容器重建期间 PG 短暂不可用 | healthcheck + `depends_on: condition: service_healthy`，后端自动重连；前端 1-2s 闪断 |
| alembic 0081 误覆盖手工调整 | UPDATE WHERE `value='5'` 严格限定；描述也用 replace 不重写整字段 |
| 配置默认改了但旧 admin-PUT 行仍是 5 | bootstrap 优先读 system_config 行；若 admin 已 PUT 过 5，需手动 PUT 到 20 |

### 50 并发预估（最终）

| 机制 | 效果 |
|---|---|
| `DB_POOL_SIZE=20` + `max_overflow=10` = 30 max | 50 请求 → 30 in-flight + 20 排队；远超单 PG 连接开销预算 |
| `PG max_connections=200` | 30 元库 + 业务 DB × N + alembic 临时连接 + admin < 100，留出 100 余量 |
| `LLM_CONCURRENCY_LIMIT=20` Semaphore | 50 请求 → 20 LLM in-flight + 30 排队等闸 |
| fallback 退避 1s~4s | 429 不再立即重打备选模型 |

**之前担心的 50 人瓶颈（DB pool 排队）已落地解除。**