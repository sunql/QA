# 变更：feat-data-domain-layer（数据域实体化 + 数据层按数据源探测）

- **日期**：2026-10-25
- **分支**：`feat/data-domain-layer`（自 main `cc4b8986`）
- **状态**：已提交（ca02b9b5 后端 / 47a31fd9 前端 / 76201834 docs）+ dev 发版完成
  （2026-10-25：镜像重建、容器重启、alembic no-op @0134、菜单 seed 54 行、
  bundle hash index-BQKIEAKl 且无烤入 localhost 基址）；待用户 UI 验证 → 方案 3 讨论
- **来源**：2026-10-25 用户三问设计讨论（数据域/数据层/工具维护），方案 1、2 落地，方案 3 记录待议

## 0. 背景与需求

1. **数据域**：从硬编码词表（`agent_vocabulary.AGENT_DATA_DOMAINS`）升级为可维护的
   企业数据域基础数据——含主管理组织（来自组织管理）；业务对象挂域（一主多附，
   有且仅有一个主域）；Agent 创建时从维护好的域中选择。
2. **数据层**：从硬编码词表（`AGENT_DATA_LAYERS`）升级为按数据源探测——先选数据源，
   按表名前缀规则判断该源有哪些层（ODS/DWD/DWS/ADS/DIM…）。
3. **方案 3（记录，待单独讨论）**：工具维护入口已存在（AdminToolsPage +
   `agent_tool_config` DB 行 + 代码侧 handler 白名单双结构，不是自动发现）；
   待方案 1/2 完成后单独讨论是否补充文档/runbook。

## 1. 链路设计（用户确认版）

```
数据域 data_domain（新建，含主管理组织 FK organizations）
   ↑ primary_domain_id（NOT NULL FK 列，天然唯一主域）+ business_object_domain（附域关联表）
业务对象 business_object（既有治理注册层）
   ↑ header_class_id（既有 FK，本次收紧 NOT NULL + 唯一索引）
本体类 ontology_class（域不落库，运行时经 header 反查继承）
```

- 本体核心表零改动；召回 / NL2SQL 路径不动。
- **规则 ①**：`business_object.header_class_id` 收紧 NOT NULL（空绑定 BO 无法参与
  图谱与问答，无存在意义）。
- **规则 ②（选项 A）**：`header_class_id` 加唯一索引——一个本体类只允许被一个 BO
  作 header，类的域继承唯一主域自动成立。
- 域的日常维护入口 = 业务对象页（主域/附域控件）+ 新增数据域管理页（基础数据 + 组织）。

## 2. 与既有「域」概念的关系（重要，防三套口径混淆）

| 概念 | 载体 | 语义 | 本次处置 |
|---|---|---|---|
| Agent 授权域 | `AGENT_DATA_DOMAINS` 常量 → 迁移为 `data_domain` 表 | **授权词表**（闭合，决定 Agent 能访问什么） | SSOT 移入 DB，常量降级为 fallback |
| 覆盖度域轴 | `class_domain_mapping.domain`（自由文本） | **分类轴**（开放，知识扩到新域就冒新域；wiki_coverage_models.py 明确不复用授权词表） | **保持独立不动** |
| 数据层 | `AGENT_DATA_LAYERS` 常量 → 迁移为 `data_layer_rule` 表 | Agent 授权/展示 + 表名前缀识别规则 | SSOT 移入 DB，chat_recall 前缀识别收敛同源 |

## 3. 后端实现（方案 1）

### 3.1 迁移 0133（data_domain + BO 域绑定 + header 收紧）

1. `data_domain`：code PK(20) / name / primary_org_id FK organizations（可空，RESTRICT）/
   description / enabled / created_by + TimestampMixin
2. seed 三域：PROCUREMENT→org(code=procurement)、QUALITY→org(code=quality)、
   LOGISTICS→NULL（无对应组织）；org 缺失时子查询置 NULL（幂等，可重放）
3. **存量清理**：DELETE header_class_id IS NULL 的 BO（dev 实测 IQC/NCR 两行、
   三表引用 0；迁移内置引用守卫，有引用则 raise 阻断）
4. `business_object.primary_domain_id`：加列 → 回填 'PROCUREMENT'（7 行全为采购域）→
   NOT NULL → FK RESTRICT
