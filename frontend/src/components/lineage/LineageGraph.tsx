/** LineageGraph — ECharts graph 渲染数据血缘边（Phase 2.3）。
 *
 * 核心思路：
 * - edgesToGraphOption 为纯函数（输入 LineageEdgeRead[] → ECharts graph option）；
 *   可单测、可在非 ECharts 环境复用（如 SSR / 导出 SVG）。
 * - 节点：表级血缘用对象名；字段级血缘用 "object.field" 形式（同一对象不同字段各算独立节点）
 * - 颜色：每层一种色（SOURCE_SYSTEM 蓝、ODS 青、DWD 蓝绿、DWS 绿、ADS 橙、KPI 红、AI 紫）
 * - 边：active=true 实线 / active=false 虚线；transformationRule 作 label 显示
 * - 力导向布局（force layout）便于看层级结构
 */
import ReactECharts from "echarts-for-react";
import type { LineageEdgeRead, LineageLayer } from "../../types/lineage";

export const LAYER_COLORS: Record<LineageLayer, string> = {
  SOURCE_SYSTEM: "#1677ff", // 主蓝
  ODS: "#13c2c2", // 青
  DWD: "#52c41a", // 绿
  DWS: "#722ed1", // 紫
  ADS: "#fa8c16", // 橙
  KPI: "#f5222d", // 红
  AI: "#eb2f96", // 品红
};

export interface GraphNode {
  id: string;
  name: string;
  category: LineageLayer;
  itemStyle: { color: string };
  symbolSize: number;
  isField: boolean;
}

export interface GraphLink {
  source: string;
  target: string;
  lineStyle: { color: string; type: "solid" | "dashed"; width: number };
  label: { show: boolean; formatter: string };
}

export interface GraphOption {
  nodes: GraphNode[];
  links: GraphLink[];
}

function nodeId(object: string, field: string | null): string {
  return field ? `${object}.${field}` : object;
}

function nodeName(object: string, field: string | null): string {
  return field ? `${object}.${field}` : object;
}

export function edgesToGraphOption(edges: LineageEdgeRead[]): GraphOption {
  const nodeMap = new Map<string, GraphNode>();
  const links: GraphLink[] = [];

  for (const edge of edges) {
    const srcId = nodeId(edge.sourceObject, edge.sourceField);
    const tgtId = nodeId(edge.targetObject, edge.targetField);

    if (!nodeMap.has(srcId)) {
      nodeMap.set(srcId, {
        id: srcId,
        name: nodeName(edge.sourceObject, edge.sourceField),
        category: edge.sourceLayer,
        itemStyle: { color: LAYER_COLORS[edge.sourceLayer] },
        symbolSize: edge.sourceField ? 28 : 40,
        isField: edge.sourceField !== null,
      });
    }
    if (!nodeMap.has(tgtId)) {
      nodeMap.set(tgtId, {
        id: tgtId,
        name: nodeName(edge.targetObject, edge.targetField),
        category: edge.targetLayer,
        itemStyle: { color: LAYER_COLORS[edge.targetLayer] },
        symbolSize: edge.targetField ? 28 : 40,
        isField: edge.targetField !== null,
      });
    }

    const ruleText = edge.transformationRule ?? "";
    const linkLabel = ruleText.length > 30 ? `${ruleText.slice(0, 27)}...` : ruleText;

    links.push({
      source: srcId,
      target: tgtId,
      lineStyle: {
        color: "#999",
        type: edge.isActive ? "solid" : "dashed",
        width: edge.isActive ? 1.5 : 1,
      },
      label: {
        show: ruleText.length > 0,
        formatter: linkLabel,
      },
    });
  }

  return { nodes: Array.from(nodeMap.values()), links };
}

interface LineageGraphProps {
  edges: LineageEdgeRead[];
  height?: number;
}

/** ECharts 渲染的 graph option（最终传给 ReactECharts）。*/
function buildEChartsOption(option: GraphOption): Record<string, unknown> {
  return {
    tooltip: {
      trigger: "item",
      formatter: (params: { dataType?: string; data?: { name?: string; transformationRule?: string } }) => {
        if (params.dataType === "edge" && params.data?.transformationRule) {
          return `规则：${params.data.transformationRule}`;
        }
        return params.data?.name ?? "";
      },
    },
    legend: [
      {
        data: Object.keys(LAYER_COLORS),
        textStyle: { fontSize: 11 },
        top: 8,
        left: 8,
      },
    ],
    series: [
      {
        type: "graph",
        layout: "force",
        roam: true,
        draggable: true,
        force: { repulsion: 300, edgeLength: 120 },
        categories: Object.keys(LAYER_COLORS).map((name) => ({
          name,
          itemStyle: { color: LAYER_COLORS[name as LineageLayer] },
        })),
        nodes: option.nodes.map((n) => ({
          id: n.id,
          name: n.name,
          category: n.category,
          symbolSize: n.symbolSize,
          itemStyle: n.itemStyle,
        })),
        links: option.links.map((l) => ({
          source: l.source,
          target: l.target,
          lineStyle: l.lineStyle,
          label: l.label,
        })),
        edgeLabel: { fontSize: 10, show: false },
        label: { show: true, position: "right", fontSize: 11 },
        emphasis: { focus: "adjacency", lineStyle: { width: 3 } },
      },
    ],
  };
}

export default function LineageGraph({ edges, height = 600 }: LineageGraphProps) {
  if (edges.length === 0) {
    return null;
  }
  const option = buildEChartsOption(edgesToGraphOption(edges));
  return <ReactECharts option={option} style={{ height }} notMerge />;
}
