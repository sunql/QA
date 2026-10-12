import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuthStore } from "../../stores/authStore";

const ALLOWED_WHEN_MUST_CHANGE = ["/auth/change-password", "/auth/login"];

function isAllowedWhenMustChange(pathname: string): boolean {
  return ALLOWED_WHEN_MUST_CHANGE.some((p) => pathname.startsWith(p));
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
