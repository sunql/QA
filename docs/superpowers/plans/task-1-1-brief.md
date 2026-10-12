# Task 1.1 Brief: 模型配置 RBAC + Endpoint URL 校验

> **Implementer 必读** —— 这是你唯一的需求源；不要回看 plan 文件做"补充"。如发现 brief 与 plan 不一致，**以 brief 为准**并报告冲突。

**Source plan**: `/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/.worktrees/fix-audit-2026-10-12/docs/superpowers/plans/2026-10-12-audit-fixes.md`
**Worktree**: `/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/.worktrees/fix-audit-2026-10-12/`
**Branch**: `fix/audit-2026-10-12`
**Audit ref**: `docs/QA-1012-01.md` Section 1（"高：普通用户可以管理全局模型配置"）
**Severity**: HIGH
**Commit prefix**: `[audit-fx-1.1]`
**Report path**: `/tmp/sdd-audit-2026-10-12/task-1-1-report.md`
**Ledger path**: `<worktree>/.superpowers/sdd/2026-10-12-audit-fixes/progress.md`

---

## Context（必读）

审计发现：普通登录用户（无 admin 角色）能创建/修改全局模型配置，并能改 `apiEndpoint` 不重提 key。修改 endpoint 时保留旧 key（密文），新客户端用旧 key 调新端点 → **凭据外发风险**。

你的任务：把 `/api/v1/model-configs` 的写接口（POST/PUT/DELETE）锁到 `admin` 角色，并在 endpoint 变更时强制要求新 key；同时新建 `url_validator` 阻断生产环境的 http/内网/loopback。

**前置依赖**：无（这是 task 1，后续 task 不阻塞你）。

---

## Files

**Modify:**
- `backend/app/api/v1/model_config.py` — POST/PUT/DELETE 加 `dependencies=[Depends(require_admin)]`
- `backend/app/api/v1/dependencies.py` — 新增 `require_admin` 工厂
- `backend/app/services/model_config_service.py` — `updateConfig` 接收新 key 才能改 endpoint
- `backend/app/infrastructure/llm/factory.py` — 创建客户端前调 `validateEndpointUrl`
- `backend/app/infrastructure/llm/openai_client.py` — endpoint 变更时重新绑定 key

**Create:**
- `backend/app/infrastructure/url_validator.py`
- `backend/app/tests/unit/test_url_validator.py`（5 用例）
- `backend/app/tests/integration/test_model_config_rbac.py`（3 用例）

---

## Step 1（RED）：写失败测试

### `backend/app/tests/unit/test_url_validator.py`

```python
import pytest
from app.infrastructure.url_validator import validateEndpointUrl


class TestValidateEndpointUrl:
    def test_https_production_allowed(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        assert validateEndpointUrl("https://api.openai.com/v1") is None

    def test_http_production_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="必须为 https"):
            validateEndpointUrl("http://api.openai.com/v1")

    def test_private_ip_production_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="私有地址"):
            validateEndpointUrl("https://10.0.0.1/v1")

    def test_loopback_production_blocked(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(ValueError, match="私有地址"):
            validateEndpointUrl("https://127.0.0.1/v1")

    def test_localhost_dev_allowed(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "development")
        assert validateEndpointUrl("http://localhost:1234/v1") is None
```

### `backend/app/tests/integration/test_model_config_rbac.py`

```python
import pytest
from httpx import AsyncClient
from app.main import createApp


@pytest.fixture
async def client(dbSession):
    app = createApp()
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_unprivileged_user_cannot_create_model(client, regularUserToken):
    headers = {"Authorization": f"Bearer {regularUserToken}"}
    resp = await client.post("/api/v1/model-configs", json={
        "provider": "openai", "name": "test", "modelId": "gpt-4o",
        "apiEndpoint": "https://api.openai.com/v1", "apiKey": "sk-test"
    }, headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_create_model(client, adminToken):
    headers = {"Authorization": f"Bearer {adminToken}"}
    resp = await client.post("/api/v1/model-configs", json={
        "provider": "openai", "name": "test2", "modelId": "gpt-4o",
        "apiEndpoint": "https://api.openai.com/v1", "apiKey": "sk-test"
    }, headers=headers)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_endpoint_change_requires_new_key(client, adminToken, existingModelConfig):
    headers = {"Authorization": f"Bearer {adminToken}"}
    resp = await client.put(f"/api/v1/model-configs/{existingModelConfig.id}", json={
        "apiEndpoint": "https://api.anthropic.com/v1"
        # 故意不带 apiKey
    }, headers=headers)
    assert resp.status_code == 422
    assert "apiKey" in resp.json()["detail"]
```

