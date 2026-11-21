"""影子比对脚本（D13）口径的单测：退出码语义 + 子集切分 + 差异指标。

为什么这些必须钉死
------------------
- **退出码语义**：内容差异 0 但顺序有差异 ⇒ 退出码必须是 **3**（不是 0）。
  退出码 0 被当成「完全一致」，而 D14 的判据是「内容等价 + 顺序等价」两条
  分开计量 ⇒ 顺序差异被吞掉 = 判据失效。
- **子集切分**：生产渲染的是**召回子集**（≤ 30 类），子集外的父类 / 外键目标
  解析成 `None` 这条逻辑**只在子集里才被触发**。只比全集 ⇒ 这条逻辑一次都
  没跑到 ⇒ 比对工具发现不了子集语义下的偏差。
- **差异指标分两把尺**：内容差异（行多重集）与顺序敏感差异（逐行序列）必须
  分开报，否则「排序问题」会被误报成「快照丢了事实」。
"""

from __future__ import annotations

import pytest

from app.domain.semantic_manifest_schema import SCHEMA_VERSION
from scripts import semantic_schema_shadow_diff as diffModule


class Test子集切分:
    def test_全集模式只产出一个子集(self) -> None:
        subsets = diffModule._subsets(["A", "B", "C"], "none")
        assert len(subsets) == 1
        assert subsets[0][1] == ["A", "B", "C"]

    def test_逐类模式把每个类单独切成一个子集(self) -> None:
        subsets = diffModule._subsets(["A", "B", "C"], "each")
        assert [label for label, _ in subsets] == ["A", "B", "C"]
        assert subsets[1][1] == ["B"]

    def test_分组模式按固定大小分组(self) -> None:
        names = [f"C{i}" for i in range(7)]
        subsets = diffModule._subsets(names, "groups")
        assert [len(namesInSubset) for _, namesInSubset in subsets] == [5, 2]
        assert subsets[0][1] == names[:5]
        assert subsets[1][1] == names[5:]

    def test_未知模式直接拒绝(self) -> None:
        with pytest.raises(SystemExit):
            diffModule._subsets(["A"], "unknown")


class Test差异指标:
    def test_内容差异忽略行序(self) -> None:
        """同一批事实换个顺序 ⇒ 内容差异 0（顺序差异另计）。"""
        left = ["### A", "  x: STRING", "  y: INT"]
        right = ["### A", "  y: INT", "  x: STRING"]
        assert diffModule._contentDiff(left, right) == 0

    def test_内容差异能抓出少了一行(self) -> None:
        left = ["### A", "  x: STRING", "  y: INT"]
        right = ["### A", "  x: STRING"]
        assert diffModule._contentDiff(left, right) == 1

    def test_内容差异能抓出多了一行(self) -> None:
        left = ["### A", "  x: STRING"]
        right = ["### A", "  x: STRING", "  z: DATE"]
        assert diffModule._contentDiff(left, right) == 1

    def test_仅顺序不同的块被单独归因(self) -> None:
        left = ["### A", "  x: STRING", "  y: INT"]
        right = ["### A", "  y: INT", "  x: STRING"]
        assert diffModule._orderOnlyClasses(left, right) == ["### A"]

    def test_内容不同的块不算仅顺序差异(self) -> None:
        left = ["### A", "  x: STRING", "  y: INT"]
        right = ["### A", "  x: STRING", "  z: DATE"]
        assert diffModule._orderOnlyClasses(left, right) == []


class Test顺序差异归因:
    """D14：顺序差异必须**全部**归因到「类内列序」，否则「已知差异」的说法失效。

    全局「行多重集相等」不足以证明两侧渲染的是**同一批事实** —— 一行从类 A 的
    块搬到类 B 的块，全局仍然相等，但事实归属变了（列挂到了别的表上）。
    ⇒ 「顺序差异 = 已知差异」这个结论必须建立在「逐块内容都相等」之上。
    """

    def test_逐块内容都相等时没有归因缺口(self) -> None:
        left = ["### A", "  x: STRING", "  y: INT"]
        right = ["### A", "  y: INT", "  x: STRING"]
        assert diffModule._blockContentMismatches(left, right) == []

    def test_某块少了一行会被列为归因缺口(self) -> None:
        left = ["### A", "  x: STRING", "  y: INT"]
        right = ["### A", "  x: STRING"]
        assert diffModule._blockContentMismatches(left, right) == ["### A"]

    def test_行在块之间搬家会被列为归因缺口(self) -> None:
        """内容多重集整体相等，但一行从 A 块搬到 B 块 ⇒ 事实归属变了。"""
        left = ["### A", "  x: STRING", "### B", "  y: INT"]
        right = ["### A", "### B", "  x: STRING", "  y: INT"]
        assert diffModule._blockContentMismatches(left, right) == ["### A", "### B"]

    def test_快照侧多出的块也被列为归因缺口(self) -> None:
        left = ["### A", "  x: STRING"]
        right = ["### A", "  x: STRING", "### B", "  y: INT"]
        assert diffModule._blockContentMismatches(left, right) == ["### B"]


