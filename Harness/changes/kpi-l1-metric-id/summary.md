# 变更：kpi-l1-metric-id（B1+B5，KPI 治理 → 技术链路提案 Phase 1）

- **日期**：2026-10-28
- **作者**：Claude
- **Phase**：Phase 1（提案 `../2026-10-27-kpi-governance-into-nl2sql-proposal.md` 的 B1+B5 批次）
- **状态**：draft（待部署验证后转 in-review）
- **关联变更**：[KPI 治理注入提案](../2026-10-27-kpi-governance-into-nl2sql-proposal.md)
- **迁移版本**：无（B1 最小版不加列不迁移；后续如需 FeatureDefinition.metric_id 另起 0136+）
- **分支**：`feat/kpi-l1-metric-id`

---

## 1. 需求

L1 KPI 直连的 FeatureDefinition 解析只有 `kpi.formula == FD.feature_definition` 字符串
等值一条路：锚点脆弱、失败静默。2026-10-27 探针实证：dev 12 条存量 KPI 全走此路径且
**无一条命中**（全部静默降级为无数据回答）；`ontology_metric` 总数为 0（/metric 从未
成功过）。B1 = 解析契约改「外键链路优先 + 等值回退可见化」；B5 = 消灭「有技术形态无
治理户头」的孤儿指标。

## 2. 设计评审

候选与取舍见提案 §B1/§B5。本批落地时的两个实证修正：

1. **探针①=12/②=0/④=0** ⇒ B1 的 metric 路径当前是零用户的前瞻契约，真正见效的是
   回退路径的 warning 可见化；锚点列选 `feature_definition`（与回退同列，漂移面两份
   归一），`feature_name` 列锚点变更留给 B2 对账批次决策。
2. **实现中发现 /metric 在 main 上本来就是坏的**：`OntologyService.createMetric`
   要求 keyword-only `actor`，`chat_domain` 调用漏传 → `TypeError` → 500。这解释了
   dev `ontology_metric` 为 0。本批顺修（actor=调用方，匿名链路 "chat"，与
   `_handleAgentRun` 同约定）。

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

## 6. 测试

新增（12 用例全绿）：
- `integration/test_chat_l1_calculation_resolution.py`（6）：外键命中不进回退 /
  metric 未命中降级 / metric.formula 空白降级 / 存量回退命中 warning / 双未命中
  None+warning / 空 formula 静默 None。悬空 metric_id 用例被 FK 约束否定
  （DB 层不可能悬空——恰是外键链路可靠性的佐证），改用空白 formula 覆盖降级分支。
- `integration/test_chat_metric_draft_kpi.py`（6）：HTTP 全链路 /metric → DRAFT 户头
  （created_by 归属、尾注回显）／户头失败不砸指标答案／service 幂等／撞码后缀／
  grain 不覆盖 warning 不阻断／覆盖不告警。

回归：邻域单测 `test_chat_service.py`+`test_chat_service_stream.py`（基线比对，
净新增失败 0——既有 6 红为 BuildAnswerPrompt/stream 超时族，与本批无关）；
KPI 集成 48 绿；ontology 集成 52 绿。
单测 fake 修正：`_FakeOntologyService.createMetric` 补 `actor`（原 fake 断言的是
main 上实际会 500 的坏调用路径——假替身保真度问题）；两文件 autouse stub
`ensureDraftForMetric` 抛错走降级路径（MagicMock session 恒真值会塞 mock 垃圾进答案）。

## 7. 安全审查

- createMetric 漏传 actor 属审计归属缺失（高优顺修）；修复后 /metric 审计链恢复。
- `ensureDraftForMetric` 绕过 ACL？——chat `/metric` 走 `getCurrentUser` 认证，
  created_by 落真实 userId；DRAFT 户头不进 L1 缓存、不改变任何读路径权限面。
  MEDIUM：DRAFT 户头的后续 PUT 走既有 ACL（owner/admin），无新增暴露面。
- 无 SQL 拼接（全 ORM 参数化）；无密钥操作。

## 8. 部署验证

未部署（draft）。部署时冒烟：
1. `docker exec -it qa-backend python -c` 探活；
2. chat 发 `/metric 测试指标 = SUM(x)/COUNT(*)` → 答案含户头尾注；
3. dev 库验证 `SELECT * FROM kpi_catalog WHERE kpi_code LIKE 'KPI_AUTO_M%';`
4. 观察后端日志 `L1 回退路径` warning 出现频率（存量 12 条 KPI 的脆弱路径可见化）。

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
