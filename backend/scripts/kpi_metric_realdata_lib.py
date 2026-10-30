"""KPI 指标技术形态治理：真实数据验证脚本的**共享常量、DSN 解析与输出 helper**。

脚本分六个模块（按职责拆，单文件均 < 800 行）：

- ``kpi_metric_realdata_lib``（本文件）：常量、DSN 解析、三种判定输出、结尾汇总；
- ``kpi_metric_realdata_db``：数据库只读 helper + 开关行的读写与还原；
- ``kpi_metric_realdata_s3``：S3 的判定 oracle（列差异 / 分母判定 / formula 覆盖）；
- ``kpi_metric_realdata_s4``：S4 发布闸门（开关 ON/OFF 的可还原写）；
- ``kpi_metric_realdata_inject``：S5 契约注入端到端（含审核模拟）；
- ``kpi_metric_realdata``（入口）：S1–S9 各步骤与主流程。

⚠️ 本模块**必须保持零 app 依赖**：入口在设置 ``DATABASE_URL`` **之前**就 import 它，
此时 import ``app.*`` 会让 ``app.config`` 读到默认 DSN（``localhost:5432``，那是
OpenMetadata 的库）。``_s3`` / ``_s4`` 允许 import app，但只能被入口在
``DATABASE_URL`` 设置**之后**导入。

目标库
------
正式库 ``qa_metadata``（**prod 同源**；真实的 12 条 KPI 与 ``schema_cache`` 在那里）
或隔离测试库 ``qa_metadata_test*``。守卫见 :mod:`scripts.kpi_metric_db_guard`。

⚠️ **本脚本不猜环境变量顺序**（T16-03）：``TEST_DATABASE_URL`` / ``DATABASE_URL`` /
``PG*`` **一律不参与**解析。本仓 dev 工作流普遍导出 ``TEST_DATABASE_URL``，
若作为回退，S1–S9 会打到 ``qa_metadata_test``（那里没有那 12 条 KPI）
⇒ 验证对象被偷换。

PASS / FAIL / SKIP 语义
-----------------------
- ``PASS`` —— 断言成立；
- ``FAIL`` —— 断言不成立（脚本或实现的缺陷，必须暴露）⇒ 影响退出码；
- ``SKIP`` —— **前置条件不满足**（库种子不齐），该步骤在当前库上**无法执行**：
  它既不是「通过」也不是「失败」⇒ 影响退出码（不能被读成绿）。

三态退出码：``0`` = 全 PASS / ``1`` = 有 FAIL / ``2`` = 无 FAIL 但有 SKIP。

⚠️ **有跳过步骤时不说「all steps passed」**：结尾汇总会打印 ``NOTE  M step(s) skipped``
并逐项列出被跳过的步骤与原因。

机器可读结论行
--------------
汇总固定打印一行 ``RESULT: PASS|SKIP|FAIL steps=9 passed=… failed=… skipped=…``。
**缺这一行 ⇒ stdout 被截断 ⇒ 验收不通过**（T16-15）。

写库边界
--------
脚本**默认只读**。唯一的写操作是 ``system_config`` 里的灰度开关行，退出前
**原样还原**（原本不存在 ⇒ 删除）。回填落库要显式加 ``--apply-backfill``
（对应 spec §13「产出需业务方审核后才落库」）。
"""

from __future__ import annotations

import os

from scripts.kpi_metric_db_guard import assertSafeTarget, maskDsn

# 脚本在审计 outbox / 业务数据里的 actor 标识
SCRIPT_ACTOR = "kpi-metric-realdata"

# 本计划引入的迁移 head（Task 2 的 0136 + Task 3 的 0137）
EXPECTED_ALEMBIC_VERSION = "0137"

# 契约注入用的问句：**必须含 KPI code**（精确 alias 路径）。
# 自然语言问句走 Jaccard 路径，实测最高分 0.09 < 0.75 阈值 ⇒ 不可达（遗留项 L1）。
CONTRACT_QUESTION = "查一下 KPI_SUPPLIER_OTD 的口径"

