"""中文用户面向文案集中模块。

将原本散落在 ``app/services/`` 与 ``app/infrastructure/`` 各处的
user-facing 中文字面量集中。本模块**仅含字面量**，不引入 i18n 框架；
调用方按需 import 常量或用 ``str.format(**kwargs)`` 注入变量。

未来若需多语言，把本模块替换为 ``messages.get(lang, key)`` 调用即可，
无需改动业务代码。

包含：
- 通用错误（兜底、限流）
- 领域命令文案（DEFINE / MAP / METRIC）
- 本体/数据源/模型配置错误文案
- SQL 安全校验文案
- 路由/闲聊文案

不在本模块：
- LLM prompt 模板（属于模型指令，归模块常量如 ``_CLARIFY_SYSTEM_PROMPT``）
- ``logger.*`` 中的运维文案（面向运维排障）
- docstring 中的中文（文档）
- 正则表达式 pattern 内的字符（逻辑而非文案）
"""

# =============================================================================
# 通用
# =============================================================================

MSG_INTERNAL_ERROR = "服务内部错误，请稍后重试"
# MSG_RATE_LIMITED 在 app/domain/error_messages.py（基础设施层）

# 消息角色 → 中文说话人标签（MessageList 渲染使用）
MSG_SPEAKER_USER = "用户"
MSG_SPEAKER_ASSISTANT = "助手"


# =============================================================================
# 服务状态监控
# =============================================================================

# 单项依赖服务探测超时的提示
MSG_SERVICE_CHECK_TIMEOUT = "探测超时"
# 探测失败但错误详情不宜直出（可能含用户名/内网主机等敏感信息），完整异常只写服务端日志
MSG_SERVICE_CHECK_FAILED = "探测失败（详见服务端日志）"
# Embedding 服务未配置任何端点（无 EMBEDDING_API_BASE，也无激活的 provider）
MSG_EMBEDDING_NOT_CONFIGURED = "未配置 Embedding 端点（EMBEDDING_API_BASE 或激活的 provider）"


# =============================================================================
# 闲聊（CHITCHAT）
# =============================================================================

# 当意图判定为闲聊时的固定欢迎语；不调用 LLM，直接返回给前端
MSG_CHITCHAT_GREETING = (
    "你好！我是智能问答助手，可以基于本体元数据把自然语言问题转换为 SQL 查询，"
    "并用图表展示结果。你可以试试：\"各供应商的收货数量汇总\"。"
)


# =============================================================================
# 领域命令：DEFINE / MAP / METRIC
# =============================================================================

# 引导用户正确使用「定义指标」自然语言命令
MSG_DEFINE_METRIC_GUIDE_PREFIX = "请使用格式：定义指标 指标名 = 公式。例如："
MSG_DEFINE_METRIC_GUIDE_EXAMPLE = "定义指标 销售额 = SUM(order.amount)"

# 指标已创建的回复模板，{name} 指标名 {formula} 公式
MSG_METRIC_DEFINED = "指标「{name}」已定义：{formula}"

# 引导用户使用 /define 斜杠指令创建类
MSG_DEFINE_CLASS_GUIDE_PREFIX = "请使用格式：/define 类名 [alias=别名] [desc=描述]。例如："
MSG_DEFINE_CLASS_GUIDE_EXAMPLE = "/define 产品 alias=Product desc=公司销售商品"

# 类已创建的回复模板，{name} 类名 {alias} 别名（可选）
MSG_CLASS_CREATED = "类「{name}」已创建"
MSG_CLASS_ALIAS_SUFFIX = "（别名 {alias}）"

# 当前没有指标 / 列出指标
MSG_NO_METRICS_DEFINED = (
    "当前没有定义任何指标。你可以使用「定义指标 指标名 = 公式」来创建。"
)
MSG_METRIC_LIST_HEADER = "系统已定义以下指标：\n"

# 引导用户使用「把 X 映射到 Y」自然语言命令
MSG_MAP_PROPERTY_GUIDE = (
    "请使用格式：把 属性名 映射到 目标类。例如：把 customer_name 映射到 Customer"
)
MSG_MAP_PROPERTY_NOT_FOUND = (
    "未找到属性「{source}」或目标类「{target}」，无法建立映射。"
)
MSG_MAP_PROPERTY_RETRY_HINT = "请确认属性名与类名后重试。"
MSG_MAP_PROPERTY_OK = "属性「{property}」已映射到类「{className}」"


# =============================================================================
# 数据质量规则（data_quality_rule）
# =============================================================================

