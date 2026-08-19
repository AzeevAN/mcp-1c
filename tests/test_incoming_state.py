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


def test_данные_только_для_чтения_не_роняют_сканирование(tmp_path, monkeypatch):
    registry = _реестр(tmp_path)
    файл = registry.incoming_dir / "в.zip"
    файл.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    сканер = IncomingScanner(registry)

    # Монкепатчим Path.write_text, чтобы имитировать том только для чтения.
    оригинальная_запись = Path.write_text

    def не_может_писать(self, *args, **kwargs):
        raise PermissionError("том только для чтения")

    monkeypatch.setattr(Path, "write_text", не_может_писать)

    # scan() должен работать и вернуть файл, хотя кэш не сохранился.
    строки = сканер.scan()

    assert len(строки) == 1
    assert строки[0]["name"] == "в.zip"
    assert строки[0]["state"] == "не разобрано"


def test_каталог_с_расширением_zip_пропускается(tmp_path):
    registry = _реестр(tmp_path)
    (registry.incoming_dir / "файл.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    (registry.incoming_dir / "каталог.zip").mkdir()

    строки = IncomingScanner(registry).scan()

    # Только файл в выдаче, каталог пропущен.
    assert len(строки) == 1
    assert строки[0]["name"] == "файл.zip"


def test_состояние_json_пустой_объект_работает(tmp_path):
    registry = _реестр(tmp_path)
    файл = registry.incoming_dir / "в.zip"
    файл.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    # Кладём в incoming-state.json пустой объект.
    (registry.data_dir / "incoming-state.json").write_text("{}", encoding="utf-8")

    строки = IncomingScanner(registry).scan()

    # scan() должен работать, состояние считается с нуля.
    assert len(строки) == 1
    assert строки[0]["state"] == "не разобрано"
