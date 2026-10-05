/**
 * Wiki ↔ Ontology 链接管理页（Task 7 / feat-wiki-category）。
 *
 * 左侧：分类树 + 每个分类下的 wiki pages（叶子可点）。
 * 右侧：选中 page 的 class/property 链接列表 + 添加 Modal。
 *
 * 树结构来源（前端两次请求合并）：
 *  1. GET /wiki/categories/tree — 全量分类树
 *  2. GET /wiki/pages?limit=200 — 全量 page（按 categoryId 客户端挂到分类下）
 * 没挂分类的 page 放进「未分类」伪根，避免丢页。
 *
 * 节点 key 约定：
 *  - 分类节点  ``cat-<id>``（不可点，仅目录）
 *  - page 叶子  ``<pageId>``（可点 → 查 ontology links）
 */
import { useState, useEffect, useCallback, useMemo } from "react";
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
import { useTranslation } from "../i18n";
import { ontologyObjectLabel } from "../utils/ontologyLabel";
import {
  createWikiLink,
  listLinkableTargets,
  listWikiLinks,
  revokeWikiLink,
} from "../api/adminWikiLinks";
import {
  listWikiCategoryTree,
  listWikiPages,
} from "../api/wikiPages";
import type { WikiCategoryNode, WikiPage } from "../types/wikiPages";
import type {
  WikiLink,
  WikiLinkType,
  WikiLinkableTarget,
} from "../types/wikiLink";

/** 分类节点 key 前缀（前端区分「分类节点」与「page 叶子」用）。 */
const CATEGORY_KEY_PREFIX = "cat-";
/** 没挂分类的 page 放进这个伪根。 */
const UNCATEGORIZED_KEY = "cat-0";
const UNCATEGORIZED_TITLE = "未分类";

/** 链接类型 → i18n 键（t() 键为 string，映射表避免模板串拼接的键类型问题）。 */
const TYPE_LABEL_KEY: Record<WikiLinkType, string> = {
  class: "wikiLinks.tabs.class",
  property: "wikiLinks.tabs.property",
  metric: "wikiLinks.tabs.metric",
};

/** 全量 pages → 按 categoryId 分组（key=0 表示未分类）。 */
function pagesByCategory(pages: WikiPage[]): Map<number, WikiPage[]> {
  const out = new Map<number, WikiPage[]>();
  for (const p of pages) {
    const cid = p.categoryId ?? 0;
    if (!out.has(cid)) out.set(cid, []);
    out.get(cid)!.push(p);
  }
  return out;
}

/** 把 pages 挂到对应分类节点下作为叶子；无分类页 → "未分类"伪根。 */
function attachPagesToTree(
  cats: WikiCategoryNode[],
  pages: WikiPage[],
): DataNode[] {
  const byCat = pagesByCategory(pages);
  const uncat = byCat.get(0) ?? [];

  const attach = (node: WikiCategoryNode): DataNode => {
    const catPages = byCat.get(node.id) ?? [];
    return {
      key: `${CATEGORY_KEY_PREFIX}${node.id}`,
      title: `${node.name}（${catPages.length}）`,
      selectable: false,
      children: [
        ...node.children.map(attach),
        ...catPages.map(pageToLeaf),
      ],
    };
  };

  const roots = cats.map(attach);
  if (uncat.length > 0) {
    roots.push({
      key: UNCATEGORIZED_KEY,
      title: `${UNCATEGORIZED_TITLE}（${uncat.length}）`,
      selectable: false,
      children: uncat.map(pageToLeaf),
    });
  }
  return roots;
}

function pageToLeaf(p: WikiPage): DataNode {
  return {
    key: p.pageId,
    title: p.title,
    selectable: true,
    isLeaf: true,
  };
}

