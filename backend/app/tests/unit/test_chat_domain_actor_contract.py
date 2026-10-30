"""领域命令（/define /map /metric）actor 契约守卫（2026-10-30）。

**缺陷背景**：`OntologyService.createClass` / `updateProperty` 的 `actor` 是
**必填 keyword-only** 参数（2026-09-03 起 `689e4a6e`）。`chat_domain.py` 的
/define 与 /map 两处**从未传 actor** ⇒ 生产必然 TypeError ⇒ 这两个命令自
2026-09-03 起事实上全灭（/metric 路径同样漏传，2026-10-28 才被发现）。

**为什么单测全绿**：单测替身 `_FakeOntologyService` 的签名**比真实实现更宽松**
（当时没有 `actor` 形参）⇒ 替身比被测物宽松 ⇒ 调用点漏传也不会报错 ⇒ 假绿。
这正是「假替身保真度」失效模式：替身与真实实现的契约漂移没人守。

本文件两道防线：

1. **保真守卫**（`TestFakeFidelity`）：逐方法比对替身与真实 `OntologyService`
   的签名——真实实现里**必填**的参数，替身必须存在**且同样必填**（既不许
   更宽松，也不许更严格）；同时钉住"被比较的方法集合非空"（防空集合恒真）。
2. **行为守卫**（`TestDomainCommandActor`）：三条领域命令必须把 actor 传下去；
   认证用户传 `userId`，匿名传 `"chat"`。配**反向守卫**：证明真实实现缺 actor
   立即 TypeError（否则"行为守卫"也可能因为别的原因而绿）。

护栏：守卫断言的都是"**是谁**拒的 / **传了什么**"，不是"结果等于空/非空"
这类恒真口径。
"""

from __future__ import annotations

import inspect

import pytest

from app.dependencies import CurrentUser
from app.domain.enums import IntentType
from app.domain.models import OntologyClass, OntologyProperty
from app.services.chat_service import ChatService
from app.services.ontology_service import OntologyService
from app.tests.unit.test_chat_service import (
    _FakeAdapter,
    _FakeDatasourceService,
    _FakeEmbeddingService,
    _FakeOntologyService,
    _FakeRouter,
    _FakeSession,
    _FakeTokenUsage,
    _PipelineLlm,
    _config,
    _datasource,
    _dto,
)

# 替身必须保真覆盖的真实方法（领域命令写路径）
_FIDELITY_METHODS: tuple[str, ...] = ("createClass", "createMetric", "updateProperty")

_ANONYMOUS_ACTOR = "chat"


def _requiredParams(sig: inspect.Signature) -> dict[str, inspect.Parameter]:
    """签名里没有默认值的参数（self 除外）——真实实现的"必填契约"。"""
    return {
        name: param
        for name, param in sig.parameters.items()
        if name != "self" and param.default is inspect.Parameter.empty
    }


class TestFakeFidelity:
    """替身保真守卫：替身签名 vs 真实 OntologyService 签名。"""

    def test_compared_method_set_is_not_empty(self) -> None:
        """防空集合恒真：被比较的方法集合必须非空且方法确实存在于真实实现。"""
        assert _FIDELITY_METHODS
        for name in _FIDELITY_METHODS:
            assert hasattr(OntologyService, name), f"真实实现缺方法 {name}"
            assert hasattr(_FakeOntologyService, name), f"替身缺方法 {name}"

    @pytest.mark.parametrize("methodName", _FIDELITY_METHODS)
    def test_fake_keeps_required_params_required(self, methodName: str) -> None:
        """真实必填的参数，替身必须同样必填（不许给默认值 ⇒ 不许更宽松）。"""
        realSig = inspect.signature(getattr(OntologyService, methodName))
        fakeSig = inspect.signature(getattr(_FakeOntologyService, methodName))
        realRequired = _requiredParams(realSig)
        assert realRequired, f"真实 {methodName} 无必填参数——保真规则需重估"
        for paramName, realParam in realRequired.items():
            assert paramName in fakeSig.parameters, (
                f"替身 {methodName} 缺少真实实现的必填参数 {paramName}"
                "（真实实现要求它 ⇒ 漏传即 TypeError）"
            )
            fakeParam = fakeSig.parameters[paramName]
            assert fakeParam.default is inspect.Parameter.empty, (
                f"替身 {methodName}.{paramName} 有默认值 ⇒ 比真实更宽松 ⇒ "
                "调用点漏传该参数时单测假绿（真实实现会 TypeError）"
            )
            assert fakeParam.kind is realParam.kind, (
                f"替身 {methodName}.{paramName} 的传参方式与真实不一致："
                f"{fakeParam.kind} != {realParam.kind}"
            )

    def test_real_create_class_rejects_missing_actor(self) -> None:
        """反向守卫：真实实现缺 actor **立即** TypeError（证明旧调用点必全灭）。"""
        with pytest.raises(TypeError, match="actor"):
            OntologyService.createClass(None, None, None)  # type: ignore[arg-type]

    def test_real_update_property_rejects_missing_actor(self) -> None:
        """反向守卫：真实 updateProperty 缺 actor 同样立即 TypeError。"""
        with pytest.raises(TypeError, match="actor"):
            OntologyService.updateProperty(None, None, None, None)  # type: ignore[arg-type]