class Test退出码语义:
    def test_内容与顺序都一致时退出0(self) -> None:
        assert diffModule._exitCode(0, 0) == 0

    def test_有内容差异时退出1(self) -> None:
        assert diffModule._exitCode(3, 0) == 1

    def test_内容一致但顺序有差异时退出3(self) -> None:
        """D14 判据分两条：内容等价 + 顺序等价。顺序没归零就不能报「完全一致」。"""
        assert diffModule._exitCode(0, 12) == 3

    def test_内容差异优先于顺序差异(self) -> None:
        assert diffModule._exitCode(5, 12) == 1

    def test_有归因缺口时退出1而不是3(self) -> None:
        """内容多重集整体相等但块归属不同 ⇒ 不是纯列序 ⇒ 按内容差异处理。"""
        assert diffModule._exitCode(0, 12, blockMismatches=2) == 1

    def test_块顺序变了退出1而不是3(self) -> None:
        """块（类）之间的先后变了 ⇒ **不是** D14 接受的「已知顺序差异」。

        D14 接受的已知差异只有两类：类内列序、JOIN 条目序。
        块顺序不在其中 ⇒ 归到「必须修」⇒ 退出码 1。
        """
        assert diffModule._exitCode(0, 12, blockOrderGap=2) == 1


class _FakeSnapshot:
    """Registry 快照替身：影子比对只读这几个字段。"""

    def __init__(self, manifest: dict, schema_version: str | None = None) -> None:
        self.id = 1
        self.version = "v-test"
        self.manifest_hash = "hash"
        self.manifest = manifest
        self.schema_version = schema_version or manifest.get("schemaVersion")


class Test版本闸门:
    """影子比对必须走**运行时同一个**版本闸门。

    否则版本漂移会被误判成「快照丢了事实」⇒ 白白排查一轮。
    """

    def test_版本一致时没有错误(self) -> None:
        snapshot = _FakeSnapshot({"schemaVersion": SCHEMA_VERSION, "classes": []})
        assert diffModule._versionGate(snapshot) is None

    def test_版本不一致时给出错误说明(self) -> None:
        snapshot = _FakeSnapshot({"schemaVersion": "v0", "classes": []})
        message = diffModule._versionGate(snapshot)
        assert message is not None
        assert "版本" in message

    def test_缺版本号时也给出错误说明(self) -> None:
        snapshot = _FakeSnapshot({"classes": []})
        message = diffModule._versionGate(snapshot)
        assert message is not None
        assert "缺失" in message


class Test类集合硬断言:
    def test_两侧类集合相等时没有错误(self) -> None:
        assert diffModule._classSetMismatch(["A", "B"], ["B", "A"]) is None

    def test_快照少一个类时报错(self) -> None:
        message = diffModule._classSetMismatch(["A", "B"], ["A"])
        assert message is not None
        assert "B" in message

    def test_快照多一个类时报错(self) -> None:
        message = diffModule._classSetMismatch(["A"], ["A", "C"])
        assert message is not None
        assert "C" in message


class Test基线取全集:
    """基线 = 直读侧**全集**文本；不能是某个子集的文本。

    否则 `--subset-mode each --baseline-out x.txt` 会把「第一个类」的
    schema 文本写成基线 ⇒ 后续拿它比对得到假绿/假红，而文件本身不报错。
    """

    def test_基线包含全部类而不是单个子集(self) -> None:
        from app.services.semantic_schema_adapter import restoreSnapshotSchema

        manifest = {
            "schemaVersion": SCHEMA_VERSION,
            "classes": [
                {"className": "Supplier", "sourceTable": "SUPPLIER", "properties": []},
                {
                    "className": "PurchaseOrder",
                    "sourceTable": "PURCHASE_ORDER",
                    "properties": [],
                },
            ],
            "joins": [],
        }
        classes, joins = restoreSnapshotSchema(manifest, ["Supplier", "PurchaseOrder"])
        text = diffModule._baselineText(classes, joins)
        assert "### Supplier" in text
        assert "### PurchaseOrder" in text


