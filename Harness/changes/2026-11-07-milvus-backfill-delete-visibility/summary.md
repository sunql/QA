# 变更：milvus-backfill-delete-visibility

- **日期**：2026-11-07
- **作者**：Claude
- **Phase**：Phase 7 缺陷治理 · ⑦ 测试侧根因（Milvus 回填脚本 delete 可见性）
- **状态**：in-review
- **关联变更**：[fix-milvus-delete-visibility](../fix-milvus-delete-visibility/summary.md)（2026-09-27 的可见性实测）、
  [2026-11-06-merge-gate-flake-whitelist](../2026-11-06-merge-gate-flake-whitelist/summary.md)（本批要退休的那条白名单）
- **迁移版本**：无（本批零 DDL）
- **计划**：[plan.md](./plan.md)
- **MEMORY**：[qa-system-milvus-backfill-delete-visibility.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-milvus-backfill-delete-visibility.md)

---

## 1. 需求

**背景**：仓库里有一条被登记进 CI 抖动白名单的集成用例
`app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`，
它随机红。merge-gate 的判据是「失败集合对差，净新增为空才合并」⇒ 这类「同代码不同结论」的
随机红会**随机挡下约一半 PR**。

本批不做「让它变绿」，而做**根因**：查清它为什么随机红，并把**生产脚本里那条不成立的
契约**修掉。

**验收标准**：

1. `test_backfill_is_idempotent` 在**未改动其文件**时不再随机红 ⇒ 可从
   `.github/ci-known-flaky-tests.json` 退休该条（独立 PR-B）。
2. 回填脚本的「幂等」从**断言**变成**有机制保证的契约**：脚本结束时，被删的旧行必须已对
   后续 query 可见；否则脚本**抛错**，不返回可能撒谎的 `written`。
3. 生产风险面关闭：可见性窗口内重跑回填脚本不再造出重复向量。
4. 本批**零 DDL、零生产写**（脚本只改，不在生产上执行）。

**用户视角的「做完是什么样」**：运营在维护窗口里跑回填脚本，跑第二遍不会再写一批重复向量，
也不会出现「脚本说写了 3 行、集合里却有 6 行」的情况。

## 2. 设计评审

### 根因（四条实证，非推测）

1. **Milvus 的 delete 走三段异步管道** `Proxy → DML channel → DataNode → QueryNode`。
   `flush()` 只把 Growing → Sealed → 对象存储，**不保证** QueryNode 已装载并应用 delta binlog。
2. **实测对照**（2026-09-27，Milvus v2.4.6 真实容器，见
   [fix-milvus-delete-visibility](../fix-milvus-delete-visibility/summary.md)）：
   `consistency_level="Strong"` ❌ / `flush()` ❌ / **`release() + load()` ✅（~2.3s）**。
   同源结论已写在生产代码注释里（`milvus_dual_write.py::rebuildOntologyCollections`：
   「残留重复行正是这么攒出来的——2026-09-30 实测 32 个类被堆成 98 行」）。
3. **脚本的 delete 后面没有强制刷新**：`collection.delete(...)` 之后只有 `insert` + `flush()`。
4. **随机性的来源**：删除可见要等 `DataNode → QueryNode` 那段管道走完（秒级）⇒
   第二次扫描落在窗口内还是窗口外**随机**。

### 顺带发现的第二个缺陷（同一脚本，独立 bug）：重插的**内容**取自「另一行」

⚠️ 本节在 2026-11-07 被**代码评审推翻并更正**（早先版本写的是「旧实现里那句
`collection.delete(...)` 根本没执行过」——**这是错的**）。

**delete 打的是对的那一行**：Milvus 的 `query()` **总是返回主键**，即使 `id` 不在
`output_fields` 里（服务端 `fields_data` 始终带 PK，见 pymilvus `grpc_handler.py:2381` /
`abstract.py:445`）。
**决定性旁证**：2026-11-05 的 CI 日志里 `assert written2 == 0` **实得 `1`**
（`Harness/changes/2026-11-05-defect-priority-plan/summary.md:334`），而那次种了
**3** 行 ⇒ 若 delete 从未生效，第二跑会看到 3 行待回填、`written2` 应为 3；实测 1
⇒ **3 次 delete 里有 2 次已可见、1 次还卡在窗口内** ⇒ delete 确实发了，且各自打在了
自己那一行上。
⇒ **抖动 100% 是可见性竞态**，「删错行」不成立。

**真正取错的是重插的内容**：`by_oid` 按 `ontology_id` 建索引 ⇒ 同一 `ontology_id` 有多行时
（正是「残留重复行」状态），待重插的行会取到**另一行**的 name/alias/embedding
⇒ 内容被悄悄换掉。
⇒ 修法：待重插的行**按「需要回填的那一行自己的 auto-id」定位**（delete 与取内容用同一个键）。

### 影响面复核：只有一个受害者

同文件的 `test_backfill_then_reconcile_yields_zero_diff` **不是**第二个受害者。
逐行读过 `scripts/reconcile_milvus_id_mapping.py:112-166`：
`pg_by_key` 是 `reconcile()` 开头读 PG 的快照（用例先 `await backfill(dbSession)` ⇒ 占位行
**已 commit** ⇒ 键已存在）；`milvus_seen_keys` 是 `set` ⇒ 同一 key 的重复行**折叠**；
且对账 key 是 `(type, ontology_id)`、**不看 `external_id`** ⇒ 残留旧行也命中同一条 PG 记录
⇒ `unified_id` 相等 ⇒ 走「双侧一致 → 通过」，不计入 `rows` ⇒ `diff_count` 恒为 0 ⇒ 该用例**不红**。
旁证：白名单里从头到尾只有 `test_backfill_is_idempotent` 这一条。

### 候选方案与取舍

