# 变更：M1-1.1 —— 采购源绑定 service + CRUD API（候选 → 确认流转）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M1 采购源绑定（结构层 `0154` → **行为层：service / API / 读视图**）
- **状态**：done（分支 `feat/procurement-m0`，**未合并、未部署**）
- **关联变更**：[2026-11-18-m1-procurement-binding-tables](../2026-11-18-m1-procurement-binding-tables/summary.md)（M1-1.1 结构层，迁移 `0154`）· [2026-11-18-procurement-m0-questionnaire-intake](../2026-11-18-procurement-m0-questionnaire-intake/summary.md)（G1 汇合，41 类绑定范围）
- **迁移版本**：**无新增**（复用迁移 `0154` 的两张表；`0155` 仍留给 M1-1.2 / A4-0 串行排号）
- **MEMORY**：[qa-system-procurement-v03-landing-program.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-procurement-v03-landing-program.md)

> 本批把迁移 `0154` 的两张表**变成可用的业务能力**：写入口、候选→确认流转、
> 过滤白名单闸门、复合键连续性、软失效，以及**唯一对普通用户开放的生效视图**。

---

## 1. 需求

M1-1.1 结构层（迁移 `0154`）已把两张表和库级不变量建好，但**没有行为层**：
没有入口能写入绑定、没有「候选 → 确认」的流转、没有读路径能安全消费绑定。
G1 汇合已把**绑定范围定为 41 类**，下一步必须让这些绑定可被登记与确认。

验收标准（可测量）：

1. 写入一律落**候选**；候选**不得**出现在普通用户的读视图（D-3）。
2. 确认必须由人显式触发，**确认人取当前登录用户**（不接受客户端传入）。
3. 过滤表达式必须给出白名单，且用到的每个标识符都在白名单内（越权读闸门）。
4. 复合键序号必须从 1 起连续（缺位会让下游把「第 2 键」当「第 1 键」用）。
5. 软失效父绑定 ⇒ 子属性绑定一并失效（不留「父已过期、子还生效」的孤儿映射）。
6. 每次写入都留审计。

---

## 2. 设计评审

### 2.1 候选方案 A：校验逻辑全部写在 API 层

- 做法：路由函数里直接做白名单 / 键序号校验，再调 service 落库。
- 优点：文件少。
- 缺点：**无法在 unit 层断言** —— 一旦掺进 session，测试就退化成「直接调 service」，
  而项目规则明令禁止（`Harness/rules/测试规范.md`）。且校验散在 8 个路由里 ⇒ 漂移风险。
- 结论：**否决**。

### 2.2 候选方案 B：把校验塞进 ORM 事件监听器

- 做法：在 `before_insert` 钩子里校验白名单与键序号。
- 优点：写任何路径都自动生效。
- 缺点：**报错不可读**（`ValidationError` 从 SQLAlchemy 事件里抛出，要穿过 ORM 层）；
  且业务规则写在 ORM 里会让「候选 → 确认」的时序校验（需要读子表）无从下手。
- 结论：**否决**。

### 2.3 候选方案 C（采纳）：纯校验模块 + 服务层编排 + 两档读视图

- 做法：① 纯函数模块 `procurement_binding_validation`（无 IO，unit 可直接断言）；
  ② 服务层 `ProcurementBindingService` 编排校验 + 落库 + 审计；
  ③ API 层只做 HTTP 语义（状态码、camelCase DTO、鉴权）。
- 取舍表：

| 维度 | A（校验在 API） | B（ORM 事件） | **C（纯校验模块 + 服务层）** |
|---|---|---|---|
| unit 层可断言 | ❌ | ❌ | ✅（纯函数断言，条数见 §6） |
| 报错可读性 | ✅ | ❌ | ✅ |
| 时序校验（确认前置） | ❌ | ❌ | ✅ |
| 职责边界 | 路由 = 校验 + 编排 | ORM = 业务规则 | 各层单一职责 |

- **最终决定：方案 C**。

### 2.4 读视图分两档（本批唯一的口径决策）

候选是**未经确认的治理草案**。若普通用户能在列表里看到候选，会把它当结论用
⇒ 全量视图（含候选）**限 admin**；只有「已确认且当前生效」的行对所有登录用户可见。

| 视图 | 过滤条件 | 鉴权 |
|---|---|---|
| `GET /procurement-bindings` | 全量（含候选、含已失效） | **admin** |
| `GET /procurement-bindings/{id}` | 单条详情（含候选） | **admin** |
| `GET /procurement-bindings/effective` | `is_candidate = FALSE AND valid_to IS NULL` | 登录即可 |
| `GET /procurement-bindings/effective/properties` | 自身已确认且生效 **且父已确认且生效** | 登录即可 |

后两条是**唯一对普通用户开放**的视图（读路径的唯一入口）。

### 2.5 审计动作复用（无新迁移）

`AuditService._VALID_ACTIONS` 已含 `CREATE` / `UPDATE` / `DELETE`
⇒ 候选→确认流转复用 **`UPDATE`**（before/after 双端快照），
**不需要为「确认」新增审计动作，也不需要新迁移**。

---

## 3. 数据模型变更

**无。** 本批不改任何表 / 列 / 索引 / CHECK；全部复用迁移 `0154`：

| 表 | 本批用到的列 | 库层不变量（已有） |
|---|---|---|
| `procurement_source_binding` | `class_id` / `datasource_id` / `schema_name` / `source_table` / `binding_role` / `filter_expression` / `filter_allowlist` / `is_candidate` / `confirmed_by` / `confirmed_time` / `valid_from` / `valid_to` / `created_by` | 角色字面量 CHECK · `valid_to > valid_from` CHECK · 候选 ⇔ 确认人 CHECK · 部分唯一索引 `uq_procurement_source_binding_active` |
| `procurement_property_binding` | `source_binding_id` / `property_id` / `source_column` / `key_ordinal` / `transform_expression` / `is_candidate` / `confirmed_by` / `confirmed_time` / `valid_from` / `valid_to` | 键序号 ≥ 1 CHECK · 有效期 CHECK · 候选 CHECK · 部分唯一索引（属性 / 键序号） |

库层负责「**不变量**」，服务层负责「**可用性**」：唯一约束冲突 → `ConflictError`（可读文案），
业务规则不满足 → `ValidationError`（422）。

---

## 4. 接口契约变更

**新增 10 条路由**（前缀 `/api/v1/procurement-bindings`）。JSON 为 camelCase。

