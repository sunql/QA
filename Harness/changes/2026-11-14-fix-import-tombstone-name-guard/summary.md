# 变更：2026-11-14-fix-import-tombstone-name-guard（删除的类重新导入报「已存在」但在本体管理查不到）

- **日期**：2026-11-14
- **作者**：Claude
- **Phase**：Phase 4 开发（缺陷修复：软删墓碑占名 ⇒ 导入死结）
- **状态**：in-review（待合并；**不部署**）
- **关联变更**：`Harness/changes/2026-11-14-prod-release-1114/`（生产发版 1114 批次，**独立工作流**，本批在其之后）
- **迁移版本**：无（零 DDL、零生产写）
- **分支**：`fix/import-tombstone-name-guard`（base = `d001e38e`）

---

## 1. 需求

**用户报的现象**：「我删除一个类以后，为什么重新导入的时候还是提示存在，但是在本体管理的类里面已经查询不到了。」

用户视角的三个动作构成一个**死结**：

1. 在「本体管理」页删掉一个类 ⇒ 该类**从列表消失**（`valid_to` 非空即被默认过滤）；
2. 重新导入同一批表 ⇒ 预览期**不报冲突**（预览只按 `source_table` 匹配，而删除后源表名可能已变）；
3. 执行期 `createClass` 报「类名 X 已存在」⇒ 而那个占用者**在 UI 上查不到**（默认不显示墓碑）⇒ 用户既不能重导，也找不到要恢复的东西。

⇒ **验收标准**：
1. 预览期就能看出「这个类名被一个已删除的类占着」，不必等到执行期炸；
2. 预览页上**不能勾选**注定失败的行（勾了也导不进去）；
3. 报错文案必须**指名占用者**（墓碑 id + 删除时间 + 下一步怎么走），而不是笼统的「已存在」。

## 2. 设计评审

根因定位（代码 + schema + 实库证据三处对齐）：

| 事实 | 证据 |
|---|---|
| 软删只置 `valid_to = now()`，**行还在** | `backend/app/api/v1/ontology.py` DELETE 路由；`ontology_class.valid_to` 可空 |
| 名字仍被占用 | 唯一约束 `uq_ontology_class_name_version (class_name, version)` —— **只看 class_name，与 source_table 无关** |
| 类列表默认隐藏墓碑 | `listClasses` 默认过滤 `valid_to IS NULL` |
| 预览只按 `source_table` 匹配 | `ImportConflictResolver.detect_conflicts` 旧实现只建 `source_table` 索引 |
| 执行期按 `class_name` 判重 | `ontology_service.createClass` 查 `class_name == dto.class_name`（**任何**行，含墓碑） |

⇒ **两个口径不对称**是死结的机械原因：预览按「表」看，执行按「名」看。
⇒ 实库证据：**开发库**（`qa-postgres` 5433 / `qa_metadata`，本地应用连的那个）`ontology_class`
有 **18 条墓碑**（均 2026-09-19 删除，如 `DWD_CARRIER` id=10、`ODS_BPCARRIER` id=35）
⇒ 重导这些表**逐个**会撞「类名已存在」。
（**测试库** 5434 / `qa_metadata_test` 现在是 **0 条** —— 11-14 的生产数据回填把它
`DELETE + COPY` 成了生产快照；⇒ 本批的集成用例**自己建**类再删再重建，不依赖既有墓碑。）

三个候选方案：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. 三处对齐 + 就地禁勾**（**采用**） | ①预览期按 `class_name` 也匹配一次（活类 / 墓碑都匹配），并用 `name_collision` 区分「占名」与「仅撞表」②前端**只禁勾占名**的行并就地标注占用者 ③`createClass` 报错区分「被墓碑占用」 | 三层同时收口：预览看得见、勾不了（且**不误伤**本可成功的导入）、执行期报错可执行 | 不解决「恢复后重导」——恢复后类名又被活类占着，且**无覆盖写入路径**（见 §4 记录） |
| B. 后端放宽：墓碑改名/让 `createClass` 允许重建同名类 | 改唯一约束或软删时改名 | 一步到位，重导无阻 | **改 schema**（唯一约束含 version ⇒ 需 DDL + 迁移 + 存量数据处置）⇒ 超出「小改」范围；且破坏「软删可追溯」语义 |
| C. 只改文案 | 把「已存在」换成「请先在本体管理恢复」 | 改动最小 | 用户仍在预览期看不到任何提示 ⇒ 死结的**入口**没堵上；只能解决「已报错」的情况 |

**决定 A**（用户已确认「好的」= 同意三处都改）。B 留作后续独立立项（要动 DDL）；
C 被 A 覆盖（A 的第③处就是 C）。

**A 不解决的**（写清楚免得后来人误读）：**恢复 ≠ 能重导**。
「显示已删除 → 恢复」把墓碑复活成**活类**（`valid_to` 置空）⇒ 该类名**照样被占**
⇒ 重导仍报「类名已存在」，只是这次占用者**看得见了**（活类列表里能看到）。
真正的重导需要一条**覆盖/改名**路径 —— 属另一件事，本批不做。

## 3. 数据模型变更

**无。** 零 DDL、零迁移、零 alembic 版本。

- 唯一约束 `uq_ontology_class_name_version` **不动**（它是正确的：名字不该被两个版本同时占用）；
- 墓碑数据**不动**（`valid_to` 语义不变）；
- 本批只改**读路径**（预览匹配口径）与**写路径的错误文案**。

## 4. 接口契约变更

### 4.1 HTTP API：**路径与状态码不变**，**错误文案变**

| 端点 | 原来 | 现在 |
|---|---|---|
| `POST /api/v1/ontology/classes`（同名**活类**） | 422 `类名 {name} 已存在` | **一字不变** |
| `POST /api/v1/ontology/classes`（同名**墓碑**） | 422 `类名 {name} 已存在` | 422 `类名 {name} 已被一个已删除的类占用（id={id}，删除于 {when}）：请到「本体管理」页开启「显示已删除」，恢复该类后将其改名以释放该类名（**改名需本体管理员权限**）；或改用其他类名` |
| `PUT /api/v1/ontology/classes/{id}`（改名撞**活类**） | 422 `类名 {name} 已存在` | **一字不变** |
| `PUT /api/v1/ontology/classes/{id}`（改名撞**墓碑**） | 422 `类名 {name} 已存在` | 422 `目标类名 {name} 已被一个已删除的类占用（id={id}，删除于 {when}）：请到「本体管理」页开启「显示已删除」，恢复该类并将它改名以释放类名 {name}（**改名需本体管理员权限**）；或改用其他类名` |
| `PUT /api/v1/ontology/classes/{id}`（**原样回传** `className`，同名墓碑存在） | 422 `类名 {name} 已存在`（**误伤**：用户只想改描述） | **200**（只在**真改名**时判重）⇒ 见下 |

