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

# dq-rule-auto-generation Task 6: LLM advisory
MSG_DQ_GEN_LLM_UNAVAILABLE = "AI 辅助不可用，请稍后重试或手动填写约束"
MSG_DQ_GEN_LLM_PARSE_ERROR = "AI 返回格式无法解析，请稍后重试"
MSG_DQ_GEN_BAD_VALUE = "不允许包含单引号字符"
MSG_DQ_GEN_PROPERTY_NOT_FOUND = "本体属性 id={id} 不存在"

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
# 供应商 360° ADS 视图（Phase 5.3）
# =============================================================================

# enterprise_key 在 entity_mapping 中找不到 → 提示该 supplier 尚未建档
MSG_SUPPLIER_360_NOT_FOUND = (
    "供应商 enterprise_key={key} 不存在或尚未在 entity_mapping 建档"
)

# Risk Agent 同样依赖 entity_mapping，复用 Supplier360 的 NotFound 通用消息
# （与 Phase 4.5 ACL 原则一致：避免「不存在 vs 无权限」侧信道）。
MSG_SUPPLIER_RISK_NOT_FOUND = MSG_SUPPLIER_360_NOT_FOUND

# 风险等级 → 建议动作（按等级静态生成，Phase 5.4 Round 1 不调 LLM）
MSG_RISK_ACTIONS_HIGH: tuple[str, ...] = (
    "暂停新增采购订单",
    "启动二方现场审核",
    "收紧付款账期至 30 天内",
    "通知质量与采购负责人介入",
)
MSG_RISK_ACTIONS_MEDIUM: tuple[str, ...] = (
    "加强来料抽检比例（建议 ≥ 30%）",
    "缩短付款账期至 45 天内",
    "下季度安排一次现场评审",
)
MSG_RISK_ACTIONS_LOW: tuple[str, ...] = (
    "维持当前合作模式",
    "下季度例行复核即可",
)
MSG_RISK_ACTIONS_UNKNOWN: tuple[str, ...] = (
    "暂无可用风险数据，请先在数据治理页触发 feature_value 计算",
)

# LLM 不可用时按违规 feature 拼装的降级模板（fallback_template）
MSG_RISK_POINTS_TEMPLATE = "该供应商存在以下风险点：{reasons}"


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

# 语义关系（ontology_relation）错误
MSG_ONTOLOGY_RELATION_NOT_FOUND = "OntologyRelation id={id} 不存在"
MSG_ONTOLOGY_RELATION_DUP = "该语义关系已存在（相同源类 → 目标类 → 关系类型）"
MSG_ONTOLOGY_RELATION_SELF = "源类与目标类不能相同（语义关系必须是两个不同类之间）"
MSG_ONTOLOGY_RELATION_INVALID_TYPE = "不支持的关系类型: {relationType}"

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

# ---- Wiki 知识管理（feat-wiki-knowledge）----
MSG_WIKI_PAGE_NOT_FOUND = "知识条目「{pageId}」不存在"
MSG_WIKI_PAGE_DUPLICATE = "知识条目 ID「{pageId}」已存在"
MSG_WIKI_PAGE_DUPLICATE_SKIPPED = "知识条目 ID「{pageId}」已存在且正文一致，本次导入跳过"
MSG_WIKI_PAGE_DIMENSION_INVALID = "知识维度「{dimension}」不合法"
MSG_WIKI_PAGE_STAGE_INVALID = "知识结构阶段「{stage}」不合法"
MSG_WIKI_PAGE_STATUS_INVALID = "知识条目状态「{status}」不合法"
MSG_WIKI_PAGE_ID_INVALID = "知识条目 ID 不能为空，且仅允许字母、数字、连字符与下划线"
MSG_WIKI_IMPORT_TASK_TYPE_INVALID = "导入任务类型「{taskType}」不合法"
MSG_WIKI_IMPORT_SOURCE_TYPE_INVALID = "导入来源类型「{sourceType}」不合法"

# --- M8：Agent 工具（wiki_search / wiki_read / rule_evaluate / coverage_status）---
MSG_WIKI_AGENT_PAGE_REF_NOT_FOUND = "没有找到与「{ref}」匹配的知识条目"
MSG_WIKI_AGENT_PAGE_REF_AMBIGUOUS = "「{ref}」匹配到 {count} 条知识，请指明具体条目"
MSG_WIKI_AGENT_RULE_NO_EXAMPLES = (
    "知识条目「{pageId}」的规则没有存 dry-run 样例，无法试跑 —— "
    "请先在条目上补充样例（input + expectedOutput.matched）"
)

