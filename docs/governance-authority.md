# Authority 治理文档 v1（v3.1 任务 M5）

> 来源：v3.1 蓝图 §4.13 / §12.2 + 季度评审 2026-09-29 §6/§8/§9 决策（D1=(b) / D2=双列 / D3=必含+案例库+模板化裁决请求）
> 文档定位：内部「软约束」治理流程第一版（v1），覆盖范围、原则、字段语义、委员会、仲裁流程、联动、案例库、模板与后续待办。
> 状态：**草案 v1** —— 由治理委员会（M5 §3）首次会议后审稿；不进入法定流程前仅作治理流程参考。

---

## §1 范围与原则

### 1.1 文档范围

本文件描述知识条目的 **Authority 字段**（`authority_level` L0-L5 数据精度 + `authority_department` 11 部门组织归属）在跨部门场景下的治理流程。具体回答：

- **谁能修改**：业务专家通过 `PATCH /wiki/pages/{pageId}`（含 `authorityDepartment`）显式提交；委员会决议可批量覆写
- **何时仲裁**：跨部门对同一条事实存在「部门归属」「操作语气」冲突
- **怎么仲裁**：M5 §4 流程
- **与哪些字段联动**：置信度（`confidence` / `confidence_level`）、拒答原因（`refuse_reason`）
- **争议落地**：不阻塞写入，但记录到治理日志供委员会追溯

**不在本文件范围**：

- `authority_level` L0-L5 的**取值算法**（属于学习回路 M3，不进治理）
- 知识条目的**业务维度**（`dimension` 字段，机制 1 自动分类，人写反）
- 知识条目的**生命周期状态**（`status` 字段，纯业务流转）
- 冲突检测的**算法**（属于机制 3 的自动检测，不进治理）

### 1.2 核心原则

| 原则 | 描述 |
|---|---|
| **软约束，非强校验** | Authority 字段**写入不阻塞**（任一部门可填、List 可为空），仅在治理流程触发时进入仲裁；DB 不建引用、不可弃用 |
| **双轴并存，语义不重叠** | `authority_level` 表**数据精度**（L0-L5 客观），`authority_department` 表**组织归属**（11 部门主观）；两轴互不替代 |
| **不进学习回路** | Authority 字段不进入 `progressive_upgrader` / `claim_extractor` / `confidence_service` 的信号链路 —— 改字段不影响模型决策，仅供治理追溯 |
| **治理流程推动新行必填** | 存量行 NULL 合法（不破坏向后兼容），新写入行由治理流程推动逐步必填；目标是 v3.2 起 100% 行带 department |
| **可追溯、可驳回** | 所有覆写记录进入审计（`audit_log`）—— 谁、什么时候、改成什么、依据什么；委员会可批量回滚 |

---

## §2 字段语义定义

### 2.1 `authority_level` L0-L5 —— 数据精度等级

| 值 | 含义 | 典型场景 |
|---|---|---|
| **L5** | 国家强制 / 法律法规原文 | 反洗钱法规、ISO 标准 |
| **L4** | 行业权威 / 公认标准 | 会计准则、行业协会指引 |
| **L3** | 集团内部决议 / 正式发文 | 集团红头文件、内部 SOP 终版 |
| **L2** | 部门内正式生效文件 | 部门规章、已审批的业务规则 |
| **L1** | 项目级 / 临时性结论 | 项目决议草稿、过渡期备忘 |
| **L0** | 个人意见 / 待验证 | 个人笔记、未经审核的引用 |

**语义要点**：

- L0-L5 是**客观事实**的精度评估 —— 不随部门变更（同一份国家法规在不同部门看都是 L5）
- 由学习回路 (`progressive_upgrader.py:255`) 自动调整：**置信度 ≥ 0.95 + LLM 复审通过** 可升一级；**手动打回证据**可降一级
- **冲突时不应被覆写**：若一条 L5 法规与某部门决议冲突，应走部门决议修改流程，不是 L5 降级

### 2.2 `authority_department` —— 组织归属（v3.1 新增）

11 个合法值（详见 `wiki_models.KNOWLEDGE_AUTHORITY_DEPARTMENTS`）：

```
前 9 个常规组织归属：
    SALES_MGMT      销售管理部
    FINANCE         财务部
    SCM             供应链/采购
    QA              质量部
    HR              人事
    IT              IT/技术
    OPS             运营部
    EXEC            高管/决策层
    LEGAL           法务
后 2 个软约束占位：
    INDUSTRY_STANDARD   行业标准/外部权威（如金融行业指引）
    CROSS_DOMAIN        跨部门共识
```

