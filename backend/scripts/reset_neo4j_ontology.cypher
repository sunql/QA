// feat-business-data-reset — Neo4j 本体子图清理
// 注意：本仓库本体图实际标签是 Class / Property（非 Ontology* 前缀）。
// 详见 summary.md 修正说明。先 CALL db.labels() 确认。

MATCH (n)
WHERE n:Class OR n:Property
DETACH DELETE n;