⚠️ **状态码与错误 envelope 一字不变**（422 + `DomainError` → `str(exc) == message`）⇒
前端错误处理路径不受影响；只有文案变了。

⚠️ **文案里必须写全处置路径**：只写「恢复该类」会把用户送进**第二个**死胡同 ——
`restoreClass` 只把 `valid_to` 置空（墓碑复活成**活类**）⇒ 类名**照样被占** ⇒
重导仍报「类名已存在」。真正释放类名要「**恢复 + 改名**」⇒ 两套文案都按此写全。

⚠️ **文案必须点明「改名需本体管理员权限」**（评审 MEDIUM）：处置链上两步的权限
**不同** —— 「恢复」是 owner 部门成员可做（`restoreClass`），「**改名**」是 **admin-only**
（`PUT /ontology/classes/{id}`）⇒ 不点明会把非 admin 用户指引去做**自己做不到**的事。

⚠️ **改名路径用独立文案**（`MSG_CLASS_NAME_OCCUPIED_BY_TOMBSTONE_RENAME`）：
用户正在把类改成 `{name}`，处置动作是「恢复**那个**类并给它改名」；
若复用建类文案（「恢复该类后将其改名」），读起来像让用户把**自己正在改名的类**
再改一次名 ⇒ 绕圈。

⚠️ **同名多行不再炸 500**：原 `scalar_one_or_none()` 在同名多行时抛
`MultipleResultsFound`（未捕获 ⇒ 500）⇒ 改 `.first()` ⇒ 422。

⚠️ **`PUT` 的判重只在「真改名」时执行**（`newName != existing.class_name`）：
本体编辑表单**每次都提交 `className`**（原样回传）⇒ 若不区分，则「活类 X + 同名墓碑 X」
形态下用户只想改描述也会被 422，并被指去做「恢复那个类并改名」——用户**根本没在改名**，
且改不动的**活类**比看不见的墓碑更难办。原样回传不产生新的占名冲突 ⇒ 跳过校验。
**真改名仍拦**（反向守卫用例钉住）。

### 4.2 预览冲突流（`POST /api/v1/datasources/{datasourceId}/import-preview`）：**冲突项语义变**

| 字段 | 原来 | 现在 |
|---|---|---|
| `conflicts[].type` | `class` / `class_tombstoned` | **枚举不变** |
| `conflicts[].source_table` | 恒等于**提议的那张表**（= 冲突落点） | **不变**（base 已是提议表；本批只是新增「按类名命中」的匹配路径，落点口径沿用） |
| `conflicts[].existing_name` | 占用者类名 | 不变 |
| `conflicts[].existing_id` / `existing_valid_to` | 占用者 id / 删除时间 | 不变 |
| `conflicts[].name_collision` | **不存在** | **新增**（`nameCollision`）：`true` = 类名被占（`createClass` 必然拒 ⇒ 前端禁勾）；`false` = 仅源表相同（导入其实能成功 ⇒ 只提示） |

新增的匹配口径（**关键**）：提议类与已有类的**类名相同**（源表不同也算）⇒ 报冲突。
理由：`createClass` 判重只看 `class_name` ⇒ 源表不同**不代表**能建出来。

#### `name_collision` 的三态语义（前端据此分档，**不是**二态）

| 后端返回值 | 含义 | `createClass` 结果 | 前端行为 |
|---|---|---|---|
| `true` | **类名**被占（活类**或**墓碑） | **必然拒**（422） | **禁勾**该行 + 标注占用者（id + 删除时间/活类提示） |
| `false` | 仅**源表**相同（类名没被占） | 成功 | **只提示**，**不禁勾**（禁勾会误伤本可成功的导入） |
| **字段缺失**（`undefined`）或 **`null`** | 后端**未升级**（灰度期）/ 未判定 | 未知 | **不下结论**：不禁勾、**不承诺**「可正常导入」⇒ 文案写「需在执行期确认」 |

⚠️ **活类与墓碑一视同仁**：`name_collision=true` 的活类冲突（`type=class`）
**同样禁勾** —— `createClass` 只按 `class_name` 判重，占住类名的**活类**同样
让这一行在执行期必然失败。若按「冲突类型是不是墓碑」来决定禁勾，会漏掉活类
占名 ⇒ 那一行注定失败却要等失败才知道 ⇒ 与本批「预览期就把死结摊开」的目标
自相矛盾。**活类 vs 墓碑的差别只体现在文案**（「已被软删除类占用」vs
「类名已被现有类占用」），不影响禁勾谓词。

⚠️ **前端不得比后端严**：禁勾集合必须**恰好**等于 `name_collision===true`
的行 ⇒ 前端严于后端 = 误伤可成功的导入；前端宽于后端 = 用户白等一轮报错。

### 4.3 记录（本批**不**做的事）

- **无覆盖写入路径**：类的导入没有「覆盖既有类」选项（`conflictResolutions` 恒为 `[]`）⇒
  「恢复墓碑」与「重新导入同名类」**互斥**；想重导只能改类名。
  ⇒ 属**产品决策**，本批不动。
- **不物理清理墓碑**：物理删除墓碑行要按 FK 顺序处理 9 张引用表 ⇒ 高危，
  属运维动作（已另行给出手工 SQL 顺序），不写进代码。
- **不自动改名**：有人建议占用时自动改名（`X` → `X_1`）⇒ **不采纳**：自动改名
  会静默改变用户可见的类名并制造第二个语义混乱的名字 ⇒ 正确做法是**报错 +
  让用户显式选择**（已实现）。

## 5. 实现要点

### ① `backend/app/services/import_conflict_resolver.py`（预览口径对齐）

索引（大小写口径各自明确，统一建立在「活类 ∪ 墓碑」上）：

| 函数 | 索引键 | 大小写 | 说明 |
|---|---|---|---|
| `_indexByTable` | `source_table` | **不敏感** | 与既有 CLASS/PROPERTY 冲突一致 |
| `_indexByName` | `class_name` | **敏感** | 对齐 `createClass` 的等值比较（PG `=`） |

匹配顺序（`_findOccupant`）：

