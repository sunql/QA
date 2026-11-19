# M1-1.5 采购术语补齐（term_dictionary 目录化）

## 1. 背景与目标

V0.3 实施计划 1.5：「采购术语补齐（`term_dictionary`）：寻源/询价/报价/定标/到货/收货/
零库存供应商等」。术语字典**全文注入每次 NL2SQL 计划 prompt**（无分页无截断）⇒
词条就是系统的业务口径；此前靠人工后台录入（已漂移过一次：供货量改
DTL/`RCV_QTY_PUU`，见 `851d6049`）⇒ 本批把口径**目录化**为纯数据 + 幂等应用脚本。

约束：
- 迁移号 0159 已预留给 A4-0 ⇒ **不做数据迁移**，走目录 + 脚本；
- 问卷口径：C4.2 寻源/招标/询价「没有」；B3.1/D4 询价草稿 only；A1.2 准时以收货
  日期为准；A5.1 口径「目前不定，后续通过系统配置」；C3.3 权威价**空白**；
  零库存供应商 = `DIM_SUPPLIER.YPTHFLGM_0 = 2`（1=非零库存）。

## 2. 决策

- **决策 SSOT**：`backend/app/domain/procurement_term_catalog.py` —— frozen slotted
  `ProcurementTerm`（含 `hasSourceData` / `evidence`）+ `PROCUREMENT_TERMS`（10 条）
  + 纯函数（`termNames` / `sourceBackedTerms` / `unsourcedTerms` / `mappedClassNames` / `termSpec`）。
- **应用脚本**：`backend/scripts/apply_procurement_terms.py` —— 默认 dry-run；
  `--apply` 经 `TermDictionaryService.createTerm` 落库（映射存活校验不被绕过）；
  幂等（一致跳过）；**漂移报 exit 2 不静默覆盖**（term_dictionary 无 update 端点，
  覆盖会丢人工改动）。
- **三条硬规则**（单测 + 集成钉住）：
  1. 无源词条不许挂 `mappedClassName`（防「0 行 = 真的是 0」假答案）；
  2. 未确认口径显式写「未确认」（A5.1）；不编造已确认口径；
  3. 到货 ≠ 收货；指标口径以收货日期/数量为准。

## 3. 落地物

| 文件 | 说明 |
|---|---|
| `backend/app/domain/procurement_term_catalog.py`（新建） | 10 条术语目录 + 纯函数 |
| `backend/scripts/apply_procurement_terms.py`（新建） | 幂等应用脚本（dry-run 默认 / `--apply` / drift exit 2） |
| `backend/app/tests/unit/test_procurement_term_catalog.py`（新建） | 39 用例：覆盖度/唯一性/长度契约/无源不挂类/有源挂 Lineage B/evidence/immutable 反证 |
| `backend/app/tests/integration/test_term_dictionary_supply_qty.py`（追加 3 用例） | 目录 10 条过真实写入口 + 读回一致 + 属性校验反证（缺 YPTHFLGM_0 ⇒ 422） |
| `docs/update/1118/M1/M1-1.5-采购术语补齐.md`（新建） | 本批核对表 |

为什么集成用例**追加**进既有文件：CI `backend-integration` 按「排序后下标 mod 4」
切分 ⇒ 新建文件会重排分片 ⇒ 假 net-new（M1-1.3 已踩过）。

## 4. 实证

- 单测 39 passed；集成文件 12 passed（9 既有 + 3 新增）；既有 `test_term_dictionary.py` 7 passed；评审修复后定向 51 passed 重跑全绿。
- dev 5433 实证：dry-run created 10 / drifted 0 → `--apply` 落库 10 条
  （`created_by=ops-procurement-terms`）→ 二次运行 skipped 10 / exit 0（幂等）；
  `term_dictionary` 14 行（4 既有 + 10 新），无源词条 `mapped_*` 三列全空。
- ruff check / format 全绿。

## 5. 评审发现与处置

code-reviewer 审查（含实跑测试 + 脚本 smoke）：**0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW，APPROVE**。两条 LOW 均已修复：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | LOW | `_matchesExisting` 与 `_diffFields` 是同一字段清单的两份手维护拷贝，目录加字段时会各自漂移 | 收敛为唯一清单 `_fieldPairs`，match/diff 都从它推导；修复后 51 用例重跑全绿 |
| 2 | LOW | dry-run 把「待创建」印成 `created (N)`，CI 日志会误读成已写入 | 输出标签按模式区分：dry-run 印 `would-create (dry-run)`，`--apply` 印 `created` |

## 6. 遗留

- 权威价口径（C3.3）待业务确认 ⇒ 确认后改目录「报价」一条 + 重跑脚本（先裁决漂移）。
- M1-1.6 范围裁决落地（准则 H 落表 ⇒ CI 面 ⇒ merge-gate exit 6 仲裁）。
- M1-b 确认闸 blocked：C 问卷 C3.1–C3.8 空白 ⇒ 41 类绑定范围未确认。
