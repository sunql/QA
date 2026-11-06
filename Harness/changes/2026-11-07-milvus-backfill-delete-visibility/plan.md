# plan：⑦ 测试侧根因 —— backfill_milvus_external_id.py 的 delete 可见性

> 分支 `fix/milvus-backfill-delete-visibility`，base = `origin/main`（`865463d9`）。
> 本批**单独立项**：改 `backend/scripts/backfill_milvus_external_id.py` = 碰生产 Milvus 写路径，
> 按 standing rule「不在别的批次里顺手修」必须独立走 TDD + 独立 PR。
> **本批零 DDL、零生产写**（脚本只改，不在生产上执行）。

---

## 一、根因（不是猜测，是四条实证）

### 1. Milvus 的 delete 走三段异步管道

`Proxy → DML channel → DataNode → QueryNode`。`flush()` 只把 **Growing → Sealed → 对象存储**，
**不保证 QueryNode 已装载并应用 delta binlog**。

仓库里已有同一现象的实测记录（`Harness/changes/fix-milvus-delete-visibility/summary.md`，2026-09-27，
Milvus v2.4.6 服务端 + 真实容器）：

| 方法 | delete 后立即 query |
|---|---|
| `consistency_level="Strong"`（per-request） | ❌ 仍读到残留（Bounded 5s 容忍窗口覆盖 per-request） |
| `flush()` | ❌ 仍读到残留 |
| `release() + load()` | ✅ 立即读到删后状态（实测 ~2.3s） |

同源结论还写在生产代码的注释里（`app/infrastructure/milvus_dual_write.py::rebuildOntologyCollections`）：

> 「此部署下 Milvus 的批量 delete 不可靠（**删除要 release+load 才可见**），
> 残留重复行正是这么攒出来的——2026-09-30 实测 32 个类被堆成 98 行 / 3642 个属性被堆成 10926 行」

### 2. 脚本的 delete 后面没有强制刷新

`backend/scripts/backfill_milvus_external_id.py`：

```
collection.delete(f"id == {auto_id}")     # ← 无 flush / 无 release+load
...
collection.insert([...]) ; collection.flush()
```

`flush()` 只落盘 ⇒ **delete 对后续 query 不可见** ⇒ 旧行（`external_id == ""`）还在。

### 3. 脚本自述的幂等契约是假的

脚本 docstring 写「Idempotent: second scan finds no rows with external_id == "" → written=0.」，
集成测试 `test_backfill_is_idempotent` 断言 `written1 == 3 / written2 == 0`。
实际：第二次扫描**仍能查到**旧行（delete 还没可见）⇒ `written2 == 1` ⇒ 用例红。
⇒ 这不是「测试不稳定」，是**生产脚本的契约不成立**。

### 4. 随机性的来源与「本文件只有一个受害者」

删除可见要等 `DataNode → QueryNode` 那段管道走完（秒级）⇒ 第二次扫描落在窗口内还是窗口外
**随机** ⇒ 这正是 B7 白名单里那条 `test_backfill_is_idempotent`「同代码不同结论」的机制。

⚠️ **只此一个受害者**（2026-11-07 复核，推翻了本文件早先版本的说法）：
同文件的 `test_backfill_then_reconcile_yields_zero_diff` **不是**第二个受害者。
逐行读过 `scripts/reconcile_milvus_id_mapping.py:112-166`：
- `pg_by_key` 是在 `reconcile()` 开头从 PG 读的快照，而该用例先 `await backfill(dbSession)`
  ⇒ 回填写下的占位行**已经 commit** ⇒ `pg_by_key[("class","5001")]` 存在；
- `milvus_seen_keys` 是 `set` ⇒ 同一 key 出现两次会**折叠**；
- 且 `reconcile` 的 key 是 `(type, ontology_id)`，**不看 `external_id`** ⇒ 旧行残留也命中同一条 PG 记录
  ⇒ `unified_id` 相等 ⇒ 走「双侧一致 → 通过」分支，不计入 `rows`。