| 方法 | 路径 | 鉴权 | 请求体 | 响应 |
|---|---|---|---|---|
| GET | `` | admin | — | `SourceBindingRead[]` |
| GET | `/effective` | 登录 | — | `SourceBindingRead[]` |
| GET | `/effective/properties` | 登录 | — | `PropertyBindingRead[]`（**读路径唯一入口**） |
| GET | `/{id}` | admin | — | `SourceBindingDetail`（绑定 + 属性绑定列表） |
| POST | `` | admin | `SourceBindingCreate` | `SourceBindingRead`（201） |
| POST | `/{id}/properties` | admin | `PropertyBindingCreate` | `PropertyBindingRead`（201） |
| POST | `/{id}/confirm` | admin | — | `SourceBindingRead` |
| POST | `/{id}/expire` | admin | — | `SourceBindingRead` |
| POST | `/{id}/properties/{propertyBindingId}/confirm` | admin | — | `PropertyBindingRead` |
| POST | `/{id}/properties/{propertyBindingId}/expire` | admin | — | `PropertyBindingRead` |

**路由顺序**：`/effective` 与 `/effective/properties` 必须声明在 `/{id}` **之前**，
否则会被 `/{id}` 吃掉并因 `id: int` 解析失败返回 422。

**错误码映射**（`statusForError`，生产 / 测试 app 共用）：

| 情形 | 状态码 |
|---|---|
| 非 admin 调写入口 / 调全量读视图 | 403 |
| 类 / 数据源 / 属性不存在、属性跨类、过滤白名单不合法、**变换表达式不合法**、键序号不连续、**确认时属性未全部确认**、**父已失效时改子绑定**、**actor 超长**、**键序号越 SMALLINT** | 422 |
| 同键位重复、重复确认、重复失效、**父已确认时追加/确认/失效子绑定（冻结）** | 409 |
| 绑定 id 不存在、**路径父 id 与属性绑定归属不符** | 404 |

**确认请求体**：确认动作**不接受请求体**（**不接受客户端传入确认人** ——
确认人取 `CurrentUser.userId`）。DTO 层的 `SourceBindingConfirm` 因无路由使用已删除
（避免死代码误导）。

---

## 5. 实现要点

### 5.1 文件清单

| 文件 | 职责 | 行数 |
|---|---|---|
| `backend/app/services/procurement_binding_validation.py` | **纯校验**（无 IO）：标识符抽取（双引号整体抽取 + `$`）、**三张授权清单**（绑定白名单 / 受控函数 / 结构词禁表）、**恒真谓词闸门**（记号化判定「裸列↔常量」）、过滤与变换表达式闸门、键序号连续性、角色字面量、列名合法性、白名单条目校验（含长度） | 690 |
| `backend/app/services/procurement_binding_service.py` | 编排：查询 / 写入 / 确认 / 失效 + 审计 + 视图辅助（批量取名，避免 N+1）+ **行锁** + **commit 路径翻译** + **父子有效期嵌套** | 783 |
| `backend/app/schemas/procurement_binding.py` | DTO（camelCase）+ DTO 层标识符早失败 + **边界对齐库列宽**（长度上限用共享常量） | 109 |
| `backend/app/api/v1/procurement_bindings.py` | 10 条路由 + ORM → DTO 组装 | 302 |
| `backend/app/tests/unit/test_procurement_binding_validation.py` | **100** 条纯函数断言 | 520 |
| `backend/app/tests/integration/test_procurement_binding_api.py` | **71** 条真实 PG + 完整 API 链路断言 | 1442 |
| `backend/scripts/check_procurement_binding_mutations.py` | **变异反证**（证明测试不是「永远绿」；**判「红的原因」**） | 626 |

### 5.2 三个关键实现细节

**① 过滤白名单：不给白名单 = 拒绝，不是放行。**
过滤表达式将来会被拼进读路径的 WHERE ⇒ 用到白名单外的列 = 越权读。
规则：有表达式 ⇒ 白名单必须非空；表达式用到的**每个列标识符**都要在白名单内（大小写不敏感）；
**字符串字面量整段剥掉**（`STATUS = 'ORDER_QTY'` 不该要求放行 `ORDER_QTY`）；
**函数调用**走独立的受控清单（`SAFE_SCALAR_FUNCTIONS`，**不可自报**）；**SQL 关键字不算**；
**结构词禁表**（`SELECT` / `FROM` / `UNION` … 不许作为标识符出现）；
**禁 `OR`**（否则 `X = 1 OR 1=1` 恒真 ⇒ 行级过滤失效）。
表达式安全性复用数据质量侧的 `validate_expression`（禁 DML / UNION / 注释）。

**② 键序号连续性：库层只保证「不重复」，不保证「1..n 连续」。**
复合主键缺位会让下游把「第 2 键」当「第 1 键」用 ⇒ 服务层在**确认时**统一校验
（`validateKeyOrdinals`），库层部分唯一索引仍兜底。

**③ 软失效时刻：`now()` 是事务开始时刻。**
同事务新建的行 `valid_from` 与 `now()` **完全相同** ⇒ 直接写 `valid_to = now()`
会撞 `valid_to > valid_from` 的 CHECK。解法：失效时刻取
`max(now, valid_from + 1s)`（`_expireAt`），差 1 秒即可绕开。

### 5.3 关键查询口径

`listEffectivePropertyBindings` 是**读路径的唯一入口**：属性绑定必须**自身已确认且生效**，
**且父源绑定已确认且生效** —— 后半个条件防的是「父已过期、子还生效」的孤儿映射
（`expireSourceBinding` 也会主动把子绑定一并置失效，双保险）。

两条父状态谓词的**可观测性不同**（变异反证实测）：

| 谓词 | 可观测性 | 依据 |
|---|---|---|
| 父**必须已确认** | ✅ 可观测 | 子可以先于父被确认（属性确认只要求父仍是候选态）⇒ 父未确认时子不得进入生效视图 |
| 父**必须未失效** | ⚪ **冗余防御**，结构上不可观测 | 父失效时子由**级联失效**一并作废 ⇒ 删掉该谓词行为不变 |

### 5.4 装配与守卫

- 生产装配：`app/main.py`（`createApp()`）；测试装配：`app/tests/_testapp.py`（`buildTestApp()`）。
- 新增路由必须进 `app/tests/integration/test_governance_write_route_auth.py`：
  7 条写路由进 `_WRITE_ROUTES`；`GET /effective` 与 `GET /effective/properties` 进
  `_READ_ROUTES`（**不得**带 admin）；`GET ""` 与 `GET /{id}` 进**新增**的
  `_ADMIN_ONLY_READ_ROUTES`（**必须**带 admin）。
  ⇒ 该守卫直接构建**生产 app** 检查依赖链，能抓到「生产装配漏挂 requireAdmin」而测试 app 照样全绿的结构性盲区。

### 5.5 加固批次（代码评审 + 安全审查后）

评审（code-reviewer + security-reviewer）各报 **2 条 HIGH**（两侧重叠），
本批已全部修完。核心是引入两条统一原则来消解冲突：

**原则一：状态机 —— 确认 = 版本冻结点**

```
候选态（父未确认）──▶ 确认态（父已确认，子集合冻结）──▶ 失效态（只读）
   可增删子、可确认子        只能整体失效（级联子）        不可再写
```