```
name = 提议类名 ; table = 提议源表
occupant = by_name[name]        # 类名优先：真正挡 createClass 的就是它
  ├─ 命中 → 冲突（name_collision=True；活类/墓碑一视同仁）
  └─ 未命中 → occupant = by_table[table]
                ├─ 命中 → 冲突（name_collision=False；类名没被占 ⇒ 导入能成功）
                └─ 未命中 → 无冲突
```

⚠️ **类名优先于源表**：真正挡 `createClass` 的是**占名**那个；只撞表的占用者未必挡路。

⚠️ **活类优先于墓碑**：同名同时存在活类 + 墓碑 ⇒ 只报一条冲突
（否则前端同表两条冲突，无法消歧）；活类是用户**看得见**的占用者（能改名/能删除）。

⚠️ **占用者选取必须确定且与 `createClass` 同 id**：排序键
`_occupantOrderKey` = `valid_to ASC NULLS FIRST → version DESC → id ASC`
（活类优先；多个墓碑取**最早删除**的那个），
`createClass` / `updateClass` 的 `ORDER BY` 用**同一套** ⇒ 前端提示的
「去恢复 id=N」不会与执行期报错指向不同行。

⚠️ **时区安全**（`_asUtc`）：DB 列是 `timestamptz`（asyncpg 返回 aware），
但排序键里一旦混入 naive 值，naive vs aware 的 datetime 比较会抛
`TypeError: can't compare offset-naive and offset-aware datetimes`
⇒ **整个预览 500**。⇒ naive 一律按 UTC 归一（与 PG 对 timestamptz 的口径一致），
`None` 归一到 `datetime.min(UTC)`。

### ② `backend/app/domain/schemas.py`（冲突项新增 `name_collision`）

`ImportConflict` 新增 `name_collision: bool | None = None` ⇒ 序列化后为 `nameCollision`。
语义（**三态**，与前端分档一一对应）：

| 值 | 含义 | 前端行为 |
|---|---|---|
| `true` | **类名**被占（活类**或**墓碑）⇒ `createClass` 必然拒 | **禁勾** + 标注占用者 |
| `false` | 仅**源表**相同 ⇒ 类名没被占 ⇒ 导入其实能成功 | 只提示，**不禁勾** |
| `null` / 字段缺失 | **未判定**（灰度期 / producer 忘填） | 第三态：既不禁勾，也**不承诺**「可正常导入」 |

⚠️ 后端**按契约填值**（预览期一定会判定：命中类名 ⇒ `true`，命中源表 ⇒ `false`），
但 schema 允许 `null` ⇒ 前端必须能吃下「没有值」⇒ 两端口径**刻意不同**：
后端按语义填，前端按「有没有值」分三档。
⚠️ 默认值必须是 `None` 而不是 `False`：若默认 `False`，后端漏填时前端会**误判**
「类名没被占 ⇒ 可正常导入」⇒ 假承诺。

### ③ `backend/app/services/messages_zh.py` + `ontology_service.py`（报错指名占用者）

- 新增 `MSG_CLASS_NAME_OCCUPIED_BY_TOMBSTONE` 与
  `MSG_CLASS_NAME_OCCUPIED_BY_TOMBSTONE_RENAME`（与既有 `MSG_CLASS_NAME_EXISTS` 并列）；
- 新增模块级纯函数 `_classNameConflictError(occupant, name, *, renaming=False)`：
  `occupant.valid_to is not None` ⇒ 墓碑文案（改名路径用 RENAME 版）；否则 ⇒ 原文案；
- 新增 `NamedTuple _ClassOccupant(id, class_name, valid_to)`：
  判重只 `select` 这 **3 列**，不加载整个实体（否则连带拉起 selectin 关系）；
- `createClass` / `updateClass` 两处判重**都改用该函数** ⇒ 口径一致；
- 查重从 `scalar_one_or_none()` 改成 `.first()`：
  ⚠️ 同名多行时 `scalar_one_or_none()` 抛 `MultipleResultsFound` ⇒ **500**；
  `.first()` ⇒ 正常 422（**报错而非炸**）；
- 判重排序补 `valid_to ASC NULLS FIRST → version DESC → id ASC` ⇒ 同名多行时
  结果**确定**（无序 `first()` 是任意的），且与 resolver 口径一致。

### ④ `frontend/src/components/localImport/PreviewStep.tsx`（禁勾 + 就地标注）

**三态模型**（`collisionState`）：`name`（禁勾）/ `table`（只提示）/ `unknown`（不下结论）。

**冲突 → 行 的映射必须是精确匹配**（不能用小写折叠）：

| 索引 | 键 | 用途 |
|---|---|---|
| `conflictByTable` | 冲突的 `sourceTable`（= **提议的那张表**，精确） | 主路径 |
| `conflictByProposedName` | 冲突的 `proposedName`（= 提议类名，精确） | 兜底（提议表名为空时） |

⇒ 表名与类名分属**两个命名空间**、分别索引。若把两者混成**一个小写 key 空间**
再「先到先得」，`T1` 与 `t1` 两张表会互相牵连 ⇒ **本可成功的行被误禁**
（实测：类名匹配是大小写敏感的，两张表只撞表不撞名）。

**禁勾谓词**（`blockedRowKeys`）：只收 `conflict?.nameCollision === true` 的行
⇒ 前端禁勾集合**恰好**等于 `createClass` 的拒绝集合（活类/墓碑一视同仁）。

- 类表 `rowSelection` 加 `getCheckboxProps`：占名 ⇒ `{ disabled: true, title: <原因> }`
  （join 表本来就有 `getCheckboxProps`，类表此前没有 ⇒ 这正是缺口）；
  `title` 按冲突类型分两套文案（墓碑版 / 活类版）⇒ 「已被软删除」不会套到活类头上；
- `rowSelection.onChange` 额外过滤 `blockedRowKeys`（纵深防御：即便集合里有占名键也不提交）；
- 「全选当前筛选」跳过 `blockedRowKeys` 里的行 ⇒ **仅同源表、类名未被占的行照常入选**；
- 新增「状态」列（按 state × isTombstone 分档）：
  占名+墓碑 ⇒ `Tag(warning) 已被软删除类占用` + `墓碑 id={id} · 删除于 {when}`；
  占名+活类 ⇒ `Tag(warning) 类名已被现有类占用` + `占用类 id={id}`；
  仅撞表+墓碑 ⇒ `Tag(default) 同源表有软删除类`（**不阻断**）；
  仅撞表+活类 ⇒ `—`（沿用既有行为，不提示）；
  unknown+墓碑 ⇒ `Tag(default)` + 「需在执行期确认」；
  unknown+活类 ⇒ `Tag(default) 与现有类同名，待确认`（**不是**「—」：
  活类也可能占名 ⇒ 那一行**可能**在执行期 422，装死会让用户看不到任何信号）；
  ⇒ **不依赖 hover** 也能看到关键信息（tooltip 只承载「下一步怎么走」）；
