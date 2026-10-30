import type { Vars } from "../../../i18n";

/** 翻译函数签名：i18next 的 t 允许带插值变量。 */
export type Translate = (key: string, vars?: Vars) => string;

/** 后端把 axios 错误归一化成 AppError{code,status,detail}，没有 .response。 */
interface StatusError extends Error {
  status?: number;
}

/** 取 HTTP 状态码：拒绝值可能不是 Error，不能直接取属性。 */
export function errorStatus(error: unknown): number | undefined {
  return error instanceof Error ? (error as StatusError).status : undefined;
}

/**
 * 取后端给出的诊断文本。AppError.message 的来源优先级：响应 detail 对象里的 message
 * → 响应体顶层 error → axios 兜底文案（见 api/client.ts 的 normalizeError）。
 */
export function errorDetailText(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "";
}

/**
 * 把后端错误翻译成用户可读的提示。只按 status 分流——AppError.code 对所有
 * DomainError 都是 "UNKNOWN"，不可用于判别。
 */
export function describeError(t: Translate, error: unknown): string {
  const status = errorStatus(error);
  const detail = errorDetailText(error);
  if (status === 404) return t("agentTask.messages.runNotFound");
  if (status === 403) return t("agentTask.messages.forbidden");
  if (status === 409) return t("agentTask.messages.conflict");
  if (status === 422) {
    return detail
      ? t("agentTask.messages.validationFailed", { detail })
      : t("agentTask.messages.validationFailedGeneric");
  }
  if (status === undefined || status === 0) {
    return t("agentTask.messages.networkError");
  }
  return t("agentTask.messages.requestFailed", { message: detail });
}
