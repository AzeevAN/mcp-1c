"""Дата принятой структуры переживает restore и не угадывается для старых строк."""

import json

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import Registry
from mcp1c.tools import sources_snapshot


def test_legacy_source_date_survives_restore_and_old_row_remains_unknown(tmp_path):
    source_dir = tmp_path / "export"
    source_dir.mkdir()
    archive = write_export(source_dir, build_configuration(name="Demo"))
    registry = Registry(tmp_path / "data")
    accepted = registry.add_configuration(archive)
    assert accepted.last_successful_load_at is None
    registry.save()
    assert accepted.last_successful_load_at is not None

    restored = Registry(registry.data_dir)
    assert restored.restore() == []
    row = next(row for row in sources_snapshot(restored).configurations if row.name == "Demo")
    assert row.last_successful_load_at == accepted.last_successful_load_at

    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    for source in payload["sources"]:
        source.pop("last_successful_load_at", None)
    registry.registry_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    old = Registry(registry.data_dir)
    assert old.restore() == []
    row = next(row for row in sources_snapshot(old).configurations if row.name == "Demo")
    assert row.last_successful_load_at is None


def test_failed_legacy_registry_write_does_not_publish_date(tmp_path, monkeypatch):
    source_dir = tmp_path / "export"
    source_dir.mkdir()
    archive = write_export(source_dir, build_configuration(name="Demo"))
    registry = Registry(tmp_path / "data")

    def fail_write(_payload):
        raise OSError("отказ записи")

    source = registry.add_configuration(archive)
    monkeypatch.setattr(registry, "_write_registry_payload", fail_write)
    with pytest.raises(OSError, match="отказ записи"):
        registry.save()
    assert source.last_successful_load_at is None
    assert registry.sources["Demo"] is source


def test_legacy_repeat_changes_date_only_after_save(tmp_path):
    source_dir = tmp_path / "export"
    source_dir.mkdir()
    archive = write_export(source_dir, build_configuration(name="Demo"))
    registry = Registry(tmp_path / "data")
    first = registry.add_configuration(archive)
    registry.save()
    previous = first.last_successful_load_at
    repeated = registry.add_configuration(archive)
    assert repeated.last_successful_load_at == previous
    registry.save()
    assert repeated.last_successful_load_at > previous
