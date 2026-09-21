import pytest
import json
import anyio
from dataclasses import replace
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.capabilities import CapabilityRuntime, CapabilitySettingsStore
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.intake_v2 import ExportIdentity
from mcp1c.intake_v2_registry import GenerationPointer
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry, RegistryError
from mcp1c.server import build_server
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem
from mcp1c.tools import list_configurations, search_syntax
from conftest import build_configuration, write_export

from mcp1c.source_modes import (
    ActivationComponent,
    ActivationManifest,
    ActivationMode,
    PlatformDeclaration,
)


def _registry(tmp_path, mode=ActivationMode.B_FULL):
    registry = Registry(tmp_path / "data")
    identity = ExportIdentity.configuration("Demo")
    activation = ActivationManifest(
        mode=mode,
        identity_incarnation="inc-1",
        physical_generation_root_id="root-1",
        configuration_version="1",
        main=ActivationComponent("source-b" if mode is ActivationMode.B_FULL else "source-a", "candidate.zip", "a" * 64, "b" * 64),
        extensions=(),
        expected_previous_activation=None,
        transaction_id="tx-1",
        recovery_id="recovery-1",
    )
    pointer = GenerationPointer.for_manifest(
        type("Manifest", (), {"identity": identity, "generation_id": "g1", "sha256": "c" * 64})(),
        activation=activation,
    )
    registry._generation_pointers[identity.grouping_key] = pointer
    return registry


@pytest.mark.parametrize("version", ["8.3.24", "8.3.24.1234"])
def test_platform_declaration_accepts_three_or_four_components(version):
    assert PlatformDeclaration(version).to_dict() == {
        "version": version,
        "source": "user",
        "status": "declared",
    }


@pytest.mark.parametrize("version", ["8.3", "8.3.24.1.5", "latest", "8.3.x"])
def test_platform_declaration_rejects_non_numeric_shape(version):
    with pytest.raises(ValueError, match="3 или 4"):
        PlatformDeclaration(version)


def test_platform_declaration_rejects_other_provenance():
    with pytest.raises(ValueError, match="source=user"):
        PlatformDeclaration("8.3.24", source="source-a")


def test_registry_persists_and_clears_b_full_platform_declaration(tmp_path):
    registry = _registry(tmp_path)
    saved = registry.set_platform_version("Demo", "8.3.24.1234")
    assert saved.status == "declared"
    assert registry.platform_declaration("Demo").version == "8.3.24.1234"
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    payload.pop("generation_manifests", None)
    registry.registry_path.write_text(json.dumps(payload), encoding="utf-8")
    restored = Registry(registry.data_dir)
    assert restored.restore() == []
    assert restored.platform_declaration("Demo").version == "8.3.24.1234"
    identity = ExportIdentity.configuration("Demo")
    restored._generation_pointers[identity.grouping_key] = registry._generation_pointers[identity.grouping_key]
    restored.clear_platform_version("Demo")
    assert restored.platform_declaration("Demo") is None


def test_registry_declares_platform_for_a_only(tmp_path):
    registry = _registry(tmp_path, ActivationMode.A_ONLY)
    assert registry.set_platform_version("Demo", "8.3.24").version == "8.3.24"


def test_manual_platform_is_used_by_notes_and_syntax_tools(tmp_path):
    registry = Registry(tmp_path / "data")
    source = tmp_path / "source"
    source.mkdir()
    config = replace(build_configuration(name="Demo"), platform="")
    registry.add_configuration(write_export(source, config), keep_source=False)
    syntax = SyntaxIndex(platforms=["8.3.27.2130"], source="test")
    syntax.add(SyntaxItem(
        id="global/СтрРазделить",
        kind="method",
        name_ru="СтрРазделить",
        parent_ru="Глобальный контекст",
        since="8.3.6",
        description="Разделяет строку.",
    ))
    registry.add_syntax(save_syntax(syntax, tmp_path / "syntax.json.gz"))

    registry.set_platform_version("Demo", "8.3.5.1570")

    context = registry.resolve("Demo")
    assert context.platform == "8.3.5.1570"
    assert context.syntax_relation == "newer"
    assert "Фактическая версия платформы неизвестна" not in list_configurations(registry)
    answer = search_syntax(registry, "СтрРазделить", config="Demo")
    assert "Фактическая версия платформы неизвестна" not in answer
    assert "8.3.5.1570" in answer


@pytest.mark.anyio
async def test_manual_platform_is_visible_through_mcp_tools_call(tmp_path):
    registry = Registry(tmp_path / "data")
    source = tmp_path / "source"
    source.mkdir()
    config = replace(build_configuration(name="Demo"), platform="")
    registry.add_configuration(write_export(source, config), keep_source=False)
    syntax = SyntaxIndex(platforms=["8.3.27.2130"], source="test")
    syntax.add(SyntaxItem(
        id="global/СтрРазделить",
        kind="method",
        name_ru="СтрРазделить",
        parent_ru="Глобальный контекст",
        since="8.3.6",
        description="Разделяет строку.",
    ))
    registry.add_syntax(save_syntax(syntax, tmp_path / "syntax.json.gz"))
    registry.set_platform_version("Demo", "8.3.5.1570")
    server = build_server(registry)

    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    catalog = await session.list_tools()
                    assert any(tool.name == "search_syntax" for tool in catalog.tools)
                    result = await session.call_tool(
                        "search_syntax",
                        {"query": "СтрРазделить", "config": "Demo"},
                    )
            finally:
                tasks.cancel_scope.cancel()

    text = result.content[0].text
    assert result.is_error is not True
    assert "8.3.5.1570" in text
    assert "Фактическая версия платформы неизвестна" not in text


def test_configuration_delete_clears_platform_declaration_durably(tmp_path):
    registry = _registry(tmp_path)
    registry.set_platform_version("Demo", "8.3.24")

    registry.remove("Demo")

    assert registry.platform_declaration("Demo") is None
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert "platform_declarations" not in payload
    restored = Registry(registry.data_dir)
    assert restored.restore() == []
    assert restored.platform_declaration("Demo") is None


def test_dashboard_platform_set_clear_require_admin_and_csrf(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = _registry(tmp_path)
    capabilities = CapabilityRuntime(
        CapabilitySettingsStore(registry.data_dir),
        active=(),
    )
    client = живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_ON,
                reference=ReferenceService.discover(registry.data_dir),
                capabilities=capabilities,
            )
        )
    )
    assert client.post(
        "/login", data={"token": "admin-token"}, follow_redirects=False
    ).status_code == 303

    csrf_denied = client.post(
        "/api/v1/sources/platform/set",
        json={"configuration": "Demo", "platform_version": "8.3.24"},
        headers={"origin": "https://evil.invalid"},
    )
    assert csrf_denied.status_code == 403

    set_response = client.post(
        "/api/v1/sources/platform/set",
        json={"configuration": "Demo", "platform_version": "8.3.24"},
    )
    assert set_response.status_code == 200
    assert set_response.json()["status"] == "declared"

    clear_response = client.post(
        "/api/v1/sources/platform/clear",
        json={"configuration": "Demo"},
    )
    assert clear_response.status_code == 200
    assert clear_response.json()["status"] == "unknown"