class TestDomainCommandActor:
    """行为守卫：三条领域命令必须把 actor（及部门）传到底层本体服务。"""

    async def _build(
        self, classes: list[OntologyClass] | None = None
    ) -> tuple[ChatService, _FakeOntologyService]:
        ontology = _FakeOntologyService(classes)
        service = ChatService(
            datasourceService=_FakeDatasourceService(_datasource()),
            ontologyService=ontology,
            modelRouterService=_FakeRouter(_config()),
            tokenUsageService=_FakeTokenUsage(),
            embeddingService=_FakeEmbeddingService(),
            llmFactory=lambda config: _PipelineLlm(),
            adapterProvider=lambda datasourceId, ds: _FakeAdapter([]),
        )
        return service, ontology

    async def test_define_passes_authenticated_user_as_actor(self) -> None:
        """认证用户：createClass 的 actor 必须是该用户 id（不是泛化的 "chat"）。"""
        service, ontology = await self._build()
        user = CurrentUser(userId="alice", departments=("采购部", "财务部"))
        await service.processMessage(
            _dto("/define 产品"), _FakeSession(), user=user
        )
        assert ontology.createdClasses, "createClass 未被调用（断言对象为空 ⇒ 无效断言）"
        assert ontology.createdClassActors == ["alice"]

    async def test_define_anonymous_falls_back_to_chat_actor(self) -> None:
        """匿名链路（无 CurrentUser）：actor 退化为固定串，不能是 None（审计必填）。"""
        service, ontology = await self._build()
        await service.processMessage(_dto("/define 产品"), _FakeSession())
        assert ontology.createdClasses
        assert ontology.createdClassActors == [_ANONYMOUS_ACTOR]

    async def test_map_passes_authenticated_user_as_actor(self) -> None:
        """认证用户：updateProperty 的 actor 必须是该用户 id。"""
        prop = OntologyProperty(
            id=10, class_id=1, property_name="customer_name", data_type="STRING"
        )
        customer = OntologyClass(id=1, class_name="Customer", properties=[prop])
        order = OntologyClass(id=2, class_name="Order")
        service, ontology = await self._build(classes=[customer, order])
        user = CurrentUser(userId="bob", departments=("销售部",))
        await service.processMessage(
            _dto("把 customer_name 映射到 Order"), _FakeSession(), user=user
        )
        assert ontology.updatedProperties, "updateProperty 未被调用（断言对象为空）"
        assert ontology.updatedPropertyActors == ["bob"]

    async def test_metric_passes_authenticated_user_as_actor(self) -> None:
        """认证用户：createMetric 的 actor 必须是该用户 id。"""
        service, ontology = await self._build()
        user = CurrentUser(userId="carol", departments=("采购部",))
        await service.processMessage(
            _dto("定义指标 销售额 = SUM(order.amount)"), _FakeSession(), user=user
        )
        assert ontology.createdMetrics, "createMetric 未被调用（断言对象为空）"
        assert ontology.createdMetricActors == ["carol"]

    async def test_define_intent_is_still_class_define(self) -> None:
        """反向守卫：上面的 actor 断言必须建立在**真的走了 /define 路径**上。"""
        service, ontology = await self._build()
        response = await service.processMessage(_dto("/define 产品"), _FakeSession())
        assert response.intent == IntentType.DEFINE.value
        assert ontology.createdMetrics == [], "/define 不应顺带建指标"
