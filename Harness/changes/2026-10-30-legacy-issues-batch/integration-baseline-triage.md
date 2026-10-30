# Integration 基线红处置表（CI 首轮全量暴露）

- **日期**：2026-10-30
- **背景**：第四轮 CI（run 41058271071）首次完整跑通 integration 全量（297 文件 / 4 分片），
  暴露 **85 条失败（shard1 26F+4E / shard2 33F / shard3 22F）+ shard0 待统计**。
  三角定位：**全部失败在 main @ `1b9f4f71` 本地逐一复现（shard1/2/3 已验证，净新增 = 0）**——
  这是「无 CI + 从未整体跑过全量」积累的基线债，不是本批引入。
- **根因分类方法**：每个文件提取首个 `E` 行报错 + 本地/CI 双环境对照。
- **处置分层**：
  - **A 层 = 脚手架/环境问题**：测试保留，修环境装配或 fixture，不改断言语义；
  - **B 层 = 断言依赖累积库状态**：测试保留，断言改写为「自足式」（用例内自造数据，不依赖库里已有种子）；
  - **C 层 = 需验尸**：行为变更 / 真 bug / 测试过时三种可能并存，验尸前不动；
    验尸判据：行为对 ⇒ 改测试；行为错 ⇒ 修系统；测试彻底过时 ⇒ 作废并在此表注明原因。

## A 层：修脚手架（19 条，最便宜，先做）

| 文件 | 条数 | 首个报错 | 根因 | 修法 |
|---|---|---|---|---|
| test_milvus_wiki_fields | 3 | `MilvusException: database not found[qa_ci]` | `MILVUS_TEST_DB_NAME` 指定的 database 全新 Milvus 实例里不存在（以前靠生产实例里恰好有的 `qa_test` 库兜底） | CI Milvus 启动后预建 `qa_ci` database（或 conftest ensure-create）；一次性修好 3 个文件 |
| test_milvus_document_fields | 4 | 同上 | 同上 | 同上 |
| test_rag_api | 4 | `Milvus 写入失败: database not found[qa_ci]` | 同上 | 同上 |
| test_business_object_migration | 1 | `Connect call failed ('127.0.0.1', 5433)` | **测试硬编码** `localhost:5433` + dev 密码，违反 env 契约 | 改读 `TEST_DATABASE_URL` |
| test_incoming_inspection_class | 1E | 同 5433 connect（shard0 待全量确认） | 同上 | 同上 |
| test_chat_stream_l1_l4 | 2E | `NotNullViolationError: header_class_id`（setup 阶段） | fixture 向 business_object 插 null，未对齐 NOT NULL 约束 | fixture 补 header_class_id；**验尸附带**：确认 seed 回填路径本应给值 |
| test_stream_meta_single_frame | 2E | 同上 | 同上 | 同上 |
| test_evidence_sql_hook | 1 | 同上（非 setup） | 同上 | 同上 |
| test_wiki_relation_api | 2 | `UniqueViolation uq_ontology_class_name_version` | schema 加了类名+版本唯一约束，fixture 重复插类名未适配 | fixture 改用不重名/复用已有类 |

## B 层：断言改自足式（34 条）

| 文件 | 条数 | 首个报错 | 根因 | 修法 |
|---|---|---|---|---|
| test_wiki_coverage_api | 14 | `assert 48 == 8` | 网格数 = 现库类种子数（本地累积库 8 类 vs 全新库 48 类）——断言把「库里有几个类」当成了常量 | 用例内自建固定类集合再断言网格形状 |
| test_ontology_reconcile_sync | 8 | `assert 5 == 0` / `assert 7 == 2` | 同「库内既有类/属性数量当常量」 | 同上 |
| test_local_import | 3 | `assert 7 == 2` | 同上 | 同上 |
| test_knowledge_compiler_reconcile | 3 | `{'class': 5, ...} == {'class': 0, ...}` | diff 统计依赖库内既有本体与对账基线的差量 | 用例内构造已知差量场景 |
| test_apply_supplier_join_edges | 2 | `assert 0 == 7` | 源数据（dim_supplier 等）在全新库为空 ⇒ 0 条边 | 用例内 seed 源数据 |
| test_apply_dim_imaterial_join_edges | 1 | `(11, MATERIAL_CODE...) not in {(7,...), (8,...)}` | 断言写死了库内自增行 id | 断言改按业务键（非代理键）匹配 |
| test_ontology_governance_integration | 1 | `assert 28 == 27` | 种子计数当常量 | 同 B 通法 |
| test_seed_entity_mapping | 1 | `assert 2 == 20` | 同上 | 同上 |
| test_reconcile_neo4j_id_mapping | 1 | `expected 0 diff, got 7` | Neo4j/PG 两侧对账依赖库内既有状态 | 用例内构造对齐/不对齐两态 |

