# 变更：feat-property-ref-normalize（复合形式 `业务名 (alias)` property 归一化）

- **日期**：2026-09-18
- **作者**：启琳（Claude Code）
- **状态**：✅ 完成。后端 186 例全绿（21 新 + 2 planToText + 163 既有），前端未受影响，tsc 0 错；真机 8 次 `curl /chat` 全 200，原 hit rate 0% 复合串失败场景修。

## 1. 问题

多轮 NL2SQL 偶发 LLM 把 schema 渲染格式 `供应商 (BPSNUM_0)` 原样抄作 `property_name`。`validatePlan` 按 `_classRefNames` 集合单 token 严格匹配，`"供应商 (BPSNUM_0)" in {供应商, BPSNUM_0, ...}` 永远 false → 整轮失败，报错"本体 schema 中不存在该属性（已比对全部类的业务名/别名/物理列）"。

### 4 层根因（前序探测）

1. **schema 渲染诱导** — `nl2sql_service.buildSchemaText:1058-1061` 把 `供应商 (BPSNUM_0): varchar (column=BPSNUM_0)` 整段呈现给 LLM
2. **校验缺归一化** — `_classRefNames` 只含单 token，复合串严格 false
3. **state 持久化未过滤** — `chat_service._saveQueryState:3653-3655` 原样写 SessionQueryState（真库已观察到 `interpretation` 被污染成 `收货数量（QTYUOM_0）` 中文括号）
4. **planToText 回灌** — `query_plan.planToText:218` 把 plan 原样注入下一轮 prompt，LLM 学回去

### 用户场景复现（hot-deploy 前）

同问句连续 5 次跑 `curl /chat`，第 3 次 LLM 输出复合串 → 报错 `选中的属性 供应商 (BPSNUM_0) 不属于选定的任何类`，全链路失败。

## 2. 关键决策

| 决策 | 结论 |
|---|---|
| 修复范围 | A+D+B 组合（validatePlan 容错 + from_dict 后归一化 + planToText 拆分渲染） |
| 多轮 state 已有残留 | **不清理**——用户决定"后面专门考虑"，本特性只处理新进入的 plan |
| schema 渲染格式（C 选项） | **不动**——prompt 工程收益不稳，留待后续 prompt 优化 |
| 归一化目标 token | **业务名优先**（拆括号前半段），回退 alias；都不在则原样保留（原有错误链路接管，不掩盖真正未知属性） |
| 复合形式识别 | 仅匹配一对 ASCII `()`；中文 `（）`、嵌套 `(( ))`、空括号 `()` 均原样保留（不在本特性改造范围，避免误伤其他含义） |
| helper 复用方式 | nl2sql_service 用 `_classRefNames` 已有合法引用集合（含限定形式）；query_plan 复刻一份 regex（同口径），避免反向依赖形成循环导入 |
| 调用时机 | `generateValidatedPlan` 内 `generateQueryPlan` 返回后立即归一化（parse→validate 之间）；planToText 在 prompt 渲染时拆分 |

## 3. 改动清单

### `backend/app/services/nl2sql_service.py`
- **新增** `_COMPOUND_REF_RE` 正则（行 130）：`r"^(.*?)\s*\(([^()]+)\)\s*$"`
- **新增** `_splitCompoundRef(prop)`（行 132-148）：拆 `'name (alias)'` → `(name, alias)`；非字符串 / 无括号原样返回
- **新增** `_normalizePlanProperties(plan, classes)`（行 150-200）：把 plan 里所有 prop 字段（selectedProperties / aggregations[*].property / groupBy / partitionBy / sortBy[*].property / joins[*].columns）中的复合形式替换成首个合法 token；返回新 plan（frozen 不可变要求 `dataclasses.replace`）
- **新增** 调用点 `generateValidatedPlan` 内（行 1532-1535）：`generateQueryPlan` 返回后立即归一化，validatePlan 看到的是清洗后的 plan

### `backend/app/domain/query_plan.py`
- **新增** `_COMPOUND_REF_RE` 正则（同上，行 24）
- **新增** `_stripCompoundRef(prop)`（行 27-44）：取拆出的业务名，无 ontology 上下文做合法性校验
- **改** `planToText` 全部 prop 字段（行 218、220、222、226、234）：`_aggText` / `_sortText` / `selectedProperties` / `groupBy` / `partitionBy` 经 `_stripCompoundRef` 拆分后再渲染

