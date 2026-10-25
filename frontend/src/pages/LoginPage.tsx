import { useState } from "react";
import { useNavigate, useLocation, Navigate } from "react-router-dom";
import { Form, Input, Button, Checkbox, message } from "antd";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "../stores/authStore";
import type { AppError } from "../api/client";

export default function LoginPage() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const loc = useLocation();
  const token = useAuthStore((s) => s.token);
  const login = useAuthStore((s) => s.login);
  const [submitting, setSubmitting] = useState(false);

  if (token) {
    const from = (loc.state as { from?: string } | null)?.from ?? "/";
    return <Navigate to={from} replace />;
  }

  const onFinish = async (values: { username: string; password: string; rememberMe: boolean }) => {
    setSubmitting(true);
    try {
      await login(values.username, values.password, values.rememberMe ?? false);
      const state = useAuthStore.getState();
      if (state.mustChangePassword) {
        message.info(t("userMenu.changePasswordHint"));
        nav("/change-password", { replace: true });
        return;
      }
      const from = (loc.state as { from?: string } | null)?.from ?? "/";
      nav(from, { replace: true });
    } catch (e: unknown) {
      // 拦截器 reject 的是 normalizeError 产出的 AppError（code/status/detail，
      // **无 .response**；fix/login-401-white-screen，2026-10-25）。
      // 旧实现读 e.response.data.detail.code —— AppError 上不存在该形状，
      // 登录失败永远落到 networkError 分支弹「网络异常」。
      // 后端登录失败（用户不存在 / 密码错 / 账号禁用）统一 401 防枚举
      // （AuthFailedError → HTTP 401），因此登录接口上 status=401 即
      // 「账号或密码错误」；429 = 限流；其余按网络异常兜底。
      const appErr = e as Partial<AppError> | null;
      if (appErr?.status === 401) {
        message.error(t("auth.login.invalidCredentials")); // 账号或密码错误
      } else if (appErr?.status === 429) {
        message.error(t("auth.login.rateLimited")); // 请求过于频繁，请稍后再试
      } else {
        message.error(t("auth.login.networkError"));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--bg-base, #0f1e2e)" }}>
      <div style={{ width: 400, padding: 32, background: "var(--bg-container, #152838)", borderRadius: 4 }}>
        <h2 style={{ textAlign: "center", marginBottom: 24 }}>{t("auth.login.title")}</h2>
        <Form layout="vertical" onFinish={onFinish}>
          <Form.Item name="username" label={t("auth.login.username")} rules={[{ required: true }]}>
            <Input autoFocus autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label={t("auth.login.password")} rules={[{ required: true }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="rememberMe" valuePropName="checked" initialValue={false}>
            <Checkbox>{t("auth.login.rememberMe")}</Checkbox>
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={submitting}>
            {t("auth.login.submit")}
          </Button>
          <div style={{ marginTop: 16, textAlign: "center", color: "var(--text-secondary, #888)" }}>
            {t("auth.login.forgot")}
          </div>
        </Form>
      </div>
    </div>
  );
}