## C 层：逐条验尸（32 条，验尸前不动）

### C-1 鉴权守卫集群（4 条，**安全相关，最优先**）

| 文件 | 条数 | 首个报错 | 验尸问题 |
|---|---|---|---|
| test_wiki_structure_api | 1 | `assert 200 == 403`（匿名请求成功） | stage 端点守卫是被有意移除还是从未存在？（对照 [[qa-system-router-auth-mandatory]] 审计） |
| test_wiki_batch_delete_api | 1 | `assert 200 == 403` | 同上 |
| test_kpi_catalog_search_api | 1 | `assert 200 in (401, 403)` | 同上 |
| test_wiki_conflict_api | 1 | `assert 404 == 403`（路由 404） | 路由是改名了还是没了？测试 URL 过期 or 端点真丢 |

⚠️ 若验尸结论是「守卫缺失」，这是**产品代码真问题**（CRITICAL 级），立即修系统而非改测试。

### C-2 行为/契约类（17 条）

| 文件 | 条数 | 首个报错 | 验尸问题 |
|---|---|---|---|
| test_admin_user_password | 4 | `{"code":"MUST_CHANGE_PASSWORD"}` | 建用户后强制改密是后来加的策略？测试没跟上（大概率改测试） |
| test_seed_business_objects | 5 | `assert 5 == 6` | seed 是 5 个还是 6 个？哪个是预期？ |
| test_business_object_registry | 3 | `NCR should be valid` | 与 seed 数量同源，先定 seed 再看 |
| test_multistep_global_filter | 3 | `_GlobalFilterLlm has no attribute completeStream` | mock 落后接口（假替身保真度）——改 mock，但顺带确认接口演进无回归 |
| test_term_dictionary_supply_qty | 4 | 待逐条 | L1 SECURITY preamble 加壳后 prompt 结构变化，断言子串丢失（大概率改断言锚点） |
| test_term_dictionary_inject | 1 | `价格表号='T10' not in prompt` | 同上 |
| test_app_wiring | 1 | catch-all Mount 未找到 | MCP 挂载装配是有意变更还是意外丢失？ |
| test_classify_and_recall | 1 | `QUERY is REFINE` | 路由意图漂移：分类器行为变更 or 测试数据触发漂移 |
| test_chat_hypothesis | 1 | multistep trigger 未持久化 | 同上 |
| test_chat_agent_run | 1 | `assert 0 == 1`（done 事件无 card） | card 装配路径变更 or 测试过期 |
| test_receiptdetail_qty_alias | 1 | 待逐条 | 别名守卫行为确认 |

### C-3 待提取细节（11 条）

| 文件 | 条数 | 备注 |
|---|---|---|
| test_data_quality_api | 2 | 日志里报错块未提取到（NL2SQL PLAN_EMPTY 降级噪声），修复批次本地复跑取证据 |
| test_ontology_class_versioning | 1 | 疑似与 wiki_relation 同族（唯一约束 fixture 适配） |

## 与本批 PR 的关系

- 本表 **不阻塞 PR #73 合并**：净新增失败 = 0（shard1/2/3 已逐一在 main 复现验证）；
- CI 将如实带红运行（unit 14 条 + integration ~85+ 条），每次红都是待修清单的提醒；
- 修复批次顺序建议：**A 层（一次性 Milvus 建库 + env 契约）→ C-1 鉴权集群（安全优先）→ B 层 → C-2/C-3**。
- shard0 失败清单出来后补进本表。
