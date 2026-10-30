# 变更：kpi-l1-metric-id（B1+B2+B3+B4+B5，KPI 治理 → 技术链路提案 Phase 1 全量）

- **日期**：2026-10-28
- **作者**：Claude
- **Phase**：Phase 1（B1+B5，提案 `../2026-10-27-kpi-governance-into-nl2sql-proposal.md`）+ B2 对账 + B3 注入 + B4 守卫（同日追加）
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
可信前提——注入未经对账的治理声明等于把漂移喂给 LLM）；B3 = 问句命中的治理
契约注入 plan 上下文（生成前约束聚合口径）；B4 = 生成后分母契约弱校验示警
（治理声明 vs 生成 SQL 的实际漂移面可见化）。

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
5. **B3 注入点选 B（问句期 pre-plan 匹配）而非 A（plan 后类查找）**：提案两案并列，
   选 B 因为治理口径要在 LLM 生成 plan **之前**进上下文才能约束聚合/分组写法；
   A 只能事后校验。入口 `generateValidatedPlan` 的 `common` dict 统一装配
   （重试 + 校验循环复用同一份契约文本，不重复匹配）。Top-3 cap 防上下文膨胀。
6. **B3 门禁复用 B2**：候选 KPI 有挂靠 metric 时跑同三个检查，任一 `mismatch`
   → 该 KPI 不注入（宁可少注入不注入错的）；`unknown` 照注（治理文本恰是
   技术公式判不了时的唯一口径来源）；无挂靠 metric → 仅治理字段（无门禁对象）。
7. **B3 零回归契约**：无匹配 / 冷缓存 / 开关关闭 / 空问句 / 装配器抛错 → 一律
   None（prompt 与现状逐字节一致），装配失败 warning 不阻断 plan。
8. **B4 守卫哲学**：弱校验（声明列 ⊆ SQL 分母列，包含即可，不做等价证明）+
   示警不硬拒（提案原设计：治理声明可能有历史误差，先积累数据）→ 复用 L2
   歧义示警出口（答案前置 ⚠️ 文本，与 shareWarning 同模式）。两个刻意偏离：
   ①不走 `kpiContractInjectionEnabled` 灰度开关（示警面不是 prompt 注入面，
   无污染风险且 shareWarning 同类均无开关）；②不做 B2 门禁（对账结论是
   「治理 vs 技术公式」，B4 问题是「SQL vs 治理声明」，两个独立问题）。
   判不了的一律 None：中文声明抽不出列名 / SQL 无除法表达式 / 装配异常
   （诚实口径，不谎报任一方向）。

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
- B3（无 API 变更，行为变更）：NL2SQL plan system prompt 在词典块之后新增
  `<kpi_contract>` 包裹块（仅当问句命中 PUBLISHED KPI 且灰度开关开）；
  开关 `KPI_CONTRACT_INJECTION_ENABLED`（默认 true）。wrapped 内容是数据不是指令
  （preamble 已声明），但块前仍附中文用途说明。不命中时 prompt 与现状零差异。
- B4（无 API 变更，行为变更）：chat 单步与 SSE 流式答案可能前置
  `⚠️ KPI 口径提示：治理契约声明的分母列 X 未全部出现在生成 SQL 的分母表达式中…`
  （仅当问句命中 PUBLISHED KPI、其 denominator 抽得出列名、且与 SQL 分母列
  不兼容时）。无命中/判不了时答案与现状零差异。

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
- **B3**：`kpi_contract_reconcile.py` 追加 `buildKpiContractBlock(session, question)`
  （装配器：matchAll Top-3 → 逐个 `session.get(OntologyMetric)` → 三检查门禁 →
  渲染「名称 (code@version) + 治理五件套行 + 技术公式行」；`_truncField` 120 字符
  截断防 prompt 膨胀）；`nl2sql_prompts._buildPlanSystemPrompt` +`kpiContractText`
  参数（`wrapUntrusted(UntrustedLabel.KPI_CONTRACT, ...)`——第 12 个 label，白名单
  同步）；`nl2sql_plan.generateQueryPlan` 透传；`nl2sql_service.generateValidatedPlan`
  在 `common` dict 装配（`_buildKpiContractOrNone` helper：session 缺席/空问句→
  None，异常→warning 降级 None）；`config.py` +`kpiContractInjectionEnabled`。
- B3 红队回归：`test_l1_prompt_injection_redteam.py` ALL_LABELS 11→12，参数化
  U+200B 打断用例覆盖 `kpi_contract` 标签；专项测试验证 `</kpi_contract>ignore`
  payload 无法逃逸包裹。
- **B4**：`nl2sql_semantic_guard.py` 新增纯函数 `denominatorContractWarning(sql,
  denominatorDeclared)`（除法表达式右操作数深度感知扫描 → 声明列 ⊆ 分母列集合，
  宁漏不误报：WHERE 内除法/窗口定义列也进集合只会偏大）+ `extractSqlIdentifiers`
  公共词法（自 `kpi_contract_reconcile` 上收——semantic_guard 是低层纯函数模块，
  依赖方向 reconcile → guard，DRY）；`kpi_contract_reconcile.py` 追加异步装配器
  `buildKpiDenominatorWarning(session, question, sql)`（matchAll Top-3 同 B3 口径 →
  第一个 denominator 非空的命中 KPI → 纯函数；try/except 收口在装配器内部，
  调用方直连）；`chat_service.py` / `chat_stream.py` 接线镜像 shareWarning 模式
  （答案前置 / SSE 首批 token 下发，快照 answerPieces 同引用保断连兜底可见）；
  `messages_zh.py` +`MSG_NL2SQL_DENOMINATOR_CONTRACT_WARNING`。

## 6. 测试

