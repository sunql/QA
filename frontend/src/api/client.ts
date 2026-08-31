import axios, { AxiosError, AxiosInstance, AxiosResponse } from "axios";
import { message } from "antd";
import {
  API_BASE_URL,
  REQUEST_TIMEOUT_MS,
  DEFAULT_TENANT_ID,
  DEFAULT_USER_ID,
} from "../config";
import type { ApiResponse } from "../types/common";
import { i18n } from "../i18n";

// 创建带默认配置的 axios 实例
export function createHttpClient(): AxiosInstance {
  const instance = axios.create({
    baseURL: API_BASE_URL,
    timeout: REQUEST_TIMEOUT_MS,
    headers: {
      "Content-Type": "application/json",
      "X-Tenant-Id": DEFAULT_TENANT_ID,
      "X-User-Id": DEFAULT_USER_ID,
    },
  });

  // 响应拦截：解包 ApiResponse 信封，失败时抛出 Error
  instance.interceptors.response.use(
    (response: AxiosResponse<ApiResponse<unknown>>) => {
      const body = response.data;
      if (body && typeof body === "object" && "success" in body) {
        if (!body.success) {
          const errMsg = body.error ?? i18n.t("errors.requestFailed");
          void message.error(errMsg);
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
      const errMsg =
        apiError ??
        (status
          ? i18n.t("errors.requestFailedHttp", { status: String(status) })
          : i18n.t("errors.networkError"));
      void message.error(errMsg);
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