**语义要点**：

- **表组织归属**，不表「哪个部门应负责」「哪个部门说的话算」
- 一条知识**可有多个归属**：用 `CROSS_DOMAIN` 标识跨部门共识条目；其他枚举均为**单部门归属**
- **冲突时该字段是关键仲裁依据**：QA 部与 OPS 部对同一条数据有不同看法时，谁的 `authority_department` 优先？—— 见 §5 联动逻辑
- **NULL 合法**：旧数据迁移期 / 治理流程未推动的行保留 NULL；新写入行由治理流程推动必填

### 2.3 双轴关系（决策 D2=双列）

```
authority_level = 客观精度   → 学习回路维护（M3 / progressive_upgrader）
authority_department = 主观   → 由治理流程维护（M5 / 本文）
```

两轴**互不替代**：

- 一份 ISO 9001 法规：`authority_level=L5`（数据精度高）+ `authority_department=INDUSTRY_STANDARD`（外部权威，非本企业部门）
- 一份本企业财务部决议：`authority_level=L3`（集团内部决议）+ `authority_department=FINANCE`（财务部归属）

**判断优先级**：

1. 数据精度（`authority_level`）优先 —— L5 数据不能被 L2 部门决议覆写
2. 组织归属（`authority_department`）冲突时，走 §4 仲裁流程
3. 同等精度 + 同等归属 = 共识，无冲突

---

## §3 治理委员会组织建议

### 3.1 委员会组成（v3.1 推荐结构）

| 角色 | 人数 | 来源 | 职责 |
|---|---|---|---|
| **主任委员** | 1 | 高管层 | 召集会议、决议最终签发、对外发声 |
| **常任委员** | 3-5 | 各业务部门 Owner | 数据治理负责人（一个部门一名） |
| **执行秘书** | 1 | 数据治理团队 | 议程组织、决议落地跟进、文档维护 |
| **领域专家** | 0-N（按议题） | 按议题邀请 | 特定领域的资深业务专家 |
| **法务观察员** | 1 | 法务部 | 法规解读、合规审核（L5/L4 数据相关） |

### 3.2 主任委员与常任委员的来源

- **主任委员**：建议由 CIO 任命或由数据治理委员会推举
- **常任委员**：每个一级部门（销售/财务/供应链/质量/HR/IT/运营/高管/法务）的**部门 Owner** 担任
- 名单由 v3.1 启动会议确定，2026-09-29 决策：**v3.x 治理委员会建立后再激活**（见 §8）

### 3.3 会议机制

| 会议类型 | 频率 | 议程 |
|---|---|---|
| **例行会议** | 月度 | 上月冲突清单、上月决议执行情况、当月新增议题 |
| **专项会议** | 按需（触发后 7 天内召开） | 单条跨部门冲突的深度讨论 |
| **年度复盘** | 每年 12 月 | 全年案例库复盘、规则修订、下年规划 |

### 3.4 决议生效路径

```
委员会决议（书面签字）
   ↓
执行秘书操作（PATCH 或工单系统批量更新）
   ↓
审计记录（audit_log）
   ↓
通知相关 Owner（站内信 + 邮件）
```

---

## §4 冲突仲裁流程

### 4.1 触发条件

满足下列**任一**条件即触发仲裁流程：

1. **同 claim 跨部门归属冲突**：两个部门的 claim 对同一事实有不同表述（如「折扣归类」财务 vs 销售）
2. **claim vs page 归属冲突**：page 归属 QA 部，但某 claim 归属 SALES_MGMT（如业务专家单方面补充）
3. **共识提案**：任一常任委员可提案「此条应升级到 `CROSS_DOMAIN`」
4. **置信度冲突**：`confidence_level=REFUSE` 且 `reason: conflicting_departments`（见 §5）

### 4.2 流程化步骤（蓝图 §4.13 落地）

```
[Step 1] 发现冲突
    ├─ 自动检测：机制 3 conflict_detection 标出「同 page_id 多 department」
    └─ 手动报告：任一常任委员或业务专家可发起工单

[Step 2] 列出 conflict_departments
    └─ 列出所有相关 department 值（如 [FINANCE, SALES_MGMT]）

[Step 3] 通知双方 Owner（7 天 SLA）
    ├─ 站内信：@ 双方常任委员 + 业务专家
    └─ 邮件：抄送主任委员

[Step 4] 双方面谈（3 个工作日内）
    ├─ 双方各自陈诉立场
    ├─ 必要时邀请法务观察员解读 L5/L4 数据
    └─ 形成「非正式」共识或分歧记录

[Step 5] 治理委员会决议（专项会议）
    ├─ 听取双方面谈记录
    ├─ 表决（主任委员 + 在场常任委员 ≥ 2/3 通过）
    └─ 形成书面决议（签字版）

[Step 6] 落库
    ├─ 执行秘书 PATCH 涉及的 page/claim
    ├─ audit_log 写「治理委员会决议 + 编号 + 日期」
    ├─ confidence_level 触发 REFUSE 的同时附 conflict_departments
    └─ 站内信 + 邮件通知双方

[Step 7] 复盘（30 天后）
    ├─ 检查是否仍存在未解决分支
    └─ 补充到 §6 案例库
```