5. `business_object_domain`：(bo_code, domain_id) 复合 PK，双 FK
6. `header_class_id`：SET NOT NULL + `uq_business_object_header_class` 唯一索引
   （dev 实测无重复绑定）

### 3.2 模型 / Schema / 服务

- `DataDomain`、`BusinessObjectDomain` 模型
- `DataDomainService`：CRUD + 删除引用守卫（business_object.primary_domain_id /
  business_object_domain / agent_definition.data_domains JSONB @>）→ 409
- `dataDomainRegistry`：businessObjectRegistry 同款（warmUp/reloadOne/isValid/getAll）；
  校验用 **enabled 集**，禁用域不可用于新写入，存量行不动
- Schema：DataDomainCreate/Read；BusinessObjectCreate/Update 增
  primaryDomainId + attachedDomainIds；header_class_id 改必填（Create）
- Agent 词表校验切换：`_vocabCheckDomains` 改查 dataDomainRegistry（未 warmUp 时
  fallback 常量——授权语义下不许开放未加载状态）；`/agents/options` domains 同源
- `normalizeDataDomainCode`：复用 BusinessObjectCodeNew 同款 format 规则
  （大写+字母数字+下划线，≤20）

## 4. 后端实现（方案 2）

### 4.1 迁移 0134（data_layer_rule）

- `data_layer_rule`：id PK / layer(20) / pattern(100，可空=手工声明层，不可前缀探测) /
  priority（小者先匹配）/ datasource_type（可空=全类型通用，留扩展）/ enabled
- **双 partial unique index** 保 NULL pattern 可重复层值：
  `uq_layer_rule_pattern ON (layer, pattern) WHERE pattern IS NOT NULL` +
  `uq_layer_rule_manual ON (layer) WHERE pattern IS NULL`
  （不用 PG15 `NULLS NOT DISTINCT`——prod PG 版本无保证）
- seed 7 行（优先级 ODS_DIM_=10 最先，其余递增）：
  `ODS_DIM_→ODS_DICT, ADS_→ADS, DWS_→DWS, DWD_→DWD, DIM_→DIM, ODS_→ODS_BUSINESS`
  （与 chat_recall._getClassLayer 现行为逐字对齐）+ `FEATURE`（pattern=NULL，
  手工声明层——存量 agent_definition/agent_access_policy 在用，必须保留合法）

### 4.2 层探测 API

- `GET /datasources/{id}/layers`：读 schema_cache（**cache-only**，miss 不实时探库
  ——Oracle 全量 introspect 可能超前端 30s 全局超时；响应带 fromCache=false，
  前端提示先执行探测）→ 表名按规则 priority 匹配前缀 → `[{layer, tableCount}]`
  + `unknownTables`（未归类计数**必须透出**，ERP 原生命名断层不吞）
- `layerRuleRegistry`：warmUp 加载 enabled 规则；`distinctLayers()` 供 Agent 层词表
- `_vocabCheckLayers` / `/agents/options` layers：registry（fallback 常量）
- `chat_recall._getClassLayer` 收敛：默认规则表抽到共享常量
  `data_layer_defaults.py`（seed 与 recall 同源）；`_rankByLayer` 经 registry
  同步快路径取规则，未 warmUp fallback 默认常量。`_fetchAllDimClasses` 的
  `LIKE 'DIM_%'` SQL 硬编码不动（行为敏感，另立 follow-up）

## 5. 前端实现

- 新增 `DataDomainPage`（数据域管理：CRUD + org tree 选主管理组织）+ 路由 + 菜单
  seed（`seed_menu_config` upsert，注意 item.profile 必填历史坑）
- `BusinessObjectPage`：header_class_id 改必填；primaryDomainId 单选 +
  attachedDomainIds 多选（主域互斥校验）
- `DatasourcePage`：数据层分布面板（GET /datasources/{id}/layers，未探测时引导）
- `AgentRegistryPage`：无改动（domains/layers 下拉已走 `/agents/options`，后端换源后自动 DB 驱动）
- i18n zh-CN 补 key

## 6. 测试与验证

