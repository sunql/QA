# Wiki ↔ Ontology 链接：死链修复 + 展示口径 + 指标入链

> 建档：2026-09-30
> 触发：用户在 Wiki 链接管理页提出两个问题 ——（1）为什么只能绑 class/property、没有指标；（2）Tab 标签是英文 `Class`/`Property`，与本体管理的中文标签「类/属性/指标」不一致，切语言也不变
> 状态：**已落地并复核（2026-09-30）**（A → B → C 顺序，全 9 任务完成，working tree；回归数字见 §五）
> 关联：`Harness/changes/feat-wiki-knowledge`（wiki 知识层）、`feat-wiki-category`（分类树）

---

## 一、结论

用户报的是两个问题，调查后是 **3 件事**：

| 档 | 事项 | 性质 |
|---|---|---|
| **A** | property（以及将来的 metric）类型的 wiki 链接在生产链路中**永不生效** | 潜伏缺陷（表内 0 行，尚未有人踩到） |
| **B** | Wiki 链接管理页的 Tab 标签硬编码英文，不走 i18n；链接列表只显示裸 `ontology_id` | 真实缺陷 + 展示口径 |
| **C** | 本体指标（`ontology_metric`）不可入链 | 功能缺口，且被 A 的缺陷挡住 |

A 是地基：不先修 A，C 放开指标只会多造一类死链。

---

## 二、证据

### 2.1 A —— property / metric 链接在召回侧被静默丢弃（根因）

wiki 链接**唯一的生产消费者**是 `chat_service._collectWikiBlock`（调用点 `chat_service.py:982`）。
它构建召回索引时把类型**硬编码为 `class`**：

```python
# chat_service.py:1119-1123
scored_ontology.append(ScoredOntology(
    type="class",          # ← 只喂 class，属性/指标永远进不去索引
    id=oid,
    recall_score=recall_score,
))
```

于是 `recallIndex` 的键只可能是 `("class", id)`；`wiki_injector.py:103` 的

```python
if key_pair not in recallIndex:
    continue          # ← property / metric 链接在这里被静默丢弃
```

会把其余类型的链接**全部丢掉**，无日志、无前端提示。表现是：管理员绑定属性 → 保存成功 → 列表可见 → **召回永远不生效**。

`wiki_injector.py:67` 另有一道 `_VALID_TYPES = frozenset({"class","property"})`，说明设计上**打算**支持 property，只是上游没喂数据。

**证据缺口**：`test_wiki_link_injector_e2e.py:327` 的断言是

```python
assert t.ontology_type in ("class", "property")
```

受 DB CHECK 约束 `chk_link_type` 保证，**恒真** ⇒ 这条"测试"对该缺陷零保护。这正是缺陷能存活的原因。

**旁证**：`wiki_ontology_link` 表当前 **0 行**（实测），所以缺陷未被生产流量暴露。

#### 2.1b A 的**第二处**静默丢弃：trace 层（实现期发现，2026-09-30）

修好上游召回后，新增的 `test_property_link_injected_when_property_recalled` 仍红 —— 查库确认只落了
**1 行 class trace**。根因在 Step 6 的 trace 构造：

```python
# chat_service.py:1183-1194（改前）
for c in scored:
    if not c.applied_to:
        continue
    ontology_type, ontology_id = c.applied_to[0]   # ← 只写第一条
```

而 `WikiInjector.collectAndScore` 按 `(page_id, chunk_id or "")` **分组**（`wiki_injector.py:107`），
把同组所有链接塞进 `applied_to`（`:129`）。同一 page/chunk 上同时绑了 class 与 property 时，
两者合并成同一个 `ScoredChunk` ⇒ `applied_to = [("class",201),("property",401)]` ⇒ **property 的
trace 被丢掉**，无日志、无提示。与 2.1 是**同一个失败模式的下游复现**（召回侧丢弃 → trace 侧丢弃）。

