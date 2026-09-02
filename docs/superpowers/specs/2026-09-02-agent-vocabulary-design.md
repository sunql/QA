# Agent 词表治理（小改动：data_domains / data_layers）

> 日期：2026-09-02
> 类型：后端 + 前端 + 校验；零 Alembic 迁移、零新表
> 关联：`Harness/changes/feat-agent-vocabulary/`（执行期创建）

## Context（为什么做）

`AgentDefinition.data_domains` / `data_layers` 是 JSONB 数组（models.py:1231-1235），当前**没有任何词表约束**——前端 `<Select mode="tags">`（AgentRegistryPage.tsx:457-462）允许用户输入任意字符串，新写入的"垃圾词"全部静默落库。

`data_layers` 已有部分兜底：`_normalizeDataLayer`（schemas.py:1930）在写入边界做 strip+upper，**但域完全没有归一化也没有词表**。结果：

- 同一语义 `procurement` / `PROCUREMENT` / `  Procurement ` 在库内并存，列表过滤靠内存比对即出现偏差。
- `data_layers` 因为运行时分层授权校验依赖严格大写词（`_normalizeDataLayer` 把 `feature` 错误地变成 `FEATURE` 但与 `tool.data_layers` 已声明的 `FEATURE` 匹配，所以暂未爆雷），自由输入等于**把授权语义隐藏起来**。
- 列表过滤 `GET /agents?dataDomain=X`（agent_registry_service.py:93）只能按精确字符串匹配，错一个字就漏。

**目标**：用代码常量锁死合法词表 + DTO 写入边界严格 422 校验 + `GET /agents/options` 下发选项，让前端从自由输入改成约束多选。

**前后分离**：Agent 元数据 `data_layers`（展示/授权用，DIM/DWD/FEATURE）与 `LineageLayer`（enums.py:144，数据血缘用，7 层 SOURCE_SYSTEM/ODS/.../AI）是**两套词汇**——同名 DWD 是巧合，本期不混。

## 目标

1. **词表 SSOT 在后端**：`AGENT_DATA_DOMAINS = ("PROCUREMENT", "QUALITY", "LOGISTICS")`、`AGENT_DATA_LAYERS = ("DIM", "DWD", "FEATURE")` —— 代码常量、零 DB 表。
2. **DTO 写入边界严格**：新写入的 `data_domains` / `data_layers` 元素必须 ∈ 词表，否则 422 + `details.allowed / details.rejected`。
3. **归一化**：`data_domains` 加 strip+upper（与既有 `_normalizeDataLayer` 对齐），空字符串拒收。
4. **选项下发**：`GET /api/v1/agents/options` 返回 `{domains: [...], layers: [...]}`，前端从接口取下拉数据。
5. **列表过滤扩展**：补 `dataLayer` 单值 Query 参数；`dataDomain` 改同名重复绑定支持多值（前端多选下拉 → `?dataDomain=A&dataDomain=B`）。
6. **不迁移历史不规范行**：继续读、继续展示；但新写入必走校验。

## 非目标（YAGNI）

1. **不建字典表**——域/层不在"运营可配置"范围，按代码常量管理即可。
2. **不暴露工具列表给 `/agents/options`**——大改动（Agent 工具绑定可配置化）排期后单独做，本期仅词表治理。
3. **不修改 `_normalizeDataLayer` 的归一化方向**——已有 strip+upper；不引入 lower-case、trim 标点等更激进规则。
4. **不改 `LineageLayer` 枚举**——属于血缘领域，混入会污染授权语义。
5. **不写数据迁移脚本**——历史不规范行保留（详见 §6 取舍）。

## 架构

### 数据 / 接口

