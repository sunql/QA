import { Button, Card, Descriptions, Tag } from "antd";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "../i18n";
import { useAuthStore } from "../stores/authStore";
import type { AuthMeRead } from "../types/auth";

export default function ProfilePage() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const user = useAuthStore((s) => s.user) as AuthMeRead | null;

  if (!user) return null;

  return (
    <Card
      title={t("auth.profile.title")}
      extra={<Button type="primary" onClick={() => nav("/change-password")}>{t("auth.profile.editPassword")}</Button>}
    >
      <Descriptions column={1} bordered>
        <Descriptions.Item label={t("auth.profile.username")}>{user.username}</Descriptions.Item>
        <Descriptions.Item label={t("auth.profile.displayName")}>{user.displayName}</Descriptions.Item>
        <Descriptions.Item label={t("auth.profile.email")}>{user.email ?? "—"}</Descriptions.Item>
        <Descriptions.Item label={t("auth.profile.roles")}>
          {user.roles.map((r) => <Tag key={r}>{r}</Tag>)}
        </Descriptions.Item>
        <Descriptions.Item label={t("auth.profile.organizations")}>
          {user.organizations.length === 0 ? "—" : user.organizations.map((o) => <Tag key={o}>{o}</Tag>)}
        </Descriptions.Item>
        <Descriptions.Item label={t("auth.profile.tenant")}>{user.tenantId}</Descriptions.Item>
        <Descriptions.Item label={t("auth.profile.lastLogin")}>
          {user.lastLoginAt ?? "—"}
        </Descriptions.Item>
      </Descriptions>
    </Card>
  );
}
