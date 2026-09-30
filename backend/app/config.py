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
    # Milvus database（逻辑库）名；空 = 默认库 "default"。
    # 用途：测试套件把它指向独立测试库（qa_test），让 drop/重建只作用于测试数据。
    # 此前 integration 的 milvusCleanClient 夹具 drop 的是**默认库里的生产本体集合**，
    # 一次裸 pytest 就把线上向量删空（2026-09-30 事故）。
    milvusDbName: str = Field(default="", alias="MILVUS_DB_NAME")
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
    # ===== Schema 发现 =====
    # 单数据源允许发现的表数量上限：本地导入/元数据发现的硬保护，防止超大 schema
    # 撑爆 introspection 响应体与缓存。大型 ERP（如 Sage X3 生产库 1600+ 表）可按需调高。
    # 注意：NL2SQL 提示词使用本体 schema（导入后手工维护的类）而非该 introspection 缓存，
    # 调高不会撑爆 NL2SQL 提示词，只会让 introspection / 预览响应体变大。
    schemaMaxTables: int = Field(default=3000, alias="SCHEMA_MAX_TABLES")

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
    # env 回退路径的输出维度**声明**（H7）：env 路径没有 registry 那样的 dimension
    # 元数据，只有模型名，而「模型名 → 维度」无可靠映射 ⇒ 不猜。声明后 resolver 才会
    # 与 Milvus 集合维度比对并 fail-fast；不声明则仅记 warning（不静默）。
    embeddingDimension: int | None = Field(default=None, alias="EMBEDDING_DIMENSION")

    # ===== 限流（Phase 5.5）=====
    rateLimitEnabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rateLimitRequests: int = Field(default=30, alias="RATE_LIMIT_REQUESTS")
    rateLimitWindow: str = Field(default="minute", alias="RATE_LIMIT_WINDOW")

    # ===== CORS =====
    # 默认值覆盖常见 dev 来源：localhost / 127.0.0.1 / 局域网子网 192.168.x.x /
    # 10.0.x.x / 172.16-31.x.x（Docker Desktop 主机回环 192.168.65.x 兼容）。
    # 显式 allowlist（非 "*"）的原因：allow_credentials=True 与 "*" 组合违反 CORS 规范，
    # 浏览器会拒绝 SSE 等跨域请求。
    # 生产仍走 isProduction 分支返回 []，由 Nginx 反代同源承载。
    corsOrigins: str = Field(
        default=(
            "http://localhost:5173,"
            "http://127.0.0.1:5173,"
            "http://192.168.50.26:5173"
        ),
        alias="CORS_ORIGINS",
    )

    # ===== RBAC / Auth (feat-user-auth) =====
    authMinDelayMs: int = Field(default=200, alias="AUTH_MIN_DELAY_MS")
    """登录失败时的等长延迟（毫秒），用于拖慢枚举攻击；0=禁用。"""
    bcryptRounds: int = Field(default=10, alias="BCRYPT_ROUNDS")
    """bcrypt 哈希轮数；当前默认 10（生效值，与本文件唯一声明一致）；生产建议 ≥ 12，
    抬升需显式设 BCRYPT_ROUNDS（bcrypt 校验自带成本参数，旧哈希不受影响）。"""
    jwtTtlSeconds: int = Field(default=86400, alias="JWT_TTL_SECONDS")
    """access token 有效期（秒）；默认 24h。"""
    jwtSecret: str = Field(default="development-jwt-secret-change-me", alias="JWT_SECRET")
    """JWT 签名密钥（HS256/HS512）；生产必须 ≥ 32 字节随机串。"""
    jwtAlgorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    """JWT 签名算法；HS256/HS384/HS512。"""
    jwtIssuer: str = Field(default="qa-system", alias="JWT_ISSUER")
    """JWT iss claim。"""
    jwtAudience: str = Field(default="qa-system", alias="JWT_AUDIENCE")
    """JWT aud claim。"""
    dbPoolSize: int = Field(default=20, alias="DB_POOL_SIZE")
    """SQLAlchemy 连接池 size（feat-db-pool-size-tune / 0081）。运行时由 system_config
    行 ``DB_POOL_SIZE`` 覆盖（main.py lifespan 启动期一次性读取后注入
    ``app.infrastructure.database._db_pool_config``）；此处仅作 env 未设时的默认。
    2026-09-19 bump：50 并发下旧默认 5/10=15 max 会排队，20/10=30 给 DB 留 headroom
    （配合 PG max_connections=200；0081 已把已 seed 的旧默认 5 幂等升到 20）。"""
    dbMaxOverflow: int = Field(default=10, alias="DB_MAX_OVERFLOW")
    """SQLAlchemy 连接池 max_overflow（同上，system_config ``DB_MAX_OVERFLOW`` 运行时覆盖）。"""

    @field_validator("rateLimitRequests")
    @classmethod
    def _validateRateLimitRequests(cls, value: int) -> int:
        if value < 1:
            raise ValueError(MSG_RATE_LIMIT_REQUESTS_INVALID)
        return value

    # ===== 路由策略 =====
    sessionAffinityTurns: int = Field(default=3, alias="SESSION_AFFINITY_TURNS")
    nl2sqlMaxRetries: int = Field(default=2, alias="NL2SQL_MAX_RETRIES")

    # ===== 证据分页（feat-evidence-extension）=====
    evidence_page_default: int = Field(default=50, alias="EVIDENCE_PAGE_DEFAULT")
    evidence_page_max: int = Field(default=200, alias="EVIDENCE_PAGE_MAX")

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


