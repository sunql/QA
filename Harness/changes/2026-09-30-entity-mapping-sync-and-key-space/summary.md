# 变更：供应商全称检索失效 + entity_mapping 同步源表与键空间

- **日期**：2026-09-30
- **Phase**：bugfix（数据维护链路 + 供应商名解析）
- **状态**：done
- **关联变更**：[fix-entity-mapping-sync-bootstrap](../fix-entity-mapping-sync-bootstrap/summary.md)（同一脚本的前一次修复）
- **MEMORY**：[[qa-system-entity-mapping-sync-bootstrap]]、[[qa-system-enterprise-key-space]]

---

## 1. 需求

用户报两个缺陷：

1. 选「饼状图」显示占比，但没输出饼图 → 见 [chart-decision-engine](../../changes/2026-09-30-chart-decision-engine/summary.md)（另一条独立缺陷链，commit `fcbb6fd`）
2. 用供应商**全称**检索：`未在主数据中找到名为 '浙江力航汽车部件有限公司' 的供应商。
   请用 enterprise_code（如 10105）重试`；但用编号 `B125` 或简称 `浙江力航` 都能找到。
   **「浙江力航汽车部件有限公司只是一个例子，其他的全称也一样找不到」**

验收标准：

- 全称能解析为正确编码（B125），且不是只修好这一个例子
- 简称 / 编码路径不回退
- `entity_mapping` 与 THBI 数仓主数据一致

## 2. 设计评审

### 根因（两条独立缺陷，症状相同）

| # | 根因 | 触发症状 | 修复位置 |
|---|---|---|---|
| 1 | **prod `entity_mapping` 实际为空**（仅 1 行手工垃圾数据 `' B019 圣特'`，name=NULL）；真实 354431 行在 `qa_metadata_restore_tmp`。全称走 `_BARE_NAME_RE` → resolver → 空表 → 硬报错；编号/简称**绕过 resolver**（走 NL2SQL 对数仓的 LIKE / 数字正则）故恰好能查到 —— 这就是「编号能查、全称不能」的不对称来源 | 全称 100% 失败 | 重跑同步 |
| 2 | 同步脚本源表写成 `THBI.DWD_SUPPLIER` / `THBI.DWD_MATERIAL`，**这两张表在库里已不存在**（实跑 ORA-00942）；现役主数据表是 `DIM_SUPPLIER(BPSNUM_0/BPSNAM_0)` / `DIM_IMATERIAL(ITMREF_0/ITMDES1_0..3)` | 同步 0 行 | `scripts/sync_entity_mapping_from_thbi.py` |

### 修复 1 过程中发现的两条新缺陷

| # | 根因 | 触发症状 | 证据 |
|---|---|---|---|
| 3 | `_stableKey` 键空间只有 2³²。MATERIAL 350922 条在该空间里**碰撞 12 次**，`ON CONFLICT ... DO UPDATE SET name` 把 12 对编码静默折叠成 12 行（后者编码消失、前者 name 被覆盖），rowcount 仍报满额、无告警 | 同步 planned=350922 但落库 350910 | 只读探针实测：12 个 key 各含两个真实业务码；生日公式期望 14.3。原注释写的「35w 输入 < 10⁻⁵」比实际乐观约 6 个数量级 |
| 4 | `_BARE_NAME_RE` 无左边界。中文无词间空格，贪婪字符类把紧邻动词吞进公司名 | 真实问句 8 条里 3 条失败：「查询浙江力航…」「我要看浙江力航…」「用浙江力航…」 | 提取结果为「查询浙江力航汽车部件有限公司」（含动词），精确/LIKE 双双落空 |

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| (A) 只重跑同步（改源表） | ✅ 必选，但不充分 | 不修 #3 会持续丢行；不修 #4 粘连形态仍失败 |
| (B) 键空间 2³² → 2⁴⁸ | ✅ 采用 | 碰撞期望 14.3 → 2.2e-4；上界 8.4e14 远在 BIGINT 内。代价：既有键全失效 → 需清表重放（备份在，且 354k 行可由 THBI 确定性重放） |
| (C) 保留 2³² + 确定性加盐重哈希 | ❌ 否 | 不删数据但需证明「增删码时键稳定」，复杂度换 12 行收益，违反 KISS |
| (D) 动词黑名单扩 `_BARE_NAME_RE` | ❌ 否 | 中文动词/名词组合不可枚举（查询/我要看/用/对比/帮我看…），漏一个就失败一次 |
| (E) **词典裁决左边界**：右边界固定的候选窗口交 entity_mapping 的 3500 个真实名字裁决，取最长精确命中 | ✅ 采用 | 命中即证明是左边界被污染；窗口仅 ~26 个；热路径（裸名提取本就正确）零额外查询 |
| (F) 键派生保留两份实现 | ❌ 否 | 两处注释都写「同源（SSOT）」而代码是复制粘贴 —— 注释不是约束。收敛到 `app/domain/enterprise_key.py` |

