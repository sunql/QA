"""基础设施层用户面向文案。

承载 ``app/infrastructure/`` 各模块需要抛给上层 / 最终用户的中文错误文案。

放在 ``app/domain/`` 而非 ``app/services/messages_zh`` 是为了避免
反向依赖：``services → infrastructure`` 是单向，infrastructure 不应
``import app.services.*``。``app/domain/`` 是最底层，所有层都可依赖。

与 ``app/services/messages_zh.py`` 内容不重叠（后者承载业务服务文案）。
"""

# =============================================================================
# 通用
# =============================================================================

MSG_RATE_LIMITED = "请求过于频繁，请稍后再试"
MSG_DATASOURCE_CONNECT_OK = "连接成功"

# =============================================================================
# SQL 安全校验（business_db_pool）
# =============================================================================

MSG_SQL_EMPTY = "SQL 为空"
MSG_SQL_MULTI_STATEMENT = "禁止多语句执行"
MSG_SQL_PARSE_FAILED = "无法解析 SQL 语句"
MSG_SQL_FORBIDDEN_OPERATION = "禁止的 SQL 操作: {verb}"
MSG_SQL_NOT_READONLY = "仅允许只读查询（SELECT/WITH），收到: {verb}"

# =============================================================================
# API Key 加密（security/crypto）
# =============================================================================

MSG_FERNET_KEY_INVALID = "SECRET_KEY 不是合法的 Fernet 密钥"
MSG_FERNET_KEY_INVALID_DETAIL = (
    "请运行: python -c \"from cryptography.fernet import Fernet; "
    'print(Fernet.generate_key().decode())"  原始错误: {exc}'
)
MSG_API_KEY_DECRYPT_FAILED = "API Key 密文无法解密，可能密钥已变更"

# =============================================================================
# LLM 客户端（llm/openai_client, ollama_client, embedding_client）
# =============================================================================
#
# {provider} 占位符为客户端简称（如 "OpenAI"、"Ollama"、"Embedding"），
# 调用方按 provider 传入；统一在同一模板里拼接，避免为每个 provider 重复定义。

MSG_LLM_CALL_FAILED = "{provider} 调用失败: {exc}"
MSG_LLM_STREAM_FAILED = "{provider} 流式调用失败: {exc}"
MSG_LLM_STREAM_INVALID_JSON = "{provider} 流式返回非法 JSON: {line}"
MSG_LLM_HTTP_ERROR = "{provider} HTTP {status}: {body}"

# —— 客户端配置缺失（运维面向，提示如何修复）——
# 触发条件：既无 API key 也无 base_url（完全未配置任何 embedding 服务）。显式配置了
# base_url 的本地服务（Ollama / oMLX 无需鉴权）允许空 key。
MSG_EMBEDDING_MISSING_API_KEY = "Embedding 缺少 API Key"
MSG_EMBEDDING_MISSING_API_KEY_DETAIL = "未配置任何 embedding 服务：请设置 EMBEDDING_API_BASE 或激活 embedding_provider（或注入 EmbeddingClient）"
MSG_AZURE_OPENAI_MISSING_ENDPOINT = "Azure OpenAI 缺少 endpoint 配置"
MSG_AZURE_OPENAI_MISSING_ENDPOINT_DETAIL = "请配置 api_endpoint 或 AZURE_OPENAI_ENDPOINT"

# =============================================================================
# 系统配置（app/config, app/main）
# =============================================================================

MSG_RATE_LIMIT_REQUESTS_INVALID = "RATE_LIMIT_REQUESTS 必须 >= 1"
MSG_API_DESCRIPTION = "智能问答系统 - 模型路由 / 本体 / NL2SQL / 图表渲染"


# =============================================================================
# 数据质量规则（data_quality_rule）
# =============================================================================

