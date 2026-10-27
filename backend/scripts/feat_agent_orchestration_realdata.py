"""通用多工具 Agent 编排：真实数据验证脚本（feat-agent-orchestration）。

目的
----
在**真实 PostgreSQL**（``qa_metadata``，prod 同源）上验证本特性的关键不变量，
每一步输出一行 ``PASS`` / ``FAIL``，任一失败即以非零码退出。

覆盖的步骤
----------
1. 迁移版本 = ``0135``，五张新表都在（``agent_orchestration_profile`` /
   ``agent_orchestration_binding`` / ``agent_orchestration_run`` /
   ``agent_orchestration_step`` / ``agent_orchestration_usage``）。
2. 设计 §2.2 点名的**六张既有表**行数在「特性的写入路径」前后完全一致
   （``agent_definition`` / ``agent_access_policy`` / ``agent_tool_config`` /
   ``agent_schedule`` / ``agent_run_log`` / ``session_token_usage``）。
3. ``validateObjectSchema`` 真的排除 ``{}``（空 schema 非法）。
4. **禁用工具路径**（不写任何数据）：绑定 ``wiki_search`` 会被后端自己的
   ``ValidationError("工具已禁用：wiki_search")`` 拒绝（API 边界即 422）。
5. 阶段 0 的**只修 schema**动作：只改三个**已启用**工具
   （``supplier_360`` / ``supplier_risk`` / ``graph_traverse``）的
   ``input_schema``，**绝不翻 ``enabled``**。
   ⚠️ 理由：4 个 wiki 工具被 4 个既有 legacy Agent 绑定
   （``WIKI_SEARCH_AGENT`` / ``WIKI_READ_AGENT`` / ``WIKI_RULE_AGENT`` /
   ``WIKI_COVERAGE_AGENT``，均 ``status=active``）；既有运行时只加载
   ``enabled=true`` 的工具行（``agent_tool_config_registry.py:33,79``），
   否则抛 ``ConflictError(409 tool_unbound_or_disabled)``
   （``agent_runtime_service.py:165-178``）⇒ 翻转 ``enabled`` 会让那 4 个
   **既有** Agent 从「409 不可运行」变成「真的执行」，属于对现有功能的行为变更。
   改 ``input_schema`` 本身对既有链路是**惰性**的（既有参数来自
   ``arg_extractor``），所以本步骤不改变任何既有行为。
6. 修复后三个工具 ``isOrchestrationToolValid`` 为真；四个 wiki 工具的
   ``enabled`` 仍然是 ``false``（证明脚本没有翻转开关）。
7. 为一个**既有** Agent 建编排 profile：创建后 ``enabled=false`` /
   ``revision=1``；toggle 之后 ``revision=2``。
8. 建一次 run：返回 ``QUEUED`` + ``runId``；快照里只有只读白名单里的工具名
   ⇒ 只读性成立（本脚本不执行任何工具）。
9. feature-off 路径：``ENABLE_AGENT_ORCHESTRATION=false`` 时新路由不出现在
   ``/openapi.json``。

幂等
----
- 脚本**只写自己拥有的行**：profile 用 ``instructions`` 里的标记串
  （``MARKER``）识别，run 用 ``question`` 里的标记串识别；每次运行先删掉自己
  上一次的残留再重建。
- 步骤 5 的 schema 写入是**幂等**的：值相同就不写。
- 对 ``agent_tool_config`` 的修改只改 ``input_schema``（行数不变），
  并且脚本会打印改动前后的原值，便于按 ``<表名>_<YYYYMMDD>`` 约定回溯。

用法
----
::

    # 显式指定 DSN（推荐；口令不要写进仓库）
    export AGENT_ORCHESTRATION_REALDATA_DATABASE_URL='postgresql+asyncpg://<user>:<pwd>@localhost:5433/qa_metadata'
    cd backend && ./.venv/bin/python scripts/feat_agent_orchestration_realdata.py

    # 或用 PG* 变量拼装（PGPASSWORD 必填）
    PGPASSWORD=... PGHOST=localhost PGPORT=5433 PGUSER=qa_user PGDATABASE=qa_metadata \
        ./.venv/bin/python scripts/feat_agent_orchestration_realdata.py

⚠️ 绝不指向生产服务器。本脚本会写库。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行：把 backend/ 放进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# 配置（必须在 import app.* 之前解析：app.config 读 DATABASE_URL）
# ---------------------------------------------------------------------------

MARKER = "feat-agent-orchestration-realdata"

EXPECTED_ALEMBIC_VERSION = "0135"

NEW_TABLES = (
    "agent_orchestration_profile",
    "agent_orchestration_binding",
    "agent_orchestration_run",
    "agent_orchestration_step",
    "agent_orchestration_usage",
)

# 设计 §2.2：结构/约束/语义不得改动的既有表
LEGACY_TABLES = (
    "agent_definition",
    "agent_access_policy",
    "agent_tool_config",
    "agent_schedule",
    "agent_run_log",
    "session_token_usage",
)

# 阶段 0：schema-only 修复目标（这三个工具本来就 enabled=true）
FIRST_WAVE_TOOLS = ("supplier_360", "supplier_risk", "graph_traverse")

# 这四个工具必须保持 enabled=false（本脚本绝不碰）
DISABLED_WIKI_TOOLS = ("wiki_search", "wiki_read", "rule_evaluate", "coverage_status")

# 灰度手册 §2.3 的权威 schema（脚本与手册必须同源，不能各写一份）
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "supplier_360": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "additionalProperties": False,
    },
    "supplier_risk": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "additionalProperties": False,
    },
    "graph_traverse": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "additionalProperties": False,
    },
    "wiki_search": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "additionalProperties": False,
    },
    "wiki_read": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "additionalProperties": False,
    },
    "rule_evaluate": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "additionalProperties": False,
    },
    "coverage_status": {"type": "object", "properties": {}},
}


def _maskDsn(dsn: str) -> str:
    """口令一律打码（``user:***@host``）。"""
    if "@" not in dsn or "://" not in dsn:
        return "***"
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"


def _resolveDatabaseUrl() -> str:
    """DSN 解析顺序：显式变量 → DATABASE_URL → PG* 变量拼装。"""
    explicit = os.environ.get("AGENT_ORCHESTRATION_REALDATA_DATABASE_URL")
    if explicit:
        return explicit
    fallback = os.environ.get("DATABASE_URL")
    if fallback:
        return fallback
    password = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise SystemExit(
            "缺少数据库口令：请设置 AGENT_ORCHESTRATION_REALDATA_DATABASE_URL "
            "（或 DATABASE_URL / PGPASSWORD）。脚本绝不内置任何口令。"
        )
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5433")
    user = os.environ.get("PGUSER", "qa_user")
    database = os.environ.get("PGDATABASE", "qa_metadata")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


DATABASE_URL = _resolveDatabaseUrl()
os.environ["DATABASE_URL"] = DATABASE_URL
# 默认静默 SQL 回显（脚本输出只保留 PASS/FAIL 行；需要排查时设 LOG_LEVEL=INFO）
os.environ.setdefault("LOG_LEVEL", "WARNING")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.agent_orchestration_schemas import (  # noqa: E402
    AgentOrchestrationProfileCreate,
    AgentOrchestrationToggle,
    AgentRunCreate,
)
from app.domain.exceptions import ValidationError  # noqa: E402
from app.domain.models import AgentDefinition, LlmConfig  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.agent_orchestration_config_service import (  # noqa: E402
    AgentOrchestrationConfigService,
)
from app.services.agent_orchestration_run_service import (  # noqa: E402
    AgentOrchestrationRunService,
)
from app.services.agent_orchestration_validation import (  # noqa: E402
    READ_ONLY_HANDLER_REFS,
    assertOrchestrationToolValid,
    isOrchestrationToolValid,
    validateObjectSchema,
)

# 引擎在非生产环境默认 echo SQL（info 级）；本脚本的输出只保留 PASS/FAIL 行。
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# 输出helpers
# ---------------------------------------------------------------------------

FAILURES: list[str] = []


def passStep(step: str, detail: str = "") -> None:
    print(f"PASS  {step}" + (f" — {detail}" if detail else ""))


def failStep(step: str, detail: str) -> None:
    FAILURES.append(f"{step}: {detail}")
    print(f"FAIL  {step} — {detail}")


def check(step: str, ok: bool, detail: str = "") -> bool:
    if ok:
        passStep(step, detail)
    else:
        failStep(step, detail or "断言不成立")
    return ok


# ---------------------------------------------------------------------------
# 步骤实现
# ---------------------------------------------------------------------------


async def step1_schema(session: AsyncSession) -> None:
    """迁移版本 = 0135，五张新表都在。"""
    version = (
        await session.execute(text("SELECT version_num FROM alembic_version"))
    ).scalar_one_or_none()
    check(
        "S1 迁移版本",
        version == EXPECTED_ALEMBIC_VERSION,
        f"version_num={version} (期望 {EXPECTED_ALEMBIC_VERSION})",
    )
    rows = (
        await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(:names)"
            ),
            {"names": list(NEW_TABLES)},
        )
    ).scalars().all()
    missing = sorted(set(NEW_TABLES) - set(rows))
    check(
        "S1 五张新表",
        not missing,
        f"存在 {len(rows)}/5" + (f"，缺失 {missing}" if missing else ""),
    )


async def _legacyCounts(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in LEGACY_TABLES:
        value = (
            await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
        ).scalar_one()
        counts[table] = int(value)
    return counts


async def step3_schemaValidation() -> None:
    """``validateObjectSchema`` 真的排除 ``{}``。"""
    try:
        validateObjectSchema({})
    except ValidationError as error:
        passStep("S3 空 schema 被拒", f"ValidationError: {error}")
    else:
        failStep("S3 空 schema 被拒", "validateObjectSchema({}) 竟然通过了")

    validateObjectSchema(TOOL_SCHEMAS["supplier_360"])
    passStep("S3 合法 schema 通过", "supplier_360 的 §2.3 schema 被接受")


async def step4_disabledToolPath(session: AsyncSession) -> None:
    """禁用工具路径：绑定 wiki_search 必须被后端自己的校验拒绝（不写库）。"""
    row = (
        await session.execute(
            text(
                "SELECT name, enabled, handler_kind, handler_ref, input_schema "
                "FROM agent_tool_config WHERE name = :name"
            ),
            {"name": "wiki_search"},
        )
    ).mappings().first()
    if row is None:
        failStep("S4 禁用工具路径", "agent_tool_config 里没有 wiki_search 行")
        return
    check(
        "S4 wiki_search 仍是禁用态",
        not bool(row["enabled"]),
        f"enabled={row['enabled']}（脚本不翻转它）",
    )
    try:
        assertOrchestrationToolValid(row, "wiki_search")
    except ValidationError as error:
        expected = "工具已禁用：wiki_search"
        check(
            "S4 禁用工具被拒",
            str(error) == expected,
            f"message={error!s} (期望 {expected!r})",
        )
    else:
        failStep("S4 禁用工具被拒", "assertOrchestrationToolValid 没有拒绝禁用工具")


async def step5_fixSchemas(session: AsyncSession) -> None:
    """阶段 0 的 schema-only 修复（不动 ``enabled``）。"""
    changed = 0
    for name in FIRST_WAVE_TOOLS:
        row = (
            await session.execute(
                text(
                    "SELECT id, name, enabled, handler_ref, input_schema "
                    "FROM agent_tool_config WHERE name = :name"
                ),
                {"name": name},
            )
        ).mappings().first()
        if row is None:
            failStep("S5 schema 修复", f"agent_tool_config 里没有 {name} 行")
            continue
        target = TOOL_SCHEMAS[name]
        before = row["input_schema"]
        if before == target:
            print(f"      {name}: schema 已是目标值，未写入（enabled={row['enabled']}）")
            continue
        await session.execute(
            text(
                "UPDATE agent_tool_config SET input_schema = :schema "
                "WHERE id = :id AND enabled IS TRUE"
            ),
            {"schema": json.dumps(target), "id": row["id"]},
        )
        changed += 1
        print(
            f"      {name}: input_schema {json.dumps(before, ensure_ascii=False)}"
            f" -> {json.dumps(target, ensure_ascii=False)}"
            f"（enabled 保持 {row['enabled']} 未改）"
        )
    await session.commit()
    passStep("S5 schema-only 修复", f"实际写入 {changed} 行（enabled 未翻转）")


async def step6_toolOptions(session: AsyncSession) -> None:
    """修复后：3 个已启用工具可用，4 个 wiki 工具仍禁用。"""
    rows = (
        await session.execute(
            text(
                "SELECT name, enabled, handler_kind, handler_ref, input_schema "
                "FROM agent_tool_config WHERE name = ANY(:names)"
            ),
            {"names": list(FIRST_WAVE_TOOLS) + list(DISABLED_WIKI_TOOLS)},
        )
    ).mappings().all()
    byName = {row["name"]: row for row in rows}

    okFirst = all(
        isOrchestrationToolValid(byName.get(name)) for name in FIRST_WAVE_TOOLS
    )
    check(
        "S6 三个已启用工具可编排",
        okFirst,
        ", ".join(
            f"{name}={isOrchestrationToolValid(byName.get(name))}"
            for name in FIRST_WAVE_TOOLS
        ),
    )
    stillDisabled = [
        name for name in DISABLED_WIKI_TOOLS if not byName.get(name)
        or bool(byName[name]["enabled"])
    ]
    check(
        "S6 四个 wiki 工具仍未启用",
        not stillDisabled,
        "全部 enabled=false（证明脚本没有改变既有 legacy Agent 的可运行性）",
    )
    check(
        "S6 只读白名单仍是 7 项",
        len(READ_ONLY_HANDLER_REFS) == 7,
        f"READ_ONLY_HANDLER_REFS={sorted(READ_ONLY_HANDLER_REFS)}",
    )


async def _pickAgent(session: AsyncSession) -> str | None:
    """挑一个既有 ACTIVE Agent（优先有编排工具绑定的），且没有既有 profile。"""
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


async def _cleanupOwnedRows(session: AsyncSession, agentCode: str) -> None:
    """删除本脚本上一次留下的 profile / run（级联删绑定、step、usage）。"""
    await session.execute(
        text(
            "DELETE FROM agent_orchestration_run WHERE question LIKE :marker"
        ),
        {"marker": f"%{MARKER}%"},
    )
    await session.execute(
        text(
            "DELETE FROM agent_orchestration_profile WHERE instructions LIKE :marker"
        ),
        {"marker": f"%{MARKER}%"},
    )
    await session.commit()


async def step7_profile(session: AsyncSession, actor: CurrentUser) -> str | None:
    """建 profile（enabled=false / revision=1）→ toggle（revision=2）。"""
    agentCode = await _pickAgent(session)
    if agentCode is None:
        failStep(
            "S7 编排 profile",
            "没有可用的既有 ACTIVE Agent（都已有 profile）；请先清理或换库",
        )
        return None
    await _cleanupOwnedRows(session, agentCode)

    configService = AgentOrchestrationConfigService()
    created = await configService.create(
        session,
        AgentOrchestrationProfileCreate(
            agent_code=agentCode,
            instructions=(
                f"[{MARKER}] 只读汇总：仅允许调用白名单只读工具，禁止任何写操作。"
            ),
            tool_names=list(FIRST_WAVE_TOOLS),
        ),
        actor,
    )
    check(
        "S7 profile 创建后为停用",
        created.enabled is False and created.revision == 1,
        f"agent={created.agent_code} enabled={created.enabled} revision={created.revision}",
    )
    bound = sorted(binding.tool_name for binding in created.bindings)
    check(
        "S7 绑定的是三个只读工具",
        bound == sorted(FIRST_WAVE_TOOLS),
        f"bindings={bound}",
    )

    toggled = await configService.toggle(
        session,
        agentCode,
        AgentOrchestrationToggle(enabled=True, revision=created.revision),
        actor,
    )
    check(
        "S7 toggle 后启用且 revision=2",
        toggled.enabled is True and toggled.revision == 2,
        f"enabled={toggled.enabled} revision={toggled.revision}",
    )
    return agentCode


async def step8_createRun(
    session: AsyncSession, actor: CurrentUser, agentCode: str
) -> None:
    """建 run：QUEUED + runId；快照只含只读工具（只读性成立）。"""
    modelId = (
        await session.execute(
            text("SELECT id FROM llm_config WHERE is_active IS TRUE ORDER BY id LIMIT 1")
        )
    ).scalar_one_or_none()
    if modelId is None:
        failStep("S8 创建 run", "llm_config 里没有启用中的模型配置")
        return

    runService = AgentOrchestrationRunService()
    created = await runService.createRun(
        session,
        AgentRunCreate(
            agent_code=agentCode,
            input=f"[{MARKER}] 只读冒烟：列出该 Agent 可用的只读能力，不做任何写操作。",
            model_config_id=int(modelId),
        ),
        actor,
    )
    check(
        "S8 run 创建为 QUEUED",
        str(created.status) == "RunStatus.QUEUED" or created.status.value == "QUEUED",
        f"run_id={created.run_id} status={created.status}",
    )
    check("S8 run_id 非空", bool(created.run_id), f"run_id={created.run_id}")

    snapshot = (
        await session.execute(
            text(
                "SELECT tool_bindings_snapshot FROM agent_orchestration_run "
                "WHERE run_id = :runId"
            ),
            {"runId": created.run_id},
        )
    ).scalar_one()
    names = sorted(entry.get("toolName") for entry in snapshot)
    check(
        "S8 快照只含只读白名单工具",
        set(names) <= set(FIRST_WAVE_TOOLS),
        f"snapshot_tools={names}（无任何写工具）",
    )
    print(f"      run_id={created.run_id}（由本脚本创建；Worker 若在跑会接手该 run）")


async def step9_featureOff() -> None:
    """feature-off：新路由不出现在 /openapi.json。"""
    probe = (
        "import json;from app.main import createApp;"
        "app=createApp();"
        "ks=sorted(app.openapi()['paths'].keys());"
        "print(json.dumps([k for k in ks "
        "if 'agent-orchestration' in k or 'agent-runs' in k]))"
    )
    env = dict(os.environ)
    env["ENABLE_AGENT_ORCHESTRATION"] = "false"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
        check=False,
    )
    if result.returncode != 0:
        failStep(
            "S9 feature-off 路由缺席",
            f"探测子进程退出码 {result.returncode}: {result.stderr.strip()[-300:]}",
        )
        return
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    check(
        "S9 feature-off 时新路由缺席",
        paths == [],
        f"/openapi.json 中的编排路径 = {paths}",
    )


async def step10_cleanup(session: AsyncSession, agentCode: str | None) -> None:
    """删除本脚本自建的行 ⇒ 库回到运行脚本前的状态（零残留）。"""
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
    for run in runs:
        print(f"      删除自建 run: run_id={run[0]} status={run[1]}")
    for profile in profiles:
        print(
            f"      删除自建 profile: agent_id={profile[0]} "
            f"enabled={profile[1]} revision={profile[2]}"
        )
    await _cleanupOwnedRows(session, agentCode or "")
    remaining = (
        await session.execute(
            text(
                "SELECT (SELECT count(*) FROM agent_orchestration_run WHERE question LIKE :m)"
                " + (SELECT count(*) FROM agent_orchestration_profile WHERE instructions LIKE :m)"
            ),
            {"m": f"%{MARKER}%"},
        )
    ).scalar_one()
    check("S10 自建行已清理", int(remaining) == 0, f"残留行数={remaining}")


async def main() -> int:
    print(f"DSN  = {_maskDsn(DATABASE_URL)}")
    print(f"MARKER = {MARKER}")
    print("-" * 72)

    sessionFactory = getSessionFactory()
    # 引擎在非生产环境默认 echo SQL；把它的 logger 压到 WARNING，
    # 让脚本输出只剩 PASS/FAIL 行（排查时用 LOG_LEVEL=INFO 恢复）。
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
    actor = CurrentUser(userId="realdata-script", roles=("admin",), departments=())

    beforeCounts: dict[str, int] = {}
    agentCode: str | None = None

    async with sessionFactory() as session:
        await step1_schema(session)
        beforeCounts = await _legacyCounts(session)
        passStep(
            "S2 既有表基线行数",
            ", ".join(f"{table}={count}" for table, count in beforeCounts.items()),
        )

        await step3_schemaValidation()
        await step4_disabledToolPath(session)
        await step5_fixSchemas(session)
        await step6_toolOptions(session)

        agentCode = await step7_profile(session, actor)
        if agentCode is not None:
            await step8_createRun(session, actor, agentCode)

    await step9_featureOff()

    async with sessionFactory() as session:
        await step10_cleanup(session, agentCode)
        afterCounts = await _legacyCounts(session)
        drift = {
            table: (beforeCounts[table], afterCounts[table])
            for table in LEGACY_TABLES
            if beforeCounts[table] != afterCounts[table]
        }
        check(
            "S2 既有表行数前后一致",
            not drift,
            "六张既有表行数无变化"
            if not drift
            else f"行数变化 {drift}",
        )

    print("-" * 72)
    if FAILURES:
        print(f"FAILED  {len(FAILURES)} step(s):")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("OK  all steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
