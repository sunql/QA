---
name: coding-skill
description: 分层实现规范（API/Service/Domain/Infrastructure/Test）
---

# 编码技能

按分层实现新功能，每层职责清晰：

## 分层实现顺序
1. **Domain**：`domain/models.py`（ORM）、`enums.py`、`schemas.py`（DTO）、`exceptions.py`。
2. **Infrastructure**：DB、LLM 客户端、Token 计数、安全工具。
3. **Service**：业务编排、事务边界、调 infrastructure。
4. **API**：路由、依赖注入、DTO 序列化。
5. **Test**：单元（纯逻辑）、集成（API+DB）、E2E。

## 规范
- 遵守 `项目编码规范.md`：camelCase 函数/变量，PascalCase 类型，UPPER_SNAKE 常量；ORM/Pydantic 字段 snake_case。
- 不可变：领域逻辑返回新对象。
- 显式错误处理：服务层转领域异常。
- 单文件 ≤ 800 行，函数 < 50 行，嵌套 ≤ 4 层。

## 示例
新增一个 service 方法：
1. 先在 `tests/unit/test_<svc>.py` 写失败测试（AAA 结构）。
2. 实现最小代码通过。
3. 重构。
