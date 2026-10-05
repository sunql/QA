# 操作手册：修正 DIM_FACILITY 的关联与外键元数据

> 面向：管理员手工操作 | 日期：2026-10-03
> 关联变更：`Harness/changes/fix-dim-facility-connectivity/`
> 状态：✅ **A/B/C/D 四项已于 2026-10-03 全部执行完毕并落库复核**
> （本文档保留，作为操作依据与回滚参考；若日后数据重置需重做，照此执行。
> 回滚见 [`rollback-metadata.sql`](./rollback-metadata.sql)）

### 执行结果（2026-10-03，走 API）

| 项 | 操作 | 结果 |
|---|---|---|
| A | 删除扇出边 83、84 | `ontology_join` 85 → **83** 条 |
| B | 撤销属性 702 的 FK | `fk=false, ref=NULL` |
| C | 补属性 836 的 FK | `fk=true, ref=1` |
| D | 补属性 2 的主键 | `pk=true` |

未改动：边 82、85；属性 703（本就正确）；属性 835（**有意不标 FK** —— 见 §0 原因）。

---

## 0. 为什么要做

2026-10-03 故障：多步问题「B019 圣特公司近 12 个月供货量下降的原因」第 2 步
（按收货地点拆分）报 `以下表无法通过关联路径连通：DIM_FACILITY`。

排查后你手工补了 4 条 JOIN 边。**其中 2 条会让查询结果错 9 倍**，必须撤掉。
本手册的核心不是"补东西"，而是**先撤错的、再补对的**。

### ⚠️ 已实测确认的三件事（不是推测）

**① `DIM_FACILITY` 的唯一约束是复合键，不是单列主键**

Oracle 数据字典 `all_constraints` / `all_cons_columns` 实查：

```
UK_DIM_FACILITY_LEGCPY_FCY   constraint_type = U   ENABLED
  ├ position 1 → LEGCPY_0
  └ position 2 → FCY_0
主键(P)约束：0 个
```

**② 两条边会扇出，另两条不会**（真库跑 `COUNT(*)`，不是估算）

| 边 id | 源类 → 目标类 | 源列 → 目标列 | 事实表行数 | JOIN 后行数 | 倍数 | 判定 |
|---|---|---|---|---|---|---|
| 82 | DIM_FACILITY → 采购订单明细 | `FCY_0 → PUR_SITE_CODE` | 927,631 | 927,631 | 1.00× | ✅ 保留 |
| 83 | DIM_FACILITY → 采购订单明细 | `LEGCPY_0 → COM_CODE` | 927,631 | **8,082,340** | **8.72×** | ❌ **删除** |
| 84 | DIM_FACILITY → 收货单明细 | `LEGCPY_0 → COM_CODE` | 3,558,004 | **31,575,628** | **8.87×** | ❌ **删除** |
| 85 | DIM_FACILITY → 收货单明细 | `FCY_0 → RCV_SITE_CODE` | 3,558,004 | 3,558,004 | 1.00× | ✅ 保留 |

根因：`LEGCPY_0`（公司代码）在这张表里 **27 个不同值，但最多一个公司挂着 10 个工厂**。
所以按公司码 JOIN，一行事实会匹配到多行工厂。

`FCY_0`（工厂代码）则相反：**41 行里 41 个不同值，重复组 0 个**，本身就是超键，
单列连完全无损。

**③ 扇出是静默错答，比原来的报错危险**

走边 83/84 的任何聚合，供货量 SUM 会被放大约 9 倍。
HTTP 200、数字形状正常、量级离谱 —— 不会有人发现。

### ⚠️ 还有一个坑：FK 标记会诱导 LLM 写错 SQL

本系统把外键渲染进 prompt 的方式是 `FK → 目标表`，**不带目标列名**
（`backend/app/services/nl2sql_schema.py:311-312`）。

也就是说，给 `COM_CODE` 打上 FK 标记，等于在 prompt 里明确告诉 LLM
「这一列可以关联到 DIM_FACILITY」—— 它会照着连，然后写出 9 倍放大的 SQL。