- 后端：迁移测试（真实 PG，含存量清理守卫反向用例）+ service/API 集成测试；
  全量跑按净新增失败 0 取证法
- 前端：tsc + vitest 受影响文件
- 部署注意：prod alembic 0132 → 本次 2 个迁移；0133 的存量清理守卫在 prod
  数据不同时可能阻断，runbook 要写明先跑守卫 SQL 盘点

## 7. 方案 3 记录（待单独讨论）

- 现状结论：工具不是程序搜索发现的；执行体（handler/arg_extractor）在代码侧
  白名单 `_VALID_HANDLER_REFS`，暴露与元数据由 `agent_tool_config` DB 行管理，
  管理入口 AdminToolsPage；新工具 = 代码 handler（发版）+ 配置行（免发版）
- 待议：是否补 runbook 文档、是否需要「工具 discovered-from-registry」类增强

## 8. 进度记录（2026-10-25）

### 后端已落地
- 0133 / 0134 迁移在 dev 库验证通过（注意：alembic 整批单事务，0134 失败会回滚 0133）
- 模型 / Schema / registry×2 / service×2 / API×2 / chat_recall 收敛全部就位
- 测试库 seed 链调整：conftest（unit+integration）先 `seedDataDomains` +
  `seedLayerRules` + `seedHeaderOntologyClasses`（0133 后 BO seed 需 ontology_class
  解析 header；测试库 truncate 后 ontology 空 → `_seed_rows` 全过滤）再
  `seedBusinessObjects`；`seedBusinessObjects` 空 rows 早退（空 INSERT 撞 NOT NULL）
- 新增测试：test_layer_rule_matching（10）、test_data_domain_registry（4）、
  test_data_domain_api（9）、test_datasource_layers_api（3）、
  test_business_object_api 重写（主域/附域契约）、test_agent_options_api 更新
  （layers 7 值 priority 序）、test_business_object_schemas/service 更新
- 顺手修的既有红（与本案无关，env 漂移）：test_agent_loop_security 3 例——
  venv 已装 langchain_core，`_initLoopState` 走 HumanMessage 路径
  （`.type == "human"`），测试只认 dict shim 的 `role == "user"`

### 实施坑（复现即查）
- `pg_insert(...).values([])` 空 values 会生成只含 server_default 列的 INSERT
  （撞 NOT NULL），必须早退
- pytest 用 AsyncMock 做 result 对象时 `.all()`/`.first()` 返回 coroutine——
  result 要用 MagicMock 包
- seed_business_objects 曾有结构性 bug：list literal 直接 `return [`，行尾
  过滤器不可达（IQC 无类时 NOT NULL 崩）——已修为 `rows = [` + 过滤

### 待办
- [x] 全量 unit run2 收尾确认——**净新增失败 0 已取证**（2026-10-25 18:05，
  27:23s，4274 passed / 24 failed / 1 skipped）：基线 31 红
  （/tmp/qa-unit-triangulation/baseline-failed.txt，与分支首跑逐字节一致）−
  3（agent_loop_security env 漂移已修）− 4（test_chat_service 按既有约定
  ignore）= 24，run2 红集合严格等于该子集。注意：早期摘要「refine ×12 /
  基线 27」是笔误，证据文件为准（refine_shortcuts 基线即 14 红）
- [x] 前端（DataDomainPage / BusinessObjectPage 域控件 / DatasourcePage 层面板 /
  types+api 模块 / i18n / 菜单 seed）——2026-10-25 完成
- [x] prod runbook：0133 存量清理守卫预检 SQL（§9）
- [ ] 分主题提交（等用户确认后执行）

### 前端落地明细（2026-10-25）
- `DataDomainPage.tsx` 新增：CRUD + enabled 行内 Switch + org TreeSelect；路由
  `/data-domains`（App.tsx）；菜单 seed `item.dataDomains` bizConfig sort_order=372
- `BusinessObjectPage.tsx`：primaryDomainId 必填单选 + attachedDomainIds 多选
  （前端镜像 `attached≠primary` 校验 + 主域变更联动清附域）；headerClassId 加
  `allowClear`（清空仅触发表单必填校验，不落库语义由后端守）