/** 收集全部分类节点 key（含子分类），用作 defaultExpandedKeys —— 让首次打开就把整棵树铺平。 */
function allKeys(cats: WikiCategoryNode[]): string[] {
  const out: string[] = [];
  const visit = (c: WikiCategoryNode) => {
    out.push(`${CATEGORY_KEY_PREFIX}${c.id}`);
    c.children.forEach(visit);
  };
  cats.forEach(visit);
  return out;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export function WikiLinksPage() {
  const { t, locale } = useTranslation();
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
  const [linkType, setLinkType] = useState<WikiLinkType>("class");
  const [links, setLinks] = useState<WikiLink[]>([]);
  const [linkables, setLinkables] = useState<WikiLinkableTarget[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [tree, setTree] = useState<WikiCategoryNode[]>([]);
  const [allPages, setAllPages] = useState<WikiPage[]>([]);
  // 用 controlled expandedKeys：tree 数据回来后立刻铺开整树，
  // 避免 antd v5 defaultExpandAll 在「首次 render 数据空 + 二次 render 数据齐」
  // 这条链上失效（实测 defaultExpandedKeys/defaultExpandAll 都救不回来）。
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [form] = Form.useForm();

  // 两次请求合并：分类树 + 全量 pages（按 categoryId 客户端挂载）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [cats, pageList] = await Promise.all([
          listWikiCategoryTree(),
          listWikiPages({ limit: 200 }),
        ]);
        if (cancelled) return;
        setTree(cats);
        setAllPages(pageList.rows);
        setExpandedKeys(allKeys(cats));
      } catch {
        if (!cancelled) message.error(t("wikiLinks.treeLoadFailed"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  // 组装 antd tree data：分类节点 + page 叶子
  const treeData = useMemo(
    () => attachPagesToTree(tree, allPages),
    [tree, allPages],
  );

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
      message.error(t("wikiLinks.loadFailed"));
    }
  }, [selectedPageId, t]);

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
      message.error(t("wikiLinks.loadTargetsFailed"));
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
        chunk_id:
          values.scope === "chunk" ? (values.chunk_id as string) : null,
        note: (values.note as string) || null,
      });
      message.success(t("wikiLinks.addSuccess"));
      setModalOpen(false);
      form.resetFields();
      refresh();
    } catch {
      message.error(t("wikiLinks.addFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  // Revoke a link
  const revoke = async (id: number) => {
    try {
      await revokeWikiLink(id);
      message.success(t("wikiLinks.revokeSuccess"));
      refresh();
    } catch {
      message.error(t("wikiLinks.revokeFailed"));
    }
  };

  const filteredLinks = links.filter((l) => l.ontology_type === linkType);

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("wikiLinks.title")}</h2>
      <div style={{ display: "flex", gap: 16, marginTop: 16 }}>
        {/* Left: wiki page tree */}
        <div style={{ width: 280, border: "1px solid #f0f0f0", padding: 8 }}>
          <div
            style={{ marginBottom: 8, color: "#666", fontSize: 12 }}
          >
            {t("wikiLinks.pageTreeLabel")}
          </div>
          <Tree
            treeData={treeData}
            expandedKeys={expandedKeys}
            onExpand={(keys) => setExpandedKeys(keys as string[])}
            selectedKeys={selectedPageId ? [selectedPageId] : []}
            onSelect={(keys) => {
              const k = keys[0] as string | undefined;
              // 分类节点（cat- 前缀）不可点；只有 page 叶子更新 selectedPageId
              if (k && !k.startsWith(CATEGORY_KEY_PREFIX)) {
                setSelectedPageId(k);
              }
            }}
          />
        </div>

        {/* Right: links panel */}
        <div style={{ flex: 1 }}>
          <Tabs
            activeKey={linkType}
            onChange={(k) => setLinkType(k as WikiLinkType)}
            items={[
              { key: "class", label: t("wikiLinks.tabs.class") },
              { key: "property", label: t("wikiLinks.tabs.property") },
              { key: "metric", label: t("wikiLinks.tabs.metric") },
            ]}
          />
          <Space style={{ marginBottom: 12 }}>
            <Button
              type="primary"
              onClick={openAddModal}
              disabled={!selectedPageId}
            >
              + {t("wikiLinks.addBinding")}
            </Button>
          </Space>

          {filteredLinks.length === 0 ? (
            <div style={{ color: "#999", padding: "24px 0" }}>
              {selectedPageId
                ? t("wikiLinks.noLinks")
                : t("wikiLinks.selectPageHint")}
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
                  {t(TYPE_LABEL_KEY[link.ontology_type])}
                </Tag>
                <span>
                  {link.ontology_name
                    ? ontologyObjectLabel(link.ontology_name, link.ontology_alias, locale)
                    : `ID:${link.ontology_id}`}
                </span>
                <span style={{ color: "#666" }}>
                  weight={link.weight.toFixed(2)}
                </span>
                {link.note && (
                  <span style={{ color: "#999", fontSize: 12 }}>
                    {link.note}
                  </span>
                )}
                <Button
                  danger
                  size="small"
                  style={{ marginLeft: "auto" }}
                  onClick={() => revoke(link.id)}
                >
                  {t("wikiLinks.revoke")}
                </Button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Add link modal */}
      <Modal
        title={t("wikiLinks.addBinding")}
        open={modalOpen}
        onCancel={() => {
          setModalOpen(false);
          form.resetFields();
        }}
        footer={null}
      >
        <Form form={form} onFinish={submit} layout="vertical">
          <Form.Item name="scope" label={t("wikiLinks.scopeLabel")} initialValue="page">
            <Radio.Group>
              <Radio value="page">{t("wikiLinks.scopePage")}</Radio>
              <Radio value="chunk">{t("wikiLinks.scopeChunk")}</Radio>
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
                  label={t("wikiLinks.chunkIdLabel")}
                  rules={[
                    { required: true, message: t("wikiLinks.chunkIdRequired") },
                  ]}
                >
                  <Input placeholder={t("wikiLinks.chunkIdPlaceholder")} />
                </Form.Item>
              ) : null
            }
          </Form.Item>

          <Form.Item
            name="ontology_id"
            label={t("wikiLinks.ontologyObject")}
            rules={[
              { required: true, message: t("wikiLinks.ontologyRequired") },
            ]}
          >
            <Select
              options={linkables.map((tgt) => ({
                value: tgt.id,
                label: ontologyObjectLabel(tgt.name, tgt.alias, locale),
              }))}
              placeholder={t("wikiLinks.ontologyPlaceholder")}
              showSearch
              filterOption={(input, option) =>
                (option?.label ?? "")
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
            />
          </Form.Item>

          <Form.Item
            name="weight"
            label={t("wikiLinks.weight")}
            initialValue={1.0}
          >
            <Slider min={0} max={1} step={0.1} />
          </Form.Item>

          <Form.Item name="note" label={t("wikiLinks.note")}>
            <Input.TextArea maxLength={200} rows={3} />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0 }}>
            <Space>
              <Button type="primary" htmlType="submit" loading={submitting}>
                {t("wikiLinks.submit")}
              </Button>
              <Button
                onClick={() => {
                  setModalOpen(false);
                  form.resetFields();
                }}
              >
                {t("wikiLinks.cancel")}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
