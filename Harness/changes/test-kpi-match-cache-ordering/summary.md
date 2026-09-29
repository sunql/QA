# 变更：test-kpi-match-cache-ordering

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：评估剩余项批次 **Phase B**（M10，**改判**：放弃倒排索引实现，改为对真实实现的差分属性测试）
- **状态**：done
- **关联变更**：同批 [fix-chat-disconnect-persistence](../fix-chat-disconnect-persistence/summary.md)、[fix-llm-transient-retry](../fix-llm-transient-retry/summary.md)、[chore-l3-deadcode-and-prior-cte-contract](../chore-l3-deadcode-and-prior-cte-contract/summary.md)、[fix-embedding-provider-type-guard](../fix-embedding-provider-type-guard/summary.md)、[fix-milvus-list-all-pagination](../fix-milvus-list-all-pagination/summary.md)
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 **M10** + §3 P3 第 18 项（评估日期 2026-09-25）
- **commit**：`7442f8e`（纯 `test:` 变更；另有一条注释更正的跟进提交，见 §6）

---

## 1. 需求

评估原文 M10 是「把 `KpiMatchCache._by_keyword` 的线性子串扫描换成倒排索引」。**准入前压测推翻了这个前提**（详见 §2 的三条实测），因此本条目**改判**为做它下面**真正缺的东西**：

`KpiMatchCache` 是 KPI 语义匹配的快路径，其**输出顺序**由实现细节决定（`_by_keyword` 的**插入序即输出序**，`kpi_match_cache.py:68-87`）。而现有测试用的**全是自行重写了一套不同算法的桩**（`test_kpi_semantic_match_service.py::_StubCache`：外层遍历 KPI、命中即 `break`，按目录序）⇒ **真实实现的顺序零覆盖**，任何一次「顺手改成 sorted / dict 重建」都会静默改变候选顺序（下游 `kpi_semantic_match_service` 按 Jaccard 排序，相同分数时**顺序即结果**）。

**验收标准**：① 对**真实 `KpiMatchCache`**（非替身）建立**差分**校验：测试侧独立复算「文档化的匹配规则 + 顺序规则」，与缓存输出逐条比对；② 覆盖重叠关键词、子串命中、大小写、重复关键词、无命中、空关键词；③ `refreshOne` / `onKpiChanged` **前后同序同集**（identity 语义按实现文档化）；④ **不改任何生产代码**。

## 2. 设计评审

### 改判依据（三条实测，2026-09-26）

| 维度 | 实测 |
|---|---|
| **收益为零** | 真实目录只有 **13 个 KPI**（`scripts/seed_kpi_semantic_index.py`，约 50 个关键词），线性扫描（`kpi_match_cache.py:81-86`）本就是**亚毫秒级** ⇒ 倒排索引买不到任何**可测**收益 |
| **代价是两处静默偏离** | ① 空关键词 `""`：`"" in s` **恒真**，旧实现命中**全部** KPI，而子串索引永远不含 `""` ⇒ 返回 `[]`；② `refreshOne` / `onKpiChanged`（`:140-168` / `:116-138`）只改 `_by_keyword`，索引**不重建** ⇒ 改动过的关键词**漏匹配** + 未改动的关键词**顺序漂移** |
| **不 scale** | 即便按原计划实现：500 关键词 × 30 字实测 **+42.3 MB** 纯索引开销；5,000 关键词约 **420 MB** |

⇒ **放弃索引**（用户已批准的方案变更）。本条目下真正的风险是**覆盖缺失**，故改为差分属性测试。

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 原计划的子串倒排索引 | 否决（三条实测，见上）；若将来切回，正确形态是 §6 末尾的最小修正版 |
| B | 只加「输出顺序」的快照断言（golden） | 否决：快照断言的期望值来自**实现输出**，是自证；实现改了快照跟着改，测不出漂移 |
| C | **差分属性测试**（选定） | 测试侧用朴素实现独立复算「匹配规则」（用户关键词 × 目录关键词的子串匹配、大小写不敏感、去重）与「顺序规则」（用户关键词外层、`_by_keyword` 插入序内层），再与缓存输出逐条比对 —— **不是把缓存输出抄一遍当期望值** |
| D | 顺手改掉「`[""]` 命中全部」这个怪癖 | **否决（本批口径）**：这是**语义判定**问题（算不算缺陷需产品判断），本批只**钉死现状**、不偷偷改 —— 已登记为后续独立条目 |

### 文档化并钉死的两条实现语义

1. **空关键词**：`findByAnyKeyword([""])` 命中**全部「有关键词」的 KPI**（无关键词的 KPI 不在内）—— 现状语义，测试钉死，改不改另议；
2. **`refreshOne` 的尾移怪癖**：刷新会**重新查询该行**并把其关键词追加到 `_by_keyword` 插入序**末尾** ⇒ 命中集合不变但**顺序会翻转**（`before=['A','B'] after=['B','A']`）。这是**已知怪癖**，与既有单测同口径钉死，避免「无意的顺序漂移」与「有意的尾移」混为一谈。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本；**不改 Milvus / PG 的 KPI 数据**。

