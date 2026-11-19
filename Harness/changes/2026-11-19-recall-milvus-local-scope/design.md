# 向量检索按本地类 id 收敛到 Milvus `expr`（Gap A）设计

> 承接：[2026-11-19-recall-datasource-filter-anchor](../2026-11-19-recall-datasource-filter-anchor/design.md)（PR #157，数据源过滤 + 关键词锚定）
> 承接：[2026-11-19-recall-anchor-fallback-rescue](../2026-11-19-recall-anchor-fallback-rescue/design.md)（PR #158，Gap B 锚定兜底救援）
> 与 [2026-11-19-prod-incident-recall-window-miss](../2026-11-19-prod-incident-recall-window-miss/prod-incident-analysis.md)（2026-11-19 生产问法敏感漏选事故分析）
>
> 分支：`fix/recall-milvus-local-scope`（从 `origin/main` `7dc9d874` 切出）

## 1. 缺陷（Gap A = 根因）

Gap B（PR #158）已把「向量路径失效时的第二条腿」补上，但**根因仍在**：

```
WARNING app.services.recall_datasource_filter:召回候选跨源过滤 dropped=210 total=304 … datasourceId=1
WARNING app.services.chat_recall:本体类回退降级 reason=no_match hits=20 total=94 kept=43 …
```

链路：

1. **候选集已按数据源收窄**：`listClasses` 取全局 304 类 ⇒ `applyDatasourceRecallFilter` 筛到 **94 个本数据源类**（`dropped=210`）。这一层是对的。
2. **但向量检索没有跟着收窄**：`searchByKeyword(question, topK=20, typeFilter="class")` 打的是
   **Milvus 全局集合**（304 类 + 墓碑向量 + 重复向量）⇒ top20 名额被跨源类、墓碑类吃光。
3. **命中解析不出本地类** ⇒ `validHitsCount=0` ⇒ `no_match` 早退 ⇒ 走 Gap B 救援/兜底。

⇒ **数据源过滤只作用在「候选集」，没有作用在「向量检索窗口」**。二者之间隔着一层：
过滤在**应用侧**（Python），召回在**检索侧**（Milvus），两侧口径不一致 ⇒ 跨源类照样能占名额。

生产实证（PR #157 上线后日志）：`hits=20 total=94` —— 20 个向量命中里**能解析到本地 94 类的为 0**。

## 2. 方案：把本地类 id 集推进 Milvus `expr`

```
今天：  listClasses(304 全局) ──跨源过滤──► 94 本地类 ──► [候选集]
                                            │
       Milvus.search(全局 304) ─────────────┘  ← 两个口径不一致（缺口在这）
                                             │ 本地类 id 集
                                             ▼
       Milvus.search(expr="ontology_id in [本地 id 集]")  ← 两侧口径一致
```

### 2.1 实测证据：Milvus 2.4.6 的 `expr` 过滤行为（本地只读探针）

生产 Milvus 是 `milvusdb/milvus:v2.4.6`（`docker/docker-compose.yml:83`）。
本地只读探针（`default` 库 `ontology_class_embeddings`，59 行 / 59 个 distinct id，
**未写入任何数据**）实测：

| 探针 | 结果 |
|---|---|
| 无 `expr`，`limit=160` | 59 hits（= 全量） |
| `expr="ontology_id in [<19 个 id>]"`，`limit=160` | **19 hits，distinct=19，越界 0** |
| 同上，`limit=1000` | 19 hits（**不欠返回**） |
| `expr="ontology_id in [<全部 59 个 id>]"`，`limit=160` | 59 hits，越界 0 |
| `expr="ontology_id < 0"`（deny-all 形态） | **0 hits，语法合法** |
| `expr="ontology_id < 0 and ontology_id >= 0"`（逻辑恒假式） | **0 hits，语法合法**（`and` 在 2.4.6 可用） |
| `expr="ontology_id in [<30 个 id>]"`，`limit=59`（= 集合大小） | **期望 30 行 ⇒ 实返 30 行，越界 0（不欠返回）** |
| `search` 带 `expr`（随机向量，`limit=59`） | 命中 30 条，**越界 id 数 = 0**（无作用域时 59 条） |
| 10000 个 id 的 `in` 表达式（≈ 48.9 KB） | **语法 OK**（2000 个 id ≈ 13.8 KB 亦 OK） |

