# 变更：fix-class-tombstone-restore

- **日期**：2026-10-03
- **作者**：Claude / 启琳
- **状态**：✅ 实现 + 回归全绿 + 已部署真机验证
- **关联变更**：[fix-dim-facility-connectivity](../fix-dim-facility-connectivity/summary.md)
  §7 中"其余 7 个非 ODS 孤岛"已自我纠正 —— 其中 `DWD_BUSINESS_PARTNER` /
  `DWD_CUSTOMER` 是**墓碑不是孤岛活类**；本变更一并接住。

## 0. 落地与验证（2026-10-03 21:5x）

| 项 | 结果 |
|---|---|
| 后端部署 | `./scripts/deploy_backend.sh` ✅（含 alembic 0110 + restore 路由 + 补注册修复） |
| 前端部署 | `docker compose build --no-cache --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend` + `up -d frontend` ✅；nginx reload 后 `/api` 200 |
| **DWD_BUSINESS_PARTNER (id=9)** | `valid_to=NULL` ✅ / id_mapping `obj:CLASS:9` ✅ / Neo4j 节点 ✅ |
| **DWD_CUSTOMER (id=11)** | `valid_to=NULL` ✅ / id_mapping `obj:CLASS:11` ✅ / Neo4j 节点 ✅ |
| 活类总数 | 32 → **34**（经 nginx 实测） |
| 全量列表 | `includeExpired=true` → 52 行，墓碑 18（原 20 − 恢复 2） |

### 落地中发现并修复的新缺陷：`restoreClass` 漏补 id_mapping

**症状**：id=9/11 首次 restore 后 PG 已活、但 Neo4j 无节点、`id_mapping` 无行。

**根因**：`restoreClass` 原写成 `if class_uid_row is not None:` 才 upsert Neo4j——
把「id_mapping 行**必然**存在」当成了前提。但该特性晚于部分老类落库，且历史回填
**只覆盖活类**（活类 32 行 vs 全量 34 类）；墓碑类从来没有行。于是：

- Neo4j 拿不到 `unified_id` → 节点不建 → 知识图谱查不到该类
- 更隐蔽：`updateClass` 的 `resolveByExternal` 返回 None 时抛 `RuntimeError`，
  而它被 `except Exception → _logNeo4jFailure` **best-effort 静默吞掉** ——
  接口照样 200，Neo4j 属性**永远不再同步**。这是一个静默失败。

**修复**：`class_uid_row is None` 时调 `IdMappingService.register(...)` 补行，再 upsert。
`model_dump` 语义与 `createClass` 的注册保持一致（`business_object="CLASS"` /
`external_id=str(entity.id)` / `pg_table="ontology_class"`）。

**测试**（`integration/test_class_restore.py`，8 个）：
- `test_restore_backfills_missing_id_mapping`：删掉 id_mapping → restore → 行补回 + Neo4j 节点在
- `test_restore_legacy_class_then_update_works`：**必须断言 Neo4j alias 已更新**，
  不能只断言 HTTP 200 —— 后者是假绿（RuntimeError 被吞）

> 教训：断言「副作用成功」时，若该副作用包在 best-effort 的 `try/except` 里，
> 那么接口层状态码不构成证据，必须直查被写入的存储。


---

## 1. 需求

「DWD_BUSINESS_PARTNER / DWD_CUSTOMER 这两个对象，我在本体管理的类里面看不到
这两个对象，但是通过本地数据初始化导入这两个对象，却提示我已经存在。」

不是 bug 的**症状**矛盾，而是两处**各自有意**的设计撞在一起：

| 层 | 现状（实测） | 意图 |
|---|---|---|
| `listClasses(includeExpired=False)` | `WHERE valid_to IS NULL`，把墓碑过滤掉 | 默认列表不被尘态 |
| `createClass` 名称占用校验 | 不带 `valid_to` 过滤，任一行的同名都拒绝 | 防止同名双活类（DB 唯一约束 `(class_name, version)` 的兜底） |

⇒ UI 看不到墓碑 → 用户重导 → `createClass` 命中同名墓碑 → `ValidationError(
MSG_CLASS_NAME_EXISTS.format(name="..."))` = 「类名 DWD_BUSINESS_PARTNER 已存在」。

执行路径才报错，预览路径**完全不响**（`detect_conflicts` 拿的是过滤后的
`listClasses`），是体验上的真正缺陷。

### 范围：20 个纯墓碑

```sql
SELECT class_name, count(*) FILTER (WHERE valid_to IS NULL) live,
                count(*) FILTER (WHERE valid_to IS NOT NULL) tombstone
FROM ontology_class GROUP BY class_name HAVING bool_or(valid_to IS NOT NULL);
```

