"""品牌配置公开端点（真实 PostgreSQL + 完整 HTTP 链路）。

覆盖 4 件事（每条都是**会咬人**的失败模式）：

1. **端点是公开的**：登录页在未登录时也要显示系统名称 ⇒ 不能挂 `getCurrentUser`。
   本用例**不带任何身份头**请求它，拿到 200 才算数（带上 admin 头的 200 说明不了公开）。
2. **无配置行 ⇒ 返回源码默认值**：全新库 / 种子没跑时也不能 500 或返回空串。
3. **DB 里配了就生效**：admin 改 `system_config` 后，端点返回新值（免发版的意义）。
4. **`BRAND_TITLE` 未配置 ⇒ 跟随 `BRAND_NAME`**：改了主名，标签页标题自动跟着变。

强制规则（`Harness/rules/测试规范.md`）：真实 PostgreSQL + 完整 API 链路，
禁止 sqlite 内存库 + 直接调 service。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import getSettings
from app.services.branding_service import (
    BRAND_NAME_EN_KEY,
    BRAND_NAME_KEY,
    BRAND_SHORT_NAME_KEY,
    BRAND_TITLE_KEY,
    DEFAULT_BRAND_NAME,
    DEFAULT_BRAND_NAME_EN,
    DEFAULT_BRAND_SHORT_NAME,
)


async def _seedBrandRow(dbSession, key: str, value: str | None) -> None:
    """往 system_config 写一行品牌配置（测试 Arrange）。"""
    await dbSession.execute(
        text(
            "INSERT INTO system_config (key, value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (key) DO UPDATE "
            "SET value = EXCLUDED.value, updated_time = NOW()"
        ),
        {"k": key, "v": value, "d": "品牌配置（测试）"},
    )
    await dbSession.commit()


@pytest.mark.asyncio
class TestBrandingApi:
    """GET /api/v1/branding — 4 cases。"""

    async def test_is_public_without_any_identity_header(self, client) -> None:
        """**不带任何身份头**也能拿到 200（登录页在未登录时就要用它）。

        带 admin 头的 200 说明不了「公开」—— 那种 200 可能来自鉴权通过。
        """
        resp = await client.get("/api/v1/branding")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for field in ("brandName", "brandNameEn", "brandShortName", "brandTitle"):
            assert field in body, f"响应缺字段 {field}: {body}"

    async def test_returns_source_defaults_without_config_rows(self, client) -> None:
        """库里没有品牌行 ⇒ 返回源码默认值（不 500、不返回空串）。"""
        resp = await client.get("/api/v1/branding")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["brandName"] == DEFAULT_BRAND_NAME
        assert body["brandNameEn"] == DEFAULT_BRAND_NAME_EN
        assert body["brandShortName"] == DEFAULT_BRAND_SHORT_NAME
        assert body["brandTitle"] == DEFAULT_BRAND_NAME

    async def test_reflects_configured_values(self, client, dbSession) -> None:
        """DB 里配了就生效（免发版改品牌的意义）。"""
        await _seedBrandRow(dbSession, BRAND_NAME_KEY, "泰鸿智云")
        await _seedBrandRow(dbSession, BRAND_NAME_EN_KEY, "Taihong Cloud")
        await _seedBrandRow(dbSession, BRAND_SHORT_NAME_KEY, "TC")
        await _seedBrandRow(dbSession, BRAND_TITLE_KEY, "泰鸿智云 · 数据智能")

        resp = await client.get("/api/v1/branding")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["brandName"] == "泰鸿智云"
        assert body["brandNameEn"] == "Taihong Cloud"
        assert body["brandShortName"] == "TC"
        assert body["brandTitle"] == "泰鸿智云 · 数据智能"

    async def test_title_follows_name_when_title_not_configured(self, client, dbSession) -> None:
        """`BRAND_TITLE` 未配置 ⇒ 跟随 `BRAND_NAME`。

        种子把 BRAND_TITLE 播成 NULL 就是为了这个：改主名时标题自动跟着变。
        """
        await _seedBrandRow(dbSession, BRAND_NAME_KEY, "泰鸿智云")

        resp = await client.get("/api/v1/branding")
        assert resp.status_code == 200, resp.text
        assert resp.json()["brandTitle"] == "泰鸿智云"


@pytest.mark.asyncio
async def test_branding_not_blocked_in_real_auth_mode(client, monkeypatch) -> None:
    """AUTH_MODE=real（**生产模式**）下也不能被挡。

    登录页在未登录时就要读品牌 ⇒ 生产上必须能拿到。
    `rateLimitDep` 用的是「可选身份」（`_optionalCurrentUser`），无 Bearer 时
    降级到 IP bucket 而非 403 —— 本用例锁住这一点（同类回归见
    `test_auth_login_real_mode.py`：公开端点曾被误挡 403）。

    ⚠️ **必须用 setenv 而不是 monkeypatch.setattr(settings, ...)**：
    `getSettings` 是 `@lru_cache`，monkeypatch 改的是**缓存里那个实例**；
    紧接着 `cache_clear()` 会让下一次 `getSettings()` 重新从 env 构造 ⇒
    monkeypatch 的值直接丢了 ⇒ 用例悄悄跑在 stub 模式下（假绿）。
    setenv + cache_clear 才是「让请求链路真的跑在 real 模式」。
    """
    monkeypatch.setenv("AUTH_MODE", "real")
    monkeypatch.setenv("ALLOW_STUB_WHEN_REAL", "false")
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")
    getSettings.cache_clear()
    try:
        # 先自证：当前确实是 real 模式（否则本用例等于没测）
        assert getSettings().authMode == "real"
        resp = await client.get("/api/v1/branding")
        assert resp.status_code == 200, (
            f"real 模式下 branding 被挡（多半是 rateLimitDep 误挡 403）：{resp.text}"
        )
        assert "brandName" in resp.json()
    finally:
        getSettings.cache_clear()
