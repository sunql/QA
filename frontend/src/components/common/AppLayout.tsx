import { useEffect, useState } from "react";
import { Layout, Menu, Spin, Switch, theme } from "antd";
import { useLocation, useNavigate, Outlet } from "react-router-dom";
import { useThemeStore } from "../../stores/themeStore";
import { useTranslation } from "../../i18n";
import { fetchMenuConfig } from "../../api/menuConfig";
import type { MenuConfig, MenuItem, MenuSection } from "../../types/menuConfig";
import { FALLBACK_NAV } from "./fallbackNav";
import { renderIcon } from "./menuIcons";
import LanguageSwitch from "./LanguageSwitch";
import MessageBell from "../MessageBell";

const { Sider, Header, Content } = Layout;
const { useToken } = theme;

const OPEN_KEYS_STORAGE = "menu.openKeys";

function readOpenKeys(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(OPEN_KEYS_STORAGE);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
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
  const [useFallback, setUseFallback] = useState(false);
  const [openKeys, setOpenKeys] = useState<string[]>(() => readOpenKeys());

  useEffect(() => {
    let cancelled = false;
    fetchMenuConfig()
      .then((cfg) => {
        if (!cancelled) setMenuConfig(cfg);
      })
      .catch((err) => {
        // eslint-disable-next-line no-console
        console.warn("[menu-config] fallback to static nav:", err);
        if (!cancelled) setUseFallback(true);
      });
    return () => { cancelled = true; };
  }, []);

  // Determine selected key from current path
  const allItems: MenuItem[] = useFallback || !menuConfig || menuConfig.sections.length === 0
    ? FALLBACK_NAV.map((n) => ({
        code: n.key, labelKey: n.labelKey, iconCode: null,
        sortOrder: 0, permissionCode: null, roles: [], path: n.key,
      }))
    : menuConfig.sections.flatMap((s) => s.children);
  const selectedItem =
    allItems.find((i) => i.path && location.pathname.startsWith(i.path)) ??
    allItems[0] ?? null;

  // Persist openKeys
  const handleOpenChange = (keys: string[]) => {
    setOpenKeys(keys);
    try {
      window.localStorage.setItem(OPEN_KEYS_STORAGE, JSON.stringify(keys));
    } catch {
      // localStorage 不可用（隐私模式）静默忽略
    }
  };

  // Render AntD Menu items prop
  const menuItems = useFallback || !menuConfig || menuConfig.sections.length === 0
    ? FALLBACK_NAV.map((n) => ({
        key: n.key, label: t(n.labelKey),
        icon: null,
      }))
    : menuConfig.sections.map((section: MenuSection) => ({
        key: section.code,
        icon: renderIcon(section.iconCode ?? undefined),
        label: t(section.labelKey),
        children: section.children.map((child) => ({
          key: child.code,
          icon: renderIcon(child.iconCode ?? undefined),
          label: t(child.labelKey),
        })),
      }));

  const isLoading = !useFallback && menuConfig === null;

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
        ) : (
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={selectedItem ? [selectedItem.code] : []}
            openKeys={useFallback ? undefined : openKeys}
            onOpenChange={useFallback ? undefined : handleOpenChange}
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
          <span>{t(selectedItem.labelKey)}</span>
          <Switch
            checked={isDark}
            onChange={toggleTheme}
            aria-label={t("appLayout.themeToggle")}
            checkedChildren={t("appLayout.themeDark")}
            unCheckedChildren={t("appLayout.themeLight")}
          />
          <LanguageSwitch />
          <MessageBell />
        </Header>
        <Content style={{ margin: 24, background: token.colorBgContainer, borderRadius: 8 }}>
          <Outlet />
        </Content>
      </div>
    </Layout>
  );
}