# 本脚本计划执行的步骤总数（S1–S9）。
# ⚠️ 这是**计划值**，不是实测值 —— 它让「stdout 被截断」可判定（T16-15）：
# 汇总行必须出现且 ``steps=9``；缺行 ⇒ 视同截断，验收不通过。
MAX_STEPS = 9

# 目标 DSN 的**唯一**环境变量来源。
# ⚠️ **不猜环境变量顺序**（T16-03）：`TEST_DATABASE_URL` / `DATABASE_URL` / `PG*`
# 一律**不参与**解析 —— 本仓 dev 工作流普遍导出 `TEST_DATABASE_URL`，若参与回退，
# S1–S9 会打到 `qa_metadata_test`（那里没有那 12 条 KPI）⇒ 验证对象被偷换。
DSN_ENV_NAME = "KPI_METRIC_REALDATA_DATABASE_URL"

# 收集失败项；``SKIP`` **不进**这个列表（库种子不满足不是脚本的缺陷）
FAILURES: list[str] = []

# 收集被跳过的步骤（``(步骤, 原因)``）；汇总必须如实列出。
SKIPS: list[tuple[str, str]] = []

# 层 2 的三档 verdict 字面量（与 ``app.services.kpi_metric_reconcile`` 一致）。
# ⚠️ 本模块必须**零 app 依赖**：入口在设置 ``DATABASE_URL`` 之前就 import 它
# （否则 ``app.config`` 会读到 5432 的默认 DSN）。字面量一致性由单测钉住。
VERDICT_PASS = "pass"
VERDICT_MISMATCH = "mismatch"
VERDICT_UNKNOWN = "unknown"


def expectedVerdictFor(tier: str | None, deviations: object) -> str:
    """PLAN 档位 + 已登记偏差 ⇒ 期望的层 2 verdict。

    ``deviations`` 传 PLAN 的 ``deviations``（空序列 / ``None`` = 无偏差）。

    为什么抽成纯函数：真实 12 条 PLAN **全部**有 deviations ⇒ 「无偏差」的
    两条分支（tier A ⇒ pass / tier B/C ⇒ unknown）在实跑里**永不执行**。
    留作未测代码，等哪天 PLAN 清空 deviations 时会静默生效 —— 由单测直接
    覆盖这两条路径（FIX-2）。
    """
    if deviations:
        return VERDICT_MISMATCH
    return VERDICT_PASS if tier == "A" else VERDICT_UNKNOWN


def resolveDatabaseUrl(explicit: str | None = None) -> tuple[str, str]:
    """解析目标 DSN，返回 ``(dsn, 来源说明)``。

    **只接受两个来源**，都拿不到 ⇒ ``SystemExit``：

    1. 显式参数 ``explicit``（命令行 ``--database-url``）
    2. 专用环境变量 ``KPI_METRIC_REALDATA_DATABASE_URL``

    ⚠️ **不猜环境变量顺序**（T16-03）：``TEST_DATABASE_URL`` / ``DATABASE_URL`` /
    ``PG*`` **一律不参与**。守卫只负责「这个名字的库能不能写」，不负责「猜哪个库」。
    """
    if explicit:
        return explicit, "--database-url"
    value = os.environ.get(DSN_ENV_NAME)
    if value:
        return value, DSN_ENV_NAME
    raise SystemExit(
        "缺少数据库连接：请用 --database-url 或设置 KPI_METRIC_REALDATA_DATABASE_URL。"
        "本脚本不猜环境变量顺序（不读 TEST_DATABASE_URL / DATABASE_URL / PG*）。"
    )


def guardTarget(dsn: str, *, allowProdWrite: bool = False) -> str:
    """目标库守卫（薄封装，便于脚本入口统一调用）。

    ⚠️ 目标是 ``qa_metadata``（正式库 / prod 同源）时**必须**显式传
    ``allowProdWrite=True``，否则 ``SystemExit``（T16-01 第 3 闸）。
    """
    return assertSafeTarget(dsn, allowProdWrite=allowProdWrite)


def info(message: str) -> None:
    """打印**观测值**（不是判定）。观测值不参与退出码。"""
    print(f"INFO  {message}")


def passStep(step: str, detail: str = "") -> None:
    """断言成立。"""
    print(f"PASS  {step}" + (f" — {detail}" if detail else ""))


