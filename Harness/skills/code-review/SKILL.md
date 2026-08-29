---
name: code-review
description: 代码审查清单（FastAPI/React）
---

# 代码审查技能

## 检查清单
- [ ] 可读且命名清晰
- [ ] 函数 < 50 行，文件 < 800 行，嵌套 ≤ 4 层
- [ ] 错误显式处理（无静默吞异常）
- [ ] 无硬编码密钥/凭据
- [ ] 无 print/console.log 调试残留
- [ ] 新功能有测试，覆盖率 ≥ 80%
- [ ] 不可变：无原地修改（ORM 持久化例外）
- [ ] 输入校验在边界完成
- [ ] SQL 经 SQL Guard（业务查询）
- [ ] LLM 调用记录 Token 消耗

## 严重级别
- CRITICAL：安全漏洞/数据丢失 -> 阻断
- HIGH：Bug/显著质量问题 -> 合并前修
- MEDIUM：可维护性 -> 记录
- LOW：风格 -> 可选

## 安全触发
认证/输入处理/DB查询/文件/外部API/加密/SQL Guard 变更必须用 security-reviewer。
