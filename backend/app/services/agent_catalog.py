"""M3-3 采购 Agent 场景目录（设计稿 §5.2 / §8.4）。

`GET /api/v1/procurement/agents/catalog`：返回采购域 Agent 的场景目录——
可用工具 + 能力说明 + 可运行性，**不暴露 handlerRef**（能力面是给调用方/
前端做场景选择的，不是给实现面的）。

目录口径：`agent_code` 以 ``PROCUREMENT_`` 开头（场景目录是给采购 Copilot 的
场景选择器用的；全局/知识 Agent 不进这个目录）。可运行性 = tool_name 非空
且 status=active 且该工具 config enabled。
"""

from __future__ import annotations

from typing import Any

from app.domain.knowledge_schemas import (
    ProcurementAgentCatalogEntryRead,
    ProcurementAgentToolRead,
)


def buildProcurementCatalog(
    agentRows: list[Any],
    toolConfigByName: dict[str, Any],
) -> list[ProcurementAgentCatalogEntryRead]:
    """agent_definition 行（+工具 config）→ 目录条目（纯函数，新列表）。"""
    entries: list[ProcurementAgentCatalogEntryRead] = []
    for row in agentRows:
        toolName = getattr(row, "tool_name", None)
        toolRead: ProcurementAgentToolRead | None = None
        runnable = False
        if toolName:
            config = toolConfigByName.get(toolName)
            if config is not None:
                toolRead = ProcurementAgentToolRead(
                    name=toolName,
                    description=getattr(config, "description", None),
                    data_object=getattr(config, "data_object", None),
                    data_layers=tuple(getattr(config, "data_layers", None) or ()),
                )
                enabled = bool(getattr(config, "enabled", False))
                runnable = enabled
        entries.append(
            ProcurementAgentCatalogEntryRead(
                agent_code=row.agent_code,
                agent_name=row.agent_name,
                description=row.description,
                status=str(row.status),
                runnable=runnable,
                tool=toolRead,
            )
        )
    return entries
