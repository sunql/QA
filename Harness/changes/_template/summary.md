# 变更 SSOT 模板

> 复制此目录为 `changes/<feature-name>/summary.md`，按阶段填写。
> **强约束**：必须满足 [变更记录强制规范](../../rules/变更记录强制规范.md) §一 的 5 条，缺任一项视为部署阻塞。

---

# 变更：<feature-name>

- **日期**：YYYY-MM-DD
- **作者**：Claude / <姓名>
- **Phase**：Phase X.Y <模块名>
- **状态**：draft / in-review / done / superseded（superseded 时必须在第 9 段指出由谁替代）
- **关联变更**：[feat-predecessor](../feat-predecessor/summary.md)（如有）
- **迁移版本**：0069_xxx, 0074_xxx（如有 alembic 迁移，文件名 ≤ 32 字符）
- **MEMORY**：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/<slug>.md](...)

---

## 1. 需求
<背景与目标，验收标准>
> 用用户视角描述「为什么做」和「做完是什么样」。避免「我以为」式表达。

## 2. 设计评审
<方案选择、多视角意见、最终决定>
> 至少 2 个候选方案 + 取舍表 + 最终决定。Bug fix 段可简化为「根因 + 修复方案」。

## 3. 数据模型变更
<新增/修改的表、列、本体节点；迁移脚本说明>
> 含迁移文件名（≤ 32 字符校验）、CheckConstraint 变化、索引（含部分索引）。

## 4. 接口契约变更
<新增/修改的 API、DTO 字段>
> 含 HTTP 方法 + 路径 + 请求/响应字段；状态机变化（如有）单独列段。

## 5. 实现要点
<关键文件、算法、依赖>
> 关键文件路径 + 关键算法步骤 + 依赖注入点（FastAPI Depends 等）。

## 6. 测试
<新增测试用例、覆盖率结果>
> 列出测试用例文件名 + 关键场景 + 覆盖率（≥ 80%）。

## 7. 安全审查
<是否触发 security-reviewer，结果>
> 触发条件：认证、密钥、SQL Guard、用户输入、外部 API、文件操作、加解密、支付。
> 结果：CRITICAL（必修）/ HIGH（合前修）/ MEDIUM（TODO）/ LOW（NOTE）。

## 8. 部署验证
<冒烟结果>
> docker compose 启动 + 端点冒烟（`curl /openapi.json | jq '.paths | keys'`）+ 真实数据验证脚本输出（参考 [开发流程规范 §真实数据验证](../../rules/开发流程规范.md#每轮真实数据验证开发门禁)）。

## 9. 关联
- 设计稿：`docs/设计0X.md`
- Wiki：`Harness/wiki/<>.md`
- Rules：`Harness/rules/<>.md`
- Memory：`~/.claude/projects/.../memory/<slug>.md`
- 关联变更（predecessor / successor）：`../feat-xxx/summary.md`

---

## SSOT 校验清单（合并前必查）

- [ ] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [ ] 9 段都非空，无 TBD/TODO/待补 占位
- [ ] 第 2 段 ≥ 2 个候选方案对比
- [ ] 第 3 段迁移文件名 ≤ 32 字符
- [ ] 第 7 段触发了 security-reviewer 则必填 CRITICAL/HIGH/MEDIUM/LOW 至少 1 项
- [ ] 第 8 段 docker compose 冒烟命令 + 输出贴出
- [ ] 第 9 段 ≥ 3 个跨文件链接（关联变更 / Wiki / Rules / Memory 中至少 3 个）
- [ ] 相关 wiki 文档已更新（数据模型 / 接口 / 架构 / 前端 至少 1 处）
- [ ] 至少 1 条 MEMORY 索引已在 `~/.claude/projects/.../memory/MEMORY.md` 添加
- [ ] 涉及真实 SQL/DB 改动时 `scripts/<feature>_realdata.py` 已跑通

> 上述任一项未打勾 → code-review 打回 → 不允许合。
