# 生产发布记录：main `df9b5b3a`（1031）

- **日期**：2026-10-31
- **目标**：192.168.205.82（CentOS 7，内网隔离，Docker Compose）
- **代码基线**：`main df9b5b3a`（tree `834c72f6`）
- **镜像迁移 head**：`0138`
- **构建机**：开发机（arm64）交叉构建 `linux/amd64`；生产侧只 `docker load`，不构建
- **操作分工**：镜像包与本文档由开发机准备；生产服务器操作由用户手工执行

## 1. 交付物

| 文件 | 镜像 | 架构 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-backend-amd64-1031.tar.gz` | `qa-system-backend:amd64-1031` | linux/amd64 | `12347ec3c901793e51c40c5dbfe494de83d64f3bbfb7910e42fcd7a71853eb1a` | 217M |
| `/tmp/qa-frontend-amd64-1031.tar.gz` | `qa-system-frontend:amd64-1031` | linux/amd64 | `b6786c587326a857f99faf66edd678f2b30c6570e68f3adab404b68f71e65147` | 26M |

- 后端镜像 229M / 前端 27M（docker image 层）；包格式与 1030 相同（OCI layout，`docker load` 已验证）
- 生产侧 retag：`qa-system-backend:amd64-1031 → qa-backend:offline`；
  `qa-system-frontend:amd64-1031 → qa-system_frontend`（下划线）

## 2. 构建与核验

- 发版源：fresh worktree `/private/tmp/qa-deploy-1031-prod`，detached `origin/main` = `df9b5b3a`
- 镜像内 5 个关键文件与工作树 sha256 逐一相同：
  `app/services/time_scope_resolver.py`、`nl2sql_dialects.py`、`chat_helpers.py`、
  `agent_runtime_service.py`、`app/api/v1/id_mapping.py`
- 后端镜像 alembic heads = `['0138']`
- 前端 bundle = `assets/index-BKgf5Vyo.js`；`localhost:5173/api/v1` 绝对地址 0 命中（同源相对路径）
- 两镜像均为 `linux/amd64`；后端镜像内 `/app/.env` 不存在（未把 dev 凭据烤进镜像）
- 构建偏差（唯一）：本机无法拉取 `docker/dockerfile:1.7`（`# syntax=` 指令），后端使用去掉
  首行 syntax 指令的 Dockerfile 副本构建，其余逐字一致，构建层语义不变

## 3. 本批上线内容

相对生产上一版（1030 发版）：

- 后端：#80 L4 token 取证、#81 L4 冷启动、#82 L4 工具结果瘦身、#83 L4 system 角色、
  #84 报告内嵌分节图 + L4 SQL 回显、#86/#87 id_mapping 权限与契约、#88 id_mapping
  回填脚本 + 真实数据门禁、#89 供货量词条 + Oracle 自然月模板、#90 季度/半年时间口径
  确定性解析
- 前端：#84 图表渲染（单轮/多步图表进最终报告、多类别可读性、PNG 导出、流式门控）

无新环境变量；`/opt/qa-system/docker/.env` 未改动（SECRET_KEY 复用）。

## 4. 生产预检实测：term_dictionary

2026-10-31 只读实测（生产 4 行，**id 与开发库整体 +2，不能按 id 核对**）：

| id | term | mapped_class_name | mapped_property_name | formula_hint 关键内容 |
|---|---|---|---|---|
| 5 | 外协供应商 | `DIM_SUPPLIER` | `BPSNUM_0` | 外协不计入生产供货量；检查 `TCLCOD_0 IN ('A02','A03','A04','A05')` |
| 6 | 生产型物料 | `DIM_IMATERIAL` | `TCLCOD_0` | `TCLCOD_0 IN ('A02','A03','A04','A05')` |
| 7 | 供货量 | `DWD_GOODS_RECEIPT_DTL` | `RCV_QTY_PUU` | JOIN `DIM_IMATERIAL.ITMREF_0 = DTL.ITEM_CODE`；日期列 `DTL.RCV_DATE`；`SUM(DTL.RCV_QTY_PUU)`；**不含 `DWD_GOODS_RECEIPT_LINE`** |
| 8 | 外部供应商贸易 | `DWD_GOODS_RECEIPT_DTL` | `INTER_COM_CODE` | `INTER_COM_CODE='1' AND INTER_SITE_CODE='1'`（含例外规则） |

**结论**：生产「供货量」已指向存活类 DTL，类/属性/`formula_hint` 三处均正确，
Phase 5.1（词条漂移修复）**整段跳过，无生产写入**。

> 踩坑记录：runbook 初稿按开发库写 `WHERE id=5`，在生产把「外协供应商」误当作供货量。
> 后续所有词条核对一律按 `term` 名定位。

## 5. 发版步骤（实际执行摘要）

```text
Phase 1 预检    compose config / alembic_version / term_dictionary / 磁盘 / 现役镜像
Phase 2 备份    pg_dump → /root/backup/qa_metadata_pre_1031_MMDD.sql
                备份 tag：qa-backend:pre-1031 / qa-system_frontend:pre-1031
Phase 3 传输    scp 两包 → sha256 比对 → docker load → retag 到生产现役名
Phase 4 迁移    qa_compose stop backend
                qa_compose run --rm backend alembic upgrade head → 0138
Phase 5 数据    term_dictionary 核对：已正确，跳过（无写入）
Phase 6 起服务  qa_compose up -d backend frontend
                docker exec qa-backend python scripts/seed_menu_config.py（幂等）
Phase 7 验收    见下
```

## 6. 验收结果（用户实测全部通过）

| # | 项 | 判据 |
|---|---|---|
| ① | 迁移版本 | `alembic_version` = `0138` |
| ② | 0138 两列 | `session_message.query_plan` / `steps_snapshot` 存在 |
| ③ | 词条 | `供货量` = `DWD_GOODS_RECEIPT_DTL / RCV_QTY_PUU`，`formula_hint` 无 LINE |
| ④ | 健康 / 路由 | `/api/v1/health` 200；`POST /api/v1/auth/login` 422（路由+校验正常） |
| ⑤ | 新代码 | 容器内存在 `/app/app/services/time_scope_resolver.py` |
| ⑥ | 前端 bundle | `index-BKgf5Vyo.js` |
| ⑦ | UI 冒烟 | 半年口径问题日期边界由服务端解析；多步图表进最终报告且多类别可读；供货量问题落 DTL 不落 LINE |

## 7. 回滚锚点

- 镜像：`qa-backend:pre-1031` / `qa-system_frontend:pre-1031`（retag 回现役名后 `up -d`）
- 数据库：`/root/backup/qa_metadata_pre_1031_MMDD.sql`
- 0138 仅新增两个可空 JSONB 列，对旧数据无破坏

## 8. 备注

- 本批不带 agent-orchestration worker（生产未启用灰度，与 1030 一致）。
- 镜像内 `backend/app/tests/**` 含本地测试库口令 `qa_pg_dev_2026`（历史镜像同样存在，
  为 dev 凭据、非生产口令），不在本批范围，按安全规则记录一笔。
- 生产 `term_dictionary` 的 seed 代际与开发库不同（生产 4 行、id 5–8；开发 4 行、id 3–6），
  对比一律按 `term` 名。
