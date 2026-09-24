import { useEffect, useState } from "react";
import { Layout, Menu, Spin, Switch, theme, Alert } from "antd";
import { useLocation, useNavigate, Outlet } from "react-router-dom";
import { useThemeStore } from "../../stores/themeStore";
import { useTranslation } from "../../i18n";
import { fetchMenuConfig } from "../../api/menuConfig";
import type { MenuConfig, MenuItem, MenuSection } from "../../types/menuConfig";
import { renderIcon } from "./menuIcons";
import LanguageSwitch from "./LanguageSwitch";
import MessageBell from "../MessageBell";
import UserMenu from "./UserMenu";

const { Sider, Header, Content } = Layout;
const { useToken } = theme;

const OPEN_KEYS_STORAGE = "menu.openKeys";

// 默认展开名单（feat-user-onboarding-ext 2026-09-20）：
// 登录/页面刷新后只展开「AI Agent」section — 因为默认落地页 = /chat
// （AIChatService，挂在 AI Agent 下）。其他所有 section 一律折叠，
// 用户反馈「业务配置」「智能分析」「系统信息配置」等都被localStorage
// 残留带开，与「菜单默认折叠」预期不符。每次 mount 强制 reset 到此默认，
// 不读 localStorage（用户手动展开的 section 不跨 session 保留）。
const DEFAULT_OPENED_SECTIONS: readonly string[] = ["section.aiAgent"];

function getInitialOpenKeys(): string[] {
  return [...DEFAULT_OPENED_SECTIONS];
}

function writeOpenKeys(keys: string[]): void {
  try {
    window.localStorage.setItem(OPEN_KEYS_STORAGE, JSON.stringify(keys));
  } catch {
    // localStorage 不可用（隐私模式）静默忽略
  }
}

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { token } = useToken();
  const { t } = useTranslation();
  const isDark = useThemeStore((s) => s.isDark);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);
  const [collapsed, setCollapsed] = useState(false);
  const [menuConfig, setMenuConfig] = useState<MenuConfig | null>(null);
  // fetchError 仅在 /menu-config 真正失败时设置；空 sections 不算错
  // （admin 没给某 user 授权时合法场景）。feat-rbac-identity 后端是 SSOT。
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [openKeys, setOpenKeys] = useState<string[]>(() => getInitialOpenKeys());

  useEffect(() => {
    let cancelled = false;
    fetchMenuConfig()
      .then((cfg) => {
        if (!cancelled) {
          setMenuConfig(cfg);
          setFetchError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          // 永远从 DB 拿；任何失败都明确暴露（不留静默兜底，避免出现
          // 「与权限不符的扁平菜单」误导用户）。
          // eslint-disable-next-line no-console
          console.error("[menu-config] fetch failed:", err);
          setFetchError(
            err instanceof Error ? err.message : String(err),
          );
        }
      });
    return () => { cancelled = true; };
  }, []);

  // 所有可见叶子项从 menuConfig.sections 派生；DB 返回空就是空，
  // 不再有 FALLBACK_NAV 兜底（feat-user-onboarding 2026-09-20 移除）。
  const allItems: MenuItem[] = menuConfig
    ? menuConfig.sections.flatMap((s) => s.children)
    : [];
  const selectedItem =
    allItems.find((i) => i.path && location.pathname.startsWith(i.path)) ??
    allItems[0] ?? null;

  // Persist openKeys（会话内手动展开的 section 写 localStorage 仅供同 session 刷新用；
//   跨登录/重启会被 getInitialOpenKeys reset 回 DEFAULT_OPENED_SECTIONS）
  const handleOpenChange = (keys: string[]) => {
    setOpenKeys(keys);
    writeOpenKeys(keys);
  };

  // Render AntD Menu items prop — 永远只从 DB 渲染，无 fallback。
  const menuItems: NonNullable<React.ComponentProps<typeof Menu>["items"]> =
    menuConfig
      ? menuConfig.sections.map((section: MenuSection) => ({
          key: section.code,
          icon: renderIcon(section.iconCode ?? undefined),
          label: t(section.labelKey),
          children: section.children.map((child) => ({
            key: child.code,
            icon: renderIcon(child.iconCode ?? undefined),
            label: t(child.labelKey),
          })),
        }))
      : [];

  const isLoading = menuConfig === null && fetchError === null;
  const sectionsEmpty =
    menuConfig !== null && menuConfig.sections.length === 0;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        style={{
          height: "100vh", position: "fixed", left: 0, top: 0, bottom: 0,
          background: "#001529", zIndex: 100,
        }}
      >
        <div
          style={{
            height: 48, margin: 12, color: "#fff", textAlign: "center",
            lineHeight: "48px", fontWeight: 600, overflow: "hidden",
            whiteSpace: "nowrap",
          }}
        >
          {collapsed ? "QA" : t("appLayout.brand")}
        </div>
        {isLoading ? (
          <div style={{ padding: 16, color: "#fff", textAlign: "center" }}>
            <Spin size="small" />
          </div>
        ) : fetchError ? (
          <Alert
            type="error"
            showIcon
            message={t("appLayout.menuLoadFailed")}
            description={fetchError}
            style={{ margin: 12 }}
          />
        ) : sectionsEmpty ? (
          <div
            style={{
              padding: 16, color: "rgba(255,255,255,0.65)",
              textAlign: "center", fontSize: 12,
            }}
          >
            {t("appLayout.menuEmpty")}
          </div>
        ) : (
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={selectedItem ? [selectedItem.code] : []}
            openKeys={openKeys}
            onOpenChange={handleOpenChange}
            items={menuItems}
            onClick={({ key }) => {
              const target = allItems.find((i) => i.code === key && i.path);
              if (target?.path) navigate(target.path);
            }}
          />
        )}
      </Sider>

      <div
        style={{
          flex: 1, marginLeft: collapsed ? 80 : 200,
          display: "flex", flexDirection: "column",
        }}
      >
        <Header
          style={{
            background: token.colorBgContainer,
            padding: "0 24px",
            fontSize: 16,
            fontWeight: 500,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            position: "sticky",
            top: 0,
            zIndex: 99,
          }}
        >
          <span>{selectedItem ? t(selectedItem.labelKey) : ""}</span>
          <Switch
            checked={isDark}
            onChange={toggleTheme}
            aria-label={t("appLayout.themeToggle")}
            checkedChildren={t("appLayout.themeDark")}
            unCheckedChildren={t("appLayout.themeLight")}
          />
          <LanguageSwitch />
          <MessageBell />
          <UserMenu />
        </Header>
        <Content style={{ margin: 24, background: token.colorBgContainer, borderRadius: 8 }}>
          <Outlet />
        </Content>
      </div>
    </Layout>
  );
}