"""真实数据验证脚本的**数据库访问 helper**（按职责从入口脚本拆出）。

包含五类东西：

1. :class:`ToolRow` —— ``OrchestrationToolRow`` 协议（``agent_orchestration_validation.py:61``）
   的结构化实现。``assertOrchestrationToolValid`` / ``isOrchestrationToolValid``
   是**纯函数**（只读 5 个字段、不碰 DB），所以可以把数据库行适配成这个
   dataclass 后自由地做「副本」实验（**不改库**）。
2. 行读取helper（工具行 / schema 快照 / enabled 快照 / 既有表行数）。
3. 阶段 0 的 **snapshot → 修改 → 还原**（退出时把库恢复原状）。
4. 前置条件自建行（没有 ACTIVE Agent / 没有 active 模型配置时自建，退出时删除）。
5. 清理期的查询（脚本自建行清单 / 残留计数）与前置条件准备
   （:func:`prepareAgent` / :func:`prepareModelConfig`）。

⚠️ 本模块 import ``app.*`` ⇒ 必须在入口脚本设置好 ``DATABASE_URL`` **之后**
才被 import。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from feat_agent_orchestration_realdata_lib import (
    DISABLED_WIKI_TOOLS,
    FIRST_WAVE_TOOLS,
    LEGACY_TABLES,
    MARKER,
    SCRIPT_ACTOR,
    SCRIPT_AGENT_CODE,
    SCRIPT_MODEL_NAME,
    failStep,
    skipStep,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AgentStatus
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import AgentDefinition, LlmConfig

# ---------------------------------------------------------------------------
# 失败分类：前置条件形态 vs 真正的缺陷
# ---------------------------------------------------------------------------
#
# ``SKIP`` 的含义是「这一步在当前库上**无法执行**」——库的种子不满足前提
# （没有 ACTIVE 的 Agent / 没有启用的模型配置 / 工具被禁用 / 工具行不存在 …）。
# 这些是**领域异常**，脚本按具体类型判定，不靠「捕获所有异常」蒙混：
#
# - ``PREREQUISITE_ERRORS``（下面这个元组）⇒ SKIP（写明缺什么）；
# - 其它任何异常 ⇒ **FAIL**（脚本/环境缺陷，必须暴露，不能被吞掉）。
#
# ⚠️ 领域异常的语义：
# - ``NotFoundError``：Agent / 模型配置 / profile / run 不存在；
# - ``ConflictError``：Agent 已有 profile、profile 已禁用、模型配置已停用、
#   编排配置没有可用的工具绑定；
# - ``ValidationError``：工具不存在 / 工具已禁用 / 工具不在只读白名单 /
#   schema 不合法（``assertOrchestrationToolValid`` 抛的）。
PREREQUISITE_ERRORS = (NotFoundError, ConflictError, ValidationError)


@dataclass(frozen=True)
class ToolRow:
    """``OrchestrationToolRow`` 协议的结构化实现（**不依赖 ORM**）。

    字段与 ``agent_orchestration_validation.OrchestrationToolRow`` 一一对应：
    ``name`` / ``enabled`` / ``handler_kind`` / ``handler_ref`` / ``input_schema``。
    """

    name: str
    enabled: bool
    handler_kind: str
    handler_ref: str
    input_schema: Any


def toolRow(row: Any) -> ToolRow:
    """把 ``agent_tool_config`` 的一行适配成 :class:`ToolRow`。"""
    return ToolRow(
        name=row["name"],
        enabled=bool(row["enabled"]),
        handler_kind=row["handler_kind"],
        handler_ref=row["handler_ref"],
        input_schema=row["input_schema"],
    )


async def fetchToolRow(session: AsyncSession, name: str) -> Any | None:
    """读取 ``agent_tool_config`` 的一行（``None`` 表示该工具不存在）。"""
    return (
        await session.execute(
            text(
                "SELECT id, name, enabled, handler_kind, handler_ref, input_schema "
                "FROM agent_tool_config WHERE name = :name"
            ),
            {"name": name},
        )
    ).mappings().first()


async def fetchToolRows(session: AsyncSession, names: tuple[str, ...]) -> dict[str, ToolRow]:
    """批量读取工具行并适配成 :class:`ToolRow`（按 name 索引）。"""
    rows = (
        await session.execute(
            text(
                "SELECT name, enabled, handler_kind, handler_ref, input_schema "
                "FROM agent_tool_config WHERE name = ANY(:names)"
            ),
            {"names": list(names)},
        )
    ).mappings().all()
    return {row["name"]: toolRow(row) for row in rows}


async def snapshotToolSchemas(session: AsyncSession) -> dict[str, Any]:
    """记录第一波三个工具的**原始** ``input_schema``（退出时还原）。"""
    rows = (
        await session.execute(
            text(
                "SELECT name, input_schema FROM agent_tool_config "
                "WHERE name = ANY(:names)"
            ),
            {"names": list(FIRST_WAVE_TOOLS)},
        )
    ).mappings().all()
    return {row["name"]: row["input_schema"] for row in rows}


async def snapshotEnabledFlags(session: AsyncSession) -> dict[str, bool]:
    """记录 4 个 wiki 工具的 ``enabled``（脚本运行前的观测状态）。"""
    rows = (
        await session.execute(
            text(
                "SELECT name, enabled FROM agent_tool_config "
                "WHERE name = ANY(:names)"
            ),
            {"names": list(DISABLED_WIKI_TOOLS)},
        )
    ).mappings().all()
    return {row["name"]: bool(row["enabled"]) for row in rows}


async def legacyCounts(session: AsyncSession) -> dict[str, int]:
    """六张既有表的行数（``agent_definition`` 排除脚本自建的那一行）。"""
    counts: dict[str, int] = {}
    for table in LEGACY_TABLES:
        if table == "agent_definition":
            value = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM agent_definition "
                        "WHERE agent_code <> :code"
                    ),
                    {"code": SCRIPT_AGENT_CODE},
                )
            ).scalar_one()
        else:
            value = (
                await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
        counts[table] = int(value)
    return counts


async def writeToolSchema(session: AsyncSession, toolId: Any, schema: Any) -> int:
    """写入某个工具的 ``input_schema``（**不动** ``enabled``）。

    返回 UPDATE **实际命中的行数**：
    - ``1`` ⇒ 真的写进去了；
    - ``0`` ⇒ 该行不存在或 ``enabled IS TRUE`` 不成立（只写启用的行）⇒ **没写**。

    ⚠️ 调用方必须用这个行数汇报，**不能**无条件声称「写了 N 行」。
    """
    result = await session.execute(
        text(
            "UPDATE agent_tool_config SET input_schema = :schema "
            "WHERE id = :id AND enabled IS TRUE"
        ),
        {"schema": json.dumps(schema), "id": toolId},
    )
    return int(result.rowcount or 0)


async def restoreToolSchemas(
    session: AsyncSession, originalSchemas: dict[str, Any]
) -> dict[str, bool]:
    """把第一波三个工具的 ``input_schema`` 写回运行前的原值，并逐行核验。"""
    for name, original in originalSchemas.items():
        await session.execute(
            text(
                "UPDATE agent_tool_config SET input_schema = :schema WHERE name = :name"
            ),
            {"schema": json.dumps(original), "name": name},
        )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT name, input_schema FROM agent_tool_config "
                "WHERE name = ANY(:names)"
            ),
            {"names": list(originalSchemas)},
        )
    ).mappings().all()
    return {
        row["name"]: row["input_schema"] == originalSchemas.get(row["name"])
        for row in rows
    }


async def pickActiveAgentCode(session: AsyncSession) -> str | None:
    """挑一个既有 ACTIVE Agent（优先绑了第一波工具的），且尚无编排 profile。"""
    rows = (
        await session.execute(
            text(
                "SELECT a.agent_code FROM agent_definition a "
                "LEFT JOIN agent_orchestration_profile p ON p.agent_id = a.id "
                "WHERE a.status = 'active' AND p.id IS NULL "
                "ORDER BY (a.tool_name IN ('supplier_360','supplier_risk','graph_traverse')) DESC, "
                "a.agent_code LIMIT 5"
            )
        )
    ).scalars().all()
    return rows[0] if rows else None


async def ensureAgent(session: AsyncSession) -> tuple[str, bool]:
    """确保有一个可用的 ACTIVE Agent，返回 ``(agent_code, 是否脚本自建)``。

    优先**借用**既有 ACTIVE Agent（脚本不删它）；库里一个都没有时**自建**
    一个（退出时删除，代号固定，可与既有行区分）。
    """
    existing = await pickActiveAgentCode(session)
    if existing is not None:
        return existing, False

    await session.execute(
        text("DELETE FROM agent_definition WHERE agent_code = :code"),
        {"code": SCRIPT_AGENT_CODE},
    )
    await session.commit()
    session.add(
        AgentDefinition(
            agent_code=SCRIPT_AGENT_CODE,
            agent_name="Realdata Orchestration Script Agent",
            status=AgentStatus.ACTIVE.value,
            data_domains=[],
            data_layers=[],
            tool_name="supplier_360",
        )
    )
    await session.commit()
    return SCRIPT_AGENT_CODE, True


async def ensureModelConfig(session: AsyncSession) -> tuple[int, bool]:
    """确保有一个启用的模型配置，返回 ``(model_id, 是否脚本自建)``。"""
    existing = (
        await session.execute(
            text("SELECT id FROM llm_config WHERE is_active IS TRUE ORDER BY id LIMIT 1")
        )
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing), False

    await session.execute(
        text("DELETE FROM llm_config WHERE model_name = :name"),
        {"name": SCRIPT_MODEL_NAME},
    )
    await session.commit()
    model = LlmConfig(
        model_name=SCRIPT_MODEL_NAME,
        provider="openai",
        is_active=True,
    )
    session.add(model)
    await session.flush()
    await session.commit()
    return int(model.id), True


async def deleteScriptRows(session: AsyncSession) -> None:
    """删除本脚本自建 / 自留的一切行（run → profile → 审计 outbox → agent → 模型配置）。"""
    marker = f"%{MARKER}%"
    await session.execute(
        text("DELETE FROM agent_orchestration_run WHERE question LIKE :marker"),
        {"marker": marker},
    )
    await session.execute(
        text("DELETE FROM agent_orchestration_profile WHERE instructions LIKE :marker"),
        {"marker": marker},
    )
    # 配置 / 运行 service 会在同一事务里 enqueue 审计 outbox 行；那也是脚本自留的。
    await session.execute(
        text(
            "DELETE FROM audit_outbox WHERE actor = :actor "
            "AND entity_type IN ('agent_orchestration_profile', 'agent_orchestration_run')"
        ),
        {"actor": SCRIPT_ACTOR},
    )
    await session.execute(
        text("DELETE FROM agent_definition WHERE agent_code = :code"),
        {"code": SCRIPT_AGENT_CODE},
    )
    await session.execute(
        text("DELETE FROM llm_config WHERE model_name = :name"),
        {"name": SCRIPT_MODEL_NAME},
    )
    await session.commit()


async def fetchScriptRows(session: AsyncSession) -> tuple[list[Any], list[Any]]:
    """本脚本自建的 run 行与 profile 行（按 marker 匹配）。"""
    runs = (
        await session.execute(
            text("SELECT run_id, status FROM agent_orchestration_run WHERE question LIKE :m"),
            {"m": f"%{MARKER}%"},
        )
    ).all()
    profiles = (
        await session.execute(
            text(
                "SELECT agent_id, enabled, revision FROM agent_orchestration_profile "
                "WHERE instructions LIKE :m"
            ),
            {"m": f"%{MARKER}%"},
        )
    ).all()
    return list(runs), list(profiles)


async def countScriptResidue(session: AsyncSession) -> int:
    """本脚本自建行的**残留计数**（run + profile + 自建 Agent + 自建模型配置）。"""
    remaining = (
        await session.execute(
            text(
                "SELECT (SELECT count(*) FROM agent_orchestration_run WHERE question LIKE :m)"
                " + (SELECT count(*) FROM agent_orchestration_profile WHERE instructions LIKE :m)"
                " + (SELECT count(*) FROM agent_definition WHERE agent_code = :agentCode)"
                " + (SELECT count(*) FROM llm_config WHERE model_name = :modelName)"
            ),
            {
                "m": f"%{MARKER}%",
                "agentCode": SCRIPT_AGENT_CODE,
                "modelName": SCRIPT_MODEL_NAME,
            },
        )
    ).scalar_one()
    return int(remaining)


async def prepareAgent(session: AsyncSession, stepLabel: str) -> str | None:
    """准备一个可用的 ACTIVE Agent（优先借用既有的；没有就自建）。

    **前置条件形态**的领域异常（``PREREQUISITE_ERRORS``）⇒ ``SKIP``（写明缺什么）；
    **其它**异常（``IntegrityError`` / ``OperationalError`` 等，即脚本或环境的
    缺陷）⇒ ``FAIL``——必须暴露，不能被当成「库种子不满足」蒙混过去。

    返回 ``None`` 表示这一步已被判定（跳过或失败）。
    """
    try:
        agentCode, isSelfCreated = await ensureAgent(session)
    except PREREQUISITE_ERRORS as error:
        skipStep(
            stepLabel,
            f"无法准备 ACTIVE 的 agent_definition（{type(error).__name__}: {error}）"
            "；本步骤在当前库上无法执行",
        )
        return None
    except Exception as error:  # noqa: BLE001 — 非前置条件异常 = 缺陷，记 FAIL 暴露
        failStep(
            stepLabel,
            f"准备 ACTIVE 的 agent_definition 抛出非前置条件异常"
            f"（{type(error).__name__}: {error}）",
        )
        return None
    origin = "脚本自建（退出时删除）" if isSelfCreated else "借用既有 Agent（不删除）"
    print(f"      使用 Agent：{agentCode}（{origin}）")
    return agentCode


async def prepareModelConfig(session: AsyncSession, stepLabel: str) -> int | None:
    """准备一个启用的模型配置（优先借用既有的；没有就自建）。

    异常分类与 :func:`prepareAgent` 一致：前置条件形态 ⇒ ``SKIP``，
    其它异常 ⇒ ``FAIL``。

    返回 ``None`` 表示这一步已被判定（跳过或失败）。
    """
    try:
        modelId, isSelfCreated = await ensureModelConfig(session)
    except PREREQUISITE_ERRORS as error:
        skipStep(
            stepLabel,
            f"无法准备 is_active 的 llm_config（{type(error).__name__}: {error}）；"
            "本步骤在当前库上无法执行",
        )
        return None
    except Exception as error:  # noqa: BLE001 — 非前置条件异常 = 缺陷，记 FAIL 暴露
        failStep(
            stepLabel,
            f"准备 is_active 的 llm_config 抛出非前置条件异常"
            f"（{type(error).__name__}: {error}）",
        )
        return None
    origin = "脚本自建（退出时删除）" if isSelfCreated else "借用既有模型配置（不删除）"
    print(f"      使用模型配置：id={modelId}（{origin}）")
    return int(modelId)