⇒ **`COM_CODE` 侧不能打 FK 标记**（属性 `702` 需要撤销，`835` 不要打）。
只有 `*_SITE_CODE` 侧（对应唯一的 `FCY_0`）才该打。

---

## 1. 操作总览

| # | 操作 | 在哪个 Tab | 对象 |
|---|---|---|---|
| A | **删除**扇出边 | 关联 | 边 83、边 84 |
| B | 撤销 FK 标记 | 属性 | 属性 `702` |
| C | 补 FK 标记 | 属性 | 属性 `836` |
| D | 补主键标记 | 属性 | 属性 `2` |
| — | 边 82、85 **不用动** | 关联 | — |

---

## 2. 入口

左侧菜单：**业务配置 → 本体管理**（网址 `/ontology`）

⚠️ 进页面后默认停在「**类**」Tab，需要手动点左边的「**关联**」或「**属性**」Tab
（Tab 切换不改网址，刷新会回到「类」）。

页面上有 5 个 Tab：**类 / 属性 / 关联 / …**（只用到「关联」和「属性」两个）。

---

## 3. 操作 A：删除 2 条扇出边

1. 左侧菜单 **业务配置 → 本体管理**
2. 点 **关联** Tab
3. 在表格里找到下面两行（表格**不显示边 id**，靠「源类 → 目标类」+「源列」+「目标列」辨认）：

   | 源类 → 目标类 | 源列 | 目标列 | 描述开头 |
   |---|---|---|---|
   | `DIM_FACILITY → DWD_PURCHASE_ORDER_DTL` | `LEGCPY_0` | `COM_CODE` | 采购订单的公司编码和… |
   | `DIM_FACILITY → DWD_GOODS_RECEIPT_DTL` | `LEGCPY_0` | `COM_CODE` | 公司及工厂信息的公司编码和… |

   > 「关联」Tab 的这一列表格显示的是**物理名**（`JoinTab.tsx:85` 取的是
   > `className` 原始字段，不走别名格式化），所以直接找 `DIM_FACILITY` 开头的那两行。
   > ⚠️ 别和「属性」Tab 的显示搞混：「属性」Tab 的**「引用类」下拉**走的是另一套
   > 格式化，会显示成「别名（物理名）」，见 §5。

4. 这一行最右侧「**操作**」列点红色小按钮「**删除**」
5. 弹出气泡确认框，标题「**确认删除？**」，点「**确定**」
6. 右上角出现绿色提示「**已删除**」= 成功

**重复一次**，把两条都删掉。

### 验证

刷新「关联」Tab，`DIM_FACILITY` 相关应该**只剩 2 行**（都是 `FCY_0` 开头的那两条）。

---

## 4. 操作 B：撤销属性 702 的 FK 标记

属性 `702` = `DWD_GOODS_RECEIPT_DTL`（收货单明细）的 `COM_CODE`。
它已经被标成 `外键 + 引用类=工厂`，需要撤掉。

1. 左侧菜单 **业务配置 → 本体管理**
2. 点 **属性** Tab
3. 表格最左列就是「**ID**」，找到 **ID = 702** 的那一行
   （源字段 `COM_CODE`，标签列有绿色 `FK` 标签）
4. 点该行「**操作**」列的「**编辑**」
5. 弹窗标题「**编辑属性**」，按顺序做两步：

   a. **取消勾选「外键」**（`外键` checkbox，去掉绿勾）
   b. **清空「引用类」下拉** —— 点下拉框右侧的 `×`（清除图标），
      或选中后按退格清掉，让它变回 placeholder「选择外键指向的本体类」

   > ⚠️ **两步都要做，顺序不能反。**
   > 如果只取消勾选「外键」但留下引用类，属性会变成
   > `外键=false + 引用类=工厂` —— 不会影响 prompt（FK 标记要 `外键=true` 才渲染），
   > 但数据不干净，以后容易看懵。
   >
   > ⚠️ **不能只清空引用类而保留「外键」勾选** —— 界面会拦住并弹红色提示
   > 「**勾选外键后必须选择引用类**」，这是防止造出
   > `外键=true + 引用类=空` 这种会让数据质量规则生成器把属性列进 `blocked[]` 的组合。