为什么「确认后冻结子集合」：否则可以在**父已确认之后**追加并确认子绑定 ⇒
复合键序号连续性闸门（只在父确认那一刻跑一次）被绕过 ⇒ 生效映射出现缺位键
（`[1, 3]`）⇒ 下游把「第 2 键」当「第 1 键」用。
要改绑必须走「失效现役行 → 建新候选 → 重新确认」。

**原则二：锁序固定为 父 → 子**

所有写路径先 `SELECT ... FOR UPDATE` 锁父源绑定，再锁子属性绑定 ⇒ 不会死锁。
`populate_existing=True` 强制用库里的最新值覆盖 identity map 里的陈旧对象。
没有行锁的话，两个并发确认请求在 READ COMMITTED 下会**同时**读到
`is_candidate = TRUE` ⇒ 双双通过「不能重复确认」的判定 ⇒ 后写者覆盖先写者的确认人。

**九条加固的具体落法**：

| # | 加固项 | 落法 |
|---|---|---|
| 1 | `transform_expression` 无校验 | `validateTransformExpression`：禁 DML/UNION/注释/`;`；**列** ⊆ {本属性 `source_column`}，**函数** ⊆ `SAFE_SCALAR_FUNCTIONS`（21 个受控函数） |
| 2 | 键序号闸门未覆盖属性确认路径 | `confirmPropertyBinding` 重跑 `validateKeyOrdinals` |
| 3 | 键序号连续性按「含已失效」算会误判 | 连续性**按未失效的绑定**算（已失效序号不占位）⇒ 「已失效序号 1 + 生效序号 2」被拒 |
| 4 | `{id}` 路径未校验归属 | 属性确认 / 失效都校验「路径父 id == 实际归属」，不符 ⇒ 404 |
| 5 | 级联失效缺审计 | 每个被级联失效的子行写 `UPDATE` 审计（改了行就要留痕） |
| 6 | DB 异常翻译 | `IntegrityError` ⇒ 409；其它 `DBAPIError` ⇒ 422（细节只进服务端日志，用户看不到 SQL / 表名 / 参数） |
| 7 | 状态机 | `_assertParentEditable`：父已失效 ⇒ 422；父已确认 ⇒ 409（子集合冻结） |
| 8 | 行锁 | `_lockSourceBinding` / `_lockPropertyBinding`（`FOR UPDATE` + `populate_existing`） |
| 9 | 边界 | 键序号 `le=32767`（库列 SMALLINT）；actor ≤64 字符（库列 String(64)，超长抛 `StringDataRightTruncationError` **不是** IntegrityError ⇒ 未处理就是 500）；白名单项必须标识符且 ≤100 字符（PG `ARRAY(String(100))` **元素超长是静默截断**不报错） |

**标识符抽取的两处加固**：

- **双引号标识符整体抽取**：`"SALARY NET"` 是一个**带空格的列名** ⇒ 按词切分会把它
  拆成 `SALARY` / `NET` 并逐词放行 ⇒ 现在整段抽成一个 token。
- **引号内的关键字仍是标识符**：`"IN"` 是被引号包住的保留字 ⇒ 关键字豁免只对
  未加引号的部分生效。
- **零标识符过滤表达式拒绝**：`1=1` 把行级过滤抹成全表读 ⇒ 与「不给白名单」同属越权面 ⇒ 422。

**不修项（登记为已知风险）**：

| 项 | 理由 |
|---|---|
| stub 鉴权下匿名即 admin | 平台级问题（`DEFAULT_STUB_ROLES = ("user", "admin")`），生产由 `validateProduction()` 硬闸兜住；仓库既有约定是治理写面只收口到 admin 角色（仅审批决策要求 `isRealIdentity`） |
| 唯一索引不允许「已有生效绑定时再提候选变更」 | 候选也占槽位 ⇒ 改需**新迁移**；读路径未上线 ⇒ 无实际风险窗口 ⇒ 本批只改 409 文案 |
| D-3「未确认候选不得被生产读路径消费」目前只在 HTTP 层落地 | NL2SQL 读路径尚未接线（M1-1.2 的活） |

### 5.6 加固批次二（安全审查**第二轮**后）

第一轮加固完成后又跑了一轮安全审查（`security-reviewer` 复审），报
**2 HIGH / 3 MEDIUM / 8 LOW**。本节是第二轮的处置表。

**根因收敛成一句话**：第一轮把「白名单」当成了**一张**清单，而它其实是**三张**：

| 清单 | 授权什么 | 谁来填 | 越权后果 |
|---|---|---|---|
| 绑定白名单 | **列**（数据）—— 行级过滤能引用哪些列 | 写入方自报 | 引用白名单外的列 ⇒ 越权读 |
| `SAFE_SCALAR_FUNCTIONS` | **函数**（代码）—— 表达式能调用哪些函数 | **系统内置，不可自报** | `pg_sleep` ⇒ 阻塞；`UTL_HTTP` ⇒ 外联 |
| 保留字禁表 | **结构词** —— 表达式里不许出现的 SQL 结构 | 系统内置 | 子查询跨表读 / 多语句 |

**为什么函数必须走独立清单**：绑定白名单是「这一列」的授权清单，让它兼任函数清单 ⇒
一个函数名进了白名单就等于**永久授权**。函数是**代码**，列是**数据** ⇒ 分开。

**三条新闸门（过滤表达式与变换表达式同级）**：

| 闸门 | 挡什么 | 落法 |
|---|---|---|
| **禁 `OR`** | `STATUS = 'x' OR 1=1` 恒真 ⇒ 行级过滤被抹平成全表读 | `_assertNoOr`：只在**剥掉字面量与引号标识符之后**的文本上匹配 ⇒ `STATUS = 'A OR B'` 不误杀；顶层与嵌套都禁 |
| **受控函数清单** | `PG_SLEEP(10) > 0`、`DBMS_LOCK.SLEEP(10)`、`UTL_HTTP(...)` | `_assertFunctionsAllowed`：**调用形态**（`NAME(`）的标识符必须 ∈ `SAFE_SCALAR_FUNCTIONS`；普通标识符仍走绑定白名单 |
| **结构词禁表** | `EXISTS (SELECT 1 FROM X)` —— 白名单**自报** `SELECT` / `FROM` 就能塞子查询 | `_assertNoReservedWords`：`EXPRESSION_RESERVED_WORDS`（37 词）不许作为标识符出现；白名单**条目**也不许是保留字 |

**其余六项**：

