"""S5：契约块注入端到端（含「审核模拟」）。

**为什么单列一个模块**：S5 的判据不是「块非空」这么简单 —— 真实目录里
12/12 的 KPI 在回填后都是 ``mismatch``（PLAN 刻意保留业务治理列、技术侧写
物理列，见 ``KpiBackfillPlan.deviations`` 与 T15-L）。因此：

- 全部 ``mismatch`` ⇒ 契约块**必须为空**（对账拦截生效）；
- 只要有一条非 ``mismatch`` ⇒ 契约块**必须非空**（否则注入链路坏了）。

「块非空」这一条只能在**模拟业务方审核完成**后验证：把目标 KPI 的治理列
临时对齐到物理列（仅隔离测试库），重跑一遍，退出前**原样还原**。
正式库上这一项 ``SKIP`` —— 审查模拟要写库，不在正式库做。
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_contract_builder import buildKpiContractBlock
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_reconcile import (
    VERDICT_MISMATCH,
    VERDICT_PASS,
    reconcileMetric,
)
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf
from app.services.nl2sql_dialects import resolveDialect
from app.services.nl2sql_prompts import _buildPlanSystemPrompt
from scripts.kpi_metric_realdata_db import (
    countLinkedKpis,
    currentDatabaseName,
    fetchKpis,
)
from scripts.kpi_metric_realdata_lib import (
    CONTRACT_QUESTION,
    check,
    info,
    skipStep,
)

TARGET_KPI = "KPI_SUPPLIER_OTD"

# 正式库名（与 dev 同名）—— 审查模拟要写库，正式库一律不做
PROD_DB_NAME = "qa_metadata"

# 审查模拟用的对齐值：与 PLAN 的**技术侧**一致（OTD：物理列 + 物理粒度）
ALIGNED_GOVERNANCE: dict[str, object] = {
    "numerator_columns": ["RECEIPT_DATE"],
    "denominator_columns": ["PROMISED_RECEIPT_DATE"],
    "grain_keys": ["SUPPLIER_CODE", "FACILITY_CODE"],
}

_GOVERNANCE_FIELDS = ("numerator_columns", "denominator_columns", "grain_keys")


async def _linkedVerdicts(session: AsyncSession) -> dict[str, str]:
    """每个已挂靠 KPI 的层 2 判定（``kpi_code → verdict``）。"""
    verdicts: dict[str, str] = {}
    for kpi in await fetchKpis(session):
        if kpi.metric_id is None:
            continue
        metric = await session.get(OntologyMetric, kpi.metric_id)
        result = reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
        verdicts[kpi.kpi_code] = result.verdict
    return verdicts


def _checkBlockText(block: str) -> None:
    """契约块文本的四个硬断言（S5.2–S5.5）。"""
    check("S5.2 契约块含 KPI code", TARGET_KPI in block, "")
    check("S5.3 契约块含标题", "【KPI 口径契约】" in block, "")
    check(
        "S5.4 允许形态 SUM(分子)/SUM(分母)",
        "SUM(分子)/SUM(分母)" in block,
        "",
    )
    check(
        "S5.5 禁止形态 AVG(分子/分母)",
        "AVG(分子/分母)" in block,
        "",
    )


def _checkPrompt(block: str) -> None:
    """反假绿：断言 prompt **文本**里真的出现契约内容（S5.6–S5.8）。"""
    prompt = _buildPlanSystemPrompt(
        "CREATE TABLE T (QTY INT)",
        resolveDialect("mysql", None),
        "",
        kpiContractBlock=block,
    )
    check(
        "S5.6 prompt 里真的含契约标题",
        "【KPI 口径契约】" in prompt,
        f"prompt {len(prompt)} 字符",
    )
    check(
        "S5.7 prompt 里真的含比率形态指引",
        "SUM(分子)/SUM(分母)" in prompt,
        "",
    )
    check(
        "S5.8 prompt 里真的含 KPI code",
        TARGET_KPI in prompt,
        "",
    )


async def _writeGovernance(
    session: AsyncSession, kpi: KpiCatalog, values: dict[str, object]
) -> None:
    """参数化 UPDATE 三个治理列 + 提交 + **显式 refresh**（调用方负责还原）。

    ⚠️ 不能用 ``session.expire_all()``：过期对象的下一次属性访问（连 ``kpi.id``）
    会在异步会话外触发惰性加载 ⇒ ``MissingGreenlet``。``refresh`` 是显式 await。
    """
    await session.execute(
        update(KpiCatalog).where(KpiCatalog.id == kpi.id).values(**values)
    )
    await session.commit()
    await session.refresh(kpi)


async def _reviewSimulation(session: AsyncSession) -> None:
    """模拟业务方审核完成（治理列对齐物理列）⇒ 契约块必须出现。

    ⚠️ 只写**隔离测试库**；正式库直接 ``SKIP``（本脚本不在正式库做非开关写）。
    """
    if await currentDatabaseName(session) == PROD_DB_NAME:
        skipStep(
            "S5.9 审核模拟（治理列对齐后契约块出现）",
            "正式库不做治理列写入 —— 审核模拟只在隔离测试库执行",
        )
        return

    target = next(
        (k for k in await fetchKpis(session) if k.kpi_code == TARGET_KPI), None
    )
    if target is None or target.metric_id is None:
        skipStep(
            "S5.9 审核模拟（治理列对齐后契约块出现）",
            f"库里没有已挂靠的 {TARGET_KPI}",
        )
        return

    # 先把原值**拷贝**成普通值（还原时不依赖 ORM 状态）
    original = {field: getattr(target, field) for field in _GOVERNANCE_FIELDS}
    try:
        await _writeGovernance(session, target, ALIGNED_GOVERNANCE)
        metric = await session.get(OntologyMetric, target.metric_id)
        after = reconcileMetric(governanceSpecOf(target), techniqueOf(metric))
        check(
            f"S5.9 审核模拟：{TARGET_KPI} 治理列对齐后 verdict = pass",
            after.verdict == VERDICT_PASS,
            f"实际 {after.verdict}；findings={after.reasons()}",
        )
        block = await buildKpiContractBlock(session, CONTRACT_QUESTION)
        check(
            "S5.10 审核模拟：对齐后契约块非空",
            bool(block.strip()),
            f"{len(block)} 字符",
        )
        _checkBlockText(block)
        _checkPrompt(block)
    finally:
        await _writeGovernance(session, target, original)
        unchanged = all(
            getattr(target, field) == original[field] for field in _GOVERNANCE_FIELDS
        )
        check("S5.11 审核模拟的治理列已还原", unchanged, "")


async def step5_contractInjection(session: AsyncSession) -> None:
    """S5 主步骤：先判拦截语义，再做审核模拟。"""
    cache = get_kpi_match_cache()
    await cache.ensureFresh(session)

    linked = await countLinkedKpis(session)
    if linked == 0:
        skipStep(
            "S5 契约块注入端到端",
            "库内 KPI 均未挂靠 ontology_metric（回填尚未落库）—— "
            "契约注入的输入不存在，无法验证",
        )
        return

    verdicts = await _linkedVerdicts(session)
    allMismatch = bool(verdicts) and all(
        verdict == VERDICT_MISMATCH for verdict in verdicts.values()
    )
    block = await buildKpiContractBlock(session, CONTRACT_QUESTION)

    if allMismatch:
        check(
            f"S5.1 {len(verdicts)} 条全部 mismatch ⇒ 契约块必须为空（对账拦截生效）",
            not block.strip(),
            f"{len(block)} 字符",
        )
        info(
            "全部 mismatch 的来源 = PLAN 的**刻意**治理↔物理偏差（T15-L）；"
            "业务方审核并对齐治理列之前，本特性只观察、不注入"
        )
    else:
        check(
            "S5.1 存在非 mismatch 的已挂靠 KPI ⇒ 契约块必须非空",
            bool(block.strip()),
            f"{len(block)} 字符",
        )

    if block.strip():
        _checkBlockText(block)
        _checkPrompt(block)

    await _reviewSimulation(session)


__all__ = ["ALIGNED_GOVERNANCE", "TARGET_KPI", "step5_contractInjection"]
