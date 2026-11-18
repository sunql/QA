# 变更：采购平台 V0.3 · M0.4 答案级评估器 + 首跑（A5）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M0 底座 / W1 轨 A（A5：M0.4 方案 B 的验证侧）
- **状态**：in-review（分支 `feat/procurement-m0`，**未合并、未部署**）
- **关联变更**：[2026-11-18-procurement-m0-render-order](../2026-11-18-procurement-m0-render-order/summary.md)（A4，被本批验证）；[2026-11-18-procurement-m0-flags](../2026-11-18-procurement-m0-flags/summary.md)（A2）
- **迁移版本**：无（本批无 DDL / 无数据迁移；head 仍 `0153`，下一个可用号 `0154` 留给 A4-0）
- **验收包**：[docs/update/1118/M0/evidence/answer-parity.md](../../../docs/update/1118/M0/evidence/answer-parity.md)
- **方案**：[M0.4-G3修复方案与答案级评估口径.md](../../../docs/update/1118/M0/M0.4-G3修复方案与答案级评估口径.md)
- **计划**：[并行轨与汇合点 §3 轨 A · A5](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_并行轨与汇合点_1118.md)

> 用户已拍板的三条前提（2026-11-18）：**生产暂不部署**；**功能需要全部完成**；
> **本地全绿后统一部署**。决策 **D-2**：本地档通过即可开闸，不做生产档复跑、不加对冲，
> 差异登记为排期 **R9**、在 **M7** 统一暴露。

---

## 1. 需求

**用户视角**：A4 让开闸前后 schema 文本逐字节相同 —— 但「prompt 相同」只是**必要条件**。
M0.4 要证的是：打开 `ENABLE_SEMANTIC_RELEASE` 后，**回答的是不是同一件事**
（口径一致、答案等价），而不是「机制层文本相同」。

**验收标准（计划 A5 行）**：退出码语义正确 + **阳性对照自检**通过。

本批把这条验收拆成三层证据：
1. **工具层**：退出码契约可复现（与 D17 同一套 0/2/3/4/5/6）；
2. **自检层**：33 条「期望值先写死」的用例，且**反向守卫**证明它不是空集；
3. **实跑层**：真实 DB + 真实模型，两态 × 两遍，同态抖动 J = 0。

---

## 2. 设计评审

### 2.1 为什么不能只看 SQL 文本

`diag_semantic_switchover.py` 比的是**文本**。文本相同 ⇒ 机制确定；但两段不同的 SQL
可能查的是同一件事（列序、别名、`IN` 单值 vs `=`），两段只差一个谓词的不同 SQL
也可能"看起来很接近"。所以本批引入**口径元素集合（L3）**作为跨态对比的真判据，
文本只作**定位**（L1）。

### 2.2 L3 为什么是硬红线、为什么不设阈值

M0.4 §4.3 要求「口径覆盖差异率 < 5% 可接受」。**否决**：阈值会把「丢一个谓词」
在元素多的大查询里稀释掉（Q02 有 65 个元素，丢一个只有 1.5%）。丢口径就是查错了数，
没有任何百分比可以换。⇒ L3 差异必须为 **0**，`rate` 只作为**报告信息**保留。

### 2.3 同态抖动基线 J：先证明自己可复现

每态各跑 2 遍。**同态两遍不一致 ⇒ J > 0 ⇒ 退出码 6**，而不是判 3。
理由：模型输出本身有抖动时，"跨态不同"可能是采样噪声，把噪声判成"开关改变了口径"
会让人去修错的东西。这条规则在 L1/L3 与 L2/L4/L5 上**都**生效。

### 2.4 实跑不执行 SQL：L2–L5 必须**显式拒绝**，不能静默跳过

实跑模式只读治理表、只调模型，**不执行**生成的 SQL（对业务库零写入）。
⇒ L2/L4/L5 没有样本。若用户要求这些层，脚本返回 **4** 并提示改用 `--replay-dir`；
静默判"通过"是这类工具最容易犯的假绿。

### 2.5 空集不许通过（本仓反复踩的坑）