MSG_DQ_RULE_NOT_FOUND = "数据质量规则 id={id} 不存在"
MSG_DQ_RULE_CODE_EXISTS = "数据质量规则编码「{code}」已存在"

# Phase 1.2 evaluator
MSG_DQ_EVAL_INVALID_IDENTIFIER = (
    "数据质量评估 SQL 标识符不合法（仅允许字母/数字/下划线，且不以数字开头）: {value}"
)
MSG_DQ_EVAL_INVALID_EXPRESSION = (
    "数据质量评估表达式不合法（仅允许标识符 + 比较 + 算术 + 括号）: {value}"
)
MSG_DQ_EVAL_INVALID_REF_FORMAT = (
    "REFERENTIAL 规则的 rule_expression 必须为 `REF <ref_table>.<ref_column>` 格式，收到: {value}"
)
MSG_DQ_EVAL_RULE_EXPRESSION_REQUIRED = (
    "规则类型 {ruleType} 必须填写 rule_expression"
)
MSG_DQ_EVAL_TARGET_COLUMN_REQUIRED = (
    "规则类型 {ruleType} 必须填写 target_column"
)
MSG_DQ_EVAL_DATASOURCE_NOT_FOUND = "评估 DQ 规则 {ruleId} 找不到关联业务数据源"
MSG_DQ_EVAL_RULE_TYPE_UNSUPPORTED = "不支持的数据质量规则类型: {ruleType}"


# =============================================================================
# 数据血缘（data_lineage，Phase 2.1）
# =============================================================================

MSG_LINEAGE_EDGE_NOT_FOUND = "数据血缘边 id={id} 不存在"
MSG_LINEAGE_EDGE_EXISTS = (
    "已存在相同上下游的血缘边: {src} -> {tgt}（source_field / target_field 需同时区分）"
)
MSG_LINEAGE_SELF_LOOP = (
    "血缘边禁止自指（source 与 target 同层同对象），收到: {src} -> {tgt}"
)


# =============================================================================
# 跨系统编码映射（entity_mapping，Phase 3.1）
# =============================================================================

MSG_ENTITY_MAPPING_NOT_FOUND = "编码映射 id={id} 不存在"
MSG_ENTITY_MAPPING_EXISTS = (
    "已存在同实体类型 + 企业代理键 + 源系统的映射: "
    "{entityType} enterprise_key={enterpriseKey} source_system={sourceSystem}"
)
MSG_ENTITY_MAPPING_DATE_RANGE = "生效日期不得晚于失效日期: {effectiveDate} > {expiryDate}"


# =============================================================================
# 模型路由
# =============================================================================

MSG_NO_MODEL_AVAILABLE = "没有可用的模型配置"
MSG_NO_ENABLED_MODEL = "没有启用的模型配置"


# =============================================================================
# 模型配置 CRUD
# =============================================================================

MSG_MODEL_CONFIG_NAME_EXISTS = "模型名 {name} 已存在"
MSG_MODEL_CONFIG_NOT_FOUND = "模型配置 {id} 不存在"
# ChatService 中指定的 modelId 无效或已禁用
MSG_MODEL_CONFIG_UNAVAILABLE = "指定的模型配置 {id} 不存在或已禁用"


# =============================================================================
# Embedding 服务注册表
# =============================================================================

MSG_EMBEDDING_PROVIDER_NAME_EXISTS = "embedding 服务名 {name} 已存在"
MSG_EMBEDDING_PROVIDER_NOT_FOUND = "embedding 服务 {id} 不存在"
# 并发激活冲突（DB 层部分唯一索引兜底）：两个请求同时把不同服务置为激活
MSG_EMBEDDING_PROVIDER_ACTIVE_CONFLICT = "同时存在多个待激活的 embedding 服务，操作冲突，请重试"
# 运行时维度守卫：激活的 embedding 服务输出维度必须与 Milvus 集合一致，否则语义检索
# 插入/查询会静默错乱。提示走重建 + 回填流程（scripts/backfill_milvus_embeddings.py）。
MSG_EMBEDDING_PROVIDER_DIMENSION_MISMATCH = (
    "embedding 服务 {name} 输出维度 {dimension} 与 Milvus 集合维度 {milvusDimension} 不一致；"
    "请重建集合为 {dimension} 维并回填（scripts/backfill_milvus_embeddings.py）"
)


# =============================================================================
# 本体（Ontology）
# =============================================================================

