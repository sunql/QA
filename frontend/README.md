# 智能问答系统 - 前端

独立 React + TypeScript + Vite 应用，使用 Ant Design 构建 UI，ECharts 渲染图表。

## 技术栈

- React 18.3 + TypeScript 5.5 + Vite 5.4
- Ant Design 5.20（UI 组件）
- ECharts 5.5（图表，Phase 3+ 使用）
- Zustand（状态管理）
- axios（HTTP 客户端，统一响应信封解包）
- react-router-dom 6（路由）
- Vitest + Testing Library（单元测试，覆盖率 80%）

## 目录结构

```
frontend/
├── src/
│   ├── api/            # axios 客户端与各领域 API 封装
│   ├── components/      # 通用组件（布局等）
│   ├── pages/          # 路由页面
│   ├── types/          # TypeScript 类型定义（与后端 camelCase JSON 对齐）
│   ├── tests/          # 单元测试
│   ├── App.tsx         # 路由根
│   ├── main.tsx        # 应用入口
│   └── config.ts       # 全局配置
└── ...
```

## 开发

```bash
cd frontend
npm install        # 安装依赖
npm run dev        # 启动开发服务器（默认 5173，代理 /api 到后端 8000）
npm run build      # 构建生产包
npm run test       # 运行测试
npm run test:coverage  # 覆盖率报告（阈值 80%）
```

## 环境变量

可选通过 `.env.local` 配置：

```
VITE_API_BASE_URL=/api/v1   # API 基地址，默认 /api/v1（通过 Vite 代理转发到后端）
```

## 模块状态

- **Phase 1**：模型配置管理（CRUD，功能完整）、会话用量查询
- Phase 2-5：本体管理、智能问答（NL2SQL + 图表）、多数据源 —— 页面占位中
