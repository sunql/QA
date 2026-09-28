# 变更：fix-chat-disconnect-persistence

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：评估剩余项批次 **Phase B**（H4；同批 M4 / M5+M8 / H7 / M9 / M10）
- **状态**：done
- **关联变更**：同批 [fix-llm-transient-retry](../fix-llm-transient-retry/summary.md)、[chore-l3-deadcode-and-prior-cte-contract](../chore-l3-deadcode-and-prior-cte-contract/summary.md)、[fix-embedding-provider-type-guard](../fix-embedding-provider-type-guard/summary.md)、[fix-milvus-list-all-pagination](../fix-milvus-list-all-pagination/summary.md)、[test-kpi-match-cache-ordering](../test-kpi-match-cache-ordering/summary.md)；前批 [../fix-chat-retry-failure-details/summary.md](../fix-chat-retry-failure-details/summary.md)（M7 二次失败用量，与本项同属「失败路径的计量诚实性」）
- **迁移版本**：`0085_session_message_interrupted`（`down_revision = 0084_mcp_call_log`）
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.2 **H4** + §3 P1 第 7 项（评估日期 2026-09-25，行号见 §5 更正）
- **commit**：`970594f`（fix）+ `223c190`（test 补假会话 `Session.info`）

---

## 1. 需求

SSE 流式回答**中途客户端断连**时，已经逐块下发给用户的回答片段**整轮丢失**：历史面板里这一轮只剩 user 提问，assistant 行不存在，用户刷新后看不到自己刚看到的半截答案（也会误以为提问没发出去）。

根因不是「落库失败」而是**落库点根本走不到**：既有实现「先攒完再写」（`_storeSessionMessages` 在流结束后调用），而断连时生成器**多数停在 `yield` 上**（不在任务栈上，`async for` 也不调 `aclose()`）⇒ `except CancelledError` 与 `finally` **都不触发**，攒好的片段随请求一起消失。

用户口径（binding）：**落「已产出的部分答案」+ 标记中断** —— 不要求「补全答案」，也不接受「悄悄丢掉」。

**验收标准**：① 断连后库中该轮 user + assistant 行都在，assistant 行内容 = 断连前已下发的片段；② 该行被标记为中断（`interrupted=true`），前端明确提示「已中断」而非当完整回答渲染；③ **正常跑完的一轮不得被兜底逻辑重复写第二行**；④ 断连轮次的查询状态照常保存（否则下一轮追问失去锚点）。

## 2. 设计评审

### 候选方案（压测推翻了前两个）

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 生成器 `finally` / `except CancelledError` 里落库 | **否决（实测）**：断连有两种时序，A 情形（多数）生成器停在 `yield` 上，两者**都不触发**。只能抓到少数情形（正 `await` 上游时抛进生成器帧） |
| B | `await asyncio.shield(task)` 保「先落库再写状态」 | **否决（实测）**：anyio 取消是**电平触发**（`_deliver_cancellation` 自我重排直至内部任务全完），`await shield(task)` **必定立刻抛 `CancelledError`**，其后代码全是死代码 —— shield 保护的是内层任务，不是你 await 它的能力 |
| C | **`StreamingResponse(background=...)`**（选定） | Starlette 在收敛任务组**之外** await 它（`responses.py:282-283`），取消范围退出时吸收自身取消 ⇒ 断连时**确定会跑到**；此刻请求作用域 session 仍开着（`getDb` 的 `yield` 栈包住 `await response(...)`）。运行时实验：`yield0 | BACKGROUND_RAN | CALL_RETURNED` |
| D | 分离 `asyncio.create_task` 写库 | 否决：事件循环关闭/进程退出不可控（任务可被 GC 中途回收，除非存进模块级 `set` + `done_callback` discard）—— 收益与 C 相同而风险更高 |

### 实现细节的三处口径（均由压测决定）

1. **单一写入点 + 单发标志**，不留第二写入点：情形 B 下生成器的 `finally` 会**早于** background 跑，两个写入点必须共用同一标志 —— 不如只留一个。
2. **兜底写入用独立会话**（`getSessionFactory()`），请求会话**只用来读快照**。这不是防御性写法：取消打在最近一个 await 上（实测落在 `_recordUsage` 的 `commit() → flush()` 中途），请求会话随即 needs-rollback（直接写抛 `PendingRollbackError`）；即便先 `rollback()`，底层 asyncpg 连接已关而 SQLAlchemy 未察觉（探针实测 `pg_closed=True` 且 `invalidated=False`）⇒ 下一条语句整条失败于 `InterfaceError: connection is closed`。`pool_pre_ping` 兜不住（连接是在**被持有期间**死掉的，pre_ping 只在签出时检查）。
3. **不做 token 回填**（诚实性，见 §6）。

