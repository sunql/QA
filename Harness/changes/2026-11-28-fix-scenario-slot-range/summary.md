# 变更：2026-11-28-fix-scenario-slot-range（场景槽位范围约束 + 发起前校验修复）

- **日期**：2026-11-28
- **作者**：Claude
- **Phase**：采购 Copilot 场景（P4/P5）
- **状态**：**代码完成；生产上线需执行一次 seed（见 §5 上线必做动作）**
- **关联变更**：commit `4caec807`（首版：场景槽位范围约束 + 发起前校验）、
  本批 commit（契约级硬约束 + seed 定向补丁 + 前端提示修正）
- **迁移版本**：无（`slot_schema` 是 JSONB 列，改内容不需 DDL）
- **缺陷**：采购 Copilot → 到货跟踪场景，高级参数「周期期序」填 `1`
  ⇒ **查询了所有供应商**（用户实测本机复现）

---

## 1. 缺陷现象 + 根因

### 1.1 现象

采购 Copilot → 「到货跟踪」场景 → 展开高级参数 → 「周期期序」填 `1` → 发起 →
返回的结果里**所有供应商**都被列出来了（用户只想要某一个供应商的到货情况）。

### 1.2 根因链（4 环，缺一不可）

1. **语义**：`periodIndex`（周期期序）语义是 `0=本期`、`-1=上期`，**正数非法**
   —— 工具契约 `delivery_tracking_query._resolveAnchor` 明确
   「periodIndex 不能为正数（未结束的周期不可查询）」；
2. **触发条件**：前端 number 槽位控件**无范围校验** ⇒ 用户能填正数；
3. **放大器**：工具报错后，编排 runner 记工具步 `FAILED` 但**不终止 run**，
   回主循环让 **LLM 重新规划**；
4. **丢槽位**：场景槽位只拼进**问题文本**（`renderScenarioQuestion`），
   没有「槽位 → 工具参数」的强制注入 ⇒ LLM 重规划时丢了 `supplierKeys`
   ⇒ 工具拿不到供应商过滤条件 ⇒ 查了所有供应商。

即：**「非法参数」被「runner 不终止 + 槽位只进文本」放大成「静默的错误结果」**。

### 1.3 首版（commit `4caec807`）的修复与**遗留缺口**

首版做了两层防线：

- **A 防线（体验层）**：`slot_schema` 声明 `minimum` / `maximum`，
  前端 `InputNumber` 按此限制输入；
- **B 防线（正确性层）**：发起编排**前**校验槽位值 ⇒ 非法直接 422，
  不进 LLM、不产生错结果。

**遗留缺口（Critical）**：`seed_copilot_scenarios` 用
`pg_insert(...).on_conflict_do_nothing(index_elements=["scenario_code"])`，
即「**行不存在才 INSERT，已存在不覆盖任何字段**」⇒ **已部署库的存量场景行
拿不到新增的 `minimum` / `maximum`**：

- A 防线（前端读 DB 的 `slotSchema`）在生产**不生效**；
- B 防线里「通用范围规则」也读 DB 的 min/max ⇒ 读不到就 `continue`
  ⇒ **范围校验在生产也放行** ⇒ 用户的原缺陷在生产原样存在。

首版的 `Harness/changes` 未记录此缺口 ⇒ **不要把该缺陷当成已闭环**。

## 2. 本次修复：两层防线（契约层立即生效 + 配置层补齐）

### 2.1 B 防线内部拆成「配置层 + 契约层」

| 层 | 规则来源 | 真值位置 | 生产是否立即生效 |
|---|---|---|---|
| 配置层（通用） | `slot_schema[].minimum` / `maximum` | **DB**（可被管理员改、可过期） | ❌ 依赖 seed 补丁 |
| **契约层（硬约束）** | `periodIndex ≤ 0`、`anchorDay ∈ 1..28` | **代码**（`copilot_scenario_slot_validation.py`） | ✅ **立即生效** |