- 顶部 **两个** Alert：
  `tombstoned-alert`（墓碑冲突）+ `name-taken-alert`（活类占名）
  ⇒ 两者的用户处境不同，不能混在一张 Alert 里；
- 墓碑 Alert 每项：真占名时**说出占用者**（`占用类名 {name}（id={id}）`）
  —— 冲突落点是「提议的那张表」，只显示该表名会让用户不知道该去恢复**哪个**类；
  ⚠️ **不要**加「占用者类名 ≠ 提议类名」这种条件：`name_collision=true` 的占用者
  **必然**来自按类名命中 ⇒ `occupant.class_name == 提议类名` 恒成立 ⇒ 该分支
  **不可达** ⇒ 配套用例会变成**假绿**（夹具用了后端产不出的数据）；
  ⚠️ **仅撞表时不说「占用类名」**（那与「类名未被占用」自相矛盾）；
  ⚠️ **unknown 时不承诺「可正常导入」**（只能说「需在执行期确认」）；
- 日期格式跟随 i18n locale（`useTranslation().locale`），非法时间戳/非法 locale
  都不炸（`formatWhen` 兜底 `—`）⇒ 不再硬编码 `zh-CN`。

### ⑤ i18n（`zh-CN.ts` + `en-US.ts`，两份同步）

本批在 `localImport.preview` 下**新增 20 个键**、**改写 2 个键**
（该 namespace 由 **17** 个键变为 **37** 个键；数字由脚本对 base/current
两份文件做键集差分实测得出，见 §6）：

- 新增（20）：`tombstonedAlertOccupiedBy` / `tombstonedAlertTableOnly` /
  `tombstonedAlertUnknown` / `tombstoneTag` / `tombstoneRowHint` /
  `tombstoneTooltip` / `tombstoneCheckboxDisabled` / `tombstoneTableOnlyTag` /
  `tombstoneTableOnlyHint` / `tombstoneTableOnlyTooltip` / `tombstoneUnknownTooltip` /
  `nameTakenAlert` / `nameTakenAlertItem` / `nameTakenTag` / `nameTakenRowHint` /
  `nameTakenTooltip` / `nameTakenCheckboxDisabled` /
  `nameTakenUnknownTag` / `nameTakenUnknownTooltip` / `status`；
- 改写（2）：`tombstonedAlert` / `tombstonedAlertItem`。

⚠️ 文案口径修正：「恢复」**不会**释放类名（恢复只是把墓碑复活成活类，名字照样被占）
⇒ 处置路径写全为「**恢复该类后将其改名**以释放该类名」。

⚠️ **权限口径（两步权限不同，两步都要写明）**：处置动作分两步 ——
「**恢复**」= **owner 部门成员或管理员**（`restoreClass` → `assertCanModify`），
「**改名**」= **admin-only**（`PUT /ontology/classes/{id}` → `requireAdmin`）。
⇒ 墓碑相关文案统一写「恢复该类（需 owner 部门成员或管理员）后将其改名
（改名需本体管理员权限）」。只写后一半会让非 admin 用户**照做后在第一步就被 403 挡下**。

⚠️ **活类占名分支同样要点明权限（第二轮安全复审 MEDIUM）**：本批新增的
`nameTakenTooltip` / `nameTakenCheckboxDisabled`（类名被**活类**占用）最初写的是
「请到「本体管理」页将该类**改名或删除**」——而这两步**都是 admin-only**
（`PUT` 与 `DELETE` 均 `Depends(requireAdmin)`）⇒ 文案把非 admin 用户指去做
**自己做不到**的事。已补「（改名/删除需本体管理员权限）」。
**口径**：文案里出现的每一个写操作，都必须与路由上的鉴权依赖一致。

⚠️ **不要把「恢复」写成「释放类名」**：恢复后类名照样被占（活类）⇒ 必须写「恢复 + 改名」。

⚠️ **zh/en 键集一致性由测试守**，不是靠人肉同步：前端测试里有一条
「`localImport.preview` 的 zh 与 en 键集完全相同」的守卫用例
（直接 import `zhCN` / `enUS` 比对）⇒ 漏一个键就红。

### ⑥ 评审后的后端收口（`ontology_service.py` 三处）

| 改动 | 原因 | 口径 |
|---|---|---|
| 新增模块级纯函数 `_isoUtc(value)` | 报错文案里的「删除于 {when}」此前用裸 `isoformat()` ⇒ naive 时间戳输出**不带时区**，与前端按本地时区解析的结果对不上 | naive 一律按 **UTC** 归一，输出**带时区偏移**的 ISO 串（`+00:00`） |
| `updateClass` 的改名撞名校验改为**只在真改名时**触发（`newName != existing.class_name`） | 本体编辑表单**每次都提交 className**（原样回传）⇒ 原实现下「活类 X v2 + 同名墓碑 X v1」这种形态，用户只想改描述也会被拦，并被告知「去恢复那个类并改名」（与本次编辑无关） | 判重只在**类名真的变了**时执行 ⇒ 无关编辑不被误伤 |
| `restoreClass` 的 docstring 订正 | 原 docstring 让人以为 `restoreClass` 能「释放类名」 | 明确写：`restoreClass` 只把 `valid_to` 置空 ⇒ 墓碑复活成**活类** ⇒ 类名**照样被占**；真正的释放路径 = **恢复 + 改名** |

⚠️ `_isoUtc` 与 resolver 里的 `_asUtc` 是**两个**函数、**两处**调用点
（一个格式化输出、一个归一排序），**刻意不合并**：合并会让「排序用的归一」
与「展示用的格式化」互相耦合，改一处容易改坏另一处。

## 6. 测试

### RED 证据（改实现前亲眼看到失败）

