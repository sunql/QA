# 变更：chore-l3-deadcode-and-prior-cte-contract

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：评估剩余项批次 **Phase B**（M5 + M8 合并一份 —— 同源：`WITH WITH` 地雷的两端）
- **状态**：done
- **关联变更**：同批 [fix-chat-disconnect-persistence](../fix-chat-disconnect-persistence/summary.md)、[fix-llm-transient-retry](../fix-llm-transient-retry/summary.md)、[fix-embedding-provider-type-guard](../fix-embedding-provider-type-guard/summary.md)、[fix-milvus-list-all-pagination](../fix-milvus-list-all-pagination/summary.md)、[test-kpi-match-cache-ordering](../test-kpi-match-cache-ordering/summary.md)
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 **M5** + §2.3 **M8** + §3 P2 第 11 项（评估日期 2026-09-25；行号更正见 §5）
- **commit**：`72d34a1`

---

## 1. 需求

两件同源的事：

1. **M5 死代码**：`ChatService._executeChainedSteps` / `_executeSingleChainedStep`（连同其上的 `# L3 CTE 串联引擎（Task 3.3）` 段落）**无人调用** —— L3 多步串联最终走的是 `_streamMultiStep` / 非流式多步路径，这两个函数从未被执行过，却带着完整实现与专有导入，让读者以为存在第二套串联引擎（改到它上面即为静默失效）。
2. **M8 真缺陷（比评估原文更严重）**：`WITH WITH` 地雷。`render_prior_cte()`（`chained_step_plan.py:64`）产出**自带前导 `WITH`** 的片段，而消费者 `nl2sql_service.generateSql` 又拼 `f"WITH {prior_cte}\n{sql}"` ⇒ 拼出 `WITH WITH ...`。**守卫放行**它：`_assert_read_only` 只看首个 token，而 `WITH` 在白名单里 ⇒ 到库侧才报语法错，又被 `chat_service` 的宽 `except Exception` 吞成 `success=False`（用户看到的是「无法回答」，日志里没有可操作的根因）。

用户口径（binding）：**删引擎，保留 `prior_cte` 能力并修契约** —— 不是把能力一起删掉。

**验收标准**：① 两个引擎函数及其专有导入、专有测试全部删除，全树零引用；② `prior_cte` 能力保留，契约钉死为 **WITH-less 片段**（唯一合法形态）；③ **`WITH WITH` 不可能再被构造出来**，带 `WITH` 的入参在入口被拒并给可操作消息；④ 保留的活代码（`_summarizeStepData`、`chained_step_plan.py`）不受影响；⑤ 相关文档漂移同批修正。

> ⚠️ **如实标注一处后果（事后复核，写入评估文档 §15 残差）**：被删的引擎是 `prior_cte` 形参**唯一的生产调用方**
> ⇒ 删除后该能力**当前无生产调用者**，只被契约测试 `test_prior_cte_contract.py` 驱动。
> 保留它仍是用户口径（「删引擎、保能力」），但**若长期不接线，应连同 `app/domain/chained_step_plan.py`
> 一并评估删除** —— 否则等于把 M5 删掉的死代码换了个位置留着。本行不改变本批的任何实现与验收结论。

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 只删死代码，`prior_cte` 契约不动 | 否决：**地雷仍在**。删掉引擎后 `prior_cte` 仍有两个潜在消费者（LLM 生成路径 / 上层显式传入），下一个消费者会原样复现 `WITH WITH` |
| B | 删死代码 + 保留 `prior_cte` 且**只改 `render_prior_cte` 去掉 `WITH`** | 否决（半修）：产出端修好，**入口仍不校验** —— 手写/LLM 传入带 `WITH` 的片段照样炸到库侧 |
| C | **删死代码 + 产出端去 `WITH` + 入口显式拒绝自带 `WITH` + 只读校验按拼接后形态做**（选定） | 两端同时钉死；非法形态在**进库前**被拒，错误类型是 `Nl2SqlError`（可被上层按语义处理，而不是库侧语法错被吞） |
| D | 顺手把 `prior_cte` 能力也删掉 | 否决：用户口径明确「保留 `prior_cte` 能力并修契约」；且它是多步串联的真实能力，删了是功能倒退 |

### 只读校验为什么不能直接复用 `_assert_read_only(prior_cte)`

WITH-less 片段的首个 token 是 **CTE 别名**（如 `c0`），不在白名单里 ⇒ 直接校验必然误拒合法片段。故 `_assertPriorCteSafe` 按**拼接后的真实形态**校验：`WITH <片段> SELECT 1` —— 校验的对象与最终执行的对象一致。

### 删除边界（用户口径）

