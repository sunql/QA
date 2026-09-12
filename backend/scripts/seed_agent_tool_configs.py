"""幂等 upsert 内置工具（feat-agent-tool-config-db, 2026-09-03；M8 起 7 个）。

lifespan 每次启动调用；name 命中 → 比较并更新元数据；未命中 → 插入。
不做 audit（seed 性质；不算业务写入）。

handler / extractor 引擎在代码（app.services.agent_tools）—— DB 仅存
元数据（name / description / data_object / data_layers / handler_kind /
handler_ref / arg_extractor_kind / enabled）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AgentToolConfig
from app.domain.schemas import _normalizeDataObject
from app.services.agent_tool_config_service import AgentToolConfigService

logger = logging.getLogger(__name__)


TOOL_SEEDS: list[dict[str, Any]] = [
    {
        "name": "supplier_360",
        "description": "查询单供应商 360° 视图（主数据 + 交付/质量/价格表现 + 跨系统编码）",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "FEATURE"],
        "handler_kind": "BUILTIN",
        "handler_ref": "supplier_360",
        "arg_extractor_kind": "supplier_key",
        "enabled": True,
    },
    {
        "name": "supplier_risk",
        "description": "评估单供应商风险等级（RISK_SCORE 主路径 + LLM 风险点）",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "FEATURE"],
        "handler_kind": "BUILTIN",
        "handler_ref": "supplier_risk",
        "arg_extractor_kind": "supplier_risk_key",
        "enabled": True,
    },
    {
        "name": "graph_traverse",
        "description": "供应链链路推理：从供应商出发的多跳可达业务实体",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "DWD"],
        "handler_kind": "BUILTIN",
        "handler_ref": "graph_traverse",
        "arg_extractor_kind": "supplier_graph_key",
        "enabled": True,
    },
    # ---- feat-wiki-knowledge M8：企业 Wiki 知识工具 ----
    # data_layers 一律为空（层无关）：知识条目不属于 DIM/DWD/FEATURE 任何一层，
    # 硬塞一个层会得到一个语义错误的授权。ACL 退化为对象粒度（data_object）。
    {
        "name": "wiki_search",
        "description": "检索企业知识条目：按标题/正文模糊匹配，返回摘要与维度",
        "data_object": "WIKI_PAGE",
        "data_layers": [],
        "handler_kind": "BUILTIN",
        "handler_ref": "wiki_search",
        "arg_extractor_kind": "wiki_text",
        "enabled": True,
    },
    {
        "name": "wiki_read",
        "description": "读取单条知识：全文 + 事实原子 + 已确认关系 + 结构化规则/流程",
        "data_object": "WIKI_PAGE",
        "data_layers": [],
        "handler_kind": "BUILTIN",
        "handler_ref": "wiki_read",
        "arg_extractor_kind": "wiki_text",
        "enabled": True,
    },
    {
        "name": "rule_evaluate",
        "description": "对知识条目上的可执行业务规则跑 dry-run，报告通过/失败/判不了三态",
        "data_object": "WIKI_RULE",
        "data_layers": [],
        "handler_kind": "BUILTIN",
        "handler_ref": "rule_evaluate",
        "arg_extractor_kind": "wiki_text",
        "enabled": True,
    },
    {
        "name": "coverage_status",
        "description": "知识覆盖度：矩阵汇总 + 最紧迫缺口 + 未挂业务对象的条目",
        "data_object": "WIKI_COVERAGE",
        "data_layers": [],
        "handler_kind": "BUILTIN",
        "handler_ref": "coverage_status",
        "arg_extractor_kind": "wiki_no_args",
        "enabled": True,
    },
]


def _needsUpdate(row: AgentToolConfig, seed: dict) -> bool:
    """比较行与 seed 元数据，决定是否需要 upsert。"""
    return (
        row.description != seed.get("description")
        or row.data_object != _normalizeDataObject(seed["data_object"])
        or list(row.data_layers or []) != list(seed.get("data_layers", []))
        or row.handler_kind != seed["handler_kind"]
        or row.handler_ref != seed["handler_ref"]
        or row.arg_extractor_kind
        != seed.get("arg_extractor_kind", "supplier_key")
    )


async def seedAgentToolConfigs(session: AsyncSession) -> int:
    """幂等 upsert 全部种子工具。返回改动行数（用于 lifespan 日志）。"""
    service = AgentToolConfigService()
    changed = 0
    for seed in TOOL_SEEDS:
        existing = (
            await session.execute(
                select(AgentToolConfig).where(AgentToolConfig.name == seed["name"])
            )
        ).scalar_one_or_none()
        if existing is None or _needsUpdate(existing, seed):
            await service.upsertSeed(session, seed["name"], seed)
            changed += 1
    if changed:
        await session.flush()
        logger.info("seedAgentToolConfigs: %d/%d tools updated", changed, len(TOOL_SEEDS))
    return changed


async def main() -> None:
    """Standalone 入口：连真实 PG 并跑一次 seed（运维 / CI 用）。"""
    from app.infrastructure.database import getSessionFactory
    from app.tests import _pg_support  # noqa: F401  # 仅触发 settings env loading

    factory = getSessionFactory()
    async with factory() as session:
        count = await seedAgentToolConfigs(session)
        await session.commit()
        print(f"seed_agent_tool_configs: {count}/{len(TOOL_SEEDS)} changed")


if __name__ == "__main__":
    asyncio.run(main())