⇒ `diff_count` 恒为 0 ⇒ 该用例**不红**。旁证：白名单里从头到尾只有
`test_backfill_is_idempotent` 这一条，从未登记过第二个。

### 5. 顺带发现的第二个缺陷：重插的**内容**可能取自「另一行」

⚠️ 早先版本把这条写成「delete 打的不是那一行」——**这是错的**，已按证据更正。

**delete 打的是对的那一行**：Milvus 的 `query()` **总是返回主键**，即使 `id` 不在
`output_fields` 里（服务端在 `fields_data` 里始终带上 PK，见 pymilvus
`grpc_handler.py:2381` / `abstract.py:445`）。
决定性旁证：2026-11-05 的 CI 日志里 `assert written2 == 0` **实得 1**
（`Harness/changes/2026-11-05-defect-priority-plan/summary.md:334`），而那次种了
**3** 行 ⇒ 若 delete 从未生效，第二跑会看到 3 行待回填、`written2` 应该是 3；
实测是 1 ⇒ **3 次 delete 里有 2 次已经可见，只有 1 次还卡在窗口内**
⇒ delete 确实发了，且打在了各自那一行上。
⇒ **抖动 100% 是可见性竞态**，不是「删错行」。

**真正取错的是重插的内容**：脚本用 `by_oid`（按 `ontology_id` 建索引）取待重插的行：

```python
auto_id = r.get("id")           # ← delete 用它，打在正确的行上
full = by_oid.get(ontology_id)  # ← 重插内容用它 ⇒ 同 ontology_id 有多行时取到「另一行」
```

一旦集合里同一个 `ontology_id` 有两行（正是上面那种「残留重复行」状态），
`by_oid` 会把**另一条**行的 name/alias/embedding 插成新行 ⇒ 内容被悄悄换掉。
⇒ 本批一并修掉：**待重插的行必须按「需要回填的那一行自己的 auto-id」定位**。

---

## 二、影响面

| 项 | 值 |
|---|---|
| 直接受害者 | `app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`（白名单已登记）—— **本文件唯一的受害者**（§1.4 已复核） |
| 生产影响 | 运营在**可见性窗口内**重跑回填脚本 ⇒ 旧行被当成「还要回填」⇒ 删一行 + 再插一行 ⇒ **重复向量**（与 2026-09-30 事故同机制） |
| 内容正确性影响 | 集合里同一 `ontology_id` 已有重复行时，重插的**内容**取自另一行 ⇒ 内容被悄悄换掉（§1.5） |
| 波及面 | 只在「回填脚本被连续跑两次」时触发；单次运行不产生重复 |
| 本批不波及 | `deleteByOntologyIdDual`（在线本体更新路径，同一 delete 语义）—— 见 §五「不做的事」 |

---

## 三、方案候选与取舍

| 候选 | 做法 | 取舍 |
|---|---|---|
| **A. release + load（选定）** | 写完后强制 QueryNode 重装 segment + delta binlog | ✅ 仓库已实测有效（~2.3s）；✅ 确定性；❌ 这 2-3s 内该集合**未加载** ⇒ 针对它的检索拿不到正常结果 |
| B. 只 flush | 现有行为 | ❌ 实测无效（2026-09-27） |
| C. per-request `consistency_level="Strong"` | 查询侧强一致 | ❌ 实测被 Bounded 5s 窗口覆盖 |
| D. 有界轮询等收敛 | 反复扫直到旧行消失 | ❌ 依赖传播时延上界（未知）；且**不刷新** ⇒ 线上仍在 Bounded 窗口内读到残留 |
| E. drop + 重建集合 | 照抄 cleanup 的收敛策略 | ❌ **危险**：drop 与重插之间集合是空的 ⇒ 脚本崩在中间 = 线上向量清空（2026-09-30 事故原样重演）；只适合「以 PG 为真源整体重建」 |
| F. 只修测试（测试里调 refresh helper） | 脚本不动，测试里 release+load | ❌ 只让测试绿，**生产的假幂等契约仍在** ⇒ 重复向量风险不解 ⇒ 白名单条目退休了但问题还在 |