| 候选 | 做法 | 取舍 |
|---|---|---|
| **A. release + load（选定）** | 写完后强制 QueryNode 重装 segment + delta binlog | ✅ 仓库已实测有效（~2.3s）；✅ 确定性；❌ 该集合 2-3s **未加载** ⇒ 针对它的检索拿不到正常结果 |
| B. 只 flush | 现有行为 | ❌ 实测无效（2026-09-27） |
| C. per-request `consistency_level="Strong"` | 查询侧强一致 | ❌ 实测被 Bounded 5s 窗口覆盖 |
| D. 有界轮询等收敛 | 反复扫直到旧行消失 | ❌ 不刷新 ⇒ 线上仍在窗口内读到残留；且传播时延无已知上界 |
| E. drop + 重建集合 | 照抄 cleanup 的收敛策略 | ❌ **危险**：drop 与重插之间集合为空 ⇒ 脚本崩在中间 = 线上向量清空（2026-09-30 事故原样重演） |
| F. 只修测试（测试里调 refresh helper） | 脚本不动 | ❌ 只让测试绿，**生产的假幂等契约仍在** ⇒ 白名单退休了但问题还在 |

**最终决定**：**A**，并在其之上加一道**复扫校验**（仍有旧行可见 ⇒ 抛错）。
A 的代价可接受：这 2-3s 的未加载窗口只出现在「一次性维护脚本执行期间」，
而同部署里单条 delete+insert+flush 就要 10-25s ⇒ 同量级的小头。已在脚本 docstring 与
wiki 里写明。

**明确不做**（见 plan.md §五）：不动在线路径的 `deleteByOntologyIdDual`（属产品侧口径问题，
需单独立项）；不 drop 重建；不做去重；不在生产上执行脚本。

## 3. 数据模型变更

**无**。本批零 DDL、零 Alembic 迁移、不改任何表/列/索引/本体节点。

涉及的数据**语义**变化（不是结构变化）：

| 对象 | 变化 |
|---|---|
| `ontology_{class,property,metric}_embeddings` | `external_id` 为空的行被回填为 `obj:{type}:{ontology_id}`（既有行为，脚本早已在做） |
| `id_mapping` | 回填行同时写占位行（既有行为，`ON CONFLICT (business_object, external_id) DO NOTHING`） |
| 同上 | **新增不变量**：回填结束后，被删的旧行必须已对 query 可见（由脚本的复扫校验强制） |

## 4. 接口契约变更

**无对外 API / DTO 变化**。

内部契约变化（Python 层）：

| 符号 | 变化 |
|---|---|
| `app.infrastructure.milvus_client.refreshCollection(collection, *, waitForLoad=True)` | **新增**。`release()` → `load()` → `utility.wait_for_loading_complete(..., timeout=60)`。失败向上抛。`waitForLoad=False` ⇒ 非阻塞版 |
| `app.infrastructure.milvus_client.refreshWikiCollection()` / `refreshDocumentCollection()` | 改为委托给 `refreshCollection`（去重），**但显式传 `waitForLoad=False`** ⇒ **行为与改动前完全一致**（非阻塞） |
| `scripts.backfill_milvus_external_id.backfill(session) -> int` | 签名不变；**语义收紧**：不再返回「可能撒谎的 `written`」——后置条件不成立时抛 `BackfillContractError`（`RuntimeError` 子类） |
| `scripts.backfill_milvus_external_id.runBackfill(session, *, apply) -> _BackfillReport` | **新增**。返回 `written` / `planned` / `skipped` / `invalid` / `pgConflict` / `failedCollections` / `errors` 多个计数（`skipped` 必须让调用方看见） |
| `scripts.milvus_external_id_apply.BackfillContractError` | **新增**（`RuntimeError` 子类）：契约被破坏 ⇒ 必须中止，不得继续 |
| `scripts.milvus_external_id_apply.VisibilityGap` | **新增**：复扫缺口（`residue` / `missingNew` / `wrongUid`） |
| `scripts.milvus_external_id_placeholder.PLACEHOLDER_OK / _CONFLICT / _FAILED` | **新增**：PG 占位行三态 |
| `scripts.milvus_write_target_guard.assertSafeMilvusTarget(dbName, *, allowProdWrite=False)` | **新增**：Milvus 写目标闸门 |
| `scripts.backfill_milvus_external_id` 的 CLI | **新增参数**：`--apply`（默认预演）、`--allow-prod`（允许写生产 `default` 库）；退出码 0 / 1 / 2 |

## 5. 实现要点

### `backend/app/infrastructure/milvus_client.py`

新增 `refreshCollection(collection)`：`release()` → `load()` → `wait_for_loading_complete`。
`wait_for_loading_complete` 是**必需的**：pymilvus 的 `load()` 只是发一个请求就返回，
不等装载完成。`refreshWikiCollection` / `refreshDocumentCollection` 收敛到该原语。

### 文件拆分（工程结构硬上限：单文件 ≤ 800 行、函数 < 50 行）

初版重写把编排 + Milvus 原语 + PG 判定 + 写目标闸门塞进**一个文件**（941 行）⇒ 超 800 行硬上限
（`Harness/rules/工程结构.md:27`）。拆成四个职责单一的模块：

| 文件 | 行数 | 职责 |
|---|---|---|
| `backend/scripts/backfill_milvus_external_id.py` | 470 | **编排与 CLI**：跑哪几个集合、PG 怎么处理、汇总报告、退出码 |
| `backend/scripts/milvus_external_id_apply.py` | 363 | **Milvus 侧原语**：读行 → 体检 → delete → insert → 刷新 → 复扫校验 |
| `backend/scripts/milvus_external_id_placeholder.py` | 221 | **PG 侧占位行三态**：已存在 / 冲突 / 不可用 |
| `backend/scripts/milvus_write_target_guard.py` | 64 | **Milvus 写目标闸门**：默认只允许非生产库（`default` 库须显式 `--allow-prod`） |

拆分理由：Milvus 侧有两条独立硬约束（delete 之前的体检、delete 之后的后置条件校验），
彼此耦合但与「跑哪几个集合、PG 怎么处理」无关 ⇒ 拆开才读得懂。

### `backend/scripts/milvus_external_id_apply.py`（Milvus 侧原语）

1. **扫描带 `id`**：元数据投影（`id, ontology_id, type, name, alias, description,
   external_id`，不含 1024 维 embedding ⇒ 扫描成本低），挑出 `external_id` 为空的行
   —— 它们**自带 auto-id**。
2. **按行自身的 auto-id 定位**：全量读（含 embedding）按 **`id`** 建索引（不再按
   `ontology_id`）⇒ delete 打在「要回填的那一行」上，**重插的内容也取自同一行**。
