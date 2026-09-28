/**
 * Wiki ↔ Ontology 链接管理页（Task 7）。
 *
 * 左侧：Wiki 页面树（Tree）；右侧：选中页面的 class/property 链接列表 + 添加 Modal。
 *
 * 树暂用静态 stub 数据（3 个示例节点），后续接入 WikiPageService.listPages()。
 */
import { useState, useEffect, useCallback } from "react";
import {
  Button,
  Form,
  Input,
  Modal,
  Radio,
  Select,
  Slider,
  Space,
  Tabs,
  Tag,
  Tree,
  message,
} from "antd";
import type { DataNode } from "antd/es/tree";
import {
  createWikiLink,
  listLinkableTargets,
  listWikiLinks,
  revokeWikiLink,
} from "../api/adminWikiLinks";
import type {
  WikiLink,
  WikiLinkType,
  WikiLinkableTarget,
} from "../types/wikiLink";

// ---------------------------------------------------------------------------
// Stub wiki page tree (TODO: replace with WikiPageService.listPages())
// ---------------------------------------------------------------------------
const STUB_PAGES: DataNode[] = [
  {
    title: "采购管理",
    key: "page-001",
    children: [
      { title: "供应商准入流程", key: "page-001-01" },
      { title: "采购订单执行", key: "page-001-02" },
    ],
  },
  {
    title: "质量管理",
    key: "page-002",
    children: [
      { title: "IQC 来料检验", key: "page-002-01" },
    ],
  },
  {
    title: "仓储物流",
    key: "page-003",
    children: [
      { title: "入库作业", key: "page-003-01" },
      { title: "出库配送", key: "page-003-02" },
    ],
  },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export function WikiLinksPage() {
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
  const [linkType, setLinkType] = useState<WikiLinkType>("class");
  const [links, setLinks] = useState<WikiLink[]>([]);
  const [linkables, setLinkables] = useState<WikiLinkableTarget[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm();

  // Fetch links when page or type changes
  const refresh = useCallback(async () => {
    if (!selectedPageId) {
      setLinks([]);
      return;
    }
    try {
      const rows = await listWikiLinks({ page_id: selectedPageId });
      setLinks(rows);
    } catch {
      message.error("加载链接失败");
    }
  }, [selectedPageId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Open add modal — fetch linkable targets
  const openAddModal = async () => {
    try {
      const targets = await listLinkableTargets(linkType);
      setLinkables(targets);
      setModalOpen(true);
    } catch {
      message.error("加载可链接目标失败");
    }
  };

  // Submit new link
  const submit = async (values: Record<string, unknown>) => {
    if (!selectedPageId) return;
    setSubmitting(true);
    try {
      await createWikiLink({
        page_id: selectedPageId,
        ontology_type: linkType,
        ontology_id: values.ontology_id as number,
        weight: values.weight as number,
        chunk_id: values.scope === "chunk" ? (values.chunk_id as string) : null,
        note: (values.note as string) || null,
      });
      message.success("添加成功");
      setModalOpen(false);
      form.resetFields();
      refresh();
    } catch {
      message.error("添加失败");
    } finally {
      setSubmitting(false);
    }
  };

  // Revoke a link
  const revoke = async (id: number) => {
    try {
      await revokeWikiLink(id);
      message.success("已撤销");
      refresh();
    } catch {
      message.error("撤销失败");
    }
  };

  const filteredLinks = links.filter((l) => l.ontology_type === linkType);

  return (
    <div style={{ padding: 24 }}>
      <h2>Wiki ↔ Ontology 链接管理</h2>
      <div style={{ display: "flex", gap: 16, marginTop: 16 }}>
        {/* Left: wiki page tree */}
        <div style={{ width: 280, border: "1px solid #f0f0f0", padding: 8 }}>
          <div style={{ marginBottom: 8, color: "#666", fontSize: 12 }}>
            请选择 Wiki 页面
          </div>
          <Tree
            treeData={STUB_PAGES}
            selectedKeys={selectedPageId ? [selectedPageId] : []}
            onSelect={(keys) => {
              const k = keys[0] as string | undefined;
              setSelectedPageId(k ?? null);
            }}
          />
        </div>

        {/* Right: links panel */}
        <div style={{ flex: 1 }}>
          <Tabs
            activeKey={linkType}
            onChange={(k) => setLinkType(k as WikiLinkType)}
            items={[
              { key: "class", label: "Class" },
              { key: "property", label: "Property" },
            ]}
          />
          <Space style={{ marginBottom: 12 }}>
            <Button
              type="primary"
              onClick={openAddModal}
              disabled={!selectedPageId}
            >
              + 添加绑定
            </Button>
          </Space>

          {filteredLinks.length === 0 ? (
            <div style={{ color: "#999", padding: "24px 0" }}>
              {selectedPageId ? "暂无链接" : "请先选择左侧 Wiki 页面"}
            </div>
          ) : (
            filteredLinks.map((link) => (
              <div
                key={link.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "8px 12px",
                  borderBottom: "1px solid #f0f0f0",
                }}
              >
                <Tag color={link.ontology_type === "class" ? "blue" : "green"}>
                  {link.ontology_type}
                </Tag>
                <span>ontology_id={link.ontology_id}</span>
                <span style={{ color: "#666" }}>
                  weight={link.weight.toFixed(2)}
                </span>
                {link.note && (
                  <span style={{ color: "#999", fontSize: 12 }}>{link.note}</span>
                )}
                <Button
                  danger
                  size="small"
                  style={{ marginLeft: "auto" }}
                  onClick={() => revoke(link.id)}
                >
                  撤销
                </Button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Add link modal */}
      <Modal
        title="添加绑定"
        open={modalOpen}
        onCancel={() => {
          setModalOpen(false);
          form.resetFields();
        }}
        footer={null}
      >
        <Form form={form} onFinish={submit} layout="vertical">
          <Form.Item
            name="scope"
            label="作用范围"
            initialValue="page"
          >
            <Radio.Group>
              <Radio value="page">覆盖全页</Radio>
              <Radio value="chunk">仅限此段落</Radio>
            </Radio.Group>
          </Form.Item>

          <Form.Item
            noStyle
            shouldUpdate={(prev, curr) => prev.scope !== curr.scope}
          >
            {({ getFieldValue }) =>
              getFieldValue("scope") === "chunk" ? (
                <Form.Item
                  name="chunk_id"
                  label="段落 ID"
                  rules={[{ required: true, message: "请输入段落 ID" }]}
                >
                  <Input placeholder="请输入 chunk_id" />
                </Form.Item>
              ) : null
            }
          </Form.Item>

          <Form.Item
            name="ontology_id"
            label="本体对象"
            rules={[{ required: true, message: "请选择本体对象" }]}
          >
            <Select
              options={linkables.map((t) => ({
                value: t.id,
                label: `${t.name}${t.alias ? ` (${t.alias})` : ""}`,
              }))}
              placeholder="搜索或选择本体对象"
              showSearch
              filterOption={(input, option) =>
                (option?.label ?? "")
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
            />
          </Form.Item>

          <Form.Item name="weight" label="权重" initialValue={1.0}>
            <Slider min={0} max={1} step={0.1} />
          </Form.Item>

          <Form.Item name="note" label="备注">
            <Input.TextArea maxLength={200} rows={3} />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Space>
              <Button
                type="primary"
                htmlType="submit"
                loading={submitting}
              >
                提交
              </Button>
              <Button
                onClick={() => {
                  setModalOpen(false);
                  form.resetFields();
                }}
              >
                取消
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
