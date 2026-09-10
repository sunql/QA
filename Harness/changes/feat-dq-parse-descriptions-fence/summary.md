# 变更：DQ parse-descriptions —— LLM ```json``` fence 自动剥离

- **日期**：2026-09-10
- **作者**：QA System
- **Phase**：bugfix（生产 503 → 200）
- **状态**：done（已部署 + 真机冒烟通过）

## 1. 需求

用户报障（数据质量向导，使用 AI 建议时）：

> POST /api/v1/data-quality/rules/generate/parse-descriptions 503 (Service Unavailable)
> （AI 返回格式无法解析）

前端调用 DQ 规则生成的「AI 建议」步骤持续 503，业务卡死，无法采纳任何
constraint suggestion。

## 2. 根因

`data_quality_rule_llm_service.parsePropertyDescriptions` 在收到 LLM
response 后直接 `json.loads(content)`。deepseek-chat（生产配置
`modelId=1`）的 content 几乎总是用 markdown ```json … ``` 代码块包裹
JSON 文本，**首字符不是 `{`**，触发 `json.JSONDecodeError`：

```python
content = getattr(response, "content", "") or ""
try:
    parsed = json.loads(content)        # ← 裸 loads，未剥 fence
except json.JSONDecodeError as e:
    raise LLMUnavailableError(MSG_DQ_GEN_LLM_PARSE_ERROR) from e
# → HTTP 503「AI 返回格式无法解析，请稍后重试」
```

与历史 NL2SQL `parseSqlFromResponse` 同一根因模式 —— 但 NL2SQL 侧已修
（`_JSON_FENCE_RE` + `_stripJsonFence`），DQ 侧从未同步，所以 DQ 一直是
潜在 503 雷区。

## 3. 设计

DQ 侧独立引入 fence 处理（不复用 NL2SQL helper，避免跨服务耦合）：

```python
# app/services/data_quality_rule_llm_service.py
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

def _stripJsonFence(content: str) -> str:
    """优先抽出 ```json/``` 代码块，否则原样返回。"""
    if not content:
        return content
    match = _JSON_FENCE_RE.search(content)
    if match:
        return match.group(1).strip()
    return content.strip()
```

`parsePropertyDescriptions` 调用顺序调整为：

```python
content = getattr(response, "content", "") or ""
payload_text = _stripJsonFence(content)        # ← 新增剥 fence
try:
    parsed = json.loads(payload_text)
except json.JSONDecodeError as e:
    raise LLMUnavailableError(MSG_DQ_GEN_LLM_PARSE_ERROR) from e
```

- 三种返回形态全部覆盖：
  - `` ```json\n{...}\n``` `` → 剥掉 fence（deepseek 默认）
  - `` ```\n{...}\n``` `` → 剥掉 fence（无语言标记）
  - `{...}` → 原样通过（向后兼容裸 JSON）
- 仍非 JSON → 下层 `json.JSONDecodeError` 兜底，**保留 503 契约**
  （不是任何内容都 200；坏 JSON 仍按 unavailable 上抛）。

## 4. 数据模型变更

无。

## 5. 接口契约变更

无。响应 schema、错误码、503 文案一字未动。

## 6. 测试

### 新增（3 条，integration）

`app/tests/integration/test_dq_rule_generate_llm_api.py`：

| 用例 | 覆盖 |
|---|---|
| `test_parse_descriptions_strips_markdown_json_fence` | LLM 返回 `` ```json ... ``` `` → 200 + suggestions |
| `test_parse_descriptions_strips_bare_fence_without_language` | 仅 `` ``` ... ``` `` 也正常解析 |
| `test_parse_descriptions_accepts_bare_json_unchanged` | 裸 JSON 回归基线（向后兼容） |

TDD 流程：3 条 RED 确认（404/503 非预期）→ 实现 `_stripJsonFence` → GREEN。

### 回归

- `test_dq_rule_generate_llm_api.py`：**15 通过**（原 12 + 新 3）
- 整个 DQ integration 套件（`llm_api + generate_api + generate_confirm_api`）：**29 通过**
- `test_parse_descriptions_returns_suggestions`（happy path）与
  `test_parse_descriptions_llm_down_returns_503`（LLM 宕机 → 503 契约）
  仍未坏。

## 7. 安全审查

- `_stripJsonFence` 仅做正则剥外壳，不接触业务数据。
- 不引入新 prompt / 不变更 system prompt 内容。
- `_sanitizeContext` 等既有 prompt 护栏与本特性无关，未触碰。
- 503 文案不变：仍 `MSG_DQ_GEN_LLM_PARSE_ERROR`，不泄漏 LLM 原文。

## 8. 部署验证

2026-09-10 已部署 qa-backend 容器（`docker cp backend/app/. qa-backend:/app/app/` +
`docker restart qa-backend`）。

容器内核对：

```
$ docker exec qa-backend grep -n "_JSON_FENCE_RE\|_stripJsonFence" \
    /app/app/services/data_quality_rule_llm_service.py
42:# 与 nl2sql_service._JSON_FENCE_RE 同语义，本服务独立一份避免跨服务耦合。
43:_JSON_FENCE_RE = re.compile(...)
46:def _stripJsonFence(content: str) -> str:
53:    match = _JSON_FENCE_RE.search(content)
127:    # _stripJsonFence 优先剥 ```json/``` fence，否则原样
129:    payload_text = _stripJsonFence(content)
```

真机冒烟（POST /api/v1/data-quality/rules/generate/parse-descriptions，
`{"classId":1,"modelId":1}`，admin 角色）→ **HTTP 200，2 条 suggestions**：

```json
{
  "suggestions": [
    {"propertyId":1,"propertyName":"SUPPLIER_KEY","kind":"allowed_values",
     "values":["ACTIVE","INACTIVE","SUSPENDED"],"confidence":0.95,
     "rationale":"description 明确说明 SUPPLIER_KEY 表示供应商状态，..."},
    {"propertyId":5,"propertyName":"OTD_RATE_12M","kind":"allowed_values",
     "values":null,"confidence":0.85,
     "rationale":"... 连续数值范围约束，不适合枚举 allowed_values ..."}
  ],
  "persistedPropertyIds": []
}
```

升级前：同样请求 → 503 「AI 返回格式无法解析」。
升级后：200 + 真实 LLM 推理结果。fence-stripping 在生产 deepseek-chat
链路生效。

## 9. 已知遗留

- DQ 侧 description 字段空时 LLM 仍可能产出空 suggestions 列表（不算
  bug，本就是 advisory）；建议运营侧对所有 ontology_property 维护 description。
- `_JSON_FENCE_RE` 与 `nl2sql_service._JSON_FENCE_RE` 同语义但独立维护，
  故意不复用以避免跨服务耦合。后续如出现 fence 形态变异，需两侧同步
  修订。

## 10. 关联

- 同根因历史修复：Harness/wiki/nl2sql-engine.md 中
  `parseSqlFromResponse` 的 fence 处理（NL2SQL 侧已落地）。
- Wiki 可补 Harness/wiki/data-quality.md 关于 parse-descriptions
  「LLM 输出格式容错」小节（如尚未收录）。