### 测试
- **新增** `backend/app/tests/unit/test_property_ref_normalize.py`（21 例）：
  - `TestSplitCompoundRef`（8 例）：标准 / 仅括号 / 仅业务名 / 空字符串 / 双侧空格 / 中文括号 / 非字符串 / 嵌套括号
  - `TestNormalizePlanProperties`（11 例）：业务名命中 / alias 回退 / 双不命中 / 单 token 合法 / 中文括号不动 / aggregations / groupBy / partitionBy / sortBy / joins / 空 classes / 返回新对象
  - `TestValidatePlanAfterNormalize`（2 例）：复合串 plan 归一化后 validatePlan 通过 + 真正未知属性仍报错
- **新增** `backend/app/tests/unit/test_query_plan.py`（2 例）：planToText 拆分 selectedProperties + aggregations + sortBy 三处

## 4. 设计要点

**为什么不改 schema 渲染**：删 `供应商 (BPSNUM_0)` 的括号会损失 LLM 急需的 alias 提示；保留括号又会持续诱导 LLM 直接抄。归一化是事后兜底，最稳。

**为什么不改 `_classRefNames` 让它接受复合形式**：校验函数继续严格匹配（语义清晰，"未在本体中"vs"在但形态不对"），归一化集中在 parse 出口一次完成，两层各司其职。

**为什么不直接改 `_propertyOwnerHint` 错误文案**：归一化已经让复合串走通，原错误文案对真正未知的属性仍生效，不掩盖问题。

**不可变性**：所有修改经 `dataclasses.replace` 返回新对象，输入 plan 与字段不变（CLAUDE.md 强制规则）。

## 5. 验证

### 单测
```bash
cd backend
export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
python -m pytest app/tests/unit/test_property_ref_normalize.py \
                app/tests/unit/test_query_plan.py \
                app/tests/unit/test_query_plan_validation.py \
                app/tests/unit/test_nl2sql_service.py \
                app/tests/integration/test_receiptdetail_qty_alias.py
# 186 passed in 43s
```

### 真机复现（hot-deploy 后）
- `curl /chat` 同问句 8 次：8/8 全 200，answer_len 524-587，data_rows 9 稳定
- state 注入复合串（模拟历史污染）：5/5 全 200 —— planToText 在 prompt 渲染时已拆复合形式，LLM 不再生成复合串（治源）
- 单测 `TestValidatePlanAfterNormalize.test_compound_form_selectable_properties_pass`：构造完整复合串 plan（含 aggregations / groupBy / partitionBy / sortBy / formula），归一化后 validatePlan 必须返回空 issues

## 6. 部署

按 [[qa-system-stale-container-deploy]]：本特性已 hot-deploy 到 `qa-backend`（脚本一次性灌 `app/` + `scripts/` + `alembic/` + 快照 + 重启）。

⚠️ **docker cp 是临时的**：用户提交后请固化：`docker compose build --no-cache backend && docker compose up -d backend`。本特性无需 alembic 迁移（仅 helper + 渲染路径），不影响启动链路。

## 7. 不做（明示）

- **多轮已有 SessionQueryState 残留清洗** — 用户决定"后面专门考虑"。若要清理，可加一次性脚本：`UPDATE session_query_state SET last_plan = ...normalized... WHERE ...`。建议 P1。
- **改 schema 渲染格式（C 选项）** — prompt 工程收益不稳，留待后续。
- **`interpretation` 字段归一化** — 该字段是 LLM 自由文本，无法用 schema 规则校验；只能约束 prompt 让 LLM 别再写括号。本特性不动。
- **检测 + 告警**（prod 命中复合串时报警） — 当前 planToText 已彻底剥离，prod 命中概率极低；监控可在下一步加。

## 8. 相关

- [[qa-system-supplier-360-ads]]：原 27 类绑定 THBI DWD，property_alias 列已被 alias 填充，本特性依赖该 schema 数据完整
- [[qa-system-cross-class-property-hint]]：`_propertyOwnerHint` 同文件相邻，思路一致——校验错误带可操作指引，本特性保留此特性
- [[qa-system-stale-container-deploy]]：hot-deploy 与固化命令 SSOT