| 级别 | 发现 | 处置 |
|---|---|---|
| **MEDIUM** | 标识符抽取**不认 `$`** ⇒ `status$1` 被切成 `status` ⇒ 白名单 `STATUS` 放行 ⇒ 稳定的白名单外列绕过通道（而 `status$1` 永远进不了合法白名单 ⇒ 绕过是稳定的） | `_IDENTIFIER_RE` 改含 `$`（抽取认 `$`）；`_COLUMN_RE` 保持**不含** `$`（列名不许带 `$`）⇒ 两张正则必须分开 |
| **MEDIUM** | 6 处裸 `session.commit()`：延迟约束（外键 / 部分唯一索引 / CHECK）在 commit 时才触发 ⇒ 裸 commit 抛 `IntegrityError` ⇒ 用户看到 **500** | 新增 `_commitOrTranslate`：commit 也走翻译器；`IntegrityError` 按 `constraint_name` / SQLSTATE 分流（`uq_*` ⇒ 409；`ck_*` / 外键 / 非空 ⇒ 422） |
| **MEDIUM** | `propertyCountMap` 不过滤 ⇒ **已失效**的属性行会通过「条数」从登录即可访问的 `/effective` 视图侧面泄漏（D-3） | 加 `onlyEffective` 开关；`/effective` 走生效口径（可观测场景：`FILTER` 角色的父不需要属性就能确认 ⇒ 可以先建再失效 ⇒ 父生效但名下属性全是失效行 ⇒ 条数必须为 0） |
| **LOW** | 建属性绑定时不校验键序号**前缀** ⇒ 可先建序号 3 再建 1 ⇒ 中间缺 2 ⇒ 这个源绑定**永远无法被确认**，用户要等到「确认」才撞墙 | 建时就查前缀（口径同连续性闸门：**按未失效的绑定算**）⇒ 错在当场 |
| **LOW** | 「不存在」与「不属于该父」两条不同文案 ⇒ 存在性探测口子 | 两条合并为一句「属性绑定 id=X **不存在或不属于**源绑定 id=Y」 |
| **LOW** | 纯空白 `filter_expression` 落库成空串 ⇒ 「空过滤」与「没有过滤」变成两个状态 | `_normalizeExpression` ⇒ 空白一律归一为 `NULL`（`transform_expression` 同） |
| **LOW** | 约束名会泄漏库表结构 | 约束名**只进服务端日志**；用户只看到归一化提示（唯一约束冲突 / 数据不满足库层约束 / 关联对象不存在） |
| **LOW** | 变异反证没覆盖新闸门 | 变异从 9 条扩到 **16 条**（M11–M16 覆盖 OR 禁 / 受控函数 / 结构词 / `$` 抽取 / 条数口径 / 建时前缀） |

**第二轮不修项**：

| 项 | 理由 |
|---|---|
| 白名单不锚定到**真实数据源列** | 白名单项目前只校验「形状合法」；「这一列在该数据源里真的存在」要连库看 schema ⇒ 属 M1-1.2 读路径落地时的活 |
| 列表接口无分页 | 本批数据量是治理元数据（几十行级）⇒ 先不做；读路径上线前再补 |
| 企业 / 租户隔离 | 本表尚无 `enterprise_id` 维度 ⇒ 需与 M1-1.2 的发布编排一起设计 |
| 引号标识符分支只做纵深防御 | `"SALARY NET"` 这类带空格的列名在真实物理表里极罕见 ⇒ 保留支持但不为其加专门用例 |

### 5.7 加固批次三（代码评审**第三轮**后）

第三轮 `code-reviewer` 终于跑完，报 **2 HIGH / 4 MEDIUM / 2 LOW**。这一轮的问题
不再是个别漏网，而是**「证明力」问题** —— 「守得住」的结论本身可能是假的。

#### HIGH-1：结构词禁表漏 `TABLE`，且 `EXISTS` 的禁令是**死代码**

- **`TABLE` 漏网**：PostgreSQL 的 `TABLE relation` 等价于 `SELECT * FROM relation`
  ⇒ `EXISTS (TABLE other_tbl)` **不出现 SELECT / FROM** 也能跨表读 ⇒ 上一轮的
  「结构词禁表」挡不住它。⇒ `TABLE` / `LATERAL` 进 `EXPRESSION_RESERVED_WORDS`。
- **`EXISTS` 是死代码**：`EXISTS` 原本在 `_SQL_KEYWORDS` 里 ⇒ 被 `extractIdentifiers`
  **提前滤掉** ⇒ `_assertNoReservedWords` 永远看不到它 ⇒ 那条禁令**形同虚设**
  （只有加引号的 `"EXISTS"` 才命中）。⇒ 把 `EXISTS` **移出**关键字表
  （它是结构词，必须能被看见）。⇒ 这就是「库里放一个词之前先问：它会被谁提前吃掉」。

#### HIGH-2：行级过滤仍可被构造成**恒真** ⇒ 禁 `OR` 只堵住一种写法

`OR` 只是恒真的**一种**语法。禁掉 OR 之后，下面这些照样恒真、且引用的都是
**合法白名单内的列** ⇒ 白名单闸门看不出问题：

| 恒真写法 | 为什么恒真 |
|---|---|
| `1 = 1` | 常量与常量（零标识符，已被「必须引用白名单内的列」挡住） |
| `A = A` | 列与列（两侧都不是常量） |
| `ABS(A) >= 0` | 函数值恒非负 |
| `CASE ... ELSE 1 END = 1` | `ELSE` 分支恒为 1（NULL 也走 ELSE） |
| `UPPER(A) = 'X'` | **只有函数比较** ⇒ 与行无关（但这反而不是恒真，是「不可用」） |

**结构性解法**（本批采纳）：过滤表达式必须**至少含一处「裸列 与 常量」的直接比较**。

```
<裸列> <比较符> <常量>          ← 必须至少有一处
以 AND / NOT / 括号连接（OR 禁）
```

为什么这样就**证不出**恒真：顶层只有 `AND` / `NOT`（`OR` 已禁）⇒ 合取式里
只要有一项依赖行 ⇒ **整个合取式依赖行** ⇒ 不可能恒真。这不是「枚举禁法」
（会被下一种写法绕过），而是**语法级不变量**。

- 记号化判定：需要区分「标识符 / 函数名 / 字面量」⇒ 用记号序列判定「比较符的
  紧邻两侧是不是「裸列 + 常量」」（`NAME(` 形态 = 函数名，不是裸列）。
- `CASE` 是 SQL 关键字 ⇒ 不会被标识符闸门看见 ⇒ 走**原文文本**匹配的
  `_assertNoFilterOnlyWords`（只对过滤表达式生效；变换表达式里 `CASE` 只是常量投影）。
- **刻意取舍**：只有函数比较的表达式不被接受（`UPPER(A) = 'X'` ⇒ 422）⇒ 需要
  函数时**另配一处裸列比较**：`STATUS = '有效' AND UPPER(NAME) = 'X'`。

#### MEDIUM-1：白名单长度闸门只住在 DTO 层 ⇒ 非 HTTP 写入方绕得过

