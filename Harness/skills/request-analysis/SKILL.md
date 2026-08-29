---
name: request-analysis
description: 分析需求，拆解为可实现的验收标准与任务
---

# 需求分析技能

## 输入
用户需求描述 + 相关设计稿（docs/）+ 现有 wiki。

## 输出
- 验收标准（Given/When/Then）
- 涉及组件清单
- 数据模型变更
- 接口契约变更
- 风险与依赖

## 步骤
1. 读 `Harness/wiki/architecture.md` 与对应 Phase wiki。
2. 拆解功能点，明确输入/输出/边界。
3. 列验收标准（正常 + 边界 + 异常）。
4. 识别需新建/修改的文件（按 `工程结构.md` 分层）。
5. 标注安全敏感点（需 security-reviewer）。
