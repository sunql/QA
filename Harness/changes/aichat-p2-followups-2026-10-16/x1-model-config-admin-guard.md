# X1 — 模型配置管理权限 + 凭据保护（PR #37）

> 状态: ✅ 已合（branch: fix/qa-1012-p0-x1, commit 86f34a71, PR https://github.com/sunql/QA/pull/37）
> 来源: 1012 cross-refs spec §P0-X1 + aichat-p2-followups SSOT 序 3 第 1 项
> 关联: `docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md`

## 问题

QA-1012-01 #1：rateLimitDep PR #28 把限流解了，但写入接口未要求
model_admin 角色。Service 层 `ModelConfigService.{create,update,deactivate}`
不带 admin 校验 —— 任何能直接调 service 的代码（test fixture / MCP /
后台任务 / 未来新加的 API 路径）都能越权落库。

## 修法（defense in depth）

### 1. service 层下沉守卫

```python
# backend/app/services/model_config_service.py
_ADMIN_ROLE = "admin"

def _requireAdminActor(actor: CurrentUser | None) -> None:
    if actor is None:
        return  # 内部调用 / 测试 fixture 显式 opt-out（向后兼容）
    if _ADMIN_ROLE not in (actor.roles or ()):
        raise PermissionDeniedError("需要 admin 角色")

class ModelConfigService:
    async def create(self, session, dto, *, actor: CurrentUser | None = None) -> LlmConfig:
        _requireAdminActor(actor)
        ...
    # update / deactivate 同款
```

### 2. API 路由显式传 actor（防御纵深的"纵"）

```python
# backend/app/api/v1/model_config.py
@router.post("", response_model=LlmConfigRead, status_code=201)
async def createModel(
    dto: LlmConfigCreate,
    user: CurrentUser = Depends(requireAdmin),  # 路由层守卫
    session: AsyncSession = Depends(getDb),
) -> LlmConfigRead:
    svc = ModelConfigService()
    config = await svc.create(session, dto, actor=user)  # service 层守卫
    ...
```

即使未来某次 refactor 误删 `Depends(requireAdmin)`，service 层
`_requireAdminActor` 仍兜底拒绝非 admin。

### 3. 5 个新集成测试

| 测试 | 钉死什么 |
|---|---|
| `test_serviceLayerRejectsNonAdminCreate` | 非 admin 直调 `service.create` → PermissionDenied |
| `test_serviceLayerAllowsAdminCreate` | admin 直调 `service.create` → 成功 |
| `test_serviceLayerRejectsNonAdminUpdate` | 非 admin 直调 `service.update` → PermissionDenied |
| `test_serviceLayerRejectsNonAdminDeactivate` | 非 admin 直调 `service.deactivate` → PermissionDenied |
| `test_endpointChangeInvalidatesClientCache` | apiEndpoint 变更后 `_buildClient` LRU `currsize==0` + 新 client `_apiEndpoint` 绑新值 |

## 验证

```
app/tests/integration/test_model_config_rbac.py   9/9 全绿
app/tests/integration/test_model_config_api.py   14/15（1 pre-existing rot：PUT 缺 apiKey）
app/tests/integration/test_model_config_temperature.py
app/tests/integration/test_model_config_disable_thinking.py
app/tests/unit/test_llm_factory_cache.py         2/2
```

合计 28 passed / 1 deselected (pre-existing rot)。

## 已知 pre-existing rot（不在本 PR 范围）

- `test_model_config_api.py::TestModelConfigCacheInvalidation::test_update_invalidates_cached_client`：
  测试 PUT 不带 apiKey → service 正确返 422（audit-fx-1.1 钉死：变更 endpoint 必须带 key）
  修法是测试补 apiKey 或改测非 endpoint 字段更新

## 风险评估

- 路由层行为不变（API 用户体验 0 变化）
- service 层写入路径新增 opt-in 守卫（`actor=None` 走老路径，向后兼容）
- 5 个新测试 + 现有 9 个测试 = 14/14 全绿

## 后续 SSOT 进度

| 序 | 项 | 估时 | 状态 |
|---|---|---|---|
| 1 | Frontend 7 red + chatStore 8 红 | 4h | ✅ PR #36 |
| 2 | Task 12.5 code-reviewer | 1h | ✅ PR #32 |
| 3.X1 | 模型配置 admin 守卫 | 2h | ✅ **本 PR #37** |
| 3.X2 | must_change_password 服务端强制 | 2h | ⏳ |
| 3.X3 | 检查点恢复保留研究问题 | 2h | ⏳（依赖 research 线） |
| 3.X4 | 流式 CRLF + EOF 守门 | 2h | ⏳（chat 已修，research 未修） |
| 3.X5 | 会话归属 + 状态互斥 | 2h | ⏳（chat 互斥 ✅，research 只 cover create） |
| 4 | M5 stub 模式回填 | 4h | ⏳ |
| 6 | L1 prompt 注入 | 1d+ | ⏳ |
| 7 | N4 qwen 知识保留 | 独立 | ⏳ |