结论：
- **过滤不欠返回**：作用域内能召回的都被召回（Milvus 2.4 的过滤检索在该量级下是完备的）。
  ⇒ 「加 `expr` 会因为非索引标量字段而漏召回」的风险**实测排除**。
- **`ontology_id` 没有标量索引也不影响正确性**（本仓 v1 集合只建了 `embedding` 的 HNSW 索引）。
- deny-all 用**逻辑恒假式**表达（`ontology_id < 0 and ontology_id >= 0`）：不依赖
  「id 恒为正」这个**数据假设**，即使将来出现负 id 也恒空。
- **expr 规模余量充足**：生产量级 94 个本地类 ⇒ 396 字节；全局 304 类 ⇒ 1.6 KB；
  上限 2000 个 id ≈ 13.8 KB（Milvus 实测可解析）。⇒ 上限不是「刚好卡住」的脆弱阈值。

> 该探针只跑 `search`/`query`，**不写不删**。本地 Milvus 是生产数据面（见
> `syncEmbedding` 的注释），故本仓不做「插测试行再删」的验证。
> ⚠️ 本地集合只有 59 行（生产 304 类 + 重复向量）⇒ 探针证明的是**过滤语义正确**，
> 不是生产量级下的召回完备性；后者靠生产灰度日志（命中数/越界数）观测。

### 2.2 参数契约（**空集 ≠ 不限**）

新增可选参数 `classOntologyIds: Sequence[int] | None`：

| 取值 | 语义 |
|---|---|
| `None` | **不限**（不施加作用域）⇒ 与今天逐字节一致 |
| `[]`（空集） | **deny-all** ⇒ class 检索恒空（**绝不能**当成「不限」） |
| 非空 | 只保留这些本体类 id 的向量 |

**元素校验（越界即炸，不静默改写）**：必须是 `int`（`bool` 是 `int` 的子类 ⇒ 显式
排除）、必须 `>= 0`（本体类 id 是 DB 自增主键）。非法元素抛 `ValueError` ——
`int("12")` / `int(True)` 会把非法值悄悄改成**另一个合法 id** ⇒ 作用域指向别的类
⇒ 比报错更糟。

作用域**只作用于 `typeFilter == "class"` 的集合**：
`typeFilter=None` 的跨集合检索里，property/metric/kpi 集合各有各的 id 空间，
套 class 的 id 集是错的 ⇒ 只对 class 集合施加，其余集合不受限。

**id 空间前提（重要）**：作用域里的 id 必须是 **`ontology_class.id`（真实主键）**。
会话**钉住语义发布**时候选类来自发布快照，其 id 是按类名排序**合成**的 `1..N`
（`semantic_schema_adapter._synthesizeIds`）⇒ 与 Milvus 的 `ontology_id` 不是同一
空间 ⇒ 该路径**不施加作用域**（见 §4.6）。

### 2.3 调用链（单点注入）

```
chat_recall._selectRelevantClasses          ← 唯一注入点（typeFilter="class"）
  └─ OntologyService.searchByKeyword(..., classOntologyIds=...)
       └─ semantic_registry_rag_gate.searchRegistryEmbeddings(..., classOntologyIds=...)
            ├─ _searchV1 ─► milvus.searchEmbeddingsByTypeRouted(..., classOntologyIds=...)
            │                  └─ _searchCollection(..., expr=classScopeExpr)
            └─ _searchV2 ─► milvus_registry_v2.searchRegistryV2(..., expr=治理expr AND classScopeExpr)
```

**作用域来自哪**：`allClasses`（已按数据源过滤的候选集）的 id 集。
`_selectRelevantClasses` 已有 Gap B 引入的 `isDatasourceScoped` 标志：
- `True`（数据源过滤生效）⇒ 传本地 id 集 ⇒ 向量窗口与候选集口径一致。
- `False`（`no_datasource` 未选数据源 / `no_cache` 目录缺失）⇒ 传 `None` ⇒
  与今天完全一致（此时候选集本来就是全局的，两侧口径本来就一致）。