**选定 A**，并在其之上加一道**复扫校验**：刷新后仍有 `external_id` 为空的行 ⇒ **抛错**，
不返回可能撒谎的 `written`。

**A 的代价如何接受**：这 2-3s 的未加载窗口只出现在「一次性维护脚本执行期间」，
而脚本本来就在改这个集合（delete+insert）；同部署里**单条 delete+insert+flush 就要 10-25s**
（`scripts/backfill_milvus_embeddings.py` 注释实测）⇒ 2-3s 是同量级的小头。
⇒ 必须在脚本 docstring 里写明这个窗口。

---

## 四、实施步骤（TDD：RED → GREEN → IMPROVE）

### Task 1 — `milvus_client.refreshCollection` 原语 + 两个既有 helper 收敛到它

- `backend/app/infrastructure/milvus_client.py`
  - 新增 `refreshCollection(collection: Collection, *, waitForLoad: bool = True) -> None`：
    `release()` → `load()` → `utility.wait_for_loading_complete(..., timeout=60)`。
    ⚠️ **超时/异常必须向上抛**（刷新没成功必须让调用方知道，绝不静默继续）——
    脚本的复扫校验依赖「刷新确实发生且完成」这个前提，静默吞掉异常会让复扫变成空转。
  - `refreshWikiCollection()` / `refreshDocumentCollection()` 改为调用它（DRY），但
    **显式传 `waitForLoad=False`** ⇒ **行为与改动前完全一致**（这两个 helper 原本
    只做 release+load、不等装载完成；既有集成路径的时序不得被本次重构改变）。
- 守卫：`backend/app/tests/unit/test_milvus_refresh_collection.py` 钉死
  **调用顺序 = release → load → wait**（wait 必须在 load 之后：先等后装等于白等），
  `wait_for_loading_complete` 用 collection 名 + alias + 非空超时，
  **默认是阻塞版**，且**两个既有 helper 保持非阻塞**。

### Task 2 — 脚本重写（核心）

`backend/scripts/backfill_milvus_external_id.py`：

1. **扫描带 `id`**：新增 metadata 投影（`id, ontology_id, type, name, alias, description, external_id`），
   找出 `external_id` 为空的行 —— 带着它们自己的 auto-id。
2. **按行自身的 auto-id 定位**：full 读（含 `embedding`）结果按 **`id`** 建索引（不再按 `ontology_id`），
   ⇒ delete 打在「需要回填的那一行」上，**重插的内容也取自同一行**。
3. **行在两次读之间消失 ⇒ 跳过**（不拿空 name/embedding 去插一行残缺数据）；
   `id` / `ontology_id` 不可用 ⇒ 跳过并计入 `skipped`（CLI 退出码 1 ⇒ 不谎报成功）。
4. **PG 先落盘（commit）→ 再做 Milvus 变更**：Milvus 的写不可回滚 ⇒ 占位行必须先持久化。
5. **delete → insert → flush → release+load → 复扫校验**。
6. **复扫校验**（三条后置条件，缺一即 `RuntimeError`，绝不返回可能撒谎的 `written`）：
   ① `insert` 返回条数 == 预期行数；② 被删的旧 auto-id 已不可见；
   ③ 新插入的行已可见，且**按插入顺序逐行**带本次期望的 `external_id`
   （不用 uid 集合差集：同一 `ontology_id` 本就可有多行，集合差集分不清「哪一行是新行」）。
7. **`query(limit=)` 撞上限 ⇒ 抛错**：Milvus 会**静默截断**（不报错）⇒ 读到的不是全量时
   被截掉的行既不会被回填、也不会被复扫校验看到 ⇒ 契约成了空话。
