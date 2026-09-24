import { useEffect, useState, useCallback } from "react";
import {
  Tabs,
  Modal,
  Input,
  Tag,
  Table,
  Tooltip,
  Typography,
  Button,
  Space,
} from "antd";
import { SearchOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { listClasses, searchOntology } from "../api/ontology";
import type { OntologyClass, OntologySearchHit } from "../types/ontology";
import { useTranslation } from "../i18n";
import ClassTab from "../components/ontology/ClassTab";
import PropertyTab from "../components/ontology/PropertyTab";
import MetricTab from "../components/ontology/MetricTab";
import JoinTab from "../components/ontology/JoinTab";
import SemanticRelationTab from "../components/ontology/SemanticRelationTab";
import BatchRelationModal from "../components/ontology/BatchRelationModal";

const { Title } = Typography;

const ENTITY_TYPE_COLOR: Record<OntologySearchHit["type"], string> = {
  class: "blue",
  property: "cyan",
  metric: "purple",
};

export default function OntologyPage() {
  const { t } = useTranslation();
  const [classes, setClasses] = useState<OntologyClass[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<OntologySearchHit[]>([]);
  const [searchModalOpen, setSearchModalOpen] = useState(false);
  const [batchModalOpen, setBatchModalOpen] = useState(false);

  const loadClasses = useCallback(async () => {
    try {
      setClasses(await listClasses());
    } catch {
      // 错误已由拦截器提示
    }
  }, []);

  useEffect(() => {
    void loadClasses();
  }, [loadClasses]);

  const handleSearch = async (q: string) => {
    const query = q.trim();
    if (!query) return;
    setSearching(true);
    try {
      const hits = await searchOntology(query, { topK: 15 });
      setSearchResults(hits);
      setSearchModalOpen(true);
    } catch {
      // 错误已由拦截器提示
    } finally {
      setSearching(false);
    }
  };

  const searchColumns = [
    {
      title: t("forms.ontology.searchColumns.type"),
      dataIndex: "type",
      width: 80,
      render: (tp: OntologySearchHit["type"]) => (
        <Tag color={ENTITY_TYPE_COLOR[tp]}>{t(`enums.entityType.${tp}`)}</Tag>
      ),
    },
    { title: t("forms.ontology.searchColumns.name"), dataIndex: "name" },
    {
      title: t("forms.ontology.searchColumns.alias"),
      dataIndex: "alias",
      render: (v: string | null) => v ?? t("common.dash"),
    },
    {
      title: t("forms.ontology.searchColumns.description"),
      dataIndex: "description",
      width: 280,
      ellipsis: { showTitle: false },
      render: (v: string | null) =>
        v ? (
          <Tooltip placement="topLeft" title={v}>
            {v}
          </Tooltip>
        ) : (
          t("common.dash")
        ),
    },
    {
      title: t("forms.ontology.searchColumns.score"),
      dataIndex: "score",
      width: 90,
      render: (s: number) => <span style={{ fontSize: 12 }}>{(s * 100).toFixed(0)}%</span>,
    },
  ];

  return (
    <div style={{ padding: "0 24px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <Title level={4} style={{ margin: 0 }}>
          {t("pages.ontology")}
        </Title>
        <Space>
          <Button icon={<ThunderboltOutlined />} onClick={() => setBatchModalOpen(true)}>
            {t("forms.ontology.batchRelations.button")}
          </Button>
          <Input.Search
            placeholder={t("forms.ontology.semanticSearchPlaceholder")}
            enterButton={
              <span>
                <SearchOutlined /> {t("semanticSearchButton")}
              </span>
            }
            style={{ width: 340 }}
            loading={searching}
            onSearch={(q) => void handleSearch(q)}
            allowClear
          />
        </Space>
      </div>
      <Tabs
        defaultActiveKey="class"
        items={[
          {
            key: "class",
            label: t("forms.ontology.tabs.classes"),
            children: <ClassTab classes={classes} refreshClasses={loadClasses} />,
          },
          {
            key: "property",
            label: t("forms.ontology.tabs.properties"),
            children: <PropertyTab classes={classes} refreshClasses={loadClasses} />,
          },
          {
            key: "metric",
            label: t("forms.ontology.tabs.metrics"),
            children: <MetricTab classes={classes} />,
          },
          {
            key: "join",
            label: t("forms.ontology.tabs.joins"),
            children: <JoinTab classes={classes} />,
          },
          {
            key: "semanticRelation",
            label: t("forms.ontology.tabs.semanticRelations"),
            children: <SemanticRelationTab classes={classes} />,
          },
        ]}
      />
      <Modal
        title={t("forms.ontology.semanticResultsCardTitle")}
        open={searchModalOpen}
        onCancel={() => setSearchModalOpen(false)}
        footer={null}
        width={760}
        destroyOnHidden
      >
        {searchResults.length === 0 ? (
          <div style={{ color: "#999", textAlign: "center", padding: 24 }}>
            {t("pages.searchEmpty")}
          </div>
        ) : (
          <Table
            rowKey={(r) => `${r.type}-${r.id}`}
            size="small"
            dataSource={searchResults}
            columns={searchColumns}
            pagination={{ pageSize: 10 }}
          />
        )}
      </Modal>
      <BatchRelationModal
        open={batchModalOpen}
        onClose={() => setBatchModalOpen(false)}
      />
    </div>
  );
}
