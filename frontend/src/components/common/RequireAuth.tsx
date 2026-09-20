// 路由守卫（feat-user-auth，2026-09-20）
//
// 包裹任何需要登录的页面：无 token 或 401 后未恢复 → 跳 /login。
// 守卫不渲染内容（占位空白），由 <Navigate> 触发重定向。
//
// 不在这里做"已登录但 mustChangePassword"的拦截 —— 那是业务逻辑（强制跳
// /change-password），交给 LoginPage 内部 setLoginResult 后跳转。

import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { selectIsAuthenticated, useAuthStore } from "../../stores/authStore";

interface RequireAuthProps {
  children: ReactNode;
}

export function RequireAuth({ children }: RequireAuthProps): ReactNode {
  const isAuthed = useAuthStore(selectIsAuthenticated);
  const location = useLocation();
  if (!isAuthed) {
    // 把当前路径带过去 —— 登录成功后可以回跳
    return (
      <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
    );
  }
  return children;
}