# Class 错误
MSG_CLASS_NAME_EXISTS = "类名 {name} 已存在"
MSG_PARENT_CLASS_NOT_FOUND = "父类 id={id} 不存在"
MSG_ONTOLOGY_CLASS_NOT_FOUND = "OntologyClass id={id} 不存在"
MSG_ONTOLOGY_CLASS_EXPIRED = (
    "OntologyClass id={id} 已失效（valid_to={valid_to}），请基于当前版本更新"
)
MSG_CLASS_INHERIT_SELF = "类不能继承自身"
MSG_CLASS_INHERIT_CYCLE = "父类 id={id} 是当前类的后代，设置继承会形成环"
MSG_INHERIT_CHECK_UNAVAILABLE = "继承环检测不可用，已拒绝更新"
MSG_CLASS_ALREADY_EXPIRED = "OntologyClass id={id} 已是历史版本，无需重复删除"

# Property / Metric 错误
MSG_ONTOLOGY_PROPERTY_NOT_FOUND = "OntologyProperty id={id} 不存在"
MSG_ONTOLOGY_METRIC_NOT_FOUND = "OntologyMetric id={id} 不存在"

# Join 目录错误
MSG_ONTOLOGY_JOIN_NOT_FOUND = "OntologyJoin id={id} 不存在"
MSG_ONTOLOGY_JOIN_COLUMN_COUNT_MISMATCH = (
    "源列与目标列数量必须一致（source_columns 与 target_columns 逐列配对）"
)
MSG_ONTOLOGY_JOIN_DUP = "该关联关系已存在（相同源类/源列 → 目标类/目标列）"

# 向量检索 / 同步（Milvus）
MSG_VECTOR_SEARCH_FAILED = "向量检索失败"

# ── Schema 自动发现（schema_introspection_service）────────
MSG_DATASOURCE_USERNAME_INVALID_ORACLE_OWNER = "数据源用户名不能作为 Oracle schema owner"
MSG_DATASOURCE_TYPE_NOT_SUPPORTED = "不支持的数据源类型: {dsType}"
MSG_DATASOURCE_SCHEMA_READ_FAILED = "数据源 {datasourceId} schema 读取失败"
MSG_DATASOURCE_SCHEMA_READ_FAILED_DETAIL = "业务数据库读取失败，请检查数据源连接配置，详见服务端日志"
MSG_DATASOURCE_SCHEMA_TABLE_LIMIT_EXCEEDED = "数据源 {datasourceId} schema 表数量 {tableCount} 超过上限 {maxTables}"
MSG_DATASOURCE_SCHEMA_TABLE_LIMIT_DETAIL = "请检查 schema owner 范围，或调整上限配置"
MSG_VECTOR_SYNC_FAILED = "向量同步失败: {exc}"


# =============================================================================
# 数据源（Datasource）
# =============================================================================

MSG_DATASOURCE_NOT_FOUND = "数据源 {id} 不存在"
MSG_DATASOURCE_CONNECT_FAILED = "连接失败: {message}"
MSG_DATASOURCE_HOST_NOT_ALLOWED = "主机 {host} 不在允许列表内"
MSG_DATASOURCE_HOST_ALLOWLIST_DETAIL = "允许的主机: {hosts}"
MSG_DATASOURCE_NAME_EXISTS = "数据源名称 {name} 已存在"
# MSG_DATASOURCE_CONNECT_OK 在 app/domain/error_messages.py（基础设施层）


# =============================================================================
# NL2SQL 引擎错误（用于 raise + StreamEvent error 字段）
# =============================================================================

MSG_NL2SQL_PLAN_INVALID = (
    "无法生成有效的查询计划，请换一种问法或补充本体元数据"
)
MSG_NL2SQL_PLAN_VALIDATION_FAILED = (
    "无法生成通过校验的查询计划，请换一种问法或补充本体元数据"
)
MSG_NL2SQL_SQL_INVALID = (
    "无法生成有效的查询 SQL，请换一种问法或补充本体元数据"
)
MSG_NL2SQL_SQL_FAILED = "无法生成 SQL"


# =============================================================================
# NL2SQL 术语字典
# =============================================================================

MSG_TERM_DICT_TERM_EXISTS = "术语「{term}」已存在"
MSG_TERM_DICT_NOT_FOUND = "术语 id={id} 不存在"


# =============================================================================
# 图表（Chart）
# =============================================================================

# 兜底规则生成的 ECharts 默认标题（chart_service.py 规则分支）
MSG_CHART_TITLE_PIE = "数据分布"
MSG_CHART_TITLE_RESULT = "查询结果"
