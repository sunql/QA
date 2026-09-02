"""Agent 词表 SSOT（Phase 7 feat-agent-vocabulary）。

锁定 `AgentDefinition.data_domains` / `data_layers` 的合法值集合。
**词表 SSOT 在代码常量**——零 Alembic 迁移；新增域/层需改本文件并发版。
近期规模：域 3 个（PROCUREMENT/QUALITY/LOGISTICS），层 3 个（DIM/DWD/FEATURE）。

与 `LineageLayer`（enums.py:144，7 层血缘 SOURCE_SYSTEM/ODS/.../AI）的边界：
同名 DWD 是巧合——`LineageLayer` 是数据血缘词汇，`AGENT_DATA_LAYERS` 是 Agent
授权与展示词汇，语义不同，不可混用。
"""

from __future__ import annotations

AGENT_DATA_DOMAINS: tuple[str, ...] = ("PROCUREMENT", "QUALITY", "LOGISTICS")
AGENT_DATA_LAYERS: tuple[str, ...] = ("DIM", "DWD", "FEATURE")


def normalizeAgentDomain(value: str) -> str:
    """域归一化：strip + upper，空串（含纯空白）→ ValueError。

    与 `schemas._normalizeDataLayer` 同模式（strip+upper），保证写入边界一致。
    """
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("data_domains 元素不允许为空字符串")
    return normalized
