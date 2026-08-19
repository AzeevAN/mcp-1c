"""Шесть состояний файла и кэш хеша."""
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c.incoming import (
    STATE_FAILED,
    STATE_NEW,
    STATE_READY,
    STATE_UPDATED,
    IncomingScanner,
)
from mcp1c.registry import KIND_MODULES, Registry, Source


def _реестр(tmp_path) -> Registry:
    registry = Registry(tmp_path / "data")
    registry.incoming_dir.mkdir(parents=True)
    return registry


def test_незнакомый_файл_не_разобран(tmp_path):
    registry = _реестр(tmp_path)
    (registry.incoming_dir / "в.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)

    строки = IncomingScanner(registry).scan()

    assert [s["state"] for s in строки] == [STATE_NEW]


def test_знакомый_хеш_даёт_разобрано(tmp_path):
    registry = _реестр(tmp_path)
    файл = registry.incoming_dir / "в.zip"
    файл.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    сканер = IncomingScanner(registry)
    хеш = сканер.digest(файл)
    registry.sources["Р:modules"] = Source(
        id="Р:modules", kind=KIND_MODULES, origin="в.zip", sha256=хеш
    )

    строки = сканер.scan()

    assert строки[0]["state"] == STATE_READY


def test_то_же_имя_другой_хеш_даёт_обновлённую(tmp_path):
    registry = _реестр(tmp_path)
    файл = registry.incoming_dir / "в.zip"
    файл.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    registry.sources["Р:modules"] = Source(
        id="Р:modules", kind=KIND_MODULES, origin="в.zip", sha256="другой"
    )

    строки = IncomingScanner(registry).scan()

    assert строки[0]["state"] == STATE_UPDATED


def test_неудача_переживает_пересоздание_сканера(tmp_path):
    registry = _реестр(tmp_path)
    файл = registry.incoming_dir / "в.zip"
    файл.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    IncomingScanner(registry).note_failure(файл, "битый архив")

    строки = IncomingScanner(registry).scan()

    assert строки[0]["state"] == STATE_FAILED
    assert "битый архив" in строки[0]["detail"]


def test_хеш_не_пересчитывается_пока_файл_не_менялся(tmp_path, monkeypatch):
    registry = _реестр(tmp_path)
    файл = registry.incoming_dir / "в.zip"
    файл.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    сканер = IncomingScanner(registry)
    сканер.digest(файл)

    считали = []
    monkeypatch.setattr(
        "mcp1c.incoming._sha256_файла",
        lambda путь: считали.append(путь) or "x",
    )
    сканер.digest(файл)

    assert считали == []
