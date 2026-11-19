# 召回数据源过滤 + 关键词锚定（跨源幽灵表 / 问法敏感漏选 根治设计）

- **日期**：2026-11-19
- **状态**：设计已获用户批准（三问裁决：fail-open+披露 / 无源不过滤+披露 / recallInfo+前端）
- **触发事故**：2026-11-19 生产（main `c959b77f` 部署后）
  - **缺陷②（跨源幽灵表）**：「公司目前有多少供应商」→ planner 被校验器引导到 `main_data_supplier`
    （id 270，物理表在另一数据源）→ `ORA-00942`，值域采样同表空转 6 次。
    止血：id 270 已置 `valid_to` 墓碑（2026-11-19 生产已执行，生效——校验器相似类名列表已不含它）。
  - **缺陷①（召回漏选）**：墓碑后同一问句变为「礼貌拒绝」：planner 选了真实类 DIM_SUPPLIER
    （id 12，别名「供应商主数据」，`valid_to` NULL，探针已核实），但 validator 报「不在本体 schema 中」
    ——即 DIM_SUPPLIER 没进召回窗口。「公司有多少供应商」（不带「目前」）可正常召回并回答
    ⇒ 问法改变 embedding 排序，字面锚定对措辞不敏感，用字面兜向量的底。

## 代码事实（已核实）

- `datasourceId` 已流到召回入口：`chat_recall.py:178`（P0-3），目前只用于加载 state，未参与类筛选。
- 召回链：候选集（钉版 `view.classes` 或 `ontology_service.listClasses`，均过滤 `valid_to IS NULL`）
  → `_selectRelevantClasses`（`chat_recall.py:485`）向量 `searchByKeyword` topK → `classByIdForWeight`
  交集 → ODS 过滤 → `_expandByJoinNeighbors` 扩边 → `MAX_CLASSES` 截断。
- `chat_recall.py:224` 与 `chat_service.py:996` 两处 `listClasses` 全局取类（无数据源过滤）。
- `schema_cache` 表（`models.py:1147`）：`(datasource_id, schema_name)` 唯一，Oracle 按 owner 拆多份，
  `schema_data` 为结构化表清单 JSON ⇒ **现成的类→源判定数据，无需新迁移**。
- `ontology_class` 无 `datasource_id` 列（列清单见 `Harness/wiki/data-model.md` ontology_class 条目）。

## 第 1 件：召回入口按数据源过滤候选类

### 组件

新模块 `backend/app/services/recall_datasource_filter.py`（chat_recall.py 已超 800 行红线，不再内嵌）：

- `loadDatasourceTableSet(session, datasourceId) -> set[str] | None`
  查 `schema_cache WHERE datasource_id = ?`，多 owner 行取并集，全部 lower() 归一；
  无行 / 空清单 / 读异常 ⇒ 返 `None` + warning，不阻断主链路（对齐 `_getClassFilterMaxClasses` 口径）。
- `filterClassesByDatasource(classes, tableSet) -> (kept, dropped)`
  匹配键 = `source_table` 最后一段（兼容 owner 前缀 `"THBI.xxx"`）lower() ∈ tableSet；
  `source_table` 为空的类保留（无法判定不误杀），仅计数。

### 三态行为（用户裁决）

| 场景 | 行为 | 披露 |
|---|---|---|
| 有缓存 + 有 datasourceId | 硬过滤，跨源类不进候选集 | `datasourceFiltered = N` |
| datasourceId=None | 不过滤（同现状） | `datasourceFilterSkipped = "no_datasource"` |
| schema_cache 缺失/空/读失败 | 不过滤（fail-open） | `datasourceFilterSkipped = "no_cache"` |

### 调用点（两处共用，先过滤后进召回链）

- `chat_recall.py` `classifyAndRecall`：`allClasses`（钉版快照与治理表两条路）取完即过滤；
  钉版快照同样过滤（SQL 终究在当前源执行）。