8. 文档串改写：把「幂等」从**断言**改成**有机制保证的契约**，并写清 2-3s 未加载窗口
   （该窗口内针对该集合的检索**拿不到正常结果** ⇒ 必须在维护窗口执行）。

### Task 3 — 集成测试补一条「直接钉住后置条件」

`backend/app/tests/integration/test_reconcile_milvus_id_mapping.py` 追加：
回填后**扫不到任何 `external_id` 为空的行**，且**本次种入的** `ontology_id`
（5001/5002/5003）每个只剩一行（比 `written2 == 0` 更直接地钉住可见性契约）。
⚠️ 去重断言**只针对本次种入的 ontology_id**：脚本的职责是「一行换一行」，
**不做**跨行去重（§五）⇒ 断言范围一旦扩到集合里所有行，就会因为脚本承诺之外的
历史残留而误红。

⚠️ 改这个文件 ⇒ B7 规则④生效 ⇒ 白名单里那条对**本 PR 不再豁免**
⇒ `test_backfill_is_idempotent` 照常计入判据 ⇒ **PR-A 的闸门结果就是修复是否生效的证据**。

### Task 4 — PR-B：退休白名单那条（独立 PR）

`.github/ci-known-flaky-tests.json` 删掉 `test_backfill_is_idempotent` 那条
（文件保留、`entries` 变空数组 —— **文件不能删**：`loadFlakeWhitelist` 读不到文件 ⇒
`FlakeWhitelistError` ⇒ 闸门退出码 3「失败关闭」）。
⚠️ 该文件属 CI 相关面 ⇒ 闸门必判 `CI_SURFACE_CHANGED`（退出码 6）⇒ 走 owner 越权口显式合并。

---

## 五、不做的事（明确边界）

- ❌ **不动 `deleteByOntologyIdDual`**（在线本体更新路径）：它也是 `delete + flush`，
  同样有可见性窗口 ⇒ 改完本体后 0~5s 内检索可能拿到旧向量。
  ⇒ 属**产品侧口径**问题（在线路径要不要接受 2-3s 未加载窗口）⇒ 单独立项，本批不做。
- ❌ **不 drop 重建集合**（见 §三 候选 E）。
- ❌ **不做去重**：脚本只保证「每行 external_id 非空」，不保证「每个 ontology_id 只有一行」。
  历史上已经攒下的重复行用 `scripts/backfill_milvus_embeddings.py --cleanup` 收敛。
- ❌ **不在生产上跑回填脚本**（本批只改代码，不执行 ⇒ 零生产写）。
- ❌ **不改白名单工具本身**（`ci_flakes.py` / `ci_surface.py`）—— 本批只退休一条数据。

---

## 六、验证策略（本地**不能**跑 Milvus integration）

- **铁律**：Milvus 19530 是生产容器；仓库的 integration 清理夹具会 drop 集合。
  虽然 2026-09-30 已加库级隔离（`MILVUS_DB_NAME=qa_test` + `guardIsolatedDatabase`），
  本批**仍不在本地跑 Milvus integration 套件** ⇒ **integration 行为只能靠 CI 取证**。
- **本地可跑**：`backend/app/tests/unit/test_backfill_milvus_delete_visibility.py`
  （Milvus 用 fake，**PG 用真实 PostgreSQL** —— `Harness/rules/测试规范.md` 要求数据层必须真实，
  不许用假库代替；PG 侧的失败路径用**真实的 `IntegrityError`** 触发：
  `id_mapping.unified_id` 是主键，而脚本的 `ON CONFLICT (business_object, external_id)` 覆盖不到主键冲突）。
- **生产安全网**（只读快照，跑测试前后各取一次，证明没动生产）：
  `default` 库：`ontology_class_embeddings` 50 / `ontology_property_embeddings` 3743 /
  `ontology_metric_embeddings` 0 / `query_embeddings` 1155 / `wiki_page_embeddings` 64 /
  `document_embeddings` 36（2026-11-07 取样）。