### 4.3 7 天 SLA 说明

- **Step 3→Step 5** 总共给双方 Owner 7 个自然日反应时间（含周末）
- 超 SLA 未答复 = 默认同意对方立场（治理流程推动力，不阻塞）
- 例外：涉法务的 L5/L4 数据可申请延长至 14 天，需主任委员批准

### 4.4 紧急通道（高管介入）

对**显著影响业务**的冲突（如「返利规则跨部门分歧导致客户合同冻结」），可走紧急通道：

1. 任一常任委员发起紧急通道请求
2. 涉事部门同步确认（口头 + 站内信），避免单方越权
3. 主任委员 24 小时内召集**临时专项会议**（5 人决策常任含：主任 + 涉事方 + 法务观察员）
4. 决议执行后补走 §4.2 流程补录文档

---

## §5 与 confidence / refuse_reason 联动

### 5.1 联动背景

蓝图 §12.2 定义的离散 4 级置信度（HIGH/MEDIUM/LOW/REFUSE）中，`REFUSE` 是**拒绝回答**级别。触发 `REFUSE` 的场景包括：

| 触发场景 | refuse_reason 取值 |
|---|---|
| 数据冲突（多部门说法不一） | `CONFLICTING_DEPARTMENTS` |
| 数据过期（valid_to < 当前时间） | `EXPIRED` |
| 数据缺失（无证据 / 置信度 < 0.3） | `INSUFFICIENT_EVIDENCE` |
| 数据越权（涉及敏感字段但用户无权限） | `UNAUTHORIZED` |
| 其他 | `OTHER` |

### 5.2 与 authority_department 的具体规则

- **`CONFLICTING_DEPARTMENTS`** 触发时，**必须**在 response payload 附 `conflict_departments: list[str]`
  - 例：`{ refuse_reason: "CONFLICTING_DEPARTMENTS", conflict_departments: ["FINANCE", "SALES_MGMT"] }`
  - 前端据此展示「X 部门 vs Y 部门」徽标，提示用户「请与 X / Y 部门确认」
- **多部门同义表达**：若两 claim 内容相同但归属不同，**合并**为 `CROSS_DOMAIN` 共识条目（不视为冲突）
- **L5 数据不可被 REFUSE**：`authority_level=L5` 的数据即使被某部门质疑，仍可回答（附 L5 提示），不进入 REFUSE 路径

### 5.3 与 progressive_upgrader 的边界

| 维度 | progressive_upgrader（M3） | 治理流程（M5） |
|---|---|---|
| 触发 | 置信度计算 + 人工打回 | 冲突报告 + 委员会决议 |
| 修改字段 | `authority_level`（升/降） | `authority_department`（改/合并） |
| 频率 | 高频（每次新数据都会触发） | 低频（仅冲突时触发） |
| 决策者 | 业务专家 + 自动化规则 | 治理委员会 |

**边界原则**：governance 流程**不会**自动触发 `progressive_upgrader`；M3 自动升降权**不会**自动覆写 department。两路决策独立。

---

## §6 跨部门冲突案例库（v1）

### 6.1 案例 A：销售 vs 财务「折扣归类」

**背景**：

某集团统一折扣规则 `PAGE-RULE-DISCOUNT-V3`，`authority_department=FINANCE`（财务部规章）。

销售部补充一条 claim：「现金折扣应计入销售收入」（原 `PAGE-RULE-DISCOUNT-V3` 仅写「冲减应收账款」），claim 归属 `SALES_MGMT`。

**冲突**：

- 财务部立场：折扣应冲减应收账款（按现行会计准则）
- 销售部立场：现金折扣影响销售业绩核算，应单列

**仲裁路径**：

1. 自动检测：机制 3 标出「同 page 多 department」[FINANCE, SALES_MGMT]
2. 双方 Owner 陈诉：财务部引用 IAS 18 / CAS 14，销售部引用集团内部业绩核算指引
3. 委员会讨论 → 折中方案