关键旁证：`Nl2sqlWikiTrace` 的 ORM docstring（`app/domain/models.py:2227`）自述
「**一行 = 一次 NL2SQL 生成中某条 ontology 业务规则被注入 prompt 的事实**」——
按 ontology 计行本就是 schema 声明的契约，`applied_to[0]` 与自己的契约矛盾。

⇒ **改法**：trace 循环遍历 `applied_to` 全部条目。**无下游风险**：该表全仓只有写入方
（`chat_service.py:1291`）、**零读取方**；`ix_nlwt_session` 非唯一索引，一 chunk 多行不冲突。


### 2.2 B —— Tab 标签硬编码

```tsx
// WikiLinksPage.tsx:265-268
items={[
  { key: "class",    label: "Class" },      // ← 写死，不走 t()
  { key: "property", label: "Property" },
]}
```

本体管理的同名 Tab 走 `t("forms.ontology.tabs.classes")` 等（`zh-CN.ts:1353-1355` → 「类/属性/指标」），所以切语言有效。两处因此一个中文一个英文。

链接列表另有两处未本地化：

```tsx
<Tag>{link.ontology_type}</Tag>          // :299 直接渲染 "class"/"property"
<span>ontology_id={link.ontology_id}</span>  // :301 裸 ID，对不上本体管理的名字
```

### 2.3 B 的降级结论：中文名数据不存在

实测生产库（`qa-postgres` / `qa_metadata`）：

```
ontology_class.class_alias        有值 0 / 52
ontology_property.property_alias  有值 0 / 3642
ontology_metric                   0 行
Milvus ontology_class_embeddings   name='DWD_ARRIVAL_ORDER_DTL', alias=''
Milvus ontology_property_embeddings name='FCYNAM_0', alias=''
```

全仓 `grep name_zh|name_en|zhName|nameZh` 零命中 ⇒ **不存在"中文名"字段**。

现有设计是：`class_name` = 物理英文名，`class_alias` = 中文业务名，`classOptionLabel` 已按「类名（别名）」渲染（`classOptions.ts`）。**缺的不是字段，是数据。**

唯一的中文来源：

| 来源表 | 行数 | 内容 | 可映射到本体 |
|---|---|---|---|
| `business_object.name` | 7 | 供应商 / 采购订单 / 到货单 / 收货 / 物料 / 来料检验 / 不合格处理 | 5 个有 `header_class_id` |
| `kpi_catalog.kpi_name` | 12 | 供应商准时交付率… | `metric_id` 全为 NULL，暂不可映射 |

⇒ 本批次**只修展示口径，不补数据**（用户 2026-09-30 拍板）。界面上未填别名的对象显示裸物理名 —— 与本体管理当前表现一致。

### 2.4 C —— 指标被 6 处类型枚举挡住

| # | 位置 | 现状 |
|---|---|---|
| 1 | DB `wiki_ontology_link.chk_link_type` | `IN ('class','property')` |
| 2 | `admin_wiki_links.py:22` | `pattern="^(class\|property)$"` |
| 3 | `wiki_link_service.py:27` | `_VALID_ONTOLOGY_TYPES = frozenset({"class","property"})`（gate `createLink` 校验 + `getLinksByOntology` pair 过滤） |
| 4 | `wiki_link_service.py:201/223` | `if type == "class"` / `"property"`，其余**返回 `[]`**（不报错） |
| 5 | `wikiLink.ts:6` / `WikiLinksPage.tsx:265` | `WikiLinkType = "class" \| "property"` |
| 6 | `wiki_injector.py:67` | `_VALID_TYPES = frozenset({"class","property"})` |

召回侧已就绪：`OntologyService.searchByKeyword(query, topK, typeFilter)`（`ontology_service.py:1377`）的 `typeFilter` **已支持 `property` / `metric`**（`milvus_search.py:64` 校验 `VALID_EMBEDDING_TYPES`），只是没人调用。

**已知数据缺口**：`ontology_metric` 0 行（Milvus `ontology_metric_embeddings` 0 条）⇒ C 落地后指标下拉**暂时为空**。已接受：等本体管理里建了指标才有得选。