MSG_SCHEMA_DQ_RULE_NAME = "规则中文名（如：订单数量必须 > 0）"
MSG_SCHEMA_DQ_RULE_CODE = "规则业务编码（unique，全大写下划线）"
MSG_SCHEMA_DQ_DATASOURCE_ID = "数据源 ID（评估执行时使用的业务库）"
MSG_SCHEMA_DQ_TARGET_TABLE = "目标业务表（如 PORDER）"
MSG_SCHEMA_DQ_TARGET_COLUMN = "目标列（表级规则可为空）"
MSG_SCHEMA_DQ_RULE_TYPE = "规则类型：COMPLETENESS / VALIDITY / UNIQUENESS / CONSISTENCY / REFERENTIAL"
MSG_SCHEMA_DQ_RULE_EXPRESSION = "规则表达式（如 ORDER_QTY > 0；仅 VALIDITY/CONSISTENCY 用）"
MSG_SCHEMA_DQ_THRESHOLD = "通过率阈值（0-100，DECIMAL(5,2)）"
MSG_SCHEMA_DQ_SEVERITY = "严重级别：HIGH / MEDIUM / LOW / INFO"
MSG_SCHEMA_DQ_IS_ENABLED = "是否启用"
MSG_SCHEMA_DQ_VERSION = "规则治理版本号"
MSG_SCHEMA_DQ_OWNER = "责任方（部门/人）"
MSG_SCHEMA_DQ_DESCRIPTION = "规则说明"
MSG_SCHEMA_DQ_CREATED_TIME = "创建时间"
MSG_SCHEMA_DQ_UPDATED_TIME = "更新时间"

# Phase 1.2 evaluator 响应字段描述
MSG_SCHEMA_DQ_EVAL_RULE_ID = "规则 ID"
MSG_SCHEMA_DQ_EVAL_RULE_CODE = "规则编码"
MSG_SCHEMA_DQ_EVAL_RULE_TYPE = "规则类型"
MSG_SCHEMA_DQ_EVAL_DATASOURCE_ID = "评估所用业务数据源 ID"
MSG_SCHEMA_DQ_EVAL_TOTAL_COUNT = "评估总行数"
MSG_SCHEMA_DQ_EVAL_PASSED_COUNT = "通过的行数"
MSG_SCHEMA_DQ_EVAL_PASS_RATE = "通过率（0-100）"
MSG_SCHEMA_DQ_EVAL_STATUS = "PASS（通过阈值）/ FAIL（不通过）"
MSG_SCHEMA_DQ_EVAL_EVALUATED_AT = "评估时间"
MSG_SCHEMA_DQ_EVAL_DURATION_MS = "评估耗时（毫秒）"
MSG_SCHEMA_DQ_EVAL_MESSAGE = "评估说明或错误信息"
MSG_SCHEMA_DQ_EVAL_RULE_IDS = "要评估的规则 ID 列表"
MSG_SCHEMA_DQ_EVAL_RESULTS = "每条规则的评估结果"
MSG_SCHEMA_DQ_EVAL_SUMMARY_TOTAL = "本次评估的规则总数"
MSG_SCHEMA_DQ_EVAL_SUMMARY_PASSED = "本次评估通过的规则数"

# Phase 1.3 数据质量评分
MSG_SCHEMA_DQ_SCORE_ID = "评分记录 ID"
MSG_SCHEMA_DQ_SCORE_TARGET_TABLE = "目标表（GLOBAL 时为 '*'）"
MSG_SCHEMA_DQ_SCORE_TYPE = "聚合粒度：TABLE / GLOBAL"
MSG_SCHEMA_DQ_SCORE_COMPLETENESS = "完整性维度分（0-100，Phase 1.2 已实现）"
MSG_SCHEMA_DQ_SCORE_VALIDITY = "合理性维度分（0-100）"
MSG_SCHEMA_DQ_SCORE_UNIQUENESS = "唯一性维度分（0-100）"
MSG_SCHEMA_DQ_SCORE_CONSISTENCY = "一致性维度分（0-100）"
MSG_SCHEMA_DQ_SCORE_TIMELINESS = "时效性维度分（0-100，Phase 2 补）"
MSG_SCHEMA_DQ_SCORE_REFERENTIAL = "引用完整性维度分（0-100）"
MSG_SCHEMA_DQ_SCORE_OVERALL = "整体评分 = 6 维非 NULL 平均（0-100）"
MSG_SCHEMA_DQ_SCORE_EVALUATED_AT = "评估时间戳"
MSG_SCHEMA_DQ_SCORE_DURATION_MS = "评估总耗时（毫秒）"
MSG_SCHEMA_DQ_SCORE_RULES_COUNT = "本次评估涉及的规则数"
MSG_SCHEMA_DQ_SCORE_CREATED_TIME = "记录创建时间"
MSG_SCHEMA_DQ_SCORE_UPDATED_TIME = "记录更新时间"

