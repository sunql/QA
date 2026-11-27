# 品牌名参数化 — 交付摘要

- **日期**：2026-11-27
- **分支**：`feat/configurable-brand`（基线 `origin/main` = `f065d48d`）
- **方案**：见 [plan.md](./plan.md)

## 1. 做了什么

系统标题 / 名称（浏览器标签页、侧边栏展开与折叠态、登录页标题）从**写死的字面量**
改为**运行时参数**：

```
system_config 的 4 个键（admin「系统配置」页可改，带 audit_log）
   ↓ GET /api/v1/branding（公开只读，无需登录）
brandingStore（zustand）
   ↓
侧边栏展开态 / 折叠态 / 登录页标题 / document.title
```

| key | 含义 | 默认值 |
|---|---|---|
| `BRAND_NAME` | 品牌主名称（中文） | 泰鸿智脑 |
| `BRAND_NAME_EN` | 品牌英文名 | Taihong ZhiNao |
| `BRAND_SHORT_NAME` | 侧边栏折叠态缩写 | TH |
| `BRAND_TITLE` | 浏览器标签页标题（留空 ⇒ 跟随主名称） | 泰鸿智脑 |

## 2. 改动清单

**后端**

| 文件 | 改动 |
|---|---|
| `app/services/branding_service.py`（新） | 解析口径（`resolveBranding` / `loadBranding`）+ 源码默认值 + 长度上限；**永不抛错**（DB 异常也回落默认） |
| `app/api/v1/branding.py`（新） | `GET /api/v1/branding`（**公开**，无鉴权、无改密守卫；`Cache-Control: no-store`） |
| `app/domain/schemas.py` | `BrandingRead`（CamelModel ⇒ JSON camelCase） |
| `scripts/seed_system_config.py` | `BRAND_CONFIG_SEEDS` + `seed_brand_config`（**value 不覆盖**，只刷 description） |
| `app/main.py` | 挂路由（公开）+ 启动链播种品牌键 |
| `app/tests/_testapp.py` | 挂路由（测试应用与生产应用同构） |
| `app/tests/integration/test_route_auth_guard.py` | **公开路由白名单登记** `("GET", "/api/v1/branding")` —— 漏登记该守卫会红（CI 集成分片红） |

**前端**

| 文件 | 改动 |
|---|---|
| `src/branding/defaults.ts`（新） | 兜底默认值 + `Branding` 类型 + `sanitizeBranding` |
| `src/api/branding.ts`（新） | `fetchBranding()`（`silentErrors`，失败静默回落） |
| `src/stores/brandingStore.ts`（新） | zustand store（初始值 = 兜底 ⇒ 首屏零闪烁） |
| `src/components/common/AppLayout.tsx` | 展开态取中文名/英文名、折叠态取缩写（不再有 `"TH"` 字面量） |
| `src/pages/LoginPage.tsx` | 标题插值 `t("auth.login.title", { brand })` |
| `src/App.tsx` | 启动时加载品牌 + 写 `document.title` |
| `src/i18n/{zh-CN,en-US}.ts` | `auth.login.title` 改 `{brand}` 插值；删除 `appLayout.brand` |
| `src/i18n/useTranslation.ts` | `locale` 类型从 `"zh-CN"` 修正为 `"zh-CN" \| "en-US"`（此前是**谎言**，导致调用方无法按语言分支） |

## 3. 关键设计取舍

- **为什么不走构建期环境变量**：改一次要重建 + 发版前端镜像，不是运维参数。
  走 `system_config` ⇒ admin 改一行即生效，不用发版。
- **为什么端点公开**：登录页在未登录时就要显示名称。只返回产品命名，不含业务数据。
  **写**入口仍是 admin-only 的 `PUT /api/v1/admin/system-config/{key}`。
- **为什么品牌种子是 insert-if-absent（与阈值种子相反）**：品牌是**内容**，
  admin 改过的值不能被发版重置（否则每次部署品牌名被打回原形）。
