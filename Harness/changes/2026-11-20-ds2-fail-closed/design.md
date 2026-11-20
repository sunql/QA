# C 方案：数据源入口代码级 fail-closed（停用源显式 API 堵死）

- 日期：2026-11-20
- 来源：`Harness/changes/2026-11-20-recall-fix-release/acceptance-1120.md` §7.3 交接项 ①
- 前置事实：1120 验收实证——B 方案（`is_active=false`）只挡 UI 下拉（`listDataSources(true)`），
  **后端 `DataSourceService.get()` 不校验 `is_active` ⇒ 显式传 `datasourceId=2` 的 API 仍走得通**
  ⇒ ds2 仍是 1119 故障（跨源幽灵表 ORA-00942）的复现入口（在 B 执行前）/ 隐患（B 执行后）。
- 实现回执：TDD 落地（RED→GREEN 实证）；定向 312 用例 303 passed / 7 failed
  （**7 个全部为基线既有红，git-diff-identity 实证，净新增 0**）；lint 净新增 0。
- 状态：**已评审（2026-11-20 用户拍板）**：① 409 + 简化叙事——ds2 是**测试数据源**、忽略不计，
  「曾对用户可见」只是测试使用场景，不作为论据展开；② L2 本批做；③ discovery_snapshot 不切。

## 1. 目标与非目标

**目标**：`is_active=false` 的数据源，除管理面外，任何消费入口都进不来。
显式引用停用源的请求在**入口处被拒**（fail-closed），不再依赖「schema_cache 恰好缺失」这种偶然形态。

**非目标（边界，勿扩）**：
1. **不扩「类→源」语义**：`ontology_class` 无 `datasource_id`/`is_active` 列，
   类粒度的可见性是采购域 M1 挂账，**不在本批做**（验收记录已明示「不要在 C 里顺手扩」）。
2. **不动管理面**：`DatasourcePage`（`activeOnly=false`）必须仍能看到/编辑/恢复停用源。
3. **不替代 B**：B（生产 `UPDATE data_source SET is_active=false WHERE id=2`）是数据处置、须用户批准执行；
   C 是代码守卫，两者独立生效、互不依赖——**B 未执行时 C 也能独立堵住 API 面**。

## 2. 调用面盘点（recon 实测，main @ 3a90e633）

### 2.1 消费路径（要堵）——共 5 处 `DataSourceService.get()`

| # | 位置 | 角色 |
|---|---|---|
| 1 | `api/v1/research.py:350`（`_resolveDatasource`） | 研究会话创建，显式 `datasourceId` |
| 2 | `chat_service.py:991` | 单聊召回链路（`classifyAndRecall`） |
| 3 | `chat_l4.py:100` | L4 链路 |
| 4 | `research_agent_execution.py:156` | 研究执行时重读 |
| 5 | `research_schema_reflection.py:104` | schema 反射 |

多步链路（`chat_multistep.py`）的 `pc.ds.id` 取自 `chat_service.py:991` 同一 dto ⇒ **由 #2 一并覆盖**，
入口口径矩阵（验收 §「4 个过滤调用点同源」）的结论保持成立。

### 2.2 管理路径（不动）——继续用裸 `get()`

- `api/v1/datasource.py:80/:119/:147/:230`（详情/更新/删除/测试连接路由）
- `datasource_service.py:124/:206`（`update`/`delete` 内部）
- `discovery_snapshot_service.py:363/:444/:478`（introspection 是管理操作，停用源仍可手动刷）
- `research.py:1125` 等映射类调用（读行→DTO，不进召回链路）

## 3. 设计：两层防线

### L1（主防线）——`getActive()` + 消费入口切换

1. 新增领域异常 `DataSourceInactiveError(ConflictError)`
   （`backend/app/domain/exceptions.py`；复用既有 `ConflictError` ⇒ HTTP 409，
   全局 handler 零新增映射）。
