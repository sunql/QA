# 变更：fix-embedding-provider-type-guard

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：评估剩余项批次 **Phase B**（H7）
- **状态**：done
- **关联变更**：同批 [fix-chat-disconnect-persistence](../fix-chat-disconnect-persistence/summary.md)、[fix-llm-transient-retry](../fix-llm-transient-retry/summary.md)、[chore-l3-deadcode-and-prior-cte-contract](../chore-l3-deadcode-and-prior-cte-contract/summary.md)、[fix-milvus-list-all-pagination](../fix-milvus-list-all-pagination/summary.md)、[test-kpi-match-cache-ordering](../test-kpi-match-cache-ordering/summary.md)
- **迁移版本**：无（`provider_type` 列已在 `0012`；**刻意不新增 CHECK 约束**，理由见 §3）
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.2 **H7**（评估日期 2026-09-25）
- **commit**：`d278822`

---

## 1. 需求

两条**静默腐化**路径，都在 embedding provider 解析上：

1. **env 回退分支绕过维度守卫**：`embedding_provider_factory.py` 的 env 回退路径在维度守卫**之前**就 `return` 了 —— 部署方把 `EMBEDDING_MODEL` 换成一个（输出维度不同的）模型时，**没有任何报错**：写入 Milvus 时才在检索侧表现为「搜不到 / 结果离谱」，且与向量库已有 1024 维数据混写。
2. **未知 `provider_type` 静默按 OpenAI 兼容处理**：`provider_type` 列此前是**死元数据**（工厂从不读）。配置写错、或将来接入原生协议的服务时，表现是难以定位的调用错误（把「不支持」伪装成「网络/鉴权问题」）。

用户口径（binding）：**守卫 + 未知类型 fail-fast，不发明无法验证的协议** —— 即不假装支持 ollama/omlx 的原生协议，只把「只支持什么」显式化。

**验收标准**：① env 回退路径的维度守卫与 registry 路径**同口径**（声明值不符 → `ConfigError`）；② 未知/空 `provider_type` → `ConfigError`，消息给出被拒值与已知集合；③ 已声明的合法配置行为不变；④ **不发明**无法验证的协议支持；⑤ 未声明维度的情形**不猜测**、但要留痕。

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 只在 registry 路径加 `provider_type` 校验 | 否决：env 回退是**真正无声**的那条路（无元数据可比对），漏掉它等于只修了看得见的一半 |
| B | env 路径也去猜模型维度（维护「模型名 → 维度」表） | **否决**：没有可靠映射（同名不同版本、代理端点改写都会变）。**猜错会拦下正确部署**，比不猜更糟 ⇒ 未声明时只记 warning 并**显式说出这条路没有守卫** |
| C | **维度守卫提升为两条路径共用 + 已知集合校验 `provider_type`**（选定） | 一处守卫、两种声明来源（`embedding_provider.dimension` / `EMBEDDING_DIMENSION`），消息里带 `source` 便于定位是哪条路 |
| D | 给 `provider_type` 加 DB 级 CHECK 约束 | 否决：会与**前端枚举**（`types/embeddingProvider.ts`：`ollama|omlx|openai_compatible`）形成两处 SSOT，加值时必然漂移；校验放应用层 + 写进 SSOT |
| E | 顺手实现 ollama/omlx 原生客户端 | 否决（用户口径）：**不发明无法验证的协议** —— 没有可验证的端点与协议文档，实现出来是猜的 |

### 已知集合的来源与限制

`KNOWN_PROVIDER_TYPES = ('ollama', 'omlx', 'openai_compatible')` 取自**前端下拉枚举**（用户可见的取值集合），因此前后端一致；消息文案显式提示「前端下拉与后端 `KNOWN_PROVIDER_TYPES` 需同步扩展」。

**客户端构造仍只支持 OpenAI 兼容形态**：`ollama` / `omlx` 通过其 OpenAI 兼容端点工作。该限制写入模块 docstring 与 message 文案 —— 这是「不发明协议」的落地方式（承认限制，而不是假装支持）。

## 3. 数据模型变更