⇒ **同一个标志同时守住「宽窄一致」与「不收窄时不收窄」**，不需要第二个状态。

### 2.4 顺带修掉的两个已知脏东西（**只在 class 集合内**）

- **墓碑向量**：停用类（`valid_to` 墓碑）不在 `listClasses` 里 ⇒ 其 Milvus 向量被 `expr` 排除。
  生产事故里排第 1 的幽灵表 `main_data_supplier(id=270)` 就是墓碑类 ⇒ 本次一并出局。
- **跨源重复向量**：跨源类的 id 不在本地 id 集 ⇒ 排除（2.47× 重复向量问题**不修**，只是不再
  参与 class 检索窗口；写路径的墓碑向量未删仍是独立挂账）。

## 3. 语义矩阵

| 场景 | 数据源过滤 | 向量作用域 | 结果 |
|---|---|---|---|
| 选了数据源，过滤生效 | 生效（94/304） | `ontology_id in [本地 94]` | **窗口与候选集一致**（本批目标） |
| 未选数据源（`no_datasource`） | 跳过 | `None`（不限） | 与今天一致（候选集本来就全局） |
| 数据源目录缺失（`no_cache`） | fail-open 跳过 | `None` | 与今天一致 |
| 本地类为空 | 生效（0 类） | `[]` = deny-all | 恒空 ⇒ 走既有 `no_hits` 早退（`total == 0` 分支更早命中） |
| **钉住语义发布**（`view is not None`） | 生效（快照类也过数据源过滤） | **不施加**（`None`） | 快照 id 是**合成 id**，与 Milvus 主键不同空间 ⇒ 施加会过滤错空间（见 §4.6）；与今天一致 |
| **作用域 id 数超上限**（> 2000） | 生效 | 降级为**不限** + 告警 | expr 过长会让 Milvus 解析失败 ⇒ 被吞成 `search_error` ⇒ 静默退回本批要修的宽窗；显式降级更稳（宽窗仍受数据源过滤约束） |
| **作用域窗口为空**（本地类没有向量 / expr 过严） | 生效 | 一次**无作用域**重试 | 重试命中能解析出本地类 ⇒ 正常裁剪（自愈）；解析不出 ⇒ `no_match` ⇒ 锚定救援窄窗（与今天同状态一致）；重试仍空 ⇒ `no_hits` ⇒ 宽窗回退 |
| Gap B 救援路径 | — | — | **不受影响**（救援用锚定，不走向量） |

**降级路径不受影响**：`search_error` / `no_hits` / `no_match` / `ods_only_hits` 的分支结构与
Gap B 完全一致；本批只改「向量检索本身查哪些行」。

## 4. 变更设计

### 4.1 `milvus_client.py`（底层，两条检索路径共用）

```python
def buildClassIdScopeExpr(ontologyIds: Sequence[int] | None) -> str | None:
    """本体类 id 作用域 → Milvus ``expr``。

    - ``None`` ⇒ 不限（返回 None：不施加过滤）
    - 空集 ⇒ deny-all（``ontology_id < 0``；本体类 id 恒为正）
    - 非空 ⇒ ``ontology_id in [...]``
    """
```

放在 `milvus_client.py`（而非 `milvus_search.py`）的原因：v1（`milvus_search`）与
v2（`milvus_registry_v2`）都要用它，而两者都已 import `milvus_client` ⇒ 放在
`milvus_search` 会让 v2 反向依赖 v1 模块。

### 4.2 `milvus_search.py`（v1 路径）

- `_searchCollection(collectionName, queryEmbedding, topK, expr=None)`：把 `expr` 透传给
  `collection.search(...)`（**v1 集合今天不带 expr**，这是本次新增能力）。
- `searchEmbeddingsByTypeRouted(queryEmbedding, topK, typeFilter, classOntologyIds=None)`：
  `typeFilter == "class"` ⇒ 施加作用域；`typeFilter is None` ⇒ 只对 class 集合施加。

