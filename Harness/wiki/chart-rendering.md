# 图表渲染（Phase 4）

## 原则

后端只返回 ECharts 的 `option` JSON 结构，前端只渲染不处理数据，降低耦合。

## 图表类型智能推荐

未指定 `chartType` 时按查询结果特征自动判断：

| 结果特征 | 推荐图表 |
|----------|----------|
| 1 维度(String) + 1 指标(Number) | 饼图 或 柱状图 |
| 1 维度(时间) + 1 指标 | 折线图 |
| 2 维度 + 1 指标 | 分组柱状图 或 热力图 |
| 其他 | 表格 |

## Option 生成

`services/chart_service.py`：

- `table`：构建 HTML 表格数据。
- `pie`：第一列为标签，第二列为数值。
- `bar`：维度为 xAxis，指标为 series。
- `line`：时间维度为 xAxis，指标为 series。

## 数据类型识别

- 维度列：String / 时间类型。
- 指标列：Number 类型。
- 多指标：生成多 series。

## 前端

`frontend/src/components/chat/ChartRenderer.tsx`：用 `echarts-for-react` 直接渲染后端返回的 `option`，大数据量启用 `dataZoom` 与采样。
