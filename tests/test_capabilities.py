"""Внутренний startup-контракт необязательных capability-модулей."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.capabilities import (
    CapabilityRuntime,
    CapabilitySettingsStore,
    MAX_SERVER_SETTINGS_BYTES,
    CapabilityConfigurationError,
    CapabilityContractError,
    CapabilityDefinition,
    CapabilityModule,
    CapabilityTool,
    load_capability_modules,
    parse_capability_config,
    resolve_capability_settings,
)
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c import server as server_module
from mcp1c.server import build_server


CORE_TOOLS = [
    "list_configurations",
    "list_extensions",
    "search_objects",
    "search_procedures",
    "get_procedure",
    "get_callers",
    "get_object",
    "get_related",
    "compare_configurations",
    "search_syntax",
    "get_syntax",
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _server(tmp_path, *, enabled_capabilities=()):
    reference = ReferenceService.discover(tmp_path, database_path="off")
    return build_server(
        Registry(tmp_path),
        reference=reference,
        enabled_capabilities=enabled_capabilities,
    )


def _names(server) -> list[str]:
    return [tool.name for tool in asyncio.run(server.list_tools())]


def test_default_не_меняет_публичный_каталог_и_не_импортирует_демо(tmp_path):
    sys.modules.pop("mcp1c.capability_modules.diagnostics", None)

    server = _server(tmp_path)

    assert _names(server) == CORE_TOOLS
    assert "mcp1c.capability_modules.diagnostics" not in sys.modules


def test_off_не_импортирует_и_не_инициализирует_синтетический_модуль(
    tmp_path,
    monkeypatch,
):
    import_marker = tmp_path / "imported"
    init_marker = tmp_path / "initialized"
    module_name = "synthetic_capability_canary"
    (tmp_path / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('imported')\n"
        "def load():\n"
        f"    Path({str(init_marker)!r}).write_text('initialized')\n"
        "    return ()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    definitions = {
        "canary": CapabilityDefinition("canary", f"{module_name}:load")
    }

    names = parse_capability_config("off", definitions=definitions)
    modules = load_capability_modules(names, definitions=definitions)

    assert modules == ()
    assert module_name not in sys.modules
    assert not import_marker.exists()
    assert not init_marker.exists()


def test_enabled_импортирует_и_инициализирует_только_выбранный_canary(
    tmp_path,
    monkeypatch,
):
    import_marker = tmp_path / "imported"
    init_marker = tmp_path / "initialized"
    module_name = "synthetic_enabled_capability_canary"
    (tmp_path / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('imported')\n"
        "def load():\n"
        f"    Path({str(init_marker)!r}).write_text('initialized')\n"
        "    return ()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    definitions = {
        "canary": CapabilityDefinition("canary", f"{module_name}:load")
    }

    names = parse_capability_config("canary", definitions=definitions)
    modules = load_capability_modules(names, definitions=definitions)

    assert modules == (CapabilityModule("canary", ()),)
    assert import_marker.read_text(encoding="utf-8") == "imported"
    assert init_marker.read_text(encoding="utf-8") == "initialized"


@pytest.mark.parametrize(
    "value",
    ("", "diagnostics,", ",diagnostics", " diagnostics", "diagnostics ",
     "diagnostics,diagnostics", "off,diagnostics", "DIAGNOSTICS"),
)
def test_некорректная_конфигурация_отклоняется(value):
    with pytest.raises(CapabilityConfigurationError):
        parse_capability_config(value)


def test_неизвестный_модуль_отклоняется_до_import():
    with pytest.raises(CapabilityConfigurationError, match="unknown"):
        parse_capability_config("unknown")


def test_main_отклоняет_неизвестный_модуль_до_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP1C_CAPABILITIES", "unknown")
    monkeypatch.setattr(
        server_module,
        "Registry",
        lambda data: pytest.fail("Registry не должен создаваться"),
    )

    with pytest.raises(SystemExit) as error:
        server_module.main(["--data", str(tmp_path), "--transport", "stdio"])

    assert error.value.code == 2


def test_server_settings_важнее_env_и_переживают_restart(tmp_path):
    store = CapabilitySettingsStore(tmp_path / "data")
    store.save(("diagnostics",))

    resolved_store, enabled = resolve_capability_settings(
        tmp_path / "data",
        environment="unknown",
    )

    assert enabled == ("diagnostics",)
    assert resolved_store.load() == ("diagnostics",)


def test_enable_и_disable_применяются_двумя_независимыми_startup(
    tmp_path,
):
    data = tmp_path / "data"
    store = CapabilitySettingsStore(data)
    probe = """
