import { Avatar, Badge, Dropdown, type MenuProps } from "antd";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "../../i18n";
import { UserOutlined } from "@ant-design/icons";
import { useAuthStore } from "../../stores/authStore";

export default function UserMenu() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const user = useAuthStore((s) => s.user);
  const mustChange = useAuthStore((s) => s.mustChangePassword);
  const logout = useAuthStore((s) => s.logout);

  const items: MenuProps["items"] = [
    {
      key: "header",
      type: "group",
      label: user ? t("userMenu.displayName", { displayName: user.displayName, username: user.username }) : "—",
    },
    { type: "divider" },
    {
      key: "profile",
      label: t("userMenu.profile"),
      icon: <UserOutlined />,
      onClick: () => nav("/profile"),
    },
    {
      key: "changePassword",
      label: (
        <span>
          {t("userMenu.changePassword")}
          {mustChange && (
            <Badge dot style={{ marginLeft: 8 }} title={t("userMenu.changePasswordHint")} />
          )}
        </span>
      ),
      onClick: () => nav("/change-password"),
    },
    { type: "divider" },
    {
      key: "logout",
      label: t("userMenu.logout"),
      onClick: async () => {
        await logout();
        nav("/login", { replace: true });
      },
    },
  ];

  const initial = user?.username?.[0]?.toUpperCase() ?? "?";

  return (
    <Dropdown menu={{ items }} placement="bottomRight" trigger={["click"]}>
      <span style={{ cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 8 }}>
        <Avatar size="small">{initial}</Avatar>
        <span>{user?.username ?? "—"}</span>
      </span>
    </Dropdown>
  );
}