PG 对 `ARRAY(String(100))` 的**元素超长是静默截断**（不是报错）⇒ 只在 DTO 层卡
⇒ 导入作业 / 脚本 / 内部服务都能写进超长白名单 ⇒ 「写时校验的白名单」与「落库的
白名单」不是同一个对象。⇒ 长度上限提成**共享常量** `MAX_ALLOWLIST_ITEM_LENGTH`
（DTO 与纯校验层共用，DRY），校验层也卡。

#### MEDIUM-2：`IntegrityError` 的两条分支只有一条被钉住 ⇒ 「CHECK/外键 ⇒ 422」不可证

集成层**造不出** CHECK 约束触发场景（约束不会自然触发）⇒ 该分支此前**无断言**
⇒ 不可证。⇒ 把分流逻辑提成**纯函数** `_classifyIntegrityError`，在 unit 层用
构造的异常对象直接钉住两条分支（唯一 ⇒ 409 / CHECK·外键·非空 ⇒ 422），
并断言**约束名不出现在用户可见文案里**（只进服务端日志）。

#### MEDIUM-3 + MEDIUM-4：变异反证的**结论可能是假的**（本批最关键的一条）

| 问题 | 后果 |
|---|---|
| 脚本只判「测试变红」 | 变异可能让测试因为**导入失败 / 语法错 / 夹具炸 / 另一条断言**而红 ⇒ 与被测不变量无关 ⇒ 「守得住」是假的 |
| 脚本的退出码契约被工具崩溃破坏 | `subprocess.TimeoutExpired` 冒出去 ⇒ Python 退出码 1 ⇒ 而 1 在契约里表示「有变异漏网（存在假绿）」⇒ **工具故障被读成相反的结论** |

处置：
- 每条变异带 `expect` = **预期失败的那条断言的源码文本**（pytest 失败回溯会打出
  这一行）⇒ 输出里必须出现其中之一 ⇒ 否则判「**红得不是地方**」⇒ 工具侧故障（退出码 2）。
- `TimeoutExpired` / `OSError` **捕获**（不再冒泡）⇒ 工具侧故障 ⇒ 退出码 2。
- 退出码契约收紧：**0** = 全捕获 / **1** = 有漏网 / **2** = 工具侧故障（结论不可用）。

#### LOW-1 / LOW-2

| 级别 | 发现 | 处置 |
|---|---|---|
| **LOW** | `_dbErrorHint` 的「not null」分支对 PostgreSQL **永不命中**（PG 的文案是 `null value in column ...`） | 文案补 `null value` / `not-null` 两种形态 |
| **LOW** | 级联失效时子行的 `valid_to` 可能**晚于**父行的 `valid_to`（父子有效期窗口不嵌套） | 父的失效时刻后移到 `max(父原失效时刻, max(子最早合法失效时刻))`；子行写入取 `max(父失效时刻, 子自身最早合法失效时刻)` ⇒ 子恒 ≤ 父 |

#### 本轮附带修的**功能缺口**（评审之外发现的真问题）

共享的 DQ 表达式校验器（`validate_expression`）**只认 ASCII** ⇒ 中文字面量
（`STATUS = '有效'`）一律被拒 ⇒ 而本项目的业务数据是中文 ⇒ 过滤表达式在业务上
**不可用**。⇒ 送检前把**字面量内部**的非 ASCII 字符掩码成占位符
（`_maskNonAsciiInLiterals`）：只动字面量内部 ⇒ 标识符抽取 / 关键字匹配 / 函数抽取
本来就整段剥掉字面量 ⇒ **判定结果不变**，只是让共享校验器接受中文。
引号**之外**的非 ASCII 仍被拒。

---

## 6. 测试

| 验证项 | 方式 | 结果 |
|---|---|---|
| 纯校验层 | `./scripts/run_local_tests.sh app/tests/unit/test_procurement_binding_validation.py` | ✅ **100 passed**（第三轮 +29） |
| ORM 层（已有） | `app/tests/unit/test_procurement_binding_models.py` | ✅ 11 passed |
| API 全链路 | `./scripts/run_local_tests.sh app/tests/integration/test_procurement_binding_api.py` | ✅ **71 passed**（真实 PG 5434 + 完整 HTTP 链路；第三轮 +9） |
| 治理写入口守卫 | `app/tests/integration/test_governance_write_route_auth.py` | ✅ **27 passed**（含 12 条采购绑定路由断言） |
| 校验层 + API 合跑 | 上面第 1、3 行一起 | ✅ **171 passed**（真实 PG 5434；第二/4 行各自单跑 11 / 27 passed） |
| **变异反证** | `backend/.venv/bin/python backend/scripts/check_procurement_binding_mutations.py` | ✅ **exit 0**（25 条中 **24 条可观测变异全部被捕获**，1 条预期漏网） |

**覆盖的行为（71 条集成断言）**：候选落库 · 候选不出现在生效视图 · 全量视图限 admin ·
写入口限 admin · 类 / 数据源不存在 · 非法角色 · 同键位 409 · 过滤表达式无白名单 422 ·
白名单外标识符 422 · 带白名单放行 · 属性绑定创建 · 跨类属性 422 · 重复属性 409 ·
非法列名 422 · 无属性不能确认 · FILTER 角色可零属性确认 · **确认人取当前登录用户** ·
确认后进入生效视图 · 重复确认 409 · **键序号缺位 422** · 失效移出生效视图 ·
**子绑定随父失效** · 重复失效 409 · **失效后同键位可再建（软版本化）** ·
详情含属性绑定 · 未知 id 404 · 写入留审计 · 确认记为 UPDATE 且 before/after 齐全 ·
**确认要求属性全部已确认** · **已失效绑定不能确认** · **父失效时不能挂/确认/失效子绑定** ·
**父已确认时追加子绑定 409（冻结）** · **属性确认重跑键序号闸门** · **跨父确认 404** ·
**跨父失效 404** · **单条属性失效** · **属性重复失效 409** · **父已确认时失效子绑定 409** ·
**级联失效写子行审计** · **变换表达式注入 422** · **本列变换表达式放行** ·
**白名单项非标识符 422** · **白名单项超长 422** · **超长 actor 422（非 500）** ·
**键序号越 SMALLINT 422** · **生效属性映射可见 / 父候选时不可见 / 父失效时不可见** ·
**列表过滤 classId / datasourceId / effectiveOnly** ·
**过滤表达式含 OR 422** · **过滤表达式调未知函数 422** · **过滤表达式含子查询 422** ·
**`$` 列名绕不过白名单 422** · **空白过滤表达式落库为 null** ·
**生效视图属性条数不含已失效行** · **「不存在」与「不属于」同一句 404** ·
**第三轮新增**：`EXISTS (TABLE t)` 422（跨表读绕过）· `CASE ... ELSE 1 END = 1` 422 ·
`STATUS = STATUS` 422 · `UPPER(STATUS) = 'X'` 422 · `ABS(STATUS) >= 0` 422 ·
「函数比较 + 一处裸列比较」**放行** · **级联失效后子 `valid_to` ≤ 父 `valid_to`**。

