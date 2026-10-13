import axios, { AxiosError, AxiosInstance, AxiosResponse } from "axios";
import type { MessageInstance } from "antd/es/message/interface";
import { API_BASE_URL, REQUEST_TIMEOUT_MS, DEFAULT_TENANT_ID } from "../config";
import type { ApiResponse } from "../types/common";
import { i18n } from "../i18n";
import { useAuthStore } from "../stores/authStore";

// ---------------------------------------------------------------------------
// AppError interface — unified error shape for frontend error handling
// ---------------------------------------------------------------------------

/** Unified error shape: code + status + detail, extends Error. */
export interface AppError extends Error {
  code: string;
  status: number;
  detail?: unknown;
}

/**
 * Normalize any AxiosError into an AppError.
 *
 * Extracts `detail.code` from the backend response (Task 1.2 / 2.1 error code
 * definitions) so downstream consumers (e.g. LoginPage) can branch on the code
 * rather than parsing raw message strings.
 *
 * 401 side effects (localStorage.clear + redirect) are preserved from Task 2.1.
 *
 * `fallbackMessage` 承接调用方（拦截器）**已经算好**的文案：响应信封里的
 * `error`（字符串）与字符串形态的 `detail` 都归它管。不传时退化为 `err.message`。
 * 少了这条兜底，同一个 404 会变成 toast 说「未找到」、而 `AppError.message`
 * 却是「Unknown error」—— 下游按 message 分流的页面拿到的是后者。
 */
export function normalizeError(
  err: AxiosError<ApiResponse<unknown>>,
  fallbackMessage?: string
): AppError {
  const status = err.response?.status ?? 0;
  const rawDetail = (err.response?.data as { detail?: unknown } | undefined)?.detail;
  const detail = rawDetail ?? null;
  const code =
    typeof rawDetail === "object" && rawDetail !== null && "code" in rawDetail
      ? String((rawDetail as { code: unknown }).code)
      : "UNKNOWN";
  // 文案优先级：对象形态 `detail.message`（后端给的最具体文案）> 调用方传入的
  // fallbackMessage（拦截器已算好的 toast 文案——信封 `error` 或字符串 `detail`）
  // > axios 自身的 `err.message` > 兜底串。
  // 注意「同源」不是全局不变量：当后端同时给了信封 `error` 与对象 `detail.message`
  // 时，toast 显示前者、此处取后者，两者仍是两条不同文案（刻意如此——后者更具体）。
  const message =
    typeof rawDetail === "object" && rawDetail !== null && "message" in rawDetail
      ? String((rawDetail as { message: unknown }).message)
      : fallbackMessage ?? err.message ?? "Unknown error";

  // Object.assign to a fresh Error — same pattern as brief GREEN
  const appErr: AppError = Object.assign(new Error(message), { code, status, detail });

  // 401: clear token + redirect (Task 2.1 — preserved)
  if (status === 401) {
    localStorage.removeItem("token");
    window.location.href = "/auth/login";
  }

  return appErr;
}

// ---------------------------------------------------------------------------
// Message API holder
// ---------------------------------------------------------------------------

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
      config.headers = config.headers ?? {};
      config.headers.Authorization = `Bearer ${token}`;
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
      // 直接 setState 而非 logout()：拦截器是同步路径，不需要异步调 API
      // audit-fx-2.1：同时清理 localStorage（持久层），防止过期 token 被 persist 恢复
      if (status === 401) {
        localStorage.removeItem("token");
        useAuthStore.setState({
          token: null,
          user: null,
          mustChangePassword: false,
        });
      }

      const errMsg =
        apiError ??
        (typeof apiDetail === "string" ? apiDetail : undefined) ??
        (status
          ? i18n.t("errors.requestFailedHttp", { status: String(status) })
          : i18n.t("errors.networkError"));
      // 401 不弹 toast —— 由 RequireAuth 或登录页处理
      if (status !== 401) {
        showError(errMsg);
      }
      // audit-fx-3.3: use normalizeError to produce AppError with code + status + detail
      // 文案复用上面那份 errMsg —— 否则会出现「弹窗说未找到、页面拿 Unknown error」
      // 这对自相矛盾的用户提示。唯一例外见 normalizeError 内注释：后端同时给了信封
      // `error` 与对象 `detail.message` 时，message 取更具体的那条。
      const normalized = normalizeError(error, errMsg);
      return Promise.reject(normalized);
    }
  );

  return instance;
}

// 默认单例
export const httpClient = createHttpClient();
// 别名：feat-user-auth 的 auth.ts / authStore.ts 习惯用 apiClient；
// 旧 API（feature/kpi/dq 等）继续用 httpClient，指向同一实例。
export const apiClient = httpClient;