### 4.3 `milvus_registry_v2.py`（v2 路径）

`searchRegistryV2(..., expr=...)` 已有 `expr` 能力 ⇒ 本批只把**作用域 expr 与治理维度 expr
用 `and` 合并**（治理 expr 优先，不变）。

### 4.4 `semantic_registry_rag_gate.py`

`searchRegistryEmbeddings(queryEmbedding, topK, typeFilter, registryFilter, classOntologyIds=None)`
→ 透传给 v1 / v2 两条路径。

**v1 路径的 fail-closed 规则不变**：那条规则针对**治理维度**（domain/org/entity/release/validAt），
作用域是**数据源相关性**，两者语义不同 ⇒ v1 + 作用域 = 照常过滤检索（**不是** fail-closed）。

### 4.5 `ontology_service.py`

`searchByKeyword(query, *, topK, typeFilter, registryFilter, classOntologyIds=None)` → 透传。

### 4.6 `chat_recall.py`（唯一注入点）

```python
scopeIds: list[int] | None = None
if isDatasourceScoped:
    if view is not None:
        # 钉住语义发布 ⇒ 快照 id 是合成 id（1..N），与 Milvus 主键不同空间
        # ⇒ 不施加作用域（与今天逐字节一致）+ 告警
        logger.warning("... scene=pinned_scope_skipped")
    else:
        scopeIds = sorted({cls.id for cls in allClasses if cls.id is not None})
if scopeIds is not None and len(scopeIds) > _CLASS_SCOPE_MAX_IDS:
    logger.warning("... scene=class_scope_too_large")
    scopeIds = None            # 超限 ⇒ 显式降级为不限（宽窗仍受数据源过滤约束）
if scopeIds is None:
    # 无作用域 ⇒ 调用形态逐字不变（不多传 kwarg）：既有替身只认
    # ``(query, *, topK, typeFilter)``，多传 kwarg 会让它们整体炸掉。
    hits = await self._ontology.searchByKeyword(
        question, topK=topK, typeFilter="class"
    )
else:
    hits = await self._ontology.searchByKeyword(
        question, topK=topK, typeFilter="class", classOntologyIds=scopeIds
    )
    if not hits:
        # 作用域窗口为空 ⇒ 一次**无作用域**重试（自愈 + 与今天同口径）
        logger.warning("... scene=class_scope_bypass")
        hits = await self._ontology.searchByKeyword(
            question, topK=topK, typeFilter="class"
        )
```

**为什么「无作用域时不多传 kwarg」**：与闸门层 ``_routedSearch`` 同一口径 ——
「无作用域 = 与今天逐字节一致」这条不变量要落在**每一层的调用形态**上。
`searchByKeyword` 的既有替身（`test_chat_service.py` / `test_classify_and_recall.py` /
`test_multistep_global_filter.py` 等 10 余处）签名都是 ``(query, *, topK=5, typeFilter=None)``；
多传一个 kwarg ⇒ 替身抛 `TypeError` ⇒ 被 chat_recall 的 `except Exception` 吞成
`search_error` 降级 ⇒ **测试静默失去向量路径的覆盖**（断言仍绿，但测的不是同一条路）。

**为什么「作用域窗口为空」要重试而不是直接回落宽窗**：作用域生效后「窗口为空」有两种
成因 —— (a) 本地类在 Milvus 里**没有向量**（embedding 缺口）；(b) expr 过滤过严。
今天（无作用域）在 (a) 这种状态下是「全局窗口命中跨源类 ⇒ `no_match` ⇒ 锚定救援窄窗」；
若作用域路径直接回落宽窗，就会**比今天更宽**（43 个本地类全进 schema ⇒ 召回裁剪被
架空，正是本批要消灭的症状）⇒ 行为回退。故：重试一次无作用域检索，让三种结果各自
落到与今天同口径的分支（正常裁剪 / 锚定救援 / 宽窗回退）。重试是**有意的作用域绕过**
⇒ 记 `WARNING` + `scene=class_scope_bypass`（不用 `reason=`：回退率按「含 `reason=`
的行」聚合，这里不是召回回退）。