| 轮次 | 层 | RED 现象 |
|---|---|---|
| 第一轮 | 后端单测（4 条新用例） | `assert 0 == 1` ⇒ 按类名匹配的墓碑/活类**一条都没报出来** |
| 第一轮 | 后端集成（1 条新用例） | `assert '已删除' in '类名 TombstoneOccupied 已存在'` ⇒ 文案未区分墓碑 |
| 第一轮 | 前端（3 条用例） | ①`expect(checkbox.disabled).toBe(true)` ⇒ 实际 `false`（可勾选）②`getByText("已被软删除类占用")` ⇒ 元素不存在 ③Alert 里找不到占用者类名 `DWD_CUSTOMER_OLD` |
| 第二轮（评审 HIGH） | 后端单测（5 条新用例） | `AttributeError: 'ImportConflict' object has no attribute 'name_collision'` ⇒ 冲突项还不带「占名 vs 仅撞表」的区分 |
| 第二轮 | 前端（2 条新用例） | ①禁勾谓词比 `createClass` 宽 ⇒「仅撞表」的行被误禁 ②Alert 未列占用者类名 |
| 第三轮（确定性） | 后端单测（2 条新用例） | `assert 7 == 8` ⇒ 同名多墓碑时选中的占用者**随输入顺序漂移**，且与 `createClass` 的取行顺序不一致 |
| 第四轮（时区） | 后端单测（1 条新用例） | `TypeError: can't compare offset-naive and offset-aware datetimes` ⇒ 排序键混入 naive `valid_to` ⇒ 预览 500 |
| 第五轮（评审 HIGH 收口：活类/墓碑一视同仁） | 前端（2 条用例） | ①活类占名的行 checkbox 仍 `disabled=false` ⇒ 前端**宽于**后端，那一行注定在执行期失败 ②活类占名不在任何 Alert 里 ⇒ 用户预览期看不到 |
| 第五轮 | 前端（1 条用例） | 大小写不同的两张表（`T1` / `t1`）**互相牵连** ⇒ `t1` 被误禁（本可成功的行被堵死） |
| 第六轮（文案不越界） | 前端（2 条用例） | ①缺 `nameCollision` 时 Alert 承诺「可正常导入」⇒ 灰度期**假承诺** ②仅撞表的行却说「占用类名」⇒ 与「类名未被占用」自相矛盾 |
| 第七轮（评审 MEDIUM-2：原样回传误伤） | 后端集成（1 条新用例） | 把 `updateClass` 的条件临时改回「只要提交了 `class_name` 就校验」（`newName is not None and True`）⇒ `testUpdateClassUnchangedNameIgnoresSameNameTombstone` 失败：`assert 422 == 200` + 报错「目标类名 TwinName 已被一个已删除的类占用（id=7…）……」⇒ 钉住的正是「用户只想改描述却被占名校验拦下」这个误伤 |
| 第七轮 | 前端（2 条用例） | ①Alert 点不出占用者 id ②活类 + `nameCollision` 缺失时渲染 `—`（那一行**可能**在执行期 422，装死等于零信号） |
| 第七轮（评审 MEDIUM-1：假绿用例） | 前端（1 条用例） | 原用例夹具用了 `existingName: "DWD_CUSTOMER_OLD"` ≠ 提议类名 —— 后端**产不出**这种数据（`name_collision=true` 的占用者必然来自按类名命中）⇒ 该用例钉的是一个**不可达分支**；改用后端可产出的夹具后，旧实现（带 `existingName !== proposedName` 条件）直接**渲染不出占用者** ⇒ 红 |

### GREEN 证据（HEAD 实测，2026-11-14）

```
# 后端（resolver 单测，不碰 DB）
$ pytest app/tests/unit/test_import_conflict_resolver_tombstone.py -q
20 passed

# 后端（ontology 集成，真实 PostgreSQL 5434 + 真实 API 链路）
$ pytest app/tests/integration/test_ontology_api.py -q
4 failed, 51 passed      ← 4 failed = **基线既有**（见下）

# 后端（导入预览 API 层：口径经真实 HTTP 入口验证）
$ pytest app/tests/integration/test_local_import.py -q
3 failed, 12 passed      ← 3 failed = **基线既有**（见下）

# 后端（本批改判重 ⇒ 相邻的 ontology 文件不能被带红）
$ pytest app/tests/integration/test_ontology_class_versioning.py \
      app/tests/integration/test_ontology_governance_integration.py -q
2 failed, 20 passed      ← 2 failed = **基线既有**（与基线逐条相同）

# 后端（恢复链路 / 治理字段 / 属性 / 既有 resolver 未被本批破坏）
$ pytest app/tests/unit/test_ontology_governance_fields.py \
      app/tests/unit/test_ontology_property.py \
      app/tests/integration/test_class_restore.py \
      app/tests/services/test_import_conflict_resolver.py -q
30 passed

# 后端（九文件一次性合跑，确认互不干扰 —— 133 passed / 9 failed）
$ pytest <上述 9 个文件> -q
9 failed, 133 passed     ← 9 failed **逐条等于**基线那 9 条

# 前端
$ vitest run src/tests/PreviewStepTombstone.test.tsx
✓ 17 passed
$ vitest run                # 全量
✓ 234 files / 2390 tests passed
$ npx tsc -b
exit 0
```

⚠️ **基线既有失败**（用 `git stash push -- backend/app` 跑基线证明**与本批无关**）：

| 文件 | 基线 | 本批 | 根因 |
|---|---|---|---|
| `test_ontology_api.py` | `4 failed, 48 passed` | `4 failed, 51 passed` | `warmAgentCaches` 预置了 header 本体类：`testListClassesEmpty` / `testUpdateClass` / `testUpdateClassSyncsNeo4j` / `testSyncMissingEmbeddings` |
| `test_local_import.py` | `3 failed, 8 passed` | `3 failed, 12 passed` | 同上：这几条用 `SELECT count(*)` 统计全表，把 seed 的 5 个 header 类/属性也数了进去 ⇒ `assert 6 == 1` / `assert 7 == 2` |
| `test_ontology_class_versioning.py` + `test_ontology_governance_integration.py` | `2 failed, 20 passed` | `2 failed, 20 passed` | 治理字段回填 / 归属数据两条，**失败用例逐条相同** |

⇒ 三处**净新增失败均为 0**；差出来的 **7** 条正是本批新增的集成用例
（`test_ontology_api` +3、`test_local_import` +4）。
⚠️ 这 9 条既有失败**本批不修**（属 0133 header seed 的测试卫生问题，与本主题无关；
在本分支修会与其它批次抢同一批断言 ⇒ 另立变更处理）。

⚠️ **并发跑 pytest 会假红**：两个会话同时打同一测试库（5434）⇒ asyncpg
`DeadlockDetectedError`（peer 事务持有锁）。⇒ 本批的基线取证**串行**执行。

### 覆盖点

