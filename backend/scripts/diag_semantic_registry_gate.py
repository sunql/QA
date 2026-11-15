"""D15 运行时探针：开关打开时，schema 文本渲染路径是否真的走「语义快照」，
且**完整性守卫没有触发降级**。

为什么需要独立探针（而不是只看单测）：
单测用替身注入快照，证的是「闸门逻辑对」；本探针连**真实 dev 库** +
**真实已发布快照**跑一遍，证的是「接线对 + 生产同款数据下不降级」。

判读口径（与 D14 的事实等价判据同源）：
- 快照侧与直读侧的**按块行多重集**差异数 = 0 ⇒ 内容等价；
- 闸门**没有**打 ERROR 日志 ⇒ 守卫未判定「快照缺事实」⇒ 未降级；
- 闸门返回的文本与**快照侧**渲染文本**逐字节相同** ⇒ 返回的确实是快照口径
  （不是直读结果，也不是「碰巧一样」）。

**不靠日志字面量判路径**：是否走到快照，由「复原视图对象是否不同于入参对象」
判定（`loadClassesForSchema` 的返回契约）；适配器的日志只作**人工诊断材料**，
文案改动不会让探针误判。

本脚本**只读**（SELECT），不写任何库。

用法::

    # 开关必须显式打开（脚本本身 fail-fast，避免跑成「开关关」的空证据）
    ENABLE_SEMANTIC_REGISTRY_NL2SQL=true \\
      uv run python scripts/diag_semantic_registry_gate.py \\
      --database-url-env QA_METADATA_DSN

退出码：
- ``0``：快照路径已走到；守卫未降级；两侧按块行多重集等价；
- ``2``：守卫判定有事实缺口（已降级）——**不可接受**；
- ``3``：内容等价判据不通过（块差异 ≠ 0）；
- ``4``：前置条件不满足（开关未开 / 无 ACTIVE 快照 / 复原不可用）；
- ``5``：**取快照本身失败**（连不上库 / `semantic_release` 表不存在等）——
  与「4 = 有快照但用不上」区分开：前者是环境问题，后者是数据/版本问题。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.db_snapshot_guard import (  # noqa: E402
    buildSessionFactory,
    connectionErrorExit,
    maskDatabaseUrl,
    resolveDatabaseUrl,
)

# 退出码（与模块文档一致）
EXIT_OK = 0
EXIT_FACT_GAP = 2
EXIT_NOT_EQUIVALENT = 3
EXIT_PRECONDITION = 4
EXIT_FETCH_FAILED = 5


class _LogCollector(logging.Handler):
    """收集指定模块的日志——降级会以 ERROR 级别落日志，复原会以 INFO 落日志。

    只作**诊断材料**：判路径不依赖这些文案（见模块文档）。
    """

    def __init__(self, level: int) -> None:
        super().__init__(level=level)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


async def _run(databaseUrl: str) -> int:
    from app.config import getSettings
    from app.services.nl2sql_schema import buildSchemaText
    from app.services.ontology_service import OntologyService
    from app.services.semantic_schema_adapter import loadClassesForSchema
    from app.services.semantic_schema_gate import _factDiff, renderSchemaText

    settings = getSettings()
    if not settings.enableSemanticRegistryNl2sql:
        print("前置条件不满足：ENABLE_SEMANTIC_REGISTRY_NL2SQL 未打开", file=sys.stderr)
        return EXIT_PRECONDITION

    factory = buildSessionFactory(databaseUrl)
    errorCollector = _LogCollector(logging.ERROR)
    infoCollector = _LogCollector(logging.INFO)
    gateLogger = logging.getLogger("app.services.semantic_schema_gate")
    adapterLogger = logging.getLogger("app.services.semantic_schema_adapter")
    gateLogger.addHandler(errorCollector)
    adapterLogger.addHandler(infoCollector)
    gateLogger.setLevel(logging.ERROR)
    adapterLogger.setLevel(logging.INFO)
    try:
        async with factory() as session:
            ontology = OntologyService()
            classes = await ontology.listClasses(session)
            joins = await ontology.listJoins(session)

            # ① 能不能取到快照？（取不到 ⇒ 5；取得到但不可用 ⇒ 4）
            try:
                restoredClasses, restoredJoins = await loadClassesForSchema(
                    classes, joins, session=session, enabled=True
                )
            except Exception as exc:  # noqa: BLE001 - 取快照失败是环境/部署问题
                print(f"取快照失败（表不存在 / 连不上 / 装配异常）：{exc}", file=sys.stderr)
                return EXIT_FETCH_FAILED

            directText = buildSchemaText(classes, joins=joins)
            gatedText = await renderSchemaText(classes, joins=joins, session=session)
    finally:
        gateLogger.removeHandler(errorCollector)
        adapterLogger.removeHandler(infoCollector)

    print(f"类数量 = {len(classes)}，join 数量 = {len(joins)}")
    print(f"直读文本行数 = {len(directText.splitlines())}")
    print(f"闸门文本行数 = {len(gatedText.splitlines())}")
    print(f"两侧字节相同 = {gatedText == directText}")

    print("\n== 适配器日志（诊断材料，非判据）==")
    for message in infoCollector.records:
        print(f"  {message}")

    # ② 快照路径是否真的走到？——**看对象身份**，不看日志文案。
    if restoredClasses is classes:
        print(
            "\n结论：快照路径**未走到**（无 ACTIVE 快照 / schema 版本漂移 / 快照陈旧或畸形）"
            " ⇒ 本次运行不构成影子接入的有效证据",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION

    snapshotText = buildSchemaText(restoredClasses, joins=restoredJoins)
    print(f"\n快照侧文本行数 = {len(snapshotText.splitlines())}")
    print(f"闸门返回 == 快照侧渲染（逐字节）= {gatedText == snapshotText}")

    gaps = _factDiff(directText, gatedText)
    errors = errorCollector.records
    for message in errors:
        print(f"[ERROR 日志] {message}")

    if errors:
        print(f"\n结论：守卫判定有事实缺口，已降级（ERROR 日志 {len(errors)} 条）")
        return EXIT_FACT_GAP
    if gaps:
        print(f"\n结论：内容不等价，块差异 {len(gaps)} 处：")
        for gap in gaps[:10]:
            print(f"  - {gap}")
        return EXIT_NOT_EQUIVALENT
    print("\n结论：快照路径已走到；守卫未降级；两侧按块行多重集等价（差异 0）")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="D15 语义快照闸门运行时探针（只读）")
    parser.add_argument(
        "--database-url-env",
        default="QA_METADATA_DSN",
        help="存放 DSN 的环境变量名（避免口令进 argv）",
    )
    args = parser.parse_args()

    rawUrl = os.getenv(args.database_url_env)
    if not rawUrl:
        print(f"环境变量 {args.database_url_env} 未设置", file=sys.stderr)
        return EXIT_PRECONDITION

    try:
        databaseUrl = resolveDatabaseUrl(None, args.database_url_env, allowInlinePassword=False)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PRECONDITION

    print(f"目标库 DSN = {maskDatabaseUrl(databaseUrl)}")

    try:
        return asyncio.run(_run(databaseUrl))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 连接类异常统一打码退出
        raise connectionErrorExit(exc, databaseUrl) from exc


if __name__ == "__main__":
    raise SystemExit(main())
