# 变更：采购平台 V0.3 · M0.4 渲染顺序统一（A4）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M0 底座 / W1 轨 A（A4：M0.4 方案 B）
- **状态**：in-review（分支 `feat/procurement-m0`，**未合并、未部署**）
- **关联变更**：[2026-11-18-procurement-m0-flags](../2026-11-18-procurement-m0-flags/summary.md)（A2）；[2026-11-18-procurement-m0-criteria](../2026-11-18-procurement-m0-criteria/summary.md)（A3）
- **迁移版本**：无（本批无 DDL / 无数据迁移）
- **验收包**：[docs/update/1118/M0/evidence/render-order.md](../../../docs/update/1118/M0/evidence/render-order.md)
- **方案**：[M0.4-G3修复方案与答案级评估口径.md §3](../../../docs/update/1118/M0/M0.4-G3修复方案与答案级评估口径.md)
- **计划**：[并行轨与汇合点 §3 轨 A · A4](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_并行轨与汇合点_1118.md)

> 用户已拍板的三条前提（2026-11-18）：**生产暂不部署**；**功能需要全部完成**；
> **本地全绿后统一部署**。D-1 已拍板执行方案 B。

---

## 1. 需求

**用户视角**：G3 第 2 条（答案级评估）不过，根因是**开闸前后 prompt 的行序不同** ——
关态直读侧按 ORM 物理行序、JOIN 按调用方顺序；开态快照侧按 `propertyName` / `joinKey`
排序。⇒ 打开 `ENABLE_SEMANTIC_RELEASE` 等于给模型换了一份 prompt，SQL 与答案跟着变。
G3 要的是「开闸 = 恒等变换」，不是「开闸 = 另一次调参」。

**验收标准（M0.4 §3.1，可测量）**：

1. **V1** 直读侧同一 DB 状态多次渲染逐字节相同；
2. **V2** 直读侧与快照侧 schema 文本逐字节相同；
3. **V3** 渲染顺序 == `semantic_compiler` 产出的 manifest 顺序；
4. **V4** 缺 `join_key` / 缺 `property_name` 时排末尾、不抛错；
5. **V5** 既有单测定向回归，**净新增失败 0**。

---

## 2. 设计评审

### 2.1 只有两条顺序轴

逐行核对 `buildSchemaText`：类块顺序由 `_topoSortByInheritance` 按 `(class_name, id)`
决定（已确定），摘要段内部按限定名排序，间接 JOIN 提示 `sorted(set)`，孤岛判定用集合。
⇒ 真正影响字节的只有**类内列序**与**JOIN 行序**两处。

### 2.2 排序键放哪：共享原语，而不是各写一份

备选：两侧各写 `sorted(key=lambda x: x.name)`。否决 —— 两份键一旦漂移，
「逐字节相同」会在某个边角（如 `None` 处理）悄悄失效，且没有任何测试看得见。
最终放到 `semantic_manifest_common.py`，编译器与渲染器**import 同一个函数**；
`test_semantic_render_order.py::test_两侧的排序键就是共享原语` 反向守卫：
直接按原语排序，与两侧实际产出对照，改任一侧立刻红。

### 2.3 `None` 键：排末尾而不是抛错

DB 列 `property_name` / `join_key` 均 NOT NULL，但测试构造实体与脏快照可能缺键。
渲染路径不能因为一个缺键属性整段挂掉（V4）。元组键 `(name is None, name or "")`
对真实数据与纯字符串比较**同序**，因此不改变既有 manifest 字节。

### 2.4 复原层的职责不变

`restoreSnapshotSchema` 仍只做「manifest 顺序 → 实体顺序」的忠实搬运，**不排序**。
排序权在渲染器 —— 否则排序键会隐含多一个字段面（复原层看得见的字段），
一旦复原层改按 `sourceColumn` 排，已发布快照会在**不重新发布**的情况下改变。
`test_semantic_schema_adapter.py::test_列顺序忠实于manifest里属性的顺序` 据此改写为
**断言实体顺序**（原先断言渲染文本顺序，A4 后会与渲染排序冲突）。

### 2.5 钉住重放用例的「对照组」必须换判据

`test_重放读同一发布则文本逐字节相同` 原先用 `withB.text != first.text` 证明
「真去用 B 被守卫拒绝、回到了直读口径」。A4 后直读与快照逐字节相同 ⇒ 该断言
按设计会假红：文本不再能区分走了哪条路径（这正是方案 B 的目的）。
改为断言 `withB.text == first.text` 且 `withB.semanticReleaseId is None`
—— 路径判据交给 **发布 id**，文本相等成为 A4 的目标本身。

---

## 3. 数据模型变更

无。不改 ORM 关系、不加索引、不动迁移（head 仍 `0153`，下一个可用号 `0154` 留给 A4-0）。

**刻意不做的**：不给 `OntologyClass.properties` 关系加 `order_by`。理由同 §2.4 ——
关系排序会把顺序写进 ORM 契约，而排序是**渲染**关注点；且 `selectin` + `order_by`
会对全量加载施加额外排序成本。

---

## 4. 接口契约变更

| 契约 | 变更 | 处置 |
|---|---|---|
| **D15「关 = 逐字节等于今天」** | ❌ **本轮刻意改变**（关态列序/JOIN 序变化） | 已知风险（M0.4 §3）；量化归 A5（S2）；生产未部署、开关未开闸 |
| D16 钉住重放 | ✅ 不变 | 快照侧键本来就一致；`test_重放…` 仍绿（对照组判据改写，见 §2.5） |
| `buildSchemaText` 输出 | 列序改为 `property_name` 升序、JOIN 行按 `join_key` 升序 | 调用方无需改动；下游若依赖行序（当前未发现）需复核 |