契约层的存在理由：**范围规则的真值应在代码里**（工具契约），场景配置里的
min/max 只是给前端的提示 —— 否则配置一过期（seed 没跑、库被改），防线就跟着失效。
契约层只对**绑定了 `DELIVERY_TRACKING_AGENT`** 的场景生效，且只对该场景
**声明了**对应槽位时生效（不误报）。

⇒ **即使 seed 补丁还没在生产跑，用户的原缺陷也已修好**（422 而非错结果）。

### 2.2 A 防线补齐：seed 定向字段补丁

新增 `SLOT_FIELD_PATCHES`（按 `scenario_code` → `slot_key` → 要合并的字段）
+ `_patchSlotFields(session)`：upsert 之后，**只**把命中的槽位**合并**
这些字段，保留该槽位其它已有字段（`label` / `type` / `conflictKeywords` /
`hint` …）；**管理员自加的槽位不动**、**补丁未命中的槽位不动**、
**不新增槽位**。运维可从 stdout 的 `patched <code>: merged slot fields for [...]`
确认补丁命中了哪些槽位。

沿用本仓既有先例（`RETIRED_SLOT_KEYS` 定向清理）：seed 不整体 upsert 场景配置
（会覆盖管理员对 template / chips 的定制）。

### 2.3 前端提示修正（3 个次生问题）

| 问题 | 现象 | 修法 |
|---|---|---|
| 越界提示残留 | `rc-input-number@9.5.0` 越界时不触发 `onChange`（值进不来 ✅），但**失焦**时把 `1` 夹到 `0` 并 `onChange(0)` ⇒ 值已合法，红色「必须在 -12 ~ 0 之间」却**永久留在屏幕上** | `onChange` 里**同步重算**提示状态 + `onBlur` 清标志位 |
| 下界丢失 | 通用 number 槽位的硬编码 `min={0}` 改成 `min={range.min}` ⇒ DB 未打补丁时 `range.min` 为 `undefined` ⇒ **负数可入槽位**（旧代码挡住） | 下界取 `range.min ?? 0` |
| 默认值未夹取 | `Number(values[slot.key] ?? 0)`：声明 `minimum > 0` 的槽位页面一进来就显示 `0` 并可能弹越界提示，而 `0` 其实**从未提交** | 默认值按范围夹取（`clampToRange`）；**未提交过的默认值不参与越界判定** |

## 3. 其它一并修的

- **min/max 类型硬化**：配置里 `minimum` / `maximum` 直接取 JSONB 原值比较 ⇒
  管理员写成字符串 `"0"` 会 `TypeError` ⇒ **500 而非 422**。现在比较前统一过
  `_asNumber`；非数值（None / 不可解析）⇒ **跳过该侧比较**（不 500）。
- **成对校验不再误报**：成对校验原按 `agentCode == DELIVERY_TRACKING_AGENT`
  判定 ⇒ 若将来有场景绑该 Agent 却**没声明** `anchorDay` 槽位，仍会报
  「必须与 anchorDay 一起使用」（该场景本就无此槽位 ⇒ 误报）。现在
  **仅当场景声明了 `anchorDay`（且 `periodIndex`）槽位时**才做成对校验。

## 4. 改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/services/copilot_scenario_slot_validation.py` | 新增**契约层**（`periodIndex ≤ 0`、`anchorDay 1..28`）；min/max 过 `_asNumber`；成对校验按槽位声明收窄 |
| `backend/scripts/seed_copilot_scenarios.py` | 新增 `SLOT_FIELD_PATCHES` + `_patchSlotFields()`（定向合并，不整体 upsert） |
| `backend/app/services/procurement_copilot.py` | 接线点不变（`validateScenarioSlotValues` 在 `createRun` 之前调用） |
| `frontend/src/components/copilot/ScenarioAdvancedParams.tsx` | 提示状态随 `onChange`/`onBlur` 重算；下界兜底 `range.min ?? 0`；默认值按范围夹取 |
| `backend/app/tests/unit/test_copilot_scenario_slot_validation.py` | 契约层 / 类型硬化 / 成对校验适用范围 |
| `backend/app/tests/unit/test_copilot_scenario_seeds.py` | 补丁幂等、只改命中槽位、保留其它字段、不新增槽位、不碰未登记场景 |
| `backend/app/tests/integration/test_procurement_copilot_api.py` | 「DB 无 min/max 时 periodIndex=1 仍 422 且不建 run」 |

