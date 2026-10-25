# 变更：fix-login-401-white-screen（登录失败白屏 + 错误提示不可达）

- **日期**：2026-10-25
- **分支**：`fix/login-401-white-screen`（自 eb70f1bf），commit `0802894b`
- **状态**：dev 完成（36/36 受影响测试绿、全量净新增失败 0、tsc 干净）；待用户验证 + 部署

## 0. 用户报障

登录时用户名/密码输错 → 跳到 `auth/login` 路径，白屏无任何提示。

## 1. 根因（两个叠加）

| # | 位置 | 问题 |
|---|---|---|
| 1 | `client.ts` `normalizeError` | 401 时 `window.location.href = "/auth/login"` **硬跳**——App.tsx 路由表只有 `/login`，`/auth/login` 不存在；且无 `*` 兜底路由 → 白屏。硬跳卸载页面，LoginPage 的 `message.error` 永远不执行 |
| 2 | `LoginPage.tsx` catch | 读 `e.response.data.detail.code`——拦截器 reject 的是 `normalizeError` 产出的 **AppError（无 `.response`）**；且后端登录失败统一 401 防枚举（`AuthFailedError`），body 是 `error` 文本**没有 `detail.code`**。即使不白屏，也永远弹「网络异常」而非「用户名或密码错误」 |

同族潜伏：`RequireAuth` mustChangePassword 白名单/重定向目标写的 `/auth/change-password` 也是不存在的路由（真实路由 `/change-password`）。

## 2. 修复

1. `normalizeError` 改**纯归一化**（删 401 硬跳块）：401 清理归响应拦截器（原本就有：清 localStorage + authStore + clearLastSessionIds + notifyUserSwitch），重定向归 RequireAuth 软跳转（带 `state.from`），登录提示归 LoginPage toast。
2. `LoginPage` 按 **`AppError.status` 分流**：401 → invalidCredentials（防枚举 ⇒ 登录接口上 401 即账号或密码错误）；429 → rateLimited；其余 → networkError。
3. `RequireAuth` 白名单 → `["/change-password", "/login"]`，重定向目标 → `/change-password`。
4. `App.tsx` 加 `<Route path="*">` → `<Navigate to="/">` 兜底（未知路径不再白屏；未登录经 RequireAuth 落 `/login`）。

## 3. 测试（假绿修复是本变更的一半）

- `client-error.test.ts` / `client-interceptor.test.ts` **原是镜像复刻实现**（本地拷贝一份逻辑来测，真实现坏了照样绿）→ 重写为 import 真实现；拦截器用真 `createHttpClient` + axios adapter 桩驱动完整链路（断言 401 清 store/localStorage、reject AppError、**不发生 location 赋值**）。
- 两个 `LoginPage` 测试文件 mock 的都是生产链路不存在的形状 → 重写为拦截器真实 reject 的 AppError 形状；删除不可能出现的 `AUTH_EXPIRED`/`detail.code` 用例（那是 dependencies.py 的 token 过期形状，登录接口不产生）。
- 新增：App 未知路径兜底（authed → `/chat`、未登录 → `/login`）；RequireAuth 真实路由白名单语义。
- 净新增失败 0：全量 9 红 = client.test.ts 3（干净 HEAD 复跑确认既有）+ kpi/ontology 5（记录在案基线红）。

## 4. 已知边界 / 后续

- **ChangePasswordPage 同族问题未修**（改旧密码错 → 401 被拦截器清 token 踢回 /login，提示也读不到）：修它需要拦截器区分「登录接口 401」与「会话过期 401」，超出本期，单独开变更。
- `state.from` 只记 pathname 不带 search（`/data-quality?tab=reports` 重登录后落默认 tab）——既有行为，未动。
- 部署：纯前端改动；生产走前端镜像 rebuild（`--build-arg VITE_API_BASE_URL=` 必须显式空，见 PROD-FRONTEND-REBUILD 同款流程）。