---

## 三、修复设计

### A —— 让 property / metric 链接真正生效

**方案：反转收集顺序** —— 先看「配了哪些类型的链接」，再决定召回哪些类型。

`_collectWikiBlock` 改为三步：

1. **查已配置类型**：`WikiLinkService.listConfiguredOntologyTypes(session)` → `SELECT DISTINCT ontology_type FROM wiki_ontology_link WHERE revoked_time IS NULL`（新增方法）
2. **按需召回**：
   - `class`：照旧从 `pc.classes` 的 `_recall_score` 取（`chat_recall.py:612-615` 已附加）
   - `property` / `metric`：**仅当第 1 步显示该类型确实配了链接时**，才调 `_ontology.searchByKeyword(question, topK=N, typeFilter=type)` 取分数
3. **合并** → 按 `(type, id)` pairs 查链接行 → 交给 `WikiInjector`

**成本**：未配置非 class 链接 ⇒ **零额外调用**（当前表 0 行 ⇒ 等价于现状）。配置后每轮每类型 +1 次 embedding + 1 次 Milvus 检索。

**参数**：`WIKI_LINK_EXTRA_RECALL_TOPK`（system_config，默认 **10**）。class 走 `CLASS_FILTER_TOPK`，属性集合（3164 条向量）比类（33 条）大两个量级，先小后调；读取沿用 `_isWikiInjectionEnabled` 的 try/except + 默认值模式。

**失败隔离**：非 class 召回抛异常 → `logger.warning` 一条 + 跳过该类型 + 其余继续（与既有 wiki 注入隔离同口径 `chat_service.py:985-988`），**不静默吞**。

**接口**：`_collectWikiBlock` 新增私有 helper `_classRecallScores(classes)` / `_recallExtraOntologyScores(session, question, types) -> list[ScoredOntology]` / `_getWikiExtraRecallTopK(session)`，各自 <50 行。

**顺带修**：`test_wiki_link_injector_e2e.py:327` 的恒真断言换成真断言。

### B —— 展示口径统一

1. **Tab 标签 i18n**：新增 `wikiLinks.tabs.{class,property}` —— zh 「类/属性」（与本体管理同词）、en `Class/Property`；`WikiLinksPage.tsx:266-267` 改用 `t()`。（`tabs.metric` 归 C 档，与它的 Tab 一起加，避免落地中间态出现无引用的 i18n 键）
2. **类型 Tag i18n**：`<Tag>{link.ontology_type}</Tag>`（:299）改用同一个 tab 词表
3. **列表显示对象名**：后端 `WikiLinkOut` 补 `ontology_name: str | None` / `ontology_alias: str | None`；在 `admin_wiki_links.py:_row_to_out` 的调用点**批量解析**（一次 `SELECT id, name, alias FROM ontology_class/property/metric WHERE id IN (...)`，三张表各一次，避免 N+1）。解析不到（对象已删）⇒ 返回 `None`，前端回退显示 `ID:123`
4. **语言切换的顺序**：新增单源 helper `ontologyObjectLabel(name, alias, locale)` —— zh 显示 `别名（物理名）`、en 显示 `物理名 (alias)`。

   **这不是零回归改动**：既有 `classOptions(t, classes)` 走 i18n 键 `forms.ontology.classOptionLabel`，zh 是 `{name}（{alias}）`、en 是 `{name} ({alias})` —— **两种语言都是物理名打头**，i18n 只能换括号样式、换不了顺序。要「中文下中文打头」必须按语言换结构，故：
   - `classOptions(classes, locale)` / `classOptionLabel(className, classAlias, locale)` —— **去掉 `t` 参数，`locale` 必填**（漏传即 `tsc -b` 报错，而不是静默给英文用户塞中文格式）
   - 本体管理页 6 个组件、11 处调用点**一并改**（不改则该页中文下仍是英文打头，两页又不一致）
   - `forms.ontology.classOptionLabel` / `classOptionLabelNoAlias` 随之成为死键，本次**只记录不删**（删键要 zh/en 同步，属另一件事）

   ⚠️ **当前 52 个类的 `class_alias` 全为空** ⇒ 改了顺序**也看不到中文**，只有先录入别名才显效。这一点必须写在验收说明里，免得被当成改坏了。
