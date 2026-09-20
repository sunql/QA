import axios, { AxiosError, AxiosInstance, AxiosResponse } from "axios";
import type { MessageInstance } from "antd/es/message/interface";
import { API_BASE_URL, REQUEST_TIMEOUT_MS, DEFAULT_TENANT_ID } from "../config";
import type { ApiResponse } from "../types/common";
import { i18n } from "../i18n";
import { useAuthStore } from "../stores/authStore";

/**
 * antd message 实例的 holder。
 *
 * 拦截器在 module load 时就生效，不在 React 树里 —— 静态 ``import { message }
 * from "antd"`` 拿不到 App 上下文（动态主题），会在控制台报
 * "Static function can not consume context like dynamic theme"。
 *
 * 解法：把 ``messageApi`` 留成 holder，由 ``<App>`` 内部调 ``App.useApp()``
 * 拿到真实实例后，通过 ``setMessageApi`` 注入。Holder 在 App 挂载前是
 * ``null``，期间不弹提示以避免静默丢错；首屏请求靠调用方自己处理 toast。
 */
let messageApi: MessageInstance | null = null;

export function setMessageApi(api: MessageInstance): void {
  messageApi = api;
}

function showError(content: string): void {
  if (messageApi) {
    void messageApi.error(content);
  }
}

/**
 * 给非 httpClient 路径（裸 axios.postForm 等）用的 toast 出口。
 * 这些路径不经过拦截器，要自己手动弹错。
 */
export function showMessageError(content: string): void {
  showError(content);
}

// 创建带默认配置的 axios 实例
export function createHttpClient(): AxiosInstance {
  const instance = axios.create({
    baseURL: API_BASE_URL,
    timeout: REQUEST_TIMEOUT_MS,
    headers: {
      "Content-Type": "application/json",
      "X-Tenant-Id": DEFAULT_TENANT_ID,
    },
  });

  // 请求拦截：注入 Authorization: Bearer <token>（feat-user-auth）
  // token 不存在时不注入 —— stub 模式 / 登录页 / 公开端点。
  instance.interceptors.request.use((config) => {
    const token = useAuthStore.getState().token;
    if (token) {
      config.headers.set("Authorization", `Bearer ${token}`);
    }
    return config;
  });

  // 响应拦截：解包 ApiResponse 信封，失败时抛出 Error
  instance.interceptors.response.use(
    (response: AxiosResponse<ApiResponse<unknown>>) => {
      const body = response.data;
      if (body && typeof body === "object" && "success" in body) {
        if (!body.success) {
          const errMsg = body.error ?? i18n.t("errors.requestFailed");
          showError(errMsg);
          return Promise.reject(new Error(errMsg));
        }
        return { ...response, data: body.data };
      }
      return response;
    },
    (error: AxiosError<ApiResponse<unknown>>) => {
      const status = error.response?.status;
      const apiError = error.response?.data?.error;
      const apiDetail = (error.response?.data as { detail?: unknown } | undefined)?.detail;

      // 401：token 失效 / 未登录 → 清 store 触发重定向到 /login
      // 不弹 toast（避免每次跳转都弹"登录已失效"，已经准备重定向了）
      if (status === 401) {
        useAuthStore.getState().clear();
      }

      const errMsg =
        apiError ??
        (status
          ? i18n.t("errors.requestFailedHttp", { status: String(status) })
          : i18n.t("errors.networkError"));
      // 401 不弹 toast —— 由 RequireAuth 或登录页处理
      if (status !== 401) {
        showError(errMsg);
      }
      // 携带领域异常 detail（如 NL2SQL 校验差异），供错误消息折叠展示
      // 携带 HTTP status，便于业务页面按状态分流（如 403 → 权限提示 Modal）
      const err = new Error(errMsg) as Error & { detail?: string; status?: number };
      if (typeof apiDetail === "string") {
        err.detail = apiDetail;
      }
      if (typeof status === "number") {
        err.status = status;
      }
      return Promise.reject(err);
    }
  );

  return instance;
}

// 默认单例
export const httpClient = createHttpClient();