3. **两次扫描之间消失的行 ⇒ 跳过**（不拿空 name/embedding 插一行残缺数据）；
   `id` / `ontology_id` 不可用 ⇒ 跳过并计入 `skipped`。
4. **delete 之前先体检集合 schema**（`assertCollectionUsable`）：少一个字段 ⇒ 整批 insert 会被
   服务端拒绝 ⇒ 若此时已 delete 过，**同批的行全部丢失**（Milvus 无自动备份 ⇒ 不可逆）
   ⇒ 必须在**任何** delete 之前中止。
5. **delete 之前先体检载荷**（`payloadProblem`）：缺 embedding / 维度不符 / 非数值 / id 不可用
   ⇒ 排除出本批次（不删、不插、计入 `invalid`）。旧实现 `full.get("embedding") or []` 会把
   缺失向量悄悄变成空列表 ⇒ 整批 insert 因维度不匹配失败 ⇒ 同批已 delete 的行全部丢失。
6. **delete → insert → flush → `refreshCollection` → 复扫校验**（`applyBackfill`）。
7. **复扫校验**（**四条**后置条件，缺一即 `BackfillContractError`，绝不返回可能撒谎的 `written`）：
   ① `insert` 返回条数 == 预期行数；② `insert` 返回的 `primary_keys` **条数** == 预期行数
   （否则配不出「哪一行是新行」）；③ 被删的旧 auto-id 已不可见；④ 新插入的行已可见，且
   **按插入顺序逐行**带本次期望的 `external_id`。
   复扫用 `consistency_level="Strong"`：集合默认 Bounded（5s 容忍窗口）⇒ 默认查询可能读到
   旧视图 ⇒ 后置条件形同虚设。
8. **`query(limit=)` 撞上限 ⇒ 抛错**：Milvus 会**静默截断**（不报错）⇒ 读到的不是全量时
   被截掉的行既不会被回填、也不会被复扫校验看到 ⇒ 契约成了空话。
   （同款守卫见 `scripts/backfill_milvus_embeddings.py:127`。）
9. **批量 delete**（`id in [...]`，`_DELETE_BATCH_SIZE = 500`）：把「delete 已发出、insert 还没
   发出」的危险窗口从 N 次串行往返压到 1 次（生产 property 集合 3743 行 ⇒ 逐行 delete 是分钟级）。
10. **`VisibilityGap` 三类缺口**：`residue`（已删行仍可见）/ `missingNew`（新行不可见）/
    `wrongUid`（可见但 external_id 不符）。

### `backend/scripts/milvus_external_id_placeholder.py`（PG 侧三态）

`ensurePlaceholder` 返回三态而不是布尔：

| 状态 | 含义 | 动作 |
|---|---|---|
| `PLACEHOLDER_OK` | 占位行**已存在且一致**（生产常态 —— `reconcile_milvus_id_mapping.py` 的职责就是先写占位行） | 照常回填 |
| `PLACEHOLDER_CONFLICT` | `(bo, ext)` 被**另一个** `unified_id` 占用 ⇒ `ON CONFLICT (business_object, external_id) DO NOTHING` 会**静默放过** ⇒ 写下去就是悬垂引用 | 跳过该行，计入 `pgConflict`，**不改写 PG**、**不删 Milvus 行** |
| `PLACEHOLDER_FAILED` | 其它 PG 失败（主键冲突等） | 跳过该行，计入 `skipped` |

⚠️ `id_mapping.unified_id` 是**主键**，而 `ON CONFLICT (business_object, external_id)` **覆盖不到
主键冲突** ⇒ 主键冲突必然抛 `IntegrityError` ⇒ 旧实现「只看有没有抛异常」会把
「占位行已存在（正常）」误判成「PG 写失败」⇒ **向量一行都修不了**，运维只看到误导性日志。
⇒ 必须**回读判定**，不能只看异常。

### `backend/scripts/milvus_write_target_guard.py`（写目标闸门）

默认**只允许非生产库**；`default` 库（生产）必须显式 `--allow-prod`。
`describeMilvusTarget(uri, dbName)` 只回显 `host:port/db`，**绝不回显凭据**。

### `backend/scripts/backfill_milvus_external_id.py`（编排与 CLI）

- **PG 先落盘（`commit`）→ 再做 Milvus 变更**：Milvus 的写不可回滚 ⇒ 占位行必须先持久化。
- **CLI 退出码**：`skipped > 0` ⇒ 退出码 1（**跳过必须让调用方看见**），
  否则自动化任务会把「还有行没回填」记成成功；另 2 = 参数/闸门错误。
- **默认预演**，必须显式 `--apply` 才写。
- docstring 把「幂等」从**断言**改成**有机制保证的契约**，并写明 2-3s 未加载窗口。

**依赖注入点**：脚本从 `app.infrastructure.milvus_client` 导入 `refreshCollection`；
单测通过 `monkeypatch.setattr` 替换**两个命名空间**里的 `Collection` / `_connAlias`
（编排模块与 `milvus_external_id_apply` 各持有一份引用），**但保留真实的 `refreshCollection`**
—— 只把 `milvus_client.utility` 换成假的 `wait_for_loading_complete`（记录调用参数）
⇒ 「脚本确实用了共享原语、且等到了装载完成」被钉住，而整个过程不连服务端。

## 6. 测试

### 新增单测（Milvus 用 fake，**PG 用真实 PostgreSQL**）

初版把 41 条用例塞进**一个**测试文件（941 行）⇒ 超 800 行硬上限 ⇒ 拆成
**共享 support 模块 + 两个关注面各一个测试文件**（与仓库既有 `_ci_gate_support.py` 做法一致）：

| 文件 | 行数 | 条数 | 关注面 |
|---|---|---|---|
| `backend/app/tests/unit/_milvus_backfill_support.py` | 298 | — | 共享替身（假 Milvus + 固定数据 + PG 读回） |
| `backend/app/tests/unit/test_backfill_milvus_delete_visibility.py` | 308 | 13 | delete 可见性与幂等契约 |
| `backend/app/tests/unit/test_backfill_milvus_safety_guards.py` | 460 | 15 | 安全护栏与运维可见性 |
| `backend/app/tests/unit/test_milvus_write_target_guard.py` | 71 | 7 | 写目标闸门 |
| `backend/app/tests/unit/test_milvus_refresh_collection.py` | 83 | 4 | `refreshCollection` 原语 |