## 4. 接口契约变更

**无生产代码变更**（纯 `test:`）。本项建立的是**测试契约**：

| 契约 | 内容 |
|---|---|
| 匹配规则 | 用户关键词是**目录关键词的子串**（不反向）；大小写不敏感；同一 KPI 多关键词命中只出现一次 |
| 顺序规则 | 用户关键词**外层**、目录关键词 `_by_keyword` **插入序**内层；按此顺序去重收集 |
| 写路径语义 | `refreshOne` 后命中集合不变、身份是新读到的那行（非陈旧命中）、被刷新关键词移到插入序末尾；`onKpiChanged` 全量/单条失效语义 |
| 未预热语义 | 未预热时读路径**安静降级**（空结果），`getAll` 抛错（因为「未预热还能拿到目录」本身是启动期缺陷） |

## 5. 实现要点

| 位置 | 改动 |
|---|---|
| `app/tests/unit/test_kpi_match_cache.py`（**新增 363 行，唯一改动**） | 用**桩会话**喂 `Row(kpi_code, semantic_keywords)`，驱动**真实** `KpiMatchCache`（`warmUp` / `findByAnyKeyword` / `refreshOne` / `onKpiChanged` / `getAll`）；测试侧 `_expectedHitSet` / `_expectedOrder` 为独立复算；分 4 个测试类：命中与顺序 / 空与边界 / 写路径失效 / 未预热语义 |
| 生产代码 | **零改动**（`app/services/kpi_match_cache.py` 未触碰） |
| 后续注释更正 | 文件 docstring 原并列引用 `test_kpi_catalog_api.py` 的桩，**复核后该文件与关键词匹配路径无关**（0 处 `findByAnyKeyword`）—— 属计划笔误，已在跟进提交里更正为「全树只有这一处关键词匹配桩」（见 §6） |

## 6. 测试

- **单元 `test_kpi_match_cache.py` 17 passed**（本批聚焦 6 文件 91 passed 的一部分）。
- **容器内真机探针（`/tmp/probe_m10.py`，15 项全 PASS，2026-09-27）** —— 合成语料 `Row("A", ["销售额","销售额增长率"]) / Row("B", ["销售额增长率"]) / Row("C", ["库存周转率"]) / Row("D", None)`，驱动**部署物里的真实缓存**：

  ```
  PASS  预热后 PUBLISHED KPI 全部入 code 索引  | ['A', 'B', 'C', 'D']
  PASS  NULL 关键词的 KPI 仍可 by_code 命中
        目录关键词数 = 3（A/B/C 共 3 个去重关键词）
  PASS  重叠关键词命中 A,B 且无重复（A 只出现一次）  | ['A', 'B']
  PASS  短词命中长关键词（销售额 ⊂ 销售额增长率）  | ['A', 'B']
  PASS  无命中语料返回空 / 大小写不敏感 / 重复用户关键词不产生重复结果
  PASS  空关键词语义钉死：[""] 命中全部「有关键词」的 KPI（D 无关键词故不在内）  | ['A', 'B', 'C']
  PASS  refreshOne 后命中集合不变  | ['B', 'A']
  PASS  refreshOne 索引的是本次新读到的行（旧对象被换掉，非陈旧命中）
  PASS  refreshOne 把被刷新的关键词挪到插入序末尾（已知怪癖，与单测同口径）  | before=['A', 'B'] after=['B', 'A']
  PASS  改关键词后旧词不再命中 A / 新词命中 A
  PASS  全量失效后未预热态返回空
  ```

- **prod 数据现实（重要校准，写入评估文档）**：`kpi_catalog` 现 12 个 KPI、`semantic_keywords` **全为 NULL** ⇒ **快路径在生产上目前是惰性的**（任何关键词查询命中 0 条）。批内综合探针（`probe_batch.py` 的 M10 段）首跑因此报 1 FAIL（`"" → 0 / 12`），经诊断**不是产品缺陷而是断言与数据不符**：该探针写成「`""` 命中全部目录 KPI」，但目录里根本没有关键词。已改为**数据感知**断言（无关键词时显式标注「本项在 prod 数据上不可判，改由合成语料探针证明」）——现 28 项全 PASS。
- **注释更正（跟进提交）**：`test_kpi_match_cache.py` 的 docstring 原写「`test_kpi_semantic_match_service.py::_StubCache`、`test_kpi_catalog_api.py` 的 fake 都是外层遍历 KPI、命中即 break」；复核发现 `test_kpi_catalog_api.py` 与关键词匹配路径**毫无关系**（`grep -c findByAnyKeyword` = 0）⇒ 该并列引用是**计划文本里的笔误**，已改为只引用真实存在的那一处，并注明更正原因。**这是文档准确性问题，不影响任何断言**（该文件 17 例在更正前后均全绿）。
- **若将来要切回索引**（保留记录，最小正确形态，四条**一条都不能省**）：① 索引必须**在每次 `_by_keyword` 变更时同步重建**；② key 取 `_by_keyword` 插入序（顺序即契约）；③ `index[""] = list(by_keyword)` 保住空关键词语义；④ 只索引**目录关键词**的子串，且 `len(kw) > 上界` 时回退线性扫描（不可只索引短目录关键词 —— 那会让短用户词漏掉长目录关键词）。更正确的结构是 Aho-Corasick / 后缀自动机，而非子串枚举。