```
┌──────────────────────────────────────────────────────────┐
│ AgentDefinition.data_domains / data_layers （JSONB 数组） │
│   - schema 保持；零迁移                                   │
└──────────────────────────────────────────────────────────┘
                       ▲
                       │ 严格校验（DTO field_validator）
                       │
┌──────────────────────────────────────────────────────────┐
│ AGENT_DATA_DOMAINS / AGENT_DATA_LAYERS （代码常量）       │
│   - app/domain/agent_vocabulary.py                        │
│   - 词表 SSOT                                            │
└──────────────────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│ GET /api/v1/agents/options                               │
│   - 返回 { domains: [...], layers: [...] }              │
│   - 路由必须在 /{agent_code} 之前注册（防 options 被吞） │
└──────────────────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│ 前端 useAgentOptions() hook                              │
│   - 拉一次 /agents/options，缓存到内存                   │
│   - 新建/编辑/详情/策略/列表过滤 五处共用                │
└──────────────────────────────────────────────────────────┘
```

### 写入边界

```python
# app/domain/agent_vocabulary.py（新）
AGENT_DATA_DOMAINS: tuple[str, ...] = ("PROCUREMENT", "QUALITY", "LOGISTICS")
AGENT_DATA_LAYERS:  tuple[str, ...] = ("DIM", "DWD", "FEATURE")

# app/domain/schemas.py（修改）
from app.domain.agent_vocabulary import AGENT_DATA_DOMAINS, AGENT_DATA_LAYERS

def _normalizeAgentDomain(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("data_domains 元素不允许为空字符串")
    return normalized

class AgentDefinitionCreate(CamelModel):
    data_domains: list[str] = Field(default_factory=list)
    data_layers:  list[str] = Field(default_factory=list)

    @field_validator("data_domains")
    @classmethod
    def _check_domains(cls, v: list[str]) -> list[str]:
        normalized = [_normalizeAgentDomain(x) for x in v]
        rejected = [x for x in normalized if x not in AGENT_DATA_DOMAINS]
        if rejected:
            raise ValueError(MSG_AGENT_DOMAIN_NOT_IN_VOCAB.format(
                value=",".join(rejected), allowed=",".join(AGENT_DATA_DOMAINS)
            ))
        # 顺序保留 + 去重（前端多选可能产生重复）
        seen: set[str] = set()
        deduped: list[str] = []
        for x in normalized:
            if x not in seen:
                seen.add(x); deduped.append(x)
        return deduped

    @field_validator("data_layers")
    @classmethod
    def _check_layers(cls, v: list[str]) -> list[str]:
        normalized = [n for n in (_normalizeDataLayer(x) for x in v if x is not None) if n]
        rejected = [x for x in normalized if x not in AGENT_DATA_LAYERS]
        if rejected:
            raise ValueError(MSG_AGENT_LAYER_NOT_IN_VOCAB.format(
                value=",".join(rejected), allowed=",".join(AGENT_DATA_LAYERS)
            ))
        seen: set[str] = set()
        deduped: list[str] = []
        for x in normalized:
            if x not in seen:
                seen.add(x); deduped.append(x)
        return deduped
```

`_check_layers` 复用既有 `_normalizeDataLayer`；`AgentDefinitionUpdate.data_layers: list[str] | None` 同样加同模式 validator（None 表示字段未传，跳过）。

### 错误消息

```python
# app/domain/error_messages.py（新）
MSG_AGENT_DOMAIN_NOT_IN_VOCAB = (
    "data_domains 含未授权值 '{value}'；合法集：{allowed}。"
    "如需新增域，请在 Agent 注册表中按业务约定调整 AGENT_DATA_DOMAINS。"
)
MSG_AGENT_LAYER_NOT_IN_VOCAB = (
    "data_layers 含未授权值 '{value}'；合法集：{allowed}。"
    "Agent 层词表仅含 DIM/DWD/FEATURE；血缘层 LineageLayer 不在此范围内。"
)
```

