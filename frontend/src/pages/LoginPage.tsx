import { useState } from "react";
import { useNavigate, useLocation, Navigate } from "react-router-dom";
import { Form, Input, Button, Checkbox, message } from "antd";
import { useTranslation } from "react-i18next";
import { RobotOutlined } from "@ant-design/icons";
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
    <div className="login-page">
      {/* AI 装饰层：神经网络节点 + 连线（纯 CSS，离线可用） */}
      <div className="login-orb login-orb-1" aria-hidden />
      <div className="login-orb login-orb-2" aria-hidden />
      <div className="login-orb login-orb-3" aria-hidden />
      <div className="login-grid" aria-hidden />

      <div className="login-card">
        <div className="login-brand">
          <RobotOutlined className="login-brand-icon" />
          <h2 className="login-title">{t("auth.login.title")}</h2>
          <div className="login-tagline">{t("auth.login.tagline")}</div>
        </div>
        <Form layout="vertical" onFinish={onFinish} requiredMark={false}>
          <Form.Item name="username" label={t("auth.login.username")} rules={[{ required: true }]}>
            <Input autoFocus autoComplete="username" size="large" />
          </Form.Item>
          <Form.Item name="password" label={t("auth.login.password")} rules={[{ required: true }]}>
            <Input.Password autoComplete="current-password" size="large" />
          </Form.Item>
          <Form.Item name="rememberMe" valuePropName="checked" initialValue={false}>
            <Checkbox>{t("auth.login.rememberMe")}</Checkbox>
          </Form.Item>
          <Button type="primary" htmlType="submit" block size="large" loading={submitting}>
            {t("auth.login.submit")}
          </Button>
          <div className="login-forgot">{t("auth.login.forgot")}</div>
        </Form>
      </div>
      <div className="login-footer">{t("auth.login.footer")}</div>
    </div>
  );
}