# 开发占位 JWT 密钥。Settings.jwtSecret 的默认值就是它：dev 便利与「漏配即暴露」之间的
# 妥协——缺省构造可跑，但启动自检（jwtSecretInsecurityReason，main.py lifespan 接线）
# 会对空值/占位符大声 warning。生产部署必须显式设置 JWT_SECRET（≥32 字节随机串）。
DEV_JWT_SECRET_PLACEHOLDER = "development-jwt-secret-change-me"


def jwtSecretInsecurityReason(secret: str) -> str | None:
    """返回 JWT 密钥的不安全原因；安全则返回 None（纯函数，启动自检用）。

    只判两种已知不安全形态（空 = 旧块曾声明的意图；占位符 = 缺省默认值），
    不发明「长度不足」等启发式——长度策略属于 authMode=real 的 fail-fast 范畴，另议。
    """
    if not secret:
        return (
            "JWT_SECRET 未设置（空字符串）：JWT 将无法签名/校验，"
            "authMode=real 下所有请求都会 401。请显式设置 JWT_SECRET。"
        )
    if secret == DEV_JWT_SECRET_PLACEHOLDER:
        return (
            "JWT_SECRET 仍用开发占位密钥（development-jwt-secret-change-me，公开已知）："
            "任何能连到服务的人都能伪造任意用户 token。生产必须显式设置 "
            "JWT_SECRET（≥32 字节随机串）。"
        )
    return None


def productionAuthMisconfiguration(settings: Settings) -> str | None:
    """返回生产环境鉴权配置错误的原因；无错误则返回 None（纯函数，启动自检用）。

    判两种已知形态，二者互相独立：

    1) ``APP_ENV=production`` 但 ``AUTH_MODE`` 不是 ``real``（漏设即默认 ``stub``）：
       ``getCurrentUser`` 会把无头请求解析为 ``anonymous`` 而**不抛异常**，
       router 级 ``Depends(getCurrentUser)`` 因此形同虚设——非公开路由匿名可达。
    2) ``APP_ENV=production`` 但 ``AUTH_STUB_ENABLED`` 为真：任何客户端可伪造
       ``X-User-Roles=admin`` 绕过 ACL。注意 ``AUTH_MODE=real`` 但
       ``AUTH_STUB_ENABLED=1`` 时本条仍成立。

    只做「返回原因」这一件事，不抛异常、不阻塞启动：是否 fail-fast 是调用方的
    运行时行为决定，不属于本函数。
    """
    if settings.appEnv != "production":
        return None
    if settings.authMode != "real":
        return (
            f"APP_ENV=production 但 AUTH_MODE={settings.authMode!r}（应为 'real'）："
            "stub 模式把无头请求解析为 anonymous 而不报错，router 级 "
            "Depends(getCurrentUser) 形同虚设，非公开路由匿名可达。"
            "生产必须设 AUTH_MODE=real。"
        )
    if settings.authStubEnabled:
        return (
            "APP_ENV=production 但 AUTH_STUB_ENABLED 为真："
            "任何客户端可伪造 X-User-Roles=admin 绕过 ACL。"
            "生产必须设 AUTH_STUB_ENABLED=0 + 反向代理剥离 X-User-* 头，"
            "或接入 JWT/IdP 替换 getCurrentUser。"
        )
    return None


@lru_cache
def getSettings() -> Settings:
    """返回缓存的只读 Settings 单例。"""
    return Settings()
