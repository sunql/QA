---
name: unit-test-ci
description: 覆盖率验证与 CI 门禁
---

# 覆盖率验证技能

## 命令
```bash
# 后端
cd backend
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80

# 前端
cd frontend
npm run test:coverage
```

## 门禁规则
- 覆盖率 < 80% -> CI 失败，阻断合并。
- 关注 `term-missing` 输出的未覆盖行，补测试或标注 `pragma: no cover`。

## 排除
- `app/tests/*`、`app/alembic/*` 不计入覆盖。
- 纯数据类、`if TYPE_CHECKING` 可排除。

## 新增代码要求
新功能 PR 必须附带测试，新增行覆盖率应接近 100%。
