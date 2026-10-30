import axios, { AxiosError, AxiosInstance, AxiosResponse } from "axios";
import type { MessageInstance } from "antd/es/message/interface";
import { API_BASE_URL, REQUEST_TIMEOUT_MS, DEFAULT_TENANT_ID } from "../config";
import type { ApiResponse } from "../types/common";
import { i18n } from "../i18n";
import { useAuthStore } from "../stores/authStore";
import { clearLastSessionIds } from "../stores/persistChatUiState";
import { notifyUserSwitch } from "../stores/userSwitch";

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
 * 纯归一化：401 的登录态清理（localStorage + authStore）由响应拦截器负责，
 * 掉线重定向由 RequireAuth 软跳转负责，登录失败提示由 LoginPage toast 负责。
 * （fix/login-401-white-screen：旧实现在此处 window.location.href 硬跳
 * "/auth/login"——不存在的路由白屏，且页面卸载吃掉 LoginPage 的错误提示。）
 */
export function normalizeError(err: AxiosError<ApiResponse<unknown>>): AppError {
  const status = err.response?.status ?? 0;
  const rawDetail = (err.response?.data as { detail?: unknown } | undefined)?.detail;
  const detail = rawDetail ?? null;
  const code =
    typeof rawDetail === "object" && rawDetail !== null && "code" in rawDetail
      ? String((rawDetail as { code: unknown }).code)
      : "UNKNOWN";
  // 后端 ErrorResponse(CamelModel) 的业务文案放在响应体顶层 `error`（字符串），
  // `detail` 恒为 null —— 只读 detail 会把「工具不存在：X」这类诊断丢掉，
  // 页面只能拿到 axios 的 "Request failed with status code 422"。
  // 取值优先级：detail 对象里的 message → 响应体顶层 error → 网络异常兜底 → axios 兜底文案。
  // 网络异常（status=0，即没有拿到任何 HTTP 响应）必须落到「网络异常，请稍后重试」：
  // toast 已经是这个文案，若 reject 的 Error 里换成 axios 兜底/Unknown error，
  // 页面 catch 后展示的文案就与 toast 不一致（2026-10-30 修复）。
  const bodyError = (err.response?.data as { error?: unknown } | undefined)?.error;
  const message =
    typeof rawDetail === "object" && rawDetail !== null && "message" in rawDetail
      ? String((rawDetail as { message: unknown }).message)
      : typeof bodyError === "string" && bodyError
        ? bodyError
        : status === 0
          ? i18n.t("errors.networkError")
          : err.message ?? "Unknown error";

  // Object.assign to a fresh Error — same pattern as brief GREEN
  const appErr: AppError = Object.assign(new Error(message), { code, status, detail });

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

/** 401 但并非会话过期的凭证校验接口：改密接口的 401 = 旧密码校验失败，
 *  用户 token 仍有效——清登录态会把正在改密的人踢回 /login 且提示丢失
 *  （fix/login-401-white-screen 后续，2026-10-25）。由调用页自己 toast。 */
const CREDENTIAL_CHECK_URLS = ["/auth/me/password"];

function isCredentialCheckRequest(url: string | undefined): boolean {
  return CREDENTIAL_CHECK_URLS.some((u) => (url ?? "").includes(u));
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
      // 例外：凭证校验接口（改密）的 401 = 旧密码错，token 仍有效，不清登录态
      // 不弹 toast（避免每次跳转都弹"登录已失效"，已经准备重定向了）
      // 直接 setState 而非 logout()：拦截器是同步路径，不需要异步调 API
      // audit-fx-2.1：同时清理 localStorage（持久层），防止过期 token 被 persist 恢复
      if (status === 401 && !isCredentialCheckRequest(error.config?.url)) {
        localStorage.removeItem("token");
        useAuthStore.setState({
          token: null,
          user: null,
          mustChangePassword: false,
        });
        // 掉线也算换人：内存里的对话与恢复指针都属于那个已失效的登录会话。
        // 与 logout() 同口径，避免「此路径少清一处」的纵深缺口。
        clearLastSessionIds();
        notifyUserSwitch();
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
      const normalized = normalizeError(error);
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