**假 Milvus 复刻 delete 的异步可见性**：`delete` 只进 pending 集合，
**pending 的行在 query 里仍然可见**，只有 `release + load` 才生效（`applyDeletesOnLoad`）。
PG 侧走**真实** `dbSession` 夹具（真实 PostgreSQL；`Harness/rules/测试规范.md` 要求
数据层必须真实，不许用假库代替）⇒ 冲突 / 主键冲突都是**真约束**触发的。

⚠️ **support 模块暴露的是普通函数 `makeFakeMilvus(monkeypatch)`，不是 fixture**：
fixture 被 import 进测试模块后，它的依赖（另一个 fixture）要靠测试模块的命名空间解析
⇒ 跨模块再引一个 fixture 会变成 `fixture 'store' not found`；而直接 import fixture 名字
又会被测试函数的同名参数遮蔽（ruff F811）⇒ 由各测试模块自己声明一个 3 行的 fixture 包一层。

### `test_backfill_milvus_delete_visibility.py`（13 条）—— 可见性与幂等契约

| 用例 | 钉住的行为 |
|---|---|
| `test_backfill_fills_external_id_for_every_target_row` | 每个待回填行都被回填，旧行确实消失，**PG 占位行内容正确**（不只是计数） |
| `test_backfill_is_idempotent_across_two_runs` | **第二次 written == 0**（旧实现下必然红） |
| `test_delete_targets_the_rows_own_auto_id_when_ontology_id_duplicated` | 同 `ontology_id` 两行时，按行自身 auto-id 定位；重插内容取自被替换的那一行 |
| `test_raises_when_deleted_rows_stay_visible_after_refresh` | 刷新后旧行仍可见 ⇒ 抛错 |
| `test_refresh_happens_after_insert_and_flush` | 顺序 = delete → insert → flush → release → load → 复扫 query；且共享原语被调用（带集合名/超时/别名） |
| `test_no_writes_when_nothing_needs_backfill` | 无待回填行 ⇒ 零写、零 delete、零 refresh、零 PG 行 |
| `test_row_vanishing_between_the_two_scans_is_skipped` | 消失的行被跳过，不计入 written |
| `test_raises_when_insert_count_does_not_match` | `insert` 返回条数不符 ⇒ 抛错（不返回撒谎的 written） |
| `test_raises_when_inserted_rows_are_not_visible` | 插入的行不可见 ⇒ 抛错 |
| `test_raises_when_inserted_rows_carry_wrong_external_id` | 新行可见但**内容错位** ⇒ 抛错（判别「逐行配对」与「uid 集合差集」） |
| `test_raises_when_query_hits_milvus_page_limit` | 读到的行数撞上查询上限 ⇒ 抛错（静默截断 ⇒ 契约成空话） |
| `test_rows_with_unusable_ids_are_skipped_and_reported` | `id`/`ontology_id` 不可用 ⇒ 跳过、计入 `skipped`、不写 PG |
| `test_pg_placeholder_is_durable_when_milvus_mutation_fails` | Milvus 变更失败时，PG 占位行**已 commit**（用另一个 session 读，证明落盘） |

### `test_backfill_milvus_safety_guards.py`（15 条）—— 安全护栏

| 用例 | 钉住的行为 |
|---|---|
| `test_pg_primary_key_conflict_skips_row_without_deleting_milvus_row` | **真实 `IntegrityError`** ⇒ 跳过该行，不删 Milvus 行、不计入 written |
| `test_pg_placeholder_is_durable_when_milvus_mutation_fails` | PG 占位行**已落盘**（跨 session 读） |
| `test_pg_placeholder_already_exists_is_treated_as_ready` | 占位行**已存在且一致** ⇒ 不是失败，照常回填（生产常态） |
| `test_pg_placeholder_with_conflicting_unified_id_is_reported_as_conflict` | `(bo, ext)` 被别的 `unified_id` 占用 ⇒ 判冲突、不改写 PG、不删 Milvus 行 |
| `test_row_with_missing_embedding_is_skipped_before_delete` | 缺 embedding ⇒ 排除出本批次，**且不得删它** |
| `test_row_with_missing_embedding_is_skipped_even_without_known_dimension` | **维度未知时**缺 embedding 仍必须被排除（变异取证 M13 暴露的盲区，见下） |
| `test_raises_when_insert_primary_keys_count_does_not_match` | `primary_keys` **条数**对不上 ⇒ 抛错（无法逐行校验 ⇒ 拒绝返回 written） |
| `test_collection_missing_required_field_fails_before_any_delete` | schema 缺字段 ⇒ **在任何 delete 之前**抛错 |
| `test_dry_run_does_not_write_anything` | 默认预演：既不写 PG，也不删/插 Milvus |
| `test_dry_run_reports_pg_conflict_without_writing` | 预演也要暴露 PG 冲突（否则运维看不到「这行永远修不了」） |
| `test_one_failing_collection_does_not_abort_the_others` | 单集合失败 ⇒ 继续处理其余集合，最后统一汇报 |
| `test_external_id_is_built_by_the_unified_id_ssot` | `external_id` 必须等于 `buildUnifiedId(...)`（走 SSOT 构造器） |
| `test_delete_is_batched_into_one_in_expression` | N 行 ⇒ 一次 `id in [...]` delete（不是 N 次 RPC） |
| `test_rescan_query_uses_strong_consistency` | 复扫 query 必须显式 `consistency_level="Strong"` |
| `test_pg_integrity_error_logs_the_database_cause` | PG 冲突时日志里必须有**数据库的原始错误**，不能只有异常类名 |

### `test_milvus_write_target_guard.py`（7 条）—— 写目标闸门

生产 `default` 库无 opt-in 时被拦、opt-in 时放行、非生产库自由、空库名视作生产、
描述只回显 `host:port/db`、**不泄漏凭据**、默认 host/port。

### `test_milvus_refresh_collection.py`（4 条）—— 原语本身的守卫

顺序（release → load → **wait**，wait 必须在 load 之后：先等后装等于白等）、
必须等待装载完成且带超时、等待失败向上抛、**默认是阻塞版**、
以及两个既有 helper 确实委托给它**且保持非阻塞**（不改变既有集成路径的时序）。