---

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/services/semantic_manifest_common.py` | 新增 `propertyNameSortKey` / `joinKeySortKey`（含 `None` 兜底与设计注释） |
| `backend/app/services/semantic_compiler.py` | `_groupActiveProperties`、`_compileJoins` 改用共享键 |
| `backend/app/services/nl2sql_schema.py` | 列渲染循环、`_renderJoinLines` 按共享键排序 |
| `backend/app/tests/unit/test_semantic_render_order.py` | 新增：V1（3）/ V3（3）/ V4（2），共 8 条 |
| `backend/app/tests/unit/test_semantic_schema_equivalence.py` | `_assertContentEqual` → `_assertByteIdentical`（逐字节 + 多重集兜底） |
| `backend/app/tests/unit/test_semantic_schema_gate.py` | 「同文本」用例改名；路径证据改用 `semanticReleaseId`（INFO 日志在降级判定前就会打出 ⇒ 不可用） |
| `backend/app/tests/unit/test_semantic_schema_adapter.py` | 复原层「不排序」改为断言实体顺序 |
| `backend/app/tests/unit/test_semantic_release_pin.py` | 对照组判据由文本差改为发布 id（§2.5） |

---

## 6. 测试

### 6.1 新增 / 修改用例

- **V1**：属性 / JOIN 三种输入排列、类列表三种排列、摘要段三种排列 ⇒ 逐字节相同
  （排列用身份/倒序/左旋，确定性，不用随机）。
- **V3**：`_columnOrder(text) == manifest 属性顺序`；JOIN 描述序 == manifest JOIN 序；
  以及「按共享原语直接排序」三方对照。
- **V4**：缺名属性排最后且不抛错；缺 `join_key` 的 JOIN 排最后、其余 JOIN 全部渲染。
- 反向守卫：`test_两侧的排序键就是共享原语`。

### 6.2 实测结果（2026-11-18）

```text
app/tests/unit/test_semantic_render_order.py              8 passed  (3.36s)
12 文件定向回归（渲染 / 编译器 / 闸门 / 钉住 / NL2SQL / JOIN 图 / 摘要）
                                                        500 passed (163.62s)
schema 相关真实库集成 4 文件                              33 passed, 1 failed
```

失败 1 条为**既有环境性失败**（`test_list_classes_returns_updated_class_with_properties`，
断言 `len==1` 实得 6 = 5 种子 + 1 遗留）—— 见证据 §3，非净新增。

### 6.3 覆盖率

本批无新增服务代码分支（3 处一行排序调用），新增 8 条用例；覆盖率随 M0 末统一统计，
不在单独批次产出（避免红跑产物被误当门禁）。

---

## 7. 安全审查

无新增外部输入面：排序键只比较 DB 已存的 `property_name` / `join_key`（NOT NULL），
不解析、不求值、不拼接进 SQL；`_sanitizeSchemaField` 净化路径不变。
无 CRITICAL / HIGH。

### 7.1 代码评审（code-reviewer，2026-11-18）

| 级别 | 发现 | 处置 |
|---|---|---|
| MEDIUM | 新闸门用例用「快照已复原」INFO 日志当路径证据 —— 该日志在 `_factDiff` 判定**之前**打出，降级路径同样有 ⇒ 假绿 | ✅ 已修：改用 `renderSchemaWithRelease` 并断言 `semanticReleaseId == 1`（唯一能区分「真用快照」与「降级直读」的值）；日志断言语义已写入 docstring |
| LOW | `_assertByteIdentical` 的行多重集断言在逐字节断言之后 ⇒ 永远不会执行 | ✅ 已修：先算行差并 `pytest.fail` 报出「仅直读有 / 仅快照有」样例 |
| LOW | `test_semantic_release_pin.py` 1593 行（仓库上限 800） | 记档延后：本批只改 ~10 行，拆分另立 |
| 备注 | 闸门 `_factDiff` 把**块内顺序**当作非事实差异（`test_块内列序不同不算事实差异` 钉着）。A4 后两侧逐字节相同，该容忍已过时：将来只丢一侧排序时闸门会放行 | **登记为 A4-follow-up**（见 §9），本批不动运行时闸门 |

评审同时确认：`_buildCriticalColumnsDigest` / `_buildIndirectJoinHints` / 孤岛标注 /
`describe_table` 均与行序无关；无第二处渲染器被本次排序破坏；`list_joins` 的顺序差异是
已登记的历史分歧，不在 schema 文本路径上。

---

## 8. 部署验证

### 8.1 本地

见 §6.2；无 DDL，无需重建库。

### 8.2 真实数据验证报告

**未做**（需先存在一份发布快照）。建议 A9 首次发布后用
`scripts/semantic_schema_shadow_diff.py` 复跑：A4 后「仅顺序不同（exit 3）」应恒为 0，
出现即回归。

---

## 9. 关联

- 证据：[render-order.md](../../../docs/update/1118/M0/evidence/render-order.md)
- 方案与评估口径：[M0.4](../../../docs/update/1118/M0/M0.4-G3修复方案与答案级评估口径.md)
- 下一步：A5（答案级评估脚本 S2/S3）→ A6（KPI 12 条归因）
- **A4-follow-up**（登记，不阻塞）：把闸门事实校验从「块内容等价」收紧为「块内顺序也等价」
  —— A4 后这是运行时可强制的，届时排序回归会在生产闸门就地暴露（当前仅 CI 强制）
- 回滚：还原 3 处排序调用即可，无数据副作用
