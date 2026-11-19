"""把采购术语目录（M1-1.5）落进 term_dictionary。

为什么走 service 写入口而不是裸 INSERT
------------------------------------
``TermDictionaryService.createTerm`` 带**映射存活校验**（类必须 ``valid_to IS NULL``、
属性必须命中 ``property_name`` / ``source_column``）与重名守卫 —— 绕过它写入就等于
绕过校验。写入口同时被集成测试覆盖（POST /api/v1/term-dictionary），本脚本与
测试消费**同一份目录**（``app/domain/procurement_term_catalog.py``）⇒ 不会手抄漂移。

**为什么不做 update / 覆盖写**
------------------------
term_dictionary **没有 update 端点**（只有 GET/POST/DELETE）。已存在的词条如果与
目录不一致 ⇒ 这是**漂移信号**（有人在后台改了口径，或目录改了没落库）⇒
脚本**报告差异并 exit 2**，让人先裁决，而不是静默覆盖（覆盖 = 丢掉人工改动）。

幂等语义：不存在的词条创建；字段完全一致的跳过；不一致的报差异。

用法::

    cd backend
    uv run python scripts/apply_procurement_terms.py            # dry-run（默认）
    uv run python scripts/apply_procurement_terms.py --apply    # 实际写入

退出码：0 全部一致或已创建 / 2 存在漂移（dry-run 同样报 2，便于 CI 拦截）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
if env.exists():  # 宿主机布局；容器内 SECRET_KEY 已由 compose 注入环境
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

sys.path.insert(0, os.getcwd())

from sqlalchemy import select  # noqa: E402

from app.domain.models import TermDictionary  # noqa: E402
from app.domain.procurement_term_catalog import (  # noqa: E402
    PROCUREMENT_TERMS,
    ProcurementTerm,
)
from app.domain.schemas import TermDictionaryCreate  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.term_dictionary_service import TermDictionaryService  # noqa: E402

SCRIPT_USER = "ops-procurement-terms"
EXIT_OK = 0
EXIT_DRIFT = 2


@dataclass(frozen=True, slots=True)
class ApplySummary:
    """一轮应用的逐词条处置结果（不可变，测试直接断言）。"""

    created: tuple[str, ...]
    skipped: tuple[str, ...]
    drifted: tuple[str, ...]


# 比对字段唯一清单：(库列名, 目录字段取值)。match 与 diff **都从这里推导**
# —— 两份手维护的字段列表会在目录加字段时各自漂移（评审 LOW-1）。
def _fieldPairs(spec: ProcurementTerm, row: TermDictionary) -> tuple[tuple[str, object], ...]:
    return (
        ("definition", spec.definition),
        ("mapped_class_name", spec.mappedClassName),
        ("mapped_property_name", spec.mappedPropertyName),
        ("formula_hint", spec.formulaHint),
    )


def _diffFields(spec: ProcurementTerm, row: TermDictionary) -> list[str]:
    """列出与库内行不一致的字段名（读侧口径：snake_case 列名）。"""
    current = {
        "definition": row.definition,
        "mapped_class_name": row.mapped_class_name,
        "mapped_property_name": row.mapped_property_name,
        "formula_hint": row.formula_hint,
    }
    return [name for name, specValue in _fieldPairs(spec, row) if current[name] != specValue]


async def applyTerms(session, service: TermDictionaryService, *, apply: bool) -> ApplySummary:
    """逐词条比对并（可选）创建；返回摘要。``apply=False`` 时只读比对。"""
    created: list[str] = []
    skipped: list[str] = []
    drifted: list[str] = []
    # dry-run（apply=False）时不写库，但待创建词条仍进 ``created``；
    # 输出侧把标签换成 would-create (dry-run)，避免日志误读成已写入（评审 LOW-2）。

    existingRows = (await session.execute(select(TermDictionary))).scalars().all()
    byTerm = {row.term: row for row in existingRows}

    for spec in PROCUREMENT_TERMS:
        row = byTerm.get(spec.term)
        if row is None:
            if apply:
                await service.createTerm(
                    session,
                    TermDictionaryCreate(
                        term=spec.term,
                        definition=spec.definition,
                        mapped_class_name=spec.mappedClassName,
                        mapped_property_name=spec.mappedPropertyName,
                        formula_hint=spec.formulaHint,
                    ),
                    createdBy=SCRIPT_USER,
                )
            created.append(spec.term)
        elif not _diffFields(spec, row):
            skipped.append(spec.term)
        else:
            # 有 update 端点之前，漂移必须人工裁决 —— 不静默覆盖。
            diffs = ", ".join(_diffFields(spec, row))
            print(
                f"[DRIFT] 「{spec.term}」与目录不一致（字段：{diffs}）；目录 evidence：{spec.evidence}",
                file=sys.stderr,
            )
            drifted.append(spec.term)

    return ApplySummary(created=tuple(created), skipped=tuple(skipped), drifted=tuple(drifted))


def _printSummary(summary: ApplySummary, *, apply: bool) -> None:
    createdLabel = "created" if apply else "would-create (dry-run)"
    print(f"{createdLabel} ({len(summary.created)}): {list(summary.created)}")
    print(f"skipped ({len(summary.skipped)}): {list(summary.skipped)}")
    print(f"drifted ({len(summary.drifted)}): {list(summary.drifted)}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="采购术语目录落库（幂等；默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际写入（缺省只比对，不写库）")
    args = parser.parse_args()

    factory = getSessionFactory()
    service = TermDictionaryService()
    async with factory() as session:
        summary = await applyTerms(session, service, apply=args.apply)

    _printSummary(summary, apply=args.apply)
    if summary.drifted:
        print(
            f"存在 {len(summary.drifted)} 条漂移词条，人工裁决后再同步（未做任何覆盖）。",
            file=sys.stderr,
        )
        return EXIT_DRIFT
    if not args.apply and summary.created:
        print("dry-run：以上词条待创建；加 --apply 落库。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
