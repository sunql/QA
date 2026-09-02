from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.domain.agent_vocabulary import (
    AGENT_DATA_DOMAINS,
    AGENT_DATA_LAYERS,
    normalizeAgentDomain,
)
from app.domain.error_messages import (
    MSG_AGENT_DOMAIN_NOT_IN_VOCAB,
    MSG_AGENT_LAYER_NOT_IN_VOCAB,
)
from app.domain.schemas import AgentDefinitionCreate


class TestConstants:
    def test_domains_constant(self) -> None:
        assert AGENT_DATA_DOMAINS == ("PROCUREMENT", "QUALITY", "LOGISTICS")

    def test_layers_constant(self) -> None:
        assert AGENT_DATA_LAYERS == ("DIM", "DWD", "FEATURE")


class TestNormalizeAgentDomain:
    def test_strips_and_uppercases(self) -> None:
        assert normalizeAgentDomain("  procurement ") == "PROCUREMENT"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            normalizeAgentDomain("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError):
            normalizeAgentDomain("   ")


class TestAgentDefinitionCreateValidation:
    def _dto(self, **kwargs) -> AgentDefinitionCreate:
        base = {
            "agentCode": "TEST_AGENT",
            "agentName": "test",
            "dataDomains": ["PROCUREMENT"],
            "dataLayers": ["FEATURE"],
        }
        base.update(kwargs)
        return AgentDefinitionCreate(**base)

    def test_valid_domains_and_layers_pass(self) -> None:
        d = self._dto(
            dataDomains=["PROCUREMENT", "QUALITY"],
            dataLayers=["DIM", "DWD", "FEATURE"],
        )
        assert d.data_domains == ["PROCUREMENT", "QUALITY"]
        assert d.data_layers == ["DIM", "DWD", "FEATURE"]

    def test_domain_normalized_lowercase(self) -> None:
        d = self._dto(dataDomains=["procurement"])
        assert d.data_domains == ["PROCUREMENT"]

    def test_layer_normalized_lowercase(self) -> None:
        d = self._dto(dataLayers=["feature", "dim"])
        assert d.data_layers == ["FEATURE", "DIM"]

    def test_domain_unknown_raises_with_message(self) -> None:
        with pytest.raises(PydanticValidationError) as exc:
            self._dto(dataDomains=["PROCUREMENT", "NONSENSE"])
        assert MSG_AGENT_DOMAIN_NOT_IN_VOCAB.split("{value}")[0] in str(exc.value)

    def test_layer_unknown_raises_with_message(self) -> None:
        with pytest.raises(PydanticValidationError) as exc:
            self._dto(dataLayers=["FEATURE", "KAFKA"])
        assert MSG_AGENT_LAYER_NOT_IN_VOCAB.split("{value}")[0] in str(exc.value)

    def test_domain_dedup_preserves_order(self) -> None:
        d = self._dto(dataDomains=["PROCUREMENT", "QUALITY", "PROCUREMENT"])
        assert d.data_domains == ["PROCUREMENT", "QUALITY"]

    def test_layer_dedup_preserves_order(self) -> None:
        d = self._dto(dataLayers=["FEATURE", "DIM", "FEATURE"])
        assert d.data_layers == ["FEATURE", "DIM"]

    def test_empty_domain_element_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            self._dto(dataDomains=["PROCUREMENT", "  "])

    def test_empty_lists_allowed(self) -> None:
        d = self._dto(dataDomains=[], dataLayers=[])
        assert d.data_domains == []
        assert d.data_layers == []