## 3. 数据模型变更

`enterprise_key` 区间重划（旧区间作废，**不兼容**；无外部表引用该 BIGINT，已核 `feature_value.entity_key` / `document_entity_relation.entity_key` 均为 VARCHAR 业务码）：

| entity_type | 旧区间 | 新区间 |
|---|---|---|
| SUPPLIER（真实） | 800000–4295767295 | 1_000_000 – 1_000_000+2⁴⁸ |
| MATERIAL（真实） | 4295767296–8591534591 | 1_000_000+2⁴⁸ – 1_000_000+2×2⁴⁸ |
| 未注册类型（通用段） | 8591534592 起 | 1_000_000+2×2⁴⁸ 起 |

合成 seed 区间（100001–500001）不变，新增分段起点 1_000_000 仍在其上。

## 4. 接口契约变更

无 API 形状变更。行为变更：`SupplierNameResolver.resolve` 新增 Pass 2b（裸名候选集查询），
仅在「Pass 2 精确匹配落空 **且** 候选窗口多于一个」时触发；返回值语义不变
（`resolved_by="name_exact"`，`original_name` 为**词典里的**真实名字，`apply()` 靠子串定位仍能正确替换）。

## 5. 实现要点

- `app/domain/enterprise_key.py`：`stableKey` + `KEY_RANGE_SIZE` + `offsetFor` + `ENTITY_TYPE_OFFSETS`。
  脚本与 service 均 import，不再各存一份。
- `_bareNameCandidates(name)`：按公司后缀（长后缀优先）反推左边界窗口，前缀 ≥4 字符。
- `_longestExactMatch(session, candidates)`：`name IN (...)` 一次查询，取最长命中。
  取最长而非唯一命中 —— 窗口是嵌套串（「浙江力航汽车部件有限公司」与「力航汽车部件有限公司」可能都是真实供应商），覆盖更多原文者才是用户所指。
- 测试假 session 从「按调用顺序弹队列」改为「按 compiled SQL 分派」：新增一次查询时，顺序队列会让既有用例取到下一个分支的结果，把「实现正确」伪装成「测试红了」。

## 6. 测试

| 文件 | 覆盖 |
|---|---|
| `app/tests/unit/test_enterprise_key.py`（新） | 确定性、区间不重叠、**2³² 下实测碰撞的 10 对真实编码做回归夹具**、生日公式门槛（350922 条期望碰撞 < 1e-3）、通用段回退、BIGINT 上界 |
| `app/tests/unit/test_supplier_name_resolver.py` | 新增 8 例：4 种粘连动词形态、最长命中优先、**热路径不加查询**（反向守卫）、词典未命中仍走 LIKE 报未找到（反向守卫）、`_bareNameCandidates` 窗口枚举 4 例 |
| `app/tests/unit/test_sync_entity_mapping_from_thbi.py` | `TestSourceTables` 钉住源表与列名契约；区间断言改用 SSOT 常量 |

