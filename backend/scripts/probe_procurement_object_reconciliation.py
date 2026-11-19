"""采购本体对象核对探针（只读）：M1-1.4 的源侧事实底座。

为什么需要它
------------
M1-1.4 要对 V0.3 §3.2 的 **10 个采购本体对象**逐条「对现状」。本体的现状
（`ontology_class` / `business_object`）在**本地开发库**里可以直查，但**源侧**
（THBI Oracle / SRM MySQL）的事实（某张表有没有数据、是不是空表）不在库里 ——
只能实测。

⚠️ 已有实测只覆盖了 **KPI 读路径**用到的那几张表
（见 `docs/update/1118/M0/evidence/source-capability-scan.txt`）。
M1-1.4 要核对的 10 个对象里有几个**头表/候选头表**从没被测过行数
（报价清单头 / 采购订单头 / 收货头 / 付款行 / 发票 DTL / 退货明细）⇒
「用哪个类当业务对象的头」这件事**不能靠猜**。

纪律（与 `probe_procurement_source_capability.py` 同款，不可放宽）

1. **只读**：全部语句是 ``SELECT COUNT(*)`` 聚合，不取明细行。
2. **走系统自带只读通道**：``BusinessDbAdapter.execute_read_only``（SQL Guard 校验路径）。
3. **不落密**：口令由系统 Fernet 解密后仅用于建连，不打印、不落盘、不进 argv。
4. **只写证据文件**：证据文件不含真实 DSN / 口令。

用法::

    cd backend
    uv run python scripts/probe_procurement_object_reconciliation.py \
      --out ../docs/update/1118/M1/evidence/procurement-object-reconciliation.txt

退出码：0 通过 / 1 环境错。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

sys.path.insert(0, os.getcwd())

from app.config import getSettings  # noqa: E402
from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.business_db_pool import get_adapter  # noqa: E402


@dataclass(frozen=True)
class Probe:
    """一条只读探针：标题 + SQL。"""

    title: str
    sql: str


# THBI Oracle（datasource 1）：10 个对象各自的**候选头表**。
#
# 分组说明（与 M1-1.4 核对表的行一一对应）：
# - 报价（QUOTATION）：三个候选头表都要看行数 —— 「报价清单头」是不是空壳，
#   决定 BO 的头类选它还是选年度报价明细。
# - 采购申请 / 付款 / 收货头 / 订单头：头表存在与否决定「对象有没有头」。
# - 发票 / 退货：确认 `_DTL` 变体是不是空表（避免把空表挂成业务对象头）。
THBI_PROBES: tuple[Probe, ...] = (
    Probe("报价 · 报价清单头（DWD_SUPPLIER_PRICE_LIST_HEADER）", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PRICE_LIST_HEADER"),
    Probe("报价 · 报价清单（DWD_SUPPLIER_PRICE_LIST）", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PRICE_LIST"),
    Probe("报价 · 报价明细配置（DWD_SUPPLIER_PRICE_LIST_CONFIG）", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PRICE_LIST_CONFIG"),
    Probe("报价 · 年度报价明细（DWD_PURCHASE_QUOTATION_YEARLY_DTL）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_QUOTATION_YEARLY_DTL"),
    Probe("采购申请 · 请购行（DWD_PURCHASE_REQUISITION_LINE）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_REQUISITION_LINE"),
    Probe("采购订单 · 订单头（DWD_PURCHASE_ORDER）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_ORDER"),
    Probe("采购订单 · 订单头宽表（DWD_PURCHASE_ORDER_DTL）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_ORDER_DTL"),
    Probe("收货 · 收货头（DWD_GOODS_RECEIPT）", "SELECT COUNT(*) AS n FROM THBI.DWD_GOODS_RECEIPT"),
    Probe("收货 · 收货头宽表（DWD_GOODS_RECEIPT_DTL）", "SELECT COUNT(*) AS n FROM THBI.DWD_GOODS_RECEIPT_DTL"),
    Probe("到货 · 到货单宽表（DWD_ARRIVAL_ORDER_DTL）", "SELECT COUNT(*) AS n FROM THBI.DWD_ARRIVAL_ORDER_DTL"),
    Probe("付款 · 付款（DWD_SUPPLIER_PAYMENT）", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PAYMENT"),
    Probe("付款 · 付款行（DWD_SUPPLIER_PAYMENT_LINE）", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PAYMENT_LINE"),
    Probe("发票 · AP 发票明细（DWD_AP_INVOICE_DTL）", "SELECT COUNT(*) AS n FROM THBI.DWD_AP_INVOICE_DTL"),
    Probe(
        "发票 · AP 发票明细时间跨度（判它是不是「文件抽取」旁表）",
        "SELECT COUNT(*) AS n, MIN(CREATED_DATE) AS min_created, MAX(CREATED_DATE) AS max_created "
        "FROM THBI.DWD_AP_INVOICE_DTL",
    ),
    Probe("发票 · 采购发票头（DWD_PURCHASE_INVOICE）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_INVOICE"),
    Probe("发票 · 采购发票行（DWD_PURCHASE_INVOICE_LINE）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_INVOICE_LINE"),
    Probe("发票 · 采购发票 DTL（DWD_PURCHASE_INVOICE_DTL）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_INVOICE_DTL"),
    Probe("退货 · 退货明细（DWD_PURCHASE_RETURN_DTL）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_RETURN_DTL"),
    Probe("物料 · 物料工厂（DWD_ITEM_FACILITY）", "SELECT COUNT(*) AS n FROM THBI.DWD_ITEM_FACILITY"),
    Probe("供应商 · 供应商主数据（DIM_SUPPLIER）", "SELECT COUNT(*) AS n FROM THBI.DIM_SUPPLIER"),
    Probe("供应商 · ODS 供应商（BPSUPPLIER 落库）", "SELECT COUNT(*) AS n FROM THBI.ODS_BPSUPPLIER"),
    Probe("供应商 · 合作伙伴主数据（DWD_BUSINESS_PARTNER）", "SELECT COUNT(*) AS n FROM THBI.DWD_BUSINESS_PARTNER"),
    Probe("物料 · 物料主数据（DIM_IMATERIAL）", "SELECT COUNT(*) AS n FROM THBI.DIM_IMATERIAL"),
    Probe("物料 · ODS 物料主数据（ODS_ITMMASTER）", "SELECT COUNT(*) AS n FROM THBI.ODS_ITMMASTER"),
)

PROBE_SETS: dict[int, tuple[Probe, ...]] = {1: THBI_PROBES}

# 证据文件承诺「不落密」。驱动异常常把连接串 / 口令拼进消息
# （asyncpg 的 InvalidPasswordError、Oracle 的 ORA 消息都带 user@host）
# ⇒ 落盘前统一脱敏：`scheme://user:pass@host` → `scheme://user:***@host`，
# `password=xxx` → `password=***`。
_CREDENTIAL_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://[^:@/\s]+):([^@/\s]+)@")
_PASSWORD_PAIR = re.compile(r"(?i)\b(password|passwd|pwd|secret)\s*=\s*\S+")


def maskSecrets(message: str) -> str:
    """把异常消息里的连接串口令 / ``password=`` 值替换成 ``***``。"""
    masked = _CREDENTIAL_URL.sub(r"\1:***@", message)
    return _PASSWORD_PAIR.sub(r"\1=***", masked)


async def runDatasource(session: AsyncSession, datasourceId: int) -> list[str]:
    """对单个数据源跑完探针，返回可打印的行（失败也保留，便于取证）。"""
    ds = (
        await session.execute(select(DataSource).where(DataSource.id == datasourceId))
    ).scalar_one_or_none()
    if ds is None:
        return [f"=== datasource {datasourceId} 不存在 ==="]
    lines = [f"=== datasource {datasourceId} = {ds.name} / {ds.type} ==="]
    adapter = get_adapter(datasourceId, ds)
    try:
        for probe in PROBE_SETS[datasourceId]:
            lines.append("")
            lines.append(f"--- {probe.title} ---")
            lines.append(f"SQL: {probe.sql}")
            try:
                rows = await adapter.execute_read_only(probe.sql, maxRows=5)
            except Exception as exc:  # noqa: BLE001 —— 探针要把失败本身记下来
                lines.append(f"[ERROR] {type(exc).__name__}: {maskSecrets(str(exc))}")
                continue
            for row in rows:
                lines.append("  " + " | ".join(f"{k}={v}" for k, v in row.items()))
            if not rows:
                lines.append("  (0 行)")
    finally:
        await adapter.dispose()
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(description="采购本体对象核对探针（只读）")
    parser.add_argument("--out", help="输出文件路径（默认写 stdout）")
    args = parser.parse_args()

    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, pool_pre_ping=False)
    lines: list[str] = [
        "采购本体对象核对探针 · 只读实测（M1-1.4）",
        "口径：SELECT COUNT(*) 聚合；走 BusinessDbAdapter.execute_read_only（SQL Guard）；不落密。",
        "",
    ]
    try:
        async with AsyncSession(engine) as session:
            for datasourceId in sorted(PROBE_SETS):
                lines.extend(await runDatasource(session, datasourceId))
    except Exception as exc:  # noqa: BLE001 —— 环境错要可读
        print(
            f"[ENV ] 探针无法执行：{type(exc).__name__}: {maskSecrets(str(exc))}",
            file=sys.stderr,
        )
        return 1
    finally:
        await engine.dispose()

    output = "\n".join(lines) + "\n"
    if args.out:
        path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(output)
        print(f"已写出：{path}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
