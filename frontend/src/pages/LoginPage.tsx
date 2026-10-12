import { useState } from "react";
import { useNavigate, useLocation, Navigate } from "react-router-dom";
import { Form, Input, Button, Checkbox, message } from "antd";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "../stores/authStore";

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
      // Extract detail.code for user-facing error messages (Finding 10 — audit-fx-3.3)
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      const code = typeof detail === "object" && detail !== null && "code" in detail
        ? String((detail as { code: unknown }).code)
        : null;

      if (code === "AUTH_INVALID" || code === "E_AUTH_INVALID_PASSWORD") {
        message.error(t("auth.login.invalidCredentials")); // 账号或密码错误
      } else if (code === "AUTH_EXPIRED" || code === "E_AUTH_EXPIRED") {
        message.error("会话已过期，请重新登录");
      } else if (code === "AUTH_RATE_LIMITED" || code === "E_AUTH_RATE_LIMITED") {
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
