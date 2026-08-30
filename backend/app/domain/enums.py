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