# Phase 1.4 Chat 可信度 badge（精简版，不暴露 6 维明细）
MSG_SCHEMA_CHAT_DQ_BADGE_TARGET_TABLE = "目标表名（与 QueryPlan.selectedClasses 对齐）"
MSG_SCHEMA_CHAT_DQ_BADGE_OVERALL = "整体评分（0-100，NULL = 已评估但全维度 NULL 或未评估）"
MSG_SCHEMA_CHAT_DQ_BADGE_EVALUATED_AT = "最新评估时间（NULL = 该表从未评估）"
MSG_SCHEMA_CHAT_DQ_BADGE_RULES_COUNT = "本次评估规则数（NULL = 未评估）"
MSG_SCHEMA_CHAT_DQ_BADGE_EVALUATED = "True=已评估，False=未评估（前端用此区分灰色 vs 红/黄/绿）"
MSG_SCHEMA_CHAT_DQ_BADGES = "目标表的可信度 badge 列表（每张 selectedClass 一个）；无 selectedClasses 或 DQ 服务降级时为 None"
MSG_SCHEMA_CHAT_SUPPLIER_360 = "供应商 360° 视图（仅 intent=supplier_360 时填充）；由 Supplier360Service 实时聚合 entity_mapping + feature_value 生成。前端 MessageItem 按字段存在性路由到 Supplier360Card"
MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING = "请在问题中提供 enterprise_key（如「供应商 100001 的 360° 视图」）"
MSG_SCHEMA_DQ_COMPUTE_EVALUATED_RULES = "本次评估的规则总数"
MSG_SCHEMA_DQ_COMPUTE_SAVED_SCORES = "本次落库的评分数（TABLE + GLOBAL）"
MSG_SCHEMA_DQ_COMPUTE_DURATION_MS = "本次 compute 全流程耗时（毫秒）"
MSG_SCHEMA_DQ_COMPUTE_SCORES = "本次落库的评分列表"

# =============================================================================
# 数据源 Schema 缓存（api/v1/datasource）
# =============================================================================

MSG_DATASOURCE_SCHEMA_NOT_CACHED = "数据源 {datasourceId} 尚未缓存 schema，请先调用 introspect"

# =============================================================================
# OpenAPI 参数描述（api/v1）
# =============================================================================

MSG_PARAM_ACTIVE_ONLY_MODELS = "仅返回启用的模型"
MSG_PARAM_ACTIVE_ONLY_DATASOURCES = "仅返回启用的数据源"
MSG_PARAM_INCLUDE_EXPIRED_ONTOLOGY = "是否包含历史版本（默认仅当前有效版本）"
MSG_PARAM_ONTOLOGY_PG_PRIMARY_KEY = "PG 表主键"
MSG_PARAM_EMBEDDING_VECTOR = "embedding 向量"
MSG_PARAM_SEARCH_QUERY = "自然语言关键词"
MSG_PARAM_SEARCH_TOP_K = "返回条数"
MSG_PARAM_SEARCH_ENTITY_TYPE = "限定本体类型"
MSG_PARAM_MILVUS_ONTOLOGY_ID = "PG 表主键"
MSG_PARAM_MILVUS_DATASOURCE_ID = "0 表示未限定数据源"

# =============================================================================
# Pydantic Schema 字段描述（app/domain/schemas）
# =============================================================================
#
# 这些 description 用于 OpenAPI 文档（/docs），与 api/v1 的 Query/Body 描述同源，
# 集中在此处便于文案统一管理与未来 i18n 化。

