import { Routes, Route, Navigate } from "react-router-dom";
import { App as AntApp } from "antd";
import { useEffect } from "react";
// `AntdApp` 由 ThemedRoot 包好（main.tsx → ThemedRoot → AntdApp），
// 这里只是 useApp() 拿实例来注入到 httpClient 拦截器；不要再嵌套一层。
import AppLayout from "./components/common/AppLayout";
import ModelConfigPage from "./pages/ModelConfigPage";
import EmbeddingProvidersPage from "./pages/EmbeddingProvidersPage";
import ChatPage from "./pages/ChatPage";
import OntologyPage from "./pages/OntologyPage";
import DatasourcePage from "./pages/DatasourcePage";
import LocalImportInitPage from "./pages/LocalImportInitPage";
import DataQualityPage from "./pages/DataQualityPage";
import DataQualityReportCreatePage from "./pages/DataQualityReportCreatePage";
import DataQualityReportDetailPage from "./pages/DataQualityReportDetailPage";
import DataQualityReportComparePage from "./pages/DataQualityReportComparePage";
import DataQualityReportPublicSharePage from "./pages/DataQualityReportPublicSharePage";
import DataQualityRuleBatchCreatePage from "./pages/DataQualityRuleBatchCreatePage";
import { DataQualityRuleParamsPage } from "./pages/DataQualityRuleParamsPage";
import KpiCatalogPage from "./pages/KpiCatalogPage";
import FeatureCatalogPage from "./pages/FeatureCatalogPage";
import LineagePage from "./pages/LineagePage";
import EntityMappingPage from "./pages/EntityMappingPage";
import UsagePage from "./pages/UsagePage";
import ServiceStatusPage from "./pages/ServiceStatusPage";
import Neo4jGraphPage from "./pages/Neo4jGraphPage";
import MilvusVectorsPage from "./pages/MilvusVectorsPage";
import Supplier360Page from "./pages/Supplier360Page";
import SupplierRiskPage from "./pages/SupplierRiskPage";
import AgentRegistryPage from "./pages/AgentRegistryPage";
import AgentRuntimePage from "./pages/AgentRuntimePage";
import AdminAuditPage from "./pages/AdminAuditPage";
import AdminToolsPage from "./pages/AdminToolsPage";
import AdminFeatureRulesPage from "./pages/AdminFeatureRulesPage";
import AdminUsersPage from "./pages/AdminUsersPage";
import AdminRolesPage from "./pages/AdminRolesPage";
import AdminOrganizationsPage from "./pages/AdminOrganizationsPage";
import AdminMenusPage from "./pages/AdminMenusPage";
import AdminSystemConfigPage from "./pages/AdminSystemConfigPage";
import { ProfilePage } from "./pages/ProfilePage";
import AdminWikiImportPage from "./pages/AdminWikiImportPage";
import AdminWikiPagesPage from "./pages/AdminWikiPagesPage";
import AdminWikiConflictsPage from "./pages/AdminWikiConflictsPage";
import AdminWikiSuggestionsPage from "./pages/AdminWikiSuggestionsPage";
import AdminWikiCoveragePage from "./pages/AdminWikiCoveragePage";
import AdminWikiGraphPage from "./pages/AdminWikiGraphPage";
import BusinessObjectPage from "./pages/BusinessObjectPage";
import DocumentsPage from "./pages/DocumentsPage";
import OntologyPropertyAdminPage from "./pages/OntologyPropertyAdminPage";
import { setMessageApi } from "./api/client";

export default function App() {
  // 把 antd ``App`` 提供的 message 实例注入到 httpClient 拦截器。
  // 拦截器在 module load 时就生效、不在 React 树里，必须通过这种 holder
  // 模式让静态调用也能拿到带主题上下文的 message —— 否则 antd 会报
  // "Static function can not consume context like dynamic theme"。
  const { message } = AntApp.useApp();
  useEffect(() => {
    setMessageApi(message);
  }, [message]);
  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Navigate to="/models" replace />} />
        <Route path="models" element={<ModelConfigPage />} />
        <Route path="embeddings" element={<EmbeddingProvidersPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="ontology" element={<OntologyPage />} />
        <Route path="datasource" element={<DatasourcePage />} />
        <Route path="local-import" element={<LocalImportInitPage />} />
        <Route path="data-quality" element={<DataQualityPage />} />
        {/* 旧的独立路由重定向到 tab 参数（页面已下沉为 DataQualityPage 的 4 个 tab）。
            深链路由（reports/new / compare / share / :id）保留，从 tab 内导航进入。 */}
        <Route path="data-quality/generate" element={<Navigate to="/data-quality?tab=generate" replace />} />
        <Route path="data-quality/rules/batch-create" element={<DataQualityRuleBatchCreatePage />} />
        <Route path="data-quality/rule-params" element={<DataQualityRuleParamsPage />} />
        <Route path="data-quality/reports" element={<Navigate to="/data-quality?tab=reports" replace />} />
        <Route path="data-quality/reports/new" element={<DataQualityReportCreatePage />} />
        <Route path="data-quality/reports/compare" element={<DataQualityReportComparePage />} />
        <Route path="data-quality/reports/share/:token" element={<DataQualityReportPublicSharePage />} />
        <Route path="data-quality/reports/:id" element={<DataQualityReportDetailPage />} />
        <Route path="lineage" element={<LineagePage />} />
        <Route path="entity-mapping" element={<EntityMappingPage />} />
        <Route path="kpi-catalog" element={<KpiCatalogPage />} />
        <Route path="features" element={<FeatureCatalogPage />} />
        <Route path="usage" element={<UsagePage />} />
        <Route path="status" element={<ServiceStatusPage />} />
        <Route path="graph" element={<Neo4jGraphPage />} />
        <Route path="vectors" element={<MilvusVectorsPage />} />
        <Route path="supplier-360" element={<Supplier360Page />} />
        <Route path="supplier-risk" element={<SupplierRiskPage />} />
        <Route path="agents/run" element={<AgentRuntimePage />} />
        <Route path="agents" element={<AgentRegistryPage />} />
        <Route path="admin/audit" element={<AdminAuditPage />} />
        <Route path="admin/tools" element={<AdminToolsPage />} />
        <Route path="admin/feature-rules" element={<AdminFeatureRulesPage />} />
        <Route path="admin/users" element={<AdminUsersPage />} />
        <Route path="admin/roles" element={<AdminRolesPage />} />
        <Route path="admin/organizations" element={<AdminOrganizationsPage />} />
        <Route path="admin/menus" element={<AdminMenusPage />} />
        <Route path="admin/system-config" element={<AdminSystemConfigPage />} />
        <Route path="admin/wiki-pages" element={<AdminWikiPagesPage />} />
        <Route path="admin/wiki-import" element={<AdminWikiImportPage />} />
        <Route path="admin/wiki-conflicts" element={<AdminWikiConflictsPage />} />
        <Route path="admin/wiki-suggestions" element={<AdminWikiSuggestionsPage />} />
        <Route path="admin/wiki-coverage" element={<AdminWikiCoveragePage />} />
        <Route path="admin/wiki-graph" element={<AdminWikiGraphPage />} />
        <Route path="business-objects" element={<BusinessObjectPage />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="ontology-properties" element={<OntologyPropertyAdminPage />} />
        <Route path="profile" element={<ProfilePage />} />
      </Route>
    </Routes>
  );
}
