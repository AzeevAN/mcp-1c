"""Жизненный цикл реестра: reload, recovery, bootstrap и CLI-путь.

Все данные синтетические и живут в ``tmp_path``; рабочий ``data/`` не нужен.
"""

from __future__ import annotations

import json

from conftest import build_configuration, write_export
from mcp1c.intake_v2_registry import ExportIdentity
from mcp1c.registry import Registry
from mcp1c.source_modes import ActivationMode
from test_intake_v2_extensions import _stage_active_base
from test_intake_v2_legacy_cleanup import _native_generation


def _mode(registry: Registry, name: str = "DemoConfiguration"):
    pointer = registry.active_generation_pointer(
        ExportIdentity.configuration(name)
    )
    assert pointer is not None and pointer.activation is not None
    return pointer.activation.mode


def test_bootstrap_той_же_a_не_откатывает_native_b_full(tmp_path):
    data = tmp_path / "data"
    (data / "bootstrap").mkdir(parents=True)
    write_export(data / "bootstrap", build_configuration(name="DemoConfiguration"))
    registry = Registry(data)
    registry.startup()
    assert "DemoConfiguration" in registry.sources

    registry.publish_generation(
        _stage_active_base(registry, _native_generation(tmp_path, "full"))
    )
    assert _mode(registry) is ActivationMode.B_FULL

    restarted = Registry(data)
    messages = restarted.startup()
    assert _mode(restarted) is ActivationMode.B_FULL, messages
    assert restarted.resolve("DemoConfiguration").modules is not None
    again = Registry(data)
    again.startup()
    assert _mode(again) is ActivationMode.B_FULL