所有受影响类**纯墓碑、无存活同名行、version=1**。其中 18 个 `_LINE` /
DWD 历史样张；用户最关心的是 `DWD_BUSINESS_PARTNER` (id=9) 与
`DWD_CUSTOMER` (id=11)。

---

## 2. 方案（已锁定）

### A. 预览阶段把墓碑冲突显式化

| 改动 | 文件 |
|---|---|
| `ConflictType` 增加 `CLASS_TOMBSTONED = "class_tombstoned"` | `domain/schemas.py:1434-1438` |
| 预览接口查冲突时**同时**取过滤/未过滤两份：`listClasses(session, includeExpired=False)` 给真覆盖用，新增 `listClasses(session, includeExpired=True)` 给墓碑检测用 | `services/local_import_service.py:95` + 调用方 |
| `ImportConflictResolver.detect_conflicts` 新增参数 `existing_classes_all`（含墓碑）；命中 `(source_table, is_tombstoned=True)` 的类报 `CLASS_TOMBSTONED`，复用 `existing_id` / `existing_name` / `existing_valid_to` 字段（在 `ImportConflict` 里新加一个 `existing_valid_to: str \| None`） | `services/import_conflict_resolver.py` + `domain/schemas.py:1441` |
| 前端 `ImportConflict.type` 联合类型加 `"class_tombstoned"`；`PreviewStep` 检测到 tombstoned 渲染一个 `Alert`（不可关闭），提示「以下表名已被同名软删除类占用，请先到本体管理页恢复或确认忽略」，并把对应 `proposedClass.sourceTable` 自动从默认选中集合里剔除 | `types/localImport.ts:78-86` + `components/localImport/PreviewStep.tsx` |

注意：执行阶段**不变** —— 仍由 `createClass` 抛 `MSG_CLASS_NAME_EXISTS`。
前端在预览阶段就取消勾选，用户根本不会触发到这一步。

### B. 加恢复接口 + 本体页"显示已删除"

| 改动 | 文件 |
|---|---|
| `OntologyService.restoreClass(session, id, actor)`：`getClass` → ACL → 已活的类抛 `MSG_CLASS_NOT_EXPIRED` → `valid_to = NULL` → 写 `audit_record("RESTORE", ...)` → `commit` → Neo4j `createNode` 复活（与 `deleteClass` 镜像）；**Milvus 不重灌**（向量 id 一直存在，restore 后仍能召回） | `services/ontology_service.py` 紧邻 `deleteClass:497` |
| 路由 `POST /ontology/classes/{id}/restore` 204；跟 `deleteClass` 一样走 `CurrentUser` + ACL | `api/v1/ontology.py` 紧邻 `deleteClass:170` |
| 前端 `restoreClass(id)` API；`api/ontology.ts` | `api/ontology.ts` 紧邻 `deleteClass` |
| `OntologyPage.tsx` 增加 `includeExpired` 全局开关 state，传给 `ClassTab`；`ClassTab` 自己用该开关拉一次 `listClasses({ includeExpired })` 作为表格数据源（不污染其他 Tab 的 `classes`） | `pages/OntologyPage.tsx` + `components/ontology/ClassTab.tsx` |
| `ClassTab` 列加「状态」Tag（`valid_to != null` → 红色 "已删除"）；**仅在 includeExpired=true 时显示** | 同上 |
| 「恢复」按钮（Popconfirm），仅在墓碑行显示 | 同上 |
| i18n zh/en 各 4 个 key（toggle/restored/restoreConfirm/expiring） | `i18n/zh-CN.ts` + `i18n/en-US.ts` |

#### ACL 复用 `object_owner` 字段

`deleteClass` 已走 owner-based ACL；`restoreClass` 必须同等级别 —— 防止任意用户
复活别人的墓碑。`MSG_CLASS_NOT_EXPIRED` 新增。

### 不在范围（明确划界）

- **不动 createClass 的过滤**：现有的「含墓碑同名拒绝」是**正确**的，DB 唯一约束的语义；
  改它会引入「同名双活类」风险。本次只在**预览期**提示，让用户主动用 B 路径恢复。
- **不做硬删接口**：墓碑是软删的合法形态；硬删需 DDL。
- **不动属性墓碑恢复**：本体类的属性用 `valid_to` 跟随所属类（`createProperty`
  校验所属类不能为墓碑），没有独立的属性墓碑问题。

---

## 3. 全局约束

- **先出方案再改**（本文档即方案）；TDD RED→GREEN；新代码覆盖率 ≥ 80%
- 测试用真实 PostgreSQL（qa-pg-a1，**宿主机连接串必须用 `localhost:5434`**）+ **串行**
- 测试必须写在 `backend/app/tests/`
- **绝不手工跑 `alembic upgrade head`**
- 禁硬编码密钥；token 临时文件用完即删
- 函数 < 50 行；camelCase 命名；显式错误处理；不可变数据
- 改前端必须 `docker compose build --no-cache frontend`
- 禁裸 stash/pop；`git add` 显式路径