## 5. 上线必做动作 ⚠️

**代码上线 ≠ 防线完全生效**。契约层立即生效（A 防线前端限制 + 配置层范围
校验仍需 seed 补丁），因此必须在**已部署环境**跑一次：

```bash
cd backend
python -m scripts.seed_copilot_scenarios
```

预期 stdout 出现（已存在场景行拿不到 min/max 的库）：

```
patched delivery_tracking: merged slot fields for ['anchorDay', 'monthsBack', 'periodIndex']
patched pending_delivery: merged slot fields for ['minOverdueDays', 'monthsBack']
```

重复执行**幂等**（第二次无 `patched` 输出、无写库）。

## 6. 验收 SQL

确认 `delivery_tracking` 场景的 `periodIndex` 槽位已带范围：

```sql
SELECT scenario_code,
       slot -> 'periodIndex' ->> 'minimum' AS period_min,
       slot -> 'periodIndex' ->> 'maximum' AS period_max
FROM copilot_scenario,
     LATERAL jsonb_array_elements(slot_schema) AS slot
WHERE scenario_code = 'delivery_tracking'
  AND slot ->> 'key' = 'periodIndex';
```

期望：`period_min = -12`、`period_max = 0`（为 NULL ⇒ seed 未生效，重跑 §5）。

同口径可查 `anchorDay`（1..28）、`monthsBack`（1..36）与
`pending_delivery.minOverdueDays`（≥ 0）。

端到端验收：采购 Copilot → 到货跟踪 → 高级参数「周期期序」填 `1`
⇒ **应直接 422**（消息含「周期期序」），且**不产生编排 run**。

## 7. 残余风险（本批未治，列为待办）

1. **runner 对工具 FAILED 不终止 run** —— 本批**按约定不改**通用行为
   （`agent_orchestration_runner.py` 未动）。本批只在**场景槽位**这一入口
   拦住了已知非法参数；**其它契约违反仍可能复现同类错结果**：
   工具报错 → runner 记 FAILED 不终止 → LLM 重规划 → 槽位（只进问题文本、
   无强制注入）丢失 ⇒ 静默的错误结果。
   **根治方向（二选一或并做）**：
   (a) 编排层加「槽位 → 工具参数强制注入」（场景槽位直接映射为工具入参，
   不经 LLM 自由发挥）；(b) 工具步 `FAILED` 即终止 run 并把错误回灌。
2. **契约层只覆盖到货跟踪两个槽位**。其它工具的入参约束（如
   `pending_delivery_query` 的窗口）目前仍只有配置层（依赖 DB 的 min/max）。
   新增工具时应在 `copilot_scenario_slot_validation.py` 同步登记契约层规则。
3. **`minimum` / `maximum` 仍是可被管理员改的配置**。契约层是权威，配置层
   只负责「更严的前端提示」；管理员把 min/max 改宽**不会**放宽契约层。
4. **前端提示的边界**：`rc-input-number` 的越界值**不进**槽位值（不会发给
   后端），失焦时被夹回边界 ⇒ 用户可能看到「值被改了」；后端契约层仍是
   最终裁判。

## 8. 报告

- `.superpowers/sdd/2026-11-27-chase-export/slot-validation-report.md`
  （追加本批：审查发现项 ①~⑦ 的逐项处理）