- **CI 取证口径**：PR-A 改了 `test_reconcile_milvus_id_mapping.py` ⇒ 白名单豁免失效
  ⇒ 预期闸门 **退出码 0**（两侧都绿 ⇒ 净新增 0）或 **退出码 5**（基线侧偶发红 ⇒ 净丢失 1
  ⇒ 那正是「抖动消失了」的证据 ⇒ 人工仲裁放行）。**退出码 1（PR 侧红）= 修复没生效。**

---

## 七、PR 拆分与闸门预期

| PR | 内容 | 改动的文件属 CI 相关面？ | 预期退出码 |
|---|---|---|---|
| PR-A | Task 1-3（脚本 + 原语 + 单测 + 集成测试） | 否（`backend/scripts/**`、`backend/app/**`、新单测都**不是** `test_ci_*.py`） | 0 或 5 |
| PR-B | Task 4（退休白名单条目） | **是**（`.github/ci-known-flaky-tests.json`） | **6**（`CI_SURFACE_CHANGED`）⇒ owner 越权口显式合并 |

合并后**不部署**（零 DDL、零生产写；脚本只改不上线执行）。

---

## 八、实施结果（与本计划的偏差，逐条说明）

> 计划是「怎么打算做」，本节是「实际做成了什么、哪里变了、为什么变」。
> 结论与证据的完整版在 [summary.md](./summary.md)。

### 8.1 计划外新增：文件拆分（工程结构硬上限）

计划里 Task 2 写的是「重写 `backend/scripts/backfill_milvus_external_id.py`」。实际实现时发现
初版把编排 + Milvus 原语 + PG 判定 + 写目标闸门塞进**一个文件（941 行）** ⇒ **超 800 行硬上限**
（`Harness/rules/工程结构.md:27`）。⇒ 拆成四个职责单一的模块：

| 文件 | 行数 | 职责 |
|---|---|---|
| `scripts/backfill_milvus_external_id.py` | 470 | 编排与 CLI |
| `scripts/milvus_external_id_apply.py` | 363 | Milvus 侧原语（体检 / delete / insert / 刷新 / 复扫） |
| `scripts/milvus_external_id_placeholder.py` | 221 | PG 侧占位行三态 |
| `scripts/milvus_write_target_guard.py` | 64 | Milvus 写目标闸门 |

测试同样超限：初版 41 条用例在一个文件里（941 行）⇒ 拆成
`_milvus_backfill_support.py`（共享替身，298）+ `test_backfill_milvus_delete_visibility.py`（13 条，308）
+ `test_backfill_milvus_safety_guards.py`（15 条，460）+ `test_milvus_write_target_guard.py`（7 条，71）
+ `test_milvus_refresh_collection.py`（3 条，83）。

### 8.2 计划外新增：写目标闸门

计划 Task 2 没写它。**安全评审把它列为 HIGH**：脚本对连上的 Milvus 库没有「只允许非生产库」的保护
⇒ 直接在生产跑一次就可能写坏生产向量。⇒ 新增 `scripts/milvus_write_target_guard.py`
（默认只允许非生产库；`default` 库须显式 `--allow-prod`；描述函数不泄漏凭据）+ 7 条用例。
同理，CLI 也从「默认写」改成**默认预演 + `--apply` 才写**。

### 8.3 计划外新增：PG 占位行三态

计划只说「PG 写失败 ⇒ 跳过该行」。实际实现时发现「失败」有两种完全不同的含义：
占位行**已存在且一致**（生产常态）vs 槽位**被别人占用**（真冲突 ⇒ 写下去就是悬垂引用）。
⇒ 改为三态 + **回读判定**（不靠「有没有抛异常」判断）。

### 8.4 计划里少写的一条后置条件

计划 Task 2 第 6 条列了**三条**后置条件；实现时发现还差一条 ——
`insert` 返回的 `primary_keys` **条数**必须也对得上。条数对得上但主键列表被截断时，
后置条件就配不出「哪一行是新行」⇒ 只能整体拒绝。⇒ 改为**四条**。

