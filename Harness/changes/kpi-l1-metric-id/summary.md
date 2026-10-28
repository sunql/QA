# 变更：kpi-l1-metric-id（B1+B2+B5，KPI 治理 → 技术链路提案 Phase 1+对账）

- **日期**：2026-10-28
- **作者**：Claude
- **Phase**：Phase 1（B1+B5，提案 `../2026-10-27-kpi-governance-into-nl2sql-proposal.md`）+ B2 对账（同日追加）
- **状态**：draft（待部署验证后转 in-review）
- **关联变更**：[KPI 治理注入提案](../2026-10-27-kpi-governance-into-nl2sql-proposal.md)
- **迁移版本**：无（B1/B2/B5 均不加列不迁移；后续如需 FeatureDefinition.metric_id 另起 0136+）
- **分支**：`feat/kpi-l1-metric-id`

---

## 1. 需求

L1 KPI 直连的 FeatureDefinition 解析只有 `kpi.formula == FD.feature_definition` 字符串
等值一条路：锚点脆弱、失败静默。2026-10-27 探针实证：dev 12 条存量 KPI 全走此路径且
**无一条命中**（全部静默降级为无数据回答）；`ontology_metric` 总数为 0（/metric 从未
成功过）。B1 = 解析契约改「外键链路优先 + 等值回退可见化」；B5 = 消灭「有技术形态无
治理户头」的孤儿指标；B2 = 编译期「治理五件套 ↔ 技术公式」一致性对账（B3 注入的
可信前提——注入未经对账的治理声明等于把漂移喂给 LLM）。

## 2. 设计评审

候选与取舍见提案 §B1/§B5。本批落地时的两个实证修正：

1. **探针①=12/②=0/④=0** ⇒ B1 的 metric 路径当前是零用户的前瞻契约，真正见效的是
   回退路径的 warning 可见化；锚点列选 `feature_definition`（与回退同列，漂移面两份
   归一），`feature_name` 列锚点变更留给 B2 对账批次决策。
2. **实现中发现 /metric 在 main 上本来就是坏的**：`OntologyService.createMetric`
   要求 keyword-only `actor`，`chat_domain` 调用漏传 → `TypeError` → 500。这解释了
   dev `ontology_metric` 为 0。本批顺修（actor=调用方，匿名链路 "chat"，与
   `_handleAgentRun` 同约定）。
3. **B2 偏离提案一处**：提案写「结构解析不了 → LLM 判等」，但 knowledge compiler
   模块 docstring 约束「不新增任何 LLM 调用」（编译跑在 worker/请求路径，加 LLM
   引入延迟与计量面）。v1 采用结构解析 + `unknown` 诚实标注（同 cost_source 口径
   ——不谎称一致），LLM 判等延后单议。另：numerator/denominator 多为中文业务文本，
   抽不出列名是常态 ⇒ `unknown` 会是存量主流判定，这本身是对账要暴露的真相
   （治理字段从未按列名口径填写）。
4. **B2 落点实证**：`_compileSqlMetadata`（knowledge_compiler_service.py:481）在
   metric 编译时刷新挂靠 KPI 缓存，对账挂其后；对账自 commit（与三库对账
   `reconcile()` 同款——audit 巡检结果不随调用方事务回滚丢失）。

## 3. 数据模型变更

无。无迁移。

## 4. 接口契约变更

- `POST /api/v1/chat`（question=`/metric 名称 = 公式` 或自然语言"定义指标"）：
  - 行为变更：成功后自动创建 DRAFT `KpiCatalog`（幂等）；
  - 答案文本追加尾注 `（已创建草稿 KPI 户头 KPI_AUTO_M{id}，补全治理字段并发布后生效）`；
  - 修复：此前该路径恒 500（createMetric TypeError）。
- `POST/PUT /api/v1/kpi-catalog`：`metric_id` 挂靠 + grain 与 metric
  `dimension_defaults` 启发式覆盖检查（不覆盖 → 服务端 warning 日志，**不阻断**）。
  表单级提示需响应契约变更，延后至 B3 批次。
