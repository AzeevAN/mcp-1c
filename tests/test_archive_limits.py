"""Ресурсные границы внешнего ZIP и вложенного FileStorage."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from mcp1c.loader import ExportError, load
from mcp1c.registry import Registry, RegistryError
from mcp1c.syntax_parser import parse_hbk

from module_samples import v8_container_bytes


def test_json_чанк_с_аномальным_сжатием_отклоняется(tmp_path):
    manifest = {
        "schema_version": "1",
        "format": "json",
        "name": "ТестоваяКонфигурация",
        "platform": "8.3.23",
        "objects_total": 0,
        "truncated": False,
        "files": [{"path": "objects/all.001.json", "type": "Справочник", "count": 0}],
    }
    archive_path = tmp_path / "metadata.zip"
    chunk = json.dumps(
        {
            "schema_version": "1",
            "type": "Справочник",
            "chunk": 1,
            "count": 0,
            "objects": [],
        }
    ).encode() + b" " * (8 * 1024 * 1024)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("objects/all.001.json", chunk)
    assert archive_path.stat().st_size < 64 * 1024

    with pytest.raises(ExportError, match="предел|сжати"):
        load(archive_path)


def test_страница_вложенного_zip_с_аномальным_сжатием_отклоняется(tmp_path):
    storage_bytes = io.BytesIO()
    with zipfile.ZipFile(
        storage_bytes, "w", compression=zipfile.ZIP_DEFLATED
    ) as storage:
        storage.writestr("objects/bomb.html", b" " * (8 * 1024 * 1024))
    hbk = tmp_path / "help.hbk"
    hbk.write_bytes(v8_container_bytes([("FileStorage", storage_bytes.getvalue())]))

    with pytest.raises(ValueError, match="предел|сжати"):
        parse_hbk(hbk, platform="8.3.23")

    registry = Registry(tmp_path / "data")
    with pytest.raises(RegistryError, match="предел|сжати"):
        registry.add_syntax(hbk, platform="8.3.23")
    assert registry.sources == {}
