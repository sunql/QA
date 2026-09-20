// 登录页（feat-user-auth，2026-09-20）
//
// 独立布局（无 AppLayout）：居中 Card + 表单。提交 → POST /auth/login →
// 写 authStore → 跳 mustChangePassword ? /change-password : 原目标 / 默认首页。
//
// 不显式处理 401：后端失败统一抛 AuthFailedError，httpClient 拦截器弹 toast；
// 不需要再额外解析。

import { useEffect, useState, type FormEvent } from "react";
import { App, Button, Card, Checkbox, Form, Input, Typography } from "antd";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { authApi } from "../api/auth";
import { useAuthStore } from "../stores/authStore";
import { useTranslation } from "../i18n";

interface LocationState {
  from?: string;
}

export default function LoginPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const state = (location.state ?? {}) as LocationState;

  const token = useAuthStore((s) => s.token);
  const mustChangePassword = useAuthStore((s) => s.mustChangePassword);
  const setLoginResult = useAuthStore((s) => s.setLoginResult);

  const [submitting, setSubmitting] = useState(false);

  // 已登录 → 直接跳走（避免在登录页循环）。
  // 必须考虑 mustChangePassword：即使有 token，若用户待改密也强制跳 /change-password。
  // 否则会出现「已登录但留在首页 / 跳错页面」的怪状。
  useEffect(() => {
    if (!token) return;
    const target = mustChangePassword ? "/change-password" : state.from ?? "/";
    navigate(target, { replace: true });
  }, [token, mustChangePassword, state.from, navigate]);

  if (token) {
    const target = mustChangePassword ? "/change-password" : state.from ?? "/";
    return <Navigate to={target} replace />;
  }

  const handleSubmit = async (values: { username: string; password: string; remember: boolean }) => {
    setSubmitting(true);
    try {
      const result = await authApi.login({
        username: values.username,
        password: values.password,
      });
      setLoginResult({
        token: result.accessToken,
        user: result.user,
        mustChangePassword: result.mustChangePassword,
        rememberMe: values.remember,
      });
      message.success(t("userMenu.profile")); // 占位 — 真正成功提示交由目标页
      const target = result.mustChangePassword
        ? "/change-password"
        : state.from ?? "/";
      navigate(target, { replace: true });
    } catch {
      // httpClient 拦截器已弹 toast
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#f0f2f5",
      }}
    >
      <Card style={{ width: 400, boxShadow: "0 2px 8px rgba(0,0,0,0.08)" }}>
        <Typography.Title level={3} style={{ textAlign: "center", marginTop: 0 }}>
          {t("auth.login.title")}
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ textAlign: "center" }}>
          {t("auth.login.policyHint")}
        </Typography.Paragraph>
        <Form
          layout="vertical"
          onFinish={(values: unknown) =>
            void handleSubmit(values as { username: string; password: string; remember: boolean })
          }
          initialValues={{ remember: true }}
          disabled={submitting}
          // 阻止默认 form submit 触发整页刷新
          onSubmitCapture={(e: FormEvent) => e.preventDefault()}
        >
          <Form.Item
            label={t("auth.login.username")}
            name="username"
            rules={[{ required: true, message: t("auth.login.username") }]}
          >
            <Input autoComplete="username" autoFocus />
          </Form.Item>
          <Form.Item
            label={t("auth.login.password")}
            name="password"
            rules={[{ required: true, message: t("auth.login.password") }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="remember" valuePropName="checked" style={{ marginBottom: 16 }}>
            <Checkbox>{t("auth.login.rememberMe")}</Checkbox>
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" loading={submitting} block>
              {submitting ? t("auth.login.loggingIn") : t("auth.login.submit")}
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