6. 点「**确定**」
7. 绿色提示「**更新成功**」= 成功

### 验证

这一行「标签」列的**绿色 FK 标签消失**了。

---

## 5. 操作 C：给属性 836 补 FK 标记

属性 `836` = `DWD_PURCHASE_ORDER_DTL`（采购订单明细）的 `PUR_SITE_CODE`。
它对应唯一列 `FCY_0`（边 82 已验证 1:1 无损），**应该**标。

1. 同上进入 **属性** Tab
2. 找到 **ID = 836** 的行（源字段 `PUR_SITE_CODE`）
3. 点「**编辑**」
4. 勾选「**外键**」
5. 在下面的「**引用类**」下拉里选择「**公司及工厂的信息（DIM_FACILITY）**」
   - 下拉的选项文字是「**别名（物理名）**」格式，中文环境下显示为
     `公司及工厂的信息（DIM_FACILITY）`
   - 下拉可以搜索：输 `工厂`、`DIM_FACILITY` 或 `Facility` 都能筛出来
   - 鼠标移到「引用类」标签旁的 `?` 图标，会显示提示：
     「外键指向的本体类。schema 文本会据此渲染 [FK → 目标类]…」
   - 框下面有一行灰字：「设置外键不会自动创建 JOIN 边，两者需分别维护…」
     —— 这正是我们**已经手工建好边 82** 的原因
6. 点「**确定**」→ 提示「**更新成功**」

### 不要动 703

属性 `703`（收货单明细 `RCV_SITE_CODE`）**已经标好了**，不用改。

---

## 6. 操作 D：给属性 2 补主键标记

属性 `2` = `DIM_FACILITY` 的 `FCY_0`（工厂代码）。

尽管 Oracle 里是 `(LEGCPY_0, FCY_0)` 复合唯一、没有单列主键约束，
但 `FCY_0` 单列就已经是超键（41 行 41 个不同值），
**把它标成主键是诚实的、也是安全的**。

1. **属性** Tab → 找到 **ID = 2** 的行（源字段 `FCY_0`）
2. 点「**编辑**」
3. 勾选「**主键**」
4. 「外键」保持不勾
5. 点「**确定**」

### 为什么值得做

数据质量（DQ）规则生成器在处理外键属性时，会去查**引用类的主键属性**
来解析参照完整性规则的映射列。没有主键标记，将来对收货单/采购单跑
DQ 批量生成时，`RCV_SITE_CODE` 会被列进 `blocked[]`，
报「引用类未配置主键列映射」—— 不影响 NL2SQL，但会平白多一条 DQ 侧的报错。

---

## 7. 全部做完后的自查

打开 **属性** Tab，按 ID 核对下面这张表：

| ID | 源字段 | 期望「标签」列 | 期望「引用类」 |
|---|---|---|---|
| `2` | `FCY_0` | 蓝色 **PK** | 空 |
| `703` | `RCV_SITE_CODE` | 绿色 **FK** | 公司及工厂的信息（DIM_FACILITY） |
| `702` | `COM_CODE` | **无标签** | 空 |
| `836` | `PUR_SITE_CODE` | 绿色 **FK** | 公司及工厂的信息（DIM_FACILITY） |
| `835` | `COM_CODE` | **无标签** | 空 |

打开 **关联** Tab，确认源类为 `DIM_FACILITY` 的只剩 **2 行**，源列都是 `FCY_0`。

---

## 8. 如果需要回滚

