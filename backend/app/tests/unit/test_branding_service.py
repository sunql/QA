"""品牌配置服务：解析口径 + 跨栈漂移守卫（feat-configurable-brand）。

品牌默认值有**三份拷贝**：后端源码常量、前端 `src/branding/defaults.ts`、
`frontend/index.html` 的静态 `<title>`。任何一份漏改 ⇒ 接口失败时露出旧名字
（后端说 A、前端兜底 B），而且是**只在降级路径上出现**的漂移 —— 线上极难排查。

所以本文件把漂移变成可执行的断言：

1. `resolveBranding` 的解析口径（缺行 / 空值 / 超长 / 标题回落链）；
2. 后端 `DEFAULT_*` == 前端 `defaults.ts` 常量（跨栈，逐字比对）；
3. 品牌键都在种子列表里，且种子值 == 源码默认值。

DB 行为（真 PG、HTTP 端点）由
`app/tests/integration/test_branding_api.py` 覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.branding_service import (
    BRAND_KEYS,
    BRAND_NAME_EN_KEY,
    BRAND_NAME_KEY,
    BRAND_SHORT_NAME_KEY,
    BRAND_TITLE_KEY,
    DEFAULT_BRAND_NAME,
    DEFAULT_BRAND_NAME_EN,
    DEFAULT_BRAND_SHORT_NAME,
    MAX_BRAND_NAME_LENGTH,
    MAX_BRAND_SHORT_NAME_LENGTH,
    MAX_BRAND_TITLE_LENGTH,
    resolveBranding,
)
from scripts.seed_system_config import BRAND_CONFIG_SEEDS

#: 仓库根目录（本文件在 backend/app/tests/unit/ ⇒ 上溯 4 层）。
_REPO_ROOT = Path(__file__).resolve().parents[4]
_FRONTEND_DEFAULTS = _REPO_ROOT / "frontend" / "src" / "branding" / "defaults.ts"


class TestResolveBranding:
    """解析口径（纯函数）。"""

    def test_missing_keys_fall_back_to_source_defaults(self) -> None:
        """一行都没有 ⇒ 全部回落源码默认值。"""
        branding = resolveBranding({})
        assert branding.brand_name == DEFAULT_BRAND_NAME
        assert branding.brand_name_en == DEFAULT_BRAND_NAME_EN
        assert branding.brand_short_name == DEFAULT_BRAND_SHORT_NAME
        assert branding.brand_title == DEFAULT_BRAND_NAME

    def test_configured_values_win(self) -> None:
        """DB 里有值 ⇒ 用 DB 的值。"""
        branding = resolveBranding(
            {
                BRAND_NAME_KEY: "泰鸿智云",
                BRAND_NAME_EN_KEY: "Taihong Cloud",
                BRAND_SHORT_NAME_KEY: "TC",
                BRAND_TITLE_KEY: "泰鸿智云 · 数据智能",
            }
        )
        assert branding.brand_name == "泰鸿智云"
        assert branding.brand_name_en == "Taihong Cloud"
        assert branding.brand_short_name == "TC"
        assert branding.brand_title == "泰鸿智云 · 数据智能"

    def test_blank_values_fall_back(self) -> None:
        """空串 / 纯空白 ⇒ 回落默认（不留空白标题）。"""
        branding = resolveBranding(
            {
                BRAND_NAME_KEY: "",
                BRAND_NAME_EN_KEY: "   ",
                BRAND_SHORT_NAME_KEY: None,
            }
        )
        assert branding.brand_name == DEFAULT_BRAND_NAME
        assert branding.brand_name_en == DEFAULT_BRAND_NAME_EN
        assert branding.brand_short_name == DEFAULT_BRAND_SHORT_NAME

    def test_title_follows_name_when_not_configured(self) -> None:
        """`BRAND_TITLE` 未配置 ⇒ 跟随 `BRAND_NAME`（不是源码默认）。

        这是「改了主名，标签页标题自动跟着变」的实现点；种子把 BRAND_TITLE
        播成 NULL 就是为了让它生效。
        """
        branding = resolveBranding({BRAND_NAME_KEY: "泰鸿智云"})
        assert branding.brand_title == "泰鸿智云"

    def test_overlong_values_are_truncated(self) -> None:
        """超长值截断（防止 admin 贴长文本撑爆侧边栏）。"""
        branding = resolveBranding(
            {
                BRAND_NAME_KEY: "x" * 500,
                BRAND_SHORT_NAME_KEY: "y" * 500,
            }
        )
        assert len(branding.brand_name) == MAX_BRAND_NAME_LENGTH
        assert len(branding.brand_short_name) == MAX_BRAND_SHORT_NAME_LENGTH

    def test_values_are_trimmed(self) -> None:
        """前后空白被去掉（admin 粘贴时经常带空格）。"""
        branding = resolveBranding({BRAND_NAME_KEY: "  泰鸿智脑  "})
        assert branding.brand_name == "泰鸿智脑"


class TestBrandSeed:
    """品牌种子（`system_config` 的 4 行）。"""

    def test_brand_seed_covers_all_brand_keys(self) -> None:
        """每个品牌键都有种子行（否则 admin 在配置页看不到、也改不了）。"""
        seeded = {key for key, _, _ in BRAND_CONFIG_SEEDS}
        assert seeded == set(BRAND_KEYS)

    def test_brand_seed_values_match_source_defaults(self) -> None:
        """种子值 == 源码默认值（除 BRAND_TITLE，见下）。"""
        seeded = {key: value for key, value, _ in BRAND_CONFIG_SEEDS}
        assert seeded[BRAND_NAME_KEY] == DEFAULT_BRAND_NAME
        assert seeded[BRAND_NAME_EN_KEY] == DEFAULT_BRAND_NAME_EN
        assert seeded[BRAND_SHORT_NAME_KEY] == DEFAULT_BRAND_SHORT_NAME
        # BRAND_TITLE 刻意播种为 NULL：语义是「未配置 ⇒ 跟随主名称」。
        # 若播种成写死的主名，admin 改了主名却发现标题没跟着变，会以为功能坏了。
        assert seeded[BRAND_TITLE_KEY] is None

    def test_every_brand_seed_has_a_description(self) -> None:
        """admin 在配置页靠 description 理解这个键是干什么的。"""
        for key, _, description in BRAND_CONFIG_SEEDS:
            assert description.strip(), f"{key} 缺少 description"

    def test_brand_seed_never_overwrites_value(self) -> None:
        """品牌种子的冲突路径**只刷 description，不碰 value**。

        回归来历：品牌是**内容**，admin 改过之后不能被发版重置。
        （阈值种子的冲突路径是 `set_={"value": ...}` —— 那是「默认值下发」通道，
        品牌种子不能长成那样。）

        实现细节（2026-11-28 催货函抬头种子复用同一通道后调整）：内容型种子的
        upsert 收敛在 `_seedContentConfigs`（品牌 / 催货函抬头共用），`seed_brand_config`
        是它的调用方 ⇒ **断言对象随之改成那个共享实现**，并额外钉住「品牌种子确实
        走内容型通道」（否则有人把它改调阈值型通道，守卫会空转）。
        """
        import inspect

        from scripts.seed_system_config import _seedContentConfigs, seed_brand_config

        source = inspect.getsource(_seedContentConfigs)
        assert 'set_={"description": description}' in source, (
            "内容型种子的冲突路径应只刷 description"
        )
        assert 'set_={"value"' not in source, (
            "内容型种子的冲突路径不得覆盖 value（admin 改过的品牌名会被发版重置）"
        )
        assert "_seedContentConfigs" in inspect.getsource(seed_brand_config), (
            "品牌种子必须走内容型通道（只刷 description）"
        )


class TestCrossStackDefaults:
    """跨栈漂移守卫：后端常量 == 前端兜底常量。"""

    @staticmethod
    def _frontendConstant(name: str) -> str:
        """读前端 `defaults.ts` 里的常量（字符串或数字，统一按字符串比较）。"""
        text = _FRONTEND_DEFAULTS.read_text(encoding="utf-8")
        match = re.search(rf'export const {name} = (?:"([^"]*)"|(\d+));', text)
        assert match is not None, f"前端 defaults.ts 缺少常量 {name}"
        return match.group(1) if match.group(1) is not None else match.group(2)

    def test_frontend_fallback_defaults_match_backend(self) -> None:
        """前端兜底默认值必须与后端逐字一致。

        不一致的后果：后端不可达时前端显示**旧名字**，接口恢复后又变回新名字 ——
        用户会看到品牌名「自己跳了一下」，且只在降级窗口出现，极难定位。
        """
        assert self._frontendConstant("DEFAULT_BRAND_NAME") == DEFAULT_BRAND_NAME
        assert (
            self._frontendConstant("DEFAULT_BRAND_NAME_EN") == DEFAULT_BRAND_NAME_EN
        )
        assert (
            self._frontendConstant("DEFAULT_BRAND_SHORT_NAME")
            == DEFAULT_BRAND_SHORT_NAME
        )
        assert self._frontendConstant("DEFAULT_BRAND_TITLE") == DEFAULT_BRAND_NAME

    def test_frontend_truncation_limits_match_backend(self) -> None:
        """截断上限也必须两端一致。

        漂移的症状同样是「品牌名自己跳了一下」：在线路径后端截到 64，
        降级路径前端截到 32 ⇒ 只有后端挂掉时才看得出差别。
        """
        assert (
            self._frontendConstant("MAX_BRAND_NAME_LENGTH")
            == str(MAX_BRAND_NAME_LENGTH)
        )
        assert (
            self._frontendConstant("MAX_BRAND_SHORT_NAME_LENGTH")
            == str(MAX_BRAND_SHORT_NAME_LENGTH)
        )
        assert (
            self._frontendConstant("MAX_BRAND_TITLE_LENGTH")
            == str(MAX_BRAND_TITLE_LENGTH)
        )


class TestRouteAndStartupWiring:
    """端点与启动链的接线守卫。

    「只在 `main.py` 挂、忘了挂 `_testapp.py`」是本仓真实踩过的坑
    （2026-11-27 实测：端点 404，因为测试应用是独立装配的）⇒ 这里钉住两处都要挂。

    ⚠️ 断言对象是**构建出来的 app 的依赖链**，不是源码文本：
    `include_router(...)` 是多行调用，扫源码里的「同一行同时含 branding 与
    include_router」永远匹配不到 ⇒ 那种守卫是**空集假绿**（本仓已因
    「守卫空集假通过」栽过跟头，见 memory `qa-system-guard-vacuous-pass-lib-upgrade`）。
    """

    @staticmethod
    def _backendSource(relative: str) -> str:
        return (_REPO_ROOT / "backend" / relative).read_text(encoding="utf-8")

    @staticmethod
    def _brandingRoutes(app) -> list:
        """从 app 的路由树里找出 `/api/v1/branding`（含 include 级依赖）。"""
        from app.tests._route_auth_support import iterRoutesWithDeps

        return [
            (path, route, inherited)
            for path, route, inherited in iterRoutesWithDeps(app.routes)
            if path == "/api/v1/branding"
        ]

    def test_route_registered_in_app_and_test_app(self) -> None:
        """生产应用与测试应用**都要**注册 branding 路由。

        用真实 app 枚举（而不是扫源码）：只挂一处时，另一处的集成测试会 404。
        """
        from app.main import createApp
        from app.tests._testapp import buildTestApp

        def _unusedFactory() -> None:  # pragma: no cover —— 只存引用，不调用
            raise AssertionError("buildTestApp 不应在建 app 阶段调用会话工厂")

        for label, app in (
            ("main.py", createApp()),
            ("_testapp.py", buildTestApp(_unusedFactory)),
        ):
            routes = self._brandingRoutes(app)
            assert routes, f"{label}: 未枚举到 /api/v1/branding 路由"

    def test_route_is_public_in_both_apps(self) -> None:
        """端点**不挂鉴权**：挂 `getCurrentUser` 会让登录页拿不到品牌名。

        2026-11-27 实测踩坑：`test_route_auth_guard.py` 的
        「非白名单路由必须鉴权」守卫会把新公开路由判成违规 ⇒ 必须同时登记进
        `PUBLIC_ROUTES`。本用例从**依赖链**层面锁住「确实是公开的」。
        """
        from app.dependencies import getCurrentUser
        from app.main import createApp
        from app.tests._route_auth_support import declaresDependency
        from app.tests._testapp import buildTestApp

        def _unusedFactory() -> None:  # pragma: no cover —— 只存引用，不调用
            raise AssertionError("buildTestApp 不应在建 app 阶段调用会话工厂")

        for label, app in (
            ("main.py", createApp()),
            ("_testapp.py", buildTestApp(_unusedFactory)),
        ):
            for _path, route, inherited in self._brandingRoutes(app):
                assert not declaresDependency(route, getCurrentUser, inherited), (
                    f"{label}: /api/v1/branding 被挂上了鉴权（登录页拿不到品牌名）"
                )

    def test_route_is_registered_in_public_route_whitelist(self) -> None:
        """公开路由必须登记进 `test_route_auth_guard.PUBLIC_ROUTES`。

        漏登记 ⇒ 该守卫报「以下路由匿名可达」⇒ CI 集成分片红。
        """
        from app.tests.integration.test_route_auth_guard import PUBLIC_ROUTES

        assert ("GET", "/api/v1/branding") in PUBLIC_ROUTES, (
            "branding 端点未登记进 PUBLIC_ROUTES（路由鉴权守卫会红）"
        )

    def test_startup_chain_seeds_brand_config(self) -> None:
        """启动链必须**调用**品牌种子（否则 admin 在配置页看不到、也改不了）。

        断言的是调用点而不是 import —— 只留 import、删掉调用，守卫照样绿。
        """
        mainSource = self._backendSource("app/main.py")
        assert "await seed_brand_config(" in mainSource, (
            "启动链未调用 seed_brand_config（品牌键不会被播种）"
        )
