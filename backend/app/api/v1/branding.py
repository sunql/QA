"""品牌配置公开只读端点（feat-configurable-brand）。

    GET /api/v1/branding  →  { brandName, brandNameEn, brandShortName, brandTitle }

**为什么是公开端点（无鉴权）**：登录页也要显示系统名称/标题，而登录页此时用户
还没登录。端点只返回**产品命名**（4 个短字符串），不含任何租户数据、用户数据、
业务指标 —— 公开它不构成信息泄露。若将来要加敏感字段，必须先加鉴权。

**为什么不挂 `mustChangePasswordGuard`**：该守卫要求已登录用户，本端点面向
未登录用户。改密强制只对「已登录但必须改密」的场景有意义，与品牌名无关。

**为什么不挂 `getCurrentUser`**：见上。admin 侧改配置的写入口仍然是
`PUT /api/v1/admin/system-config/{key}`（admin-only + audit_log）。

值来自 `system_config` 表（运行时 SSOT），行缺失时回落到源码默认值；
**本端点永远返回 200** —— 品牌名取不到不该让页面报错。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getDb
from app.domain.schemas import BrandingRead
from app.services.branding_service import loadBranding

router = APIRouter(tags=["branding"])


@router.get("/branding", response_model=BrandingRead, summary="品牌配置（公开只读）")
async def getBranding(
    response: Response,
    session: AsyncSession = Depends(getDb),
) -> BrandingRead:
    """返回当前生效的品牌名 / 缩写 / 页面标题。

    `Cache-Control: no-store` —— 品牌名改了要**立刻**看到；让浏览器/代理缓存
    旧值等于「admin 改了没生效」，极难排查。
    """
    response.headers["Cache-Control"] = "no-store"
    branding = await loadBranding(session)
    return BrandingRead(
        brand_name=branding.brand_name,
        brand_name_en=branding.brand_name_en,
        brand_short_name=branding.brand_short_name,
        brand_title=branding.brand_title,
    )
