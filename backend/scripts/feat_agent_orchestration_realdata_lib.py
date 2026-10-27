"""通用多工具 Agent 编排真实数据验证脚本的**共享常量、DSN 解析与输出 helper**。

脚本分三个模块（按职责拆，避免单文件超 800 行）：

- ``feat_agent_orchestration_realdata_lib``（本文件）：常量、DSN 解析、三种判定输出；
- ``feat_agent_orchestration_realdata_db``：数据库访问helper（工具行适配器、
  快照 / 还原、前置条件自建行、清理）；
- ``feat_agent_orchestration_realdata``（入口）：S1–S10 各步骤与主流程。

⚠️ 本模块**不 import app.\***：入口脚本必须在设置 ``DATABASE_URL`` 之后才能
import ``app.config``，所以 DSN 解析必须留在 app 导入之前。

目标库（唯一被允许的破坏性写入目标）
------------------------------------
**隔离测试库** ``localhost:5436/qa_metadata_test``。
DSN 解析顺序（先命中先用）：

1. ``AGENT_ORCHESTRATION_REALDATA_DATABASE_URL``（显式覆盖）
2. ``TEST_DATABASE_URL``（仓内标准测试变量）
3. ``DATABASE_URL``
4. ``PG*`` 变量拼装（默认端口 **5436** / 库 **qa_metadata_test**）

⚠️ 默认值刻意指向隔离库，裸跑**不会**落到 dev 库（5433）。
⚠️ 绝不指向生产。

前置条件（库种子）与 SKIP 语义
-------------------------------
本脚本**只**断言与本特性自身相关的行为。目标库的种子不满足前提时，
该步骤输出 ``SKIP``（**不是** ``FAIL``）并说明缺什么：

- ``S7`` 需要一个 ``status='active'`` 且尚无编排 profile 的
  ``agent_definition``；库里没有 ⇒ 脚本**自建**一个（退出时删除）；
  自建也失败 ⇒ ``SKIP``。
- ``S8`` 需要一个 ``is_active`` 的 ``llm_config``；库里没有 ⇒ 脚本**自建**
  一个（退出时删除）；自建也失败 ⇒ ``SKIP``。

``SKIP`` 的语义：该步骤在当前库上**无法执行**——它既不是「通过」也不是
「失败」。退出码只由 ``FAIL`` 决定。

自清理与幂等
------------
- 脚本**只写自己拥有的行**：profile 用 ``instructions`` 里的标记串
  （``MARKER``）识别，run 用 ``question`` 里的标记串识别；自建的
  ``agent_definition`` / ``llm_config`` 行用固定 code / model_name 识别。
- 阶段 0 的 schema 写入**会在退出前被还原** ⇒ 脚本运行结束后库**回到运行
  脚本前的状态**：自建行零残留，被改写的 3 行 ``input_schema`` 也回到原值。
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

import os
from typing import Any

# 脚本自建行的标记串（profile.instructions / run.question 里都含它）
MARKER = "feat-agent-orchestration-realdata"

# 脚本自建的既有表行（借用优先；实在没有才自建，退出时删除）
SCRIPT_AGENT_CODE = "REALDATA_ORCHESTRATION_AGENT"
SCRIPT_MODEL_NAME = "realdata-orchestration-model"

# 脚本在审计 outbox 里的 actor 标识（用于清理自留的审计行）
SCRIPT_ACTOR = "realdata-script"

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

# 阶段 0 第一波：schema-only 修复目标（这三个工具本来就 enabled=true）
FIRST_WAVE_TOOLS = ("supplier_360", "supplier_risk", "graph_traverse")

# 这四个工具在既有库里通常是 enabled=false（本脚本**绝不碰**它们的 enabled）
DISABLED_WIKI_TOOLS = ("wiki_search", "wiki_read", "rule_evaluate", "coverage_status")

# 灰度手册 §2.3 的权威 schema（脚本与手册必须同源，不能各写一份）。
# 前三个是第一波要写的；后四个是**后续波次**（翻 enabled）才要写的，
# 本脚本第一波不写它们，但保留权威值供后续波次复用。
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

# DSN 解析顺序（先命中先用）。TEST_DATABASE_URL 是仓内标准测试变量，
# 也是本脚本**唯一被允许**的破坏性写入目标。
DSN_ENV_ORDER = (
    "AGENT_ORCHESTRATION_REALDATA_DATABASE_URL",
    "TEST_DATABASE_URL",
    "DATABASE_URL",
)

# 收集失败项；``SKIP`` **不进**这个列表（库种子不满足不是脚本的缺陷）
FAILURES: list[str] = []


def maskDsn(dsn: str) -> str:
    """口令一律打码（``user:***@host``）。"""
    if "@" not in dsn or "://" not in dsn:
        return "***"
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"


def resolveDatabaseUrl() -> tuple[str, str]:
    """解析目标 DSN，返回 ``(dsn, 来源说明)``。

    顺序（先命中先用）：
    1. ``AGENT_ORCHESTRATION_REALDATA_DATABASE_URL``（显式覆盖）
    2. ``TEST_DATABASE_URL``（仓内标准测试变量）
    3. ``DATABASE_URL``
    4. ``PG*`` 变量拼装（默认端口 **5436** / 库 **qa_metadata_test**）

    ⚠️ 默认值刻意指向**隔离测试库**，绝不让裸跑落到 dev 库（5433）。
    """
    for name in DSN_ENV_ORDER:
        value = os.environ.get(name)
        if value:
            return value, name

    password = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise SystemExit(
            "缺少数据库口令：请设置 AGENT_ORCHESTRATION_REALDATA_DATABASE_URL "
            "（或 TEST_DATABASE_URL / DATABASE_URL / PGPASSWORD）。脚本绝不内置任何口令。"
        )
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5436")
    user = os.environ.get("PGUSER", "qa_user")
    database = os.environ.get("PGDATABASE", "qa_metadata_test")
    dsn = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"
    return dsn, "PG* 变量拼装"


def passStep(step: str, detail: str = "") -> None:
    """断言成立。"""
    print(f"PASS  {step}" + (f" — {detail}" if detail else ""))


def failStep(step: str, detail: str) -> None:
    """断言不成立——记入失败清单并以非零码退出。"""
    FAILURES.append(f"{step}: {detail}")
    print(f"FAIL  {step} — {detail}")


def skipStep(step: str, reason: str) -> None:
    """**前置条件不满足**——只打印，不记失败。

    语义：目标库的种子不满足该步骤的前提（例如没有 ACTIVE 的 Agent）。
    ``SKIP`` **不等于**该断言通过，也**不等于**该断言失败——它表示
    「本步骤在当前库上无法执行」。仓内失败项数与退出码只由 ``FAIL`` 决定。
    """
    print(f"SKIP  {step} — {reason}")


def check(step: str, ok: bool, detail: str = "") -> bool:
    """断言并打印 ``PASS`` / ``FAIL``。"""
    if ok:
        passStep(step, detail)
    else:
        failStep(step, detail or "断言不成立")
    return ok