# --- M2：导入任务 + LLM 调用层 ---
MSG_WIKI_LLM_MODEL_UNUSABLE = "模型「{modelName}」无可用凭据，无法调用"
MSG_WIKI_LLM_EMPTY_RESPONSE = "AI 返回内容为空，请稍后重试"
MSG_WIKI_LLM_PARSE_ERROR = "AI 返回格式无法解析，请稍后重试"
MSG_WIKI_IMPORT_NO_DRAFTS = "未解析出任何可导入的知识条目，请检查内容中的标题层级"
MSG_WIKI_IMPORT_MODEL_REQUIRED = "开启自动分类时必须指定处理模型（modelId）"
MSG_WIKI_IMPORT_ALL_FAILED = "全部条目导入失败，请检查草稿内容或 pageId 是否重复"
MSG_WIKI_IMPORT_ABORTED = "导入过程中发生未预期错误，任务已中止，请查看服务端日志"
MSG_WIKI_IMPORT_CLASSIFY_INCOMPLETE = (
    "{count} 条知识未能完成自动分类（已按未分类入库），可稍后批量重分类补做"
)

# 上传解析（M2 补充）：两条消息刻意分开，因为处置动作不同 ——
# 「格式不支持」换格式即可，「文件读不出内容」要换文件。合并成一句会让
# 用户拿着一个损坏的 PDF 反复换扩展名。
MSG_WIKI_IMPORT_FILE_TYPE_UNSUPPORTED = (
    "不支持的文件类型「{filename}」，请上传 PDF / Word(.docx) / PPT(.pptx) / "
    "Excel(.xlsx/.xls) / Markdown / 纯文本。"
    "老式 .doc / .ppt 请先另存为 .docx / .pptx。"
)
MSG_WIKI_IMPORT_FILE_PARSE_FAILED = (
    "文件解析失败，可能是文件已损坏或内容加密。请确认后重试（服务端日志有详细原因）"
)
MSG_WIKI_IMPORT_FILE_TOO_LARGE = (
    "文件过大（{size} MB），单次上传上限 {max} MB"
)
MSG_WIKI_IMPORT_SOURCE_STORE_FAILED = (
    "源文件留存失败，请稍后重试；若持续失败请联系管理员。"
)

# --- M3：学习闭环反馈 ---
MSG_WIKI_FEEDBACK_ACTION_INVALID = "反馈动作「{action}」不合法"
MSG_WIKI_FEEDBACK_ENTITY_TYPE_INVALID = "反馈目标类型「{entityType}」不合法"
MSG_WIKI_FEEDBACK_MECHANISM_INVALID = "反馈机制「{mechanism}」不合法"
MSG_WIKI_FEEDBACK_MODIFICATION_REQUIRED = "反馈动作 MODIFY 必须携带 userModification"
MSG_WIKI_FEEDBACK_MODIFICATION_UNEXPECTED = (
    "反馈动作 {action} 不应携带 userModification"
)

# --- M4：关系发现 ---
MSG_WIKI_RELATION_NOT_FOUND = "知识关系 id={relationId} 不存在"
MSG_WIKI_RELATION_REVIEW_ACTION_INVALID = "关系审核动作「{action}」不合法"
MSG_WIKI_RELATION_ALREADY_REVIEWED = (
    "知识关系 id={relationId} 已是「{action}」状态，无需重复审核"
)

# --- M5：冲突检测（机制 3）---
MSG_WIKI_CONFLICT_NOT_FOUND = "知识冲突 id={conflictId} 不存在"
MSG_WIKI_CONFLICT_ACTION_INVALID = "冲突处置动作「{action}」不合法"
MSG_WIKI_CONFLICT_ALREADY_RESOLVED = (
    "知识冲突 id={conflictId} 已处置（{action}），无需重复处置"
)

# --- M5：结构化建议（机制 4）---
MSG_WIKI_SUGGESTION_NOT_FOUND = "结构化建议 id={suggestionId} 不存在"
MSG_WIKI_SUGGESTION_ALREADY_RESOLVED = (
    "结构化建议 id={suggestionId} 已处置（{status}），无需重复处置"
)

