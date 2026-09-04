"""Fail-closed граница манифеста и чанков schema v1."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from mcp1c.cli import main
from mcp1c.loader import ExportError, inspect, load
from mcp1c.registry import Registry, RegistryError
from mcp1c.tools import search_objects
from mcp1c.v8container import V8Container, is_container


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _object(name: str = "Один") -> dict:
    return {
        "full_name": f"Справочник.{name}",
        "type": "Справочник",
        "name": name,
        "synonym": name,
    }


def _json_export(
    tmp_path,
    *,
    manifest_update=None,
    chunks: list[tuple[str, dict]] | None = None,
    manifest_files: list[dict] | None = None,
):
    if chunks is None:
        chunks = [
            (
                "objects/catalog.001.json",
                {
                    "schema_version": "1",
                    "type": "Справочник",
                    "chunk": 1,
                    "count": 1,
                    "objects": [_object()],
                },
            )
        ]
    files = (
        manifest_files
        if manifest_files is not None
        else [
            {
                "path": path,
                "type": chunk.get("type", "Справочник"),
                "count": chunk.get("count", len(chunk.get("objects", []))),
            }
            for path, chunk in chunks
        ]
    )
    manifest = {
        "schema_version": "1",
        "format": "json",
        "name": "ТестоваяКонфигурация",
        "objects_total": sum(entry["count"] for entry in files),
        "truncated": False,
        "files": files,
    }
    if manifest_update is not None:
        manifest_update(manifest)

    target = tmp_path / "schema.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for path, chunk in chunks:
            archive.writestr(path, json.dumps(chunk, ensure_ascii=False))
    return target


def test_schema_version_is_mandatory(tmp_path):
    target = _json_export(tmp_path, manifest_update=lambda data: data.pop("schema_version"))

    with pytest.raises(ExportError, match="обязательное.*schema_version"):
        load(target)


def test_empty_configuration_is_a_valid_complete_export(tmp_path):
    target = _json_export(tmp_path, chunks=[], manifest_files=[])

    config = load(target)

    assert config.is_complete
    assert len(config) == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", "1.1", "не совпадает с манифестом"),
        ("type", "Документ", "не совпадает с files.*type"),
        ("chunk", 2, "имя файла задаёт 1"),
        ("count", 2, "не совпадает с files.*count"),
    ],
)
def test_json_chunk_envelope_must_match_manifest(tmp_path, field, value, message):
    chunk = {
        "schema_version": "1",
        "type": "Справочник",
        "chunk": 1,
        "count": 1,
        "objects": [_object()],
    }
    chunk[field] = value
    target = _json_export(
        tmp_path,
        chunks=[("objects/catalog.001.json", chunk)],
        manifest_files=[
            {"path": "objects/catalog.001.json", "type": "Справочник", "count": 1}
        ],
    )

    with pytest.raises(ExportError, match=message):
        load(target)


def test_actual_chunk_count_is_checked(tmp_path):
    chunk = {
        "schema_version": "1",
        "type": "Справочник",
        "chunk": 1,
        "count": 2,
        "objects": [_object()],
    }
    target = _json_export(tmp_path, chunks=[("objects/catalog.001.json", chunk)])

    with pytest.raises(ExportError, match="фактически объектов 1"):
        load(target)


def test_objects_total_must_equal_file_counts(tmp_path):
    target = _json_export(
        tmp_path, manifest_update=lambda data: data.update(objects_total=2)
    )

    with pytest.raises(ExportError, match=r"objects_total=2.*files\[\]\.count=1"):
        load(target)


def test_duplicate_full_name_is_rejected(tmp_path):
    chunks = [
        (
            f"objects/catalog.{number:03d}.json",
            {
                "schema_version": "1",
                "type": "Справочник",
                "chunk": number,
                "count": 1,
                "objects": [_object()],
            },
        )
        for number in (1, 2)
    ]
    target = _json_export(tmp_path, chunks=chunks)

    with pytest.raises(ExportError, match="Справочник.Один.*повторяется"):
        load(target)


def test_chunk_path_is_restricted_to_documented_shape(tmp_path):
    target = _json_export(
        tmp_path,
        chunks=[
            (
                "other/catalog.001.json",
                {
                    "schema_version": "1",
                    "type": "Справочник",
                    "chunk": 1,
                    "count": 1,
                    "objects": [_object()],
                },
            )
        ],
    )

    with pytest.raises(ExportError, match="path должен иметь вид"):
        load(target)


def test_chunk_number_in_path_has_three_digits(tmp_path):
    target = _json_export(
        tmp_path,
        chunks=[
            (
                "objects/catalog.1.json",
                {
                    "schema_version": "1",
                    "type": "Справочник",
                    "chunk": 1,
                    "count": 1,
                    "objects": [_object()],
                },
            )
        ],
    )

    with pytest.raises(ExportError, match="path должен иметь вид"):
        load(target)


def test_truncated_export_requires_explicit_opt_in(tmp_path):
    target = _json_export(
        tmp_path, manifest_update=lambda data: data.update(truncated=True)
    )

    assert inspect(target).truncated
    with pytest.raises(ExportError, match="truncated=true.*явный режим"):
        load(target)

    config = load(target, allow_truncated=True)
    assert config.truncated
    assert not config.is_complete


def test_registry_opt_in_marks_source_answers_and_restore(tmp_path):
    target = _json_export(
        tmp_path, manifest_update=lambda data: data.update(truncated=True)
    )
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError, match="truncated=true"):
        registry.add_configuration(target)
    assert registry.configurations == {}
    assert registry.sources == {}

    source = registry.add_configuration(target, allow_truncated=True)
    assert source.incomplete
    assert any("неполной выгрузки" in warning for warning in source.warnings)
    assert "Выгрузка конфигурации неполная" in search_objects(
        registry, "Один", source.id
    )
    registry.save()

    restored = Registry(tmp_path / "data")
    assert restored.restore() == []
    assert restored.sources[source.id].incomplete
    assert restored.configurations[source.id].config.truncated


def test_cli_requires_and_preserves_explicit_truncated_opt_in(tmp_path, capsys):
    target = _json_export(
        tmp_path, manifest_update=lambda data: data.update(truncated=True)
    )
    data_dir = tmp_path / "data"

    assert main(["reg-add", str(target), "--data", str(data_dir)]) == 2
    assert "truncated=true" in capsys.readouterr().err

    assert main(
        [
            "reg-add",
            str(target),
            "--data",
            str(data_dir),
            "--allow-truncated",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "неполной выгрузки" in output

    assert main(["stats", str(target)]) == 2
    assert "truncated=true" in capsys.readouterr().err
    assert main(["stats", str(target), "--allow-truncated"]) == 0
    assert "неполная" in capsys.readouterr().out.lower()


def test_xml_chunk_has_the_same_envelope_validation(tmp_path):
    manifest = (
        '<manifest schema_version="1" format="xml" name="Пример" '
        'objects_total="1" truncated="false">'
        '<files><item path="objects/catalog.001.xml" type="Справочник" '
        'count="1"/></files></manifest>'
    )
    chunk = (
        '<objects schema_version="1" type="Справочник" chunk="1">'
        '<object full_name="Справочник.Один" type="Справочник" name="Один"/>'
        '</objects>'
    )
    target = tmp_path / "schema-xml.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.xml", manifest)
        archive.writestr("objects/catalog.001.xml", chunk)

    with pytest.raises(ExportError, match="count.*неотрицательным целым"):
        load(target)


def test_empty_xml_configuration_has_explicit_empty_files(tmp_path):
    target = tmp_path / "schema-empty-xml.zip"
    manifest = (
        '<manifest schema_version="1" format="xml" name="Пустая" '
        'objects_total="0" truncated="false"><files/></manifest>'
    )
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.xml", manifest)

    config = load(target)

    assert len(config) == 0
    assert config.is_complete


def _epf_payloads(path: Path):
    with V8Container(path) as container:
        for entry in container.entries():
            payload = entry.read()
            yield payload
            if is_container(payload[:16]):
                with V8Container(payload) as nested:
                    yield from (item.read() for item in nested.entries())


def test_exporter_sources_and_binaries_write_documented_chunk_count_and_current_core():
    dist = PROJECT_ROOT / "exporter-1c" / "dist"
    xml_marker = 'ЗаписатьАтрибут("count"'.encode()
    json_marker = 'ЗаписатьИмяСвойства("count"'.encode()
    current_core_markers = (b"string_allowed_length", b"number_rules_resolved")

    for path in dist.glob("*.bsl"):
        # Самостоятельный runtime-снимок расширений не является schema v1 и
        # потому не пишет чанки структуры с полем count.
        if path.name.startswith("СнимокРасширений_"):
            continue
        payload = path.read_bytes()
        assert xml_marker in payload, path.name
        assert all(marker in payload for marker in current_core_markers), path.name
        if "JSON" in path.name:
            assert json_marker in payload, path.name

    # В сериализованном модуле управляемой формы кавычки удваиваются;
    # обычная форма хранит исходный BSL во вложенном V8-контейнере.
    escaped_xml_marker = 'ЗаписатьАтрибут(""count""'.encode()
    escaped_json_marker = 'ЗаписатьИмяСвойства(""count""'.encode()
    for path in dist.glob("*.epf"):
        payloads = list(_epf_payloads(path))
        assert any(
            xml_marker in payload or escaped_xml_marker in payload
            for payload in payloads
        ), path.name
        assert all(
            any(marker in payload for payload in payloads)
            for marker in current_core_markers
        ), path.name
        if "XML_JSON" in path.name:
            assert any(
                json_marker in payload or escaped_json_marker in payload
                for payload in payloads
            ), path.name
