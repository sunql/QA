# A4 证据：渲染顺序统一（M0.4 方案 B）

> 证据日期：2026-11-18
> 环境：本地测试栈（PG 5434 / `qa_metadata_test`，与 CI 同款）；唯一入口 `./scripts/run_local_tests.sh`
> 代码：branch `feat/procurement-m0`，未部署（按 2026-11-18 决策「生产暂不部署，本地全绿后统一发版」）

## 1. 改了什么（3 处纯函数 + 1 个共享原语）

| 位置 | 改动 | 作用 |
|---|---|---|
| `semantic_manifest_common.py` | 新增 `propertyNameSortKey` / `joinKeySortKey` | **唯一**排序真源；`None` 排末尾、不抛错（DB 列 NOT NULL，仅测试构造实体可能缺键） |
| `semantic_compiler.py` | `_groupActiveProperties` / `_compileJoins` 改用共享键 | 编译侧键保持不变（仍是 `property_name` / `join_key` 升序），只是抽成共享函数 |
| `nl2sql_schema.py` | 列渲染循环按 `propertyNameSortKey` 排序；`_renderJoinLines` 按 `joinKeySortKey` 排序 | 直读侧不再用 ORM 物理行序 / 调用方 JOIN 序 |

`_topoSortByInheritance` 本就按 `(class_name, id)` 排序 ⇒ 类块顺序两侧一致，本次不动。
摘要段（`_buildCriticalColumnsDigest`）内部按限定名排序、间接 JOIN 提示用 `sorted(set)`、孤岛判定用集合 ⇒ 均与行序无关，本次不动。

## 2. 判据实测（V1–V5）

| # | 判据 | 用例 | 结果 |
|---|---|---|---|
| V1 | 直读侧多次渲染逐字节相同 | `test_semantic_render_order.py::TestV1输入顺序无关`（属性+JOIN 三种输入排列 / 类列表三种排列 / 摘要段三种排列） | ✅ 3 passed |
| V2 | 直读与快照逐字节相同 | `test_semantic_schema_equivalence.py`（3 处改为 `_assertByteIdentical`）+ `test_semantic_schema_gate.py::test_开关打开且快照新鲜时与直读逐字节一致`（路径证据 = `semanticReleaseId`，见 §2.2 评审 MEDIUM） | ✅ |
| V3 | 渲染顺序 == 编译器 manifest 顺序 | `test_semantic_render_order.py::TestV3与编译器同序`（列序、JOIN 序、按共享键直接排序三方对照） | ✅ 3 passed |
| V4 | 缺键不炸且排末尾 | `test_semantic_render_order.py::TestV4缺键兜底`（缺名属性 / 缺 `join_key` 的 JOIN） | ✅ 2 passed |
| V5 | 既有单测不回归（净新增失败 0） | 见 §3 | ✅ |

```text
# V1–V4 单文件
app/tests/unit/test_semantic_render_order.py ........   8 passed in 3.36s
```

### 2.1 变异反证（证明断言不是空集）

| 变异 | 结果 |
|---|---|
| 去掉列渲染排序（`for prop in cls.properties`） | **7 failed**：等价 3 条 + V1 1 条 + V3 2 条 + V4 1 条 |
| 去掉 JOIN 行排序（`for join in joins`） | **3 failed**：V1 1 条 + V3-join 1 条 + V4-join 1 条 |

两处均改回原样后重跑全绿。

## 3. V5 定向回归

```text
./scripts/run_local_tests.sh \
  app/tests/unit/test_semantic_render_order.py \
  app/tests/unit/test_semantic_schema_equivalence.py \
  app/tests/unit/test_semantic_schema_gate.py \
  app/tests/unit/test_semantic_schema_adapter.py \
  app/tests/unit/test_semantic_release_pin.py \
  app/tests/unit/test_nl2sql_schema.py \
  app/tests/unit/test_nl2sql_service.py \
  app/tests/unit/test_join_graph.py \
  app/tests/unit/test_schema_island_marker.py \
  app/tests/unit/test_semantic_schema_shadow_diff.py \
  app/tests/unit/test_semantic_manifest_golden.py \
  app/tests/unit/test_semantic_compiler.py

======================= 500 passed in 163.62s (0:02:43) ========================
```

schema 文本相关的真实库集成用例：

```text
app/tests/integration/test_semantic_registry_service.py
app/tests/integration/test_ontology_class_versioning.py
app/tests/integration/test_seed_missing_business_objects.py

================== 1 failed, 33 passed, 46 warnings in 28.49s ====================
```

**唯一失败的定性：既有环境性失败，非 A4 净新增。**

> 纪律补充：定向回归必须**串行跑**。本次另有 review 用 pytest 并行，两个进程同时建
> fixture ⇒ `DeadlockDetectedError`（5 条 setup error）；该进程结束后单跑同文件 **38 passed**。
> 判定「回归」前先 `ps` 抓僵尸 pytest（判据书既有纪律）。

- 用例：`test_ontology_class_versioning.py::TestUpdateKeepsOwnedData::test_list_classes_returns_updated_class_with_properties`
- 断言：`assert len(current) == 1`，实得 `6`
- 实测库内容：`select count(*) from ontology_class where valid_to is null` = **6**
  （5 个 A1 种子类 `Supplier/ItemMaster/PurchaseOrder/Receipt/IncomingInspection` + 1 个此前跑该文件遗留的 `PRECEIPT`）
- 该用例假设测试库**无种子**；A4 不触碰 `OntologyService.listClasses` 与类计数，且改动前后失败形态一致
- 归入既有「装配盲区」家族，修复属测试隔离议题，不在 A4 范围

## 4. 契约变更（D15）

| 契约 | 变更 | 依据 / 处置 |
|---|---|---|
| **D15「关 = 逐字节等于今天」** | ❌ **本轮刻意改变**：关态的列序从「ORM 物理行序」变为「`property_name` 升序」；JOIN 行序从「调用方顺序」变为「`join_key` 升序」 | M0.4 §3 已知风险；这正是 S2 要量化的对象（A5）。**生产未部署、开关未开闸** ⇒ 影响面待 S2/S3 评估后随统一发版一次性暴露 |
| D16 钉住重放 | ✅ 不受影响 | 快照侧本来就走同一排序键；`test_重放读同一发布则文本逐字节相同` 仍绿。其「对照组」断言按新语义改写：A4 后**文本不再能区分走了哪条路径**，`semanticReleaseId` 才是判据（`withB.text == first.text` 且 `semanticReleaseId is None`） |

## 5. 未覆盖 / 后续

1. **真实库上的 V1/V2 复跑**：需要存在一份发布快照。建议在 A9（采购域首次发布）时用 `scripts/semantic_schema_shadow_diff.py` 复跑 —— A4 之后「仅顺序不同（exit 3）」应恒为 0，若出现即回归。
2. **S2 / S3 答案级评估**：属 A5（`eval_semantic_answer_parity.py`），本证据不含答案级结论。
3. 测试隔离：`test_ontology_class_versioning.py` 的计数断言依赖空库，待独立修复。

## 6. 回滚

纯代码、无 DDL、无数据变更。回滚 = 还原 3 处排序调用（`nl2sql_schema.py` 两处循环 + 共享键的使用），不影响已发布快照与开关状态。
