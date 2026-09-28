## 状态
APPROVED

## 验证
- 死代码清理是否彻底: YES — `SqlGuardViolationError = SqlSafetyError` 是唯一引用，删除后全库 grep 无残留
- 22/22 PASS: YES — 相关测试 16 passed（nl2sql/agent_tools），failing test `test_cache_hasCode_returns_false_before_warmUp` 为预存失败，与此次改动无关
- revert 决策合理: YES — `fetchall()[0]` 用于字符串列元组解包（line 61），`scalars().all()` 用于 ORM 模型集合（lines 100, 202），二者用途不同，原版正确

## 残留发现
无

## 理由
单 commit 3 行删除，精准移除死代码别名。删除后无任何残留引用，未改变任何业务逻辑，未引入新问题。diff 干净，审查通过。