**决议（示例）**：

- `PAGE-RULE-DISCOUNT-V3` 维持 `FINANCE`（会计准则主控）
- 新增 `PAGE-RULE-CASH-DISCOUNT-V1`，归属 `CROSS_DOMAIN`，由两部门联合署名
- 销售部原 claim 迁移到新 page，**标注 cross-ref**

**置信度联动**：

- 旧 claim `confidence_level=REFUSE`，`refuse_reason=CONFLICTING_DEPARTMENTS`
- 新 claim `confidence_level=HIGH`（两部门联合署名 + 双证据）

### 6.2 案例 B：质量 vs 运营「不良率口径」

**背景**：

某工厂不良率统计 page `PAGE-METRIC-DEFECT-RATE-V2`，`authority_department=QA`（质量部指标）。

运营部补充一条 claim：「不良率应包含运输破损」（原 page 仅含生产过程不良），claim 归属 `OPS`。

**冲突**：

- 质量部立场：不良率仅指生产过程（按 ISO 9001）
- 运营部立场：客户接收时发现的不良也应计入（按客户满意度调研）

**仲裁路径**：

1. 自动检测：标出冲突
2. 双方面谈：法务观察员解读 ISO 9001 适用范围（仅生产过程）
3. 委员会决议

**决议（示例）**：

- `PAGE-METRIC-DEFECT-RATE-V2` 维持 `QA`，**不变更**
- 新增 `PAGE-METRIC-DELIVERY-DEFECT-V1`，归属 `OPS`，由运营部单独维护
- 两指标**并存**，在仪表盘分别显示，避免误导

**置信度联动**：

- 旧 claim `confidence_level=MEDIUM`（仅适用生产）
- 新 claim `confidence_level=HIGH`（适用交付）

### 6.3 v1 案例库扩展

后续每次仲裁完成后，由执行秘书整理案例并加入 §6，**至少包含**：背景 / 冲突 / 仲裁路径 / 决议 / 置信度联动五部分。年度复盘会审阅所有 v1.x 案例是否需修订流程。

---

## §7 模板化「冲突裁决请求」

### 7.1 邮件模板

**主题**：[{{auditLabel}}] 知识库 Authority 冲突裁决请求 — {{pageId}}（{{conflictDepartments | join(', ')}}）

**正文**：

```
{{committeeChairName}} 主任委员 / 各常任委员：

收到关于知识条目 {{pageId}} 的 Authority 归属冲突报告，依据 v3.1 治理文档
§4 流程请求仲裁。

== 基本信息 ==
- 条目：{{title}}（{{pageId}}）
- 当前 authority_level：{{authorityLevel | "未填"}}
- 当前 authority_department：{{authorityDepartment | "未填"}}
- 涉及部门：{{conflictDepartments | join("、")}}

== 冲突描述 ==
{{conflictDescription}}

== 双方立场 ==
【{{departmentA}}】（Owner：{{ownerA}}, {contactA}}）
{{DepartmentA_arguments}}

【{{departmentB}}】（Owner：{{ownerB}}, {contactB}}）
{{DepartmentB_arguments}}

== 已参考的依据 ==
- v3.1 蓝图 §4.13：{{referenceA}}
- 现行法规/标准：{{referenceB}}
- 集团内部决议：{{referenceC}}

== 请求 ==
- [ ] 提交治理委员会专项会议讨论
- [ ] 保持现状不修改
- [ ] 其他：__________

执行秘书 {{secretaryName}}
{{requestDate}}
```

### 7.2 站内信模板

```
@{{ownerA}} @{{ownerB}} 您好：

知识条目「{{title}}」触发跨部门 Authority 冲突，依据治理流程 §4，需双方 7 个
自然日内反馈立场。

**条目**：{{pageId}}
**冲突**：{{conflictSummary}}

**请回复**：
- 您的立场：____
- 引用依据：____（法规 / 标准 / 决议）
- 是否接受调解：____

未答复 = 默认同意对方立场（治理流程推动力，不阻塞）。

如需延长至 14 天（限涉法务 L5/L4 数据），请直接联系主任委员 {{committeeChairName}}。

查看详情 / 回复：[站内信链接]
```

### 7.3 工单模板

**标题**：[Authority 冲突] {{pageId}} — {{conflictSummary}}

**字段**：