### 变异取证（守卫是否真能抓住回归）—— **22 项，全部变红**

工具：`/tmp/milvus_backfill_mutation_sweep.py`（备份源文件 → 施加一处变异 → 跑 4 个定向测试文件
→ 记录红/绿 → 还原）。**基线绿、22 项变异全部红、还原后复跑绿。**

| # | 变异 | 结果 |
|---|---|---|
| M1 | 不刷新集合（delete 对 query 不可见） | ✅ 红 |
| M2 | 复扫不检查「已删行是否仍可见」 | ✅ 红 |
| M3 | 复扫不检查「新行是否可见」 | ✅ 红 |
| M4 | 复扫不逐行核对新行 external_id | ✅ 红 |
| M6 | 不检查 insert 返回条数 | ✅ 红 |
| M7 | 不检查 insert 返回的 `primary_keys` 数量 | ✅ 红（**补测试后**） |
| M8 | 不检查 query 是否撞 Milvus 行数上限（静默截断） | ✅ 红 |
| M9 | 待重插的行按 `ontology_id` 取内容（取错行） | ✅ 红 |
| M10 | 复扫不用强一致（Bounded 窗口 ⇒ 后置条件形同虚设） | ✅ 红 |
| M11 | delete 改成逐行（危险窗口被拉长成 N 次往返） | ✅ 红 |
| M12 | 集合缺字段也不中止（体检失效） | ✅ 红 |
| M13 | 载荷体检失效（embedding 缺失也放行） | ✅ 红（**补测试后**） |
| M14 | 编排层不体检集合（不调 `assertCollectionUsable`） | ✅ 红 |
| M15 | 槽位冲突不判（`(bo, ext)` 被别人占用也当可用） | ✅ 红 |
| M16 | `unified_id` 冲突不判（主键冲突也当可用） | ✅ 红 |
| M17 | 占位行已存在被判成不可用（生产常态被误杀） | ✅ 红 |
| M18 | 占位行根本不写（apply 时跳过 INSERT） | ✅ 红 |
| M19 | PG 错误只落异常类名（原始错误不落日志） | ✅ 红（**变异写法更正后**） |
| M20 | 冲突行不跳过（照写 Milvus ⇒ 悬垂引用） | ✅ 红 |
| M21 | 预演也写 Milvus（`apply=False` 不再只读） | ✅ 红 |
| M22 | 单集合失败不隔离（直接向上抛，打断其它集合） | ✅ 红 |
| M23 | 生产 Milvus 闸门放行（`default` 库不再被拦） | ✅ 红 |

#### ⚠️ 首轮 4 项变异「仍是绿的」—— 三条是**空守卫**，一条是**变异写错了**

「守卫会红」≠「守卫在测你想测的东西」。首轮 22 项里有 **4 项没有变红**，逐个查清后：

| 编号 | 现象 | 根因 | 处置 |
|---|---|---|---|
| **M5** | 去掉「期望的 uid 是否可见」检查，全部测试仍绿 | 该检查被 `missingNew ∪ wrongUid` **严格蕴含**（每个期望 uid 都挂在某一个新行上 ⇒ 该行要么不可见 ⇒ `missingNew`；要么可见但 external_id 不符 ⇒ `wrongUid`）⇒ 它**永远不会独立触发** | **删除该检查**（`VisibilityGap` 从四类降为三类，docstring 写明为何它被蕴含）⇒ 死代码不留 |
| **M7** | 去掉 `primary_keys` 数量校验，全部测试仍绿 | 假 Milvus 没有「返回的主键列表被截断」这个故障模式 ⇒ 守卫**无从触发** | 给 fake 加 `primaryKeysCountOverride` 旋钮 + 新增用例 `test_raises_when_insert_primary_keys_count_does_not_match` |
| **M13** | 去掉「embedding 缺失」检查，全部测试仍绿 | 原来那条用例（`embedding: []`）会被**另一条**守卫（维度不符：0 ≠ 集合维度 2）挡住 ⇒ 「embedding 为空」这一支**从未被单独验证**（**同因掩盖**） | 新增用例 `test_row_with_missing_embedding_is_skipped_even_without_known_dimension`：把集合维度设成未知 ⇒ 「embedding 为空」成为唯一判据 |
| **M19** | 去掉 PG 原始错误落日志，全部测试仍绿 | **变异写法错了**：`str(exc)` 本身已带 DB 消息 ⇒ 原写法不是真变异 | 把变异改成**只落类名**（`type(exc).__name__`），与守卫声明的意图对齐 ⇒ 变红 |

⇒ 处置后复跑：**22/22 全部变红**，还原后复跑基线绿。

### 新增集成用例

`backend/app/tests/integration/test_reconcile_milvus_id_mapping.py`：
`test_backfill_leaves_no_row_with_empty_external_id` —— 直接钉住**后置条件**
（回填后三个集合都扫不到 `external_id` 为空的行；**本次种入的** `ontology_id`
每个只剩一行），比 `test_backfill_is_idempotent` 的「第二次 written == 0」更直接。
去重断言**只针对本次种入的 ontology_id**（5001/5002/5003）——脚本的职责是「一行换一行」，
**不做**跨行去重 ⇒ 断言范围扩到集合里所有行会因为脚本承诺之外的历史残留而误红。

### 本地执行结果

```
pytest app/tests/unit/test_backfill_milvus_delete_visibility.py \
       app/tests/unit/test_backfill_milvus_safety_guards.py \
       app/tests/unit/test_milvus_write_target_guard.py \
       app/tests/unit/test_milvus_refresh_collection.py  →  39 passed
pytest app/tests/integration/test_reconcile_milvus_id_mapping.py --collect-only  →  8 tests collected
ruff check（全部改动文件）  →  新增文件 All checks passed；
                              两个被修改文件的告警数与 HEAD 基线逐条相同（15 / 3，均为既有）
python3 /tmp/milvus_backfill_mutation_sweep.py  →  ✅ 全部变异均被至少一条测试捕获（22/22）
```