- B2（无 API 变更）：metric 编译（`compileObject`）副产物——有 findings 时落一行
  audit_log（`entity_type=kpi_metric_reconcile`，actor=`system:knowledge-compiler`，
  after JSONB 含 `kpi_findings: [{kpi_code, findings:[{check,verdict,detail}]}]`）；
  verdict ∈ `mismatch` / `unknown`；check ∈ `formula_semantics` / `grain` /
  `formula_mixing`。无 findings 不落条目（验收口径：一致 fixture 零条目）。

## 5. 实现要点

- `chat_service.py`：`_resolveCalculationFeature` 两级解析（metric_id → metric.formula
  锚 → 降级 kpi.formula 锚），回退路径进入即 warning；抽出 `_findEnabledFeature`。
- `kpi_catalog_service.py`：新增 `ensureDraftForMetric`（幂等 by metric_id；kpi_code
  确定性 `KPI_AUTO_M{id}`，撞码加时间戳后缀；**绕开 createKpi 的 datasource 强校验**
  ——自动户头创建时无源可绑——但保留 outbox `kpi_created` → audit+history 同管线）；
  `_warnGrainMismatch` + `_DIM_ALIASES` 中文别名表（英文维度键 vs 中文 grain 裸子串
  匹配恒告警，实测必加）。
- `chat_domain.py`：`user` 透传链（`_handleDomainCommand`/`_handleDefineMetric`/
  `_streamDomainCommand`）；/metric 后调 `ensureDraftForMetric`（best-effort：
  失败只降级尾注，不回滚指标本体）。
- **B2 新模块 `kpi_contract_reconcile.py`（172 行）**：三个纯检查函数
  （`_checkFormulaSemantics`/`_checkGrainCoverage`/`_checkFormulaMixing`，无 IO，
  返回 finding dict 或 None）+ `reconcileKpiContract(session, audit, metric, kpiRows)`
  聚合落 audit。`_extractSqlIdentifiers`：正则抽列名 + SQL 关键字/函数名剔除表。
  独立成模块的原因：compiler 加完 810 行超 800 上限，拆后 677 + 172。
- `knowledge_compiler_service.py`：`_compileSqlMetadata` 缓存刷新后 hook
  `reconcileKpiContract`，try/except 包裹——对账失败只 warning，不阻断编译角色。
- `kpi_catalog_service.py`：B5 的 grain 判定抽公共函数 `grainCoverageGaps`
  （B2 `_checkGrainCoverage` 复用同一别名表判定，DRY）。

## 6. 测试

新增（21 用例全绿）：
- `integration/test_chat_l1_calculation_resolution.py`（6）：外键命中不进回退 /
  metric 未命中降级 / metric.formula 空白降级 / 存量回退命中 warning / 双未命中
  None+warning / 空 formula 静默 None。悬空 metric_id 用例被 FK 约束否定
  （DB 层不可能悬空——恰是外键链路可靠性的佐证），改用空白 formula 覆盖降级分支。
- `integration/test_chat_metric_draft_kpi.py`（6）：HTTP 全链路 /metric → DRAFT 户头
  （created_by 归属、尾注回显）／户头失败不砸指标答案／service 幂等／撞码后缀／
  grain 不覆盖 warning 不阻断／覆盖不告警。
- `integration/test_kpi_metric_reconcile.py`（9，B2，真实 PG）：列不在公式集合
  mismatch+audit 落行 / 一致契约零条目 / 中文分子分母 verdict=unknown /
  grain 缺键（含别名表）/ formula 混用 / B5 形状 DRAFT 全空零 findings 零噪声 /
  对账抛错不阻断编译 / 只读性 / class 编译 skipped 不触发。
  RED→GREEN 全程：先写 9 用例跑红（5 failed），实现后全绿。

回归：邻域单测 `test_chat_service.py`+`test_chat_service_stream.py`（基线比对，
净新增失败 0——既有 6 红为 BuildAnswerPrompt/stream 超时族，与本批无关）；
KPI 集成 48 绿；ontology 集成 52 绿；B2 后 compiler 三套（B2 新增 9 + 三库
reconcile + compiler unit 22）全绿；KPI/L1/metric 全量集成 -k 选择 195 绿。
单测 fake 修正：`_FakeOntologyService.createMetric` 补 `actor`（原 fake 断言的是
main 上实际会 500 的坏调用路径——假替身保真度问题）；两文件 autouse stub
`ensureDraftForMetric` 抛错走降级路径（MagicMock session 恒真值会塞 mock 垃圾进答案）。

