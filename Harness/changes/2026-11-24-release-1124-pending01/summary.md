# 发版 1124-pending01 摘要：待到货场景全链路

- 日期：2026-11-24　分支：`feat/ontology-evolution @ 125aeebc`（PR #178 后半段，未合 main 即部署，与 1122 热修同先例）
- 镜像：backend `qa-system-backend:amd64-1124-pending01`（fa9854888fc5）/ frontend `qa-system-frontend:amd64-1124-pending01`（196b5d64d250）；**worker 与 backend 同镜像**
- 迁移：**零**（0169 保持）；runbook：同目录 `runbook.md`（Phase 0–8 + thdeploy 固定值预填）

## 功能面

1. **Copilot 场景卡片墙**（首页）→ 场景工作区两级结构；场景钉 Agent（防「工具不存在」与 422 串味）。
2. **待到货欢迎态**：业务现状卡走 `GET /procurement/pending-delivery/summary`（直调 handler，**零 LLM 成本**）+ 推荐问题 chips（预设槽位）+ 底部常驻输入栏。
3. **高级过滤**：供应商/物料/基地多选（编码 IN + 中文名模糊 LIKE；中文物料词先经 MDM 解析成 ERP 编码）、时间窗按月/按时间段二选一、最低逾期天数。
4. **结果卡结构化**：统计 + 供应商表 + TOP 行表（数据不经 LLM 复述）+「解读与建议」Markdown 卡 + 导出明细 CSV。
5. **会话侧栏**：历史列表/切换/删除；刷新自动恢复线程。

## 关键修复（同批）

- BySupplier SQL 外层误引内层别名 Q ⇒ ORA-00904/500（`125aeebc`）。
- `seed_agents` 策略愈合扩为**全量绑定并集**补缺（新工具绑 profile 的部署重跑 seed 即拿授权）。
- chips 显式传参绕过 React state 异步闭包；`setTurns` updater 内禁 .map（渲染期抛错崩树）。

## 部署要点（详见 runbook）

1. 三容器 recreate（backend → worker → frontend）；
2. `seed_agent_tool_configs`（工具 schema 新槽位）+ `seed_agents`（ACL 愈合补 PURCHASE_ORDER/ODS）；
3. **手动更新 SOURCING 编排指令**（runbook §5 内嵌脚本；播种器只补工具不改指令）；
4. 验收：welcome bundle 文案、新端点 403、schema 槽位、SOURCING 含 ODS 授权、浏览器端到端五条。

## 遗留（验收后另批）

- P2 欠交明细常驻页（参考系统第二张图）；P3 侧栏搜索/分组/置顶。
- 「运行仍在后台继续」文案在无 worker 场景的误导（低优先）。