**变异反证**：`check_procurement_binding_mutations.py` 先断言**基线全绿**，
再逐条注入 **25** 个已知错误变异，要求对应测试**必须变红**，**且红在预期的那条
断言上**（`expect` = 该断言的源码文本 ⇒ pytest 失败回溯会打出这一行）：

| 编号 | 变异 | 捕获 |
|---|---|---|
| M1 | 去掉「父已确认 ⇒ 子集合冻结」检查 | ✅ |
| M2 | 键序号连续性按「含已失效绑定」计算 | ✅ |
| M3 | 去掉「所有属性绑定须已确认」检查 | ✅ |
| M4 | 去掉 actor 长度边界检查 | ✅ |
| M5 | 去掉 transform 表达式闸门 | ✅ |
| M6 | 级联失效不写子行审计 | ✅ |
| M7 | 生效视图不校验「父必须已确认」 | ✅ |
| M8 | 属性确认不校验归属父 id | ✅ |
| M10 | 属性失效不校验归属父 id | ✅ |
| M9 | 生效视图不校验「父必须未失效」 | ℹ️ **预期漏网**（冗余防御，级联失效已保证 ⇒ 结构上不可观测） |
| M11 | 过滤表达式不禁 `OR` | ✅ |
| M12 | 过滤表达式不受受控函数清单约束 | ✅ |
| M13 | 过滤表达式不禁结构词 | ✅ |
| M14 | 标识符抽取不认 `$` | ✅ |
| M15 | 生效视图属性条数不过滤 | ✅ |
| M16 | 建属性绑定时不校验键序号前缀 | ✅ |
| M17 | 过滤表达式不禁 `CASE`（恒真） | ✅ |
| M18 | 过滤表达式不要求「裸列↔常量」比较 | ✅ |
| M19 | 结构词禁表漏 `TABLE` | ✅ |
| M20 | `EXISTS` 被算作 SQL 关键字（禁令成死代码） | ✅ |
| M21 | 白名单长度闸门被去掉 | ✅（unit 层） |
| M22 | 中文字面量掩码失效 | ✅（unit 层） |
| M23 | 级联失效不后移父行失效时刻（有效期不嵌套） | ✅ |
| M24 | `IntegrityError` 一律当冲突 | ✅（unit 层） |
| M25 | 「裸列」判定把 SQL 关键字也算成列 | ✅（unit 层） |

⚠️ **脚本自身的三个坑（都已修）**：
① 锚点**必须全文唯一** —— 先前用两行片段做锚点，结果它先命中了 `listSourceBindings`
   里 `effectiveOnly` 的同名两行 ⇒ 变异落在错误位置 ⇒ 目标测试当然不变红 ⇒
   脚本却报「漏网」⇒ **结论是假的**。现在断言锚点命中数**恰好为 1**，否则报工具侧故障。
② 「变红」必须**命中预期那条测试**（用 `-k` 限定单测），不能靠别的检查项顺便变红。
③ **红了还不够，得红在预期的那条断言上**（第三轮 MEDIUM-3）—— 否则「导入失败 /
   语法错 / 夹具炸」也会被算成「守得住」⇒ 现在每条变异带 `expect`（预期失败断言的
   源码文本）⇒ 输出里没有它就判「红得不是地方」⇒ **工具侧故障（退出码 2）**，
   而不是「捕获」。
   同轮 MEDIUM-4：`TimeoutExpired` / `OSError` **必须捕获** —— 让它冒出去会以
   Python 退出码 1 结束，而 1 在本脚本契约里表示「有变异漏网（存在假绿）」⇒
   **工具故障被读成相反的结论**。

**覆盖率**：服务层由集成测试端到端覆盖（真实 PG + 真实 API 链路），校验层 100% 纯函数覆盖。

---

## 7. 代码评审与安全审查

评审共四轮：

| 轮次 | 评审者 | 结论 | 处置 |
|---|---|---|---|
| 首轮 | code-reviewer + security-reviewer（并行） | 各 2 条 HIGH（重叠） | 合并成 9 项加固清单（§5.5） |
| 第二轮 | security-reviewer（复审加固后的代码） | **2 HIGH / 3 MEDIUM / 8 LOW** | §5.6（三张清单 + 六项处置） |
| 第三轮 | code-reviewer（前两次被 `429 Token Plan 用量上限` 终止，第三次产出） | **2 HIGH / 4 MEDIUM / 2 LOW** | §5.7（恒真谓词 / `TABLE` / `EXISTS` 死代码 / 长度闸门 / 分流可证 / 变异脚本的证明力） |
| 第四轮 | security-reviewer（复审第三轮加固后的代码） | ⚠️ **未产出**（配额未恢复） | 登记为已知缺口（§7.4）⇒ 不能当作「已评审通过」 |

### 7.1 首轮评审发现与处置

| 级别 | 发现 | 处置 | 钉它的测试 |
|---|---|---|---|
| **HIGH** | `transform_expression` 完全没有校验 ⇒ 它会被拼进读路径投影 ⇒ 等价于任意 SQL 注入面 | 新增 `validateTransformExpression`（与过滤表达式同级闸门） | `test_transform_expression_injection_rejected`（M5 反证） |
| **HIGH** | 键序号连续性闸门只在**父确认**那一刻跑 ⇒ 父确认后追加并确认子绑定可绕过 | 引入「确认 = 版本冻结点」+ 属性确认时重跑闸门 | `test_add_property_to_confirmed_source_rejected`（M1）/ `test_confirm_property_revalidates_key_ordinals`（M2） |
| **MEDIUM** | `{id}` 路径未校验属性绑定的实际归属 ⇒ 可跨父确认 | 路径父 id == 实际归属，否则 404 | `test_confirm_property_rejects_foreign_source_id`（M8）/ `test_expire_property_rejects_foreign_source_id`（M10） |
| **MEDIUM** | 级联失效改了子行却没写审计 | 每个被级联失效的子行写 `UPDATE` 审计 | `test_cascade_expire_audits_children`（M6） |
| **MEDIUM** | DB 异常未翻译 ⇒ 越界值 / 超长值直接 500 | `DBAPIError` ⇒ 422 + 服务端日志；`IntegrityError` ⇒ 409 | `test_long_user_id_rejected_not_500`（M4）/ `test_key_ordinal_over_smallint_rejected` |
| **MEDIUM** | 状态机语义不清（父已确认后能否追加子？） | 「确认 = 版本冻结点」统一表述 + `_assertParentEditable` | `test_confirm_source_requires_confirmed_properties`（M3） |
| **MEDIUM** | 确认 / 失效是「读-判-写」⇒ 并发下可双双通过 | 固定锁序（父 → 子）+ `FOR UPDATE` + `populate_existing` | （并发用例未单列，见「已知缺口」） |
| **LOW** | 列表接口的 `classId` / `datasourceId` / `effectiveOnly` 过滤未覆盖 | 补 3 条过滤断言 | `TestListFilters` |
| **LOW** | 生效属性映射无读入口 | 新增 `GET /effective/properties` | `TestEffectivePropertyView`（M7 反证） |