**为什么钉住路径不施加作用域**：快照类的 id 是**合成 id**（按类名排序分配的 `1..N`），
与 Milvus 的 `ontology_id`（`ontology_class.id` 自增主键）**不是同一 id 空间**。
拿合成 id 去过滤真实主键会同时踩三个坑：(a) 真本地类的向量被挡在窗口外 ⇒ 作用域检索
恒空；(b) 合成 id 恰好撞上别的真实主键 ⇒ 跨源/墓碑类的向量又进窗口；(c) 命中 id（真实
主键）与快照类 id（合成）错映射 ⇒ 无关类的属性被喂给 planner。该路径的 id 空间错配是
**既有缺陷**（不施加作用域时也存在），另批根治（见 summary 遗留挂账）。

**注意**：作用域**只收窄向量检索**，不改变「命中解析不出类 ⇒ 走降级」的结构；
命中解析逻辑（`hitIds` → `relevant`）逐字不变。

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| `expr` 过滤在生产大集合上欠返回（top20 拿不满） | **本地只读探针实测不欠返回**（§2.1）；集合量级（304 类 + 重复）远小于探针上限；`limit` 仍是 `topK × 8` |
| `expr` 构造异常炸上层 | 构造是纯字符串拼接（整数列表）⇒ 不会异常；`MilvusError` 由既有 `except` 兜住 ⇒ 走 `search_error` 降级 |
| 作用域 id 集与 Milvus 的 `ontology_id` 不是同一 id 空间 | Milvus 的 `ontology_id` = `ontology_class.id`（`syncEmbedding(ontologyId, ...)` 写入的就是类 id；命中侧 `hit.id` 直接与 `cls.id` 比对）⇒ **直读治理表路径**同一 id 空间；⚠️ **钉住语义发布路径不成立**（快照 id 是合成 id）⇒ 该路径不施加作用域（§4.6） |
| 空作用域被当成「不限」 | `buildClassIdScopeExpr([])` → deny-all 表达式；单测覆盖 `None` / `[]` / 非空三态 |
| 作用域让向量结果变少 ⇒ `matchedRatio` 阈值误判 | 作用域后命中全部是本地类 ⇒ `matchedRatio` 恒高 ⇒ 只会**更容易**通过 `CLASS_FILTER_HIT_MATCH_MIN` |
| 未选数据源时误收窄 | `isDatasourceScoped=False` ⇒ 传 `None` ⇒ 与今天逐字节一致 |
| v2 路径（开关打开）治理维度与作用域互相覆盖 | 用 `and` 合并，不覆盖 |
| `expr` 过长 ⇒ Milvus 解析失败（被吞成 `search_error` ⇒ 静默退回宽窗） | 作用域 id 数上限 2000（≈ 13.8 KB，实测 Milvus 可解析 48.9 KB）；超限 ⇒ 显式降级为不限 + `WARNING`（可见），不静默失效 |
| 作用域 id 含非法值（`bool` / 负数 / 非 int） | `buildClassIdScopeExpr` 显式校验 ⇒ 抛 `ValueError`（不静默 `int()` 改写）；`ontology_service` 把 `ValueError`/`TypeError` 翻译成 `MilvusError`（领域异常 ⇒ 4xx，不是 500） |
| 作用域窗口为空 ⇒ 直接宽窗比今天更宽 | 改为「无作用域重试一次」：命中解析不出本地类 ⇒ `no_match` ⇒ 锚定救援窄窗（与今天同状态一致）；重试仍空 ⇒ `no_hits` ⇒ 宽窗（行为不变）。绕过事件记 `WARNING` + `scene=class_scope_bypass` |


## 6. 测试策略（TDD：先红后绿）

- **单测** `backend/app/tests/unit/test_milvus_class_id_scope.py`（新建）：
  - `buildClassIdScopeExpr` 三态（`None` / `[]` / 非空）与语法形态；
  - deny-all 是**逻辑恒假式**（对正/负/0 id 一律不命中）；
  - 非法元素（`bool` / 负数 / 非 int）⇒ `ValueError`（不静默改写）；
  - `searchEmbeddingsByTypeRouted` 在 `typeFilter="class"` 时把 expr 传给 `_searchCollection`，
    `typeFilter=None` 时**只**对 class 集合施加；
  - v2 路径：`searchRegistryV2` 的 expr = 治理 expr `and` 作用域 expr。
