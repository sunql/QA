# 生产事故分析：问法敏感漏选「公司目前有多少供应商」

- 日期：2026-11-19
- 现象：生产「公司目前有多少供应商」→ 礼貌拒绝 + 误报「本体 schema 中不存在类 DIM_SUPPLIER」；
  「公司有多少供应商」→ 正常出 SQL 并执行。
- 状态：**分析完成，未改代码**。本文件是交接给 `fix/recall-datasource-anchor` 批次的输入。

---

## 一、结论（生产数据实测，已坐实）

**本体里 DIM_SUPPLIER 存在**（生产 `ontology_class` id=12，别名「供应商主数据」，`valid_to IS NULL`）。
校验器那句「本体 schema 中不存在类 DIM_SUPPLIER」**是错的**——它看到的是**本轮召回窗口**（≤50 类），
不是本体全集。系统把「**这一轮没召回到**」误报成了「**本体里根本没有**」。

### 实测数据

生产 `searchByKeyword(q, topK=50, typeFilter="class")`（容器内跑，与对话链路同一条代码路径）：

| 问句 | DIM_SUPPLIER(id=12) 名次 | score | topK=20 时 |
|---|---|---|---|
| 公司有多少供应商 | **15** | 0.5195 | 进窗口 ✔ |
| 公司目前有多少供应商 | **27** | 0.5084 | **被挤出** ✘ |

**「目前」两个字 = 把 DIM_SUPPLIER 从第 15 名推到第 27 名，跨过 topK=20 的悬崖。**
两问句 top-20 集合差分：

- 仅「无目前」有：`DIM_SUPPLIER(12)`、`Supplier(2)`、`supplier_material_drawing_grant(285)`
- 仅「有目前」有：`supply_property_overview(240)`、`supply_alteration_info(228)`、`system_order_strategy(300)`

⇒ 「目前」的净效果 = **把主数据类挤掉，换进 3 个无关类**。

### 权威配置（生产 `system_config` 实读）

| key | value |
|---|---|
| `CLASS_FILTER_TOP_K` | 20 |
| `CLASS_FILTER_MAX_CLASSES` | 50 |
| `CLASS_FILTER_HIT_MATCH_MIN` | 0.5 |
| `FEW_SHOT_SIMILARITY_MIN` | 0.6 |
| `FEW_SHOT_TOP_K` | 3 |
| `FEW_SHOT_EXAMPLE_LIMIT` | 400 |
| `ANCHOR_MAX_CLASSES` | **不存在**（锚定代码尚未上生产） |

环境：`ENABLE_SEMANTIC_REGISTRY_RAG` / `ENABLE_SEMANTIC_REGISTRY_NL2SQL` **均未设置 ⇒ 走默认 False**
⇒ 走 v1 集合 `ontology_class_embeddings`；`ontology_class_embeddings_v2` **0 行（不存在）** ⇒
**本地用 v1 做的复现结论可外推到生产**。

---

## 二、完整因果链（每环均有生产证据）

### 环 1 · 向量召回（真正的触发点）
`chat_recall.py:_selectRelevantClasses` → `searchByKeyword(question, topK=20, typeFilter="class")`。
整句 embedding 排名，「目前」把语义拉向「当前/现状」⇒ 主数据类掉出 topK。
**决定性旁证**：拒绝文案里列的 6 张表
（`supplier_portrait_snapshot / supply_access_bank / supply_access_liaison / supply_access /
upload_file_link_supplier / report_arrival_export_supplier`）
与「有目前」top-20 逐一对上——**唯一差别是第 1 名 `main_data_supplier`(id=270) 被剔除后，
由 `report_arrival_export_supplier`(id=215) 补位**。
⇒ 文案 = **召回窗口按层序排序后的前 6**（`unanswerable_suggestion.py` 硬上限 6）。

### 环 2 · 图层扩边（结构上救不回来）
`_expandByJoinNeighbors` 双向建边但**贪心 + 50 上限截断**：日志
`类召回扩边 hits=19 expanded=31 total=50` + `类召回扩边截断 total=50 cap=50`。
- 窗口头部那 6 张访问类在 JOIN 目录里**出边为 0（孤岛）**；
- DIM_SUPPLIER 的邻居（`DWD_AP_INVOICE_DTL` / `DWD_PURCHASE_ORDER_DTL` /
  `DWD_GOODS_RECEIPT_DTL` / `DWD_ARRIVAL_ORDER_DTL`）**都不在向量 top-20**；
