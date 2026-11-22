import { describe, expect, it, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  captureSnapshot,
  createSource,
  getSnapshot,
  listDatasources,
  listSnapshots,
  listSources,
  monitorSource,
  startRun,
  updateSource,
} from "../api/discoveryAdmin";
import {
  listOntologyClasses,
  listOntologyMetrics,
  listOntologyProperties,
} from "../api/ontologyTargets";

describe("api/discoveryAdmin", () => {
  beforeEach(() => vi.clearAllMocks());

  it("数据源/发现源/快照列表", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });

    await listDatasources();
    await listSources();
    await listSnapshots({ sourceId: 1 });

    expect(httpMock.get).toHaveBeenNthCalledWith(1, "/datasources");
    expect(httpMock.get).toHaveBeenNthCalledWith(2, "/ontology-discovery/sources", {
      params: { limit: 100, offset: 0 },
    });
    expect(httpMock.get).toHaveBeenNthCalledWith(3, "/ontology-discovery/snapshots", {
      params: { sourceId: 1, limit: 20, offset: 0 },
    });
  });

  it("登记/更新发现源走 POST/PATCH", async () => {
    httpMock.post.mockResolvedValue({ data: {} });
    httpMock.patch.mockResolvedValue({ data: {} });

    await createSource({
      name: "源",
      datasourceId: 1,
      owner: "o",
      accessPolicy: { mode: "READ_ONLY" },
    });
    await updateSource(3, { status: "DISABLED" });

    expect(httpMock.post).toHaveBeenCalledWith("/ontology-discovery/sources", {
      name: "源",
      datasourceId: 1,
      owner: "o",
      accessPolicy: { mode: "READ_ONLY" },
    });
    expect(httpMock.patch).toHaveBeenCalledWith("/ontology-discovery/sources/3", {
      status: "DISABLED",
    });
  });

  it("采集/监测/运行/快照详情", async () => {
    httpMock.post.mockResolvedValue({ data: {} });
    httpMock.get.mockResolvedValue({ data: {} });

    await captureSnapshot(2);
    await monitorSource(2);
    await startRun({ sourceId: 2, tables: ["T"], withCandidates: false });
    await getSnapshot(9);

    expect(httpMock.post).toHaveBeenNthCalledWith(1, "/ontology-discovery/snapshots?sourceId=2");
    expect(httpMock.post).toHaveBeenNthCalledWith(2, "/ontology-discovery/sources/2/monitor");
    expect(httpMock.post).toHaveBeenNthCalledWith(3, "/ontology-discovery/runs", {
      sourceId: 2,
      tables: ["T"],
      withCandidates: false,
    });
    expect(httpMock.get).toHaveBeenCalledWith("/ontology-discovery/snapshots/9");
  });
});

describe("api/ontologyTargets", () => {
  beforeEach(() => vi.clearAllMocks());

  it("类/属性/指标各走 GET", async () => {
    httpMock.get.mockResolvedValue({ data: [] });

    await listOntologyClasses();
    await listOntologyProperties();
    await listOntologyMetrics();

    expect(httpMock.get).toHaveBeenNthCalledWith(1, "/ontology/classes");
    expect(httpMock.get).toHaveBeenNthCalledWith(2, "/ontology/properties");
    expect(httpMock.get).toHaveBeenNthCalledWith(3, "/ontology/metrics");
  });
});
