"""通用多工具 Agent 编排：真实数据验证脚本（feat-agent-orchestration）。

目的
----
在**真实 PostgreSQL** 上验证本特性的关键不变量，每一步输出一行
``PASS`` / ``FAIL`` / ``SKIP``，任一 ``FAIL`` 即以非零码退出。

模块划分（按职责拆，单文件均 < 800 行）
--------------------------------------
- ``feat_agent_orchestration_realdata_lib``：常量、DSN 解析、PASS/FAIL/SKIP 输出；
- ``feat_agent_orchestration_realdata_db``：数据库访问 helper（工具行适配器、
  快照 / 还原、前置条件自建行、清理）；
- 本文件（入口）：S1–S10 各步骤与主流程。

目标库（唯一被允许的破坏性写入目标）
------------------------------------
**隔离测试库** ``localhost:5436/qa_metadata_test``。
DSN 解析顺序见 ``feat_agent_orchestration_realdata_lib.resolveDatabaseUrl``：

1. ``AGENT_ORCHESTRATION_REALDATA_DATABASE_URL``
2. ``TEST_DATABASE_URL``（仓内标准测试变量）
3. ``DATABASE_URL``
4. ``PG*`` 变量拼装（默认 **5436 / qa_metadata_test**）

⚠️ 默认值刻意指向隔离库，裸跑**不会**落到 dev 库（5433）。
⚠️ 绝不指向生产。

前置条件（库种子）与 SKIP 语义
-------------------------------
本脚本**只**断言与本特性自身相关的行为。目标库的种子不满足前提时，
该步骤输出 ``SKIP``（**不是** ``FAIL``）并说明缺什么：

- ``S7`` 需要一个 ``status='active'`` 且尚无编排 profile 的
  ``agent_definition``：优先**借用**既有 Agent；一个都没有时脚本**自建**
  一个（退出时删除，且不计入 S2 基线行数）；自建也失败 ⇒ ``SKIP``。
- ``S8`` 需要一个 ``is_active`` 的 ``llm_config``：优先**借用**既有的；
  没有时脚本**自建**一个（退出时删除）；自建也失败 ⇒ ``SKIP``。

``SKIP`` 的语义：这一步在当前库上**无法执行**——它既不是「通过」也不是
「失败」。退出码只由 ``FAIL`` 决定。

覆盖的步骤
----------
1. 迁移版本 = ``0135``，五张新表都在。
2. 设计 §2.2 点名的**六张既有表**行数在脚本的写入路径前后完全一致
   （``agent_definition`` 的计数**排除脚本自建的那一行**）。
3. ``validateObjectSchema`` 真的排除 ``{}``（空 schema 非法）。
4. **禁用工具路径**（不写任何数据）：取 ``wiki_search`` 的**真实行**，
   复制一份并把副本的 ``enabled`` 置 ``False``，验证
   ``assertOrchestrationToolValid`` 走禁用分支 ⇒
   ``ValidationError("工具已禁用：wiki_search")``（API 边界即 422）。
   ⚠️ 输出会明说这是「副本」，不会让人误读成「真实行被禁用了」。
5. 阶段 0 的**只修 schema**动作：只改三个**已启用**工具
   （``supplier_360`` / ``supplier_risk`` / ``graph_traverse``）的
   ``input_schema``，**绝不翻 ``enabled``**。
   ⚠️ 理由：4 个 wiki 工具被 4 个既有 legacy Agent 绑定；既有运行时只加载
   ``enabled=true`` 的工具行，否则抛 ``ConflictError(409 tool_unbound_or_disabled)``
   ⇒ 翻转 ``enabled`` 会让那 4 个**既有** Agent 从「409 不可运行」变成
   「真的执行」，属于对现有功能的行为变更。改 ``input_schema`` 本身对既有
   链路是**惰性**的，所以本步骤不改变任何既有行为。
   ⚠️ 写入前的原值会被快照，退出前（步骤 10）**原样写回**。
6. 修复后三个工具可用；**四个 wiki 工具的 ``enabled`` 标志与脚本运行前
   完全一致**（脚本全程不写 ``enabled``）——这是「脚本没有翻转任何开关」
   的不变量，**在任何库上都成立**（输出里会报告观测到的运行前状态）。
7. 为一个 ACTIVE Agent 建编排 profile：创建后 ``enabled=false`` /
   ``revision=1``；toggle 之后 ``revision=2``。
8. 建一次 run：返回 ``QUEUED`` + ``runId``；快照里只有只读白名单里的工具名
   ⇒ 只读性成立（本脚本不执行任何工具）。
9. feature-off 路径：``ENABLE_AGENT_ORCHESTRATION=false`` 时新路由不出现在
   ``/openapi.json``。
10. **还原**：删除本脚本自建的 profile / run / Agent / 模型配置，并把步骤 5
    改过的 3 行 ``input_schema`` **写回原值**，然后断言还原生效。

自清理与幂等
------------
- 脚本**只写自己拥有的行**：profile 用 ``instructions`` 里的标记串
  （``MARKER``）识别，run 用 ``question`` 里的标记串识别；自建的
  ``agent_definition`` / ``llm_config`` 行用固定 code / model_name 识别。
- 步骤 5 的 schema 写入**会在退出前被还原**（见步骤 10）⇒ 脚本运行结束后
  库**回到运行脚本前的状态**：自建行零残留，被改写的 3 行
  ``input_schema`` 也回到原值。
  ⚠️ 「阶段 0 只补 schema 确实有效」这个结论由**步骤 5 自己的输出**给出，
  而不是由「脚本留下痕迹」给出。
- **幂等的口径**：连续两次运行 ⇒ **退出码相同** 且 **PASS/SKIP 集合相同**
  （``FAIL`` 集合为空）。第二次运行步骤 5 仍会写入 3 行（因为第一次结束时
  已把 schema 还原成原值），这是**预期**的。

用法
----
::

    # 推荐：显式指定隔离测试库（口令不要写进仓库）
    export TEST_DATABASE_URL='postgresql+asyncpg://<user>:<pwd>@localhost:5436/qa_metadata_test'
    cd backend && ./.venv/bin/python scripts/feat_agent_orchestration_realdata.py

    # 或用 AGENT_ORCHESTRATION_REALDATA_DATABASE_URL 显式覆盖
    cd backend && AGENT_ORCHESTRATION_REALDATA_DATABASE_URL='postgresql+asyncpg://…' \
        ./.venv/bin/python scripts/feat_agent_orchestration_realdata.py

    # 或用 PG* 变量拼装（PGPASSWORD 必填；默认 5436 / qa_metadata_test）
    PGPASSWORD=... PGHOST=localhost PGPORT=5436 PGUSER=qa_user PGDATABASE=qa_metadata_test \
        ./.venv/bin/python scripts/feat_agent_orchestration_realdata.py

⚠️ 绝不指向生产服务器。本脚本会写库（并在退出前还原）。
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

# 允许从 scripts/ 直接运行：把 backend/ 与 scripts/ 放进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from feat_agent_orchestration_realdata_lib import (  # noqa: E402
    DISABLED_WIKI_TOOLS,
    EXPECTED_ALEMBIC_VERSION,
    FAILURES,
    FIRST_WAVE_TOOLS,
    LEGACY_TABLES,
    MARKER,
    NEW_TABLES,
    TOOL_SCHEMAS,
    check,
    failStep,
    maskDsn,
    passStep,
    resolveDatabaseUrl,
    skipStep,
)

# ---------------------------------------------------------------------------
# 配置（必须在 import app.* 之前解析：app.config 读 DATABASE_URL）
# ---------------------------------------------------------------------------

DATABASE_URL, DSN_SOURCE = resolveDatabaseUrl()
os.environ["DATABASE_URL"] = DATABASE_URL
# 默认静默 SQL 回显（脚本输出只保留 PASS/FAIL/SKIP 行；需要排查时设 LOG_LEVEL=INFO）
os.environ.setdefault("LOG_LEVEL", "WARNING")

from feat_agent_orchestration_realdata_db import (  # noqa: E402
    ToolRow,
    deleteScriptRows,
    ensureAgent,
    ensureModelConfig,
    fetchToolRow,
    fetchToolRows,
    legacyCounts,
    restoreToolSchemas,
    snapshotEnabledFlags,
    snapshotToolSchemas,
    toolRow,
    writeToolSchema,
)
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.agent_orchestration_schemas import (  # noqa: E402
    AgentOrchestrationProfileCreate,
    AgentOrchestrationToggle,
    AgentRunCreate,
)
from app.domain.exceptions import ValidationError  # noqa: E402
from app.infrastructure.database import getEngine, getSessionFactory  # noqa: E402
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

# 引擎在非生产环境默认 echo SQL（info 级）；本脚本的输出只保留判定行。
# ⚠️ SQLAlchemy 的 echo 日志走 InstanceLogger，它直接调 ``logger._log()``
# **绕过 logger 级别** ⇒ 真正能压住它的只有「把引擎的 echo 标志关掉」
# （见 main() 里 getEngine().echo = False）。
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


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
    """禁用工具路径（**不写库**）。

    ``assertOrchestrationToolValid`` 是纯函数 ⇒ 取 ``wiki_search`` 的**真实行**，
    复制一份并把副本的 ``enabled`` 置 ``False``，验证禁用分支被拒。
    输出会明说是「副本」，避免被误读成「真实行被禁用了」。
    """
    row = await fetchToolRow(session, "wiki_search")
    if row is None:
        skipStep(
            "S4 禁用工具路径",
            "agent_tool_config 里没有 wiki_search 行（目标库未种该工具，无法取真实行）",
        )
        return

    live = toolRow(row)
    disabledCopy = ToolRow(
        name=live.name,
        enabled=False,
        handler_kind=live.handler_kind,
        handler_ref=live.handler_ref,
        input_schema=live.input_schema,
    )
    print(f"      wiki_search 真实行 enabled={live.enabled}（脚本全程不写 enabled）")
    try:
        assertOrchestrationToolValid(disabledCopy, "wiki_search")
    except ValidationError as error:
        expected = "工具已禁用：wiki_search"
        check(
            "S4 禁用工具被拒",
            str(error) == expected,
            f"message={error!s} (期望 {expected!r})；"
            "针对真实行的副本置 enabled=False 验证禁用分支（未写库）",
        )
    else:
        failStep("S4 禁用工具被拒", "assertOrchestrationToolValid 没有拒绝禁用工具")


async def step5_fixSchemas(session: AsyncSession, originalSchemas: dict[str, Any]) -> None:
    """阶段 0 的 schema-only 修复（不动 ``enabled``）。

    原始值由调用方在写入前快照 ⇒ 退出时（步骤 10）可原样写回。
    目标库没种这三个工具行 ⇒ **SKIP**（库种子前置条件未满足，不是脚本缺陷）。
    """
    missing = [
        name for name in FIRST_WAVE_TOOLS if await fetchToolRow(session, name) is None
    ]
    if missing:
        skipStep(
            "S5 schema-only 修复",
            f"目标库 agent_tool_config 缺少工具行 {missing}"
            "（库种子前置条件未满足；本步骤在当前库上无法执行）",
        )
        return

    changed = 0
    for name in FIRST_WAVE_TOOLS:
        row = await fetchToolRow(session, name)
        if row is None:
            failStep("S5 schema 修复", f"agent_tool_config 里没有 {name} 行")
            continue
        target = TOOL_SCHEMAS[name]
        before = row["input_schema"]
        if before == target:
            print(f"      {name}: schema 已是目标值，未写入（enabled={row['enabled']}）")
            continue
        await writeToolSchema(session, row["id"], target)
        changed += 1
        print(
            f"      {name}: input_schema {json.dumps(before, ensure_ascii=False)}"
            f" -> {json.dumps(target, ensure_ascii=False)}"
            f"（enabled 保持 {row['enabled']} 未改）"
        )
    await session.commit()
    passStep("S5 schema-only 修复", f"实际写入 {changed} 行（enabled 未翻转；退出前会还原）")


async def step6_toolOptions(session: AsyncSession, preEnabledFlags: dict[str, bool]) -> None:
    """修复后：3 个第一波工具可用；**4 个 wiki 工具的 enabled 未被翻转**。"""
    byName = await fetchToolRows(
        session, tuple(FIRST_WAVE_TOOLS) + tuple(DISABLED_WIKI_TOOLS)
    )

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

    missing = [name for name in DISABLED_WIKI_TOOLS if name not in byName]
    if missing:
        skipStep(
            "S6 四个 wiki 工具的 enabled 不变量",
            f"目标库缺少工具行 {missing}，无法比较运行前后的 enabled",
        )
    else:
        nowEnabled = {name: byName[name].enabled for name in DISABLED_WIKI_TOOLS}
        unchanged = all(
            preEnabledFlags.get(name) == nowEnabled[name] for name in DISABLED_WIKI_TOOLS
        )
        detail = "；".join(
            f"{name}: pre enabled={preEnabledFlags.get(name)} → now enabled={nowEnabled[name]}"
            for name in DISABLED_WIKI_TOOLS
        )
        check(
            "S6 脚本没有翻转任何 enabled",
            unchanged,
            f"{detail}（脚本全程不写 enabled；观测到的运行前状态如上，"
            "不同库种子不同——enabled=true 的库说明该库把 wiki 工具种成了启用态）",
        )

    check(
        "S6 只读白名单仍是 7 项",
        len(READ_ONLY_HANDLER_REFS) == 7,
        f"READ_ONLY_HANDLER_REFS={sorted(READ_ONLY_HANDLER_REFS)}",
    )


async def step7_profile(session: AsyncSession, actor: CurrentUser) -> str | None:
    """建 profile（enabled=false / revision=1）→ toggle（revision=2）。

    前置条件：需要一个 ACTIVE 且尚无编排 profile 的 Agent。优先**借用**既有的；
    库里一个都没有 ⇒ 脚本**自建**一个（退出时删除，不计入 S2 基线）。
    两条路都走不通 ⇒ **SKIP**（库种子前置条件未满足，不是脚本缺陷）。
    """
    await deleteScriptRows(session)
    try:
        agentCode, isSelfCreated = await ensureAgent(session)
    except Exception as error:  # noqa: BLE001 — 前置条件不足按 SKIP 处理，不算脚本缺陷
        skipStep(
            "S7 编排 profile",
            f"无法准备 ACTIVE 的 agent_definition（{type(error).__name__}: {error}）"
            "；本步骤在当前库上无法执行",
        )
        return None
    if agentCode is None:
        skipStep(
            "S7 编排 profile",
            "目标库没有 status='active' 且尚无编排 profile 的 agent_definition，"
            "自建也未成功（库种子前置条件未满足；本步骤在当前库上无法执行）",
        )
        return None
    origin = "脚本自建（退出时删除）" if isSelfCreated else "借用既有 Agent（不删除）"
    print(f"      使用 Agent：{agentCode}（{origin}）")

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
    try:
        modelId, isSelfCreated = await ensureModelConfig(session)
    except Exception as error:  # noqa: BLE001 — 前置条件不足按 SKIP 处理
        skipStep(
            "S8 创建 run",
            f"无法准备 is_active 的 llm_config（{type(error).__name__}: {error}）；"
            "本步骤在当前库上无法执行",
        )
        return
    if modelId is None:
        skipStep(
            "S8 创建 run",
            "llm_config 里没有 is_active 的模型配置，自建也未成功"
            "（库种子前置条件未满足；本步骤在当前库上无法执行）",
        )
        return
    origin = "脚本自建（退出时删除）" if isSelfCreated else "借用既有模型配置（不删除）"
    print(f"      使用模型配置：id={modelId}（{origin}）")

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


async def step10_cleanup(session: AsyncSession, originalSchemas: dict[str, Any]) -> None:
    """**自清理**：删自建行 + 还原阶段 0 的 schema 改动 ⇒ 库回到运行脚本前。"""
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
    await deleteScriptRows(session)
    remaining = (
        await session.execute(
            text(
                "SELECT (SELECT count(*) FROM agent_orchestration_run WHERE question LIKE :m)"
                " + (SELECT count(*) FROM agent_orchestration_profile WHERE instructions LIKE :m)"
                " + (SELECT count(*) FROM agent_definition "
                "   WHERE agent_code = 'REALDATA_ORCHESTRATION_AGENT')"
                " + (SELECT count(*) FROM llm_config "
                "   WHERE model_name = 'realdata-orchestration-model')"
            ),
            {"m": f"%{MARKER}%"},
        )
    ).scalar_one()
    check(
        "S10 自建行已清理",
        int(remaining) == 0,
        "自建 run / profile / Agent / 模型配置均已删除" if not remaining else f"残留行数={remaining}",
    )

    if not originalSchemas:
        skipStep("S10 阶段 0 schema 已还原", "脚本未改写过任何 input_schema，无需还原")
        return
    restored = await restoreToolSchemas(session, originalSchemas)
    notRestored = sorted(name for name, ok in restored.items() if not ok)
    check(
        "S10 阶段 0 schema 已还原",
        not notRestored,
        "3 行 input_schema 已写回运行前的原值"
        if not notRestored
        else f"未还原 {notRestored}",
    )


async def main() -> int:
    print(f"TARGET  {maskDsn(DATABASE_URL)}  (from {DSN_SOURCE})")
    print(f"MARKER = {MARKER}")
    print("-" * 72)

    sessionFactory = getSessionFactory()
    # 引擎在非生产环境默认 echo SQL（app.infrastructure.database.getEngine 用
    # ``echo=not settings.isProduction``）。SQLAlchemy 的 echo 日志走
    # ``InstanceLogger``，它直接调 ``logger._log()`` **绕过 logger 级别**，
    # 所以压 logger 级别压不住——必须把引擎自己的 ``echo`` 标志关掉
    # （关掉后 ``_should_log_info()`` 为 False，连接不再打 SQL）。
    # 这样脚本 stdout 只剩判定行，录进文档的证据才是完整可读的。
    getEngine().echo = False
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    actor = CurrentUser(userId="realdata-script", roles=("admin",), departments=())

    beforeCounts: dict[str, int] = {}
    originalSchemas: dict[str, Any] = {}

    try:
        async with sessionFactory() as session:
            await step1_schema(session)
            beforeCounts = await legacyCounts(session)
            passStep(
                "S2 既有表基线行数",
                ", ".join(f"{table}={count}" for table, count in beforeCounts.items()),
            )

            await step3_schemaValidation()
            await step4_disabledToolPath(session)
            originalSchemas = await snapshotToolSchemas(session)
            preEnabledFlags = await snapshotEnabledFlags(session)
            await step5_fixSchemas(session, originalSchemas)
            await step6_toolOptions(session, preEnabledFlags)

            agentCode = await step7_profile(session, actor)
            if agentCode is not None:
                await step8_createRun(session, actor, agentCode)
            else:
                skipStep("S8 创建 run", "S7 已 SKIP（目标库没有可用的 ACTIVE Agent）")

        await step9_featureOff()
    finally:
        async with sessionFactory() as session:
            await step10_cleanup(session, originalSchemas)
            if beforeCounts:
                afterCounts = await legacyCounts(session)
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
