// ChangePasswordPage（feat-user-auth，2026-09-20）
//
// PUT /auth/me/password。成功后必须重新登录（后端已吊销所有 session）→
// 清 store + 跳 /login。后端错误：httpClient 拦截器弹 toast。

import { useState } from "react";
import { App, Button, Card, Form, Input, Typography } from "antd";
import { useNavigate } from "react-router-dom";

import { authApi } from "../api/auth";
import { useAuthStore } from "../stores/authStore";
import { useTranslation } from "../i18n";

interface FormValues {
  oldPassword: string;
  newPassword: string;
  confirmPassword: string;
}

export default function ChangePasswordPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const logout = useAuthStore((s) => s.logout);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (values: FormValues) => {
    if (values.newPassword !== values.confirmPassword) {
      void message.error(t("auth.changePassword.mismatch"));
      return;
    }
    setSubmitting(true);
    try {
      await authApi.changeOwnPassword({
        oldPassword: values.oldPassword,
        newPassword: values.newPassword,
      });
      void message.success(t("auth.changePassword.success"));
      // 后端已吊销所有 session —— 本地也清，跳 /login
      await logout();
      navigate("/login", { replace: true });
    } catch {
      // httpClient 拦截器已处理
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card title={t("auth.changePassword.title")} style={{ maxWidth: 560 }}>
      <Typography.Paragraph type="secondary">
        {t("auth.changePassword.policyHint")}
      </Typography.Paragraph>
      <Form
        layout="vertical"
        onFinish={(values: unknown) =>
          void handleSubmit(values as FormValues)
        }
        disabled={submitting}
      >
        <Form.Item
          label={t("auth.changePassword.oldPassword")}
          name="oldPassword"
          rules={[{ required: true }]}
        >
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item
          label={t("auth.changePassword.newPassword")}
          name="newPassword"
          rules={[
            { required: true, min: 8, message: t("auth.changePassword.policyHint") },
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          label={t("auth.changePassword.confirmPassword")}
          name="confirmPassword"
          dependencies={["newPassword"]}
          rules={[
            { required: true },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue("newPassword") === value) {
                  return Promise.resolve();
                }
                return Promise.reject(new Error(t("auth.changePassword.mismatch")));
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item style={{ marginBottom: 0 }}>
          <Button type="primary" htmlType="submit" loading={submitting}>
            {submitting ? t("auth.changePassword.submitting") : t("auth.changePassword.submit")}
          </Button>
        </Form.Item>
      </Form>
    </Card>
  );
}
