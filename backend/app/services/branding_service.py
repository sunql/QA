"""品牌配置服务（feat-configurable-brand）。

系统标题 / 名称的**运行时 SSOT** 是 `system_config` 表的 4 行：

===============  ===================================  ==================
key              含义                                 默认值
===============  ===================================  ==================
BRAND_NAME       品牌主名称（中文）                    泰鸿智脑
BRAND_NAME_EN    品牌英文名                           Taihong ZhiNao
BRAND_SHORT_NAME 侧边栏折叠态缩写                     TH
BRAND_TITLE      浏览器标签页标题                     泰鸿智脑
===============  ===================================  ==================

为什么落在 `system_config` 而不是构建期环境变量：品牌名是**运维参数**（不同交付
环境写不同名字），走 admin「系统配置」页改一行即可，不必重新构建前端镜像。

三层回落链（每一层职责单一，不重复兜底）：

1. `system_config` 行 —— 运行时 SSOT（admin 可改，写 audit_log 留痕）；
2. 本模块的 `DEFAULT_*` —— 行缺失 / 空值时的源码默认值；
3. 前端 `src/branding/defaults.ts` —— 后端不可达时的最后兜底（首屏零闪烁）。

⚠️ `BRAND_TITLE` 未配置时回落 **`BRAND_NAME`**（不是回落源码默认）⇒ 改了主名
时标签页标题自动跟随；两者都没配才回落到源码默认。

⚠️ 读取路径**永远不抛错**：品牌名配错不该让整个页面 500。取不到值就退回默认，
超长值截断（防止 admin 贴了一段长文本把侧边栏撑爆）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置键
# ---------------------------------------------------------------------------

BRAND_NAME_KEY = "BRAND_NAME"
BRAND_NAME_EN_KEY = "BRAND_NAME_EN"
BRAND_SHORT_NAME_KEY = "BRAND_SHORT_NAME"
BRAND_TITLE_KEY = "BRAND_TITLE"

BRAND_KEYS: tuple[str, ...] = (
    BRAND_NAME_KEY,
    BRAND_NAME_EN_KEY,
    BRAND_SHORT_NAME_KEY,
    BRAND_TITLE_KEY,
)

# ---------------------------------------------------------------------------
# 源码默认值（行缺失时的回落）
# ---------------------------------------------------------------------------

DEFAULT_BRAND_NAME = "泰鸿智脑"
DEFAULT_BRAND_NAME_EN = "Taihong ZhiNao"
DEFAULT_BRAND_SHORT_NAME = "TH"

#: 长度上限（防御 admin 误贴超长文本撑爆布局）。
MAX_BRAND_NAME_LENGTH = 64
MAX_BRAND_SHORT_NAME_LENGTH = 16
MAX_BRAND_TITLE_LENGTH = 128


@dataclass(frozen=True)
class Branding:
    """一次解析出来的品牌配置（不可变）。"""

    brand_name: str
    brand_name_en: str
    brand_short_name: str
    brand_title: str


def _sanitize(value: str | None, fallback: str, maxLength: int) -> str:
    """清洗单个配置值：去空白 → 空则回落 → 超长截断。

    永不抛错、永不返回空串（空串会让侧边栏标题变成空白块）。
    """
    if value is None:
        return fallback
    trimmed = value.strip()
    if not trimmed:
        return fallback
    return trimmed[:maxLength]


def resolveBranding(values: dict[str, str | None]) -> Branding:
    """从「key → value」映射解析出品牌配置（纯函数，可单测）。

    这是**唯一的解析口径**：HTTP 端点走它，种子脚本走同一份源码默认值常量 ——
    两条路径不会算出不同的品牌（那种漂移会让 admin 改了一个键却看到另一个名字）。
    """
    name = _sanitize(
        values.get(BRAND_NAME_KEY), DEFAULT_BRAND_NAME, MAX_BRAND_NAME_LENGTH
    )
    nameEn = _sanitize(
        values.get(BRAND_NAME_EN_KEY), DEFAULT_BRAND_NAME_EN, MAX_BRAND_NAME_LENGTH
    )
    shortName = _sanitize(
        values.get(BRAND_SHORT_NAME_KEY),
        DEFAULT_BRAND_SHORT_NAME,
        MAX_BRAND_SHORT_NAME_LENGTH,
    )
    # 标题回落链：BRAND_TITLE → BRAND_NAME → 源码默认
    title = _sanitize(
        values.get(BRAND_TITLE_KEY), name, MAX_BRAND_TITLE_LENGTH
    )
    return Branding(
        brand_name=name,
        brand_name_en=nameEn,
        brand_short_name=shortName,
        brand_title=title,
    )


async def loadBranding(session: AsyncSession) -> Branding:
    """从 `system_config` 读 4 个品牌键并解析（单条查询）。

    **永不抛错**：读不到行 / 值为空 / 值为 null 一律回落到源码默认；
    DB 本身出问题（连接断了、锁超时）也只记 warning 并回落到默认 ——
    品牌名取不到不该让整个页面 500（端点契约：永远返回 200）。
    """
    try:
        rows = await session.execute(
            select(SystemConfig.key, SystemConfig.value).where(
                SystemConfig.key.in_(BRAND_KEYS)
            )
        )
    except SQLAlchemyError:
        logger.warning("读 system_config 品牌键失败，回落到默认品牌名", exc_info=True)
        return resolveBranding({})
    values = {str(key): value for key, value in rows.all()}
    return resolveBranding(values)