- **删**：`_executeChainedSteps`、`_executeSingleChainedStep`、其上方段落注释、`chat_service.py` 中对 `ChainedStep` / `StepResult` / `render_prior_cte` 的导入、整份 `app/tests/services/test_l3_chained_steps.py`（211 行）。
- **保留**：`_summarizeStepData`（**活代码**，3 处在用）、`app/domain/chained_step_plan.py`（`render_prior_cte` 保留为纯函数工具，`ChainedStep` / `StepResult` 数据模型保留）。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本。

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `render_prior_cte(steps, current_index)`（`app/domain/chained_step_plan.py:64`） | 返回值由 `"WITH " + ", ".join(parts)` 改为 `", ".join(parts)` —— **WITH-less 片段** `cte1 AS (body), cte2 AS (body)`；`current_index <= 0` 返回空串 |
| `Nl2SqlService.generateSql(..., prior_cte=None)` | 入口新增 `_assertPriorCteSafe(prior_cte)`（`:2142`）；拼装处保持**恰好一个**前导 `WITH`（`:2195` `f"WITH {prior_cte}\n{sql}"`） |
| `_assertPriorCteSafe(prior_cte)`（`:488`，新增） | ① 拒绝自带前导 `WITH` 的入参（给可操作消息，不再放过）；② 按 `WITH <片段> SELECT 1` 做**只读**校验；③ 失败一律 `Nl2SqlError`（携带 detail），不是库侧语法错 |
| `_renderPriorCtePart` docstring（`:461`） | 删除「或 `WITH cte1 AS (...)`」的自相矛盾表述，只留唯一形态 |
| **明确不支持** | `prior_cte` 仍按「只读 CTE」约束（业务查询只允许只读 SELECT 的项目铁律）；不因为它是「片段」而放松 |

## 5. 实现要点

| 位置 | 改动 |
|---|---|
| `app/services/chat_service.py`（净 **-99 行**） | 删除 `_executeChainedSteps`、`_executeSingleChainedStep`、`# L3 CTE 串联引擎（Task 3.3）` 段注释、3 个专有导入 |
| `app/domain/chained_step_plan.py`（+24/-…） | `render_prior_cte` 去掉前导 `WITH`；模块头写清「自带 `WITH` 是缺陷、不是风格选择」 |
| `app/services/nl2sql_service.py`（+74/-…） | 新增 `_assertPriorCteSafe` + 入口调用 + `_renderPriorCtePart` docstring 更正 + `generateSql` docstring 写明「本方法补上唯一一个前导 `WITH`」 |
| `app/tests/services/test_l3_chained_steps.py` | **整份删除**（-211 行；与引擎同源，无其他被删符号的引用） |
| `app/tests/unit/test_prior_cte_contract.py`（新增 221 行 / 9 例） | 契约守卫：恰好一个前导 `WITH`（正则 + `not "WITH WITH"`）、带 `WITH` 入参被拒、非只读片段被拒、`current_index=0` 空片段 |

**行号更正**（评估文档原文 `chat_service.py:1935-2023`、`nl2sql_service.py:414-416` 已过期）：实为 `chat_service.py:2455-2495` / `2498-2543`（删除前）、`nl2sql_service.py` 的 `generateSql` docstring + `_renderPriorCtePart:461`。本批已写回评估文档 §2.3。

## 6. 测试

- **单元 `test_prior_cte_contract.py` 9 passed**（新增，本批 RED 先行）；
- **全树零引用证据**（容器内探针，真实模块）：
  ```
  PASS  M5 ChatService._executeChainedSteps 已删
  PASS  M5 ChatService._executeSingleChainedStep 已删
  PASS  M5 _summarizeStepData 保留（活代码）
  PASS  M8 render_prior_cte 无前导 WITH  | 'c0 AS (SELECT 1 AS x)'
  PASS  M8 拼接后恰好一个 WITH  | WITH c0 AS (SELECT 1 AS x) SELECT * FROM c0
  PASS  M8 拼接后无 'WITH WITH'
  PASS  M8 current_index=0 → 空片段
  PASS  M8 自带 WITH 的 prior_cte 被拒  | Nl2SqlError
  PASS  M8 合法 WITH-less 片段放行
  PASS  M8 非只读片段被拒  | Nl2SqlError
  ```
- **删除的可证性**：删前以 AST/grep 复验「全树仅测试引用」（活代码 `_summarizeStepData` 有 3 个调用点，明确保留）；删后跑 `test_no_duplicate_methods.py`（同批前序建立的常驻守卫）与全量 unit 无新增失败；
- **探针自身的断言缺陷（首跑 1 FAIL，如实记录）**：`M9 16384 字面量已收敛` 与 M5/M8 无关的另两条写在各自 SSOT；本条相关的同根因缺陷是「**文档提到旧写法 ≠ 代码还在用旧写法**」（详见 [fix-milvus-list-all-pagination](../fix-milvus-list-all-pagination/summary.md) §6）。
- 覆盖率：删除 + 新增契约测试，不引入新的未覆盖生产分支。

