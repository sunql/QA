# feat-semantic-release-a —— 语义发布 P1b（运行期消费 + 写路径）

- 分支：`feat/semantic-registry` → `main`（PR #115）
- 底座：`main@849de29e`（P1a / PR #114 的合并提交）
- 范围：D8 Registry 只读服务 → D9 只读 API → D10 写路径 → D11 全量回归 + 收口
- **无迁移、无 DDL、不部署**（生产仍在 alembic `0140`；`0141/0142` 待单独评审）

## 交付物

| 日 | 提交 | 内容 |
|---|---|---|
| D8 | `8646d52f` | `semantic_registry_service.py`：只读快照服务，按域缓存 + 写时失效 + 60s TTL + 代数防回填竞态；加载校验 `manifest_hash` |
| D9 | `52896079` | `/api/v1/semantic/releases` 三端点；`ENABLE_SEMANTIC_RELEASE` 默认关；`requireAdmin`；生产 app 路由装配守卫（3 条） |
| D10 | `795ff8d9` | 写路径：状态机 DRAFT→VALIDATED→ACTIVE→ARCHIVED、发布闸门、激活「顶替」、审计同事务、提交后失效；4 条 POST；路由守卫 3 → 7 条 |
| D11 | `81090a44` | 全量回归 + 失败集合对差 + PR。**路由门禁守卫兼容 FastAPI 0.141**（CI 首轮净新增 2 条失败的直接修复） |

每日细节见 `daily/D08.md` … `daily/D10.md`。

## 关键设计决策

1. **运行期只消费快照（P0 裁决一）**：`getActive()` 只读 `semantic_release`，不回查治理表、不重编译；
   静态守卫禁止 registry 出现编译/ORM 依赖标识符。
2. **空结果也缓存**：未发布是常态。
3. **缓存键 = 域作用域**，与部分唯一索引 `COALESCE(domain_code,'')` 的语义对齐。
4. **写时失效 + 60s TTL**：TTL 只兜底跨进程/手工 SQL 的变更；正常路径靠提交后 `invalidate()`。
5. **代数守卫**：失效前开始的读不得把旧快照回填进缓存。
6. **快照每次解析私有副本**：消费方原地改 dict 不会污染进程缓存。
7. **状态机双保险**：服务层迁移矩阵 409；库侧 CHECK + 部分唯一索引兜底。
8. **激活「顶替」语义**：同作用域原 ACTIVE 同事务归档；**归档后显式 `flush()`**，
   否则回滚到更小 id 时 SQLAlchemy 按主键序发送 UPDATE，瞬时两条 ACTIVE 撞唯一索引，
   合法回滚被误报 409（D10 评审 HIGH，变异 D 取证）。
9. **闸门分级**：无源 → 告警（治理债显式化）；`PUBLISHED` 的 RATIO/DIFFERENCE 缺分子/分母落点 →
   阻断；`MEASURE` 不检查分母；报告封顶 20 条。
10. **身份字段入口校验**：`actor` 1~64 / `actor_departments` ≤500（审计列宽），避免 asyncpg `DataError` 500。

## 测试与审查

- 语义面 **300 passed / 0 failed**（20 个 `*semantic*` 文件，真实 PG `qa_metadata_test_semantic` @5434 / alembic 0142）
- D10 四文件 76 passed；邻近 `test_agent_orchestration_route_auth.py` 3 passed
- 路由守卫在 **FastAPI 0.136.1 与 0.141.1 下各 6 passed**（隔离装载 `PYTHONPATH` 复测）
- 每批 `code-reviewer` + `security-reviewer` 两轮；发现与处置见各日「评审处置」
- 变异取证 A–E：闸门类型 / 迁移守卫 / 提交后失效 / 归档-激活 flush / include-context 回收（E 在 0.141.1 下 3 条红），全部还原

## 明确保留项（已入档）

- **stub 鉴权 fail-soft**：非生产环境下 stub 用户可带 admin 角色。与 D9 同口径；
  fail-closed 会击穿全部 stub 夹具，属跨特性策略变更，另行立项。
- **完整性异常保持 500**：篡改快照 fail-closed，不伪装 4xx。
- **按域版本唯一性无库侧约束**：`uq_semantic_release_global_version` 只覆盖全局；
  按域发布上线时再补同域唯一索引（需迁移 + 单独评审）。
- **本地 venv 与锁文件版本不一致**：`uv.lock` 锁 fastapi **0.141.1**（CI 装配），本地 venv 是
  **0.136.1**。守卫已覆盖两种形态；不再把「本地绿」当作版本敏感问题的证据（D10 评审 #4 的教训）。

## CI 失败集合对差

**权威判据 = 闸门自己创建的 `merge-gate / failure-set-diff` check-run 输出**（`129551469962`）。

| 项 | 值 |
|---|---|
| 基线 run | `42661565259`（main@`849de29e` push，PR 事件派发前最新完成） |
| PR run | `42725327459`（head `81090a44`，attempt 1） |
| PR run 的 main 侧底座 | `849de29e`（= 基线 head，同一棵树） |
| 失败总数 | 基线 **142** / PR **142** |
| 净新增 | **0** |
| 净丢失 | **0** |
| job 可用性 | 9 个 job 全部「可用」（无 cancelled / 无缺完成标记） |
| 结论 | 退出码 **6 / CI_SURFACE_CHANGED** ⇒ 失败集合对差本身成立（0/0），但 PR 改动了 CI 相关面，须人工仲裁 |

逐 job 失败数（两侧完全一致）：

| job | 失败数 |
|---|---|
| Backend / unit shard 0 | 10 |
| Backend / unit shard 1 | 6 |
| Backend / unit shard 2 | 1 |
| Backend / unit shard 3 | 3 |
| Backend / integration shard 0 | 31 |
| Backend / integration shard 1 | 44 |
| Backend / integration shard 2 | 25 |
| Backend / integration shard 3 | 22 |
| Frontend / typecheck + coverage | 0 |

**退出码 6 的仲裁依据**（判定集合 `A ∪ P` ∩ CI 清单）：

| 文件 | 改动性质 | 能否抑制失败 |
|---|---|---|
| `backend/app/config.py` | 新增 `ENABLE_SEMANTIC_RELEASE` 设置 + `semanticReleaseAuthMisconfiguration` 纯函数 | 否（纯新增） |
| `backend/app/tests/_testapp.py` | 开关开启时注册 semantic 路由（默认关闭 ⇒ 其余用例零影响） | 否（纯新增，且受关断开关门） |
| `backend/app/tests/integration/conftest.py` | 测试夹具里 `semanticRegistry.invalidate()` | 否（只清进程缓存） |

三者均为**新增**、不改变任何既有用例的收集/执行路径 ⇒ 失败集合可比；142 = 142、净新增 0、
净丢失 0 ⇒ 按既有流程走 `gh pr merge --admin --merge`（solo 仓库无 bypass 账号，
`enforce_admins=false` 是既定的显式越权口，见 [[qa-system-merge-gate-protection-config]]）。

> ⚠️ **本地重跑陷阱**：事后用 `python -m app.utils.ci_failure_set` 复算这一 run 会得到
> **退出码 4 / COVERAGE_MISMATCH**，并多出一个「job：`merge-gate / failure-set-diff`」。
> 原因是闸门用 `POST /check-runs` 创建的 check 会被归入该 run 的 jobs 列表；闸门自己评估时
> 该 check 尚不存在。⇒ 复算必须以 check-run 的 output 为准，或在新 sha 的 CI 刚完成、
> gate 尚未落 check 的窗口内做（见 [[qa-system-merge-gate-rerun-procedure]]）。