- 扩边在轮到 DIM_SUPPLIER 之前就被 50 上限截断 ⇒ **扩边救不回**。

### 环 3 · LLM 计划（不是幻觉，是抄 few-shot）
生产 Milvus `query_embeddings` 里存着历史成功 SQL：
「公司有多少供应商 → `SELECT COUNT(BPSNUM_0) FROM THBI.DIM_SUPPLIER WHERE ENAFLG_0 = 2`」等。
`_buildFewShot` 用**同一 embedding**、阈值 0.6 检索历史 SQL 注入计划 prompt，**且不约束当前窗口**。
⇒ 「目前」变体必然命中该示例 ⇒ LLM 照抄 `DIM_SUPPLIER + BPSNUM_0`。
**LLM 计划是对的（口径也对），是环境把它判死的。**

### 环 4 · 校验（误报 + 有害提示）
`nl2sql_plan.py:437 validatePlan` 的类表只由**窗口**构造 ⇒ 报「不在本体 schema 中」（**假**）。
`_classNameHint`（`nl2sql_refs.py:216-252`）按**编辑距离**给替代类名：
生产实测该轮给出 `Supplier`、`supply_access`、`order_batch_msg`——
其中 `Supplier`(名次 26) 与 `order_batch_msg`(名次 28) **都不在窗口内**
⇒ **照着提示改依然会被拒**，形成死循环。

### 环 5 · 重试（结构性死路）
`chat_stream_query.py:171`「不可回答短句升级追问重试」用**同一问题、同一窗口**重跑
⇒ 召回不可能变化 ⇒ 必然二次失败（生产日志两次校验失败逐字重复），**纯烧 token**。

---

## 三、附带查实的问题（在办批次未覆盖）

| # | 问题 | 生产证据 |
|---|---|---|
| 1 | **墓碑向量白占 topK 名额**：id 270 `main_data_supplier` 于 2026-11-19 08:28:51 墓碑，**但 Milvus 向量未清理**，且它在两个问句里都是**第 1 名** ⇒ 每轮稳定浪费 1 个最贵名额 | `pruned=18/19 < hits=20`；`ontology_class_embeddings` 去重 id 数=304 而活类=303；旧墓碑 8/9/46 的向量**已**清理（id 列表中缺 8,9,46）⇒ **只有最新那次墓碑漏清理** |
| 2 | **Milvus 类向量重复 2.47 倍**：751 行 / 304 个不同 id ⇒ 吃 over-fetch 预算（`limit=topK×8=160`），浪费召回多样性 | 容器内实测 |
| 3 | **校验器给出的替代类名有 2/3 不在窗口内** | 生产日志 `最相似的真实类名有：Supplier、supply_access、order_batch_msg` |
| 4 | **few-shot 未受窗口约束 + 同一问句存 5 种互相矛盾口径** | 生产日志同一问句在两次尝试里分别产出 `DIM_SUPPLIER/BPSNUM_0` 与 `DWD_BUSINESS_PARTNER/PARTNER_CODE` 两种口径，**后者名次 40，也在窗口外** |
| 5 | **拒绝文案只列窗口前 6（层序）**，且措辞说「没有相关业务数据」——但业务数据**存在**，只是没召回到 | 用户可见的错误信息本身是错的 |
| 6 | 图层对孤立家族完全无效（本地 JOIN 目录仅覆盖 66 活类中的 28 个） | 生产/本地 `ontology_join` 实测 |

---

## 四、对在办批次 `fix/recall-datasource-anchor` 的判定

### ✅ 已确认有效
- 生产走的是**向量正常路径**（日志 `本体类裁剪完成 pruned=… total=304 hits=20`，
  **无** `本体类回退降级 reason=search_error`）⇒ 锚定挂在正常路径上**会生效**。
- 接线正确：锚定结果 `newAnchored + relevant` **排在向量命中之前**，`MAX_CLASSES=50` 截断**挤不掉**锚定类；
  候选集是**数据源过滤后的活类**（`applyDatasourceRecallFilter` 先跑）⇒ 跨源过滤与锚定不互斥。
- `DIM_SUPPLIER` 别名「供应商主数据」含 3 字片段「供应商」⇒ **锚定能字面命中它**。