无迁移。`provider_type` 列已在 `0012`；**刻意不加 CHECK 约束**（理由见 §2 方案 D）。校验在应用层，两处 SSOT 的同步责任写进 SSOT 与本记录。

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `KNOWN_PROVIDER_TYPES`（模块常量） | 新增；与前端 `types/embeddingProvider.ts` 同集合 |
| `_assertProviderTypeKnown(provider)` | 新增：值不在已知集合、或为空（列非空但历史/直写可能留空）→ `ConfigError`，消息含被拒值、已知集合、provider id/name |
| `_assertDimensionMatches(*, name, declaredDimension, source)` | 由 registry 专用提升为**两条路径共用**；新增 `source` 形参区分「registry 列」/「env 变量」，便于运维定位 |
| `_assertEnvFallbackDimension()` | 新增：声明了 → 走上面的共用守卫；未声明 → `logger.warning`（**不猜测**） |
| `config.py` | `embeddingDimension: int | None = Field(default=None, alias="EMBEDDING_DIMENSION")` |
| `.env.example` | 增加 `# EMBEDDING_DIMENSION=1024` 注释行（部署方可见的自声明入口） |
| 文案 | `MSG_EMBEDDING_PROVIDER_TYPE_UNKNOWN`、`MSG_EMBEDDING_PROVIDER_DIMENSION_MISMATCH`（带 `source`） |

## 5. 实现要点

| 位置 | 改动 |
|---|---|
| `app/infrastructure/llm/embedding_provider_factory.py`（+95/-…） | `_assertProviderTypeKnown` 在**维度守卫之前**（类型未知时先报类型错，消息更可操作）；`_assertDimensionMatches` 提为共用；env 回退分支不再提前 `return`，改调 `_assertEnvFallbackDimension()`；模块 docstring 写明「客户端构造只支持 OpenAI 兼容形态」 |
| `backend/.env.example` / `app/config.py` | 新增 `EMBEDDING_DIMENSION` 声明位与注释 |
| `app/services/messages_zh.py` | 两条文案（含「前端下拉与后端需同步扩展」的提示） |
| `app/tests/unit/test_embedding_provider_factory.py`（+104 / 18 例） | 两条路径 × （相符 / 不符 / 未声明）、已知集合放行、未知/空/大小写/空白拒绝 |

## 6. 测试

- **单元 18 passed**（`test_embedding_provider_factory.py`）。
- **容器内真机探针（`/tmp/probe_batch.py` 的 H7 段，9 项全 PASS，2026-09-27，跑在部署物真实模块上）**：

  ```
  PASS  H7 已知集合非空  | ('ollama', 'omlx', 'openai_compatible')
  PASS  H7 未知 provider_type='bogus' fail-fast  | ConfigError
  PASS  H7 未知 provider_type='' fail-fast  | ConfigError
  PASS  H7 未知 provider_type='  ' fail-fast  | ConfigError
  PASS  H7 未知 provider_type='OPENAI' fail-fast  | ConfigError   ← 大小写不宽容（枚举语义）
  PASS  H7 已知 provider_type 全部放行
        Milvus 集合实际维度 = 1024
  PASS  H7 registry 路径维度不符 fail-fast  | ConfigError
  PASS  H7 registry 路径维度相符放行
        env EMBEDDING_DIMENSION = None（模型 bge-m3:latest）
  PASS  H7 env 未声明 → 不拦、不猜（记 warning）  | 无异常
  ```

- **运行时留痕实录**（env 未声明时那条 warning 的原样输出）：
  ```
  env 回退 embedding 路径未声明 EMBEDDING_DIMENSION，跳过维度守卫（模型 bge-m3:latest）；
  输出维度与 Milvus 集合 1024 不一致时只能在写入/检索侧报错
  ```
- **已知残留（如实登记，不在本批解决）**：当前部署（`bge-m3:latest`）**未声明** `EMBEDDING_DIMENSION` ⇒ env 回退路径的维度守卫**在生产现状下仍不生效**，只有 warning。要真正生效需部署方在 `.env` 里声明（`.env.example` 已给出样式）。这是「不猜测」口径的必然代价，已写进本记录与评估文档。

## 7. 安全审查

