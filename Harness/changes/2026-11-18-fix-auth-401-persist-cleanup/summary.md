# 变更：fix-auth-401-persist-cleanup

- **日期**：2026-11-18
- **作者**：Claude / 启琳
- **Phase**：生产日志取证（`JWT 已过期` + 401）⇒ 前端 401 清理路径加固
- **状态**：done（前端纯改动，零迁移、零后端、零生产写）
- **迁移版本**：无
- **分支**：`fix/auth-401-persist-cleanup`（base `origin/main` = `4cbea5eb`）

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

## 3. 修法

| 文件 | 改动 |
|---|---|
| `frontend/src/api/client.ts` | 删掉死代码 `localStorage.removeItem("token")`；注释改写为「持久层清理由 zustand persist 落盘负责」并指明守卫文件 |
| `frontend/src/api/__tests__/client-interceptor.test.ts` | 假键 `"token"` 全部换成**真实 persist 键**（键名从 `useAuthStore.persist.getOptions().name` 取，不复写字面量）；新增「持久层里的过期 token 被清掉」断言（含前置断言防恒真）+ 「persist 键名 = `qa-system-auth`」防空扫描断言；403 / 改密 401 两处改为断言**持久层不变** |
| `frontend/src/tests/authTokenCleanupGuard.test.ts` | 新建源码级守卫（4 例），用 **TypeScript AST** 判定（注释免疫、判定精确） |

守卫判定口径：
1. `client.ts` 不得对 `localStorage` 使用**任何字面量键**（`removeItem`/`getItem`/`setItem`）；
2. `client.ts` 不得出现裸 `"token"` 键的下标访问；
3. **正向**：`client.ts` 必须仍然执行 `useAuthStore.setState({ token: null, ... })` —— 防止把死代码删成「什么都不清」；
4. 防空扫描假绿：断言 `client.ts` 存在且非空。

> 为什么用 AST 而不是正则扫源码：正则会把**注释里**的示例代码也算进去（改注释就误报），
> 也会漏掉变量形式的键；AST 只看真实语法节点。（本批实测踩过：注释里写 `localStorage.removeItem("token")`
> 被正则守卫误报 ⇒ 改为 AST。）

## 4. 测试（TDD + 反证）

| 步骤 | 结果 |
|---|---|
| 基线（改动前） | `client-interceptor.test.ts` 8 passed |
| RED（新守卫，正则版） | 2 failed（命中死代码）⇒ 失败原因正确 |
| GREEN（删死代码后，正则版） | 仍红 —— **发现守卫被自己写的注释文本命中** ⇒ 改用 AST 判定 |
| GREEN（AST 版） | 12 passed |
| **反证（防空绿）** | 把死代码临时塞回 `client.ts` ⇒ 守卫「不得对 localStorage 使用字面量键」转红（1 failed）⇒ 守卫非空集；随即恢复并复跑 GREEN |
| 改写后的拦截器测试 | 改动前就全绿（9 例）⇒ 实证「持久层清理一直是对的」，本批改的是**断言的靶子**与**死代码** |
| 定向回归 | `src/api` + 守卫 + authStore + AppLayout + RequireAuth：5 files / 44 tests passed |
| 全量 + 覆盖率闸 | **240 files / 2437 tests passed，EXIT=0**；All files 84.25 \| 84.25 \| **82.53** \| 84.25（stmts\|branch\|**funcs**\|lines）四项均 ≥ 80 |
| 类型检查 | `npx tsc -b` **EXIT=0** |

## 5. 明确未做的事

- ❌ **不改** `JWT_TTL_SECONDS`（生产侧已由用户自行完成配置，本批零生产写）。
- ❌ **不做** refresh token 机制（工程量大，收益仅是自动化「延长会话」，见下）。
- ❌ **不改**后端 `JWT 已过期` 的 WARNING 日志级别（属后端改动 ⇒ 需另起后端镜像与生产发版，
  不在本「纯前端」批次内；如需降噪可另开 PR）。
- ❌ **不动** `client-error.test.ts` 里 `normalizeError` 的 `localStorage["token"]` 断言 ——
  那条用例测的是 `normalizeError` 本身不碰存储层，语义独立、无误。

## 6. 关联

- 生产侧会话时长配置：`JWT_TTL_SECONDS`（未设置 ⇒ 默认 24 小时；`user_sessions.expires_at` 用同一 TTL）。
- 后端对应位置：`backend/app/dependencies.py`（401 路径）、`backend/app/services/jwt_codec.py`（签发/校验）、
  `backend/app/config.py`（TTL 配置）。
- 相关记忆：`qa-system-two-json-safe-mappings`（同类「同名不同语义」陷阱）、`qa-system-frontend-suite-baseline`。