**发现既有假绿家族（未修，独立立项）**：`test_kpi_catalog_search_api.py::
test_search_requires_auth` 等 12 处/10 文件用 `monkeypatch.setenv("AUTH_STUB_ENABLED",
"0")` 关 stub——但 conftest `client` fixture 建立时 app 已用旧 env 构建，
`getSettings()` 是 `lru_cache` 单例 ⇒ toggle 永不生效 ⇒ 未认证请求走 anonymous
兜底拿 200。对照 `test_wiki_coverage_api.py::test_coverage_requires_auth` 同样挂。
基线 cc4b8986 复跑同节点同样失败 ⇒ 既有，净新增 0。生产无洞（真 auth dep +
nginx 剥 X-User-* 头 + prod AUTH_STUB_ENABLED=0），是「保护性断言从未工作过」
的假绿。修法需新 fixture（env 先于 app 构建），涉及 10 文件，另立批次。

## 7. 安全审查

- createMetric 漏传 actor 属审计归属缺失（高优顺修）；修复后 /metric 审计链恢复。
- `ensureDraftForMetric` 绕过 ACL？——chat `/metric` 走 `getCurrentUser` 认证，
  created_by 落真实 userId；DRAFT 户头不进 L1 缓存、不改变任何读路径权限面。
  MEDIUM：DRAFT 户头的后续 PUT 走既有 ACL（owner/admin），无新增暴露面。
- B2 对账只读不写业务表；audit actor 用 `system:knowledge-compiler`（与三库对账
  同一 system actor 口径），findings 内容只含字段级 diff 摘要，无敏感数据。
- 无 SQL 拼接（全 ORM 参数化）；无密钥操作。
- 附带发现：12 处 requires_auth 假绿测试（见 §6）——生产无洞，但该家族修复前
  「auth 强制」类回归无法被测试网捕获，建议独立批次尽快补。

## 8. 部署验证

未部署（draft）。部署时冒烟：
1. `docker exec -it qa-backend python -c` 探活；
2. chat 发 `/metric 测试指标 = SUM(x)/COUNT(*)` → 答案含户头尾注；
3. dev 库验证 `SELECT * FROM kpi_catalog WHERE kpi_code LIKE 'KPI_AUTO_M%';`
4. 观察后端日志 `L1 回退路径` warning 出现频率（存量 12 条 KPI 的脆弱路径可见化）。
5. B2：对任一挂靠 KPI 的 metric 触发编译（本体管理页编译按钮）→
   `SELECT * FROM audit_log WHERE entity_type='kpi_metric_reconcile' ORDER BY id DESC;`
   应出现 findings 条目（dev 存量 numerator 多为中文 ⇒ 预期大量 unknown）。

## 9. 关联

- 提案：`../2026-10-27-kpi-governance-into-nl2sql-proposal.md`（分支
  `docs/kpi-governance-nl2sql-proposal`）
- Wiki：`Harness/wiki/nl2sql-engine.md`（L1 段）、`Harness/wiki/数据模型.md`（两表双视角）
- Rules：`Harness/rules/测试规范.md`（真实 PG 集成）
- Memory：待合并后写 `qa-system-kpi-b1-b5-l1-metric-id`
- 前置探针（2026-10-27 dev）：①metric_id 空但有 formula=12；②挂靠=0；③漂移=0（无样本）；
  ④formula 等值命中 FD=0

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全
- [x] 9 段非空（第 8 段为 draft 待部署状态说明）
- [x] 第 2 段含候选/实证修正
- [x] 无迁移
- [x] 第 7 段安全结论（MEDIUM 一项）
- [ ] 第 8 段部署冒烟输出（部署后补）
- [x] 第 9 段 ≥3 跨文件链接
- [ ] MEMORY 索引（合并后添加）
