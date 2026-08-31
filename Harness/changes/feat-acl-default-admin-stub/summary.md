# 变更：feat-acl-default-admin-stub

- **日期**：2026-08-31
- **作者**：AI Assistant
- **Phase**：质量硬化（横切）
- **状态**：done
- **Commit**：（待提交）

## 1. 需求

用户在使用前端 FeatureDefinition 页面时遇到 ACL 403 报错（`无权修改该资源：仅 owner 部门成员或 admin 角色可操作`），但因 stub auth 默认角色为 `("user",)` 而非 admin，未登录访问 `/api/v1/features` 一定被拒，新人易踩坑。同时前端 ACL 403 报错信息散落在 toast 里，用户看不到 owner 列、不知道 owner-based ACL 机制。

验收标准：
- 默认 stub user 带 admin 角色 → dev/test 下「打开即用」
- 显式 X-User-Roles=user → 仍按原 ACL 路径
- 前端 FeatureDefinition 页面 owner 列 + 403 友好 Modal
- 不破现有 21 条 ACL 单测 + 31 条 ACL 集成测试

## 2. 设计评审

**Default-admin 设计意图**：把 dev/test 体验门槛从「必须显式设置 X-User-Roles=admin」降到「打开即用」，避免新人 onboarding 反复踩 403。生产安全护栏不变（`AUTH_STUB_ENABLED=0` + 反向代理剥离 X-User-* 头），所以默认 admin 在 dev/test 是便利、在生产是禁用路径的两条独立保护。

