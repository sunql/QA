# 变更：Embedding 模型服务注册与切换机制

- **日期**：2026-08-13
- **Phase**：Phase 5 扩展
- **状态**：done

## 1. 需求

embedding 配置此前硬编码在环境变量（`EMBEDDING_MODEL` / `EMBEDDING_API_BASE` /
`EMBEDDING_API_KEY`），`EmbeddingClient` 只读全局 `Settings`。需要**可管理、可切换**
的 embedding 服务：初始化 oMLX 的 `bge-m3-mlx-fp16`、`bge-m3-mlx-8bit` 与 Ollama 的
`bge-m3:latest` 三个模型，后期可增删改并指定当前用哪个。默认激活 Ollama bge-m3。

## 2. 设计评审

- 存储选型：数据库表 `embedding_provider` + CRUD API（用户决策），而非纯环境变量。
- 单活互斥：`is_active` 至多一行 True，复用 `datasource_service._clearOtherDefaults`
  模式；激活切换经 API 触发并失效 resolver 缓存。
- 运行时解析：新 resolver `embedding_provider_factory` 从激活 provider 解析
  `EmbeddingClient`，带缓存与失效；无激活 provider（或 DB 抖动）时回退环境变量，
  保持「未启用 registry」部署兼容。
- 维度守卫：激活 provider 的 `dimension` 与 Milvus 集合维度不一致时 **fail-fast 报错**
  （而非静默插入维度错误数据），提示重建集合 + 回填。
- 免鉴权本地服务：`api_key_encrypted` 允许 NULL；openai SDK 拒绝空 `api_key`，故显式
  base_url + 空 key 时注入占位 key。**注意**：本机 oMLX 实际开启了鉴权（`~/.omlx/settings.json`
  的 `auth.api_key`，key 值不落代码），seed 的 NULL key 需经 CRUD 加密写入真实 key；Ollama
  仍免鉴权。写真实 key 要求运行时 SECRET_KEY 为合法 Fernet key（dev 默认值非法则加密失败）。
- 密钥：Fernet 加密复用 `crypto.py`；`api_key_encrypted` 因 dev 默认 SECRET_KEY 非法
  Fernet key 而 **不** 在迁移中加密种入（种 NULL），真实 key 走 CRUD API 写入。

## 3. 数据模型变更

- 新增表 `embedding_provider`：
  `id`(BigInt PK) / `name`(varchar100, unique) / `provider_type`(varchar30) /
  `base_url`(varchar255) / `model_name`(varchar100) / `api_key_encrypted`(varchar512,
  nullable) / `dimension`(int, default 1024) / `is_active`(bool, default false) /
  `created_time` / `updated_time`（timestamptz）。
- 迁移：
  - `0012_embedding_provider`：建表 + `uq_embedding_provider_name`。
  - `0013_seed_embedding_providers`：种入 3 条（oMLX FP16 / 8bit 非激活，Ollama
    bge-m3 激活，`base_url` 分别为 `localhost:8080`、`localhost:11434`，均 1024 维，
    key 为 NULL）。**实际部署修正**：本机 oMLX 端口为 8888 且需鉴权，经 CRUD 将
    oMLX 两个 provider 的 base_url 改为 `localhost:8888`、apiKey 加密写入（见 §10 冒烟）。
  - `0014_embedding_provider_active`：部分唯一索引 `uq_embedding_provider_active`
    (`is_active WHERE is_active`)——**单活互斥的 DB 层兜底**，并发激活冲突由 service
    捕获 IntegrityError 转 422。

## 4. 接口契约变更

新增 `/api/v1/embedding-providers`（前缀挂载于 `main.py` / `_testapp.py`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 列表（`list[EmbeddingProviderRead]`） |
| POST | `/` | 201 创建（`isActive` 置 True 自动取消其他激活） |
| GET | `/active` | 当前激活（无激活 404；**声明在 `/{providerId}` 之前**） |
| GET | `/{providerId}` | 按 id 获取 |
| PUT | `/{providerId}` | 部分更新（`apiKey` 重新加密；置激活自动互斥） |
| POST | `/{providerId}/activate` | 设为唯一激活 |
| DELETE | `/{providerId}` | 204 软删除（取消激活） |