# —— 模型配置 ——
MSG_SCHEMA_MODEL_NAME = "模型名，如 gpt-4o"
MSG_SCHEMA_MODEL_PROVIDER = "提供商：openai / azure_openai / openai_compatible_proxy / ollama"
MSG_SCHEMA_MODEL_API_ENDPOINT = "API 端点；空则用 SDK 默认"
MSG_SCHEMA_MODEL_API_KEY = "明文 API Key（服务端加密存储）"
MSG_SCHEMA_MODEL_COST_INPUT = "输入 Token 单价（美元/千）"
MSG_SCHEMA_MODEL_COST_OUTPUT = "输出 Token 单价（美元/千）"
MSG_SCHEMA_MODEL_MAX_INPUT_TOKENS = "最大输入 Token 数"
MSG_SCHEMA_MODEL_WEIGHT = "加权随机权重 0-100"
MSG_SCHEMA_MODEL_COST_THRESHOLD = "单次查询成本熔断阈值（美元）"
MSG_SCHEMA_MODEL_IS_ACTIVE = "是否启用"

# —— embedding 服务注册表 ——
MSG_SCHEMA_EMBEDDING_PROVIDER_NAME = "服务展示名（唯一），如 Ollama bge-m3"
MSG_SCHEMA_EMBEDDING_PROVIDER_TYPE = "提供商类型：ollama / omlx / openai_compatible"
MSG_SCHEMA_EMBEDDING_PROVIDER_BASE_URL = "OpenAI 兼容 /v1/embeddings 端点"
MSG_SCHEMA_EMBEDDING_PROVIDER_MODEL_NAME = "模型名，如 bge-m3:latest"
MSG_SCHEMA_EMBEDDING_PROVIDER_API_KEY = "明文 API Key（服务端加密存储；本地模型可留空）"
MSG_SCHEMA_EMBEDDING_PROVIDER_DIMENSION = "该模型输出的向量维度"
MSG_SCHEMA_EMBEDDING_PROVIDER_IS_ACTIVE = "是否激活（单活，激活即取消其他服务）"

# —— 用量统计 ——
MSG_SCHEMA_USAGE_BY_MODEL = "按模型汇总"
MSG_SCHEMA_USAGE_LAST_QUESTION = "最近一条用户问题预览"
MSG_SCHEMA_USAGE_TOTAL_SESSIONS = "去重后的会话数"
MSG_SCHEMA_USAGE_TOTAL_REQUESTS = "总 LLM 调用次数"
MSG_SCHEMA_USAGE_TOTAL_TOKENS = "总 Token 消耗"
MSG_SCHEMA_USAGE_TOTAL_COST = "总成本（美元）"

# —— 查询计划 ——
MSG_SCHEMA_QUERY_PLAN_FORMULA = "公式表达式，如 SUM(Order.amount)"

# —— 数据源 ——
MSG_SCHEMA_DATASOURCE_NAME = "数据源名称（唯一）"
MSG_SCHEMA_DATASOURCE_DATABASE = "数据库名或 Oracle service_name"
MSG_SCHEMA_DATASOURCE_PASSWORD_PLAIN = "明文密码（服务端加密存储）"
MSG_SCHEMA_DATASOURCE_ORACLE_VERSION_FULL = "Oracle 版本，如 11g、12c、19c；不填默认 12c+"
MSG_SCHEMA_DATASOURCE_ORACLE_VERSION = "Oracle 版本，如 11g、12c、19c"
MSG_SCHEMA_DATASOURCE_PASSWORD_KEEP = "留空表示不修改密码"
MSG_SCHEMA_DATASOURCE_SCHEMA_TABLES = "业务库表清单"
MSG_SCHEMA_DATASOURCE_SCHEMA_CACHED_AT = "schema 缓存时间"

