# 研究 BO 臂数据源收窄（规划层）—— 2026-11-20

## 来源
1120 召回根治验收挂账 ⑪ / 交接项 ②（`acceptance-1120.md` §7.3）：
BO 臂 `_searchBusinessObjects`（`api/v1/research.py:254`）是**全局**检索（docstring 自陈）；
执行面已过滤（`selectClassesForTables` 只取「过滤后集合 ∩ ESL 表名」）⇒ 残余风险是
**规划层选错 BO**（查空 / STEP_MISSING_SQL），不是 ORA-00942。

## 方案（爆炸半径最小）
在 **`_stageEsl` 相位**对 BO 臂做**后置过滤**（`filterBusinessObjectsByDatasource`，
`research_agent_phases.py`）——不动 ESL / searcher 签名（改签名会波及全部 ESL fake）：
- 数据源取 `state["datasourceId"]`（run state 已有，`research_run_state.stateToDict`）；
- 命中口径与召回过滤同源：`loadDatasourceTableSet`（裸名 ∪ owner 限定名，.upper()）；
- 空/缺 sourceTable 保留（无法判定不误杀）；ds 无缓存 ⇒ fail-open 保留全量；
- 剔除打 **warning**（对齐召回过滤「降级留痕」口径）；
- BO 全被剔 + 其余两臂空 ⇒ 与「三臂全空」同一出口（empty_scope checkpoint，
  不喂空范围给 planner）。

## 测试（TDD，RED→GREEN）
4 个用例并入 `test_research_agent_service.py`（既有集成文件纪律）：
助手直测 ×2（保留/剔除/空表 + fail-open）+ 接线守卫 ×2（范围确认 checkpoint
里的 arms 已收窄 + 全剔落 empty_scope）。
定向回归：`test_research_agent_service.py` 63 passed；研究线回归
（adapters / schema_reflection / recall_filter unit / integration/research/ 全目录 /
test_research_stream）108 passed + 1 failed =
`test_outbox_lives_in_dedicated_test_db` **环境性红**（断言库名恒为
`qa_metadata_test`；共享测试库被采购线升到 0159 ⇒ main 线用
`qa_metadata_test_mainline` 隔离库名，基线 worktree 同库复跑同样失败，
净新增 0）。