- `DatasourcePage.tsx`：行「更多」菜单加「数据层分布」→ Modal（cache-only；
  `fromCache=false` 时 Alert 引导先「缓存 Schema」；`unknownTables` 是**计数**
  非名单——后端 layers 端点只回 count，表名清单走既有 schema 缓存端点）
- i18n：zh-CN + en-US 双语（datasource.layers.* / businessObject 新 key /
  dataDomain.* / menu.item.dataDomains）；selectHeaderClass 文案去掉「（可选）」
- types：`BusinessObject.headerClassId/primaryDomainId/attachedDomainIds` 全必填
  （0133 收紧）；`DataSourceLayersRead.unknownTables: number`
- 测试：`BusinessObjectPage.test` 补 dataDomain mock + 主域/头类选择步骤
  （0133 后两字段必填，缺一步提交会被表单拦下→createBusinessObject 不被调用的假红）；
  `businessObjectApi.test` payload 补新字段
- 前端验证：`tsc --noEmit` 0 错；受影响 4 套件 42 例全绿；全量 vitest
  9 红（client×3 / ontologyApi×5 / kpiCatalogApi×1）——stash 全部改动后复跑
  同样 9 红，**净新增失败 0 已取证**（client 红是 i18n 实例初始化回落
  "Unknown error"、ontologyApi/kpiCatalog 是 spy 签名漂移 `get(url, undefined)`，
  均为基线既有 env 漂移，与本案无关）


## 9. 生产部署 runbook（0133/0134，供操作者执行）

> 生产由用户手工操作；以下 SQL 均为只读预检，不改动数据。

### 9.1 升级前预检（0133 存量清理守卫会 raise 阻断，先盘点）

0133 会 `DELETE FROM business_object WHERE header_class_id IS NULL`，
删除前有引用守卫（4 张表）。**任意一条 count > 0 都会让 migration 整批失败**
（alembic 单事务：0134 失败会连 0133 一起回滚）：

```sql
-- ① 空绑定业务对象清单（本次要被清理的行）
SELECT code, name FROM business_object WHERE header_class_id IS NULL;

-- ② 四张引用表的引用计数（与迁移内 _REF_GUARD_TABLES 一致）
SELECT 'entity_mapping' AS t, count(*) FROM entity_mapping WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL
SELECT 'feature_definition', count(*) FROM feature_definition WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL
SELECT 'document_entity_relation', count(*) FROM document_entity_relation WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL
SELECT 'feature_rule', count(*) FROM feature_rule WHERE data_object IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL);
```

- 全为 0 → 可直接升级。
- 有引用 → 二选一：补绑 `UPDATE business_object SET header_class_id = <对应本体类 id> WHERE code = '<code>';`
  （注意规则②唯一索引：一个本体类只能当一个 BO 的头类）；或先清理引用行。

### 9.2 升级

```bash
cd <新 worktree>/backend   # 部署源必须是新 worktree（主仓常停陈旧 detached HEAD）
docker compose exec -T backend alembic upgrade head   # 0132 → 0133 → 0134
docker compose exec -T backend psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"
```

升级后重启 backend（registry 在 lifespan warmUp 会自动加载 data_domain /
data_layer_rule，无需手工操作）。

### 9.3 升级后验证

```sql
SELECT code, name, enabled FROM data_domain ORDER BY code;          -- 期望 PROCUREMENT/QUALITY/LOGISTICS
SELECT code, primary_domain_id FROM business_object ORDER BY code;  -- 期望全部非 NULL
SELECT * FROM data_layer_rule ORDER BY priority;                    -- 期望 seed 前缀规则若干
```

- API 冒烟：`GET /api/v1/data-domains`（管理页语义，默认含禁用）；
  数据源页「更多 → 数据层分布」未探测的数据源应显示引导而非报错。
- 菜单：跑一次 `seed_menu_config`（upsert）让 `item.dataDomains` 出现在
  业务配置分组；确认 `item.profile` 行仍在（历史 500 坑）。

### 9.4 回滚

`alembic downgrade 0132` 会删 data_domain / data_layer_rule 并放宽
header_class_id/primary_domain_id 约束；业务对象挂域数据（junction 表）随表删除。
** downgrade 前先确认没人已在数据域页面录入业务配置**，否则业务配置丢失。