共 95 passed（4 个相关文件）；受影响套件（含 AST 守护 `test_no_duplicate_methods.py`）139 passed。

## 7. 安全审查

- 无新增端点、无新增输入通道；SQL 仍走 ORM 参数化（`name IN (...)` 为绑定参数），无注入面
- `_longestExactMatch` 只读 SELECT，`entity_type` 恒定 `'SUPPLIER'`
- 键空间扩大不改变「同 code 同 key」性质，不影响 ACL / owner 派生

## 8. 部署验证

```bash
docker compose build backend && docker compose up -d backend   # 镜像固化（镜像==代码）
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "DELETE FROM entity_mapping;"
docker exec -e DATABASE_URL=... -e QUERY_TIMEOUT_SECONDS=600 \
  qa-backend python -m scripts.sync_entity_mapping_from_thbi
```

实测结果见文末「部署验证记录」。

## 9. 关联

- 上游：[fix-entity-mapping-sync-bootstrap](../fix-entity-mapping-sync-bootstrap/summary.md)（2026-09-16 同脚本三重 bug）
- 文档：[Harness/wiki/data-model.md](../../wiki/data-model.md)（区间表已同步更新）
- 相关代码：`app/services/supplier_name_resolver.py`、`app/services/entity_mapping_service.py`、`scripts/sync_entity_mapping_from_thbi.py`

## SSOT 校验清单

- [x] `enterprise_key` 派生只有一处实现（`app/domain/enterprise_key.py`），两处调用方均 import
- [x] 源表名与列名在测试里钉住（`TestSourceTables`），改名会在测试而非线上暴露
- [x] 键空间下限由生日公式断言守住（不是注释声称）
- [x] 解析器热路径查询次数有反向守卫用例
- [ ] 周期性同步任务（launchd / scheduler）—— 仍缺，见 bootstrap 记录的待办
- [ ] 同步脚本经 adapter 写入 `evidence` 表（每次 2 行）与其 docstring「仅写 entity_mapping」不符 —— 已记录，未修

## 部署验证记录

**修复前**（2026-09-30 上午，问题定位阶段实测）：

| 项 | 值 |
|---|---|
| `entity_mapping` 行数 | 354,411（1 垃圾行 + 3500 SUPPLIER + 350910 MATERIAL） |
| 同步 planned / 落库 | 354,422 / **354,410 MATERIAL + 3500 SUPPLIER**（少 12 行） |
| 全称解析 | `浙江力航汽车部件有限公司` → 未找到；真实问句命中 **5/8** |

**修复后**：

| 项 | 值 |
|---|---|
| 同步结果 | `planned=354422 affected=354422 erp_before=0` |
| `entity_mapping` 行数 | **354,422**（3500 SUPPLIER + 350922 MATERIAL） |
| 不同 `enterprise_key` 数 | 354,422（= 行数 ⇒ 零碰撞，跨 entity_type 亦无重叠） |
| 空 `name` / 垃圾行 | 0 / 0 |
| 键区间实测 | SUPPLIER `81324035971~281438446824712`；MATERIAL `281476003072863~562949535787384`（不重叠，落于各自 2⁴⁸ 段内） |
| 全称解析（真实词典，deployed 容器） | 真实问句 **8/8** 命中 B125 |
| 容器内代码校验 | `_bareNameCandidates` 存在、两处 `from app.domain.enterprise_key import` 存在、`KEY_RANGE_SIZE=281474976710656` |
| 健康 / 迁移 | `/api/v1/health` 200；`alembic current` = 0104 (head) |

**未覆盖**：`entity_mapping_service.searchMappings`（AdminUI AutoComplete 走这条路）
仍无 `name` 子句 ⇒ 下拉框按中文名搜不到。数据里 `name ilike '%浙江力航%'` 命中 1 行，
而该方法现有子句命中 0 行。已在 [data-model.md](../../wiki/data-model.md) 待办中标记，
**非本次修复范围**。