| 文件 | 覆盖 |
|---|---|
| `backend/app/tests/unit/test_import_conflict_resolver_tombstone.py`（改，20 条） | 活类/墓碑**按类名**匹配（源表不同也算）/ 墓碑无 `source_table` 仍按名拦 / 活类无 `source_table` 仍按名拦 / 名与表都不撞 ⇒ 无冲突（阴性）/ 同名活类优先于墓碑 / **`name_collision` 的四种取值组合** / 类名命中优先于表命中 / 墓碑占名优先于活类撞表 / **占用者选取与 `createClass` 同序、同 id** / 选取**不随输入顺序漂移** / 同名活类取最高 version / **naive `valid_to` 不炸排序** / **类名匹配大小写敏感**（`X` vs `x` ⇒ 不算占名） |
| `backend/app/tests/integration/test_ontology_api.py`（改，+3） | 建类 → 软删 → **同名再建** ⇒ 422 且文案含「已删除」+ `id={classId}`（用 `id=` 断言，避免日期数字撞出假绿）/ **原样回传 `className` 不被同名墓碑拦**（评审 MEDIUM-2）/ **真改名撞同名墓碑仍 422**（反向守卫：证明上一条不是把校验整个删掉） |
| `backend/app/tests/integration/test_local_import.py`（改，+4） | 预览 API 层：墓碑**占类名** ⇒ `class_tombstoned` + `nameCollision=true`；墓碑**仅撞表** ⇒ `nameCollision=false`；**活类占类名** ⇒ `class` + `nameCollision=true`（+ `existingValidTo is None`）；**预览 `existingId` == `createClass` 422 文案里的 `id=`**（评审 MEDIUM-3：钉住「去恢复 id=N」这条提示不是假线索） |
| `frontend/src/tests/PreviewStepTombstone.test.tsx`（改，17 条） | 墓碑行 checkbox `disabled`（强点也不选中、不进提交集）/ 墓碑行状态标签 + id + 删除时间 / 正常行不误标 / **表头「全选」也绕不过禁勾** / Alert 同时说出「要导的表」与**占用者类名 + id** / **仅撞表的墓碑不禁勾、能进提交集合** / 仅撞表的墓碑行标为非阻断状态 / **活类占名同样禁勾 + 独立标签** / **活类占名进汇总 Alert** / **大小写不同的表不互相牵连** / **缺 `nameCollision` 不承诺「可正常导入」** / **仅撞表不说「占用类名」** / **活类 + `nameCollision` 缺失 ⇒ 中性提示而非 `—`** / **zh-en 键集一致** |

⚠️ 「表头全选也绕不过禁勾」这条是**回归守卫**（实测**首次即绿**：antd 的 select-all
本就排除 `disabled` 行）⇒ 它**不是** RED 驱动的修复，而是把一条**依赖 antd 行为**的
隐含契约钉住 —— 若哪天 antd 改口径，这条会转红。

⚠️ 「仅撞表的墓碑不禁勾」这条是**第二轮评审 HIGH 的 RED**：把实现临时退回
「撞到墓碑就禁勾」的旧谓词后，该用例失败（`expected true to be false`）⇒ 钉住的
正是「前端不得比后端严」这条契约。

⚠️ 「活类占名同样禁勾」这条是**第五轮评审 HIGH 的 RED**：把谓词退回
「只禁墓碑」后，用例失败（`expected true to be false`）⇒ 钉住的是
「前端禁勾集合 = `createClass` 拒绝集合」这条**契约**，而不是实现细节。

### 覆盖率的诚实说明

本批**未跑** `--cov`（后端全量 unit 套件在本仓有 27min/28 红基线，见记忆
`qa-system-backend-unit-suite-hangs-io` ⇒ 全量门禁不可用）。
本批触及的两个后端模块（`import_conflict_resolver` / `ontology_service` 的判重分支）
由**新增用例逐分支钉住**；`ontology_service` 未跑覆盖率 ⇒ **不声称**该模块覆盖率达标。

## 7. 两路评审的发现与仲裁

本批实现完成后跑了 `code-reviewer` 与 `security-reviewer`（按项目规则「写完代码立即审查」）。

### 7.1 code-reviewer（第一轮，结论 **APPROVE**：无 CRITICAL/HIGH）

| ID | 级别 | 发现 | 处置 |
|---|---|---|---|
| MEDIUM-1 | MEDIUM | **不可达分支 + 假绿用例**：前端 Alert 里「占用者类名 ≠ 提议类名」的分支永远进不去 —— `name_collision=true` 的占用者**必然**来自按类名命中（`_indexByName`）⇒ `occupant.class_name == 提议类名` 恒成立；配套用例的夹具（`existingName: "DWD_CUSTOMER_OLD"`）是后端**产不出**的数据 ⇒ 该用例钉的是一个永不执行的代码路径 | **已修**：删掉该条件，Alert 改为输出占用者**类名 + id**（用户据此知道去恢复哪一行）；用例夹具改为后端可产出的形态（`existingName == proposedName`）。RED：旧实现下新夹具**渲染不出**占用者 |
| MEDIUM-2 | MEDIUM | **`updateClass` 误伤**：本体编辑表单每次都提交 `className`（原样回传）⇒ 「活类 X + 同名墓碑 X」形态下，用户只想改描述也被 422 拦下，并被告知「去恢复那个类并改名」——用户根本没在改名 | **已修**：校验改为只在 `newName != existing.class_name`（真改名）时执行。RED：条件临时改回「只要提交了就校验」⇒ 新用例 `assert 422 == 200`。**反向守卫**：另加一条「真改名撞同名墓碑仍 422」，证明不是把校验整个删了 |
| MEDIUM-3 | MEDIUM | **缺守卫**：「去恢复 id=N」这条承诺在代码里没有任何测试钉住预览 `existingId` 与 `createClass` 报错 `id=` 是同一行 | **已修**：`test_local_import.py::test_preview_occupant_id_matches_create_class_error_id`（断言三方相等：预览 id == 报错文案 `id=` == 建类时拿到的 id） |
| LOW-1 | LOW | 后端 `conflicts[].source_table` 的语义若被描述成「变」会与代码矛盾（base 已是提议表） | **已修（文档）**：§4.2 更正为「**不变**」——代码没改落点，本批只是新增「按类名命中」这条匹配路径 |
| LOW-5 | LOW | 后端返回带时区偏移的 UTC ISO，前端按本地时区显示（口径差异） | **不修（记录在案）**：仅影响展示的时区基准，不影响「是哪个墓碑」的判定；改口径会让前端所有时间显示不一致 ⇒ 另立变更统一处理 |
| LOW-7 | LOW | `PreviewStep.tsx` 约 **512** 行 / 组件函数偏长（基线 286 行） | **不修（记录在案）**：建议抽 `CollisionStatusCell` + `CollisionAlerts`；本批先把正确性钉住，拆分会扩大 diff 面 ⇒ 后续清理 |

