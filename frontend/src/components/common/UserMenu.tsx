// UserMenu（feat-user-auth，2026-09-20）
//
// Header 右侧头像下拉：个人信息 / 修改密码（带 mustChangePassword 红点） / 登出。
// 头像用 displayName 首字；无 displayName 时退到 username。

import { Badge, Button, Dropdown } from "antd";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "../../i18n";
import { useAuthStore } from "../../stores/authStore";

function avatarLetter(name: string | null | undefined, fallback: string): string {
  const src = (name ?? "").trim();
  return src.length > 0 ? src[0]!.toUpperCase() : fallback[0]!.toUpperCase();
}

export default function UserMenu() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const me = useAuthStore((s) => s.me);
  const logout = useAuthStore((s) => s.logout);

  const mustChange = Boolean(me?.mustChangePassword);

  const display = user?.displayName || user?.username || "?";
  const letter = avatarLetter(user?.displayName, user?.username ?? "?");

  return (
    <Dropdown
      menu={{
        items: [
          { key: "profile", label: t("userMenu.profile") },
          {
            key: "change-password",
            label: (
              <span>
                {t("userMenu.changePassword")}
                {mustChange && (
                  <Badge
                    dot
                    style={{ marginLeft: 8 }}
                    title={t("userMenu.changePasswordBadge")}
                  />
                )}
              </span>
            ),
          },
          { type: "divider" },
          { key: "logout", label: t("userMenu.logout"), danger: true },
        ],
        onClick: async ({ key }) => {
          if (key === "profile") {
            navigate("/profile");
          } else if (key === "change-password") {
            navigate("/change-password");
          } else if (key === "logout") {
            await logout();
            navigate("/login", { replace: true });
          }
        },
      }}
      trigger={["click"]}
    >
      <Button type="text" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span
          aria-hidden
          style={{
            width: 28,
            height: 28,
            borderRadius: "50%",
            background: "#1677ff",
            color: "#fff",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            fontWeight: 600,
          }}
        >
          {letter}
        </span>
        <span>{display}</span>
      </Button>
    </Dropdown>
  );
}
