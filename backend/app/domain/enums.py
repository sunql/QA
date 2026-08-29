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
