# 变更：M2′-2 NONE 指标技术形态回填（G1 答案背书，逐条裁决）

- 日期：2026-11-20　分支：`feat/m2p2-none-metrics`（off `feat/procurement-m0`）
- 载体：`backend/scripts/m2p2_backfill_none_metrics.py`（M2′-1 同款护栏 + 自愈式幂等）
- 口径权威来源：`docs/update/1118/M0/answers/kpi-slots-filled.json`（A9–A12 + 源侧实测 + 冲突/降级裁决）

## 结果（dev 5433 实跑）

**1 条回填翻转成功**：`KPI_SUPPLIER_DEFECT_RATE`（id 11，A9.1–A9.4 confirmed）
- 分子 `SUM(ARR_QTY - QUALIFIED_QTY)` / 分母 `SUM(ARR_QTY)`，同类 6（DWD_ARRIVAL_ORDER_DTL，
  实测 81,754 行/列 100% 填充）+ source 行 NUMERATOR/DENOMINATOR 双角色 + time_column=ARR_DATE
- **编译门 ok（len=101）→ formula 自愈为编译 SQL → NONE→COMPILED → 治理列回填**
  （kpi_catalog numerator/denominator_columns + grain_keys——层 2 对账的签字基准）
- **终态：Tier A eligible=True（运行时同路径 probe 实证）**；域重发布 `v-m2p2-defect-rate-1` ACTIVE

**3 条 skip-blocked（G1 裁决即脚本行为，不自由发挥）**：
| 指标 | 裁决 |
|---|---|
| FPY（id 12） | 分母单位待回问 C-05/Q6（A10.2 行数 vs A10.3 与 A9 互为补集矛盾） |
| MAINTENANCE_RATIO（id 10） | R-08 降级『准入台账只读展示』+ 停用列不存在 C-14/Q9 |
| NCR_RATE（id 13） | A11.1 治理侧摘除首期清单，保持 DRAFT（不删） |

## 过程坑（四连，全部入档）
1. **NONE→COMPILED 会 Tier B**：层 2 治理列为空 ⇒ 对账矛盾 ⇒ 必须连同 kpi_catalog
   治理列（num/den/grain_keys）一起回填；
2. **层 1 对声明文本 formula 必误判**：口径声明文本里的 token（FAIL/DWD_INCOMING_INSPECTION）
   被当 SQL 标识符 ⇒ `structured_column_missing_in_formula` ⇒ **可执行指标的 formula
   应是编译 SQL**（自愈回填，语义一致由 A9 答案背书）；
3. `metric_name` 带中文后缀 ⇒ 按 kpi_code 前缀匹配；
4. 幂等路径的写入也要计入 commit tracking（治理列回填在 `[ok]` 路径，漏记 ⇒ 写了不提交）。

## M2′ 终态（dev 库）
**4 COMPILED / 5 ESCAPE_HATCH / 3 NONE**；Tier A eligible 8 条（3 M2′-1 + 1 M2′-2 + 4 原有
ESCAPE_HATCH）；能力边界 5 条挂账（跨表表达式 ×4 + FILTER 源 ×1，见 M2′-1 记录）；
业务阻塞 3 条挂账（FPY/Maintenance/NCR，回问题号 C-05/C-14/Q6/Q9）。
