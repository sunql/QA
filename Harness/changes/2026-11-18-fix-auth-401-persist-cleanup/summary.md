# 变更：fix-auth-401-persist-cleanup

- **日期**：2026-11-18
- **作者**：Claude / 启琳
- **Phase**：生产日志取证（`JWT 已过期` + 401）⇒ 前端 401 清理路径加固
- **状态**：done（前端纯改动，零迁移、零后端、零生产写）
- **迁移版本**：无
- **分支**：`fix/auth-401-persist-cleanup`（base `origin/main` = `4cbea5eb`）
- **遗留项**：`tech-debt.md`（本批次评审挂账的 6 条）

---

## 1. 需求（来源：生产日志）

生产容器日志出现：

```
WARNING:app.dependencies:JWT 已过期: Signature has expired
INFO:     172.18.0.9:52878 - "GET /api/v1/messages/unread-count HTTP/1.1" 401 Unauthorized
```

用户问：这是什么问题？

## 2. 取证结论（代码 + 生产实测，非推测）

### 2.1 这条日志本身：**正常行为，不是故障**

| 事实 | 出处 |
|---|---|
| 日志点：`ExpiredSignatureError` ⇒ 抛 401 `AUTH_EXPIRED`「登录已过期，请重新登录」 | `backend/app/dependencies.py:209-212` |
| 访问令牌 TTL = `JWT_TTL_SECONDS`，**默认 86400 秒 = 24 小时** | `backend/app/services/jwt_codec.py:28`、`backend/app/config.py:229` |
| 生产**未覆盖**该变量 ⇒ 生效值就是 24 小时 | 生产 `docker exec qa-backend printenv JWT_TTL_SECONDS` 未设置；`docker/.env` 只含 `JWT_SECRET` |
| **没有刷新令牌端点**（只有 `/login` `/me` `/logout` `/password-policy`）⇒ 令牌到期只能重新登录 | `backend/app/api/v1/auth.py:34,43,51,70` |
| 前端 `MessageBell` 每 **30 秒**轮询 `/messages/unread-count` ⇒ 到期那一刻必然撞上 | `frontend/src/components/MessageBell.tsx:13,31` |
| 401 ⇒ 前端清登录态 ⇒ `RequireAuth` 软跳 `/login`（设计内路径） | `frontend/src/api/client.ts:154-165`、`frontend/src/components/common/RequireAuth.tsx:27-29` |

生产实测佐证：近 24 小时 `JWT 已过期` **只出现 1 次**（`2026-11-18T02:08`）⇒ 一次性事件，非循环。

**两个必须排除的误判**（都不是）：
- 不是 `SECRET_KEY` 变更 —— 那会是 `InvalidTokenError` 分支的 `Signature verification failed`（`dependencies.py:213`），与本条互斥 ⇒ 上次发版没动密钥。
- 不是会话被吊销 —— 吊销走 `dependencies.py:236-237` 的 `MSG_TOKEN_REVOKED`，文案不同。

### 2.2 顺带查出的**真问题**（本批要修的）

`frontend/src/api/client.ts` 的 401 分支里有一行：

```ts
localStorage.removeItem("token");
```

而**全仓从来没有代码写过 `localStorage["token"]`** —— 唯一写 token 的是 zustand persist，
键名 `qa-system-auth`（`frontend/src/stores/authStore.ts:73`）⇒ **那行删的是一个不存在的键 = 死代码**。

更要命的是配套测试 `frontend/src/api/__tests__/client-interceptor.test.ts` **也用这个假键造数据并断言**
「token 已从 localStorage 移除」⇒ 这条断言**证明不了任何事**：真正该保证的
「persist 键里的过期 token 被清掉」**从未被断言**（测试与生产代码犯的是同一个错 ⇒ 典型假绿）。

### 2.3 一处需要更正的判断（自查后修正）

初判曾认为「过期 token 会留在持久层、刷新页面后复活」。**实测证伪**：zustand persist 会给
`api.setState` 打补丁，内部同步 `setItem()` 把 `qa-system-auth` 整体重写（`token: null`）
⇒ **持久层清理一直是对的**，没有「复活」问题。
真实问题因此降级为：**死代码 + 一条没抓到真目标的守卫**。

### 2.4 评审补强：首版守卫有 4 处可绕过（reviewer 实测，已全部封堵）

首版守卫（AST 版）虽然比正则版可靠，但两个评审 agent 各自独立实测出**可绕过写法**：