⚠️ **Milvus 集成套件本地不跑**：19530 是生产容器，仓库的 Milvus 集成夹具会 drop 集合
（虽有 `qa_test` 库级隔离，仍按 standing rule 不在本地跑）⇒ **integration 行为由 CI 取证**。
⚠️ `scripts/backfill_neo4j_external_id.py:23 I001` 是**既有** ruff 报错，与本批无关，未动。

## 7. 安全审查

**已触发 `security-reviewer`**（本批新增了 PG 写路径的异常处理与外部系统写路径 ⇒
命中「数据库查询」「外部 API 调用」两条触发条件）。评审**提出 1 项阻断项**，已在本批修掉：

| 级别 | 发现 | 处置 |
|---|---|---|
| **H1（阻断）** | **insert 的返回结果从未被校验 ⇒ 已删的向量可能静默丢失（`written` 在撒谎）** | 已修：`MutationResult.insert_count` 与预期行数逐条比对，不符即 `RuntimeError`；且复扫校验进一步确认新行**可见且带正确 external_id**（见 §5 第 6、7 条）。新增用例 `test_raises_when_insert_count_does_not_match` + `test_raises_when_inserted_rows_are_not_visible` + `test_raises_when_inserted_rows_carry_wrong_external_id` 钉死 |

其余人工复核结论：

- **无硬编码密钥**：本批零新增配置、零新增环境变量。
- **SQL**：脚本里的 `INSERT INTO id_mapping ... ON CONFLICT (business_object, external_id)
  DO NOTHING` 是**既有语句**，用的是 SQLAlchemy `text()` + **具名参数绑定**（`:uid` 等），
  **无字符串拼接** ⇒ 无注入面。
  ⚠️ `collection.delete("id in [...]")` 是**表达式拼接**，但 id 来自 Milvus 自己
  返回的整型主键，并经 `asInt()` 校验（转不动就跳过该行）⇒ 不会拼出非法表达式。
- **异常信息外泄**：SQLAlchemy 的异常文本会带上失败的 SQL 与参数 ⇒ 已用 `_truncate()`
  截断到 200 字再进日志。
- **输入校验**：脚本的输入全部来自 Milvus 自身的数据与固定常量，无外部输入面；
  `id` / `ontology_id` 均经 `asInt()` 校验。
- **错误处理**：每个层级显式处理；PG 失败走 warn-and-skip 并汇总计数（`IntegrityError`
  与其它 `SQLAlchemyError` 分开），且 `skipped > 0` ⇒ CLI 退出码 1；
  delete/insert 不可见抛 `BackfillContractError` 而非静默吞掉 ⇒ 无静默失败。
- **副作用**：脚本在生产上有写操作，但本批**不在生产上执行** ⇒ 零生产写。

### 评审后新增的处置（本轮修掉）

| 级别 | 发现 | 处置 |
|---|---|---|
| **HIGH** | **delete → insert 之间没有「已删行不可逆」的护栏**：Milvus **无自动备份** ⇒ 若在 delete 之后、insert 之前崩溃，该行向量**永久丢失**（2026-09-30 事故同机制） | 本批已加**两道**护栏，把「静默丢失」变成「必然报错」：① delete **之前**先体检集合 schema 与每行载荷（缺字段 / 缺 embedding ⇒ 在任何 delete 之前中止）；② PG 占位行**先 commit** 再动 Milvus ⇒ 崩了也还有 PG 侧的恢复线索。⚠️ delete→insert 之间的**顺序问题本身**未改（见「后续立项」） |
| **HIGH** | **写目标闸门缺失**：脚本对连上的 Milvus 库没有「只允许非生产库」的保护（对照 `scripts/kpi_metric_db_guard.assertSafeTarget`） | 已加 `scripts/milvus_write_target_guard.py`：默认只允许非生产库，`default` 库须显式 `--allow-prod`；`describeMilvusTarget` 只回显 `host:port/db`，**绝不回显凭据**。7 条用例钉死 |
| **MEDIUM** | **PG 占位行三态被压成布尔** ⇒ 「占位行已存在」被误判成「写失败」 | 已改为三态（`PLACEHOLDER_OK` / `CONFLICT` / `FAILED`）+ **回读判定**（不靠异常判断）；`CONFLICT` 单独计数（`pgConflict`）且**不改写 PG、不删 Milvus 行** |
| **MEDIUM** | **复扫用默认一致性等级** ⇒ 集合默认 Bounded（5s）⇒ 后置条件可能读到旧视图，形同虚设 | 复扫显式 `consistency_level="Strong"`（有用例钉住） |
| **LOW** | PG 原始错误没进日志（只有异常类名）⇒ 运维分不清「已存在」与「真冲突」 | 已改为取 `exc.orig` 的原始消息并截断到 200 字 |

### 代码评审（`code-reviewer`）发现与处置

| 编号 | 发现 | 处置 |
|---|---|---|
| **M1** | `zip(...)` 必须带 `strict=True`（否则长度不匹配时静默截断） | ✅ 已加 `strict=True`（`milvus_external_id_apply.py` 的 `pairs` 构造与 fake 的列/行配对） |
| **M2** | 错误信息未带上下文 ⇒ 运维看不懂 | ✅ 已在 `BackfillContractError` 的每条消息里带上集合名、期望值与实得值 |
| **M3** | **wiki 原文说「delete 打的不是那一行」是错的** —— 实际 delete 打在正确行上，错的是**重插时按 `ontology_id` 取到了另一行的内容** | ✅ 已更正 `Harness/wiki/data-model.md` 的「Milvus 3 集合的 `external_id` 回填」小节：明确「delete 始终按行自身 auto-id（位置是对的）；错的是重插时取到了同 `ontology_id` 的另一行」⇒ 现行实现两处（delete 与取内容）用同一行 |
| **L1/L2/L5/L6** | 命名 / 注释 / 层级一致性等低优先级项 | ✅ 已按评审意见调整 |

### 评审后遗留的**阻断项**（必须在生产执行前解决）

⚠️ **`business_object` 的大小写 SSOT 未定**（脚本 docstring 已把它记为**生产执行的阻断项**）：
`id_mapping.business_object` 与 Milvus 侧 `type` 字段之间的大小写/取值口径没有单一事实源
⇒ 若两边口径不一致，占位行的 `business_object` 就会与既有行不一致 ⇒
`(business_object, external_id)` 唯一约束可能落到另一条槽位上 ⇒ 冲突/重复。
⇒ **本批只改代码，不在生产执行**；该口径需业务侧确认后单独立项。