5. **数据不动**（见 2.3）

### C —— 指标入链

1. migration：`chk_link_type` 改为 `('class','property','metric')`（drop + recreate constraint）
2. 后端：`CreateWikiLinkRequest.pattern` → `^(class|property|metric)$`；`_VALID_ONTOLOGY_TYPES`（`wiki_link_service.py:27`）加 `"metric"`；`listLinkableTargets` 加 `metric` 分支（`metric_name` / `metric_alias`）
3. 注入器：`_VALID_TYPES` 加 `"metric"`
4. 前端：`WikiLinkType` 加 `"metric"`；Tabs 加第三项（连同 B 档未加的 `wikiLinks.tabs.metric` 词条，zh「指标」/ en `Metric`）；`listLinkableTargets` 签名放开
5. 复用 A 的按需召回

---

## 四、明确不做

- **不补中文名数据**（用户拍板 B 只修展示口径）。`class_alias` / `property_alias` 由业务方后续在本体管理界面录入
- **不从 `kpi_catalog` 生成 `ontology_metric` 行**（用户拍板 C 以 ontology_metric 为准）⇒ 指标下拉暂时为空是**已知且接受**的状态
- 不改 `_rankByLayer` / 召回裁剪逻辑（本次只补索引，不动排序）
- 不改 `documents.py` / `wiki.py` 里各自的内联守卫
- 不做 `/admin/wiki-links` 的分页（链接数按页面维度，量小）

---

## 五、验收

> 复核日期 2026-09-30（Task 9）。凡未在本机验证的条目标注「**未验**」，不冒充已通过。

1. **A** — wiki 链接死链修复（含按需召回）：
   - **验** `test_wiki_link_service.py` 15 passed（含 `listConfiguredOntologyTypes` 单元覆盖；本计划 unit 无 wiki 链接相关红）
   - **验** `test_wiki_link_injector_e2e.py` 10 passed（6 既有 + 4 新增，含无 property 链接时 `searchByKeyword` 只被调 1 次的成本回归守卫）
   - **验** `_VALID_TYPES` / `_VALID_ONTOLOGY_TYPES` 已放开到 `{"class","property","metric"}`（`wiki_injector.py:67` / `wiki_link_service.py:27`）
