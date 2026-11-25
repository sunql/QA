"""待到货工具的参数解析（纯函数、fail-closed；2026-11-25 从实现模块拆出）。

拆出原因：`pending_delivery_query` 逼近文件规模上限；参数规整与 SQL 渲染
边界清晰，放一起会让两边都难读。这里只做「不可信入参 → 明确类型」，
不做 IO、不碰绑定。
"""

from __future__ import annotations

from app.domain.exceptions import ValidationError


def rejectCallerDatasource(args: dict) -> None:
    """THBI 硬规则：数据源由本体绑定唯一确定，调用方不得指定。"""
    if "datasourceId" in args:
        raise ValidationError(
            "pending_delivery_query 的数据源固定为 THBI，不接受调用方指定数据源（datasourceId）"
        )


def readIntArg(value: object, field: str, default: int) -> int:
    """整数参数（bool/字符串 fail-closed，不静默 `int()` 转换）。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数，收到 {value!r}")
    return value


def readOptionalInt(value: object, field: str) -> int | None:
    """可选正整数（None/0 ⇒ None，表示不筛）。"""
    if value is None or value == 0:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数，收到 {value!r}")
    return value


def readStrList(value: object) -> tuple[str, ...]:
    """字符串列表参数（单个字符串视作一项；非列表 ⇒ 空）。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def readFilterArgs(
    args: dict,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str | None, str | None]:
    """过滤参数：供应商键（兼容 legacy `supplierKey`）/ 物料 / 基地 / 日期段。"""
    legacySupplier = str(args.get("supplierKey") or "").strip()
    supplierKeys = readStrList(args.get("supplierKeys")) or (
        (legacySupplier,) if legacySupplier else ()
    )
    return (
        supplierKeys,
        readStrList(args.get("materialKeys")),
        readStrList(args.get("siteKeys")),
        args.get("dateFrom"),
        args.get("dateTo"),
    )