三处专门设防：
- **L4/L5**：两侧都不含数值 token ⇒ 比较器返回 `None` ⇒ 6（纯文本不同也判不了）；
- **`aggregateExitCodes([])` ⇒ 4**：一题没判过不能算通过；
- **自检反向守卫**：把 `predicateSet` 恒置空，33 条用例必须变红。

### 2.6 档位不可外推（D-2）

本地 27B 只证**机制**，不证生产 DeepSeek 的**幅度**。报告正文与结论段都写死这条，
并按 **D-2** 写明「本地档通过即可开闸，不要求生产档复跑；差异由 M7 统一暴露（R9）」。

---

## 3. 数据模型变更

无。本批全是 `backend/scripts/` + `backend/app/tests/unit/` 下的新增文件，不碰 ORM、不碰迁移。
首跑在本地开发库（PG 5433 / `qa_metadata`）上完成，**只读**。

---

## 4. 接口契约变更

| 契约 | 变更 | 处置 |
|---|---|---|
| 退出码契约（D17 `diag_semantic_switchover`） | **沿用**，不新增码 | `test_退出码契约与D17一致` 长期钉住 |
| 问题集 `questions.json` | v0 → **v1**（题目带具体实体编码；`version` + `sha256`） | 改题必须升版本并重算摘要，脚本拒绝摘要不符 |
| 档案格式 `off-1/off-2/on-1/on-2.jsonl` | 新增约定：记录可带 `rows` / `answer` | ON 侧记录**必须**带 `semanticReleaseId`，否则回放视为无效证据 |
| CLI | `--database-url-env` / `--llm-key-env` **只接受环境变量名** | 传字面量 DSN / key ⇒ 4 且不回显原值 |

---

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `backend/scripts/eval_semantic_answer_parity.py` | CLI / 报告（自检、实跑、档案回放三入口）+ 脱敏出口 |
| `backend/scripts/answer_parity_compare.py` | 退出码契约、逐题判定编排、问题集摘要 |
| `backend/scripts/answer_parity_sql.py` | L1 规范化（别名→表身份）；L3 口径元素集合 |
| `backend/scripts/answer_parity_values.py` | L2 多重集 / L4 数值容差 / L5 答案等价 |
| `backend/scripts/answer_parity_runtime.py` | 问题集加载、档案读写、实跑与前置检查编排 |
| `backend/scripts/answer_parity_cases.py` | 33 条阳性对照用例（按 0/3/6 分组） |
| `backend/app/tests/unit/test_answer_parity_compare.py` | 41 条单测 |

关键实现选择：

- **别名不是身份**：`s.BPSNUM_0` → `DIM_SUPPLIER.BPSNUM_0`；但**自连接**保留别名
  （`s1.PARENT_0 = s2.CODE` 是两个角色，坍缩就会漏判）。
- **表身份带 schema**：`ODS.T` 与 `DWD.T` 不能塌成 `T`。
- **分页即口径**：Oracle 的 `FETCH FIRST n` / `OFFSET` 进集合，且**有分页时** `ORDER BY`
  键也进集合（它决定取到哪几行）；无分页时排序只是展示顺序，不进集合。
- **OUTER 是冗余词**：`LEFT JOIN` == `LEFT OUTER JOIN`；但 INNER → LEFT 是**真差异**。
- **回放的 meta 继承档案**：回放报告的 `commit` / 档位 / 发布信息取自被回放档案，
  不冒充当前 HEAD（否则报告会把证据归到错误的代码上）。

---

## 6. 测试

### 6.1 新增用例

- **阳性对照自检 33 条**（`answer_parity_cases.py`）：规范化 ⇒ 0 / 口径变化 ⇒ 3 /
  不可判定 ⇒ 6 / 分页与 schema 限定 / L2–L5 与「空集不许通过」。
- **单测 41 条**：契约与版本化、L1/L3、L2/L4/L5、判序、自检反向守卫、档案回放、CLI。

### 6.2 实测结果（2026-11-18）

```text
scripts/eval_semantic_answer_parity.py --self-test          == 33/33 通过 ==   (exit 0)
app/tests/unit/test_answer_parity_compare.py                41 passed  (12.42s)
scripts/eval_semantic_answer_parity.py --precheck-only       差异 0；630/630 行；releaseId=1  (exit 0)
实跑（两态 × 两遍，Qwen3.8-27B-4bit，temperature=0）          4/4 题 exit 0；J = 0   (exit 0)
```

