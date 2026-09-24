import { useEffect, useState } from "react";
import { Alert, Button, Card, Empty, Select, Space, Spin, Typography } from "antd";
import { ImportOutlined } from "@ant-design/icons";
import { useTranslation } from "../i18n";
import { listDataSources } from "../api/datasource";
import type { DataSource } from "../types/datasource";
import ImportWizard from "../components/localImport/ImportWizard";

const { Text } = Typography;

/**
 * 本地数据初始化 — 独立挂载入口页（菜单 item.localImport → /local-import）。
 *
 * 与 DatasourcePage 行内「导入到本体」共用同一 ImportWizard：本页把流程提升为
 * 顶层菜单直达——选定数据源（默认 isDefault 优先，无则取列表第一个）后点「开始导入」，
 * 弹出向导做多表全量 / 单表列选导入 + 自动 join 推断。
 */
export default function LocalImportInitPage() {
  const { t } = useTranslation();
  const [datasources, setDatasources] = useState<readonly DataSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  // 非空即向导打开；key 换数据源时强制重挂载（refetch schema）
  const [initTargetId, setInitTargetId] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    listDataSources()
      .then((rows) => {
        if (cancelled) return;
        setDatasources(rows);
        const preferred = rows.find((d) => d.isDefault) ?? rows[0];
        setSelectedId(preferred ? preferred.id : null);
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const options = datasources.map((d) => ({ value: d.id, label: d.name }));

  return (
    <Card
      title={t("localImport.initPage.title")}
      styles={{ body: { paddingTop: 12 } }}
    >
      {loading ? (
        <Spin />
      ) : loadFailed ? (
        <Alert type="error" message={t("localImport.initPage.loadFailed")} showIcon />
      ) : datasources.length === 0 ? (
        <Empty description={t("localImport.initPage.noDatasource")} />
      ) : (
        <Space wrap align="center">
          <Text strong>{t("localImport.initPage.datasourceLabel")}</Text>
          <Select
            data-testid="datasourceSelect"
            style={{ width: 320 }}
            value={selectedId}
            onChange={setSelectedId}
            options={options}
          />
          <Button
            type="primary"
            icon={<ImportOutlined />}
            disabled={selectedId === null}
            onClick={() => {
              if (selectedId !== null) setInitTargetId(selectedId);
            }}
          >
            {t("localImport.initPage.startButton")}
          </Button>
        </Space>
      )}

      <ImportWizard
        key={initTargetId ?? "none"}
        open={initTargetId !== null}
        datasourceId={initTargetId ?? 0}
        onClose={() => setInitTargetId(null)}
      />
    </Card>
  );
}