---

## 4. 任务分解（逐条独立可验）

### Task 1：A — `CLASS_TOMBSTONED` 冲突检测

**TDD**：
- 新文件 `backend/app/tests/unit/test_import_conflict_resolver_tombstone.py`
- 用例：
  1. 纯活类 → 命中 `CLASS`（旧语义不变）
  2. 纯墓碑类 → 命中 `CLASS_TOMBSTONED`，`existing_id`/`existing_name`/`existing_valid_to` 全填
  3. 同表名双行（活 + 墓碑）→ 优先报 `CLASS`（活的是真冲突）；这是设计选择
  4. 表名匹配大小写不敏感（与现行一致）
  5. 属性冲突不受影响（验证未回归）

**实现**：
- `domain/schemas.py`: `ConflictType` 加 `CLASS_TOMBSTONED`；`ImportConflict` 加 `existing_valid_to: datetime | None`
- `services/import_conflict_resolver.py`: 新签名 `detect_conflicts(..., existing_classes_all)`；新增墓碑检测循环，命中表名时若该行 `valid_to != null` 报 `CLASS_TOMBSTONED`
- `services/local_import_service.py:95` 改成双查：`listClasses(False)` + `listClasses(True)`；属性列表只跟活类走（属性墓碑不存在问题）

### Task 2：A — 前端 PreviewStep 渲染墓碑冲突

**TDD**：
- `frontend/src/tests/PreviewStepTombstone.test.tsx`（新建）
- 用例：
  1. 无冲突 → 无 Alert
  2. 有 tombstoned 冲突 → 渲染 Alert 列表，列出源表 + 「将于 YYYY-MM-DD 软删除」
  3. 初始选中集合**自动剔除**墓碑源表（通过 `useEffect` 在 mount 时设置 `selectedRowKeys`）
  4. 用户手动勾选墓碑源表 → 仍允许（与 `Alert` 文案「请先到本体管理页恢复或确认忽略」一致，不强禁）

**实现**：
- `types/localImport.ts`: `ImportConflict.type` 联合类型 + `existingValidTo?: string | null`
- `components/localImport/PreviewStep.tsx`:
  - `tombstonedConflicts = useMemo(...)`
  - 顶部 `<Alert type="warning" ...>` + `Alert.item` 列出源表名
  - `useEffect`: 初次 mount 时把墓碑源表从 `selectedRowKeys` 中移除
- i18n zh/en 各 1 个 key（`tombstonedAlert` + `tombstonedAlertDesc`）

### Task 3：B — 后端 restoreClass + 路由

**TDD**：
- 新文件 `backend/app/tests/integration/test_class_restore.py`
- 用例：
  1. 软删类 → restore 后 `valid_to IS NULL`；`audit_history` 多一行 `action="RESTORE"`；Neo4j `Class` 节点存在（断言通过 `id_mapping.unified_id`）
  2. 已活类 → restore 抛 `MSG_CLASS_NOT_EXPIRED`（400 类）
  3. 不存在的 id → 404
  4. 非 owner 非 admin → 403（ACL 守卫）
  5. Neo4j/Milvus 不可用时 restore 仍成功（best-effort，与 deleteClass 同语义）
  6. 路由级：DELETE → restore → DELETE 顺序可来回（这是「活 → 死 → 活」完整路径）

**实现**：
- `messages_zh.py`: `MSG_CLASS_NOT_EXPIRED = "类 {id} 未软删除，无需恢复"`
- `services/ontology_service.py`（紧邻 `deleteClass:497`）：
  - `restoreClass(session, id, *, actor)`：与 `deleteClass` 镜像，但方向相反
  - 复用 `self._acl.assertCanModify(...)`、`_audit.record(..., action="RESTORE", ...)`
- `api/v1/ontology.py`（紧邻 `deleteClass:170`）：`@router.post("/classes/{id}/restore", status_code=204)` + `Depends(getCurrentUser)` + 复用 service

### Task 4：B — 前端本体页 includeExpired + 恢复按钮

**TDD**：
- `frontend/src/tests/OntologyPageExpired.test.tsx`（新建）
- 用例：
  1. 默认 includeExpired=false → 表格仅含活类
  2. 切换为 true → 表格多出墓碑行，状态 Tag 为红色「已删除」
  3. 墓碑行展示「恢复」按钮（活类行展示「删除」），互斥
  4. 点恢复 → `restoreClass(id)` 调用 → 成功后刷新列表（墓碑消失 + audit +1）
  5. 切换为 false 不应触发新一轮加载（已加载的「全量」缓存复用，仅前端过滤）
  6. **副作用断言**：`PropertyTab` / `JoinTab` / `MetricTab` / `SemanticRelationTab` 收到的 `classes` prop 仍是**活类**（不被墓碑污染）

