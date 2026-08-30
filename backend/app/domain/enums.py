"""领域枚举。

所有枚举值与外部契约（DB 列、JSON、配置）保持一致。
"""

from __future__ import annotations

from enum import Enum


class ProviderType(str, Enum):
    """LLM 提供商类型。"""

    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    OPENAI_COMPATIBLE_PROXY = "openai_compatible_proxy"
    OLLAMA = "ollama"


class ModelStatus(int, Enum):
    """模型配置状态。与设计稿 status TINYINT 对齐。"""

    ACTIVE = 1
    DISABLED = 0


class DataSourceType(str, Enum):
    """业务数据源类型。"""

    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    ORACLE = "oracle"


class ChartType(str, Enum):
    """图表类型。"""

    TABLE = "table"
    BAR = "bar"
    PIE = "pie"
    LINE = "line"
    SCATTER = "scatter"


class DataType(str, Enum):
    """本体属性数据类型。"""

    STRING = "STRING"
    INT = "INT"
    DECIMAL = "DECIMAL"
    DATETIME = "DATETIME"
    BOOLEAN = "BOOLEAN"


class AggFunction(str, Enum):
    """指标聚合函数。"""

    SUM = "SUM"
    AVG = "AVG"
    COUNT = "COUNT"
    MAX = "MAX"
    MIN = "MIN"


class RuleType(str, Enum):
    """数据质量规则类型。

    COMPLETENESS / VALIDITY / UNIQUENESS / CONSISTENCY / REFERENTIAL：
    Phase 1.1 已实现。TIMELINESS 留 Phase 2 血缘模块再实现。
    """

    COMPLETENESS = "COMPLETENESS"
    VALIDITY = "VALIDITY"
    UNIQUENESS = "UNIQUENESS"
    CONSISTENCY = "CONSISTENCY"
    REFERENTIAL = "REFERENTIAL"
    TIMELINESS = "TIMELINESS"


class Severity(str, Enum):
    """数据质量规则严重级别。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class ScoreType(str, Enum):
    """数据质量评分聚合粒度（Phase 1.3）。

    TABLE：按 target_table 聚合每张表的 6 维评分。
    GLOBAL：跨所有 enabled rule 聚合的整体评分（target_table='*'）。
    DATASET：本期不实现，留 Phase 3+ 决定。
    """

    TABLE = "TABLE"
    GLOBAL = "GLOBAL"


class ObjectType(str, Enum):
    """本体类业务对象类型（Phase 3.4，采购域 Sheet 03 业务对象目录）。

    Master：主数据（物料/供应商/地点等稳定参照实体）。
    Transaction：交易单据（订单/收货/发票/付款/报价等业务单据）。
    Reference：参考/配置/关联（价格配置、请购订单关联等辅助表）。
    Event：事件（本期未使用，预留）。
    """

    MASTER = "Master"
    TRANSACTION = "Transaction"
    REFERENCE = "Reference"
    EVENT = "Event"


class IntentType(str, Enum):
    """用户意图类型。

    QUERY / NEW_QUERY：全新查询（NEW_QUERY 表示有历史状态时开启的新一轮）。
    REFINE / FOLLOW_UP：多轮意图，需存在会话查询状态（见 intent_service）。
    CLARIFY：询问概念含义，不进 NL2SQL 流水线。
    DEFINE / MAP / METRIC：设计稿保留意图，暂未接入流水线。
    """

    QUERY = "query"
    NEW_QUERY = "new_query"
    REFINE = "refine"
    FOLLOW_UP = "follow_up"
    CLARIFY = "clarify"
    DEFINE = "define"
    MAP = "map"
    METRIC = "metric"
    CHITCHAT = "chitchat"


class LineageLayer(str, Enum):
    """数据血缘层级（Phase 2.1）。

    7 层模型覆盖从源端到 AI 应用的完整数据链路：
    - SOURCE_SYSTEM：业务源系统（ERP / SRM / WMS 等）
    - ODS：贴源层（Operational Data Store，原样接入 + 标准化）
    - DWD：明细层（Data Warehouse Detail，融合 + 清洗）
    - DWS：汇总层（Data Warehouse Summary，主题聚合）
    - ADS：应用层（Application Data Store，面向场景的宽表）
    - KPI：指标层（Catalog 度量）
    - AI：AI 推理产物（Feature / Embedding 等）
    """

    SOURCE_SYSTEM = "SOURCE_SYSTEM"
    ODS = "ODS"
    DWD = "DWD"
    DWS = "DWS"
    ADS = "ADS"
    KPI = "KPI"
    AI = "AI"


class RefreshFrequency(str, Enum):
    """血缘边刷新频率（Phase 2.1）。"""

    REALTIME = "REALTIME"
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class EntityType(str, Enum):
    """跨系统实体类型（Phase 3.1）。

    对应采购域业务对象目录：供应商 / 物料 / 采购订单 / 收货 / 来料检验 / 不合格处理。
    """

    SUPPLIER = "SUPPLIER"
    MATERIAL = "MATERIAL"
    PO = "PO"
    GR = "GR"
    IQC = "IQC"
    NCR = "NCR"


class SourceSystem(str, Enum):
    """源业务系统（Phase 3.1）。

    覆盖采购域涉及的系统：ERP（Sage X3）/ SRM / QMS / MDM / PLM。
    """

    ERP = "ERP"
    SRM = "SRM"
    QMS = "QMS"
    MDM = "MDM"
    PLM = "PLM"


class MatchRule(str, Enum):
    """编码匹配规则（Phase 3.1）。

    - MDM_MASTER：以 MDM 主数据记录为准（source_key 即 MDM 主键）
    - BUSINESS_KEY：按业务键规则匹配（如 PO 号 + 行号）
    - MAPPING：人工/规则映射表
    """

    MDM_MASTER = "MDM_MASTER"
    BUSINESS_KEY = "BUSINESS_KEY"
    MAPPING = "MAPPING"