def failStep(step: str, detail: str) -> None:
    """断言不成立——记入失败清单。"""
    FAILURES.append(f"{step}: {detail}")
    print(f"FAIL  {step} — {detail}")


def skipStep(step: str, reason: str) -> None:
    """**前置条件不满足**——只打印，不记失败，但**影响退出码**（三态）。"""
    SKIPS.append((step, reason))
    print(f"SKIP  {step} — {reason}")


def lastSkipReason(stepPrefix: str) -> str | None:
    """最近一次以 ``stepPrefix`` 开头的 ``SKIP`` 原因（没有则 ``None``）。"""
    for name, reason in reversed(SKIPS):
        if name.startswith(stepPrefix):
            return reason
    return None


def _stepIds(labels: list[str]) -> set[str]:
    """从断言标签里抽出**步骤号**集合（``S1.1 ...`` ⇒ ``S1``）。

    汇总行按**步骤**（S1–S9）计数，不按断言计数 —— 一个步骤里有多条
    ``check``（如 S2 的 S2.1/S2.2/S2.3）时，不能把它们算成多个步骤。
    """
    ids: set[str] = set()
    for label in labels:
        head = label.split(" ", 1)[0]
        digits = head[1:].split(".")[0]
        if head.startswith("S") and digits.isdigit():
            ids.add("S" + digits)
    return ids


def printSummary() -> int:
    """结尾汇总：**三态**退出码（T16-13）。

    - ``0`` = 全 PASS
    - ``1`` = 有 FAIL
    - ``2`` = 无 FAIL 但有 SKIP（部分未验证 ⇒ **不算通过**）

    ⚠️ 「全跳过」必须落在 ``2`` 上 —— 若落回 ``0``，一次「9 步全 SKIP」的运行
    在任何闸门里都会被读成「验证通过」。
    """
    print("-" * 72)
    if FAILURES:
        print(f"FAILED  {len(FAILURES)} check(s) failed:")
        for item in FAILURES:
            print(f"  - {item}")
    if SKIPS:
        print(
            f"NOTE  {len(SKIPS)} step(s) skipped — 见上方 SKIP 行"
            "（SKIP ≠ 通过，也不算失败）"
        )
        for step, reason in SKIPS:
            print(f"  - {step}: {reason}")

    failedSteps = _stepIds(FAILURES)
    if FAILURES and not failedSteps:
        # 无步骤前缀的失败（如「MAIN 执行异常」＝建连就崩）也必须计入失败步骤数，
        # 否则汇总会打印 ``failed=0``、``passed=9`` —— 读起来像「9 步全过」。
        failedSteps = {"MAIN"}
    skippedSteps = _stepIds([name for name, _ in SKIPS])
    passedSteps = max(MAX_STEPS - len(failedSteps) - len(skippedSteps), 0)
    if FAILURES:
        verdict = "FAIL"
    elif SKIPS:
        verdict = "SKIP"
    else:
        verdict = "PASS"
    print(
        f"RESULT: {verdict} steps={MAX_STEPS} "
        f"passed={passedSteps} failed={len(failedSteps)} skipped={len(skippedSteps)}"
    )

    if FAILURES:
        return 1
    if SKIPS:
        return 2
    print("OK  all steps passed")
    return 0


def check(step: str, ok: bool, detail: str = "") -> bool:
    """断言并打印 ``PASS`` / ``FAIL``。"""
    if ok:
        passStep(step, detail)
    else:
        failStep(step, detail or "断言不成立")
    return ok


__all__ = [
    "CONTRACT_QUESTION",
    "DSN_ENV_NAME",
    "EXPECTED_ALEMBIC_VERSION",
    "FAILURES",
    "MAX_STEPS",
    "SKIPS",
    "SCRIPT_ACTOR",
    "VERDICT_MISMATCH",
    "VERDICT_PASS",
    "VERDICT_UNKNOWN",
    "assertSafeTarget",
    "check",
    "expectedVerdictFor",
    "failStep",
    "guardTarget",
    "info",
    "lastSkipReason",
    "maskDsn",
    "passStep",
    "printSummary",
    "resolveDatabaseUrl",
    "skipStep",
]