**实现**：
- `pages/OntologyPage.tsx`：
  - 新增 `includeExpired` state + `Switch` UI（默认 false）
  - `ClassTab` 改：接受 `includeExpired` + 独立 fetch；展示数据用自己的 state；其他 Tab 仍只收活类
- `components/ontology/ClassTab.tsx`：
  - 接收新 prop `includeExpired: boolean`
  - `useEffect` 监听 `includeExpired`，拉 `listClasses({ includeExpired })`
  - 表格加「状态」列（仅 includeExpired 时展示）：`validTo != null ? <Tag color="red">{t("ontology.statusDeleted")}</Tag> : null`
  - 动作列条件渲染：活类 → 现有「编辑/版本/向量/删除」；墓碑 → 「恢复」按钮 + Popconfirm
  - `handleRestore` = `restoreClass(id)` → `void message.success(t("ontology.restored"))` → 触发自身 reload + 调用 `refreshClasses()`
- `api/ontology.ts`：
  ```ts
  export async function restoreClass(id: number): Promise<void> {
    await httpClient.post(`${BASE}/classes/${id}/restore`);
  }
  ```
- `types/ontology.ts`：`OntologyClass.validTo: string | null` 已存在，无需改
- i18n zh/en：
  - `ontology.showDeleted` / `showDeleted` (en)
  - `ontology.statusDeleted` / `statusDeleted` (en)
  - `ontology.restore` / `restore` (en)
  - `ontology.restoreConfirm` / `restoreConfirm` (en)
  - `ontology.restored` / `restored` (en)

### Task 5：回归与真机验收

1. 后端：
   - 定向：`unit/test_import_conflict_resolver_tombstone.py` + `integration/test_class_restore.py`
   - 已有套件基线 diff：建 worktree HEAD 基线（按记忆 `worktree-symlink-baseline`），跑 unit 全量，对比身份无新增红
2. 前端：
   - 新增：`PreviewStepTombstone.test.tsx` + `OntologyPageExpired.test.tsx`
   - 已有：相关套件绿
   - `npm run build`（含类型检查）
3. 真机：
   - 本体管理页 → 切到「显示已删除」→ 应看到 20 行墓碑 → 点恢复 `DWD_BUSINESS_PARTNER` → 行消失 → 切回「不显示已删除」→ 该类可编辑
   - 本地数据初始化 → 选 `DWD_BUSINESS_PARTNER` → 预览 → 顶部黄色 Alert 列出该表 → 默认未勾选 → 选其他表 → 导入成功

---

## 5. SSOT 收尾

- `Harness/wiki/ontology-page.md`（或最近写入实体增补）：补「墓碑可见 + 恢复」一节
- 记忆：
  - 新建 `qa-system-class-tombstone-restore.md`：墓碑检测在预览期 + 恢复流程要点
  - 更新 `qa-system-neo4j-ontology-empty.md`：restoreClass 也走 Neo4j `createNode`，补双向同步
- `Harness/changes/fix-dim-facility-connectivity/summary.md` §7「其余 7 个非 ODS
  孤岛」列表：`DWD_BUSINESS_PARTNER` / `DWD_CUSTOMER` 划入本变更承接

---

## 6. 风险与已知取舍

| 风险 | 缓解 |
|---|---|
| `restoreClass` 误复活同名双类（与另一活类 class_name 相同） | 不存在 —— 当前 20 个全是纯墓碑；理论上若有「同表名被复活再删又建活」的链式历史，`uq_ontology_class_name_version` 会拦在前；后端 `createClass` 的占名校验也会拦 |
| 复活后 Neo4j 节点已存在 → `createNode` 重复写入 | Neo4j upsert 用 `MERGE`（参考 `deleteClass` 镜像），不存在 duplicate |
| 复活后 Milvus 向量过时 | 接受 —— 用户可手动点「同步单类向量」（ClassTab 已有按钮）；不在范围 |
| `includeExpired=true` 时 ClassTab 的拉取与其他 Tab 的 `listClasses()` 重复请求 | 接受 —— ClassTab 自管；其他 Tab 数据是 live-only 不变 |
| 墓碑类被选为 `parentClassId` | ClassTab 编辑表单的 `parentClassOptions` 用 live `classes` 过滤（现有代码），不会把墓碑作父类 |

## 7. 不在范围（重复确认）

- `createClass` 的占名校验**不**放宽
- 不做硬删
- 不做属性墓碑独立恢复
- 不动 Neo4j 图清理策略