## 7. 安全审查

**未触发**：零生产代码改动、无认证/密钥/SQL/用户输入/加解密/支付变更。测试用桩会话不含真实凭据。

## 8. 部署验证（2026-09-27）

- **部署物**：本项**不产生部署物差异**（纯测试文件）；容器 md5 校验中 `app/tests/unit/test_kpi_match_cache.py` **MATCH**（本批 22/22 现存文件 MATCH）⇒ 仓库与容器一致，探针跑的就是本文件之外的**真实缓存实现**；
- **容器内真机探针 15 项全 PASS**（输出见 §6）—— 本项的「验收」本质上就是这组探针 + 单测的双重证据（同一实现、两种驱动方式）；
- **测试**：全量 unit+services **2 failed, 2522 passed, 1 skipped**（两条为**预存**失败，`git worktree add --detach` 在基线复现判别，delta = 0）；集成切片 120 passed + 3 例环境耦合（导出 `DATABASE_URL` 后 3/3 通过）；
- **静态检查**：新增文件 `All checks passed`；本批 22 文件 ruff 与基线逐行一致（唯一 F841 为基线既有）；
- **网关**：`/api/v1/health` 直连 8000 与经 nginx 5173 均 200。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.3 M10（标 **✅（改判）**，含三条实测理由与「prod `semantic_keywords` 全为 NULL ⇒ 快路径惰性」的校准）、§3 P3 第 18 项（划掉 + 注明改判）、§15 批次记录
- 代码契约：`app/tests/unit/test_kpi_match_cache.py`（差分属性测试）
- 被钉死的实现：`app/services/kpi_match_cache.py`（`findByAnyKeyword:68-87`、`onKpiChanged:116-138`、`refreshOne:140-173`）—— **本项不改它，只钉死它**
- 下游顺序敏感点：`app/services/kpi_semantic_match_service.py`（候选按 Jaccard 排序，相同分数时顺序敏感）
- 后续登记（**不在本批做**）：KPI 关键词索引（Aho-Corasick）；**空关键词语义**（`findByAnyKeyword([""])` 命中全部）是否算缺陷；**失败尝试的 LLM 用量采集**（需 `openai_client` 失败路径也带 usage）
- 规则：`Harness/rules/测试规范.md`（真实数据库/真实对象优先；本项用真实实现 + 桩会话）、根 `CLAUDE.md` 核心约束 #4（TDD）、`Harness/rules/开发流程规范.md`
- Memory：`qa-system-*` 新增 M10 条目（**改判** + 「现有 KPI 测试用重写算法的桩，真实顺序零覆盖」+ prod 关键词为 NULL 的校准）
- 同批：`../fix-chat-disconnect-persistence/`、`../fix-llm-transient-retry/`、`../chore-l3-deadcode-and-prior-cte-contract/`、`../fix-embedding-provider-type-guard/`、`../fix-milvus-list-all-pagination/`

## SSOT 校验清单

- [x] 第 1 段 需求：真实顺序零覆盖（桩重写了另一套算法，KPI 外层 + `break`，按目录序）+ 下游顺序敏感性 + 4 条验收标准
- [x] 第 2 段 **改判依据三条实测**（13 KPI 无可测收益 / 两处静默偏离 / 42.3 MB 不 scale）+ 4 个候选方案（含否决 golden 快照与「顺手改怪癖」）+ 两条被钉死的实现语义
- [x] 第 3 段 无迁移、不改数据
- [x] 第 4 段 **零生产代码改动**，列出所建立的四条测试契约（匹配/顺序/写路径/未预热）
- [x] 第 5 段 实现要点（新增 363 行、唯一改动）+ 跟进注释更正
- [x] 第 6 段 测试：17 例 + **15 项真机探针实录** + **prod 数据校准**（关键词全 NULL ⇒ 快路径惰性）+ 综合探针 1 FAIL 的诊断与修正 + 计划笔误的更正说明 + 索引最小正确形态存档（四条修正）
- [x] 第 7 段 安全审查：未触发（零生产改动）
- [x] 第 8 段 部署验证：无部署物差异 + md5 MATCH + 探针 + 全量测试与预存失败判别 + ruff delta 0 + 网关 200
- [x] 第 9 段 跨文件链接（评估文档含改判标注 / 测试契约 / 被钉死的实现 / 下游敏感点 / 后续登记 / 规则 / Memory / 同批）
