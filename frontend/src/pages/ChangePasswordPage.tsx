import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Form, Input, Button, Card, message, Progress } from "antd";
import { useTranslation } from "../i18n";
import { useAuthStore } from "../stores/authStore";
import { authApi } from "../api/auth";

const POLICY = /^(?=.*[A-Za-z])(?=.*\d).{8,}$/;

function strength(p: string): "weak" | "medium" | "strong" {
  if (!POLICY.test(p)) return "weak";
  if (/[^A-Za-z0-9]/.test(p) && p.length >= 12) return "strong";
  return "medium";
}

export default function ChangePasswordPage() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const changeOwn = useAuthStore((s) => s.changeOwnPassword);
  const [submitting, setSubmitting] = useState(false);
  const [pwd, setPwd] = useState("");

  const onFinish = async (v: { oldPassword: string; newPassword: string; confirmPassword: string }) => {
    if (v.newPassword !== v.confirmPassword) {
      message.error(t("auth.changePassword.mismatch"));
      return;
    }
    if (!POLICY.test(v.newPassword)) {
      message.error(t("auth.changePassword.weak"));
      return;
    }
    setSubmitting(true);
    try {
      await changeOwn(v.oldPassword, v.newPassword);
      message.info(t("auth.changePassword.success"));
      nav("/login", { replace: true });
    } catch (e: any) {
      const code = e?.response?.data?.error;
      if (code === "MSG_OLD_PASSWORD_INCORRECT") message.error(t("auth.changePassword.oldWrong"));
      else if (e?.response?.data?.error === "MSG_PASSWORD_TOO_WEAK") message.error(t("auth.changePassword.weak"));
      else message.error(t("auth.login.networkError"));
    } finally {
      setSubmitting(false);
    }
  };

  const s = strength(pwd);
  const percent = s === "weak" ? 30 : s === "medium" ? 60 : 100;

  return (
    <Card title={t("auth.changePassword.title")} style={{ maxWidth: 480, margin: "24px auto" }}>
      <Form layout="vertical" onFinish={onFinish}>
        <Form.Item name="oldPassword" label={t("auth.changePassword.oldPassword")} rules={[{ required: true }]}>
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item
          name="newPassword"
          label={t("auth.changePassword.newPassword")}
          extra={t("auth.changePassword.policyHint")}
          rules={[{ required: true }]}
        >
          <Input.Password autoComplete="new-password" onChange={(e) => setPwd(e.target.value)} />
        </Form.Item>
        <Progress percent={percent} showInfo={false} status={s === "weak" ? "exception" : s === "medium" ? "active" : "success"} />
        <Form.Item name="confirmPassword" label={t("auth.changePassword.confirmPassword")} rules={[{ required: true }]}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit" loading={submitting}>{t("auth.changePassword.title")}</Button>
        </Form.Item>
      </Form>
    </Card>
  );
}