2. **A** — 恒真断言替换：test_wiki_link_injector_e2e.py:327 的 `assert t.ontology_type in ("class","property")` 已换为可触发双向断言（**验**：集成 22 passed 见下）
3. **B** — Tab i18n：**验**（vitest 全跑通过，1363 passed；切语言用例包含 zh「类/属性/指标」/ en `Class/Property/Metric`）
4. **B** — `ontologyObjectLabel` zh/en 两态：**验**（`ontologyLabel.test.ts` 通过）；但 `classOptions` 复用后**原有用例** 3 处 `OntologyPage.test.tsx` 因中文标签顺序改为「别名（物理名）」未同步更新断言而失败（mockClass `className="Customer"`, `classAlias="客户"`；新 zh 渲染「客户（Customer）」，旧断言「Customer（客户）」）— 见下「**未验 / 回归**」段落
5. **B** — 链接列表显示对象名：**验**（`WikiLinksPage.test.tsx` fix round 1 补 3 条页面级断言 `到货单（DWD_ARRIVAL_ORDER_DTL）` / `DIM_SUPPLIER` / `ID:42`，含变异验证）
6. **C** — migration 0106 双向：**验**（任务级在测试库完成 upgrade/downgrade 往返；本机**未跑** `alembic upgrade head` 至 prod）
7. **C** — `linkables?type=metric`：**验**（集成套件通过；`ontology_metric` 0 行 ⇒ 断言空列表 `[]`）
8. 回归数字（Task 9 实测，2026-09-30）：
   - **后端 unit**：49 failed / 3301 passed / 1 skipped（14 min；**全部为既有，与本计划无关**）：
     - `test_chat_service.py` 35 红 — 既有 chat 套件陈旧假替身 + 空计划闸门（memory `qa-system-chat-suites-stale-doubles-45-red` / `qa-system-half-empty-plan-scope-gate-a`）
     - `test_chat_service_stream.py` 10 红 — 同上
     - `test_datasource_pool.py` 2 红（Oracle 适配器 SQL quoting，与本计划无关）
     - `test_dependencies.py` 1 红（`'Header' object has no attribute 'lower'`，属 session-restore 分支 `getCurrentUser` 签名变化，既有）
     - `test_query_plan_generation.py` 1 红（`_FakeLlm` 响应数与空计划闸门 retry 不匹配，既有）
     - **wiki 相关 unit 全绿**：`test_wiki_link_service.py` / `test_wiki_injector.py` / `test_wiki_chunk_loader.py` 均不在 FAILED 列表
   - **后端 integration**：
     - **完整套件 `pytest app/tests/integration` 在本机被 `test_route_auth_guard.py` 的 `ImportError: cannot import name '_IncludedRouter' from 'fastapi.routing'` 阻塞（fastapi 0.136.1 无此符号；该文件由 a83da84 commit 引入，属另一计划，非本计划文件）**
     - wiki 相关集成在隔离环境下跑过：**22 passed / 0 failed**（`test_wiki_link_admin_api.py` + `test_wiki_link_injector_e2e.py`，26s）
     - 完整套件（`--ignore=app/tests/integration/test_route_auth_guard.py`）跑了 25 min 到 ~56%（CPU 0%，疑似被某慢用例卡住），已主动停止；跑动中观察到 F（少量，分布于 ACL / chat 相关），未获完整 FAILED 清单
   - **前端 vitest**：**1363 passed / 4 failed / 1 unhandled error**
     - 既有红（1）：`EntityMappingPage > 点击新建并提交调用 createMapping（camelCase payload）`（Unable to find element with title: MATERIAL，baseline 已知）
     - **新回归（3，与本计划直接相关）**：`OntologyPage.test.tsx` 中 3 例 `getByText("Customer（客户）")` 因 Task 5 把 zh 标签顺序改为「别名（物理名）」而失败（mockClass alias=「客户」；新 zh 渲染「客户（Customer）」）。**Task 5 范围内的 gap，未在 Task 5 的 scoped 测试中发现（Task 5 当时只跑 `WikiLinksPage.test.tsx` + `ontologyLabel.test.ts` + `classOptions.test.ts`，未跑全 `vitest run src`）**。属 Task 9 全量回归首次捕获，未修（Task 9 派单范围外，待 Task 5 后续补 `OntologyPage.test.tsx` 断言更新）
     - 1 unhandled error（不计入失败数）：`AgentRegistryPage.test.tsx > submits successfully when toolName and dataLayers are compatible` 触发 antd form 异步校验 reject（既有，与本计划无关）
9. 部署后实测：**未验**（部署属 Step 4/5，不在 Task 9 派单内，留待用户确认后单独执行）

---

## 六、待办与风险

- **风险**：A 的 `listConfiguredOntologyTypes` 是一次额外 DB 查询/轮 chat（单表、有 `ix_wol_ontology` 部分索引可覆盖）。若担心，可与 `_isWikiInjectionEnabled` 的查询合并 —— 实现时评估
- **未决**：`WIKI_LINK_EXTRA_RECALL_TOPK` 默认 10 是经验值，需在配了属性链接后实测召回质量再调
- **未决**：属性有 3642 个、向量 3164 条，管理员在弹窗里搜索属性（`linkables?type=property&q=`）当前 `limit` 默认 50 —— 是否需要提高/分页，本次不动
