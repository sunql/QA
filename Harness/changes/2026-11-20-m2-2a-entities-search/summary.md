# 变更：M2-2a 采购实体解析 `GET /procurement/knowledge/entities/search`

- 日期：2026-11-20　分支：`feat/knowledge-evidence-contract`（与 M2-1 同支，off main）

## 内容（设计稿 §8.3 第一端点）
- **新路由** `app/api/v1/procurement_knowledge_entities.py`（有逻辑文件，与薄别名的
  AST 守卫文件分开）：`q / entityType / limit` ⇒ `list[ProcurementEntityMatchRead]`；
  双接线（main.py + _testapp.py，同 `enableKnowledgeFacade` 块、同前缀、同依赖）。
- **检索复用** `EntityMappingService.searchMappings`（AutoComplete 同款语义：全数字
  精确 key + ILIKE 兜底 + 0086 trigram 索引支撑 35w 行）——零重复造轮子。
- **新纯函数** `services/knowledge/entity_search.buildEntityMatches`：按统一实体
  （enterprise_key）分组 ⇒ 一实体一行 + `sourceMappings[]`（全部源系统）+
  `matchScore`（enterprise_key 精确=1.0 > 编码全等=0.9 > 前缀=0.8 > 中缀=0.6，
  确定性无 LLM），排序分数降序 + key 升序兜底。
- **双接线守卫**：源码级断言 main/_testapp 两处都注册（两套 wiring 手工平行维护
  的已知 404 坑钉死）。

## 测试（7 用例，并入新 unit 文件——unit 新建安全）
纯函数 ×5（分组/打分/精确 key/空输入/入参不变）+ handler 直调真实 PG ×1（两源
映射落库 → 契约字段 + camelCase）+ 双接线守卫 ×1。

## 已登记
- 测试直调 handler 绕过 HTTP（CI 不开 `ENABLE_KNOWLEDGE_FACADE` ⇒ HTTP 集成
  面与 P4 现状一致为 unit 守护）。
- `matchScore` 为检索期确定性得分（非持久字段）。
