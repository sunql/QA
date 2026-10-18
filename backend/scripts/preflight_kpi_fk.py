"""KPI–数据源 FK 上线 preflight（只读，F2 §4.5 部署门禁）。

在部署含 0132 迁移的 backend 之前/之后运行，确认库已进入 RESTRICT 语义：

1. ``alembic_version`` >= ``0132``（只认数字版本号；hash revision 不参与比较）
2. ``fk_kpi_catalog_datasource`` 存在且 ``confdeltype='r'``（ON DELETE RESTRICT）
3. ``kpi_catalog.datasource_id IS NOT NULL`` 行数（信息项：非 0 只 WARN，不 FAIL）

用法::

    uv run python scripts/preflight_kpi_fk.py

退出码：三项全 PASS → 0；任一 FAIL → 1。第二项是部署顺序的硬门禁 ——
backend 启动时 CMD 会自动 ``alembic upgrade head``，本脚本用于在重启前后
确认迁移真的落到了 RESTRICT，而不是停在旧镜像的 SET NULL。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.infrastructure.database import getSessionFactory

_TARGET_REVISION = "0132"
_CONSTRAINT = "fk_kpi_catalog_datasource"
_RESTRICT = "r"


def _revisionAtLeast(revision: str, target: str) -> bool:
    """版本号是否达到 target。

    只认 4 位零填充的数字版本号：链上还存在 hash 命名的 revision
    （如 ``b1ea6cb32588``），字符串比较 ``"b..." >= "0132"`` 会误判为 True。
    hash 版本或空版本一律视为未达标（这类库本就不在 0132 之后）。
    """
    return revision.isdigit() and revision >= target


@dataclass(frozen=True)
class CheckResult:
    """单项检查结果（frozen，不可变）。"""

    name: str
    passed: bool
    detail: str
    isWarning: bool = False


async def runChecks(session: Any) -> list[CheckResult]:
    """执行三项检查并返回结果（只读，不写库）。"""
    results: list[CheckResult] = []

    version = (
        await session.execute(text("SELECT version_num FROM alembic_version"))
    ).scalars().all()
    current = ",".join(sorted(version)) if version else "<empty>"
    # 多 head 时每一行都必须达标；任一 hash/旧版本 → FAIL
    results.append(
        CheckResult(
            name="alembic_version",
            passed=bool(version)
            and all(_revisionAtLeast(v, _TARGET_REVISION) for v in version),
            detail=f"current={current} required>={_TARGET_REVISION}",
        )
    )

    constraint = (
        await session.execute(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conname = :n AND conrelid = 'kpi_catalog'::regclass"
            ),
            {"n": _CONSTRAINT},
        )
    ).first()
    confdeltype = constraint[0] if constraint else None
    # pg_constraint.confdeltype 是 "char"；asyncpg 返回 bytes（b'r'）
    if isinstance(confdeltype, (bytes, bytearray)):
        confdeltype = confdeltype.decode()
    results.append(
        CheckResult(
            name="fk_on_delete",
            passed=confdeltype == _RESTRICT,
            detail=(
                f"{_CONSTRAINT} confdeltype={confdeltype!r} "
                f"required={_RESTRICT!r} (r=RESTRICT)"
            ),
        )
    )

    bound = (
        await session.execute(
            text("SELECT count(*) FROM kpi_catalog WHERE datasource_id IS NOT NULL")
        )
    ).scalar_one()
    results.append(
        CheckResult(
            name="bound_kpi_rows",
            passed=True,
            detail=f"{bound} 个 KPI 已绑定数据源（信息项）",
            isWarning=bound > 0,
        )
    )

    return results


async def main() -> int:
    factory = getSessionFactory()
    async with factory() as session:
        results = await runChecks(session)

    failed = False
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")
        if result.isWarning:
            print(f"  WARN: {result.name} 非 0，迁移会校验既有引用（不影响成败）")
        if not result.passed:
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
