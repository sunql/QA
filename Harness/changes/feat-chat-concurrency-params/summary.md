# feat-chat-concurrency-params — Chat 并发参数系统化

## 触发

2026-09-19 审计发现 `ChatService` 50 人并发会降级——根因是 3 个硬编码参数：

1. **DB 连接池** `pool_size=5 / max_overflow=10`（`database.py:33`）
2. **限流 key 策略** 固定 IP（`rate_limit.py` 的 `get_remote_address`）
3. **LLM 并发 Semaphore**（不存在，待 feat-chat-concurrency 后续计划）

本次交付 step 1：把前 2 个参数迁入 `system_config` 表，admin 可在线调整。

> LLM 并发上限（Semaphore + `LLM_CONCURRENCY_LIMIT` key）留 feat-chat-concurrency 完整计划里做。

---

## 变更内容

### 1. Alembic migration `0079_chat_concurrency_params.py`

新增 3 行到 `system_config`（幂等 `ON CONFLICT DO NOTHING`）：

| key | 默认 | 合法值 | 生效方式 |
|---|---|---|---|
| `DB_POOL_SIZE` | `"5"` | 正整数 | **重启容器** |
| `DB_MAX_OVERFLOW` | `"10"` | 非负整数 | **重启容器** |
| `RATE_LIMIT_KEY_STRATEGY` | `"ip"` | `ip` \| `user_id` \| `ip_user` | **立即生效**（admin PUT 钩子刷新缓存） |

description 字段用 `|` 分段：「含义 | 生效方式 | 合法值 | 默认值」，admin 页面直接展示。

### 2. `app/config.py`

新增 `dbPoolSize` / `dbMaxOverflow` Settings 字段（env 覆盖，默认 5/10）。
lifespan 启动期读 system_config 行覆盖 → 注入 `_db_pool_config`。
两个值可由 env var（启动期常量）或 system_config（启动期一次性读）设置，**system_config 优先**。

### 3. `app/infrastructure/database.py`

新增 module-level `_db_pool_config: dict`，`init_db_pool_config()` / `get_db_pool_config()` 公开 API。
`getEngine()` 创建 engine 时从 `_db_pool_config` 取值（不再硬编码）。

### 4. `app/infrastructure/rate_limit.py`

新增：
- `set_rate_limit_strategy(strategy)`：lifespan init / admin PUT 钩子调用，写入缓存
- `invalidate_rate_limit_key_cache()`：标记 TTL 过期（防御性保留）
- `get_rate_limit_strategy()`：请求路径只读 dict，无 IO
- `_extract_user_key(request)`：从 `request.state.user.userId` 取 userId，无 user → `"anonymous"`
- `_dynamic_key(request)`：根据 strategy 选 ip / user_id / ip_user

`_dynamic_key` 替代原 `get_remote_address` 作为 `limiter.key_func`。
TTL 30s 是防御性（防止 lifespan init 失败留脏 cache）；正常路径 cache 永远有效（lifespan + admin PUT 已显式覆写）。

### 5. `app/api/v1/system_config.py` PUT 钩子

新增 `_applyRuntimeSideEffect(key, value)`：
- `RATE_LIMIT_KEY_STRATEGY` → `set_rate_limit_strategy` 立即刷新
- `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` → 仅 warning 日志「需重启」
- 其他 key → 不触发

单调失败吞掉，不抛回 PUT 路由。

### 6. `app/main.py` lifespan init

新增 `_bootstrapChatConcurrencyConfig()`：
- 用临时 engine（默认 pool）一次性 SELECT 三个 key
- 写入 `_db_pool_config` + `_rate_limit_strategy_cache`
- DB 不可达 / 值非法 → logger.warning 后回退 Settings 默认值，**不阻塞启动**

---

## 测试

### 单元（新建 3 个）

- `app/tests/unit/test_rate_limit_dynamic_key.py`：15 个测试，三策略 + cache 行为 + limiter 接线契约
- `app/tests/unit/test_database_pool_config.py`：7 个测试，模块默认 + init 写入 + get_db_pool_config 返回副本
- `app/tests/unit/test_system_config_side_effect.py`：9 个测试，三类 key 副作用 + 异常吞掉

**结果**: 31 passed（这 3 个文件总和 29 + 既有 4 + 其它 2153 = 全套 2153 passed，1 failed 是预存 ADS 加权测试，与本次无关）。

### 真机冒烟（test DB）

```
BEFORE: {'pool_size': 99, 'max_overflow': 99} {'strategy': 'ip', 'fetched_at': 0.0}
AFTER : {'pool_size': 20, 'max_overflow': 10} {'strategy': 'user_id', 'fetched_at': 102841...}
```

DB 行覆盖生效：bootstrap 读 DB → 注入 _db_pool_config + cache。

非法值（`-7` / `garbage`）回退默认值 + warning 日志，**实际值不被坏值污染**。

---

## 部署

1. `cd backend && DATABASE_URL=... python -m alembic upgrade head`（prod 跑 prod URL）
2. `git commit`（你提交）+ `git push`
3. 后端镜像重建 + 容器 recreate（`./scripts/deploy_backend.sh` 或 `docker compose up -d --build backend`）
4. 启动日志确认：
   ```
   chat 并发配置（system_config 覆盖生效）: DB_POOL_SIZE=5 DB_MAX_OVERFLOW=10 RATE_LIMIT_KEY_STRATEGY=ip
   ```

---

## 不做（明示）

- **LLM Semaphore**：留 feat-chat-concurrency 完整计划
- **多 worker 场景的 rate_limit 共享存储**：当前 1 worker 无此问题
- **dynamic resize PG pool**：SQLAlchemy 不支持 runtime resize
- **跨 worker 一致 system_config 缓存**：进程内 dict，1 worker 场景无问题

---

## 风险

| 风险 | 缓解 |
|---|---|
| 容器重建前改 `DB_POOL_SIZE` 不生效 | description 写「需重启」，admin PUT 触发后端 warning 日志，admin UI 可选加「需重启」提示（本次未做） |
| `RATE_LIMIT_KEY_STRATEGY=ip_user` 时所有匿名用户共享 `*:anonymous` bucket | 设计权衡：admin 可改回 `ip` 或 `user_id` |
| 启动期 DB 抖动 → bootstrap 失败 | Settings/env 默认值兜底，不阻塞 uvicorn 启动 |
| 已有 rate_limit 单测使用 TestClient，monkeypatch set_rate_limit_strategy 顺序敏感 | 在 conftest 添加 cache reset fixture 防止用例间污染（如有需要） |

---

## 后续

- feat-chat-concurrency：建 `asyncio.Semaphore` 限 LLM 并发，新增 `LLM_CONCURRENCY_LIMIT` / `LLM_FALLBACK_COOLDOWN_SECONDS` 等 system_config key
- admin UI 提示「需重启」字段（可视化 DB pool 类的生效时机）