### ✅ 缺口 1（已证伪，风险为零）：`ANCHOR_MAX_CLASSES=5` + 末位 `cls.id` 升序
**2026-11-19 生产别名数据实测**（§五 探针，300 活类全集）：
问句「公司目前有多少供应商」「公司目前有多少生产类供应商」「公司有多少供应商」三问句结果**完全一致**：

```
1. id=12  exact=0 fraglen=3  DIM_SUPPLIER  alias=供应商主数据   <<<<
2. id=10  exact=0 fraglen=2  DIM_FACILITY  alias=公司及工厂的信息
=> DIM_SUPPLIER(id=12) 锚定排名: 1
```

⇒ 三条结论：
1. **DIM_SUPPLIER 锚定排名第 1**，远在上限 5 内 ⇒ **本批次修得好这个 case 的向量漏选**。
2. **锚定竞争集只有 1 个类**：全量 300 活类中，除 DIM_SUPPLIER 外**没有任何类**的
   `class_alias`/`class_name` 含「供应/应商」⇒ 不存在「同分 (0,3) 靠 id 决胜」的竞争
   （`id=2 Supplier` 的别名不含「供应商」，**不参与**锚定）。
3. **`exact=1` 档不存在**：没有任何活类的别名**恰好等于**问句片段 ⇒ `exact` 档不参与竞争。
   唯一的第 2 名 `DIM_FACILITY` 是「公司」2 字片段的**误命中**（无害，只占 1 个名额）。
⇒ **缺口 1 撤销，不需要改排序键、不需要改 `ANCHOR_MAX_CLASSES` 语义、不需要重跑 CI。**

### ⚠️ 缺口 2（高）：锚定只补「向量的洞」，补不了「few-shot 的洞」
`DWD_BUSINESS_PARTNER`(id=19，别名「合作伙伴主数据」) 名次 40，**别名不含问句任何片段 ⇒ 不会被锚定**。
而 few-shot 明确注入了它的口径（生产日志实见）⇒ **LLM 仍可能选它 ⇒ 仍被拒**。
⇒ 必须补：**few-shot 注入前按当前窗口过滤**（剔除引用了窗口外类的历史 SQL），
或**校验失败时区分「类不在窗口」→ 扩窗重试**，而不是硬拒 + 同窗口重试。

#### 缺口 2 是本 case 的**唯一残留风险**（缺口 1 证伪后）

**上线后判别口径（看失败轮次报的是哪个类）**：

| 失败日志里的类 | 含义 | 处置 |
|---|---|---|
| `DWD_BUSINESS_PARTNER` | **缺口 2 在起作用**（few-shot 把 LLM 引到窗口外的口径） | 须补 few-shot 窗口过滤 / 扩窗重问 |
| `DIM_SUPPLIER` | 锚定**没生效**（不该发生：锚定把它 prepend 到窗口第 1 位） | 查锚定是否被数据源过滤洗掉 |

**低成本止血（若上线后仍偶发失败）**：`system_config.CLASS_FILTER_TOP_K` 20 → 30
⇒ DIM_SUPPLIER 去重名次 27 ≤ 30 ⇒ 进窗口；**运行期现读，改完对新问句立即生效、无需发版**。
代价：schema 文本多 10 个类（token 上升）。注意它**修不了** DWD_BUSINESS_PARTNER（名次 40）。
（锚定上线后通常不需要此项。）

### ⚠️ 缺口 3（中）：墓碑向量占名额，数据源过滤不回收名额
`applyDatasourceRecallFilter` 是**召回之后**的过滤 ⇒ id 270 仍占 topK 第 1 名。
⇒ 需在 Milvus 侧清理墓碑类向量（同步路径补 delete），或召回时按有效 id 预筛再取 topK。

### ➕ 建议补（不阻断，但同批可做）
- **拒绝文案**改为按相关度排序、且措辞从「没有相关业务数据」改为「本轮未召回到相关业务数据」。
- **校验器**区分「窗口未覆盖」与「本体不存在」：前者应触发**扩窗重问**而非拒答。
- **`_fallbackRecall` 补锚定**（本 case 不触发，但同缺陷仍在降级路径）。
- **重试路径**区分失败类型：窗口类失败不得用同一窗口重试。

---

## 五、决定性探针（生产只读，跑完即可收口）

目标：**断言锚定在生产数据上能把 `DIM_SUPPLIER` 拉进前 5**。