- `chat_service.py:996`：plan/schema 构建链的全量类。
- 过滤在候选集源头 ⇒ 向量交集 / JOIN 扩边 / ODS 过滤等下游零改动；JOIN 扩边只见本源邻居。

### 披露面

`ClassRecallInfo` 增可选字段 `datasourceFiltered: int = 0`、`datasourceFilterSkipped: str | None`
（默认值 ⇒ wire format 向后兼容）。前端 `MessageItem.tsx` 现有 classRecall 提示位追加：
「已按当前数据源过滤 N 张跨源数据表」/「未选数据源或数据源目录缺失，跨源过滤未生效」（i18n zh/en）。
⚠️ 披露面扩大 ⇒ 既有「集合相等」型断言需同步更新（M1-1.3 已知坑）。

## 第 2 件：关键词/别名锚定（治「问法敏感漏选」）

### 机制（在 `_selectRelevantClasses` 向量召回后追加，纯增量不淘汰）

1. **取锚**：问句切 2–6 字连续片段（n-gram，无分词器），与每个活类 `class_alias` / `class_name`
   做「片段 ⊆ 别名/类名」包含匹配（大小写归一）；
2. **排序**：精确等值 > 匹配片段更长 > 更短；同分按向量 score；
3. **强制纳入**：锚定类无条件进候选集、排在向量命中之前（不被 `MAX_CLASSES` 截断挤掉），与向量命中去重；
4. **上限**：`system_config.ANCHOR_MAX_CLASSES`（默认 5），防止宽泛词灌满窗口；剩余名额仍归向量命中。

### 披露面

`recallInfo.anchoredCount: int = 0`；前端提示「已按关键词锚定 N 张数据表」（i18n zh/en）。
零命中不做任何事（锚定无 fail 概念）。

### 执行顺序

**数据源过滤（淘汰跨源）→ 锚定（在幸存类里取锚）→ 向量命中交集** ⇒ 锚定永不捞回幽灵类。

## 明确不做（YAGNI）

- ❌ `ontology_class` 不加 `datasource_id`（回填难题与运行时校验同构；等「同名表多源消歧」需求再立项）；
- ❌ 不动 Milvus/向量检索侧；
- ❌ 不引分词器/LLM 抽词；本批不动 term_dictionary（术语挂类锚定留作后续增强）；
- ❌ 不以调 `CLASS_FILTER_TOP_K` 作止血（用户已手动抬 20，治标）。

## 测试（TDD，真实 PG，集成用例并入既有文件）

红测试先行：
1. 跨源类（source_table 不在当前源 schema_cache）不得进候选集——两个调用点各一条；
2. 三态：no_cache fail-open 披露 / datasourceId=None 跳过 / 正常过滤计数；
3. owner 前缀与大小写归一（`THBI.Main_Data_Supplier` vs `main_data_supplier`）；
4. 空 `source_table` 保留不误杀；
5. 锚定：别名包含问句片段强制纳入且排向量命中之前；超 `ANCHOR_MAX_CLASSES` 截断；
   与向量命中去重；零命中零影响；
6. recallInfo 新字段 camelCase 序列化；前端提示渲染（既有前端测试模式）。

真实验证闭环：本地发版后，生产重问「公司目前有多少供应商」应命中锚定 → DIM_SUPPLIER 进 schema →
正常出数；日志零 `main_data_supplier` 相关 ORA-00942。

## 落地与运维裁决

- **分支**：修复走 **main**（生产缺陷，非采购线）；PR 合 main；采购分支阶段发版时 `git merge main` 带回。
- **部署**：生效需一次后端镜像部署（runbook 另出）。
- **墓碑 270 复活**：数据源过滤上线验证后，可原地 `update ontology_class set valid_to = null where id = 270`
  恢复该表在**正确数据源**会话下的可查性（复活必须原地，勿换行——换行=换 class_id=Milvus 向量悬空）。