2. `DataSourceService` 新增：

   ```python
   async def getActive(self, session: AsyncSession, datasourceId: int) -> DataSource:
       """按 id 获取**活跃**数据源；不存在 → 404；已停用 → 409。"""
       ds = await self.get(session, datasourceId)
       if not ds.is_active:
           raise DataSourceInactiveError(MSG_DATASOURCE_INACTIVE.format(id=datasourceId))
       return ds
   ```

3. §2.1 的 5 个消费入口 `get()` → `getActive()`。逐处最小 diff，不改方法签名、不改调用方结构。

**为什么 409 不是 404**：ds2 曾挂在聊天下拉（用户可见），不存在「泄露停用资源存在性」的问题；
停用是明确的资源状态冲突，409 + 消息「数据源已停用」比 404（伪装不存在）更可运维——
用户与排障方都能一眼区分「id 写错」与「源被停用」。

### L2（纵深兜底）——召回过滤层对「停用 + no_cache」fail-closed

`applyDatasourceRecallFilter`（`recall_datasource_filter.py:108`）当前唯一能触发的 fail-open 是
`no_cache`（缓存缺失/空/读失败 ⇒ 不过滤，披露 `datasourceFilterSkipped=no_cache`）。
加固：进入过滤时读一次 `is_active`（裸 `get()`，不递归走 getActive）⇒

- **活跃源 + no_cache** ⇒ 维持 fail-open（新源首次 introspect 前合法，不能误伤）；
- **停用源 + no_cache** ⇒ **抛 `DataSourceInactiveError`**（把「静默跨源放行」变成硬错误）。

动机：L1 靠「逐入口接线」，未来新增消费入口漏接 `getActive()` 时，
L2 保证停用源**至多得到一个显式异常**而不是静默的跨源幽灵表。

## 4. 兼容性

- 管理面零行为变化（§2.2 未动）。
- 4 个活跃数据源的消费路径零行为变化（`getActive` 对 `is_active=true` 是恒等透传）。
- 停用源引用从「静默放行」变「409」，属**刻意破坏的契约变更**，登记为本批披露项
  （对齐 A4 批次的「契约变更 D15 已刻意破坏」口径）。
- **无迁移**（零 DDL）；`.env.example` 无新开关（fail-closed 无需开关——它的反面
  「静默放行」才是要消灭的状态）。

## 5. 测试计划（TDD）

1. **RED 集成用例**（并入既有文件，遵守「集成用例须并进既有文件」纪律）：
   - chat 入口：`datasourceId=<停用源>` ⇒ 409 `DataSourceInactiveError` 消息可读；
   - research 入口：`_resolveDatasource(停用源)` ⇒ 409；
   - **防误伤守卫**：管理路由 `GET/PUT /datasources/{停用源id}` ⇒ 仍 200（管理面不受 L1 影响）。
2. **L2 用例**：停用源 + `schema_cache` 空 ⇒ 过滤层抛错（不再返回 no_cache fail-open）；
   活跃源 + 空 cache ⇒ 仍 fail-open（防「顺手全关」回归）。
3. **单测**：`getActive` 三态（不存在 404 / 停用 409 / 活跃透传）。
4. **回归口径**：失败集合对差净新增 0（对照 main 基线 8 shard 全红 + frontend pass）。

## 6. 与拆批线的关系

- 代码落点基于 **main**（recall 守卫属召回线，#157/#158/#159 已在 main），
  独立分支 + 独立 PR，**不进** b1–b4 拆批分支（那 4 个是采购线历史批次的合入载体）。
- 时序：与 #163（anchor_rescue 日志级别）同为主线小批；**不阻塞** #162 合并拍板。

## 7. 待评审决策点

| # | 决策 | 建议 | 备选 |
|---|---|---|---|
| 1 | 停用源显式引用的 HTTP 语义 | ✅ **409** + 可读消息（叙事简化：ds2=测试源，堵它因为它是测试源，不做可见性分析） | ~~404~~ |
| 2 | L2 是否本批做 | ✅ **本批做** | ~~只做 L1~~ |
| 3 | `discovery_snapshot` 是否也切 `getActive` | ✅ **不切**（introspection=管理操作） | ~~切~~ |
