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
        snapshot = _FakeSnapshot({"schemaVersion": "v1", "classes": []})
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
            "schemaVersion": "v1",
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
