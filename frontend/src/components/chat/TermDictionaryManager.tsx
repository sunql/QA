import { useEffect, useState } from "react";
import { Button, Drawer, Popconfirm, Table, Typography, message } from "antd";
import { deleteTerm, listTerms } from "../../api/termDictionary";
import type { TermDictionary } from "../../types/termDictionary";
import { useTranslation } from "../../i18n";
import { useAuthStore } from "../../stores/authStore";
import TermDictionaryFormModal from "./TermDictionaryFormModal";

const { Text } = Typography;

/** 术语词典管理抽屉：列出全部术语 + 删除。挂到 ChatPage 工具栏。 */
export default function TermDictionaryManager() {
  const { t } = useTranslation();
  const user = useAuthStore((s) => s.user);
  const isAdmin = (user?.roles ?? []).includes("admin");
  const [open, setOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [terms, setTerms] = useState<TermDictionary[]>([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setTerms(await listTerms());
    } catch {
      // 加载失败不阻断抽屉打开，留空列表
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const handleDelete = async (id: number) => {
    await deleteTerm(id);
    void message.success(t("toast.deleted"));
    await load();
  };

  return (
    <>
      <Button onClick={() => setOpen(true)}>{t("termDictionary.openManager")}</Button>
      <Drawer
        title={t("termDictionary.managerTitle")}
        open={open}
        onClose={() => setOpen(false)}
        width={680}
        // 新增限 admin（后端 403 兜底）：非管理员只看只读列表。
        extra={
          isAdmin ? (
            <Button type="primary" onClick={() => setAddOpen(true)}>
              {t("termDictionary.add")}
            </Button>
          ) : null
        }
      >
        <Table<TermDictionary>
          rowKey="id"
          loading={loading}
          dataSource={terms}
          size="small"
          pagination={false}
          locale={{ emptyText: t("termDictionary.empty") }}
          columns={[
            {
              title: t("termDictionary.labels.term"),
              dataIndex: "term",
              key: "term",
              width: 140,
            },
            {
              title: t("termDictionary.labels.definition"),
              dataIndex: "definition",
              key: "definition",
            },
            {
              title: t("termDictionary.labels.mappedClassName"),
              dataIndex: "mappedClassName",
              key: "mappedClassName",
              render: (v: string | null) =>
                v ? <Text code>{v}</Text> : <Text type="secondary">{t("common.emDash")}</Text>,
            },
            {
              title: t("termDictionary.labels.formulaHint"),
              dataIndex: "formulaHint",
              key: "formulaHint",
              render: (v: string | null) =>
                v ? <Text code>{v}</Text> : <Text type="secondary">{t("common.emDash")}</Text>,
            },
            // 删除限 admin（后端 403 兜底）：非管理员只看只读列表。
            ...(isAdmin
              ? [
                  {
                    title: t("common.delete"),
                    key: "actions",
                    width: 80,
                    render: (_: unknown, row: TermDictionary) => (
                      <Popconfirm
                        title={t("termDictionary.deleteConfirm")}
                        onConfirm={() => handleDelete(row.id)}
                      >
                        <Button danger size="small">
                          {t("common.delete")}
                        </Button>
                      </Popconfirm>
                    ),
                  },
                ]
              : []),
          ]}
        />
      </Drawer>
      <TermDictionaryFormModal
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onCreated={() => void load()}
      />
    </>
  );
}