```bash
docker exec -i qa-backend python - <<'PY'
import json, os, urllib.request
base = os.environ.get("MILVUS_URI", "http://localhost:19530")
db   = os.environ.get("MILVUS_DB_NAME", "")
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def post(path, payload):
    req = urllib.request.Request(base+path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type":"application/json"}, method="POST")
    return json.loads(op.open(req, timeout=60).read())
body = {"collectionName":"ontology_class_embeddings","filter":"id >= 0",
        "outputFields":["ontology_id","name","alias"],"limit":5000}
if db: body["dbName"] = db
rows = post("/v2/vectordb/entities/query", body).get("data", [])

TOMBSTONED = {8, 9, 46, 270}          # 已在生产 PG 确认 valid_to 非空
classes = {}
for r in rows:
    oid = r["ontology_id"]
    if oid in TOMBSTONED: continue
    nm, al = r.get("name") or "", r.get("alias") or ""
    cur = classes.get(oid)
    if cur is None or (not cur[1] and al):
        classes[oid] = (nm, al)

# ↓↓↓ 与 recall_keyword_anchor.py 逐行一致（fix/recall-datasource-anchor @ 462cf9fa）
MIN_N, MAX_N = 2, 6
def fragments(q):
    compact = "".join(q.split()); out = []; seen = set()
    for size in range(MAX_N, MIN_N-1, -1):
        for st in range(0, max(0, len(compact)-size+1)):
            f = compact[st:st+size]
            if f not in seen:
                seen.add(f); out.append(f)
    return out
def select(q, items, maxAnchored):
    frs = fragments(q); scored = []
    for cid, (nm, al) in items:
        best = None
        for target in (al, nm):
            if not target: continue
            norm = "".join(target.split()).upper()
            for fr in frs:
                fu = fr.upper()
                cand = (1, len(fu)) if fu == norm else ((0, len(fu)) if fu in norm else None)
                if cand is None: continue
                if best is None or cand > best: best = cand
        if best is not None:
            scored.append((best[0], best[1], cid, (nm, al)))
    scored.sort(key=lambda it: (-it[0], -it[1], it[2]))
    return scored[:maxAnchored]
# ↑↑↑

for q in ["公司目前有多少供应商", "公司目前有多少生产类供应商", "公司有多少供应商"]:
    sel = select(q, classes.items(), 5)
    print("=" * 74); print("问句:", q)
    for i, (ex, ln, cid, (nm, al)) in enumerate(sel, 1):
        mark = "   <<<< DIM_SUPPLIER" if cid == 12 else ""
        print(f"  {i}. id={cid:<5} exact={ex} fraglen={ln}  {nm}  alias={al}{mark}")
    pos = [i for i, (ex, ln, cid, _) in enumerate(sel, 1) if cid == 12]
    print("  => DIM_SUPPLIER(id=12) 锚定排名:", pos[0] if pos else "未进入前 5  <<< 批次修不好本 case")
PY
```

判据：`DIM_SUPPLIER(id=12)` 必须落在**前 5**（理想是第 1）。

### ✅ 探针已跑（2026-11-19），结果：**通过**

三问句（`公司目前有多少供应商` / `公司目前有多少生产类供应商` / `公司有多少供应商`）输出完全一致：

```
1. id=12  exact=0 fraglen=3  DIM_SUPPLIER  alias=供应商主数据   <<<< DIM_SUPPLIER
2. id=10  exact=0 fraglen=2  DIM_FACILITY  alias=公司及工厂的信息
=> DIM_SUPPLIER(id=12) 锚定排名: 1
```

⇒ **缺口 1 证伪**（详见 §四 缺口 1）：锚定竞争集只有 DIM_SUPPLIER 一个类，
`id=2 Supplier` 不参与锚定，`exact=1` 档不存在。
**⇒ 本批次修得好本 case 的向量漏选；剩余唯一风险是缺口 2（few-shot 污染）。**
探针脚本可复用：改 `TOMBSTONED` 集合与问句即可复测其它 case。
若被 `Supplier`(id=2) 或别的 `exact=1` 类压下去 ⇒ 锚定排序键需改（如 `(-exact, -fraglen, 层序, id)`，
或锚定**不限 5**、或对 `exact` 档单独放宽上限）。

---

## 六、附：全部只读，未改任何生产数据

日志 `docker logs qa-backend` / `psql SELECT` / Milvus `entities.query` + `entities.search`，
全部只读；探针脚本可在 `/tmp/probe_*.py` 复现。
