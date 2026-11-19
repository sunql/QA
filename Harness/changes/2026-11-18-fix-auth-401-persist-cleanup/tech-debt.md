# 401 清理路径加固 · 遗留项登记（tech-debt）

> 建档：2026-11-18（fix/auth-401-persist-cleanup 批次的 code-reviewer + security-reviewer 评审）
> 来源：本批次两个评审 agent 的 MEDIUM / LOW 结论
> **规则**：本文件是本批次遗留项的 SSOT。单项修复完成后在此标记并注明 commit，**不删条目**（保留决策轨迹）。
> **本批次边界**：纯前端、零后端、零 DDL、零生产写。以下条目均**未**在本批次处理。

---

## 优先级说明

| 级别 | 含义 |
|---|---|
| P1 | 正在遮蔽真实问题 —— 应尽快排 |
| P2 | 真实缺陷或契约不一致，但不阻塞当前工作 |
| P3 | 卫生问题（死代码、注释漂移、守卫弱化） |

---

## TD-1（P2）· 401 清理只覆盖 `httpClient` 一条链路

**状态**：待办（登记于 2026-11-18；**非本批次引入**，是既存缺口）

### 现象

`frontend/src/api/client.ts` 的 401 分支是**唯一**的登录态清理点。不经过该实例的请求收到 401 时，
内存态与持久层（`qa-system-auth` 键）里的过期 token **都不清**：

| 绕过路径 | 位置 |
|---|---|
| SSE `/chat/stream` | `frontend/src/api/chat.ts:296` |
| 多步 resume | `frontend/src/api/chat.ts:503` |
| wikiChat / research / document | `frontend/src/api/wikiChat.ts:38`、`research.ts:151`、`document.ts:181` |
| wiki links 管理 | `frontend/src/api/adminWikiLinks.ts:44,53,65,76,92` |
| 路由指标 | `frontend/src/pages/RoutingMetricsPage.tsx:27` |
| 独立 axios 实例（只注入 Authorization、无任何 401 处理） | `frontend/src/api/datasource.ts:80-94`、`frontend/src/api/localImport.ts:27-40` |

### 失败场景

令牌 TTL 86400s 到期，用户停留在聊天页 ⇒ `/chat/stream` 返回 401 ⇒ `chat.ts:302` 抛
`请求失败 (HTTP 401)`（非 AppError）⇒ 登录态不清 ⇒ `RequireAuth`（`frontend/src/components/common/RequireAuth.tsx:31-33`）
因 `token` 仍非空而放行 ⇒ 过期 JWT 继续留在 `qa-system-auth` 里，且后续每个请求都带着它，
用户整段会话卡在报错态，直到刷新页面触发 `/auth/me` 401 才被清。

### 建议修法

把这批路径统一收口到 `httpClient`；或在 `frontend/src/api/authHeaders.ts:13-25` 旁加一个共享的
401 处理器（清 store + 清持久层 + 软跳转），各路径复用。**影响面跨多个 api 模块，
不宜塞进「死代码清理」这种纯前端小批次** ⇒ 单独立项。

---

## TD-2（P2）· 改密接口的 401 豁免按 URL 匹配，连带吞掉「令牌已过期」

**状态**：待办（登记于 2026-11-18；**非本批次引入**）

### 现象

`frontend/src/api/client.ts` 的豁免是 URL 子串匹配（`CREDENTIAL_CHECK_URLS = ["/auth/me/password"]`）
⇒ 该 URL 上**任何** 401 都不清登录态，包括令牌本身已过期 / 被吊销的情况。

### 后端其实能区分

| 情形 | 后端 | 前端可见 |
|---|---|---|
| 令牌过期 / 吊销 | `backend/app/dependencies.py:31,227,252` ⇒ HTTPException，`detail = {"code": "AUTH_EXPIRED", ...}` | `code === "AUTH_EXPIRED"` |
| 旧密码错 | `backend/app/services/auth_service.py:292-293` ⇒ DomainError，`detail` 为 null | `code === "UNKNOWN"` |