# —— 聊天 / 会话 ——
MSG_SCHEMA_CHAT_HISTORY = "最近对话上下文，最多回传 20 条"
MSG_SCHEMA_CHAT_MODEL_ID = "指定使用的模型 ID；None 表示自动路由"
MSG_SCHEMA_CHAT_CHART_TYPE_EXPLICIT = "用户显式指定的图表类型；None 表示自动推荐"
MSG_SCHEMA_CHAT_DIMENSION = "维度，如按地区分组中的「地区」"
MSG_SCHEMA_CHAT_METRIC = "指标短语，如「销售额」"
MSG_SCHEMA_CHAT_CHART_TYPE_EXTRACTED = "抽取到的图表类型"
MSG_SCHEMA_CHAT_QUERY_PLAN = "ReAct 第一阶段生成并校验通过的查询计划（选中表/列/聚合/条件）"
MSG_SCHEMA_CHAT_SERVED_MODEL = "实际服务于本次回答的大模型名称（闲聊/领域命令为 None）"
MSG_SCHEMA_CHAT_QUERY_ENTITIES = "从问题中抽取的查询实体（仅查询意图返回，其余为 None）"
MSG_SCHEMA_CHAT_AFFINITY = "会话亲和性状态：锁定模型名 + 剩余轮数；解锁时为 None"
MSG_SCHEMA_CHAT_LOCKED_MODEL = "被锁定的模型名称"
MSG_SCHEMA_CHAT_REMAINING_TURNS = "剩余锁定轮数"
MSG_SCHEMA_CHAT_QUESTION = "用户问题"
MSG_SCHEMA_CHAT_DATASOURCE_ID = "可选，限定数据源"

# —— 聊天会话历史（右侧历史面板）——
MSG_SCHEMA_CHAT_HISTORY_SESSION_ID = "逻辑会话 ID（前端生成的 client-side id）"
MSG_SCHEMA_CHAT_HISTORY_FIRST_TIME = "会话首条消息时间"
MSG_SCHEMA_CHAT_HISTORY_LAST_TIME = "会话最后一条消息时间（列表排序键，DESC）"
MSG_SCHEMA_CHAT_HISTORY_MESSAGE_COUNT = "该会话累积的消息条数（user + assistant）"
MSG_SCHEMA_CHAT_HISTORY_LAST_QUESTION = "会话最后一条 user 消息内容预览（前 30 字）"
MSG_SCHEMA_CHAT_HISTORY_LAST_ANSWER_PREVIEW = "会话最后一条 assistant 消息内容预览（前 100 字）"
MSG_SCHEMA_CHAT_HISTORY_MESSAGE_ID = "SessionMessage 主键，前端用作 React key"
MSG_SCHEMA_CHAT_HISTORY_MESSAGE_ROLE = "user | assistant"
MSG_SCHEMA_CHAT_HISTORY_MESSAGE_CONTENT = "消息文本内容"
MSG_SCHEMA_CHAT_HISTORY_MESSAGE_QUESTION = "user 行：原始问题；assistant 行：null"
MSG_SCHEMA_CHAT_HISTORY_MESSAGE_SQL = "assistant 行：生成的 SQL；user 行：null"
MSG_SCHEMA_CHAT_HISTORY_MESSAGE_CREATED_TIME = "消息时间戳（带 tz）"
MSG_SCHEMA_CHAT_HISTORY_MESSAGES = "按时间正序排列的消息流（user → assistant 交错）"
MSG_HISTORY_LISTING_LIMIT = "返回条数上限（1-200，默认 50）"
MSG_HISTORY_LISTING_OFFSET = "分页偏移（默认 0）"
MSG_HISTORY_MESSAGES_LIMIT = "消息条数上限（1-1000，默认 200）"
MSG_HISTORY_MESSAGES_BEFORE_ID = "分页 cursor：取该 id 之前更早的消息"
MSG_HISTORY_DELETE_NOT_FOUND = "会话不存在或已无消息可删"
MSG_EXPORT_SESSION_EMPTY = "会话没有任何消息可导出"
MSG_EXPORT_MESSAGE_NOT_FOUND = "指定的 message_id 不属于该会话"
MSG_HISTORY_EXPORT_FILENAME = "qa-session-{sessionId}.pdf"
MSG_HISTORY_EXPORT_MESSAGE_FILENAME = "qa-message-{messageId}.pdf"
MSG_HISTORY_EXPORT_CONTENT_DISPOSITION = "attachment; filename=\"{filename}\""