| # | 绕过写法 | 为什么漏 | 封法 |
|---|---|---|---|
| ① | `window.localStorage.removeItem("token")` | 只认**裸标识符** `localStorage`；而 `window.localStorage` 恰是**本仓主流写法**（`tests/setup.ts:130`、`stores/authStore.ts:83` 等） | `isLocalStorageTarget` 同时认 `window.` / `globalThis.` / `self.` 前缀 |
| ② | `localStorage.token`（点号） | 只查 `ElementAccessExpression`（下标），点号形式完全不查（用例名却写着「点号或下标」） | 点号 + 下标都查 |
| ③ | 把 `setState({token:null})` 挪进 **fulfilled 拦截器** | 正向断言只验「文件里存在这个调用」，不验它在不在 401 分支内 ⇒ 每次成功响应都登出也照样全绿 | 正向断言**锚定在 401 判定分支之内**（`if` 的条件里必须有 `status === 401`） |
| ④ | 把 AST walker 的回调「顺手改成简洁箭头」 | `forEachChild` 的回调若返回非 undefined，TS 会把它当「停止遍历兄弟节点」的信号 ⇒ AST 被截断成 4 个节点 ⇒ 整套断言退化成**空集恒真**（评审实测：709 节点 → 4 节点，违规代码仍全绿） | 改用内建 `ts.forEachChild` 且回调类型标注 `(n: ts.Node) => void`（**TypeScript 层面禁止**返回数字）+ 采集器自检用例 |

### 2.5 评审补强：persist 写盘失败会让清理中途抛错

zustand persist 的 `setItem` **没有 try/catch**（`middleware.mjs:270-273`）⇒ 浏览器配额满 / 隐私模式下
`QuotaExceededError` **同步穿出** `useAuthStore.setState(...)`，后果三条：

1. 内存态已清（`savedSetState` 先执行），但 `qa-system-auth` 里仍是旧 token ⇒ **过期 token 复活**（§2.3 那句保证的唯一反例）；
2. `clearLastSessionIds()` / `notifyUserSwitch()` 被跳过 ⇒ 前一个人的聊天恢复指针与内存消息残留；
3. 异常止步于 `setState`，走不到 `normalizeError` ⇒ 调用方拿到 DOMException 而非 AppError，`LoginPage.tsx:42` / `ChangePasswordPage.tsx:42` 的 `status === 401` 分流失效。

同仓其它持久化 helper 都显式吞配额错（`stores/persistChatUiState.ts:62-64,84-86,126-128`），auth 这条是唯一没兜的。

## 3. 修法

| 文件 | 改动 |
|---|---|
| `frontend/src/api/client.ts` | 删掉死代码 `localStorage.removeItem("token")`；401 分支的 `setState` 包 try/catch，落盘失败时降级调 `clearPersistedAuth()`；注释改写并指明守卫文件 |
| `frontend/src/stores/authStore.ts` | 新增导出 `clearPersistedAuth()`：删除 persist 自己的键（键名从 store 自身取，不复写字面量）—— **持久层的所有权归 store**，client.ts 不直接碰 localStorage |
| `frontend/src/api/__tests__/client-interceptor.test.ts` | 假键 `"token"` 全部换成**真实 persist 键**（键名从 `useAuthStore.persist.getOptions().name` 取，不复写字面量）；新增「持久层里的过期 token 被清掉」断言（含前置断言防恒真）+ 「persist 键名 = `qa-system-auth`」防空扫描断言；403 / 改密 401 两处改为断言**持久层不变**；**新增**「persist 写盘失败 ⇒ 降级删键且异常不穿出」用例 |
| `frontend/src/tests/authTokenCleanupGuard.test.ts` | 源码级守卫（5 例），**TypeScript AST** 判定 + §2.4 的四道封堵 + 采集器自检 |
| `Harness/changes/2026-11-18-fix-auth-401-persist-cleanup/tech-debt.md` | 新建：本批次评审挂账的 6 条遗留项 |

守卫判定口径（5 条）：
1. `client.ts` 存在且非空（防空扫描假绿）；
2. **采集器自检**：能采到 401 判定那层（walker 瞎了 ⇒ 下面的「必须为空」全是恒真）；
3. `client.ts` **不得调用 localStorage 的任何方法**（含 `window.` / `globalThis.` / `self.` 前缀）；
4. `client.ts` **不得出现 localStorage 的键访问**（下标或点号，含前缀）；
5. **正向**：`client.ts` 在 **401 分支内**仍然执行 `useAuthStore.setState({ token: null, ... })`。

> 为什么用 AST 而不是正则扫源码：正则会把**注释里**的示例代码也算进去（改注释就误报），
> 也会漏掉变量形式的键；AST 只看真实语法节点。（本批实测踩过：注释里写 `localStorage.removeItem("token")`
> 被正则守卫误报 ⇒ 改为 AST。）
>
> 为什么第 3 条是「任何方法」而不是「字面量键」：兜底（写盘失败时删 persist 键）也要碰 localStorage，
> 而它**应该**待在 store 那边（持久层的所有者）⇒ client.ts 一律不碰，规则最简单也最难绕过。

