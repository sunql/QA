# 品牌名参数化（可配置的系统标题 / 名称）

- **日期**：2026-11-27
- **分支**：`feat/configurable-brand`
- **基线**：`origin/main` = `f065d48d`
- **诉求**：系统标题 / 名称目前写死（「泰鸿智脑」/「TH」），要改成可以通过参数配置。

## 1. 现状（写死点盘点）

| # | 位置 | 内容 | 形态 |
|---|---|---|---|
| ① | `frontend/index.html` `<title>` | 泰鸿智脑 | 静态 HTML（JS 加载前的兜底） |
| ② | `frontend/src/i18n/zh-CN.ts` `appLayout.brand` | 泰鸿智脑 | i18n 字面量 |
| ③ | `frontend/src/i18n/en-US.ts` `appLayout.brand` | Taihong ZhiNao | i18n 字面量 |
| ④ | `frontend/src/components/common/AppLayout.tsx:135` | `collapsed ? "TH" : t("appLayout.brand")` | 组件内字面量 |
| ⑤ | `frontend/src/i18n/{zh-CN,en-US}.ts` `auth.login.title` | 登录 · 泰鸿智脑 / Sign in to Taihong ZhiNao | i18n 字面量 |

现状守卫：`frontend/src/tests/brandRenameGuard.test.ts` —— 它断言「改名必须四处同时改」。
**该守卫的前提（品牌名是字面量）本次被推翻**，必须同步重写，否则守卫会红或变成假绿。

## 2. 方案

**SSOT = `system_config` 表的 4 行**（admin「系统配置」页可改，审计留痕）：

| key | 含义 | 默认值 |
|---|---|---|
| `BRAND_NAME` | 品牌主名称（中文） | 泰鸿智脑 |
| `BRAND_NAME_EN` | 品牌英文名 | Taihong ZhiNao |
| `BRAND_SHORT_NAME` | 侧边栏折叠态缩写 | TH |
| `BRAND_TITLE` | 浏览器标签页标题 | 泰鸿智脑 |

**分层**（三层，各层职责单一）：

```
system_config（运行时 SSOT，admin 可改）
   ↓ 缺行/空值时回落
后端源码默认值（branding_service.DEFAULT_*）
   ↓ 接口失败时回落
前端兜底默认值（frontend/src/branding/defaults.ts）
```

- `BRAND_TITLE` 未配置时回落 `BRAND_NAME`（不是回落源码默认）⇒ 改主名时标题自动跟随。
- 后端**公开**只读端点 `GET /api/v1/branding`（无鉴权）—— 登录页也要显示品牌，
  公开端点只暴露产品名，不含任何业务数据。
- 启动链播种 4 行（**insert-if-absent**，绝不覆盖 admin 改过的值）。
  与 `seed_system_config.py` 的阈值种子（DO UPDATE 语义）分开：**品牌是内容不是阈值**，
  每次发版重置品牌是不可接受的。

**前端**：

- `frontend/src/branding/defaults.ts` —— 默认常量 + `Branding` 类型。
- `frontend/src/api/branding.ts` —— `fetchBranding()`。
- `frontend/src/stores/brandingStore.ts` —— zustand store，初始值 = 前端兜底默认值
  （**首屏零闪烁**：fetch 前后显示一致，只有 DB 里改过才会变）。
- `AppLayout.tsx` —— 展开态取 `brandName`/`brandNameEn`（按当前语言），折叠态取 `brandShortName`。
- `App.tsx` —— 挂载时加载品牌并写 `document.title`。
- `LoginPage.tsx` —— 标题插值 `t("auth.login.title", { brand })`。

## 3. 为什么不选另外两种做法

- **Vite 构建期环境变量**（`VITE_BRAND_NAME`）：改一次要重新构建 + 发版前端镜像，
  不是「参数配置」的运维体验；且前端镜像在生产是固定 tag。
- **只做构建期变量**：同上，且 admin 无从改。

## 4. 漂移守卫（本次的核心工作量在守卫，不在实现）

品牌默认值有 3 份拷贝（后端源码 / 前端常量 / index.html 静态兜底），必须钉住：

| 守卫 | 位置 | 断言 |
|---|---|---|
| 跨栈漂移 | `backend/app/tests/unit/test_branding_service.py` | 后端 `DEFAULT_*` == 前端 `defaults.ts` 常量（读源文件比对） |
| 前后端兜底一致 | `frontend/src/tests/brandRenameGuard.test.ts` | `index.html` `<title>` == `DEFAULT_BRAND_TITLE` |
| 不再硬编码 | 同上 | `AppLayout.tsx` 不含 `"TH"` / `泰鸿智脑` 字面量，且用 `useBrandingStore` |
| 插值而非字面量 | 同上 | `auth.login.title` 用 `{brand}` 占位符，不含字面品牌名 |
| 种子覆盖 | `backend/app/tests/unit/test_branding_service.py` | 4 个品牌键都在种子列表里，值 == 默认值 |
| 公开端点 | `backend/app/tests/integration/test_branding_api.py` | `GET /api/v1/branding` 无需登录即可拿到 4 个字段 |

## 5. 不做的事

- **favicon 不参数化**：`index.html` 内联 data-URI SVG 的字母是二进制资产，
  改品牌后字母可能过时（保留现状，守卫里保留 favicon 断言）。
- **不做多租户品牌**：仍是单租户全局品牌。
- **`appLayout.brand` 的 i18n 键已删除**（实施时改了主意）：品牌名是**配置项**不是
  **翻译项**，留在字典里会诱导下一个人把它当翻译文案继续硬编码。删掉后
  `useTranslation` 的类型推导（`NestedKeyOf<typeof zhCN>`）会在有人再写
  `t("appLayout.brand")` 时直接报 TS 错 —— 比留一个没人用的键更安全。

## 6. 验收

- [ ] `GET /api/v1/branding`（无 token）返回 4 字段，默认值 = 泰鸿智脑 / Taihong ZhiNao / TH / 泰鸿智脑
- [ ] admin 改 `BRAND_NAME` ⇒ 侧边栏 / 登录页 / 标签页标题随之变化（无需发版）
- [ ] 改 `BRAND_SHORT_NAME` ⇒ 侧边栏折叠态随之变化
- [ ] 后端/前端单测 + 集成测试全绿；`brandRenameGuard` 重写后全绿
- [ ] `npm run build` 通过（tsc 无错）