`code-reviewer` 结论：**APPROVE**（无 CRITICAL/HIGH）；合并前建议处理 MEDIUM-1（不可达分支 +
假绿）与 MEDIUM-2（改名校验误伤），MEDIUM-3 建议顺手补上，否则「去恢复 id=N」这条承诺
在代码里没有任何守卫。**三条均已处理**。

### 7.2 security-reviewer（第一轮 + 第二轮）

| ID | 级别 | 发现 | 处置 |
|---|---|---|---|
| MEDIUM-1 | MEDIUM | **文案分流**：占用类名的处置动作是「恢复 + 改名」，而「改名」是 **admin-only**、「恢复」是 owner 部门成员可做 ⇒ 不点明权限，非 admin 用户会被指引去做**自己做不到**的事 | **已修**：`messages_zh.py` 两套文案 + 前端 `tombstoneTooltip` / `tombstoneCheckboxDisabled` 统一补「（改名需本体管理员权限）」 |
| HIGH-1 | HIGH（**既有面**） | 导入端点（`/datasources/{id}/import`）绕过 admin 写门禁 | **不在本批处理**：`git show d001e38e` 对照确认是既有状态，已登记在记忆 `qa-system-governance-write-gate`（「写面未全收口」）；收口需产品决策「类/属性/导入 REST 是否一律 admin」⇒ 另立变更 |
| MEDIUM-2 | MEDIUM（**既有面**） | `local_import_service.py` 把原始异常串回显给用户 | **不在本批处理**：同上，既有面；本批未新增回显点（新增文案全部是**常量 + 名字/id/时间**，不含异常串） |
| INFO-1 | INFO | 错误文案暴露内部 id | **接受**：id 是**预期暴露** —— 用户必须靠它去「本体管理」定位那一行；不暴露 id 则提示不可执行（本批的核心目标就是让提示可执行） |
| **二轮**-MEDIUM-1 | MEDIUM | **活类占名分支缺权限提示**：`nameTakenTooltip` / `nameTakenCheckboxDisabled`（第一轮修复**之后**新增的分支）只写「改名或删除」，但 `PUT` 与 `DELETE /ontology/classes/{id}` **都是** `Depends(requireAdmin)` ⇒ 与第一轮同类问题，只是换了分支 | **已修**：两份 i18n 均补「（改名/删除需本体管理员权限）」，并在键旁加注释说明「文案里的写操作必须与路由鉴权一致」 |
| **二轮**-LOW-1 | LOW | 墓碑文案只点明「改名需 admin」，未点明「恢复需 owner 部门成员或 admin」 | **已修**：`messages_zh.py` 两套文案 + 前端 `tombstoneTooltip` / `tombstoneCheckboxDisabled` 补「（需 owner 部门成员或管理员）」 |

**第二轮 `security-reviewer` 结论**：**可合并**（无注入、无新增越权、无密钥、无原始 SQL）；
唯一 MEDIUM 是纯文案级权限提示缺失，不构成阻断，建议同批补 2 行文案 —— **已补**。

**自查补充**（第一轮时第二轮尚未跑完；下列各项由我逐项核对，不与评审结论混同）：
本批新增 SQL 全部走 ORM 参数化（`select(...).where(OntologyClass.class_name == newName)`），
无字符串拼接；类名/表名进入文案后由 React 自动转义（无 `dangerouslySetInnerHTML`）；
无新增硬编码密钥 / 凭据；新文案只含**类名、id、时间戳**三类信息。

### 7.3 第二轮复核（实际执行）

第一轮子代理**启动阶段撞 Token 配额 429**（`已达到 Token Plan 用量上限`）⇒ 按
记忆 `subagent-dispatch-explicit-model` 改用**显式 `model: sonnet`** 重派，
第二轮两路评审**均完成并产出结论**。

**第二轮 `code-reviewer`：WARNING（0 CRITICAL / 1 HIGH / 0 MEDIUM / 3 LOW）**

| ID | 级别 | 发现 | 处置 |
|---|---|---|---|
| **二轮**-HIGH-1 | HIGH | `updateClass` 的改名撞名查询从 `scalar_one_or_none()` 改为 `.first()`，而既有单测 `test_governance_extension_acl.py` 的 fake `_Result` **只实现了 `scalar_one_or_none()`** ⇒ `test_update_owner_dept_passes`（传 `className="改名"` ⇒ 走改名校验）必然 `AttributeError` ⇒ **确定性 net-new 失败** | **已修**：`_Result` 补 `first()`（替身接口对齐真 `Result`），并加注释说明为什么两个方法都要有。RED：修前 `1 failed, 10 passed`（正是该用例）；修后 `11 passed` |

复核的**正面结论**（逐条推演，非「没看出问题」）：
「禁勾集合 == `createClass` 拒绝集合」**等价** —— `by_name` 键为**原始 `class_name`
精确匹配**（与 PG 大小写敏感 `=` 一致）；`_occupantOrderKey` 的
`(valid_to is not None, utc(valid_to), -version, id)` 与 SQL 的
`valid_to ASC NULLS FIRST, version DESC, id ASC` **同序**；执行期 `class_name` 恒等于
预览的提议名 ⇒ 不存在漏禁或误禁。

**第二轮 LOW（均记录在案，不修）**：

| ID | 发现 | 处置 |
|---|---|---|
| 二轮-LOW-1 | MEDIUM-3 的守卫只造了**单个**墓碑行 ⇒ 「同名多行时排序口径分叉」只被 resolver 侧单测钉住，**跨组件**那一半没钉住（单行无歧义 ⇒ 该用例仍会绿） | **不修**：resolver 侧已有 `test_tombstone_selection_matches_create_class_order`；跨组件多行场景需直插两行墓碑（同 `class_name` 不同 `version`），属加固项、非阻断 |
| 二轮-LOW-2 | `className: null` ⇒ 跳过校验后仍 `setattr(class_name=None)` ⇒ NOT NULL 500；`""` 会落成空类名（DTO 无 `min_length`） | **不修**：**既有**行为（旧实现同为 `if newName is not None`），本批未引入；硬化另立变更 |
| 二轮-LOW-3 | 前端日期断言（`existingValidTo=2026-09-19T04:18:04Z` ⇒ 断言含 `"9/19"`）依赖运行机时区，美洲负偏移会渲染成 `9/18` | **不修**：CI 为 UTC，本机（UTC+8）均绿；要彻底稳健须固定 `TZ=UTC`，属另立的测试基建项 |

