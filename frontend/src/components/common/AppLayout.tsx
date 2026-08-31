import { useState } from "react";
import { Layout, Menu, Switch, theme } from "antd";
import { useLocation, useNavigate, Outlet } from "react-router-dom";
import { useThemeStore } from "../../stores/themeStore";
import { useTranslation } from "../../i18n";
import LanguageSwitch from "./LanguageSwitch";

const { Sider, Header, Content } = Layout;
const { useToken } = theme;

const NAV_KEYS = [
  { key: "/models", labelKey: "appLayout.menu.models" },
  { key: "/embeddings", labelKey: "appLayout.menu.embeddings" },
  { key: "/chat", labelKey: "appLayout.menu.chat" },
  { key: "/ontology", labelKey: "appLayout.menu.ontology" },
  { key: "/datasource", labelKey: "appLayout.menu.datasource" },
  { key: "/data-quality", labelKey: "appLayout.menu.dataQuality" },
  { key: "/lineage", labelKey: "appLayout.menu.lineage" },
  { key: "/entity-mapping", labelKey: "appLayout.menu.entityMapping" },
  { key: "/kpi-catalog", labelKey: "appLayout.menu.kpiCatalog" },
  { key: "/features", labelKey: "appLayout.menu.features" },
  { key: "/usage", labelKey: "appLayout.menu.usage" },
  { key: "/status", labelKey: "appLayout.menu.status" },
  { key: "/graph", labelKey: "appLayout.menu.graph" },
  { key: "/vectors", labelKey: "appLayout.menu.vectors" },
  { key: "/supplier-360", labelKey: "appLayout.menu.supplier360" },
  { key: "/supplier-risk", labelKey: "appLayout.menu.supplierRisk" },
  { key: "/agents", labelKey: "appLayout.menu.agents" },
] as const;

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { token } = useToken();
  const { t } = useTranslation();
  const isDark = useThemeStore((s) => s.isDark);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);
  const [collapsed, setCollapsed] = useState(false);

  const selectedItem =
    NAV_KEYS.find((i) => location.pathname.startsWith(i.key)) ?? NAV_KEYS[0];

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        style={{
          height: "100vh",
          position: "fixed",
          left: 0,
          top: 0,
          bottom: 0,
          background: "#001529",
          zIndex: 100,
        }}
      >
        <div
          style={{
            height: 48,
            margin: 12,
            color: "#fff",
            textAlign: "center",
            lineHeight: "48px",
            fontWeight: 600,
            overflow: "hidden",
            whiteSpace: "nowrap",
          }}
        >
          {collapsed ? "QA" : t("appLayout.brand")}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedItem.key]}
          items={NAV_KEYS.map((i) => ({ key: i.key, label: t(i.labelKey) }))}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>

      <div
        style={{
          flex: 1,
          marginLeft: collapsed ? 80 : 200,
          display: "flex",
          flexDirection: "column",
          minHeight: "100vh",
          transition: "margin-left 0.2s",
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
        </Header>
        <Content
          style={{
            margin: 24,
            padding: 24,
            background: token.colorBgContainer,
            borderRadius: 8,
          }}
        >
          <Outlet />
        </Content>
      </div>
    </Layout>
  );
}