### 状态载体：`session.info`

`StreamPersistState` 挂在**请求 session 的 `session.info`**（`_STREAM_PERSIST_KEY = "_streamPersistState"`），而不是走形参 —— 走形参就得给 `processMessageStream` / `_streamQuery` / `_streamMultiStep` 全加一遍签名。`session` 与 background 任务同寿命（依赖 teardown 在响应体发完之后），故请求内是**同一个实例、同一个 info 字典**。

该 dataclass 是**每请求可变持有器**（按引用共享，字段就地赋值是刻意的）：这是对项目「不可变」规则的**显式例外**，已在类 docstring 写明理由；字段写全是廉价赋值（追加字符串/赋标量），不涉及 IO。

## 3. 数据模型变更

**唯一迁移**：`0085_session_message_interrupted` —— `session_message.interrupted BOOLEAN NOT NULL DEFAULT false`。

- 存量历史行天然 `false`（当时不存在中断行）；加列 + 常量默认值，向后兼容；
- `downgrade` = `DROP COLUMN`（只丢标记列，可接受）；
- prod 应用前备份 `session_message_20260927.sql`（迁移时 **636 行**回填；此后探针又写入 4 行，当前 `count(*) = 640`，其中 `interrupted` 行 1 条 = 本次断连验证）；
- ORM 侧 `app/domain/models.py` 同步加列（`nullable=False`），并由集成测试 `test_session_message_columns.py` 直连 prod schema 钉死三方一致（库列 / 库默认值 / ORM），拦住「迁移跑了但 ORM 没加列」这类只在运行期才炸的漂移。

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `ChatMessageRead`（`schemas.py`） | 新增 `interrupted: bool = False`（前端 `interrupted: boolean` 必填，可空列会让历史行静默拿不到值） |
| 唯一构造点 `session_history_service.py` | 透传 `row.interrupted` |
| `_storeSessionMessages(...)` | 新增**关键字专用**参数 `interrupted: bool | None = None` ⇒ **27 个既有调用点全部不用改** |
| API 层 `POST /api/v1/chat/stream` | `StreamingResponse(eventSource(), background=BackgroundTask(persistIfInterrupted))`；`persistIfInterrupted` 只负责调 service（事务与语义都在 service 层），吞异常只记日志（响应已发完，抛出只会污染日志） |
| 前端 `types/chatHistory.ts` / `types/chat.ts` / `chatStore.ts` / `MessageItem.tsx` | 字段贯通 + 中断行渲染 warning 提示；文案 `messageItem.interrupted`（zh/en） |
| 文案常量 | 后端 `MSG_SCHEMA_CHAT_HISTORY_MESSAGE_INTERRUPTED`（`error_messages.py`）、`MSG_STREAM_INTERRUPTED_EMPTY`（无任何已下发片段时的占位文案） |

**明确不保证**（不作为验收项，写在此处防止被当成回归）：进程被杀 / 容器停止中途的写入；客户端「中断后立刻重试」与 background 写入的**竞态** —— 幂等性不得建立在「取消时的写入一定已落库」之上。

## 5. 实现要点

| 位置 | 改动 |
|---|---|
| `chat_service.py:653-700` | `_STREAM_PERSIST_KEY` + `StreamPersistState`（`pending` / `answerPieces` / `sql` / `plan` / `resultColumns` / `totalCostUsd` / `startedAt`）+ `attachStreamPersistState` / `streamPersistStateOf` |
| `chat_service.py`（`processMessageStream` 入口） | 一进入即 `attachStreamPersistState(session)`（`pending=True`：此后任何 yield 都可能已被客户端看到） |
| `chat_service.py`（`_streamQuery` 状态变化处） | 就地赋值：`answerPieces`、`sql`、`plan`、`resultColumns`、`totalCostUsd`、`startedAt` |
| `_storeSessionMessages` 内（`await session.commit()` 之后） | 解除标记（`pending=False`）—— **落库点即解除点**，所以「哪些路径先落库后 yield」不需要逐个记住（闲聊/澄清/领域命令/卡片/多步全部自动覆盖） |
| `chat_service.py:4664` `persistInterruptedStream` | 首行短路（`pending=False` ⇒ 空操作）；写库走独立会话；同时 `_saveQueryState` 保住追问锚点；记 info 日志（session / 片段数 / 长度） |
| `app/api/v1/chat.py` | `StreamingResponse(background=BackgroundTask(persistIfInterrupted))` + docstring 说明为何这是唯一可靠钩子 |