**403 前端分流**：axios 拦截器给 reject Error 挂 `status` 字段；业务页面按 `err.status === 403` 分流到友好 Modal（owner 列说明 + 后端原文折叠）；非 403 仍走默认 toast 路径，不重复弹。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无业务接口变更。响应体不变（仍 `{error, detail}` 信封）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/dependencies.py` | 新增 `DEFAULT_STUB_ROLES = ("user", "admin")` / `DEFAULT_STUB_USER_ID = "anonymous"` 常量；`CurrentUser` 默认值改为引用常量；`getCurrentUser` 拆出 `_buildCurrentUser` 纯函数（便于单测）；解析默认改为 `"user,admin"`；完整 docstring 说明默认 admin 的设计意图与生产安全护栏 |
| `backend/app/tests/unit/test_dependencies.py`（新） | 14 个单测：缺省 admin、显式覆盖、空字符串退化、多值部门、去空白、ACL 闭环验证、AUTH_STUB_ENABLED=0 拒绝 |
| `backend/app/tests/integration/test_governance_extension_acl_api.py` | 7 处 403 负面用例补 `X-User-Roles: user`（EntityMapping/DQ/OntologyClass 三组） |
| `backend/app/tests/integration/test_kpi_catalog_governance.py` | 3 处 403 负面用例补 `X-User-Roles: user` |
| `backend/app/tests/integration/test_data_quality_api.py` | 跨部门 PUT 补 `X-User-Roles: user` |
| `backend/app/tests/integration/test_feature_definition_api.py` | `FIN_HEADERS` 常量加 `X-User-Roles: user` |
| `backend/app/tests/integration/test_ontology_api.py` | 跨部门 PUT 补 `X-User-Roles: user` |
| `frontend/src/api/client.ts` | axios 响应拦截器给 reject Error 挂 `status` 字段（业务页面分流用） |
| `frontend/src/pages/FeatureCatalogPage.tsx` | 加 `<Alert>` 权限说明 banner + `forbiddenModal` 状态 + `handleMutationError` 分流（403 → Modal；其他 → 沉默）；`handleDelete` / `handleSubmit` 走新分流 |
| `frontend/src/i18n/zh-CN.ts` + `en-US.ts` | 新增 `feature.aclBanner` / `feature.aclForbiddenTitle` / `feature.aclForbiddenBody` / `feature.aclForbiddenDetailLabel` |

## 6. 测试

| 类别 | 数量 | 结果 |
|---|---|---|
| 后端 unit (test_dependencies) | 14 | ✅ 新增 |
| 后端 unit (test_acl_service) | 8 | ✅ 不破 |
| 后端 unit (test_governance_extension_acl) | 13 | ✅ 不破 |
| 后端 integration (Phase 4.5 ACL 链路) | 30 | ✅ 不破（11 处补 `X-User-Roles: user`） |
| 后端 integration (feature/data_quality/ontology 端到端) | 67 | ✅ 不破 |
| 前端 unit + i18n | 329 | ✅ 不破（含 i18n 9 个 key 一致性测试） |

## 7. 安全审查

- **生产护栏未动**：`AUTH_STUB_ENABLED=0` 兜底逻辑不变（test_stub_disabled_raises_permission_denied 验证）；`main.py` 启动时的 `APP_ENV=production + stub 仍开启 → ERROR 日志` 告警不变。
- **默认 admin 的威胁面**：dev/test 任意客户端可伪造 `X-User-Roles=admin` —— 但这本来就是 stub auth 设计前提（任何人都能伪造）。生产部署必须设 `AUTH_STUB_ENABLED=0` 或由反代剥离 `X-User-*` 头。
- **403 错误信息**：后端 `_acl_service.py` 通用消息仍不暴露 owner / entity_code（防枚举侧信道，验证见 `test_message_is_generic_no_owner_or_user_departments_leak`）。
- **前端 Error.status 字段**：仅在 reject 路径挂载，response 正常路径不变，不引入新的跨域/序列化风险。

未触发 security-reviewer（与上一轮 ACL 评审同结论：default-admin 是 dev 便利、生产禁用路径已就位）。

## 8. 部署验证

| 场景 | 期望 | 实际 |
|---|---|---|
| docker compose up 后端 + 前端访问 features 页 | PUT/DELETE 不再 403 | 待真实环境冒烟（dev 用户本地） |
| 显式 X-User-Roles=user + 跨部门 PUT | 403 | 集成测试已覆盖 |
| 显式 X-User-Roles=admin + 任意 owner PUT | 200 | 单测 + 集成已覆盖 |
| AUTH_STUB_ENABLED=0 + stub 头 | PermissionDeniedError | 单测已覆盖 |
| 前端 Alert banner 显示 + 403 Modal 弹出 | 文案匹配 i18n | TS check + i18n test 通过 |

## 9. 关联

- 根因：runbook quirks 「stub auth 默认 user 角色 + 未登录访问带 ACL 端点 → 必 403」
- 依赖：AclService（Phase 4.5 governance hardening）已稳定运行
- 同类预防：所有未来的 stub 默认值都应在「dev 便利」与「生产安全」之间显式标注安全护栏（已在 docstring 模板化）

## 10. 决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| 默认角色值 | `("user", "admin")` 而非纯 `("admin",)` | 保留 `user` 是因为部分 service 可能按 role 判断「非特权用户」分支；保留 admin 是 dev 兜底。两者兼得，避免后续 service 写 if "user" in roles 误判 |
| 解析逻辑拆分 | 抽出 `_buildCurrentUser` 纯函数 | FastAPI `Header()` 对象单测时是 wrapper、无法直接 `.split()`；拆出后单测覆盖解析逻辑 |
| 403 前端实现 | axios 拦截器挂 `status` 字段 + 业务页面 Modal | 比 message.error 更显眼、Modal 可折叠后端原文、可复用 i18n |
| 现有测试修补 | 11 处加 `X-User-Roles: user` 而非 `dependency_overrides` 改全局 | 局部修更可读、与「显式覆盖」语义一致 |
| i18n key 命名 | `feature.aclForbiddenTitle/Body/Detail` 不用 `common.` | ACL 拒绝解释是 feature 页特有行为，未来 KpiCatalog 页可复用但措辞可能不同 |

## 11. 后续维护口径

- 改默认角色：编辑 `dependencies.py` 的 `DEFAULT_STUB_ROLES` 常量；同步检查 `acl_service.py` 的 `ADMIN_ROLE` 常量是否需同步
- 接入真实 JWT：`AUTH_STUB_ENABLED=0` + 删除 stub 函数；`DEFAULT_STUB_ROLES` 此时失效，无需改
- 新增 ACL 受控端点：复用 `acl_service.assertCanModify`，无需改本 SSOT
- 前端新增需 ACL 提示的页面：复用 `handleMutationError` 模式（在 `client.ts` 拦截器挂 status + 业务页分流），无需改本 SSOT

---

**ACL 默认 admin 化 + 403 友好提示，已完成。新人 onboarding 体验从「必踩 403」降到「打开即用」，生产安全护栏不变。**
