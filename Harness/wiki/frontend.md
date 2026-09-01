---
created: 2026-09-01
updated: 2026-09-01
---

# Frontend

> 前端架构说明（施工中）。

## 菜单架构（feat-menu-hierarchy）

### 数据流
```
AppLayout mount → fetch /api/v1/menu-config → MenuConfig (DB)
                                              ↓
                            Ant Design Menu + SubMenu
```

### 兜底
- API 失败 → console.warn + setUseFallback(true) → 渲染 fallbackNav.ts
- openKeys 持久化：localStorage["menu.openKeys"]

### 预留字段
- permissionCode: string | null
- roles: string[]

本期不消费这两字段；后续接入 acl_service 时启用。