### 8.5 计划里的复扫校验多了一个「不变量」被删掉

实现 `VisibilityGap` 时额外加了「期望的 uid 是否可见」这一项。**变异取证 M5 证明它是死代码**：
它被 `missingNew ∪ wrongUid` 严格蕴含 ⇒ 永远不会独立触发 ⇒ 删除（并在 docstring 写明为何被蕴含）。
⇒ 「守卫会红」≠「守卫在测你想测的东西」：**守不住的守卫要么补测试，要么删掉，不留着装门面**。

### 8.6 变异取证：22 项全部变红（其中 4 项首轮是绿的）

工具 `/tmp/milvus_backfill_mutation_sweep.py`：备份 → 施加一处变异 → 跑 4 个定向测试文件 →
记录红/绿 → 还原。首轮 22 项里 **4 项没变红**：

| 编号 | 根因 | 处置 |
|---|---|---|
| M5 | 被另外两项**严格蕴含** ⇒ 永不会独立触发 | **删掉该检查**（死代码） |
| M7 | fake 没有「主键列表被截断」的故障模式 ⇒ 守卫无从触发 | 给 fake 加 `primaryKeysCountOverride` + 新增 1 条用例 |
| M13 | 原来那条用例被**另一条**守卫（维度不符）挡住 ⇒ 「embedding 为空」从未被单独验证（**同因掩盖**） | 新增 1 条用例：把集合维度设成未知，使「为空」成为唯一判据 |
| M19 | **变异写错了**：`str(exc)` 本就带 DB 消息 | 变异改成「只落类名」⇒ 变红 |

⇒ 复跑：**22/22 全部变红**，还原后基线绿。完整表见 summary.md §6。

### 8.7 计划里的「验证策略」有一条未达成

计划 §六说本地**不能**跑 Milvus integration —— 仍然成立且未违反。
但计划原本指望「integration 行为由 CI 取证」；**本地已用 38 条定向单测 + 22/22 变异取证**
把可验证的部分全部覆盖 ⇒ CI 只剩「真实 Milvus 容器上的 delete→insert→refresh 端到端」这一段。

### 8.8 生产执行的阻断项（计划里没意识到的）

⚠️ **`business_object` 的大小写 SSOT 未定**：`id_mapping.business_object` 与 Milvus `type` 字段的
取值口径没有单一事实源 ⇒ 占位行的 `(business_object, external_id)` 可能落到另一条槽位上。
⇒ 已写进脚本 docstring 作为**生产执行的阻断项**；本批**不在生产执行**。

### 8.9 差点引入的一处行为变化（提交前消掉）

实现时两个既有 helper（`refreshWikiCollection` / `refreshDocumentCollection`）直接委托给了
`refreshCollection` 的**阻塞**版 ⇒ 它们会比改动前**多等一次装载完成** ⇒
既有集成路径（`test_milvus_wiki_fields.py` / `test_milvus_document_fields.py`）的时序改变
⇒ **本地无法验证**（Milvus 19530 是生产容器）⇒ 有「净新增失败」的风险。
⇒ 改为 `refreshCollection(..., waitForLoad=False)`，两个既有 helper 的语义**逐字节不变**，
回填脚本用默认的阻塞版（那才是「刷新完再复扫」的前提）。
⇒ 补用例 `test_wiki_and_document_helpers_delegate_without_blocking_on_load` 钉死。

### 8.10 PR 拆分（不变）

| PR | 内容 | CI 相关面？ | 预期退出码 |
|---|---|---|---|
| PR-A | Task 1–3（脚本四模块 + 原语 + 38 条单测 + 集成用例 + wiki） | 否 | 0 或 5 |
| PR-B | Task 4（退休白名单条目） | **是**（`.github/ci-known-flaky-tests.json`） | **6** ⇒ owner 越权口显式合并 |
