# 变更：fix-login-401-white-screen（登录失败白屏 + 错误提示不可达）

- **日期**：2026-10-25
- **分支**：`fix/login-401-white-screen`（自 eb70f1bf），已合 main `ad36d27e`（2026-10-25）；生产已上线验证（镜像包路线）
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

## 4. 后续批：改密 401 同族修复（e85ff666）

- 根因：改旧密码错 → 后端 `AuthFailedError` → 401，拦截器无条件清 token/store
  把正在改密的用户踢回 `/login`；ChangePasswordPage 读 `e.response.data.error`
  （AppError 上不存在）永远弹「网络异常」。
- 拦截器：401 清登录态前按 URL 豁免凭证校验接口（`CREDENTIAL_CHECK_URLS =
  ["/auth/me/password"]`）——该 401 = 旧密码错、token 仍有效，页面自己提示。
- ChangePasswordPage：catch 按 AppError.status 分流（401=当前密码不正确 /
  422=弱密码双保险 / 其余=网络异常），与 LoginPage 同口径。
- 测试：拦截器加豁免用例；ChangePasswordPage 测试改断言 mock message.error
  ——**antd message 渲染在 document.body 的 portal，跨测试残留会让 DOM 文本
  断言不稳定**（「Found multiple elements」/「Unable to find」随机切换），toast
  类断言一律走 message mock。
- 验证：受影响 31/31 绿、全量净新增失败 0（同 9 条既有红）、tsc 干净。

## 5. 已知边界 / 后续

- `state.from` 只记 pathname 不带 search（`/data-quality?tab=reports` 重登录后落默认 tab）——既有行为，未动。
- 生产部署：纯前端改动，走前端镜像 rebuild（`/tmp/PROD-FRONTEND-REBUILD-1025.md`）：
  tarball `frontend-src-e85ff666.tar.gz`（sha256 `d2874c22…`，git archive 自 e85ff666）。
- 本分支尚未合 main；生产验证通过后合并。