- **为什么 `BRAND_TITLE` 播种为 NULL**：语义是「未配置 ⇒ 跟随主名称」，
  改了主名标题自动跟着变。

## 4. 漂移守卫

| 守卫 | 位置 |
|---|---|
| 跨栈：后端 `DEFAULT_*` 与长度上限 == 前端 `defaults.ts` | `backend/app/tests/unit/test_branding_service.py` |
| 路由在 main.py 与 _testapp.py **两处都挂**且**都不鉴权**（查**真实 app 的依赖链**，不扫源码文本） | 同上 |
| 公开路由登记进 `PUBLIC_ROUTES` 白名单 | 同上 |
| 启动链播种品牌键 | 同上 |
| 种子 insert-if-absent（源码级断言） | 同上 |
| 前端不再写死品牌（源码级断言） | `frontend/src/tests/brandRenameGuard.test.ts`（已重写） |
| store 行为（初始值 / 成功 / 失败 / 脏值） | `frontend/src/tests/brandingStore.test.ts` |
| 侧边栏渲染取自配置 | `frontend/src/tests/brandingRender.test.tsx` |
| 端点公开 + 读 DB 值 + 标题回落链 | `backend/app/tests/integration/test_branding_api.py` |
| 种子不覆盖 admin 值 | `backend/app/tests/integration/test_seed_system_config.py` |

## 5. 已知遗留 / 不做的事

- **favicon 不参数化**：`index.html` 内联 data-URI SVG 的字母仍是静态资产，
  换品牌首字母变了要一并改（守卫会提醒）。
- 不做多租户品牌；仍是单租户全局品牌。
- `index.html` 的静态 `<title>` 保留为**JS 加载前的兜底**，运行时被
  `document.title` 覆盖。

## 6. CI 判据（2026-11-27 实测）

merge-gate `merge-gate / failure-set-diff` 的结论：

```
结论：不可合并（CI_SURFACE_CHANGED）
基线 run 45866117459（head f065d48d）：失败 122 条
PR    run 45922038120（head 381c1d71）：失败 122 条
净新增 0 条 / 净丢失 0 条
原因：PR 改动了 CI 相关面 ⇒ 两次 run 的失败集合不可比 ⇒ 对差不成立：
      ['backend/app/tests/_testapp.py', 'backend/scripts/seed_system_config.py']
      ⇒ 须人工确认；确知无影响时用 --allow-ci-change
```

- **失败集合两侧完全一致**（122 = 122，逐条 id 相同，净新增 0 / 净丢失 0）
  —— 独立复核过：按用例 id 取 PR 与基线**全分片并集**再对差，结果一致。
- ⚠️ 逐分片比对会**假报新增**：integration 分片是「排序下标 mod 4」，新增一个测试
  文件会让后面所有文件**换分片** ⇒ 必须按并集比（工具 `ci_judge.py::
  _unionUsableFailures` 就是这么做的）。
- 前端 CI ✅（typecheck + coverage 13m05s）；8 个后端分片全红，但**全部是既有红**
  （与基线同一批用例 id）。
- 品牌相关用例**全部通过**：`test_branding_service.py`（unit shard 3）、
  `test_branding_api.py` + `test_route_auth_guard.py`（integration shard 1）、
  `test_seed_system_config.py`（integration shard 3）。

## 7. 合并注意

本分支改动了 `backend/app/tests/_testapp.py` 与 `backend/scripts/seed_system_config.py`
（CI 表面文件）⇒ merge-gate 判 **CI_SURFACE_CHANGED** ⇒ 需人工裁决 / admin 绕过
（与 #178 / #183 / #184 同样处理）。

⚠️ 另注：`backend/app/tests/unit/test_no_sqlite_fallback.py` 守卫在 **main 上本来
就红**（6 个既有违规文件），本分支新增的测试文件一度被算进违规名单（docstring 里
提了那个被禁的库名全称）⇒ 已改写措辞，不扩白名单。