# --- M6：可执行规则与渐进结构（机制 5）---
MSG_WIKI_RULE_NOT_FOUND = "知识条目「{pageId}」还没有可执行规则"
MSG_WIKI_WORKFLOW_NOT_FOUND = "知识条目「{pageId}」还没有结构化流程"
MSG_WIKI_RULE_CONDITIONS_REQUIRED = (
    "规则至少要有一个条件：无条件规则会命中全部记录，不能物化也不能求值"
)
MSG_WIKI_RULE_OPERATOR_UNSUPPORTED = (
    "规则算子「{operator}」系统不认识，无法判定（可用：>= <= > < = != IN、NOT IN）"
)
MSG_WIKI_RULE_VALUE_SHAPE_INVALID = (
    "算子 {operator} 的取值必须是非空数组：写成单值这条规则永远判不了，却看不出坏"
)
MSG_WIKI_RULE_EXAMPLE_INVALID = (
    "dry-run 样例 #{index} 形状不合法：需要 input（对象）与 expectedOutput.matched（布尔）"
)


# --- M7：覆盖度自感知（机制 6）---
MSG_WIKI_COVERAGE_DOMAIN_EMPTY = "业务域不能为空（空串会让这一格无从归类）"
MSG_WIKI_COVERAGE_DOMAIN_TOO_LONG = (
    "业务域「{domain}」超过 {maxLength} 字符，超出列宽会被数据库拒绝"
)
MSG_WIKI_COVERAGE_CLASS_NOT_FOUND = "本体类 id={classId} 不存在或已下线"
MSG_WIKI_COVERAGE_MAPPING_NOT_FOUND = (
    "本体类 id={classId} 没有标注业务域「{domain}」"
)
MSG_WIKI_COVERAGE_STATUS_INVALID = "覆盖度状态「{status}」不合法"

# --- 事实原子编辑（Task 10）---
MSG_WIKI_CLAIM_NOT_FOUND = "事实原子不存在：{claimId}"
MSG_WIKI_COMPILE_TASK_NOT_FOUND = "编译作业不存在：{taskId}"
MSG_WIKI_COMPILE_SCOPE_INVALID = "不支持的编译范围：{scope}"
MSG_WIKI_AUTHORITY_LEVEL_INVALID = "权威度等级非法，合法值：{levels}"

# =============================================================================
# 图表（Chart）
# =============================================================================

# 兜底规则生成的 ECharts 默认标题（chart_service.py 规则分支）
MSG_CHART_TITLE_PIE = "数据分布"
MSG_CHART_TITLE_RESULT = "查询结果"


# =============================================================================
# KPI Catalog（Phase 4.1）
# =============================================================================

MSG_KPI_CATALOG_NOT_FOUND = "KPI Catalog id={id} 不存在"
MSG_KPI_CATALOG_DUPLICATE_CODE = "KPI 编码「{code}」已存在"
MSG_KPI_CATALOG_STATUS_NULL = "KPI status 不允许为 null（NOT NULL 约束）"


# =============================================================================
# AI Feature Layer（Phase 4.3）
# =============================================================================

MSG_FEATURE_NOT_FOUND = "Feature id={id} 不存在"
MSG_FEATURE_DUPLICATE_NAME = "Feature 名称「{name}」已存在"
MSG_FEATURE_DATASOURCE_NOT_FOUND = "数据源 id={id} 不存在"
MSG_FEATURE_VALUE_EMPTY = "特征值 value 与 value_text 不能同时为空"
MSG_FEATURE_VALUE_NO_ENTITY_KEY = "特征值缺少 entity_key 列（calculation_logic 必须返回 entity_key 与 value/value_text）"
MSG_FEATURE_TOO_MANY_ROWS = "特征计算返回 {count} 行，超过上限 {limit}（请收紧 calculation_logic 过滤条件）"
MSG_FEATURE_VALUE_TEXT_TOO_LONG = "value_text 长度 {length} 超过上限 500"
MSG_FEATURE_ENTITY_KEY_TOO_LONG = "entity_key 长度 {length} 超过上限 100"
MSG_FEATURE_EXECUTE_FAILED = "特征计算执行失败（数据源 id={id}）：{detail}"