逐题 L3 口径元素数：Q01 = 9、Q02 = 65、Q03 = 3、Q05 = 14，跨态差异均为 **0**。

### 6.3 覆盖率

本批为独立脚本 + 工具单测，不并入服务端覆盖率门禁；判定逻辑（比较层）覆盖率为
41 条单测 + 33 条自检共同覆盖，自检本身带反向守卫。

---

## 7. 安全审查

- **只读**：实跑只读治理表、只调本地模型，**不执行**生成的 SQL；退出时开关恢复为「关」
  （`try/finally`）。
- **凭据**：DSN / LLM key **只走环境变量名**；argv 传字面量 ⇒ 4，且**不回显**原值；
  日志中 DSN 打码、endpoint 打码。
- **写盘边界**：拒绝把产物写进 `raw/`（知识库只读素材区）。
- **无新增外部输入面**：问题集与档案是本地文件，均做结构化校验（摘要、字段类型、重复 id）。

### 7.1 代码评审（code-reviewer，2026-11-18）

| 级别 | 发现 | 处置 |
|---|---|---|
| HIGH | 只收 `exp.Limit` ⇒ Oracle `FETCH`/`OFFSET` 漏判（取 10 行 vs 20 行判成一致） | ✅ 已修：收 `Limit/Fetch/Offset` + 分页时 `ORDER BY` 入集合；自检补 3 条 |
| HIGH | L4/L5 两侧都无数字 ⇒ 判「相等」（空集假绿） | ✅ 已修：返回 `None` ⇒ 6；自检补 2 条 |
| MEDIUM | `ODS.T` 与 `DWD.T` 塌成同一张表 | ✅ 已修：全限定表身份 |
| MEDIUM | L2 把字符串当浮点比 ⇒ `"0001"` == `"1"` | ✅ 已修：字符串按字符串比 |
| MEDIUM | 报告 / 日志回显 endpoint | ✅ 已修：统一打码 |
| LOW | 汇总把 4 误降级为 6 | ✅ 已修：2/5/4 > 3 > 6 |
| LOW | 函数超 50 行（最长 111） | ✅ 已修：拆分到 ≤ 49 行，自检/单测复跑确认 |

评审同时确认的**刻意选择**（不是缺陷）：L3 无阈值、实跑不判 L2–L5、J>0 一律 6。

---

## 8. 部署验证

### 8.1 本地

见 §6.2。无 DDL，无需重建库。

### 8.2 真实数据验证

首跑即真实数据：本地开发库 ACTIVE 发布 `v-p2-d12`（id 1），7 个采购类 / 86 条 JOIN，
OFF 侧 `semanticReleaseId = null`、ON 侧 = 1。**生产未部署、开关未开闸**（按用户决策）。

---

## 9. 关联

- 证据：[answer-parity.md](../../../docs/update/1118/M0/evidence/answer-parity.md)
- 报告：[parity-2026-11-18-replay-Qwen3.8-27B-4bit-163156.md](../../../docs/update/1118/M0/eval/reports/parity-2026-11-18-replay-Qwen3.8-27B-4bit-163156.md)
- 档案：[eval/runs/2026-11-18/Qwen3.8-27B-4bit/](../../../docs/update/1118/M0/eval/runs/2026-11-18/Qwen3.8-27B-4bit/)
- 上游：A4（[render-order](../2026-11-18-procurement-m0-render-order/summary.md)）；A2（[flags](../2026-11-18-procurement-m0-flags/summary.md)）
- 下一步：A6（KPI 12 条归因 + 采购域 `property_alias` 回填）→ A7（口径槽位模板）
- **登记不阻塞**：
  - **R9 / M7**：生产档（DeepSeek）差异统一暴露（按 D-2，本批不要求复跑）；
  - 问题集 v2：问卷 D 回收后补 5 类 × ≥2 题；
  - L2–L5 的真实档案回放：需要能产出 `rows` / `answer` 的链路（当前实跑刻意不执行 SQL）。
