"""应用配置 - 基于 Pydantic Settings 从环境变量加载。

遵循不可变原则：Settings 实例创建后不应被修改。所有配置为只读字段。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.error_messages import MSG_RATE_LIMIT_REQUESTS_INVALID


class Settings(BaseSettings):
    """全局配置，从环境变量 / .env 加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== 运行环境 =====
    appEnv: str = Field(default="development", alias="APP_ENV")
    logLevel: str = Field(default="INFO", alias="LOG_LEVEL")

    # ===== PostgreSQL 元数据库 =====
    databaseUrl: str = Field(
        default="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5432/qa_metadata",
        alias="DATABASE_URL",
    )

    # ===== Neo4j 本体图 =====
    neo4jUri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4jUser: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4jPassword: str = Field(default="qa_neo4j_dev_2026", alias="NEO4J_PASSWORD")

    # ===== Milvus 向量库 =====
    milvusUri: str = Field(default="http://localhost:19530", alias="MILVUS_URI")
    milvusCollection: str = Field(default="ontology_embeddings", alias="MILVUS_COLLECTION")
    # wiki 知识条目向量同步总开关（feat-wiki-semantic-search）：关闭后写路径
    # 跳过向量 upsert/delete，语义检索仍可用（针对已回填的向量）。测试环境
    # 与无 embedding provider 的部署可设 false，避免每次 CRUD 等待超时。
    wikiVectorSyncEnabled: bool = Field(default=True, alias="WIKI_VECTOR_SYNC_ENABLED")

    # ===== 安全与限流 =====
    secretKey: str = Field(default="development-insecure-key-change-me", alias="SECRET_KEY")
    # 会话累计成本预算：超过后强制降级到最便宜模型。
    # 默认 1.0（0-3）：旧值 0.1 过低，少量调用后即降级，导致对话质量骤降。
    sessionBudget: float = Field(default=1.0, alias="SESSION_BUDGET")
    datasourceHostAllowlist: str = Field(default="", alias="DATASOURCE_HOST_ALLOWLIST")
    # 业务数据库读取时的硬行数上限：<= 0 表示关闭上限（fetchmany 传 None 返回全部行）。
    # 仅作为安全网（避免误写 `SELECT *` 拖垮后端），默认关闭以满足"取消智能问答结果条数限制"诉求；
    # 真要约束单次返回行数，请在 NL2SQL 计划层通过 `QueryPlan.rowLimit` 控制，或设置具体正整数。
    queryRowLimit: int = Field(default=0, alias="QUERY_ROW_LIMIT")
    queryTimeoutSeconds: int = Field(default=30, alias="QUERY_TIMEOUT_SECONDS")
    # 问题未限定任何范围（无时间表达、计划无过滤条件）的明细查询兜底行数。
    # <= 0 表示关闭该策略（行数完全交回 LLM 判断）。
    # 默认 0：取消"列出所有..."型无范围明细查询的强制 LIMIT 100，由 LLM 自由判断。
    nl2sqlNoScopeRowLimit: int = Field(default=0, alias="NL2SQL_NO_SCOPE_ROW_LIMIT")

    # ===== 认证模式（feat-user-auth，2026-09-20）=====
    # ``stub``（默认）：保持原有 stub header 行为；``X-User-Id`` 头继续生效，便于
    # 本地/自动化测试。生产部署必须设 ``real``，并由反向代理（nginx）剥离客户端
    # 传来的 ``Authorization`` / ``X-User-*`` 头保证安全（详见 Harness/wiki/operations-runbook.md）。
    # ``real``：所有 API 强制 ``Authorization: Bearer <jwt>``；无 Bearer → 403。
    # 启动期会校验 APP_ENV=production 是否误配 stub，是则 ERROR 日志告警。
    authMode: str = Field(default="stub", alias="AUTH_MODE")
    # 是否仍允许 stub 头解析（与 authMode 解耦）：``real`` 模式下也允许 stub 头，
    # 方便过渡期两套并存；生产部署时由反向代理 + APP_ENV 双重保险。
    authStubEnabled: bool = Field(default=True, alias="AUTH_STUB_ENABLED")
    # 当 authMode=real 时，stub 头是否仍允许（默认 false；测试或过渡期可设 true）
    allowStubWhenReal: bool = Field(default=False, alias="ALLOW_STUB_WHEN_REAL")
    # JWT HS256 配置（authMode=real 时必须 ≥32 字节；启动期校验）
    jwtSecret: str = Field(default="", alias="JWT_SECRET")
    jwtAlgorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwtTtlSeconds: int = Field(default=3600, alias="JWT_TTL_SECONDS")
    jwtIssuer: str = Field(default="qa-system", alias="JWT_ISSUER")
    jwtAudience: str = Field(default="qa-system-web", alias="JWT_AUDIENCE")
    # bcrypt rounds：12（OWASP 推荐上限，登录 ~250ms）
    bcryptRounds: int = Field(default=12, alias="BCRYPT_ROUNDS")
    # 防时间侧信道：登录失败时统一延迟（毫秒）
    authMinDelayMs: int = Field(default=200, alias="AUTH_MIN_DELAY_MS")

    # ===== Schema 发现 =====
    # 单数据源允许发现的表数量上限：本地导入/元数据发现的硬保护，防止超大 schema
    # 撑爆 introspection 响应体与缓存。大型 ERP（如 Sage X3 生产库 1600+ 表）可按需调高。
    # 注意：NL2SQL 提示词使用本体 schema（导入后手工维护的类）而非该 introspection 缓存，
    # 调高不会撑爆 NL2SQL 提示词，只会让 introspection / 预览响应体变大。
    schemaMaxTables: int = Field(default=3000, alias="SCHEMA_MAX_TABLES")

    # ===== PG 连接池（feat-chat-concurrency-params）=====
    # 元数据库 engine 创建时的 pool_size / max_overflow；运行时由 system_config 行
    # ``DB_POOL_SIZE`` / ``DB_MAX_OVERFLOW`` 覆盖（main.py lifespan 启动期一次性
    # 读取后注入 ``app.infrastructure.database._db_pool_config``）。两者仅作默认值：
    # 容器内如需自定义可通过 env var 覆盖，admin 通过 system_config 页面调则需重启。
    # 2026-09-19 bump：50 并发场景下旧默认 5/10=15 max 会导致 35 请求排队。
    # 20/10=30 max 给 DB 留出 headroom，配合 PG max_connections=200（docker-compose）。
    # 配合 migration 0081 把已 seed 的旧默认 5 升到 20（幂等：仅 value='5' 时改）。
    dbPoolSize: int = Field(default=20, alias="DB_POOL_SIZE")
    dbMaxOverflow: int = Field(default=10, alias="DB_MAX_OVERFLOW")

    # ===== LLM 并发上限（feat-chat-concurrency）=====
    # 全局 ``asyncio.Semaphore`` 的 limit，控制同时 in-flight 的 LLM HTTP 调用数。
    # 运行时由 system_config 行 ``LLM_CONCURRENCY_LIMIT`` 覆盖（lifespan 启动期注入
    # + admin PUT 主动 reload）。env 可覆盖默认值，但 admin 在线调整无需重启。
    llmConcurrencyLimit: int = Field(default=20, alias="LLM_CONCURRENCY_LIMIT")

    # ===== LLM: OpenAI / Azure =====
    openaiApiKey: str = Field(default="", alias="OPENAI_API_KEY")
    openaiBaseUrl: str = Field(default="", alias="OPENAI_BASE_URL")
    azureOpenaiApiKey: str = Field(default="", alias="AZURE_OPENAI_API_KEY")
    azureOpenaiEndpoint: str = Field(default="", alias="AZURE_OPENAI_ENDPOINT")
    azureOpenaiApiVersion: str = Field(default="2024-08-01-preview", alias="AZURE_OPENAI_API_VERSION")

    # ===== LLM: 国内 OpenAI 兼容代理 =====
    deepseekApiKey: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseekBaseUrl: str = Field(default="https://api.deepseek.com/v1", alias="DEEPSEEK_BASE_URL")
    qwenApiKey: str = Field(default="", alias="QWEN_API_KEY")
    qwenBaseUrl: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1", alias="QWEN_BASE_URL"
    )

    # ===== LLM: Ollama =====
    ollamaBaseUrl: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")

    # ===== LLM: HTTP 客户端 =====
    # 默认 trust_env=False 绕过系统/环境代理（macOS 系统代理曾劫持 localhost 请求导致 502）。
    # 需经 HTTPS_PROXY 或 SSL_CERT_FILE 访问外部 LLM 的部署须显式设 LLM_TRUST_ENV=true。
    llmTrustEnv: bool = Field(default=False, alias="LLM_TRUST_ENV")

    # ===== Embedding（Phase 5 相似问答检索）=====
    embeddingModel: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    embeddingApiBase: str = Field(default="", alias="EMBEDDING_API_BASE")
    embeddingApiKey: str = Field(default="", alias="EMBEDDING_API_KEY")
    # Docker 容器内访问宿主机本地模型服务的主机覆盖（如 host.docker.internal）：
    # 设置后 EmbeddingClient 将 base_url 中的 localhost/127.0.0.1 改写为该值，registry
    # 激活 provider 与 env 回退两条路径一致生效。宿主机直接部署时留空。
    embeddingHostOverride: str = Field(default="", alias="EMBEDDING_HOST_OVERRIDE")

    # ===== 限流（Phase 5.5）=====
    rateLimitEnabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rateLimitRequests: int = Field(default=30, alias="RATE_LIMIT_REQUESTS")
    rateLimitWindow: str = Field(default="minute", alias="RATE_LIMIT_WINDOW")

    # ===== CORS =====
    corsOrigins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173", alias="CORS_ORIGINS"
    )

    @field_validator("rateLimitRequests")
    @classmethod
    def _validateRateLimitRequests(cls, value: int) -> int:
        if value < 1:
            raise ValueError(MSG_RATE_LIMIT_REQUESTS_INVALID)
        return value

    # ===== 路由策略 =====
    sessionAffinityTurns: int = Field(default=3, alias="SESSION_AFFINITY_TURNS")
    nl2sqlMaxRetries: int = Field(default=2, alias="NL2SQL_MAX_RETRIES")

    @property
    def isProduction(self) -> bool:
        return self.appEnv == "production"

    @property
    def datasourceHosts(self) -> list[str]:
        """解析数据源主机白名单；为空表示不限制。"""
        if not self.datasourceHostAllowlist.strip():
            return []
        return [h.strip() for h in self.datasourceHostAllowlist.split(",") if h.strip()]

    @property
    def corsOriginList(self) -> list[str]:
        """解析 CORS 允许来源列表（逗号分隔）。

        生产环境强制为空：前端由 Nginx 反代同源承载，拒绝一切跨域请求。
        非生产默认允许本地 Vite dev server（localhost/127.0.0.1:5173）。
        使用显式 origin 而非 "*"：allow_origins=["*"] 与 allow_credentials=True 组合
        违反 CORS 规范，浏览器会直接拒绝 SSE 等跨域请求。
        """
        if self.isProduction:
            return []
        return [origin.strip() for origin in self.corsOrigins.split(",") if origin.strip()]


@lru_cache
def getSettings() -> Settings:
    """返回缓存的只读 Settings 单例。"""
    return Settings()