import asyncio
import json
import sys
from pathlib import Path

from mcp1c.capabilities import CapabilityRuntime, resolve_capability_settings
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c.server import build_server

data = Path(sys.argv[1])
store, enabled = resolve_capability_settings(data, environment="off")
registry = Registry(data)
registry.startup()
server = build_server(
    registry,
    reference=ReferenceService.discover(data, database_path="off"),
    enabled_capabilities=enabled,
    capability_runtime=CapabilityRuntime(store, active=enabled),
)
print(json.dumps([tool.name for tool in asyncio.run(server.list_tools())]))
"""

    def startup_tools() -> list[str]:
        result = subprocess.run(
            [sys.executable, "-c", probe, str(data)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": "src", "MCP1C_CAPABILITIES": "off"},
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    store.save(("diagnostics",))
    enabled_tools = startup_tools()
    store.save(())
    disabled_tools = startup_tools()

    assert enabled_tools == [*CORE_TOOLS, "diagnostics_status"]
    assert disabled_tools == CORE_TOOLS


def test_после_удаления_settings_status_снова_предсказывает_env_bootstrap(tmp_path):
    initial = CapabilitySettingsStore(tmp_path / "data")
    initial.save(("diagnostics",))
    store, active = resolve_capability_settings(
        tmp_path / "data",
        environment="diagnostics",
    )
    runtime = CapabilityRuntime(store, active=active)

    store.path.unlink()

    assert runtime.payload()["desired"] == ["diagnostics"]
    assert runtime.pending_restart() is False


def test_env_служит_fallback_только_пока_server_settings_не_созданы(tmp_path):
    store, enabled = resolve_capability_settings(
        tmp_path / "data",
        environment="diagnostics",
    )
    runtime = CapabilityRuntime(store, active=enabled)

    assert enabled == ("diagnostics",)
    assert store.path.exists() is False
    assert runtime.payload()["pending_restart"] is False

    store.save(())

    assert runtime.payload() == {
        "available": ["diagnostics", "forms"],
        "active": ["diagnostics"],
        "desired": [],
        "pending_restart": True,
    }


def test_повреждённые_server_settings_отклоняются_до_registry(
    tmp_path,
    monkeypatch,
):
    data = tmp_path / "data"
    data.mkdir()
    (data / "server-settings.json").write_text(
        '{"version": 1, "capabilities": {"enabled": ["unknown"]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        server_module,
        "Registry",
        lambda path: pytest.fail("Registry не должен создаваться"),
    )

    with pytest.raises(SystemExit) as error:
        server_module.main(["--data", str(data), "--transport", "stdio"])

    assert error.value.code == 2


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"version": 2, "capabilities": {"enabled": []}},
        {"version": True, "capabilities": {"enabled": []}},
        {"version": 1.0, "capabilities": {"enabled": []}},
        {"version": 1},
        {"version": 1, "capabilities": []},
        {"version": 1, "capabilities": {"enabled": "diagnostics"}},
        {"version": 1, "capabilities": {"enabled": ["diagnostics", "diagnostics"]}},
        {"version": 1, "capabilities": {"enabled": [], "extra": True}},
    ),
)
def test_server_settings_fail_closed_на_неверной_schema(tmp_path, payload):
    store = CapabilitySettingsStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CapabilityConfigurationError):
        store.load()


@pytest.mark.parametrize(
    "raw",
    (
        '{"version":2,"version":1,"capabilities":{"enabled":[]}}',
        '{"version":1,"capabilities":{"enabled":[],"enabled":["diagnostics"]}}',
        '{"version":1,"capabilities":{"enabled":[]},"future":NaN}',
    ),
)
def test_server_settings_отклоняет_неоднозначный_json(tmp_path, raw):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(raw, encoding="utf-8")

    with pytest.raises(CapabilityConfigurationError):
        store.load()


def test_server_settings_нормализует_слишком_глубокий_json(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        '{"version":1,"capabilities":{"enabled":[]},"future":'
        + "[" * 10000
        + "0"
        + "]" * 10000
        + "}",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityConfigurationError, match="прочитать"):
        store.load()


def test_server_settings_отклоняет_lone_surrogate_до_save(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        '{"version":1,"capabilities":{"enabled":[]},"future":"\\ud800"}',
        encoding="utf-8",
    )

    with pytest.raises(CapabilityConfigurationError, match="прочитать"):
        store.load()


def test_server_settings_имеют_лимит_размера(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_bytes(b" " * (MAX_SERVER_SETTINGS_BYTES + 1))

    with pytest.raises(CapabilityConfigurationError, match="размер"):
        store.load()


def test_server_settings_не_следует_по_symlink(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(
        '{"version":1,"capabilities":{"enabled":[]}}',
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    store = CapabilitySettingsStore(data)
    store.path.symlink_to(outside)

    with pytest.raises(CapabilityConfigurationError, match="открыть"):
        store.load()


def test_server_settings_не_зависает_на_fifo(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO недоступен на этой платформе")
    store = CapabilitySettingsStore(tmp_path)
    os.mkfifo(store.path)

    with pytest.raises(CapabilityConfigurationError, match="обычным файлом"):
        store.load()


def test_bounded_read_держит_лимит_даже_если_fstat_устарел(
    tmp_path,
    monkeypatch,
):
    import mcp1c.capabilities as capability_module

    store = CapabilitySettingsStore(tmp_path)
    store.path.write_bytes(b" " * (MAX_SERVER_SETTINGS_BYTES + 1))
    class StaleMetadata:
        st_mode = stat.S_IFREG | 0o600
        st_size = 1

    monkeypatch.setattr(capability_module.os, "fstat", lambda descriptor: StaleMetadata())

    with pytest.raises(CapabilityConfigurationError, match="размер"):
        store.load()


def test_save_атомарно_заменяет_секцию_и_сохраняет_будущие_секции(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "capabilities": {"enabled": []},
                "future": {"kept": True},
            }
        ),
        encoding="utf-8",
    )

    store.save(("diagnostics",))

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["future"] == {"kept": True}
    assert payload["capabilities"] == {"enabled": ["diagnostics"]}
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_ошибка_atomic_replace_не_портит_предыдущие_settings(
    tmp_path,
    monkeypatch,
):
    import mcp1c.capabilities as capability_module

    store = CapabilitySettingsStore(tmp_path)
    store.save(())
    original = store.path.read_bytes()
    monkeypatch.setattr(
        capability_module.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("synthetic")),
    )

    with pytest.raises(CapabilityConfigurationError, match="сохранить"):
        store.save(("diagnostics",))

    assert store.path.read_bytes() == original
    assert list(tmp_path.glob(".server-settings.json.tmp-*")) == []


def test_ошибка_directory_fsync_после_replace_считается_применённой(
    tmp_path,
    monkeypatch,
):
    import mcp1c.capabilities as capability_module

    store = CapabilitySettingsStore(tmp_path)
    store.save(())
    original_fsync = capability_module.os.fsync

    def fsync_with_directory_failure(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("synthetic directory fsync")
        return original_fsync(descriptor)

    monkeypatch.setattr(capability_module.os, "fsync", fsync_with_directory_failure)

    assert store.save(("diagnostics",)) == ("diagnostics",)
    assert store.load() == ("diagnostics",)
    assert list(tmp_path.glob(".server-settings.json.tmp-*")) == []


@pytest.mark.parametrize("transport", ("stdio", "streamable-http"))
def test_main_передаёт_capability_в_оба_транспорта(
    transport,
    tmp_path,
    monkeypatch,
):
    captured = {}

    class FakeRegistry:
        configurations = [object()]

        def __init__(self, data):
            self.data = data

        def startup(self):
            return []

        def snapshot(self):
            return self

    class FakeServer:
        def run(self, **kwargs):
            captured["run"] = kwargs

    def fake_build(registry, **kwargs):
        captured["enabled"] = kwargs["enabled_capabilities"]
        return FakeServer()

    monkeypatch.setenv("MCP1C_CAPABILITIES", "diagnostics")
    monkeypatch.setattr(server_module, "Registry", FakeRegistry)
    monkeypatch.setattr(server_module, "build_server", fake_build)
    monkeypatch.setattr(
        server_module,
        "_run_streamable_http",
        lambda server, **kwargs: captured.update(http=kwargs),
    )

    assert server_module.main(
        ["--data", str(tmp_path), "--transport", transport]
    ) == 0
    assert captured["enabled"] == ("diagnostics",)
    assert ("run" in captured) is (transport == "stdio")
    assert ("http" in captured) is (transport == "streamable-http")


def test_enabled_добавляет_ровно_один_диагностический_инструмент(tmp_path):
    server = _server(tmp_path, enabled_capabilities=("diagnostics",))

    tools = asyncio.run(server.list_tools())
    names = [tool.name for tool in tools]

    assert names == [*CORE_TOOLS, "diagnostics_status"]
    diagnostic = tools[-1]
    assert diagnostic.input_schema["properties"] == {}
    assert "не читает Registry" in (diagnostic.description or "")
    assert "не изменяет данные" in (diagnostic.description or "")


@pytest.mark.anyio
async def test_диагностический_инструмент_работает_через_полную_mcp_сессию(
    tmp_path,
):
    server = _server(tmp_path, enabled_capabilities=("diagnostics",))

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    result = await session.call_tool("diagnostics_status", {})
            finally:
                tasks.cancel_scope.cancel()

    assert [tool.name for tool in listed.tools] == [*CORE_TOOLS, "diagnostics_status"]
    assert result.is_error is False
    assert json.loads(result.content[0].text) == {
        "capability": "diagnostics",
        "state": "ready",
        "data_access": False,
        "write_access": False,
    }


def _noop() -> str:
    return "ok"


@pytest.mark.parametrize(
    "name",
    ("list_configurations", "search_reference", "find_roles_for_access"),
)
def test_имя_модуля_не_может_конфликтовать_с_ядром(tmp_path, name):
    server = _server(tmp_path)
    module = CapabilityModule(
        "synthetic",
        (CapabilityTool(name, _noop, "Синтетика."),),
    )

    with pytest.raises(CapabilityContractError, match=name):
        server.add_capability_modules((module,))

    assert _names(server) == CORE_TOOLS


def test_два_модуля_не_могут_тихо_объявить_одно_имя(tmp_path):
    server = _server(tmp_path)
    modules = (
        CapabilityModule(
            "first",
            (CapabilityTool("shared_status", _noop, "Первый."),),
        ),
        CapabilityModule(
            "second",
            (CapabilityTool("shared_status", _noop, "Второй."),),
        ),
    )

    with pytest.raises(CapabilityContractError, match="shared_status"):
        server.add_capability_modules(modules)

    assert _names(server) == CORE_TOOLS


def test_malformed_tool_даёт_контрактный_отказ_без_сырого_traceback(tmp_path):
    server = _server(tmp_path)
    malformed = (
        CapabilityModule(None, (CapabilityTool("valid", _noop, "Описание."),)),
        CapabilityModule(
            "synthetic",
            (CapabilityTool(None, _noop, "Описание."),),
        ),
        CapabilityModule(
            "synthetic",
            (CapabilityTool("valid", _noop, None),),
        ),
    )

    for module in malformed:
        with pytest.raises(CapabilityContractError):
            server.add_capability_modules((module,))

    assert _names(server) == CORE_TOOLS


def test_ошибка_добавления_откатывает_весь_набор(tmp_path, monkeypatch):
    server = _server(tmp_path)
    modules = (
        CapabilityModule(
            "synthetic",
            (
                CapabilityTool("synthetic_first", _noop, "Первый."),
                CapabilityTool("synthetic_second", _noop, "Второй."),
            ),
        ),
    )
    original = server._tool_manager.add_tool

    def fail_second(function, **kwargs):
        if kwargs.get("name") == "synthetic_second":
            raise RuntimeError("synthetic failure")
        return original(function, **kwargs)

    monkeypatch.setattr(server._tool_manager, "add_tool", fail_second)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        server.add_capability_modules(modules)

    assert _names(server) == CORE_TOOLS


def test_public_startup_документирует_settings_и_bootstrap_env():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    tools_doc = (root / "docs" / "tools.md").read_text(encoding="utf-8")
    operations = (root / "docs" / "operations.md").read_text(encoding="utf-8")

    assert "MCP1C_CAPABILITIES: ${MCP1C_CAPABILITIES-off}" in compose
    assert "MCP1C_CAPABILITIES=off" in example
    assert "`MCP1C_CAPABILITIES`" in readme
    assert "`data/server-settings.json`" in readme
    assert "`PUT /api/v1/capabilities`" in readme
    assert "«Дополнительные модули»" in readme
    assert "tests/measure_capability_startup.py --runs 10" in readme
    assert "`diagnostics_status`" in tools_doc
    assert "`PUT` того же admin-" in tools_doc
    assert "`data/server-settings.json`" in operations
    assert "`PUT /api/v1/capabilities`" in operations
    assert '"capabilities":{"enabled":["diagnostics"]}' in operations
