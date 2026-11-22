# 采购域试点签收摘要（G5）

- 生成时间：2026-11-22T12:26:05.704197+00:00
- 数据集版本：`pilot-2026-11`
- 发现源：1｜快照：1｜运行：7
- 覆盖表：DIM_SUPPLIER、DIM_IMATERIAL

## 评测结论

- **verdict**：`PASS`
- 候选数：18
- 问题数：16
- 证据完整率：1.0

| 候选类型 | 条数 |
|---|---|
| `CLASSIFICATION` | 2 |
| `METRIC` | 4 |
| `OBJECT` | 2 |
| `PROPERTY` | 9 |
| `RELATION` | 1 |

## 签收步骤（业务）

1. 打开同目录的 `candidates.csv`：逐条填 `businessVerdict`（采纳 / 驳回 / 待证）与 `businessNote`；
2. 打开 `findings.csv`：同样逐条判定；
3. **误合并复核**：本流程**不做自动合并**，候选只有被人工采纳并进入提案后才可能生效
   ⇒ 误合并率结构性为 0；如发现「候选把两个不同实体当成一个」，请在 `businessNote` 注明；
4. 统计候选**准确率 / 召回**：需要标注集（本 CSV 的 `businessVerdict` 即标注），
   签收后由管理员用 `recordEvaluation` 上报：
   `{"candidateAccuracy": …, "candidateRecall": …, "manualMinutes": …}`。

> ⚠️ CSV 含真实业务值，默认生成在仓库外（`/tmp/...`）；**不要提交进仓库**。
