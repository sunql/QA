# 变更：「默认约束 + 明确指定」双层例外规则

- **日期**：2026-09-21
- **作者**：Claude
- **Phase**：维护 / 数据与 AI 治理（无新功能；规范化已有口径）
- **状态**：done
- **关联变更**：
  - [feat-schema-digest](../feat-schema-digest/summary.md)（同日期，本规则的依赖前提）
- **迁移版本**：无（纯 description / term dict 数据调整，不动 schema）
- **MEMORY**：
  - [../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-supplier-exception-rule.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-supplier-exception-rule.md)
  - [../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-digest.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-digest.md)

---

## 1. 需求

### 背景

2026-09-21 一天内两次踩同一根因：**本体属性 description 写的「默认 + 例外」只有单层例外（声明类）**，缺「明确指定对象」例外。LLM 在用户明确指定具体对象（供应商编码、物料编号）的场景里仍套用默认过滤，导致合法查询被错误截掉。

### 目标

把这一反复出现的问题**抽象为通用规则**，固化到 [数据与 AI 治理规范 §默认约束的反例规则](../../rules/数据与AI治理规范.md#默认约束的反例规则default--exception-pattern)。后续任何「默认 X」的业务口径（含 ontology_property / term_dictionary / prompt 模板）必须配双层例外。

### 验收标准

- [x] 三处 ontology_property.description 含【口径例外】段、双层例外
- [x] term_dictionary「外部供应商贸易」新增，definition + formula_hint 双层例外
- [x] 集成测试新增 `TestExternalTradeExplicitSupplierException` 3 用例，全绿
- [x] 真机稳定性 3/3：明确指定场景不带默认过滤、默认场景带过滤
- [x] 数据与 AI 治理规范追加段落，列出规则、落地形式、防御层级、适用范围

---

## 2. 设计评审

### 候选方案对比

| 方案 | 内容 | 优 | 劣 |
|---|---|---|---|
| A. 仅修本次两条规则 | 只更新 TCLCOD_0 + INTER_COM_CODE 的 description | 最小改动，零风险 | 下次再有类似口径仍会重蹈覆辙（不抽象） |
| B. 抽象为通用规则 + 修本次两条 | 追加 Harness 规则 + 修复当前两条 + 加固测试 | 一劳永逸、新口径天然遵循 | 改动面稍广（rules + 4 处数据 + 3 测试） |
| C. 改 NL2SQL 引擎增加「明确指定场景检测」 | chat_service 加前置检测器 | 引擎级自动防御 | 误判风险大（中文歧义），改动链路过长，违反 KISS |

**最终决定**：B。本变更的核心是**规范化沉淀**，而非单点修复。

### 设计原则

1. **声明类 + 指定对象** 双层例外缺一不可（用户场景反复证明）
2. **最小表述单元 = term dict 单独条目**，不合并（LLM 按 mapped_property 找规则）
3. **防御层级 ≥ 2**：description + 顶部 digest + term dict 三层并行（命中任一即可）

---

## 3. 数据模型变更

无 alembic 迁移（纯数据层调整）。本体 schema 不变。

### 数据层修改（API 写入 prod）

| 表 | id | 字段 | 旧字数 | 新字数 | 关键变化 |
|---|---|---|---|---|---|
| ontology_property | 716 | description (INTER_COM_CODE) | 167 | 263 | 补「明确指定供应商」例外 |
| ontology_property | 715 | description (INTER_SITE_CODE) | 109 | 187 | 从值映射改写为含双层例外的口径描述 |
| ontology_property | 367 | description (BPSNUM_0) | 46 | 147 | 补双层例外（明确指定供应商 / 声明内部交易） |
| term_dictionary | 6 (新建) | 全字段 | – | – | term=「外部供应商贸易」/ mapped=DWD_GOODS_RECEIPT_LINE.INTER_COM_CODE |

### 向量同步

`updateProperty` 末尾触发 `_syncPropertyEmbeddingBestEffort`（app/services/ontology_service.py:624），
description 变化后**自动**同步 Milvus ontology_embeddings，无需手动跑脚本。

---

## 4. 接口契约变更

无新增 API。复用现有：
- `PUT /api/v1/ontology/properties/{id}` — 写 description
- `POST /api/v1/term-dictionary` — 新建 term 条目

---

## 5. 实现要点

### 关键文件

- `Harness/rules/数据与AI治理规范.md` — 追加 §默认约束的反例规则
- `backend/app/tests/integration/test_term_dictionary_supply_qty.py` — 加 `TestExternalTradeExplicitSupplierException` 3 用例
- 4 处 prod 数据通过 API 写入（prod PG qa_metadata）

### 落地模板（双层例外写法）

**ontology_property.description 末尾**：
```
【口径例外】不套用 <默认过滤> 的场景：
(1) 用户明确说明「<类别 A>」「<类别 B>」；
(2) 用户明确指定了具体 <对象类型> 编码或名称（已知对象时不限制）。
```

**term_dictionary.definition 末尾**：同 ontology_property 模板。

**term_dictionary.formula_hint 末尾**：
```
【例外】用户已明确指定 <对象> 编码/名称，或者明确声明 <类别> 时，
不应用 <默认过滤> 过滤（保留全部数据）。
```

---

## 6. 测试

### 新增用例（`backend/app/tests/integration/test_term_dictionary_supply_qty.py`）

| 用例 | 断言 |
|---|---|
| `test_external_trade_term_injected_with_exception` | term 注入 prompt、含 INTER_COM/SITE = '1' 默认过滤 |
| `test_external_trade_term_contains_explicit_supplier_exception` | 含「明确指定了具体供应商」字样（防回归单层） |
| `test_external_trade_term_distinct_from_supply_qty` | 「供货量」与「外部供应商贸易」作为独立条目共存 |

### 全文件回归（8 用例）

```
8 passed, 20 warnings in 16.68s
```

### 真机稳定性（各 3 次）

| 场景 | INTER 过滤 | TCLCOD_0 过滤 | Top3 / 三家一致 |
|---|---|---|---|
| 明确指定 B019/B125/D1 | ❌ 0/3 | ❌ 0/3 | ✅ B019/D1/B125 一致 |
| 默认 3 月供货 Top3 | ✅ 3/3 | ✅ 3/3 | ✅ B125/B019/B153 一致（无回归） |

---

## 7. 安全审查

未触发 security-reviewer 强场景（无认证/密钥/SQL Guard/外部 API/支付变更）。
属性 description 是用户向 LLM 注入业务知识的通道，**schema 渲染走 `_sanitizeSchemaField`
做 XSS 防护**（已存在）；本变更描述文本含中文「」符号，无 HTML 标签，转义安全。

| 等级 | 项 |
|---|---|
| LOW | NOTE：description 长度未做硬上限，管理员可写超长 description 撑爆 prompt（已由 feat-schema-digest 的 digest 50 条封顶 + 200 字截断兜底） |

---

## 8. 部署验证

### 部署命令

```bash
bash scripts/deploy_backend.sh  # 后端 app/scripts/alembic 同步 + 重启
```

### 冒烟

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"SUNql@123"}' | jq -r .accessToken)