# Feature 在线查询（Phase 4.4）
MSG_FEATURE_NOT_FOUND_BY_NAME = "Feature「{name}」不存在"
MSG_FEATURE_NAME_INVALID = "Feature 名「{name}」不合法（须大写字母开头 + 大写/数字/下划线）"
MSG_FEATURE_ENTITY_KEYS_TOO_MANY = "entity_keys 数量超上限 {limit}"

# Chat 特征回流（Phase 4.4）
MSG_CHAT_FEATURE_ANSWER_LINE = "{entity_key} 的 {feature_name}（{alias}）为 {value}{unit}"
MSG_CHAT_FEATURE_ANSWER_FOOTER = "（特征值有效期 {valid_at}，计算时间 {computed_at}；来源：预计算特征）"
MSG_CHAT_FEATURE_EMPTY = "特征 {feature_name} 当前没有已计算的特征值，请先触发计算"


# =============================================================================
# Governance Hardening（Phase 4.5，遗留 #68）
# =============================================================================

MSG_GOVERNANCE_PERMISSION_DENIED = (
    "无权修改 KPI「{kpi_code}」（owner={owner}，当前用户部门={user_departments}）；"
    "仅 owner 部门或 admin 角色可改"
)
MSG_GOVERNANCE_KPI_NOT_FOUND = "KPI Catalog id={id} 不存在"

# =============================================================================
# Document Catalog（Phase 5.1）
# =============================================================================

MSG_DOCUMENT_NOT_FOUND = "文档 id={id} 不存在"
MSG_DOCUMENT_DUPLICATE = "文档编号「{document_id}」已存在"
# content_hash 唯一索引（uq_document_catalog_content_hash）冲突：同一份文件已入库。
# 与 MSG_DOCUMENT_DUPLICATE 刻意分开 —— 后者是「编号重复」，前者是「内容重复」，
# 处置动作不同（前者换编号，后者换文件 / 别再传同一份）。
MSG_DOCUMENT_CONTENT_DUPLICATE = (
    "文档内容重复：content_hash 命中唯一索引 uq_document_catalog_content_hash，"
    "同一文件已入库，请勿重复上传"
)
MSG_DOCUMENT_CONTENT_EXISTS = (
    "文档内容重复：该文件已作为文档「{document_id}」入库"
    "（唯一索引 uq_document_catalog_content_hash），请勿重复上传同一文件"
)
MSG_DOCUMENT_REL_NOT_FOUND = "文档关联 id={id} 不存在"
MSG_DOCUMENT_REL_EXISTS = (
    "文档「{documentId}」与实体 {entityType}/{entityKey} 的关联已存在"
)
MSG_DOCUMENT_SOURCE_STORE_FAILED = (
    "源文件留存失败，请稍后重试；若持续失败请联系管理员。"
)

# =============================================================================
# Phase 6.3 feat-graph-traversal-api：知识图谱多跳推理
# =============================================================================

MSG_GRAPH_TRAVERSAL_UNAVAILABLE = (
    "知识图谱服务暂时不可用，无法执行多跳推理。请稍后重试，"
    "或改用数据查询问法（如「供应商 X 的订单数」）。"
)

# =============================================================================
# Phase 4.4 feat-business-object-registry：业务对象注册表
# =============================================================================

MSG_BUSINESS_OBJECT_NOT_FOUND = "业务对象「{code}」不存在"
MSG_BUSINESS_OBJECT_CODE_EXISTS = "业务对象代码「{code}」已存在"
MSG_BUSINESS_OBJECT_GRAPH_LABEL_MISMATCH = (
    "业务对象「{code}」的 graph_label 与本体类 class_name 不一致"
)
MSG_BUSINESS_OBJECT_IN_USE = "业务对象「{code}」正被以下表引用，无法删除：{tables}"

# =============================================================================
# 数据质量规则自动生成（dq-rule-auto-generation Task 4）
# =============================================================================

MSG_DQ_GEN_CLASS_NOT_FOUND = "本体类 id={id} 不存在"
MSG_DQ_GEN_DATASOURCE_NOT_FOUND = "数据源 id={id} 不存在"
MSG_DQ_GEN_RULE_CREATED = "数据质量规则「{rule_code}」已自动生成"
MSG_DQ_GEN_RULE_SKIPPED_DUPLICATE = "规则「{rule_code}」已存在，已跳过"

# =============================================================================
# 文档问答（Phase 5.6）
# =============================================================================

MSG_SESSION_NOT_OWNED = "会话不存在或不属于当前用户"
