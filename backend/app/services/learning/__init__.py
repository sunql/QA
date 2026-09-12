"""知识自学习机制（feat-wiki-knowledge，Phase 8）。

分层：

- ``llm_invoker``：统一的 LLM 调用层（按 id 选模 + fallback + 计量），
  机制 1-4 都经由它调用模型，不各自 new client。
- ``auto_classifier``（M2/M3）：机制 1 自动分类。
- 后续：``relation_discovery`` / ``conflict_detector`` / ``structure_suggester``。

与 ``app/services/`` 下其它模块的关系：本包只做「知识生长」的判断，
知识条目本身的读写仍归 ``wiki_page_service``。
"""
