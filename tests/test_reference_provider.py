"""Встроенная подписанная справка и capability-публикация её MCP-ручек."""

from __future__ import annotations

import asyncio

import pytest

from mcp1c.capabilities import CapabilityContractError
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c.server import build_server

from reference_fixture import SyntheticReferenceSigner, build_reference_database


def _service(tmp_path):
    signer = SyntheticReferenceSigner.generate()
    artifact = signer.build(
        tmp_path / "release" / "reference.mcp1cref",
        build_reference_database(tmp_path / "source.sqlite3"),
    )
    return ReferenceService.discover(
        tmp_path / "data",
        embedded_path=artifact,
        verifier=signer.verifier(),
    )


def _names(server):
    return [tool.name for tool in asyncio.run(server.list_tools())]


def test_reference_выключен_не_публикует_ручки(tmp_path):
    service = _service(tmp_path)
    names = _names(build_server(Registry(tmp_path / "registry"), reference=service))
    assert "search_reference" not in names
    assert "get_reference" not in names


def test_reference_включен_публикует_ровно_две_ручки(tmp_path):
    service = _service(tmp_path)
    names = _names(build_server(
        Registry(tmp_path / "registry"),
        reference=service,
        enabled_capabilities=("reference",),
    ))
    assert names[-2:] == ["search_reference", "get_reference"]
    assert len(names) == 14
    assert service.status.signature == "ed25519"


def test_включенная_reference_требует_проверенный_встроенный_пакет(tmp_path):
    service = ReferenceService.discover(tmp_path / "data")
    with pytest.raises(CapabilityContractError, match="подписанный пакет"):
        build_server(
            Registry(tmp_path / "registry"),
            reference=service,
            enabled_capabilities=("reference",),
        )


def test_provider_читает_подписанный_пакет_и_пересобирает_кэш(tmp_path):
    service = _service(tmp_path)
    provider = service.provider
    assert provider is not None
    assert service.status.state == "ready"
    assert service.status.index_cache == "rebuilt"
    found = provider.search("показать образец")
    assert found["results"][0]["id"] == "bsl/Example"
    card = provider.get("bsl/Example", max_chars=256)
    assert "Синтетическое описание" in card["content"]


def test_кэш_общей_справки_не_зависит_от_registry(tmp_path):
    service = _service(tmp_path)
    cache = tmp_path / "data" / "index" / "reference" / "reference.search"
    assert cache.is_file()
    Registry(tmp_path / "data").startup()
    assert cache.is_file()
    service.close()