**Step 2 跑红**：

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/.worktrees/fix-audit-2026-10-12/backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  python -m pytest app/tests/unit/test_url_validator.py app/tests/integration/test_model_config_rbac.py -v
```

预期：8 个失败（url_validator 未导入、require_admin 未定义、endpoint 变更未校验 key）。

---

## Step 3（GREEN）：最小实现

### `backend/app/infrastructure/url_validator.py`

```python
"""端点 URL 校验：生产环境阻断 http + 私有 IP/loopback。"""
from __future__ import annotations
import ipaddress
import os
from urllib.parse import urlparse


def validateEndpointUrl(url: str) -> None:
    """通过则 None；不通过抛 ValueError。"""
    parsed = urlparse(url)
    env = os.getenv("APP_ENV", "development")
    if env != "production":
        return
    if parsed.scheme != "https":
        raise ValueError("生产环境 endpoint 必须为 https")
    host = parsed.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"生产环境 endpoint 禁止指向私有地址 {host}")
    except ValueError as exc:
        if "私有地址" in str(exc):
            raise
        # host 不是 IP，DNS 解析后再判（这里简化：trust 域名）
```

### `backend/app/api/v1/dependencies.py` 新增

```python
async def requireAdmin(user: User = Depends(getCurrentUser)) -> User:
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="需要 admin 角色")
    return user
```

### `backend/app/api/v1/model_config.py` 改写

所有 POST/PUT/DELETE route 加 `dependencies=[Depends(requireAdmin)]`（在签名里加 `dependencies=` 参数或 `Depends(requireAdmin)` 注入）。

### `backend/app/services/model_config_service.py` `updateConfig`

```python
async def updateConfig(config_id: int, data: ModelConfigUpdate) -> ModelConfig:
    cfg = await loadConfig(config_id)
    if data.apiEndpoint and data.apiEndpoint != cfg.api_endpoint:
        if not data.apiKey:
            raise ValueError("apiEndpoint 变更时必须同时提供新的 apiKey")
        validateEndpointUrl(data.apiEndpoint)
        cfg.api_endpoint = data.apiEndpoint
        cfg.api_key_cipher = encrypt(data.apiKey)
    # ... 其他字段更新
    await db.commit()
    return cfg
```

**Step 4 跑绿**：

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/.worktrees/fix-audit-2026-10-12/backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  python -m pytest app/tests/unit/test_url_validator.py app/tests/integration/test_model_config_rbac.py -v
```

预期：8 个全过 + 现有 19 个集成测试不回归。

**Step 5：跑全套测试**（防回归）：

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/.worktrees/fix-audit-2026-10-12/backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test \
TEST_NEO4J_URI=bolt://localhost:7688 \
  python -m pytest app/tests/ -v
```

**Step 6：Commit**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/.worktrees/fix-audit-2026-10-12
git add backend/app/api/v1/model_config.py \
        backend/app/api/v1/dependencies.py \
        backend/app/services/model_config_service.py \
        backend/app/infrastructure/url_validator.py \
        backend/app/infrastructure/llm/factory.py \
        backend/app/infrastructure/llm/openai_client.py \
        backend/app/tests/unit/test_url_validator.py \
        backend/app/tests/integration/test_model_config_rbac.py
git commit -m "[audit-fx-1.1] model config: require admin role + endpoint URL validation"
```

---

## Constraints（必读）

- **TDD 强制**：先写测试、跑红、写实现、跑绿、commit。不允许"先实现再补测试"。
- **真实 DB**：用 PostgreSQL 5434（`qa_metadata_test`），**禁 sqlite**。
- **不**触及 chat 侧 / R3 报告侧 / ontology / milvus schema。
- **不**对 prod 库跑 alembic。
- **不**改其他 10 项审计 finding（task 1.2-5.4 各自处理）。
- **不**新增 alembic 迁移（本 task 不动 schema）。
- **不**修改 `raw/` 目录。
- 函数 < 50 行；嵌套 ≤ 4；文件 < 800 行。
- 如发现 plan 与 brief 不一致，**以 brief 为准**并在 report 里说明。
- 如发现 plan/brief 与代码现状冲突（如已有 require_admin / 已有 url_validator），**先告诉我不要硬上**。

---

## Report Contract

写到 `/tmp/sdd-audit-2026-10-12/task-1-1-report.md`：

```markdown
# Task 1.1 Report

**Status**: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED
**Commit SHA**: <实际 commit 的 SHA>
**Test summary**: <N> unit / <M> integration passed; <K> failures
**Files changed**: <list>
**Concerns**: <free text, or "none">
**Conflict between brief and code reality**: <if any, else "none">
**Notes for reviewer**: <free text, or "none">
```

返回消息（写到 stdout）只含：
- Status
- Commit SHA
- 一行测试摘要
- 任何 concerns

---

## Ledger 更新

完成后追加一行到 `<worktree>/.superpowers/sdd/2026-10-12-audit-fixes/progress.md`：

```markdown
Task 1.1: complete — <commit SHA>
```

ledger 第一次创建时首行为 `# SDD ledger — plan: docs/superpowers/plans/2026-10-12-audit-fixes.md`。