## 4. 测试（TDD + 反证）

| 步骤 | 结果 |
|---|---|
| 基线（改动前） | `client-interceptor.test.ts` 8 passed |
| RED（新守卫，正则版） | 2 failed（命中死代码）⇒ 失败原因正确 |
| GREEN（删死代码后，正则版） | 仍红 —— **发现守卫被自己写的注释文本命中** ⇒ 改用 AST 判定 |
| GREEN（AST 版） | 12 passed |
| **反证（防空绿）** | 把死代码临时塞回 `client.ts` ⇒ 守卫「不得对 localStorage 使用字面量键」转红 ⇒ 守卫非空集；随即恢复并复跑 GREEN |
| 改写后的拦截器测试 | 改动前就全绿（9 例）⇒ 实证「持久层清理一直是对的」，本批改的是**断言的靶子**与**死代码」 |
| **评审补强后 RED→GREEN** | 新守卫 5 例 + 降级兜底用例 1 例 ⇒ 全绿（15 passed / 2 files） |
| **变异反证 ×6（每条都必须转红）** | ① 裸 `localStorage.removeItem("token")` → 1 failed；② `window.localStorage.setItem("token", x)` → 1 failed；③ `localStorage.token` 点号 → 1 failed；④ `window.localStorage["token"]` → 1 failed；⑤ `setState` 挪进 fulfilled 拦截器 → 1 failed；⑥ 采集器被改成简洁箭头（AST 截断）→ **2 failed**；恢复后 5 passed |
| 定向回归 | `src/api` + 守卫 + authStore + AppLayout + RequireAuth：5 files / 44 tests passed |
| 全量 + 覆盖率闸 | **240 files / 2439 tests passed，EXIT=0**；All files 84.25 \| 84.22 \| **82.54** \| 84.25（stmts\|branch\|**funcs**\|lines）四项均 ≥ 80 |
| 类型检查 | `npx tsc -b` **EXIT=0** |
| 本地部署验证 | 新镜像 `qa-system-frontend:latest`：`removeItem("token")` 出现次数 0（旧回滚镜像为 1）、含 `qa-system-auth` 键、无绝对 URL 烤入；served sha256 == image sha256；`/` 200、`/api/v1/health` 200、无 token 的 unread-count 403；后端容器未动 |

> 变异反证是本守卫的**防空扫描**证据：每一种绕过写法都真的能让守卫转红 ⇒ 守卫不是空集恒真。
> 复现方式见 PR 描述。

## 5. 明确未做的事

- ❌ **不改** `JWT_TTL_SECONDS`（生产侧已由用户自行完成配置，本批零生产写）。
- ❌ **不做** refresh token 机制（工程量大，收益仅是自动化「延长会话」，见下）。
- ❌ **不改**后端 `JWT 已过期` 的 WARNING 日志级别（属后端改动 ⇒ 需另起后端镜像与生产发版，
  不在本「纯前端」批次内；如需降噪可另开 PR）。
- ❌ **不动** `client-error.test.ts` 里 `normalizeError` 的 `localStorage["token"]` 断言 ——
  那条用例测的是 `normalizeError` 本身不碰存储层，语义独立、无误（登记为 TD-6，可选后续清理）。
- ❌ **不处理** 401 清理覆盖面（多个独立 axios 实例与 SSE 路径不走 `httpClient`）——
  影响面跨多个 api 模块，不宜塞进本批次（登记为 TD-1，P2）。
- ❌ **不改** 改密接口 401 豁免的匹配口径（按 URL ⇒ 连带吞掉「令牌已过期」）—— 登记为 TD-2，P2。
- ❌ **不改** 「记住我」的存储语义（token 恒落 localStorage）—— 属产品决策，登记为 TD-3，P3。

> 其余遗留项（多标签页回写 TD-4、登录 401 误清会话指针 TD-5）同样登记在 `tech-debt.md`。

## 6. 关联

- 生产侧会话时长配置：`JWT_TTL_SECONDS`（未设置 ⇒ 默认 24 小时；`user_sessions.expires_at` 用同一 TTL）。
- 后端对应位置：`backend/app/dependencies.py`（401 路径）、`backend/app/services/jwt_codec.py`（签发/校验）、
  `backend/app/config.py`（TTL 配置）。
- 遗留项登记：`tech-debt.md`。
- 相关记忆：`qa-system-two-json-safe-mappings`（同类「同名不同语义」陷阱）、`qa-system-frontend-suite-baseline`、
  `qa-system-jwt-expiry-benign`（令牌到期取证）、`qa-system-source-guard-ast-not-regex`（源码级守卫为什么用 AST）。