## 8. 部署验证

**本批不部署**（零 DDL、零生产写、零配置变更；脚本只改代码，不在生产上执行）。

理由与依据：

- 改动只在 `backend/scripts/**`（一次性维护工具）、`backend/app/infrastructure/milvus_client.py`
  （新增一个未被生产代码路径调用的原语）、测试与文档 ⇒ **不影响运行中的应用行为**。
- `refreshCollection` 的唯一新调用方是回填脚本。

⚠️ **一处差点引入的行为变化，已在提交前消掉**：`refreshWikiCollection` /
`refreshDocumentCollection` 若直接委托给 `refreshCollection`（默认阻塞版），
就会**多等一次装载完成** ⇒ 这两个 helper 的既有集成路径
（`test_milvus_wiki_fields.py` / `test_milvus_document_fields.py`）行为改变 ⇒
**本地无法验证**（Milvus 19530 是生产容器，不在本地跑）⇒ 有「净新增失败」的风险。
⇒ 改为**显式传 `waitForLoad=False`**，两个既有 helper 的语义与改动前**逐字节一致**；
回填脚本用默认的阻塞版（这才是「刷新完再复扫」的前提）。
⇒ 用例 `test_wiki_and_document_helpers_delegate_without_blocking_on_load` 把这条钉死。

**冒烟等价物**：本地定向单测 39 条全绿 + 集成文件可正常收集（8 条）+ `ruff` 全绿
（新增文件零告警；两个被修改的文件的告警数与 HEAD 基线**逐条相同** ⇒ 零新增）
+ **22/22 变异取证全部变红**（守卫有效性）。
**真正的验收在 CI**：见 §9 的闸门判据。

**生产安全网（只读）**：生产 Milvus `default` 库的集合行数在合并前后各取一次快照，
确认本批没有改动生产向量数据（本批不执行脚本 ⇒ 预期完全一致）。

## 9. 关联

