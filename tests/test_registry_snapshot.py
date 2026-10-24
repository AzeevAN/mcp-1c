"""Публичный снимок реестра и его конкурентные потребители."""

from dataclasses import FrozenInstanceError

import pytest

from mcp1c.registry import KIND_CONFIGURATION, STATUS_LOADING, STATUS_READY, Registry, Source


def test_snapshot_не_меняется_вместе_с_живым_реестром(tmp_path):
    registry = Registry(tmp_path / "data")
    source = Source(
        id="ТестоваяКонфигурация",
        kind=KIND_CONFIGURATION,
        status=STATUS_LOADING,
        warnings=["исходное предупреждение"],
    )
    registry.sources[source.id] = source

    snapshot = registry.snapshot()
    assert registry.snapshot_is_current(snapshot)
    source.status = STATUS_READY
    source.warnings.append("новое предупреждение")
    registry.sources["ДругаяКонфигурация"] = Source(
        id="ДругаяКонфигурация",
        kind=KIND_CONFIGURATION,
    )

    row = snapshot.sources[source.id]
    assert row.status == STATUS_LOADING
    assert row.warnings == ("исходное предупреждение",)
    assert tuple(snapshot.sources) == (source.id,)
    with pytest.raises(TypeError):
        snapshot.sources["ещё"] = row
    with pytest.raises(FrozenInstanceError):
        row.status = STATUS_READY
    assert not registry.snapshot_is_current(snapshot)


def test_отпечаток_snapshot_совпадает_с_cas_после_разных_операций(tmp_path):
    """CAS сравнивает отпечаток снимка с ``_snapshot_fingerprint_locked``.

    Две независимые реализации одного набора полей расходились бы молча при
    добавлении поля в одну из них: вечные отказы CAS или пропуск устаревшего.
    """
    from test_intake_v2_registry import _manifest

    from conftest import build_configuration, write_export, write_syntax

    registry = Registry(tmp_path / "data")
    exports = tmp_path / "exports"
    exports.mkdir()

    def сверить() -> None:
        snapshot = registry.snapshot()
        with registry._lock:
            assert snapshot._fingerprint == registry._snapshot_fingerprint_locked()
        assert registry.snapshot_is_current(snapshot)

    сверить()
    registry.add_configuration(write_export(exports, build_configuration()))
    сверить()
    registry.add_syntax(write_syntax(exports))
    сверить()
    manifest, payloads = _manifest(tmp_path, "generation-001")
    registry.publish_generation(registry.stage_generation(manifest, payloads))
    сверить()
    registry.remove(build_configuration().name)
    сверить()


def test_snapshot_берёт_отпечаток_у_единственной_реализации(tmp_path, monkeypatch):
    registry = Registry(tmp_path / "data")
    маркер = ("единственная реализация",)
    monkeypatch.setattr(
        registry, "_snapshot_fingerprint_locked", lambda: маркер
    )

    assert registry.snapshot()._fingerprint is маркер


@pytest.mark.parametrize("change", ["package", "source_identity", "source_fields"])
def test_snapshot_кода_сохраняет_source_и_отвергает_смену_пакета(
    реестр_с_кодом, change
):
    from dataclasses import replace

    registry = реестр_с_кодом
    loaded = registry.resolve("Пример").modules
    snapshot = registry.snapshot()
    captured = snapshot.modules[loaded.source.id]
    warnings = captured.source.warnings
    assert registry.snapshot_is_current(snapshot)

    with registry._lock:
        if change == "package":
            registry.modules[loaded.source.id] = replace(loaded)
        elif change == "source_identity":
            loaded.source = replace(loaded.source)
        else:
            loaded.source.warnings.append("предупреждение текущего источника")

    assert captured.source.warnings == warnings
    assert captured.source.status == "ready"
    assert captured.source.error == ""
    assert not registry.snapshot_is_current(snapshot)