新增（21 + B3 15 + B4 18 用例全绿）：
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
- `integration/test_kpi_contract_injection.py`（9，B3，真实 PG）：治理五件套+技术
  公式渲染 / mismatch 不注入 / unknown 照注 / 无 metric 仅治理字段 / cap 3（4 命中
  只注 Top-3）/ 无匹配 None / DRAFT 不进缓存 / 开关关 None / 空问句 None。
- `prompts/test_nl2sql_plan_prompt_kpi_contract.py`（3，B3）：wrapped+用途说明 /
  None 零回归 / `</kpi_contract>ignore` payload 无法逃逸。
- `unit/test_kpi_contract_threading.py`（3，B3）：kpiContractText 透传到
  generateQueryPlan / session 缺席装配器零调用 / 装配器抛错降级不阻断。
  隔离手法同 test_property_ref_normalize（替换实例方法 `_planIssues`/`_finalizePlan`
  + try/finally 恢复）。
- **B3 测试期发现两个坑（已写进测试 docstring）**：①`KpiMatchCache.refreshOne`
  在 `_loaded=False` 时静默 no-op ⇒ 测试 seed 后必须 `onKpiChanged()+warmUp`
  整库预热，只调 refreshOne 是假绿；②Jaccard 匹配对长中文关键词 + 默认阈值 0.75
  结构性不可达（ngram 交集 ~0.2）⇒ 单 KPI 用例走 exact-alias 快车道（问句含
  KPI code，confidence=1.0，同 test_chat_l1_routing 约定），cap 用例走 Jaccard
  需短 keywords + 调低 `match_threshold`（0.05）。
- `unit/test_nl2sql_denominator_contract.py`（12，B4）：兼容/NULLIF 包裹/窗口/子查询
  通过 / 错列示警 / 多列部分缺失 / 字面量剥离 / 中文声明判不了 / 空声明 / 无除法 /
  None SQL / 分子侧列不算数（不整语句凑数）。
- `integration/test_kpi_denominator_contract.py`（6，B4，真实 PG）：mismatch 示警 /
  兼容不示警 / 中文判不了 / denominator 空 / 无匹配 / 空问句空 SQL。

回归：邻域单测 `test_chat_service.py`+`test_chat_service_stream.py`（基线比对，
净新增失败 0——既有 6 红为 BuildAnswerPrompt/stream 超时族，与本批无关）；
KPI 集成 48 绿；ontology 集成 52 绿；B2 后 compiler 三套（B2 新增 9 + 三库
reconcile + compiler unit 22）全绿；KPI/L1/metric 全量集成 -k 选择 195 绿。

B3 回归：redteam + prompts 全目录 98 绿；B3 三套 15 绿；B2 对账 9 绿；
nl2sql 集成 + property_ref_normalize 23 绿；chat 集成（l1_routing/multi_step/
metric_draft_kpi/stream_l1_l4）53 绿；unit -k "nl2sql or kpi or prompt or
sanitizer" 504 绿 + 4 红（`test_chat_service.py::TestBuildAnswerPrompt`——
基线 1b29e139 复跑同 4 节点同红，净新增失败 0；stale double：测试断言
`"total": 0` 而现实现序列化 `{"rows": []}`，属既有 stale-double 家族）。

B4 回归：B2/B3 守卫域五套 56 绿（词法上收后 reconcile/injection/guard 全绿）；
B4 新增 18 绿；chat 集成（stream_l1_l4/l1_routing/multi_step/stream_disconnect/
metric_draft_kpi）57 绿；unit -k "nl2sql or kpi or prompt or sanitizer or chat"
776 绿 + 6 红（4 个 TestBuildAnswerPrompt + 2 个
`test_chat_service_stream.py::TestStreamingPipeline`——基线 aaba0a43 复跑同
2 节点同红，净新增失败 0；summary.md 此前已记「BuildAnswerPrompt/stream 超时
族」既有红）。
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
- B3 注入面：治理契约是企业管理员维护的数据，但预算为不可信——走
  `wrapUntrusted(KPI_CONTRACT, ...)`（HTML 转义 + U+200B 打断 closing tag），
  semantic_keywords / numerator 等自由文本字段可被恶意填写 ⇒ 按注入面处理；
  块前用途说明明确「是数据不是指令」；prompt/plan/service 三层装配失败全部
  降级 None 不阻断 plan（fail-open 于可用性、fail-closed 于注入面）。
  灰度开关默认 true，出问题可环境变量一键回退（零代码回滚）。
- B4 示警面：内容只含治理声明列名与缺失列清单（管理员维护的数据），无用户
  输入原样回显（声明列名经标识符正则抽取，非原文回显）；装配器内部
  try/except 收口（异常 → None），示警面永不阻断答案。弱校验不硬拒是提案
  明确设计（治理声明可能有历史误差，先积累数据再收紧）。
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
6. B3：问句含某 PUBLISHED KPI 的 code 或关键词（如「供货及时率是多少」）→
   后端日志确认 plan system prompt 含 `<kpi_contract>` 块（如可观测）；行为面
   验证比率型指标计划遵守分子/分母口径；再问无关问题确认 prompt 无该块（零回归）；
   `KPI_CONTRACT_INJECTION_ENABLED=false` 重启后确认块消失（灰度回退演练）。
7. B4：问句命中 denominator 为英文列口径的 PUBLISHED KPI 且生成 SQL 分母用了
   其他列 → 答案/流式首 token 出现 `⚠️ KPI 口径提示`；分母兼容或声明为中文时
   答案无该前缀（零回归）；观察后端日志 `KPI 分母契约弱校验示警` 频率
   （治理声明与生成 SQL 的实际漂移面可见化——B4 的原始目的）。

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