| 操作 | 怎么撤 |
|---|---|
| B（属性 702） | 编辑 → 勾回「外键」→ 引用类选「公司及工厂的信息（DIM_FACILITY）」→ 确定 |
| C（属性 836） | 编辑 → 取消「外键」→ 清空「引用类」→ 确定 |
| D（属性 2） | 编辑 → 取消「主键」→ 确定 |
| A（边 83/84） | 需按 §3 的表单重新建一条边（**不建议撤** —— 撤了就回到"连不上"的报错） |

---

## 9. 可选：如果业务上真要「按公司拆」

边 83/84 之所以危险，是因为一个公司有多个工厂。如果确实需要按公司维度分析，
**不要用单列公司码 JOIN**，两个正确做法：

1. **用复合键 JOIN**：在「新增关联」里，源列填 `LEGCPY_0, FCY_0`，
   目标列填 `COM_CODE, RCV_SITE_CODE`（逗号分隔，位置一一配对）
   —— 实测 3,558,004 → 3,557,990，只丢 14 行（0.0004%）
2. **先聚合再 JOIN**，或对工厂去重

> 界面的「源列 / 目标列」是**纯文本框**，不是下拉，要手打列名；
> 多列就用**英文逗号**分隔。系统**不校验**源列和目标列数量是否相等，
> 填错位数不会被拦，所以打完自己数一遍。

---

## 10. 附录：不想点界面时的 API 做法

<details>
<summary>展开（需要管理员令牌）</summary>

```bash
# 把口令放进环境变量，不要写进文件或命令历史
export QA_ADMIN_PASSWORD='<你的登录口令>'

TOKEN=$(curl -s -X POST http://localhost:5173/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"${QA_ADMIN_PASSWORD}\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("accessToken",""))')

[ -n "$TOKEN" ] && echo "登录成功" || echo "登录失败"
```

```bash
# A. 删除扇出边 83、84
for jid in 83 84; do
  curl -s -X DELETE "http://localhost:5173/api/v1/ontology/joins/${jid}" \
    -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
done

# B. 撤销属性 702 的 FK（refClassId 必须显式给 null，否则残留）
curl -s -X PUT http://localhost:5173/api/v1/ontology/properties/702 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"isForeignKey": false, "refClassId": null}'

# C. 补属性 836 的 FK
curl -s -X PUT http://localhost:5173/api/v1/ontology/properties/836 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"isForeignKey": true, "refClassId": 1}'

# D. 补属性 2 的主键
curl -s -X PUT http://localhost:5173/api/v1/ontology/properties/2 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"isPrimaryKey": true}'
```

```bash
unset QA_ADMIN_PASSWORD
```

</details>

---

## 11. 不在本手册范围内

- **⚠️ 不要点「一键补关系」**（`POST /ontology/relations/backfill`）：
  它的外键补全只认 `SAGE_X3_REFERENCE_MAP` 里的 11 个列名，
  `RCV_SITE_CODE` / `PUR_SITE_CODE` / `COM_CODE` / `LEGCPY_0` **都不在里面**，
  跑了没用；更糟的是它会给 `FCY_0` 建一条**指向 `DIM_FACILITY` 自己的自引用边**
  （代码里没有「源类 == 目标类」的保护）。
- **其余 7 个非 ODS 孤岛类**仍无 JOIN 边：
  `DWD_BUSINESS_PARTNER` / `DWD_CARRIER` / `DWD_CUSTOMER` /
  `DWD_PURCHASE_QUOTATION_YEARLY_DTL` / `DWD_SUPPLIER_PAYMENT_LINE` /
  `DWD_SUPPLIER_PRICE_LIST_CONFIG` / `DWD_SUPPLIER_PRICE_LIST_HEADER`
  代码层的「校验自愈 + 孤岛标记」能兜住，补边才是治本。
- **死边巡检工具在 Oracle 上是坏的**：
  `GET /ontology/health/joins?probe=true` 必然 500（`ORA-00933`），
  因为探针 SQL 用了 `LIMIT 10000`，Oracle 19c 不认
  （`ontology_join_health_service.py:135,138`）。所以**你无法用系统自带工具
  验证边是不是死边**，本手册的数据是我另写探针在真库上跑的。
