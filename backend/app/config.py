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

    # ===== Schema 发现 =====
    # 单数据源允许发现的表数量上限：本地导入/元数据发现的硬保护，防止超大 schema
    # 撑爆 introspection 响应体与缓存。大型 ERP（如 Sage X3 生产库 1600+ 表）可按需调高。
    # 注意：NL2SQL 提示词使用本体 schema（导入后手工维护的类）而非该 introspection 缓存，
    # 调高不会撑爆 NL2SQL 提示词，只会让 introspection / 预览响应体变大。
    schemaMaxTables: int = Field(default=3000, alias="SCHEMA_MAX_TABLES")

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