class Test写文件:
    """报告/基线里是**生产元数据** ⇒ 权限 0600 + 不跟随符号链接。"""

    def test_写出的文件权限是0600(self, tmp_path) -> None:
        target = tmp_path / "report.txt"
        diffModule._write(str(target), "内容")
        assert target.read_text(encoding="utf-8") == "内容"
        assert oct(target.stat().st_mode)[-3:] == "600"

    def test_目标是符号链接时拒绝写入(self, tmp_path) -> None:
        """软链能把生产元数据写到别处 ⇒ 写之前必须拒绝跟随。"""
        real = tmp_path / "real.txt"
        real.write_text("原有内容", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        with pytest.raises(SystemExit):
            diffModule._write(str(link), "新内容")
        assert real.read_text(encoding="utf-8") == "原有内容"


class Test顺序归因分类:
    """D14 的顺序判据：**每个顺序差异块都要归到一类**，归因缺口必须为 0。

    为什么不能只报一个「顺序敏感差异行数」：那个数字只说明「有差异」，
    不说明「差异是什么」。D14 要回答的是「切换到快照路径后，prompt 变了什么」
    ⇒ 必须能把差异归到「类内列序」还是「JOIN 条目序」两类，
    并确认**没有第三类**（那就是归因缺口 = 事实变了 = 内容差异）。
    """

    def test_类块里的顺序差异归到类块(self) -> None:
        direct = ["### A (甲): table=A", "  col1: STRING", "  col2: INT"]
        snapshot = ["### A (甲): table=A", "  col2: INT", "  col1: STRING"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert attribution["byKind"] == {"类块": 1}
        assert attribution["gaps"] == []

    def test_JOIN段的顺序差异归到JOIN段(self) -> None:
        direct = ["### JOIN 关系", "  A.x → B.x", "  A.y → B.y"]
        snapshot = ["### JOIN 关系", "  A.y → B.y", "  A.x → B.x"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert attribution["byKind"] == {"JOIN 段": 1}
        assert attribution["gaps"] == []

    def test_内容不等的块算归因缺口而不是顺序差异(self) -> None:
        """块里少了一行 ⇒ 差异**不是**顺序问题 ⇒ 必须进缺口（⇒ 退出码 1）。"""
        direct = ["### A (甲): table=A", "  col1: STRING", "  col2: INT"]
        snapshot = ["### A (甲): table=A", "  col1: STRING"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert attribution["byKind"] == {}
        assert attribution["gaps"] == ["### A (甲): table=A"]

    def test_完全一致的块不进归因(self) -> None:
        lines = ["### A (甲): table=A", "  col1: STRING"]

        attribution = diffModule._orderAttribution(lines, list(lines))

        assert attribution["byKind"] == {}
        assert attribution["gaps"] == []

    def test_块之间的顺序不同归到块顺序(self) -> None:
        """两个类块整体换了位置 ⇒ 差异在**块之间** ⇒ 归到「块顺序」。

        这类差异比块内列序更值得警惕：块的先后会改变模型看到的重点顺序。
        ⇒ 单独归一类，不许混进「类内列序」。
        """
        direct = ["### A (甲): table=A", "  col1: STRING", "### B (乙): table=B", "  col2: INT"]
        snapshot = ["### B (乙): table=B", "  col2: INT", "### A (甲): table=A", "  col1: STRING"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert attribution["byKind"] == {"块顺序": 2}
        assert attribution["gaps"] == []

    def test_块顺序一致时不归到块顺序(self) -> None:
        direct = ["### A (甲): table=A", "  col1: STRING", "### B (乙): table=B", "  col2: INT"]
        snapshot = ["### A (甲): table=A", "  col1: STRING", "### B (乙): table=B", "  col2: INT"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert "块顺序" not in attribution["byKind"]
        assert attribution["gaps"] == []

    def test_摘要段的顺序差异算归因缺口而不是类块(self) -> None:
        """「关键过滤口径摘要」**不是**类块 ⇒ 它的顺序差异不在已知差异清单里。

        排除法分类的漏洞：只要「不是 JOIN 段」就归成「类块」，任何新段落都能
        借「类块」的名义绕过退出码 1 ⇒ 已知差异清单就不再封闭。
        """
        header = "### 关键过滤口径摘要（管理员维护的业务口径，生成查询时必须遵循）"
        direct = [header, "  a: 1", "  b: 2"]
        snapshot = [header, "  b: 2", "  a: 1"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert "类块" not in attribution["byKind"]
        assert attribution["gaps"] == [header]

    def test_间接JOIN段的顺序差异归到JOIN段(self) -> None:
        """「间接 JOIN 路径参考」也是 JOIN 条目列表 ⇒ 归 JOIN 段（已知差异）。"""
        header = "### 间接 JOIN 路径参考（无直接外键时可经中间表中转）"
        direct = [header, "  A → B", "  C → D"]
        snapshot = [header, "  C → D", "  A → B"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert attribution["byKind"] == {"JOIN 段": 1}
        assert attribution["gaps"] == []

    def test_未知段落类型的顺序差异算归因缺口(self) -> None:
        """未来新增的段落类型 ⇒ 既不是类块也不是 JOIN 段 ⇒ 进缺口（退出码 1）。

        这条是「已知差异清单封闭」的兜底：清单外的顺序差异必须逼人显式决定，
        不许默认按「类内列序」放行。
        """
        header = "### 未来新增段落"
        direct = [header, "  a", "  b"]
        snapshot = [header, "  b", "  a"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert attribution["byKind"] == {}
        assert attribution["gaps"] == [header]

    def test_公共段落的顺序差异算归因缺口(self) -> None:
        """首个 `### ` 之前的公共段落 ⇒ 空标题 ⇒ 不在清单内 ⇒ 进缺口。"""
        direct = ["前言", "  a", "  b"]
        snapshot = ["前言", "  b", "  a"]

        attribution = diffModule._orderAttribution(direct, snapshot)

        assert attribution["byKind"] == {}
        assert attribution["gaps"] == [""]