DTO：`EmbeddingProviderCreate/Update/Read`（camelCase）；**Read 不含 `apiKey`/`apiKeyEncrypted`**。

## 5. 实现要点

- `app/domain/models.py`：`EmbeddingProvider(Base, TimestampMixin)`。
- `app/domain/schemas.py`：三个 DTO + `MSG_SCHEMA_EMBEDDING_PROVIDER_*` 描述。
- `app/services/embedding_provider_service.py`：CRUD + `_clearOtherActives` 单活互斥 +
  `await invalidateEmbeddingClientCache()`；`update` 补 name 唯一校验（防 500）；
  `_commitOrConflict` 捕获并发唯一约束冲突转 422；`api_key` 显式置 null 视为清除 key。
  代码审查（code-reviewer，0 CRITICAL / 0 HIGH）后的 4 个 MEDIUM 修复：
  1. update 改名重名 500 → 422
  2. 单活互斥 DB 层未兜底 → 迁移 0014 部分唯一索引 + IntegrityError 转 422
  3. resolver 吞 SQLAlchemyError 掩盖缺表等配置错误 → 按 OperationalError（回退 env）
     与其余 SQLAlchemyError（fail-fast）分级
  4. ConfigError 逃逸 fire-and-forget 存储路径 → storeQueryEmbedding 捕获 DomainError
  另修 2 个 LOW：shutdown 关闭 resolver 缓存客户端；api_key null 清除。
- `app/infrastructure/llm/embedding_provider_factory.py`（新）：`getActiveEmbeddingClient()`
  带缓存解析；`invalidateEmbeddingClientCache()`（async，关旧客户端）/
  `resetEmbeddingClientCache()`（测试用，不关连接）；DB 查询失败回退环境变量并告警；
  `_assertDimensionMatches` 维度守卫。
- `app/services/embedding_service.py`：`_ensureClient` 改 async，注入客户端优先，否则走
  resolver；三个调用点改为 `(await self._ensureClient()).embed(...)`。`close()` 只关注入
  客户端（resolver 客户端归 factory 失效时关闭）。
- `app/infrastructure/llm/embedding_client.py`：缺 key 守卫放宽为「无 key 且无 base_url」
  才报错；构造 SDK 时 `api_key or _KEYLESS_PLACEHOLDER`（占位 key 满足 openai SDK 非空校验）。
- `app/infrastructure/milvus_client.py`：新增 `getEmbeddingDimension()` 供守卫读取。
- `app/api/v1/embedding_provider.py` + 两处挂载（`main.py`、`tests/_testapp.py`）。

## 6. 测试

- `app/tests/integration/test_embedding_provider_api.py`（15 测试）：CRUD、重名 422、
  update 改名重名 422、api_key null 清除落库、`GET /active` 404/命中、**单活互斥**
  （activate 顶替、create isActive 顶替、update 置激活顶替）、DB 部分唯一索引拒双激活、
  软删除 204、无 key 泄漏。
- `app/tests/unit/test_embedding_provider_factory.py`（9 测试）：无激活回退环境变量、
  按 provider 构建、空 key 本地模型、缓存命中不重复查 DB、invalidate 重新解析、维度不匹配
  抛 ConfigError、OperationalError 回退 env、ProgrammingError fail-fast。
- `app/tests/unit/test_embedding_service.py`：补充 fire-and-forget 吞 ConfigError。
- 回归：**661 passed**（此前 637，净增 24）。

## 7. 安全审查

- 未硬编码 secrets：seed key 置 NULL；真实 key 由 CRUD API 经 Fernet 加密写入，
  SECRET_KEY 仅从环境变量/docker .env 读取（dev 默认值非法 Fernet key 时加密路径
  显式报错，符合 fail-fast）。
- Read DTO 不返回 `apiKey`/`apiKeyEncrypted`，与 `LlmConfigRead` 同模式。

## 8. 部署验证

