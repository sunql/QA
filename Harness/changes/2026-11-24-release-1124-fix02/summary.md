# 发版 1124-fix02 摘要：Copilot 场景体验全量打磨

- 日期：2026-11-24　分支：`feat/ontology-evolution`（1895e3eb..HEAD，pending01 后全部修复）
- 镜像：backend `qa-system-backend:amd64-1124-fix02`（54b8bd651cbd）/ frontend `qa-system-frontend:amd64-1124-fix02`（f9cad6e0e1d5）；**worker 同 backend 镜像**
- 迁移：**零**（0169）；种子：**零**（沿用 pending01 已落库的 schema/绑定/ACL）
- runbook：同目录 `runbook.md`（Phase 0–8，thdeploy 固定值预填）

## 功能面

1. **同日复用（后台判断，前台正常问话）**：
   - 工具层：三段查询按「过滤参数+数据源+当天」缓存至当日午夜（省 DB）；
   - run 级：同用户+同 Agent+同模型+同问题文本、当日、状态 ∈ {QUEUED,RUNNING,SUCCEEDED}
     ⇒ 新 turn 指向既有 run（零 LLM 成本）；FAILED/CANCELLED 不复用（保重试）；
     同会话不同追问仍带上下文新跑（契约测试更新：第二问改用不同文字）；
2. **数据显示**：供应商名称（LEFT JOIN BPSUPPLIER ⇒ J1=河北新泰鸿汽车零部件有限公司）、
   TOP 行物料描述（LEFT JOIN ITMMASTER ⇒ X13 平头铆螺母）；长文本完整换行；
3. **冲突优先级**：对话框文字（含纯编码 B109/T3）提到的维度 ⇒ 高级参数让位；
   尾注带仲裁指令；
4. **交互**：标题带场景名；chips 仅初次显示；结果卡三块 Collapse；轨迹抽屉
   Timeline 分段（Markdown）；发送进度弹窗；Tabs 纯切换器；输入框边框加重；
   会话 id 隐藏；自动恢复移除（跨账号 404 修复）。

## 关键修复

- BySupplier 外层误引内层别名 Q ⇒ ORA-00904/500（125aeebc）；
- 冲突检测漏纯编码提问（run 44 实证：B109 文字 vs B125 参数）。

## 部署要点

1. 三容器 recreate（backend → worker → frontend）；零迁移零种子；
2. 前置：pending01 已部署（pending-delivery 端点存在、工具 schema/绑定/ACL 已落库）；
3. 验收：标题带场景名、同日复用（run 数不增）、名称/描述列、Timeline 抽屉、进度弹窗；
4. 回滚：tag 换回 + recreate（30 秒级，库不动）。

## 遗留（验收后另批）

- P2 欠交明细常驻页；P3 侧栏搜索/分组/置顶；
- 结果缓存跨天持久化（当前进程内、重启失效——保守侧）。