### 7.2 安全审查口径

| 级别 | 发现 | 处置 |
|---|---|---|
| **CRITICAL** | 无 | — |
| **HIGH** | 见 §7.1 的两条 HIGH | 已修（§5.5） |
| **MEDIUM** | 写入口若只要求登录 ⇒ 任何登录用户可改绑定 ⇒ 读路径整体偏移 | 写入口全部 `requireAdmin`；由**生产装配守卫**钉住（`_ADMIN_ONLY_READ_ROUTES`） |
| **MEDIUM** | 确认人若接受客户端传入 ⇒ 可伪造审计 | 确认动作**不接受请求体**；确认人取 `CurrentUser.userId` |
| **MEDIUM** | 过滤表达式含白名单外列 ⇒ 拼进 WHERE 即越权读 | 白名单闸门（不给白名单即拒绝）+ 表达式安全校验（复用 DQ `validate_expression`） |
| **MEDIUM** | 跨类属性绑定 ⇒ 读路径把 A 类属性映射到 B 类表 | 服务层断言属性必须属于该绑定的类 ⇒ 422 |
| **MEDIUM** | 引号标识符按词切分 ⇒ `"SALARY NET"` 被拆成两个 token 逐词放行 | 双引号标识符**整体抽取** |
| **MEDIUM** | 引号内的关键字被豁免 ⇒ `"IN"` 被当关键字放行 | 关键字豁免只对未加引号的部分生效 |
| **MEDIUM** | 零标识符过滤表达式（`1=1`）⇒ 行级过滤被抹成全表读 | 422 |
| **LOW** | 物理列名拼接注入 | DTO 层 `^[A-Za-z_][A-Za-z0-9_]*$` + 服务层 `validateColumnName` 双挡 |
| **LOW** | 白名单项超长 ⇒ PG 数组元素**静默截断**（不报错）⇒ 写时校验的白名单 ≠ 落库的白名单 | 白名单项必须标识符且 ≤100 字符（DTO + 服务层双挡） |
| **LOW** | 失效时刻与 `valid_from` 相等 ⇒ CHECK 拒 | `_expireAt` 取 `max(now, valid_from + 1s)` |

**口令 / DSN**：本批无任何凭据处理；`filter_allowlist` 等列不含敏感信息。
证据文件与本批代码**均不写真实 DSN / 口令**。

**错误信息不泄露内部结构**：`DBAPIError` 的原文（SQL / 表名 / 参数）只进服务端日志，
用户只看到「字段长度超限」「数值超出取值范围」这类归一化提示。

### 7.3 安全审查第二轮（加固后复审）

第二轮把攻击面从「字段是否合法」推到了「**授权面是否分得开**」：

| 级别 | 发现（原文要点） | 处置 | 钉它的测试 |
|---|---|---|---|
| **HIGH** | `validateFilterExpression("STATUS = 'x' OR 1=1", ["STATUS"])` → **PASS**（恒真 ⇒ 行级过滤被抹平成全表读） | `_assertNoOr`（剥掉字面量后再匹配 ⇒ `'A OR B'` 不误杀） | `test_filter_expression_with_or_rejected`（M11） |
| **HIGH** | `EXISTS (SELECT 1 FROM X)` + 白名单自报 `EXISTS/SELECT/FROM/X` → **PASS**；`PG_SLEEP(10) > 0` + 白名单自报 `PG_SLEEP` → **PASS** | 结构词禁表（37 词）+ **受控函数清单**（函数走 `SAFE_SCALAR_FUNCTIONS`，**不可自报**） | `test_filter_expression_with_subquery_rejected`（M13）/ `test_filter_expression_with_unknown_function_rejected`（M12） |
| **MEDIUM** | `extractIdentifiers("col$1") == {"col"}` ⇒ `$` 列被切开后混入白名单 | `_IDENTIFIER_RE` 含 `$`；`_COLUMN_RE` 不含 `$`（两张正则分开） | `test_dollar_column_cannot_bypass_allowlist`（M14） |
| **MEDIUM** | 未包裹的 commit 点：`:291, :337, :382, :431, :460, :501` | `_commitOrTranslate`（6 处全包）+ 按约束名分流 409/422 | `test_long_user_id_rejected_not_500` / `test_key_ordinal_over_smallint_rejected` |
| **MEDIUM** | `propertyCountMap` 无 `valid_to IS NULL` 过滤 ⇒ 登录即可访问的 `/effective` 泄漏候选/已失效条数 | `onlyEffective` 开关 + `/effective` 走生效口径 | `test_effective_view_property_count_excludes_expired`（M15） |
| **LOW** ×8 | 建时键序号前缀未校验 / 「不存在」与「不属于」文案不同（存在性探测）/ 空白表达式落库成空串 / 约束名泄漏库表结构 / 变异没覆盖新闸门 / 白名单不锚定真实列 / 无分页 / 无企业隔离 | 前四项已修（§5.6）；后四项登记为**不修项**（§5.6 表） | — |

**「不可自报」的含义**：`filter_allowlist` 是写入方填的 ⇒ 它只能授权**列**；
函数与结构词由系统内置清单决定 ⇒ 攻击者无法通过「多填几个白名单项」扩大授权面。

### 7.4 代码评审第三轮（加固后复审）

这一轮的问题**不是「又漏了两个洞」**，而是「**守得住**这个结论本身可能是假的」。