- dev DB `alembic upgrade head` 已应用；`embedding_provider` 3 条 seed 存在，Ollama 激活。
- 冒烟（`:8000`，--reload 已加载新代码）：
  - `GET /api/v1/embedding-providers` → 3 条；`/active` → Ollama bge-m3。
  - `ontology/search?q=供应商收货数量` → 命中 Supplier/Receipt/ReceiptDetail（走激活
    provider，1024 维，不回退全量）。
  - 维度守卫：PUT `dimension=1536` → search 报 `MSG_EMBEDDING_PROVIDER_DIMENSION_MISMATCH`；
    还原 1024 → 恢复。
  - 激活切换：activate oMLX FP16 → `/active` 变更；切回 Ollama → oMLX 自动取消激活。

## 9. 关联

- 任务：#51/#52/#53/#54/#55/#56/#57
- 前置：`fix-semantic-class-retrieval`（Milvus 1024 维重建 + 回填）
- Wiki：`Harness/wiki/`（NL2SQL 引擎、模型路由）
- 后续：前端管理页已完成，见 `Harness/changes/feat-embedding-provider-frontend/`。

## 10. Docker 部署配置（追加，2026-08-13）

Docker 容器内 `localhost` 指向容器自身，宿主机的 Ollama / oMLX 需经 `host.docker.internal`
访问；而 `embedding_provider` seed 与 env 的 base_url 均以 `localhost` 作规范写法。

**方案：部署级主机覆盖 `EMBEDDING_HOST_OVERRIDE`**，不改库、不改 seed：

- `config.py` 新增 `embeddingHostOverride`（`EMBEDDING_HOST_OVERRIDE`，默认空）。
- `embedding_client.py` 解析 base_url 后，将 `localhost` / `127.0.0.1` 主机改写为覆盖值
  （非 loopback 的真实远端端点不改写；override 为空时原样返回）。registry 激活 provider
  与 env 回退两条路径一致生效。`getattr(settings, ..., "")` 兼容测试注入的假 Settings。
- `docker/docker-compose.yml`：backend 透传 `EMBEDDING_MODEL/API_BASE/API_KEY/HOST_OVERRIDE`
  与 `LLM_TRUST_ENV`；新增 `extra_hosts: host.docker.internal:host-gateway`（Linux Docker
  需显式 host-gateway，macOS/Windows Desktop 自动解析，映射兼容两者）。
- `docker/.env`（及 `.env.example`）：`EMBEDDING_MODEL` 由 `text-embedding-3-small`（1536 维，
  与 1024 集合矛盾）改为 `bge-m3:latest`（1024 维）；`EMBEDDING_API_BASE` 指向
  `http://host.docker.internal:11434/v1`；`EMBEDDING_HOST_OVERRIDE=host.docker.internal`；
  `LLM_TRUST_ENV=false`。
- 测试：`test_embedding_service.py` 新增 5 个单测（hostname/IPv4/大写 loopback 改写、
  远端端点不动、空 override no-op）。回归 661 → **666 passed**。
- code-reviewer（0 CRITICAL / 0 HIGH）：修 1 个 MEDIUM——`str.replace` 对 `LOCALHOST`
  大写写法静默不改写，改为大小写不敏感定位后在原 netloc 上替换（`_replaceHost`）；
  保留 2 个 LOW（IPv6 `::1`、完整 127.0.0.0/8 不在本次范围，seed/部署均为 localhost）。

### 冒烟：oMLX 激活（2026-08-13）

- 重启两实例（`:8000` override 空 / `:18080` override=127.0.0.1）并注入合法 SECRET_KEY
  （来自 docker/.env）——dev 默认 SECRET_KEY 非法，加密 apiKey 会失败。
- oMLX 8888 全占位 key 401，确认需鉴权；`auth.api_key` 取自 `~/.omlx/settings.json`
  （不落代码），经 CRUD `apiKey` 字段 Fernet 加密写入 DB。
- `PUT` 两个 oMLX provider 的 base_url → `http://localhost:8888/v1` + apiKey；
  `POST /1/activate` → `/active` 切至 oMLX FP16，Ollama 自动取消激活（单活互斥）。
- `ontology/search?q=供应商收货数量` 在 `:8000`（直连 localhost:8888）与 `:18080`
  （override 改写为 127.0.0.1:8888）均命中（score≈0.53，与 Ollama 略有差异属正常）。
- 切回 Ollama：`POST /api/v1/embedding-providers/3/activate`。