FastAPI 默认把 Pydantic `field_validator` 抛出的 `ValueError` 包装为 `RequestValidationError`，响应体是 `{"detail": [{"loc": [...], "msg": "...", ...}]}` —— **与项目既有 `{success, error, details}` 错误信封不同**（项目自己的 `ValidationError` 经全局 DomainError handler 才有 `details`）。

**本期取舍（YAGNI）**：不引入新的全局 RequestValidationError handler，词表校验失败的 422 就走 FastAPI 默认格式。错误消息文本（`MSG_AGENT_DOMAIN_NOT_IN_VOCAB`）已直接说明合法集，前端拦截 422 解析 `detail[*].msg` 即可展示，不强制对齐 envelope。若后续多处需要统一，可在独立 change 把 RequestValidationError → 信封的 handler 补上。

### API

**新增** `GET /api/v1/agents/options`（`backend/app/api/v1/agents.py`）：

```python
class AgentOptionsRead(CamelModel):
    domains: list[str]
    layers:  list[str]

@router.get("/options", response_model=AgentOptionsRead)
async def getAgentOptions(...) -> AgentOptionsRead:
    return AgentOptionsRead(
        domains=list(AGENT_DATA_DOMAINS),
        layers=list(AGENT_DATA_LAYERS),
    )
```

路由注册位置：必须在 `/{agent_code}` 之前，否则 "options" 会被路径参数吞掉。

**修改** `GET /api/v1/agents`：补 `dataLayer` Query 参数（与 `dataDomain` 单值并存）；`dataDomain` 改同名重复绑定支持多值（FastAPI Query 同名多次出现聚合为 `list[str] | None`，无需新增参数名）。`AgentRegistryService.listAgents` 加 `data_layer: str | None` 与 `data_domains: list[str] | None`，DB 侧分别用 JSONB `contains([x])` 与 `overlap([...])`。

### 前端

| 位置 | 现状 | 改动 |
|---|---|---|
| AgentRegistryPage 新建/编辑 Modal | `<Select mode="tags" placeholder="PROCUREMENT/FEATURE" />` | 改 `<Select mode="multiple" options={...} />`，选项来自 `useAgentOptions()` |
| 详情抽屉 dataDomains/dataLayers 展示 | 标签列表 | 不动；只读展示沿用 |
| 策略子表 dataLayer 字段 | `<Input placeholder=...>`（自由输入） | 改 `<Select options={...} />`（**单值，不是多选**——`AgentAccessPolicy.data_layer` 是单值字符串字段） |
| 列表页过滤 | 仅 status 过滤 | 加 `<Select mode="multiple" options={...} />` 域过滤；前端把多选值拼接为 `dataDomain=A&dataDomain=B`（FastAPI Query 同名多次绑定为 `list[str]`，服务端 `AgentRegistryService.listAgents(data_domains=[...])` 用 JSONB `overlap` 一次性 OR 过滤） |
| `/agents/options` HTTP client | 无 | 新增 `getAgentOptions()`、`useAgentOptions()` hook |

i18n：zh-CN / en-US 加 `agentOptions.title`、`agentOptions.fetchFailed`、`agentRegistry.filter.domainPlaceholder`、`agentRegistry.filter.layerPlaceholder`、`agentRegistry.fields.dataDomainRule`、`agentRegistry.fields.dataLayerRule`。

### 数据迁移 / 取舍

**历史不规范行的处理**：
- 库内若存在 `data_layers=["procurement"]`、`data_layers=["feature"]` 等非规范词 → 列表仍能读、详情仍能展示；但**新建/更新/编辑时**，只要触碰这些行（编辑其 `data_layers`）就会要求新值 ∈ 词表。
- 不做批量数据迁移脚本（YAGNI + 安全语义：宁可暂时有混合数据，不可静默改业务数据）。
- 在变更日志与可能的 release notes 里告知：未来若需要清理，可单独发一次性脚本。

**与既有 seed 兼容**：
- `seed_agents.py` 中所有 `domains`/`layers` 全部已在词表中（PROCUREMENT/FEATURE/DIM/DWD）→ 无需改 seed。

