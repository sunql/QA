import { useEffect } from "react";
import { useAuthStore } from "../stores/authStore";

/**
 * 刷新后补回登录用户身份。
 *
 * `authStore` 的 persist `partialize` 只存 `token`（user 属于敏感/易漂移数据，不落盘），
 * 所以直接刷新页面后 `user === null`。所有按角色收敛的写入口（词条字典、本体指标等）
 * 都读 `user.roles` —— 不补这一步，管理员刷新一次就看不到写入口。
 *
 * 失败语义：`/auth/me` 的 401 已由 `apiClient` 响应拦截器统一清登录态（store 清空 →
 * `RequireAuth` 跳登录），这里只吞错，不重复弹提示。
 */
export function useRestoreUser(): void {
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const fetchMe = useAuthStore((s) => s.fetchMe);

  useEffect(() => {
    if (token && !user) {
      void fetchMe().catch(() => {
        /* 拦截器已处理 401，此处不重复提示 */
      });
    }
  }, [token, user, fetchMe]);
}