**行号更正**（评估文档原文 `chat_service.py:2904-2922`、`:3303` 已过期）：实为 `processMessageStream` 入口、`_storeSessionMessages`、流式落库点；SSE 路由 `app/api/v1/chat.py`（原无 `try/except/finally`）。本批已把更正写回评估文档 §2.2。

## 6. 测试

- **集成（真实 PG + 完整 API 链路，`test_chat_stream_disconnect.py`）4 passed**：
  1. 中途断连 → 落「已产出片段」+ `interrupted=true`；
  2. 断连早于任何内容 → 落占位文案（`MSG_STREAM_INTERRUPTED_EMPTY`）；
  3. 正常跑完 → **不重复写**（单发标志生效）；
  4. 正常跑完 → 完整答案 + `interrupted=false`。
- **RED 证据（假会话保真度，`223c190`）**：H4 落地后 **unit 套件 81 例回归**，根因是手写假会话 `_FakeSession` 缺 `Session.info` —— 不是产品缺陷，而是**替身比真身更弱**：`Session.info` 是 SQLAlchemy 恒存在的公开字典，FastAPI `getDb` 永远给真 `AsyncSession`，省掉它等于让假会话说「真实会话没有这个属性」。修法是补假会话（`self.info: dict = {}` + docstring 说明**为何不可省**），**不是**在生产代码加 `getattr` 兜底。补后 80 例恢复，剩 1 例为本批**预存**失败（判别见 §8）。
- **单元**：本批聚焦 91 passed（详见同批各 SSOT）；全量/集成数字见 §8。
- **计量诚实性（原计划此处不成立，已改）**：答复 token 只随 `isDone` 终块到达（非终块 `promptTokens=0, completionTokens=0`）⇒ 断连时刻部分答案的 token 数**根本不存在**。故**不做回填、不写假账**：兜底行的 `token_cost_usd` = 已测得部分（计划/SQL/图表/已完成调用）之和，是**真实成本的下界**（断连那一轮的用量行随被取消的 flush 一起没了，它本就未提交）。
- **不可变规则的显式例外**：`StreamPersistState` 是每请求可变持有器（理由见 §2）；这是本批唯一违反「始终创建新对象」的位置，且已写进类 docstring —— 若按不可变返回新副本，按引用的 background 任务就看不到状态，功能直接失效。

## 7. 安全审查

**未触发**（无认证/密钥/SQL/加密变更）。两点已自查：

- 兜底写入的内容 = 本轮**已下发给同一用户**的片段（不扩大可见面）；写入目标 session 来自 `dto`，与既有非流式路径同一处信任边界（`getCurrentUser` 前的 DTO 校验不变）；
- `persistIfInterrupted` 吞异常只记日志：此时响应已发完，异常无法改变给客户端的输出 ⇒ **不存在**「错误详情回注客户端」的通道。

## 8. 部署验证（2026-09-27）

- **迁移**：测试库先验 upgrade/downgrade；prod 先备份 `session_message_20260927.sql` → `alembic upgrade head` → 校验列 `NOT NULL DEFAULT false`（636 行回填）；
- **部署**：`./scripts/deploy_backend.sh`（含 `alembic/`）；**仓库 ↔ 容器 md5 22/22 现存文件 MATCH**，唯一 DIFF = 仓库已删除的 `app/tests/services/test_l3_chained_steps.py` 仍在容器内（部署脚本不删文件；测试文件不参与运行时）；
- **真机断连验证**（本批最需要真机的一项，`/tmp/probe_h4_disconnect.py`：真实登录 + `httpx.stream(trust_env=False)` 在收到首个 `token` 事件后中断）：

  ```
  session_id                     role       len  interrupted
  probe-h4-disc-20260927001839   user        12  f
  probe-h4-disc-20260927001839   assistant    1  t     ← content='按'（断连前已下发的片段）
  probe-h4-full-20260927001839   user        12  f
  probe-h4-full-20260927001839   assistant  220  f     ← 对照：正常跑完的一轮

  session_query_state：两个 session 各 1 行，turn_count=1，has_plan=t，has_sql=t
  全库：640 行中 interrupted 行 = 1（无重复行 ⇒ 单发标志生效）
  ```