**第二轮 `security-reviewer`：可合并**（无注入、无新增越权、无密钥、无原始 SQL），
唯一 MEDIUM（活类占名分支缺权限提示）**已修**，LOW（恢复权限未点明）**已修** —— 见 §7.2。

**第二轮修复后的回归**（HEAD 实测）：前端全量 **234 files / 2390 tests passed**、
`tsc -b` exit 0；后端 `test_governance_extension_acl.py` **11 passed**、
`test_ontology_api.py` 墓碑三条 **3 passed**。

⚠️ **复审的边界**：两路评审都是**静态阅读 + 定向执行**，不等于全量 CI 结论；
合并前的判据仍是 merge-gate 的「失败集合对差**净新增 0**」。

### 7.4 未采纳的建议

**自动改名**（`X` → `X_1`）以绕过占用 ⇒ **不采纳**：自动改名会静默改变用户可见的
类名，并制造出第二个语义混乱的名字 ⇒ 正确做法是报错 + 让用户显式选择（已实现）。

## 8. 部署验证

**本批不部署**（零 DDL、零生产写、零生产读）。

- 合并进 main 后**需要**一次常规前端 + 后端部署才在生产生效；
- 生产侧**当前就有**该死结的素材：生产 `ontology_class` 里的墓碑（本地已见 18 条，
  生产口径以实际为准）⇒ 重导这些表在**部署前**同样会报「类名已存在」；
- ⚠️ 本批**不**自动清理任何墓碑 ⇒ 生产数据零影响；
- ⚠️ 本批**不**提供「覆盖导入」路径 ⇒ 部署后用户仍需「恢复后改名」或「改类名」才能推进
  （这与部署前的行为一致，只是报错与预览变得可执行 ⇒ **不制造新的不可用态**）；
- ⚠️ **前后端可任意顺序部署**：前端读 `conflicts[].nameCollision`，
  后端未升级时该字段**缺失** ⇒ 前端走**第三态 unknown** ⇒ **不禁勾**
  （退化为旧行为，不会误伤），且**不承诺**「可正常导入」（文案写「需在执行期确认」）；
  后端已升级而前端未升级时 ⇒ 前端拿不到标志 ⇒ 也不禁勾 ⇒ 仍是旧行为。
  ⇒ **无「前端严于后端」的中间态** ⇒ 两批部署顺序任意。

**本地冒烟**（预览口径 + 报错文案）：

```
$ pytest app/tests/integration/test_ontology_api.py::testCreateClassDuplicateAfterSoftDeleteNamesTheTombstone \
         app/tests/integration/test_ontology_api.py::testUpdateClassUnchangedNameIgnoresSameNameTombstone \
         app/tests/integration/test_ontology_api.py::testUpdateClassRenameToTombstoneOccupiedName -q
3 passed
$ pytest app/tests/integration/test_local_import.py::test_import_preview_reports_tombstone_name_collision \
                          app/tests/integration/test_local_import.py::test_import_preview_marks_table_only_tombstone_as_non_blocking \
                          app/tests/integration/test_local_import.py::test_import_preview_marks_live_class_name_collision_as_blocking \
                          app/tests/integration/test_local_import.py::test_preview_occupant_id_matches_create_class_error_id -q
4 passed
$ vitest run src/tests/PreviewStepTombstone.test.tsx
✓ 17 passed
```

⇒ 断言：建类报错 422 + 文案含「已删除」+ `id={classId}`；
原样回传 `className` 不被墓碑拦（200）、真改名仍 422；
预览 `nameCollision` 三档取值正确（墓碑占名=true / 仅撞表=false / 活类占名=true）；
预览 `existingId` == 执行期报错 `id=`。

## 9. 关联

- **分支**：`fix/import-tombstone-name-guard`（base = `d001e38e`）
- **代码**：
  - `backend/app/services/import_conflict_resolver.py`（预览口径对齐类名 + `name_collision` + 确定性选取）
  - `backend/app/domain/schemas.py`（`ImportConflict.name_collision`）
  - `backend/app/services/messages_zh.py`（墓碑专用文案）
  - `backend/app/services/ontology_service.py`（判重报错指名占用者 + 兜底排序 + `_isoUtc` + `updateClass` 只在真改名时校验）
  - `frontend/src/components/localImport/PreviewStep.tsx`（只禁勾占名行 + 状态列 + 两个 Alert）
  - `frontend/src/types/localImport.ts`（`nameCollision?: boolean | null` 三态）
  - `frontend/src/i18n/zh-CN.ts` / `en-US.ts`（新增 **20** 键 + 改写 **2** 键；`localImport.preview` 由 17 → **37** 键，zh/en 键集由测试守）
- **测试**：
  - `backend/app/tests/unit/test_import_conflict_resolver_tombstone.py`
  - `backend/app/tests/integration/test_ontology_api.py`
  - `backend/app/tests/integration/test_local_import.py`
  - `frontend/src/tests/PreviewStepTombstone.test.tsx`
- **MEMORY**：[qa-system-import-tombstone-name-guard](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-import-tombstone-name-guard.md)
- **相关记忆**：
  [qa-system-class-tombstone-restore](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-class-tombstone-restore.md)、
  [qa-system-schema-check-constraints](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-check-constraints.md)
- **规则**：
  [Harness/rules/开发流程规范.md](../../rules/开发流程规范.md)（TDD 与评审流程）、
  [Harness/rules/测试规范.md](../../rules/测试规范.md)（真实 PostgreSQL 集成测试要求）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / 分支）
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A / B / C）+ 明确取舍与**不解决的部分**
- [x] 第 3 段无迁移（零 DDL）
- [x] 第 4 段写清接口契约变化（**含「不变的」**：状态码 / envelope / 枚举）
- [x] 第 6 段有 RED + GREEN 证据，**并诚实标注 9 条既有失败与基线证伪方法**
- [x] 第 7 段填齐（两路评审逐条发现 + 处置 + **第二轮复审 429 中断的诚实记录** + 未采纳建议）
- [x] 第 8 段：部署结论 = 不部署 + 理由 + 冒烟输出
- [x] 第 9 段 ≥ 3 个跨文件链接（代码 / 测试 / MEMORY / 规则）
- [x] MEMORY 索引已加
- [x] 不涉及真实 SQL/DB 改动 ⇒ 无真实数据验证脚本义务
