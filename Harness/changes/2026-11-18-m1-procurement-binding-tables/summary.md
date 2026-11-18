# 变更：M1-1.1（结构层）—— 采购源绑定两张表 + ORM + 迁移 0154

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M1 采购语义可发布 / M1-a 结构闸（不卡问卷）
- **状态**：in-review（分支 `feat/procurement-m0`，**未合并、未部署**）
- **关联变更**：[2026-11-18-procurement-m0-source-binding](../2026-11-18-procurement-m0-source-binding/summary.md)（A8，本批的设计来源）
- **迁移版本**：**`0154_procurement_bindings`**（head `0153` → `0154`；两张表同一迁移）
- **验收包**：[A8-采购源绑定候选与表结构设计.md](../../../docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md) §4
- **代码**：`backend/app/domain/procurement_binding_models.py`、`backend/alembic/versions/0154_procurement_bindings.py`
- **测试**：`backend/app/tests/unit/test_procurement_binding_models.py`（新增，11 条）+ `backend/app/tests/integration/test_semantic_release_constraints.py` 末段（12 条）

> 决策 **D2**（两张表 / 独立列）与 **D-3**（候选 ≠ 结论）在本批落到**库层**。
> 本批只做结构层：service / CRUD API / 绑定向导是 M1-1.1 的下一段。

---

## 1. 需求

M1-a 结构闸第 6 条逐字要求：「两张源绑定表（**D2 已定**）建好，迁移 up/down 往返绿」。
本批同时把 A8 文档 §4 的列级设计变成可执行 DDL，并用测试**双向钉住** ORM ↔ 迁移。

---

## 2. 设计评审

### 2.1 迁移号：0154 由本批占用（A4-0 顺延）

原规划把 `0154` 留给 WP-ONT-AI A4-0。落地前核过**所有分支**：`backend/alembic/versions/0154*`
不存在，A4 征集也处于暂缓 ⇒ 按「迁移号串行占号、不许插队」规则，本批占 `0154`；
A4-0 落地时取当时的**下一个空号**。A8 文档与变更记录已同步校正。

### 2.2 关键约束（为什么不是「普通唯一约束」）

| 约束 | 语义 | 为什么必须 |
|---|---|---|
| `uq_procurement_source_binding_active`（**部分**唯一，`valid_to IS NULL`） | 同一 `(class, datasource, schema, table, role)` 只允许**一条生效** | 全表唯一会让「换源/换表」变成删历史；只按 `valid_to` 判又会放飞多行并存 |
| 唯一键**含 `class_id`** | 同表双类（实测 `DWD_SUPPLIER_PRICE_LIST` / `_DEL`） | 按表名定位类会在这一例上必错 |
| `uq_procurement_property_binding_key_ordinal`（部分唯一） | 同一绑定内键序号不重复 | 4 列复合主键（`DWD_PURCHASE_QUOTATION_YEARLY_DTL` 等）只能靠它表达 |
| `ck_*_candidate` + `ck_*_confirmed` | 候选 ⇔ 无确认人；非候选**必须**有确认人 + 确认时刻 | D-3 的隔离必须是库层的，否则「改个布尔」就绕过了 |
| FK：本体/数据源 `RESTRICT`，属性绑定→源绑定 `CASCADE` | 治理证据不悬空；属性绑定生命周期从属 | 两种方向语义不同，不能一刀切 |

### 2.3 `valid_to = now()` 会撞自己的 CHECK

`now()` 是 PostgreSQL 的**事务开始时刻**，与同事务插入时的 `valid_from` 完全相同 ⇒
`valid_to > valid_from` 失败。测试里改用 `valid_from + interval '1 second'`，
并在用例里写清原因（否则后来者会以为是约束写错了）。

---

## 3. 数据模型变更

**新增两张表**（迁移 `0154`，纯加法 DDL，不触碰既有表）：

