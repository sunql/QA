# G2：设计稿 §5.5 的 6 个校验码（CLS/CLS-002/REL/AGT/GRF/MET-002）

- 日期：2026-11-22　分支：`feat/ontology-evolution`
- 依据：`docs/update/1118/M7/ontology-evolution-program.md` §三（G2 行）；
  `docs/update/采购本体模型与语义编译器详细设计_V0.1.docx` §5.5 校验规则目录
- 结论：设计稿 12 个校验码中，**此前落地约一半**；本次补齐 6 码进同一张
  `semantic_release_check` 表（不新建表、不新建发布通道）。

## 一、落地内容

| code | 判据 | severity | 事实来源 |
|---|---|---|---|
| `CLS-001` | 类名唯一 / objectType 合法 / **父子继承无环** | ERROR | manifest（**成环**是契约闸查不了的） |
| `CLS-002` | 头类存在 / 主域存在且**启用** | ERROR | manifest |
| `REL-001` | JOIN 两端类存在 + 活库语义关系类型白名单 / 端点墓碑 | ERROR | manifest + `ontology_relation` |
| `AGT-001` | Agent 数据域/数据层在启用词表 + 绑定工具已注册且启用 | WARNING | `agent_definition` / `data_domain` / `data_layer_rule` / `agent_tool_config` |
| `GRF-001` | 图 label 在 Neo4j 业务实体白名单 + 入图对象头类有主键 | WARNING | manifest + 代码白名单 |
| `MET-002` | 已发布 KPI 绑定**启用中**数据源 + 关联指标已发布 | WARNING | manifest + `data_source` |

新增模块：

- `app/services/semantic_release_design_checks.py` —— 纯判据（无 IO）；
- `app/services/semantic_release_context_facts.py` —— 活库事实装载器；
- `app/domain/graph_allowlist.py` —— 图 label/关系白名单下沉为 domain 叶子模块
  （`neo4j_client` 改为从它导入并 re-export，历史 import 路径不变）。

## 二、关键取舍（都有代码注释）

1. **CLS/REL/GRF/AGT 出一个汇总行，MET-002 逐 KPI**：
   逐类（5000）/逐 JOIN（20000）会把单发布写放大到数万行且没人读；
   汇总行条数是**常数** ⇒ 上限可精确从契约推导。
2. **`None` = 没查过**（沿用候选绑定口径）：`AGT-001` 不查就不出行；
   `REL-001` 不查只判快照可见部分（PASS 行明写「未查」）。
3. **severity 取舍**：KPI 数据源绑定在 KPI 发布闸门另有强校验
   （compliance 文档「MET-002 在别处」）⇒ 本目录按治理债 WARNING；
   图 label 不在白名单常是「有意不入图」⇒ WARNING；CLS/REL 是真结构坏 ⇒ ERROR。
4. **`GRF-001` 的 key 判据只对「声明了属性」的类生效**：空属性列表是属性治理
   （PRP）的问题，拿它报图定位会让每条早期发布都背一条无行动价值的告警。

## 三、上限重推导（防「合法 manifest 被截断」）

```
MAX_CHECKS_PER_RELEASE
  = 6 + 3×MAX_MANIFEST_METRICS + 2×MAX_MANIFEST_SOURCE_BINDINGS
    + MAX_MANIFEST_KPIS + 1 + 5          # MET-002 逐 KPI + 5 个设计码汇总行
```

新增公开常量 `MAX_MANIFEST_KPIS`（manifest `kpis.maxItems` 由它派生，不再写死 2000）。

## 四、验证

| 套件 | 结果 |
|---|---|
| `app/tests/unit/test_semantic_release_design_checks.py`（新，29 例） | 全绿 |
| `unit/test_semantic_release_checks.py` 等 7 个发布相关单测 | 124 passed |
| `integration/test_semantic_release_{service,api,constraints,route_auth}.py` | 151 passed |
| 新增集成用例（活库事实 + 端到端落表） | 5 passed |
| 单元「设计码 + 图谱」回归 | 131 passed |
| 语义编译器/切片/差异/Registry 回归 | 215 passed |

⚠️ 本机测试库须用 `QA_TEST_DB_NAME=qa_metadata_test_mainline`：共享库
`qa_metadata_test` 已被采购线升到 0159，本分支 head 是 0165。

## 五、遗留

- `AGT-001` 的「ACL 对齐」未实现：设计稿只有一句原则，没有可判定规则；
  不发明口径，等写入口收口时定义。
- `RAG-001`：依赖文档语料（与 Q04 同阻塞）。
- `KEY-001`：按 D2 偏差不建（统一键走 `entity_mapping`）。
