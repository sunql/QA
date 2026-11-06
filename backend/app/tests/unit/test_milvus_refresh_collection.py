"""refreshCollection 原语的单元守卫。

``release + load`` 是本仓库里**唯一**被实测证明能让 Milvus 的 delete 立即可见的机制
（2026-09-27 实测：``consistency_level="Strong"`` 与 ``flush()`` 都无效，
``release + load`` 有效，< 3s 完成，见
``Harness/changes/fix-milvus-delete-visibility/summary.md``）。

⇒ 顺序（先 release 后 load）、必须等待装载完成（否则 load 只是发个请求就返回）、
以及「刷新失败必须向上抛」（绝不静默继续）都要钉死。
"""

from __future__ import annotations

import pytest

import app.infrastructure.milvus_client as milvus_client


class _FakeCollection:
    def __init__(self, name: str = "fake_collection") -> None:
        self.name = name
        self.calls: list[str] = []

    def release(self, **kwargs) -> None:
        self.calls.append("release")

    def load(self, **kwargs) -> None:
        self.calls.append("load")


@pytest.fixture()
def fakeEnv(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(milvus_client, "_connect", lambda: None)
    return monkeypatch


def test_release_then_load_in_that_order(fakeEnv):
    """顺序必须是 release → load → wait（反过来等于先装载再卸载；先等后装等于白等）。"""
    fake = _FakeCollection()
    waits: list[tuple] = []

    def _fakeWait(collectionName, partitionNames=None, timeout=None, using="default"):
        waits.append((collectionName, timeout, using))
        fake.calls.append("wait")
        return True

    fakeEnv.setattr(milvus_client.utility, "wait_for_loading_complete", _fakeWait)

    milvus_client.refreshCollection(fake)

    assert fake.calls == ["release", "load", "wait"], f"调用顺序错：{fake.calls}"
    assert len(waits) == 1, "必须等待装载完成，否则 load 只是发个请求就返回"
    assert waits[0][0] == "fake_collection"
    assert waits[0][2] == milvus_client._connAlias()
    assert waits[0][1] is not None, "必须给等待设置超时，否则可能永久挂起"


def test_wait_failure_propagates(fakeEnv):
    """刷新没成功必须让调用方知道 —— 绝不静默继续。"""
    fake = _FakeCollection()

    def _boom(*args, **kwargs):
        raise RuntimeError("load timeout")

    fakeEnv.setattr(milvus_client.utility, "wait_for_loading_complete", _boom)

    with pytest.raises(RuntimeError, match="load timeout"):
        milvus_client.refreshCollection(fake)


def test_wiki_and_document_helpers_delegate_without_blocking_on_load(
    fakeEnv, monkeypatch: pytest.MonkeyPatch
):
    """两个既有 helper 必须收敛到同一个原语，且**保持原来的非阻塞语义**。

    ⚠️ 它们在本次改动前只做 ``release() + load()``（``load()`` 只是发个请求就返回）
    ⇒ 既有集成用例（``test_milvus_wiki_fields.py`` / ``test_milvus_document_fields.py``）
    的行为**不得**因此改变 ⇒ 必须显式传 ``waitForLoad=False``。
    回填脚本要的是**阻塞**版本（``waitForLoad=True``）—— 那才是让 delete 立即可见的前提。
    """
    calls: list[tuple[object, dict]] = []

    def _fakeRefresh(collection, **kwargs):
        calls.append((collection, kwargs))

    monkeypatch.setattr(milvus_client, "refreshCollection", _fakeRefresh)
    fakeEnv.setattr(milvus_client, "ensureWikiPageCollection", lambda: "wiki")
    fakeEnv.setattr(milvus_client, "ensureDocumentCollection", lambda: "doc")

    milvus_client.refreshWikiCollection()
    milvus_client.refreshDocumentCollection()

    assert [c for c, _ in calls] == ["wiki", "doc"]
    assert all(kwargs.get("waitForLoad") is False for _, kwargs in calls), (
        f"既有 helper 不得改变阻塞语义：{calls}"
    )


def test_default_variant_is_the_blocking_one(fakeEnv):
    """**默认**必须是阻塞版（等装载完成）—— 回填脚本按默认调用它。

    不等装载完成就复扫等于没刷新（``load()`` 只是发个请求就返回）。
    既有 helper 显式传 ``waitForLoad=False`` 来保持原语义（见上一条用例）。
    """
    import inspect

    signature = inspect.signature(milvus_client.refreshCollection)
    assert "waitForLoad" in signature.parameters
    assert signature.parameters["waitForLoad"].default is True, (
        "默认必须是阻塞版（等装载完成）"
    )