# —— 相似问法 ——
MSG_SCHEMA_SIMILAR_QUESTION = "相似的历史问题"
MSG_SCHEMA_SIMILAR_SQL = "该问题对应的生成 SQL"
MSG_SCHEMA_SIMILAR_SIMILARITY = "相似度（0-1，越大越相似）"
MSG_SCHEMA_SIMILAR_SUGGESTIONS = "相似问法列表"

# =============================================================================
# SQL 构建（business_db_pool）
# =============================================================================

MSG_ORACLE_NOT_SQLALCHEMY_URL = "Oracle 不使用 SQLAlchemy URL，收到类型: {dsType}"

# =============================================================================
# 数据血缘（data_lineage，Phase 2.1）
# =============================================================================

MSG_SCHEMA_LINEAGE_SOURCE_LAYER = "上游层：SOURCE_SYSTEM/ODS/DWD/DWS/ADS/KPI/AI"
MSG_SCHEMA_LINEAGE_SOURCE_SYSTEM = "上游系统简称（如 ERP/SRM/WMS）"
MSG_SCHEMA_LINEAGE_SOURCE_OBJECT = "上游对象名（表/类名，如 PORDER）"
MSG_SCHEMA_LINEAGE_SOURCE_FIELD = "上游字段名（表级血缘留空）"
MSG_SCHEMA_LINEAGE_TARGET_LAYER = "下游层：SOURCE_SYSTEM/ODS/DWD/DWS/ADS/KPI/AI"
MSG_SCHEMA_LINEAGE_TARGET_SYSTEM = "下游系统简称"
MSG_SCHEMA_LINEAGE_TARGET_OBJECT = "下游对象名"
MSG_SCHEMA_LINEAGE_TARGET_FIELD = "下游字段名（表级血缘留空）"
MSG_SCHEMA_LINEAGE_TRANSFORMATION = "转换规则描述（如「标准化 + 代理键」「CDC 原样接入」）"
MSG_SCHEMA_LINEAGE_REFRESH_FREQ = "刷新频率：REALTIME/HOURLY/DAILY/WEEKLY"
MSG_SCHEMA_LINEAGE_OWNER = "责任方（部门/人）"
MSG_SCHEMA_LINEAGE_DESCRIPTION = "血缘边说明"
MSG_SCHEMA_LINEAGE_IS_ACTIVE = "是否启用（false 表示软删除，保留历史可视化追溯）"
MSG_SCHEMA_LINEAGE_CREATED_TIME = "创建时间"
MSG_SCHEMA_LINEAGE_UPDATED_TIME = "更新时间"

# =============================================================================
# 跨系统编码映射（entity_mapping，Phase 3.1）
# =============================================================================

MSG_SCHEMA_ENTITY_MAPPING_ENTITY_TYPE = "实体类型：SUPPLIER/MATERIAL/PO/GR/IQC/NCR"
MSG_SCHEMA_ENTITY_MAPPING_ENTERPRISE_KEY = "企业统一代理键（BIGINT，MDM 主数据）"
MSG_SCHEMA_ENTITY_MAPPING_ENTERPRISE_CODE = "企业统一编码（可读，如 SUP000001）"
MSG_SCHEMA_ENTITY_MAPPING_SOURCE_SYSTEM = "源业务系统：ERP/SRM/QMS/MDM/PLM"
MSG_SCHEMA_ENTITY_MAPPING_SOURCE_KEY = "源系统原始 key"
MSG_SCHEMA_ENTITY_MAPPING_SOURCE_CODE = "源系统原始编码"
MSG_SCHEMA_ENTITY_MAPPING_MATCH_RULE = "匹配规则：MDM_MASTER/BUSINESS_KEY/MAPPING"
MSG_SCHEMA_ENTITY_MAPPING_EFFECTIVE_DATE = "生效日期"
MSG_SCHEMA_ENTITY_MAPPING_EXPIRY_DATE = "失效日期（空表示长期有效）"
MSG_SCHEMA_ENTITY_MAPPING_CREATED_TIME = "创建时间"
MSG_SCHEMA_ENTITY_MAPPING_UPDATED_TIME = "更新时间"