- **前端**：`docker compose build --no-cache frontend && up -d frontend`；nginx 实服 bundle `assets/index-suiR3M4D.js`（含中断提示文案，grep 命中 4 处）；`MessageItem.test.tsx` + `chatStore.test.ts` **58 passed**；
- **测试**：集成切片 120 passed + 3 例环境耦合（`conftest.py:28` setdefault sqlite；导出 `DATABASE_URL` 后 3/3 通过）；全量 unit+services **2 failed, 2522 passed, 1 skipped** —— 两条失败为**预存**（`TestSearchByKeywordAdsWeighting::test_weight_from_system_config_db_value`、`test_dependencies.py::test_stub_disabled_raises_permission_denied`），用 `git worktree add --detach /tmp/rbase HEAD~1` 复现同法判别，delta = 0；
- **静态检查**：本批 22 文件 ruff 输出与基线**逐行一致**（唯一 F841 为基线既有：基线 `test_milvus_client.py:174` → HEAD `:309`，同一函数未用绑定）；本批新增测试文件全 `All checks passed`；
- **网关**：`/api/v1/health` 直连 8000 与经 nginx 5173 均 200。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.2 H4（标 ✅，含行号更正）、§3 P1 第 7 项（划掉）、§15 批次记录
- 数据模型：`backend/alembic/versions/0085_session_message_interrupted.py`、`app/domain/models.py`（`SessionMessage.interrupted`）
- 契约守卫：`app/tests/integration/test_chat_stream_disconnect.py`（4 例）、`app/tests/integration/test_session_message_columns.py`（列/默认值/ORM 三方一致）
- 规则：根 `CLAUDE.md` 核心约束 #6（显式错误处理）、#3（Token 计量）、`Harness/rules/开发流程规范.md`（TDD）、`Harness/rules/数据库环境使用规范.md`（prod 先备份）
- Memory：`qa-system-*` 新增 H4 条目（断连语义 + 「`finally` 在停在 `yield` 的断连下不触发」+ 请求会话在取消后不是可靠写入通道 + 假会话 `Session.info` 保真度教训）
- 同批：`../fix-llm-transient-retry/`、`../chore-l3-deadcode-and-prior-cte-contract/`、`../fix-embedding-provider-type-guard/`、`../fix-milvus-list-all-pagination/`、`../test-kpi-match-cache-ordering/`

## SSOT 校验清单

- [x] 第 1 段 需求：断连丢半截答案的用户视角 + 用户口径（落部分答案 + 标记中断）+ 4 条验收标准
- [x] 第 2 段 4 个候选方案（含被压测**否决**的 A/B 与理由）+ 三处实现口径 + `session.info` 载体选择 + 不可变规则的显式例外
- [x] 第 3 段 迁移 `0085`：加列 + NOT NULL DEFAULT false + 降级路径 + prod 备份与 636 行回填 + ORM/库三方一致守卫
- [x] 第 4 段 接口契约：DTO/构造点/关键字专用参数（27 调用点不改）/API 钩子/前端贯通 + **明确不保证**两条
- [x] 第 5 段 实现要点逐位置表 + 评估文档行号更正
- [x] 第 6 段 测试：集成 4 例 + RED 实录（假会话保真度 81 例回归）+ 计量诚实性（不做 token 回填，成本是下界）+ 不可变例外说明
- [x] 第 7 段 安全审查：未触发 + 两点自查（可见面不扩大 / 无错误回注通道）
- [x] 第 8 段 部署验证：迁移 + md5 22/22（含唯一 DIFF 的解释）+ **真机断连与对照的库内实录** + 前端 bundle + 全量测试与预存失败判别 + ruff delta 0 + 网关 200
- [x] 第 9 段 跨文件链接（评估文档 / 迁移 / 契约守卫 / 规则 / Memory / 同批）