## 关键文件清单

**新增**
- `backend/app/domain/agent_vocabulary.py`（词表常量 + 模块 docstring 解释与 LineageLayer 的边界）
- `backend/app/tests/unit/test_agent_vocabulary.py`（normalize / 词表校验 / 去重 / 空字符串拒绝）
- `backend/app/tests/integration/test_agent_options_api.py`（GET /agents/options 集成）
- `frontend/src/api/agentOptions.ts`（HTTP 客户端）
- `frontend/src/hooks/useAgentOptions.ts`（React hook）
- `frontend/src/tests/useAgentOptions.test.tsx`（hook 单测）

**修改**
- `backend/app/domain/schemas.py`（`AgentDefinitionCreate/Update` 加 field_validator + 复用既有 `_normalizeDataLayer`；新增 `AgentOptionsRead`）
- `backend/app/domain/error_messages.py`（新增 2 条 MSG）
- `backend/app/api/v1/agents.py`（新增 `GET /options`；`listAgents` 路由补 `dataLayer` Query，`dataDomain` 改多值）
- `backend/app/services/agent_registry_service.py`（`listAgents` 加 `data_layer` 与 `data_domains: list[str]` 参数；JSONB contains/overlap 过滤）
- `frontend/src/pages/AgentRegistryPage.tsx`（4 处 Select 改造 + 域过滤 + useAgentOptions hook 接入）
- `frontend/src/i18n/zh-CN.ts` / `frontend/src/i18n/en-US.ts`（新 key）

## 验证

```bash
# 后端定向（词表校验）
cd backend
uv run pytest app/tests/unit/test_agent_vocabulary.py -v

# 后端集成（GET /agents/options + 422 校验）
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_options_api.py \
    app/tests/integration/test_agent_registry_api.py -v

# 前端 hook / 单测
cd ../frontend
npx vitest run src/tests/useAgentOptions.test.tsx src/i18n/i18n.test.ts

# 全量回归 + 覆盖率门槛
cd ../backend
TEST_DATABASE_URL=... uv run pytest app/tests/ --cov=app --cov-fail-under=80

# 前端 lint + build（前端构建进 nginx 镜像）
cd ../frontend
npx tsc --noEmit && npx vitest run
docker compose -f ../docker/docker-compose.yml up -d --build frontend
```

## 风险与权衡

| 项 | 风险 | 缓解 |
|---|---|---|
| 严格 422 | 第三方脚本/集成直调 REST 写入未授权值会失败 | 错误消息明确指引合法集；前端从 `/agents/options` 取值，UI 路径天然不会触发 |
| 历史不规范数据 | 列表展示里出现"奇怪"标签 | 不强制迁移；只新写入校验；可在 release notes 提示"如发现请反馈清理" |
| 词表硬编码 | 后续新增域/层要发版 | 当前 2-3 个域、3 个层规模极小，发版成本远低于建字典表的运维成本；YAGNI 原则保留扩展路径 |
| 路由顺序 | `/options` 被 `/{agent_code}` 吞 | 显式注释路由顺序；集成测试覆盖 `GET /agents/options` 不被吞 |
| 列表过滤 `dataDomain` 多值 | 现状 `dataDomain` 单值；要不要扩？ | 改同名重复绑定多值 + JSONB `overlap`；前端多选下拉直接对应；不新增参数名（FastAPI Query 重复同名聚合）|

## 与大改动（Agent 工具绑定可配置化）的关系

- 本期**不引入** Agent→工具的运营配置能力——工具绑定仍硬编码于 `AGENT_TOOLS` dict（agent_tools.py:287）。
- 本期**为**大改动铺路：`/agents/options` 接口返回结构可扩展 `tools: [...]` 字段；前端 `useAgentOptions()` hook 可演进为 `useAgentMetadata()`。
- 两改动解耦：无单向依赖，大改动可独立排期。