### 失败场景

用户在改密页上令牌恰好到期 ⇒ 提交 ⇒ 401 ⇒ 豁免生效 ⇒ 登录态不清（过期 token 继续留在
localStorage），且 `ChangePasswordPage.tsx:42` 把「会话已过期」显示成「旧密码错误」
⇒ 用户反复重试旧密码。

### 建议修法

豁免条件从「URL 匹配」收窄为「URL 匹配 **且** `code !== "AUTH_EXPIRED" && code !== "AUTH_INVALID"`」。

---

## TD-3（P3）· 「记住我」与实际存储位置不符：token 恒落 localStorage

**状态**：待办（登记于 2026-11-18；**非本批次引入**，是既有设计现状）

### 现象

`frontend/src/stores/authStore.ts:74-91` 的 persist **无条件**用 localStorage；
`rememberMe` 字段只被持久化、**从不参与存储选择**；而 `frontend/src/pages/LoginPage.tsx:65-66`
有「记住我」复选框。

⇒ 即便用户不勾选，JWT 也明文落在 localStorage（任意同源脚本可读、跨浏览器重启存活）。

### 为什么登记

本批次把「持久层清理」的唯一责任押在 persist 上，等于确认「token 就在 localStorage」是设计现状。
值得显式承认，而不是只强调「被清掉了」。**产品语义问题**（要不要真的做 session/local 双存储），
需产品决策，不宜由前端单方面改。

---

## TD-4（P3）· 多标签页：被清掉的过期 token 可能被另一个标签页写回

**状态**：待办（登记于 2026-11-18；**非本批次引入**）

### 现象

键 `qa-system-auth` 全标签页共享；persist 在**任意** store 写入时重写整个 partialize 快照
（zustand `middleware.mjs:465-471`）。另一个标签页只要发生一次 store 写入
（如 `authStore.ts:61-63` 的 `fetchMe` → `set({user})`），就会把它内存里的 token 一并写回 localStorage。

⇒ 变更记录里「刷新页面不会把过期 token 恢复回来」这句**仅对「观察到 401 的那个标签页」成立**。

### 建议修法

（可选）用 `storage` 事件监听做跨标签页失效传播；或把 token 移出 persist 走 sessionStorage。

---

## TD-5（P3）· `/auth/login` 的 401 也走清理分支

**状态**：待办（登记于 2026-11-18；**非本批次引入**）

### 现象

`client.ts` 的 401 分支不区分「未登录场景下的登录失败」与「已登录会话的令牌失效」
⇒ 一次密码输错的登录尝试就会触发 `clearLastSessionIds()` + `notifyUserSwitch()`
⇒ 共享终端上前一位用户的聊天恢复指针与内存消息被抹掉。

**无越权风险**（服务端已拒绝），仅数据可用性。建议：登录请求不触发会话清理。

---

## TD-6（P3）· `client-error.test.ts` 里仍留一处同类假键夹具

**状态**：待办（登记于 2026-11-18；**本批次有意不动**）

### 现象

`frontend/src/api/__tests__/client-error.test.ts:47,179` 仍用 `localStorage.setItem("token", ...)` /
`getItem("token")` 造数与断言。

### 为什么不动

该用例测的是 `normalizeError` 本身**不碰存储层**（语义独立），且断言方向是「没动」⇒
用哪个键都不影响结论。本批次的清理目标是「断言必须对准真实持久层」；这条不属于该类。

但从「全仓再也没有 token 假键」这个不变式看，留一处会让下次有人再犯同样的错时更难发现。
**可选后续清理**（改成真实 persist 键或直接删掉该夹具）。

---

## 相关

- 本批次变更记录：`summary.md`
- 相关记忆：`qa-system-jwt-expiry-benign`（令牌到期的取证与 TTL 口径）、
  `qa-system-source-guard-ast-not-regex`（源码级守卫为什么用 AST 而非正则）
