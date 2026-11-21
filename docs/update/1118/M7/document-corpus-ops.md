# 制度/合同文档语料 —— 补充操作手册（I2-C 内容侧）

> 目的：让 `POST /procurement/knowledge/documents/search`（Q04 制度检索）与
> 知识查询的 DOCUMENT 通道有可检索内容。**已实测跑通**（见 §5）。

## 一、需要交什么

| 项 | 要求 |
|---|---|
| 格式 | **PDF / DOCX / TXT / MD**（单文件 ≤ 50MB） |
| 建议首批 | 10–15 份，覆盖：采购管理制度、询价/比价办法、供应商准入与考核、
合同模板与审批流程、质量协议、检验规范、黑名单/合规制度 |
| 不需要 | 扫描件无文字的 PDF（无文本层 ⇒ 抽不出 chunk）；excel 表格（建议转说明文档） |

## 二、三种导入方式（任选）

### 方式 A：前端「文档中心」（最省事）
登录 → 左侧菜单 **文档中心**（`/documents`）→ 上传 → 填类型/密级/生效日。

### 方式 B：HTTP 接口（单份，脚本友好）
```bash
# 取 token
TOKEN=$(curl -s -X POST http://<host>:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<账号>","password":"<口令>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['accessToken'])")

# 上传（参数全在 query 上，文件走 multipart）
curl -s -X POST "http://<host>:8000/api/v1/documents/upload?\
documentType=PURCHASE_SPEC&version=v1.0&owner=procurement&\
effectiveDate=2026-01-01&securityLevel=L1" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./采购询价管理办法.pdf;type=application/pdf"
```
成功返回：`{"document_id":"DOC-XXXXXXXXXXXX","chunks":N,"status":"ingested","storage_url":"s3://qa-knowledge-sources/..."}`

### 方式 C：批量（目录循环）
```bash
for f in ./制度/*.pdf ./制度/*.docx; do
  curl -s -X POST "http://<host>:8000/api/v1/documents/upload?documentType=PURCHASE_SPEC&securityLevel=L2&owner=procurement" \
    -H "Authorization: Bearer $TOKEN" -F "file=@$f" | head -c 200; echo
done
```

## 三、参数怎么填（决定「谁能查到」）

| 参数 | 取值 | 说明 |
|---|---|---|
| `documentType` | `PURCHASE_SPEC` 采购规范/制度 · `CONTRACT` 合同 · `SOP` 作业规范 · `SPEC` 技术规格 · `INSPECTION_SPEC` 检验规范 · `QUALITY_AGREEMENT` 质量协议 · `AUDIT_REPORT` · `8D_REPORT` · `REMEDIATION_REPORT` · `OTHER` | **制度类一律用 `PURCHASE_SPEC`**（前端下拉同款） |
| `securityLevel` | `L1` 公开 · `L2` 内部 · `L3` 机密 | **L2/L3 只有具备对应密级的用户能检索到**（`document_clearance` 过滤）——这是设计，不是故障 |
| `effectiveDate` | `YYYY-MM-DD` | 生效日：查询带 `effectiveAt` 时，**未来生效的文档会被过滤**；留空=未知日期（不会被日期过滤掉） |
| `owner` | 部门码，如 `procurement` | 归属部门（展示/治理用） |
| `version` | `v1.0` 等 | 版本号（同文件改版请改版本号并重新上传） |

## 四、导入后发生了什么

1. `document_catalog` 落一行（document_id / 类型 / 版本 / 密级 / 生效日 / status=ACTIVE）；
2. 切 chunk → 调 embedding → 写 **Milvus 文档集合**（知识检索从这里召回）；
3. 源文件存 **MinIO**（`qa-objects`，桶 `qa-knowledge-sources`）——审计可回溯原文。

⚠️ **重复上传同一文件会产生新的 `document_id`**（除非接口显式指定），需要清理旧版时：
先按 `document_id` 删除（Milvus + catalog），再考虑是否保留 source 文件。

## 五、验证（怎么确认导入生效）

```bash
# ① 库里看目录
docker exec qa-postgres psql -U qa_user -d qa_metadata \
  -c "SELECT document_id, document_type, security_level, effective_date FROM document_catalog ORDER BY created_time DESC LIMIT 5;"

# ② 走知识接口验检索（Q04 口径）
curl -s -X POST http://<host>:8000/api/v1/procurement/knowledge/documents/search \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"question":"询价必须邀请几家供应商","topK":3}'
```
**已实测结果**（2026-11-21，本地栈上传一份占位制度）：
```json
{"document_id":"DOC-7DF4B953ADD1","chunks":1,"status":"ingested"}
→ 检索命中 1 条：kind=DOCUMENT, source=LIVE,
  security={"level":"L1"}, sourceRef.documentId=DOC-7DF4B953ADD1
```

## 六、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 上传成功但检索不到 | ① 用户密级不够（L2/L3 文档）② 问句与正文词不重合 ③ embedding 服务离线（看后端日志） |
| `503 文档源文件留存失败` | MinIO 不可用（`qa-objects`）——稍后重试，不是文件问题 |
| `422` | 文件抽取失败（扫描件无文本层 / 不支持格式） |
| 检索到但 Q04 答案空 | 知识 Facade 的 DOCUMENT 通道走的是**同一份文档集合**；确认 `channels` 里带了 `RAG`/`DOCUMENT` |