**未触发**（无认证、密钥、SQL、用户输入、加解密、支付变更）。两点说明：

- 本项**取消了一个宽松假设**（「未知类型 = OpenAI 兼容」），属收紧；
- `ConfigError` 消息含 provider `id` / `name` / 维度数值，均为运维内部标识，**不含** `api_key` 或密文（`_loadActiveProvider` 返回的 dict 里有 `api_key_encrypted`，守卫只读 `provider_type` 与 `dimension`，未进入任何日志/异常消息）。

## 8. 部署验证（2026-09-27）

- **部署**：`./scripts/deploy_backend.sh`（含 `alembic/`；本项无迁移）；仓库 ↔ 容器 md5 `app/infrastructure/llm/embedding_provider_factory.py`、`app/config.py`、`app/services/messages_zh.py` **MATCH**（本批 22/22 现存文件 MATCH）；
- **容器内真机探针 9 项全 PASS**（输出见 §6，含真实 Milvus 集合维度 1024 的读取）；
- **测试**：全量 unit+services **2 failed, 2522 passed, 1 skipped**（两条为**预存**失败，`git worktree add --detach` 在基线复现判别，delta = 0）；集成切片 120 passed + 3 例环境耦合（导出 `DATABASE_URL` 后 3/3 通过）；
- **静态检查**：本批 22 文件 ruff 与基线逐行一致（唯一 F841 为基线既有）；新增测试文件 `All checks passed`；
- **网关**：`/api/v1/health` 直连 8000 与经 nginx 5173 均 200。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.2 H7（标 ✅，含**已知残留**：生产未声明 `EMBEDDING_DIMENSION` ⇒ env 路径守卫仍不生效）、§15 批次记录
- 代码契约：`app/infrastructure/llm/embedding_provider_factory.py`（`KNOWN_PROVIDER_TYPES` / `_assertProviderTypeKnown` / `_assertDimensionMatches` / `_assertEnvFallbackDimension`）、`app/tests/unit/test_embedding_provider_factory.py`
- 前端同集合：`frontend/src/types/embeddingProvider.ts`（两处 SSOT 的同步责任写在 message 文案与本节）
- 规则：`Harness/rules/数据与AI治理*.md`（模型与嵌入配置治理）、根 `CLAUDE.md` 核心约束 #6（显式错误处理：不静默）
- Memory：`qa-system-*` 新增 H7 条目（维度守卫覆盖 env 回退 + 未知类型 fail-fast + 「不猜测模型维度」的取舍 + 生产仍缺声明这一残留）
- 同批：`../fix-chat-disconnect-persistence/`、`../fix-llm-transient-retry/`、`../chore-l3-deadcode-and-prior-cte-contract/`、`../fix-milvus-list-all-pagination/`、`../test-kpi-match-cache-ordering/`

## SSOT 校验清单

- [x] 第 1 段 需求：两条静默腐化路径（env 绕过守卫 / 死元数据 `provider_type`）+ 用户口径 + 5 条验收标准
- [x] 第 2 段 5 个候选方案（含**否决「猜模型维度」**与**否决 DB CHECK 约束**的理由）+ 已知集合来源 + 「客户端只支持 OpenAI 兼容」的显式承认
- [x] 第 3 段 无迁移 + 为何不加 CHECK（与前端枚举双 SSOT 漂移）
- [x] 第 4 段 接口契约：常量 / 两个守卫 / 共用化与 `source` 形参 / 配置与文案
- [x] 第 5 段 实现要点逐位置表（含守卫顺序：类型先于维度）
- [x] 第 6 段 测试：18 例 + 9 项容器探针实录 + warning 原文 + **已知残留**（生产未声明维度 ⇒ env 守卫仍不生效）
- [x] 第 7 段 安全审查：未触发 + 取消宽松假设 + 明确「密文不进消息/日志」
- [x] 第 8 段 部署验证：md5 MATCH + 探针 + 全量测试与预存失败判别 + ruff delta 0 + 网关 200
- [x] 第 9 段 跨文件链接（评估文档含残留 / 代码契约 / 前端同集合 / 规则 / Memory / 同批）