- 设计稿 / 计划：[plan.md](./plan.md)
- 前序实测：[fix-milvus-delete-visibility](../fix-milvus-delete-visibility/summary.md)（2026-09-27）
- 白名单机制：[2026-11-06-merge-gate-flake-whitelist](../2026-11-06-merge-gate-flake-whitelist/summary.md)
- CI 日志证据（`written2` 实得 1）：[2026-11-05-defect-priority-plan](../2026-11-05-defect-priority-plan/summary.md)
- Wiki：[Harness/wiki/data-model.md](../../wiki/data-model.md#id_mapping统一-id-映射中枢a1--m0-p01p03)
  ——「Milvus 3 集合的 `external_id` 回填」小节
- Rules：[Harness/rules/测试规范.md](../../rules/测试规范.md)、
  [Harness/rules/开发流程规范.md](../../rules/开发流程规范.md)
- Memory：`qa-system-milvus-backfill-delete-visibility.md`、
  `qa-system-merge-gate-flake-whitelist.md`、`qa-system-milvus-test-drops-prod.md`、
  `qa-system-milvus-delete-visibility-flake.md`（**已于本批更正前两条**）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B/C/D/E/F 六项）
- [x] 第 3 段无迁移（本批零 DDL ⇒ 无迁移文件名可校验）
- [x] 第 7 段**已触发** security-reviewer，其阻断发现已修并补了用例（insert 结果未校验 / delete→insert 无护栏 / 缺写目标闸门 / PG 三态 / 复扫一致性 / 原始错误落日志）
- [x] 第 7 段**已触发** code-reviewer，M1/M2/M3/L1/L2/L5/L6 全部处置（**M3 = wiki 原文的表述错误已更正**）
- [x] 第 8 段写明不部署的理由与等价验证（本地定向单测 38 条 + 22/22 变异取证 + CI 判据），并登记了 `refreshWikiCollection`/`refreshDocumentCollection` 的行为微变
- [x] 第 9 段 ≥ 3 个跨文件链接（关联变更 ×2 / Wiki / Rules ×2 / Memory ×3）
- [x] 相关 wiki 文档已更新：`Harness/wiki/data-model.md` 新增「Milvus 3 集合的 `external_id` 回填」，且 M3 的表述错误已更正
- [x] **守卫有效性已用变异取证证明**：22 项变异全部变红；其中 4 项首轮「仍是绿的」已逐个查清（3 项空守卫已修/已删，1 项变异写错已更正）
- [x] 全部改动文件 ≤ 800 行、函数 < 50 行、嵌套 ≤ 4 层
- [ ] 至少 1 条 MEMORY 索引已在 `MEMORY.md` 添加 —— **合并前补**（PR-B 一并补）
- [x] 涉及真实 SQL/DB 改动 ⇒ **不适用**（本批零 DDL、零生产写；脚本不在生产执行）

---

## 后续立项（不在本批范围，已记录）

本批只修「可见性契约」与「重插内容取错行」。以下各项是**独立缺陷**，需要各自立项：

| 立项项 | 内容 |
|---|---|
| **delete → insert 的崩溃安全与顺序** | 脚本先 delete 再 insert ⇒ 中途崩溃 ⇒ 该行既没被回填也没被保留（向量丢失）。本批未改顺序（改成 insert-before-delete 会把「永久丢向量」换成「永久重复向量」，且与 2026-09-30 事故的修复方向冲突）。 |
| **`id_mapping.milvus_id` 语义** | 脚本把 `ontology_id` 写进 `milvus_id`，而该列语义应是 **Milvus 的真实 auto-id**（`MutationResult.primary_keys` 里就有）。 |
| **`refreshCollection` 命名** | 名字暗示「刷新缓存」，实际语义是「release+load 以重装 segment + delta binlog」；在线业务路径上同名方法容易被误用。 |
| **`business_object` 大小写 SSOT** | `id_mapping.business_object` 与 Milvus `type` 字段的大小写/取值口径无单一事实源 ⇒ 占位行的 `(business_object, external_id)` 可能落到另一条槽位上。**这是生产执行的阻断项**（脚本 docstring 已登记）。 |
| **在线写路径仍写 `external_id=""`** | 在线本体更新路径（`milvus_dual_write.py`）新增行时 `external_id` 仍写空 ⇒ 回填脚本修完的只是存量，新写入的又变空 ⇒ 幂等契约长期不成立。 |
| **`_MILVUS_QUERY_PAGE` vs 服务端 `maxQueryResultWindow`** | 脚本按 16384 判定「撞上限」，但服务端窗口是**可配置**的 ⇒ 两边不一致时守卫会误判（要么放过真截断、要么误报）。需与部署配置对齐或改成读服务端配置。 |
| **`milvus_client.py` 拆分** | 该文件已超 800 行硬上限（工程结构规则）⇒ 需按职责拆分（连接 / 集合 / 写路径），独立立项。 |

✅ **已完成、不再需要立项的**：
- ~~生产目标闸门~~ ⇒ 本批已加 `scripts/milvus_write_target_guard.py`（见 §7）。
- ~~PG 占位行三态~~ ⇒ 本批已完成（见 §5）。

---

## 闸门判据（PR-A）

本 PR 改动了 `test_reconcile_milvus_id_mapping.py` ⇒ B7 规则④生效 ⇒ 白名单里那条对
**本 PR 不再豁免** ⇒ `test_backfill_is_idempotent` 照常计入判据 ⇒

| 闸门退出码 | 含义 | 本批解读 |
|---|---|---|
| **0** | 净新增 0 | ✅ 修复生效，两侧都绿 |
| **5** | 净丢失（基线侧偶发红） | ✅ 抖动消失的证据 ⇒ 人工仲裁放行 |
| **1** | PR 侧净新增失败 | ❌ **修复没生效** ⇒ 回到 Phase 1 重新定位 |

PR-B（退休白名单那条）改的是 `.github/ci-known-flaky-tests.json` ⇒ 属 CI 相关面 ⇒
闸门必判 **6**（`CI_SURFACE_CHANGED`）⇒ 走 owner 越权口显式合并。

---

## 闸门仲裁记录：PR-A 退出码 5（净丢失 1 条）

**check-run**：`merge-gate / failure-set-diff`，id `128105617242`
（`conclusion=failure`，标题「退出码 5 ⇒ 不可合并（净新增 / 不可用 job / 待人工确认）」）。

**闸门读数**：

| 项 | 基线 | PR |
|---|---|---|
| run | `42259464993`（head `865463d9`） | `42273074270`（head `40ec6300`） |
| 失败条数 | **140** | **139** |
| 失败集合对差 | — | **净新增 0 / 净丢失 1** |

**净丢失的那 1 条** = `app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`
—— 恰好是本批立项要消灭的那条抖动。

### 三件套证伪（确认不是「把失败藏起来了」）

**(a) 用例确实执行了，而且通过了**（不是被 skip、不是没收集）：

- 基线 integration shard 3 汇总行：`34 failed, 600 passed, 11 skipped, 1375 warnings, 2 errors in 1487.76s`
- PR integration shard 3 汇总行：`33 failed, 602 passed, 11 skipped, 1395 warnings, 2 errors in 2405.54s`
- `skipped` 两侧都是 11 ⇒ 该用例**没有被跳过**；
  `passed` 600 → 602 = **+2**：新增的 `test_backfill_leaves_no_row_with_empty_external_id`（+1）
  ＋ `test_backfill_is_idempotent` 由红转绿（+1）。
- pytest `-q` 只给失败用例打 `FAILED` 前缀；PR 日志里该用例名只出现在
  **warnings 汇总的收集清单**中（71 → 75 次提及，均无 `FAILED` 前缀），
  失败摘要里没有它 ⇒ 它跑了且过了。

**(b) 基线侧确实是偶发红，且就是那个抖动签名**：

```
FAILED app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent - assert 1 == 0
```

`assert 1 == 0` 即「第二跑仍有 1 行没被回填」⇒ 正是 delete 可见性窗口内复扫的形态
（见 §1 的三段异步管道）。失败集合对差后，**两侧的全部差异只有这一条**
（`diff` 基线 34 条 vs PR 33 条 ⇒ 少的就是它，其余 33 条逐条相同）⇒
本 PR 没有掩盖任何别的失败，也没有引入任何新失败。

**(c) 不是靠改弱断言换来的绿**：

1. diff 中 `test_backfill_is_idempotent` 的**用例体未被修改** ——
   本次对该集成文件的唯一删除是那一行
   `assert report.diff_count == 0, f"diff after backfill: {report.rows}"` 的
   **尾随空白差异**（已用 `od -c` 逐字节确认，断言文本一字未改）。
2. 本次 diff **确实触碰了该用例的责任代码路径**：`backend/scripts/**`（回填脚本重写）
   与 `backend/app/infrastructure/milvus_client.py`（新增 `refreshCollection`）⇒
   结果变好是因为行为变了，不是因为断言变松了。
3. 判据本身没有被绕过：B7 规则④因「该文件本次被改动」而让白名单豁免**失效**
   ⇒ 这条用例**照常计入判据**，没有躲在白名单后面。

⇒ 结论：**净丢失 1 条 = 抖动消失的正面证据**，不是回归，也不是藏失败 ⇒ 人工仲裁放行。

### 分片归属推导（一处更正）

本批一度用「macOS 上 `find … | sort -z` 的下标」推导分片，得 244 ⇒ 判 shard 0，
与实测（该文件在 **shard 3** 的日志里）矛盾。查因：

- CI（Ubuntu，GNU coreutils）用的是 `find … -print0 | sort -z`；
  **GNU `sort -z` 在 C.UTF-8 下按字节序排**，而 **macOS 的 BSD `sort -z` 走本地 locale 排序**，
  两者从下标 20 起就分叉（`test_agent_orchestration_runner_recovery.py` vs `…_pause.py`）。
- 按**字节序**重算：该文件下标 **243** ⇒ `243 mod 4 = 3` ⇒ **shard 3** ✓ 与实测一致；
  分片文件数也对得上（各 shard 78/79，CI 日志 `Shard 3: 78 test files`）。

⚠️ 教训：分片归属只能按 **byte-order** 推（`python3 -c "sorted(files).index(...)"`），
**不能用 macOS 的 `sort -z`**；下标推错 ⇒ 会去查错分片的日志 ⇒ 误判「用例没跑」。