| 字段 | 取值 |
|---|---|
| 工单类型 | AUTHORITY_CONFLICT |
| 优先级 | MEDIUM（L5/L4 数据升级到 HIGH） |
| 涉及条目 | {{pageId}} |
| 涉及部门 | {{conflictDepartments}} |
| 触发来源 | 自动检测 / 手动报告 / 置信度冲突 |
| 7 天 SLA 到期 | {{slaDate}} |
| 涉及 claim ID 列表 | {{claimIds}} |
| 当前 confidence_level | {{currentConfidenceLevel}} |
| 当前 refuse_reason | {{currentRefuseReason}} |

**流转节点**：

```
[Created] → [Notified Both Owners] → [Both Responded] → [Committee Meeting Scheduled]
   → [Committee Decision] → [Executed] → [Closed] → [30-day Review] → [Archived]
```

每个节点都有时限提醒；超时升级到主任委员。

---

## §8 后续待办（v3.x 激活条件）

### 8.1 v3.x 待激活项

本文件**仅作草案**，需满足下列条件才能升级为正式治理流程（v3.2 起的强制规则）：

| 条件 | 当前状态 | 负责人 |
|---|---|---|
| 治理委员会实际建立 | **未启动**（2026-09-29 决策：v3.x 起推动） | 数据治理团队 |
| 主任委员正式任命 | 未启动 | CIO |
| 9 个常任委员全部到位 | 部分到位（部门 Owner 部分指定） | 数据治理团队 |
| 仲裁流程 IT 化（工单系统） | 部分支持（站内信 + 邮件可用，工单系统未集成） | 数据治理团队 + IT 部门 |
| 案例库迁移到 Wiki page | **本文档承载**，未来迁移到 `wiki/RULES/authority-cases/` | 执行秘书 |

### 8.2 v3.x 升级目标

- **v3.2**：治理委员会建立 + 启动 §3.2 常任委员名单
- **v3.3**：工单系统集成 + 案例库迁移到 Wiki
- **v3.4**：纳入 ISO 9001 数据治理合规审计

### 8.3 v1 阶段的「软启动」

在治理委员会正式建立前，本文档 v1 已可支持：

- 业务专家**自愿发起**冲突报告（站内信 + 邮件渠道）
- 数据治理团队**非承诺式**响应（不强制 7 天 SLA）
- 案例库**手工归档**到 §6

软启动期所有仲裁都是「建议性」而非「强制性」——冲突双方仍可走原流程（部门内部决议），治理文档仅作参考。

### 8.4 与 confidence_service 的最终绑定

- 当前 `confidence_service` 不感知 `authority_department`
- v3.x 升级时需新增 `conflict_departments` 字段（响应 payload 联动）
- 暂存于 `chat_response_extensions` JSONB 字段，正式激活后迁移到独立字段

---

## 附录 A：术语对照表

| 术语 | 含义 | 别称 |
|---|---|---|
| Authority | 权威度（数据精度 + 组织归属双轴） | 权威性 |
| 软约束 | 字段可空、不强制、不阻塞 | non-blocking validation |
| 仲裁 | 跨部门冲突的解决流程 | 调解 / conflict resolution |
| Owner | 业务部门对某领域数据负责的人 | 数据 Owner / 业务专家 |
| 常任委员 | 治理委员会成员，每个部门一名 | standing committee member |
| 主任委员 | 治理委员会主席 | committee chair |
| 置信度冲突 | confidence_level=REFUSE 且 reason = CONFLICTING_DEPARTMENTS | 数据冲突 |

## 附录 B：相关文档

- [v3.1 蓝图 §4.13](系统架构优化思路0928-v3.1.md) — 字段定义、软约束口径
- [v3.1 蓝图 §12.2](系统架构优化思路0928-v3.1.md) — 置信度 4 级 / REFUSE 触发条件
- [季度评审 2026-09-29](../Harness/changes/2026-09-28-arch-upgrade-v31/quarterly-review-2026-09-29.md) — 决策 D1 / D2 / D3 来源
- [wiki_models.py](../backend/app/domain/wiki_models.py) — `KNOWLEDGE_AUTHORITY_DEPARTMENTS` 元组
- [wiki_page_service.py](../backend/app/services/wiki_page_service.py) — `_assertKnowledgeAuthorityDepartment` 兜底
- [alembic 0102](../backend/alembic/versions/0102_authority_department.py) — DB schema

## 附录 C：版本

| 版本 | 日期 | 作者 | 主要变更 |
|---|---|---|---|
| v1.0 | 2026-09-29 | 数据治理团队 | 初始草案，承接 M5 治理任务 |

---

> 本文档由 M5 任务配套产出；治理委员会成立后由委员会接管维护。
> 反馈请提交到：[数据治理 GitLab Issues / 治理文档 v1]({{TODO}})