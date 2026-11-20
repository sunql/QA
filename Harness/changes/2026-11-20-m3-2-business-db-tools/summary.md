# 变更：M3-2 业务库只读查询工具（purchase_history_query）

- 日期：2026-11-20　分支：`feat/procurement-knowledge-documents`（堆叠累积）

## 内容
- **`purchase_history_query`**（agent_tools_procurement）：supplierKey 必填 +
  materialKey?/monthsBack(1..36)/limit(≤50) ⇒ **两段确定性 SQL**（汇总 COUNT/SUM/
  MIN/MAX/AVG 价格区间 + 明细行）经 `assertReadonlySql`（SQL Guard）+
  `adapter.execute_read_only`（业务库只读）执行——**零 LLM 生成 SQL**（模板+
  字面量转义内联，adapter 无参数通道）；
- 列名来自本体映射（class 20 DWD_PURCHASE_ORDER_LINE：SUPPLIER_CODE/MATERIAL_CODE/
  PO_NO/ORDER_QTY/UNIT_PRICE/CREATION_DATE）；时间窗 `DATE 'yyyy-mm-dd'` 字面量
  （Oracle/PG 通吃）+ FETCH FIRST（Oracle 12c+/PG）；
- **AgentToolContext 扩展** `datasource`（可选）：编排/调用方绑定业务源；
  None ⇒ 回退默认活跃业务源；**停用源 fail-closed**（ConflictError 409，C 方案
  同语义——本堆叠支无 #164，#164 合并后切共享 `ensureDatasourceActive`）；
  handler 侧 resolve 后**再显式过一道闸**（纵深：monkeypatch/旁路注入也拦）；
- **runtime service** `run()` 加可选 `datasource` 参数透传 ctx（HTTP 层 datasourceId
  plumb 留待入口需要时补）；runner worker 路径 None ⇒ 回退默认源（登记行为）；
- **注册**：BUILTIN_HANDLERS + ARG_EXTRACTORS（chat 前奏抽 supplier key，复用
  extractSupplierAnyKey）+ **三处集合守卫同步**（第 4 个工具）。

## M3 剩余（更新）
- `quotation_query` **挂账**：报价↔供应商关联列不在本体映射内（class 55/31 均无
  SUPPLIER 列）⇒ 需先核源表物理列（A8 证据外的未映射列），另批；
- `procurement_policy_search`：待 ctx 扩 user（密级闸）；
- `quotation_compare`：比价规则（D3/M5）。

## 测试（10 用例 + 2 守卫文件同步）
builder SQL 形态 ×1（单语句/FETCH FIRST/时间窗字面量/物料条件缺省）+ handler
只读执行 ×1（fake adapter 两段 SQL + camelCase + 零 LLM）+ 停用源 fail-closed ×1
（含纵深路径）+ 缺 supplierKey 友好提示 ×1 + 注册/守卫 ×N。工具家族 36 passed；
ruff F821 为基线既有（runtime service 同款集合），净新增 0。