| 级别 | 发现（原文要点） | 处置 | 钉它的测试 / 变异 |
|---|---|---|---|
| **HIGH** | 结构词禁表**漏 `TABLE`** ⇒ PostgreSQL 的 `TABLE relation` ≡ `SELECT * FROM relation` ⇒ `EXISTS (TABLE other_tbl)` **不出现 SELECT / FROM** 也能跨表读 | `TABLE` / `LATERAL` 进 `EXPRESSION_RESERVED_WORDS` | `test_filter_expression_with_table_keyword_rejected`（M19） |
| **HIGH** | `EXISTS` 的禁令是**死代码** —— 它在 `_SQL_KEYWORDS` 里 ⇒ 被 `extractIdentifiers` 提前滤掉 ⇒ `_assertNoReservedWords` 永远看不见它 | 把 `EXISTS` **移出**关键字表（结构词必须能被看见） | `test_filter_expression_with_subquery_rejected`（M20） |
| **HIGH** | 行级过滤仍可被构造成**恒真**（`A = A` / `ABS(A) >= 0` / `CASE ... ELSE 1 END = 1` / `UPPER(A) = 'X'`）⇒ 禁 `OR` 只堵住一种写法 | **语法级不变量**：必须至少含一处「裸列 ↔ 常量」比较 ⇒ 顶层只有 AND/NOT ⇒ 合取式必然依赖行 | `test_filter_expression_self_comparison_rejected` 等 4 条（M18） |
| **MEDIUM** | 白名单长度闸门只住在 DTO 层 ⇒ 非 HTTP 写入方（导入作业 / 脚本 / 内部服务）绕得过 ⇒ PG **静默截断** ⇒ 写时校验的白名单 ≠ 落库的白名单 | 长度上限提成**共享常量** `MAX_ALLOWLIST_ITEM_LENGTH`，DTO 与纯校验层共用 | `test_rejects_too_long_item`（M21） |
| **MEDIUM** | `IntegrityError` 分流的两条分支只有一条被钉住 ⇒ 「CHECK / 外键 ⇒ 422」**不可证**（集成层造不出该场景） | 分流提成**纯函数** `_classifyIntegrityError`，unit 层直接钉两条分支；并断言约束名不外泄 | `TestClassifyIntegrityError`（M24） |
| **MEDIUM** | 变异反证**只判「测试变红」**，不判「红的原因对不对」⇒ 「守得住」的结论可能是假的 | 每条变异带 `expect`（预期失败断言的源码文本）⇒ 没有它 ⇒ 判「红得不是地方」⇒ **工具侧故障** | 脚本结构（§5.7） |
| **MEDIUM** | 变异脚本的**退出码契约被工具侧崩溃破坏**（`TimeoutExpired` 冒出去 ⇒ Python 退出码 1 ⇒ 被读成「存在假绿」）⇒ 结论方向反了 | 捕获 `TimeoutExpired` / `OSError` ⇒ 归工具侧故障 ⇒ 退出码 **2** | 脚本结构（§5.7） |
| **LOW** | `_dbErrorHint` 的「not null」分支对 PostgreSQL **永不命中**（PG 文案是 `null value in column ...`） | 文案补 `null value` / `not-null` 形态 | — |
| **LOW** | 级联失效时子行 `valid_to` 可能**晚于**父行 `valid_to`（父子有效期窗口不嵌套） | 父失效时刻后移到 `max(父原失效时刻, max(子最早合法失效时刻))` | `test_cascade_expire_nests_child_validity_within_parent`（M23） |

**这一轮的元教训**：前两轮在补「**洞**」，第三轮在补「**证明力**」——
「守得住」这个结论本身需要被证明，且证明方式本身也会骗人（变异脚本的退出码契约）。
⇒ 变异脚本本身的**退出码语义**（0 / 1 / 2 三态）现在与被测代码一样被当作一等公民对待。

### 7.5 已知缺口（登记，不在本批修）

| 缺口 | 影响 | 处置 |
|---|---|---|
| stub 鉴权下匿名即 admin | 本地 / 测试栈里任何调用方都是 admin | 平台级问题（`DEFAULT_STUB_ROLES`）；生产由 `validateProduction()` 硬闸兜住 ⇒ 生产无此风险 |
| 唯一索引不允许「已有生效绑定时再提候选变更」 | 改绑必须先失效再新建，不能并行提候选 | 需**新迁移**；读路径未上线 ⇒ 无实际风险窗口 ⇒ 本批只改 409 文案 |
| 并发确认 / 失效无专门的并发测试 | 行锁的 TOCTOU 保护只有代码层保证，无用例 | 需能并行压同一行的测试夹具（当前 `client` 夹具串行）⇒ 登记为 M1-1.2 前的补测项 |
| D-3「未确认候选不得被生产读路径消费」只在 HTTP 层落地 | NL2SQL 读路径若直接查库会看到候选 | M1-1.2 的活（本批读入口只有 HTTP 面） |
| **第三轮加固后的代码没有安全审查结论** | 第四轮 security-reviewer 未产出（配额未恢复）⇒ 第三轮的加固**未经安全复审** | 补跑 security-reviewer（配额恢复后）；**不能**当作「已评审通过」 |
| 白名单不锚定到真实数据源列 | 白名单项只校验形状合法，不校验「这一列在该数据源里真的存在」 | M1-1.2 读路径落地时一起做（要连库看 schema） |
| 列表接口无分页 / 无企业隔离 | 治理元数据量小；本表尚无 `enterprise_id` 维度 | 读路径上线前补 |
| `procurement_binding_validation.py` 已 **683 行**（接近 800 行上限） | 表达式记号化 / 恒真判定 / 授权清单三块挤在一个文件 | 下轮把「SQL 表达式闸门」单独拆成 `procurement_binding_sql.py`（纯函数、可独立 unit 覆盖） |

---

## 8. 部署验证

**未部署**（沿用 D-2：生产暂不部署，功能全完成 + 本地全绿后统一发版）。

本批无 DDL（复用迁移 `0154`）⇒ 无 compose 冒烟需求；迁移 `0154` 的发版仍属
「M1-1.2 / A4-0 串行排号」那条线（`0155`）。

**本地真实数据验证**：集成测试跑在真实 PostgreSQL（5434 / `qa_metadata_test`），
覆盖建表约束（部分唯一索引 → 409）、CHECK（有效期方向）、FK（类 / 数据源 / 属性）三层库层不变量。

---

## 9. 关联

- **设计稿 / 计划**：[`docs/update/1118/M0/README.md`](../../../docs/update/1118/M0/README.md) ·
  [`docs/update/1118/智能采购平台_V0.3落地实施计划_执行排期_1118.md`](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_执行排期_1118.md)
- **结构层（本批的前置）**：[`2026-11-18-m1-procurement-binding-tables`](../2026-11-18-m1-procurement-binding-tables/summary.md)
- **G1 汇合（41 类绑定范围）**：[`2026-11-18-procurement-m0-questionnaire-intake`](../2026-11-18-procurement-m0-questionnaire-intake/summary.md)
- **Wiki**：[`Harness/wiki/data-model.md`](../../wiki/data-model.md)（采购源绑定两张表）
- **Rules**：[`Harness/rules/测试规范.md`](../../rules/测试规范.md) · [`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md) · [`Harness/rules/数据与AI治理规范.md`](../../rules/数据与AI治理规范.md)
- **MEMORY**：[`qa-system-procurement-v03-landing-program.md`](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-procurement-v03-landing-program.md)

---

## 附：下一步

1. **前端绑定向导 UI**（本批只做了后端 API；向导是 M1-1.1 的另一半）。
2. **M1-1.2**：`semantic_release_check` 校验 —— 迁移 **`0155`**（`0154` 已占）。
3. **A4-0**：审批面放开 `instance_id` 可空 —— 同样需要 **`0155`** ⇒ **两条须串行排号**。