# 场景 1：明确指定（应不含 INTER 过滤）
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sessionId":"smk1","question":"B019、B125、D1 这三家供应商的供货情况","datasourceId":1,"modelId":1}'
# 预期：SQL 不含 INTER_COM_CODE / INTER_SITE_CODE 过滤

# 场景 2：默认口径（应含 INTER 过滤）
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sessionId":"smk2","question":"3月份供货量最多的三家供应商","datasourceId":1,"modelId":1}'
# 预期：SQL 含 INTER_COM_CODE='1' AND INTER_SITE_CODE='1'
```

均 3/3 稳定通过。

---

## 9. 关联

- 设计稿：无（规则抽象，无新架构）
- Wiki：`Harness/wiki/` 无须新条目（规则在 rules/ 数据治理章节）
- Rules：
  - [../../rules/数据与AI治理规范.md](../../rules/数据与AI治理规范.md#默认约束的反例规则default--exception-pattern)
- Memory：
  - [qa-system-supplier-exception-rule](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-supplier-exception-rule.md)
  - [qa-system-schema-digest](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-digest.md)
- 关联变更：
  - [feat-schema-digest](../feat-schema-digest/summary.md) — 同日期前提（顶部 digest 让 LLM 看得见 description）

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B/C）
- [x] 第 3 段无迁移文件名（纯数据调整，已注明）
- [x] 第 7 段触发了 security-reviewer（LOW NOTE 已记录）
- [x] 第 8 段冒烟命令 + 输出贴出
- [x] 第 9 段 ≥ 3 个跨文件链接（Rules + 2 个 Memory + 1 个 predecessor）
- [x] 相关 wiki 文档：规则落到 rules/ 数据治理章节（无 wiki 条目新增）
- [x] 至少 1 条 MEMORY 索引已在 MEMORY.md 添加
- [x] 真实 SQL 验证：3/3 + 3/3 稳定性冒烟通过
