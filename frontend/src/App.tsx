import { Routes, Route, Navigate } from "react-router-dom";
import AppLayout from "./components/common/AppLayout";
import ModelConfigPage from "./pages/ModelConfigPage";
import EmbeddingProvidersPage from "./pages/EmbeddingProvidersPage";
import ChatPage from "./pages/ChatPage";
import OntologyPage from "./pages/OntologyPage";
import DatasourcePage from "./pages/DatasourcePage";
import DataQualityPage from "./pages/DataQualityPage";
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

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Navigate to="/models" replace />} />
        <Route path="models" element={<ModelConfigPage />} />
        <Route path="embeddings" element={<EmbeddingProvidersPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="ontology" element={<OntologyPage />} />
        <Route path="datasource" element={<DatasourcePage />} />
        <Route path="data-quality" element={<DataQualityPage />} />
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
        <Route path="agents" element={<AgentRegistryPage />} />
      </Route>
    </Routes>
  );
}
