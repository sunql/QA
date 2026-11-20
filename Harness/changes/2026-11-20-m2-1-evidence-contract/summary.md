# 变更：M2-1 Evidence 契约补全（加性可选字段，旧调用方零破坏）

- 日期：2026-11-20　分支：`feat/knowledge-evidence-contract`（off main @ `3a90e633`）

## 内容
`KnowledgeEvidenceRead`（P4 知识 Facade 四路证据 DTO）补全跨板块设计稿 §8.6 的目标契约：
- 新增嵌套模型 `KnowledgeEvidenceSourceRef`（system/object/recordKey/documentId/queryId）
  与 `KnowledgeEvidenceSecurity`（level/scope）；
- 加性字段：`evidenceId`（**default_factory uuid4 ⇒ 每条证据响应期自动生成、逐条唯一**）、
  `confidence`（[0,1]）、`observedAt`、`validAt`、`ruleVersion`、`sourceRef`、`security`；
- **回填策略**：RAG 路 `confidence = 1 - distance`（四路唯一有分数的通道）本批即回填；
  DOCUMENT 相关（security/observedAt/validAt）随 **M2-4 文档检索**回填；
  sourceRef 随 **M2-2/2-3**（实体解析/受控邻接）回填。

## 命名偏差（登记）
设计稿证据里嵌套 `source{...}` 与现仓 `source: str`（发布面出处 RELEASE/LIVE）冲突 ⇒
物理来源定位改名 **`sourceRef`**，`source` 原样保留。confidence 用 float（仓内惯例），
不用设计示例的 "VERIFIED" 字符串。

## 测试
- 新增 2 用例（并入既有文件）：RAG 契约字段（evidenceId 逐条唯一 + confidence=1-distance
  0.9/0.89/0.88 精确断言 + camelCase 线格式）+ 契约序列化（嵌套 camelCase / 缺省构造全空 /
  旧字段原样）。
- 既有 facade contract 断言（releaseVersion/source）不动 ⇒ 兼容性被既有测试直接守护。
- 知识面回归 **107 passed**；ruff 净新增 0。