- **接线单测** `backend/app/tests/unit/test_recall_anchor_wiring.py`（并入既有文件）：
  - `isDatasourceScoped=True` ⇒ `searchByKeyword` 收到本地 id 集；
  - `isDatasourceScoped=False` ⇒ 收到 `None`（**严格替身**反证：多传 kwarg 会 TypeError）；
  - 作用域 id 集**去重 + 升序**；
  - 作用域**超上限** ⇒ 显式降级为不限 + 告警（且不污染回退率聚合）；
  - 作用域窗口为空 ⇒ **无作用域重试**一次（自愈 / 锚定救援 / 宽窗三态各有断言）；
  - **钉住语义发布视图** ⇒ 不施加作用域（合成 id ≠ 主键）。
- **集成** `backend/app/tests/integration/test_classify_and_recall.py`：
  - 跨源类向量命中 + 本地类向量命中 ⇒ 作用域生效后命中只含本地类。
- **门禁**：`merge-gate / failure-set-diff` 净新增失败 0。

## 7. 与 Gap B 的关系

| | Gap B（PR #158） | Gap A（本批） |
|---|---|---|
| 定位 | **兜底**：向量路径失效时的第二条腿 | **根因**：让向量路径本身不失效 |
| 触发 | `no_match` / `ods_only_hits` | 每次 class 向量检索 |
| 效果 | 降级时窗口更准 | `no_match` 本身大幅减少 ⇒ Gap B 很少触发 |

两者**互补且不冲突**：Gap A 生效后向量命中直接落在本地类上；Gap A 未生效/失效
（Milvus 侧不支持过滤、集合缺失）时 Gap B 兜住。两者共用 `isDatasourceScoped` 标志。

**实施后补记（2026-11-20）**：作用域 = 候选集 id 集，而命中解析用的 `classById` 也建自
**同一个候选集** ⇒ 落在作用域内的命中**必然可解析** ⇒ 正常路径下 `no_match` 分支
**不可达** ⇒ Gap B 的 `no_match` 锚定救援退化为**防御层**（Milvus 侧不支持过滤 / 集合缺失 /
id 空间漂移等异常场景）。`ods_only_hits` 救援分支仍可达（ODS 过滤发生在解析**之后**）。
这不是能力回退：Gap A 让向量路径直接命中本地类，Gap B 的窄窗救援不需要被触发。

**Gap B 的 `no_hits` 语义保持不变**（「检索基础设施故障不接管 ⇒ 宽窗」）：
作用域路径下 `no_hits` 的成因多了一种「本地类没有向量」⇒ 那里**不能**直接宽窗（会比
今天更宽）⇒ 用一次**无作用域重试**把结果分流到与今天同口径的分支（自愈 / 锚定救援 /
宽窗）。重试仍空 ⇒ 依旧是 `no_hits` ⇒ 宽窗 ⇒ Gap B 的语义逐字不变。

## 8. 不在本批范围（既有挂账）

- Milvus 写路径：墓碑向量未删 + 2.47× 重复向量（本批只在 class 检索窗口内绕开，不修写路径）。
- few-shot 未按窗口约束（`query_embeddings` 检索不带窗口约束）。
- 校验器「窗口未覆盖 vs 本体不存在」措辞。
- `chat_recall.py` 体量超 800 行上限（既有欠账）。
- **钉住语义发布路径的 id 空间错配**（既有缺陷）：快照类 id 是合成 id（1..N），
  与 Milvus 主键、`classById` 的键都不同空间 ⇒ 该路径的向量命中按 id 解析本就不可靠。
  本批**不施加作用域**以免把错空间问题放大；根治需「快照类名 → 真实主键」的映射与
  命中 id 回译（另批立项）。
- 研究入口的 BO 臂仍是 Milvus 全局检索（无数据源维度）⇒ 作用域不覆盖该臂。
