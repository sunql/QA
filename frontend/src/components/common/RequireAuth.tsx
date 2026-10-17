import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuthStore } from "../../stores/authStore";

/** X2 (QA-1012-01 #3 关联前端)：must_change_password 时允许访问的白名单路径。 */
export const ALLOWED_WHEN_MUST_CHANGE = ["/auth/change-password", "/auth/login"];

/** 判断 pathname 是否在白名单内（精确匹配 + 子路径，前缀边界防误判）。

    关键边界：用 ``p + "/"`` 而不是 ``p`` 拼接做 startsWith ，
    否则 ``/auth/change-password-v2`` 会被 ``/auth/change-password`` 误判命中。
    这是 audit-fx-1.3 后端 ``ALLOWED_PATHS_WHEN_MUST_CHANGE`` 的前端镜像。
*/
export function isAllowedWhenMustChange(pathname: string): boolean {
  return ALLOWED_WHEN_MUST_CHANGE.some(
    (p) => pathname === p || pathname.startsWith(p + "/")
  );
}

export default function RequireAuth() {
  const token = useAuthStore((s) => s.token);
  const mustChangePassword = useAuthStore((s) => s.mustChangePassword);
  const location = useLocation();

  if (!token) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }

  if (mustChangePassword && !isAllowedWhenMustChange(location.pathname)) {
    return <Navigate to="/auth/change-password" replace />;
  }

  return <Outlet />;
}