- `procurement_source_binding`：16 列，3 个普通索引/部分唯一索引 + 2 条 FK + 4 条 CHECK。
- `procurement_property_binding`：14 列，3 个索引（2 个部分唯一）+ 2 条 FK + 4 条 CHECK。

ORM 注册：`app/domain/models.py` 尾部新增 re-export import（与 `workflow_models` 同理由——
Schema drift 校验与 autogenerate 靠 `Base.metadata` 看见这些表）。

---

## 4. 接口契约变更

**本批无 API 变更**（CRUD API 是下一段）。

---

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `app/domain/procurement_binding_models.py` | 两张表 ORM；列注释常量与 CHECK 表达式字符串（迁移逐字复用） |
| `alembic/versions/0154_procurement_bindings.py` | 手写迁移（禁 autogenerate）；`upgrade` 建表+索引，`downgrade` 逆序拆 |
| `app/domain/models.py` | 尾部 re-export import |
| `app/tests/unit/test_procurement_binding_models.py` | 11 条：表名/列集合/类型/可空/注释/CHECK/**部分唯一索引谓词**/FK ondelete/迁移链/down-up 对称 |
| `app/tests/integration/test_semantic_release_constraints.py`（末段） | 12 条裸 SQL 行为：唯一谓词 / 同表双类放行 / 失效后可续 / 候选⇔确认人 / 角色字面量 / 有效期方向 / 键序号唯一 / 属性唯一 / CASCADE / 属性删除被拒 |

关键判断：

- **新增 integration 用例写进既有文件**而不是新建文件：CI 集成测试按「排序后下标 mod 4」
  轮转分片，新建文件会让后续分片整体错位（`Harness/rules/测试规范.md`）。
- **unit 里钉住 `postgresql_where`**：`alembic check` 只比名不比谓词，谓词写错会静默放行。
- **断言落到具体约束名**（从 `IntegrityError` 消息解析）：只看 `IntegrityError` 会被
  任何一条别的约束先炸而假绿。

---

## 6. 测试

```text
unit   test_procurement_binding_models.py           11 passed
unit   test_orm_check_constraints.py + test_workflow_models.py（回归）  40 passed（含前者）
integ  test_semantic_release_constraints.py         29 passed（17 条既有 + 12 条新增）
integ  test_check_schema_drift.py                   38 passed
alembic upgrade 0153 -> 0154                         ✅
alembic downgrade 0154 -> 0153                       ✅（procurement_* 表数 0）
alembic upgrade head（回到 0154）                     ✅
alembic check                                        ✅ No new upgrade operations detected
```

命令（真实 PG 5434 + 独立 Neo4j 7688，**绝不用生产实例**）：

```bash
cd backend
TEST_DATABASE_URL="postgresql+asyncpg://qa_user:***@localhost:5434/qa_metadata_test" \
TEST_NEO4J_URI="bolt://neo4j:***@localhost:7688" \
  uv run pytest app/tests/unit/test_procurement_binding_models.py \
                app/tests/integration/test_semantic_release_constraints.py -q
```

---

## 7. 安全审查

- **迁移是纯加法 DDL**：只建两张新表；`downgrade` 只删自己建的索引与表。
- **无数据迁移**：不读写任何既有行。
- **无外部连接**：迁移只连本地测试库；证据不含 DSN 与口令。
- **库层隔离**：候选绑定（`is_candidate = TRUE`）在 CHECK 约束层面就无法伪装成已确认。

---

## 8. 部署验证

**未部署**（按用户决策）。本地测试库 5434 已完成 up/down/up 往返；
开发库 5433 **未执行**迁移（本地开发栈在 M7 统一拉平）。

---

## 9. 关联

- 设计：[A8 §4](../../../docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md)
- 下一段：M1-1.1 的 **service + CRUD API + 绑定向导**（含过滤白名单校验、候选→确认流转）
- 后续：M1-1.2 `semantic_release_check`（迁移取下一个空号）
