# QA System Backend

FastAPI 后端：模型路由、Token 计量、本体管理、NL2SQL、图表渲染。

## 开发

```bash
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
uv run pytest --cov=app --cov-fail-under=80
```

详见上层 `../README.md` 与 `../Harness/wiki/`。
