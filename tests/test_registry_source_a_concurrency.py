"""Публикация Source A не возвращает устаревшее состояние после конкурента."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

from conftest import write_export
from mcp1c.intake_v2_converter import convert_collection
from mcp1c.registry import Registry, RegistryError
from test_intake_v2_runtime import _materialized


def _source_a(root, base, version):
    root.mkdir()
    config = deepcopy(base)
    config.version = version
    config.platform = "8.3.27.1000"
    config.source_format = "json"
    config.predefined_available = True
    return write_export(root, config)


@pytest.mark.parametrize(
    "initial, change",
    [
        (False, "native"),
        (True, "native"),
        (False, "native_then_remove"),
        (True, "remove"),
        (True, "legacy"),
    ],
)
def test_source_a_отклоняет_конкурентное_изменение(
    tmp_path, monkeypatch, initial, change,
):
    collection, generation = _materialized(tmp_path, "concurrent", common_forms=True)
    base = convert_collection(collection).base
    registry = Registry(tmp_path / "data")
    if initial:
        registry.add_configuration(_source_a(tmp_path / "initial", base, "0.9"))
        registry.save()
    pending_path = _source_a(tmp_path / "pending", base, "99.0")
    original_bytes = pending_path.read_bytes()
    entered, release = Event(), Event()
    original_index = registry._configuration_index

    def paused_index(config, source, **kwargs):
        if config.version == "99.0":
            entered.set()
            assert release.wait(10), "конкурирующая публикация заблокирована разбором A"
        return original_index(config, source, **kwargs)

    monkeypatch.setattr(registry, "_configuration_index", paused_index)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(registry.add_configuration, pending_path)
        try:
            assert entered.wait(10)
            if initial:
                assert registry.resolve(base.name).configuration.config.version == "0.9"
            if change.startswith("native"):
                registry.publish_generation(
                    registry.stage_generation(generation.manifest, generation.payloads)
                )
            if change in ("remove", "native_then_remove"):
                registry.remove(base.name)
                registry.save()
            if change == "legacy":
                registry.add_configuration(_source_a(tmp_path / "winner", base, "2.0"))
                registry.save()
            pointer = registry.active_generation_pointer(generation.manifest.identity)
            manifest = registry.active_generation(generation.manifest.identity)
            durable = registry.registry_path.read_bytes()
        finally:
            release.set()
        with pytest.raises(RegistryError, match="изменилась.*Повторите загрузку"):
            pending.result(timeout=10)

    assert registry.registry_path.read_bytes() == durable
    assert registry.active_generation_pointer(generation.manifest.identity) == pointer
    assert registry.active_generation(generation.manifest.identity) == manifest
    assert pending_path.read_bytes() == original_bytes
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    for current in (registry, restarted):
        if change in ("remove", "native_then_remove"):
            assert base.name not in current.configurations
        else:
            context = current.resolve(base.name)
            assert context.configuration.config.version == (
                base.version if change == "native" else "2.0"
            )
            if change == "native":
                assert context.modules is not None and context.modules.готов
                assert context.roles is not None and context.roles.ready

    if change == "native":
        # Явный повтор после конфликта идёт уже по native-пути и сохраняет B.
        registry.add_configuration(pending_path)
        retried = Registry(registry.data_dir)
        assert retried.restore() == []
        context = retried.resolve(base.name)
        assert context.configuration.config.version == "99.0"
        assert context.modules is not None and context.modules.готов
        assert context.roles is not None and context.roles.ready
        current = registry.active_generation(generation.manifest.identity)
        assert current.layers[1:] == manifest.layers[1:]


def test_source_a_не_конфликтует_с_другой_конфигурацией(tmp_path, monkeypatch):
    collection, _generation = _materialized(tmp_path, "independent")
    base = convert_collection(collection).base
    registry = Registry(tmp_path / "data")
    pending_path = _source_a(tmp_path / "pending", base, "99.0")
    other = deepcopy(base)
    other.name = "OtherConfiguration"
    other_path = _source_a(tmp_path / "other", other, "2.0")
    original_index = registry._configuration_index

    def publish_other(config, source):
        if config.name == base.name:
            registry.add_configuration(other_path)
        return original_index(config, source)

    monkeypatch.setattr(registry, "_configuration_index", publish_other)
    registry.add_configuration(pending_path)
    registry.save()
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.resolve(base.name).configuration.config.version == "99.0"
    assert restarted.resolve(other.name).configuration.config.version == "2.0"
