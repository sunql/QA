"""图表决策引擎的可调阈值：system_config 读取 + 一次装载。

**为什么在 system_config 而不写死在源码里**（`Harness/rules/魔数治理.md` 三档判定）：
这 4 个数字都是**运营期想调**的 pipeline 行为边界 —— 「饼图到几片算太多」「类目多到
几个该横过来」「矩阵多稠才算热力图」「多大的 N 还算 TOP N」。真实命中率只能在
线上观察，写死就得改代码重发。

**不治理的**：ETL 列黑名单、时间列名正则、问句关键词集合（占比/趋势/同比…）。按同一
份规则的反例档 —— 它们是解析语法与契约，admin 改了反而挂（正则写错 = 时间维识别
全废），故留在源码里由测试钉住。

读取口径与 `chat_recall._getClassFilterMaxClasses`（SSOT）一致：
**不缓存**（每请求现读，admin 改值对新问句立即生效）/ **失败不阻断**
（DB 抖、格式错、非正 → 返默认 + warning）/ **非正视同非法**（0 与负数不是
「切得更狠」而是闸门静默消失，例如 `pieMaxRows=0` 让环形图永不可达）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.data_summary import FULL_DATA_THRESHOLD

logger = logging.getLogger(__name__)

# 饼图/环形图超过这个行数就该换横向柱状（读不清的同心扇区没有价值）。
# 取代原先散在 chart_service 里未受治理的 _PIE_ROW_LIMIT = 6。
_CHART_PIE_MAX_ROWS_DEFAULT = 6
# 类目多到这个数，竖向柱状的类目名会挤成一团，横过来读。
_CHART_HBAR_MIN_ROWS_DEFAULT = 15
# 热力图门槛：交叉矩阵完备度 = 实际行数 / (维度1基数 × 维度2基数)。
# 稀疏矩阵画出来是大片空白，不如退成普通柱状。
_CHART_HEATMAP_MIN_COVERAGE_DEFAULT = 0.6
# TOP N 的上限：超过这个行数就不算「Top N」而是普通分类比较。
_CHART_TOP_N_MAX_DEFAULT = 20


async def readPositiveIntConfig(session: AsyncSession, key: str, default: int) -> int:
    """读 system_config[key] 并解析为正整数；缺席/格式错/非正 → default。

    与治理规范同口径：`text()` 直写 key（不走 ORM，避免 identity map 缓存），
    读失败只 warning 不抛。四个 int 阈值共用此实现，避免复制粘贴漂移。
    """
    try:
        row = await session.execute(
            text(f"SELECT value FROM system_config WHERE key = '{key}'")
        )
        raw = row.scalar_one_or_none()
        if raw is None or raw == "":
            return default
        value = int(raw)
        if value <= 0:
            logger.warning("%s 非正值 %r，返默认值 %d", key, raw, default)
            return default
        return value
    except (TypeError, ValueError):
        logger.warning("%s 值非法 %r，返默认值 %d", key, raw, default)
        return default
    except Exception:
        logger.warning("读取 %s 失败，返默认值 %d", key, default, exc_info=True)
        return default


async def _getChartPieMaxRows(session: AsyncSession) -> int:
    """读 CHART_PIE_MAX_ROWS；缺席/格式错/非正返默认。"""
    return await readPositiveIntConfig(session, "CHART_PIE_MAX_ROWS", _CHART_PIE_MAX_ROWS_DEFAULT)


async def _getChartHbarMinRows(session: AsyncSession) -> int:
    """读 CHART_HBAR_MIN_ROWS；缺席/格式错/非正返默认。"""
    return await readPositiveIntConfig(
        session, "CHART_HBAR_MIN_ROWS", _CHART_HBAR_MIN_ROWS_DEFAULT
    )


async def _getChartTopNMax(session: AsyncSession) -> int:
    """读 CHART_TOP_N_MAX；缺席/格式错/非正返默认。"""
    return await readPositiveIntConfig(session, "CHART_TOP_N_MAX", _CHART_TOP_N_MAX_DEFAULT)


async def _getChartHeatmapMinCoverage(session: AsyncSession) -> float:
    """读 CHART_HEATMAP_MIN_COVERAGE；缺席/格式错/超出 (0, 1] 返默认。

    比 int 多一条边界：完备度是比值，**> 1 不可能达成**，等价于把热力图永久禁用
    （闸门静默失效），故与 ≤ 0 同样按非法处理。`1.0` 是合法下界语义（要求完全稠密）。
    """
    key = "CHART_HEATMAP_MIN_COVERAGE"
    default = _CHART_HEATMAP_MIN_COVERAGE_DEFAULT
    try:
        row = await session.execute(
            text(f"SELECT value FROM system_config WHERE key = '{key}'")
        )
        raw = row.scalar_one_or_none()
        if raw is None or raw == "":
            return default
        value = float(raw)
        if not 0.0 < value <= 1.0:
            logger.warning("%s 超出 (0, 1] 区间 %r，返默认值 %.2f", key, raw, default)
            return default
        return value
    except (TypeError, ValueError):
        logger.warning("%s 值非法 %r，返默认值 %.2f", key, raw, default)
        return default
    except Exception:
        logger.warning("读取 %s 失败，返默认值 %.2f", key, default, exc_info=True)
        return default


async def loadFullDataThreshold(session: AsyncSession) -> int:
    """读 FULL_DATA_THRESHOLD；缺席/格式错/非正返默认（数据清单全量/摘要分界）。

    默认值直接 import 自 data_summary 的 FULL_DATA_THRESHOLD（SSOT，不抄字面量）。
    """
    return await readPositiveIntConfig(session, "FULL_DATA_THRESHOLD", FULL_DATA_THRESHOLD)


@dataclass(frozen=True)
class ChartThresholds:
    """一次请求装载的全部图表阈值（不可变，往下透传给纯函数决策引擎）。"""

    pieMaxRows: int
    hbarMinRows: int
    heatmapMinCoverage: float
    topNMax: int


async def loadChartThresholds(session: AsyncSession) -> ChartThresholds:
    """装载 4 个阈值。任一读取失败都会回落到默认值，故本函数不会抛。"""
    return ChartThresholds(
        pieMaxRows=await _getChartPieMaxRows(session),
        hbarMinRows=await _getChartHbarMinRows(session),
        heatmapMinCoverage=await _getChartHeatmapMinCoverage(session),
        topNMax=await _getChartTopNMax(session),
    )