## 7. 安全审查

**未触发**（无认证/密钥/用户输入新通道）。三点已自查，且其中第 2 点属**安全增强**：

- `_assertPriorCteSafe` **没有**放松只读约束：它以拼接后的真实形态走既有只读校验，`prior_cte` 里的写操作（如 `c0 AS (DELETE FROM t)`）被拒（探针已验证）；
- **原 `WITH WITH` 路径是一处校验绕过面**：`_assert_read_only` 只看首 token，`WITH` 在白名单 ⇒ 片段内的内容过去从未真正被只读校验过。本项把校验对象改成了「最终要执行的那段 SQL」，属**收紧**；
- 拒绝消息只含原因与形态要求，不含数据库结构或内部路径。

## 8. 部署验证（2026-09-27）

- **部署**：`./scripts/deploy_backend.sh`（含 `alembic/`）；仓库 ↔ 容器 md5 `app/domain/chained_step_plan.py`、`app/services/chat_service.py`、`app/services/nl2sql_service.py` **MATCH**（本批 22/22 现存文件 MATCH；唯一 DIFF = 本项**故意删除**的 `app/tests/services/test_l3_chained_steps.py` 仍留在容器里 —— 部署脚本不删文件，测试文件不参与运行时，已如实登记）；
- **容器内真机探针**：M5 3 项 + M8 7 项全 PASS（输出见 §6，跑在部署物真实模块上，非替身）；
- **测试**：全量 unit+services **2 failed, 2522 passed, 1 skipped**（两条为**预存**失败 —— L3 删除与它们无关，用 `git worktree add --detach` 在基线复现判别，delta = 0）；集成切片 120 passed + 3 例环境耦合（导出 `DATABASE_URL` 后 3/3 通过）；
- **静态检查**：本批 22 文件 ruff 与基线逐行一致（唯一 F841 为基线既有）；新增 `test_prior_cte_contract.py` `All checks passed`；
- **网关**：`/api/v1/health` 直连 8000 与经 nginx 5173 均 200。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.3 M5、M8（标 ✅，含行号更正）、§3 P2 第 11 项（划掉）、§2.5 L3 漂移行更正、§15 批次记录
- 文档漂移同批修正（docs commit）：`Harness/wiki/nl2sql-engine.md`（L3 段 + **`multi_step_plan.py` 不存在的引用**更正 + `prior_cte` 契约）、`Harness/wiki/architecture.md`（4 层路由现状）
- 代码契约：`app/domain/chained_step_plan.py`（产出端）、`app/services/nl2sql_service.py::_assertPriorCteSafe`（入口端）、`app/tests/unit/test_prior_cte_contract.py`
- 常驻守卫：`app/tests/unit/test_no_duplicate_methods.py`（前批建立，本批复跑）
- 规则：根 `CLAUDE.md` 核心约束 #2（只读 SELECT + SQL Guard）、#5（小文件/清理）、`Harness/rules/编码规范.md`
- Memory：`qa-system-*` 新增 M5/M8 条目（`prior_cte` 契约 = WITH-less + 「`WITH WITH` 地雷与首 token 白名单绕过」）
- 同批：`../fix-chat-disconnect-persistence/`、`../fix-llm-transient-retry/`、`../fix-embedding-provider-type-guard/`、`../fix-milvus-list-all-pagination/`、`../test-kpi-match-cache-ordering/`

## SSOT 校验清单

- [x] 第 1 段 需求：M5 死代码 + M8 `WITH WITH` 地雷（含「守卫放行 → 库侧报错 → 被宽 except 吞掉」的完整链路）+ 用户口径 + 5 条验收标准
- [x] 第 2 段 4 个候选方案（含「只删不动契约」与「半修产出端」两种被否决形态）+ 只读校验为何不能直接复用 `_assert_read_only` + 删除边界
- [x] 第 3 段 无迁移
- [x] 第 4 段 接口契约：产出端去 `WITH` + 入口拒绝自带 `WITH` + 恰好一个前导 `WITH` + 只读约束不放松 + docstring 矛盾表述删除
- [x] 第 5 段 实现要点逐位置表（含净行数）+ 评估文档行号更正
- [x] 第 6 段 测试：9 例 + 10 项容器探针实录 + 删除可证性（活代码保留的复验）+ 探针缺陷的同根因登记
- [x] 第 7 段 安全审查：未触发 + **本项含一处安全增强**（首 token 白名单绕过面被收紧）
- [x] 第 8 段 部署验证：md5 MATCH（含**故意删除文件仍留容器**的如实登记）+ 探针 + 全量测试与预存失败判别 + ruff delta 0 + 网关 200
- [x] 第 9 段 跨文件链接（评估文档 / 漂移文档 / 代码契约 / 常驻守卫 / 规则 / Memory / 同批）
