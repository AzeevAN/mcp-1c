"""Шесть состояний файла и кэш хеша."""
from pathlib import Path

import pytest

from conftest import состарить
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


def _архив(registry: Registry, имя: str, содержимое: bytes = b"") -> Path:
    """Пустой zip (одна запись «конец центрального каталога») или заданный."""
    путь = registry.incoming_dir / имя
    путь.write_bytes(содержимое or b"PK\x05\x06" + b"\0" * 18)
    return путь


def test_незнакомый_файл_не_разобран(tmp_path):
    registry = _реестр(tmp_path)
    состарить(_архив(registry, "в.zip"))

    строки = IncomingScanner(registry).scan()

    assert [s["state"] for s in строки] == [STATE_NEW]


def test_знакомый_хеш_даёт_разобрано(tmp_path):
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    сканер = IncomingScanner(registry)
    хеш = сканер.digest(файл)
    registry.sources["Р:modules"] = Source(
        id="Р:modules", kind=KIND_MODULES, origin="в.zip", sha256=хеш
    )

    строки = сканер.scan()

    assert строки[0]["state"] == STATE_READY


def test_то_же_имя_другой_хеш_даёт_обновлённую(tmp_path):
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    registry.sources["Р:modules"] = Source(
        id="Р:modules", kind=KIND_MODULES, origin="в.zip", sha256="другой"
    )

    строки = IncomingScanner(registry).scan()

    assert строки[0]["state"] == STATE_UPDATED


def test_неудача_переживает_пересоздание_сканера(tmp_path):
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    IncomingScanner(registry).note_failure(файл, "битый архив")

    строки = IncomingScanner(registry).scan()

    assert строки[0]["state"] == STATE_FAILED
    assert "битый архив" in строки[0]["detail"]


def test_хеш_не_пересчитывается_пока_файл_не_менялся(tmp_path, monkeypatch):
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
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
    файл = состарить(_архив(registry, "в.zip"))
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
    состарить(_архив(registry, "файл.zip"))
    (registry.incoming_dir / "каталог.zip").mkdir()

    строки = IncomingScanner(registry).scan()

    # Только файл в выдаче, каталог пропущен.
    assert len(строки) == 1
    assert строки[0]["name"] == "файл.zip"


def test_состояние_json_пустой_объект_работает(tmp_path):
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    # Кладём в incoming-state.json пустой объект.
    (registry.data_dir / "incoming-state.json").write_text("{}", encoding="utf-8")

    строки = IncomingScanner(registry).scan()

    # scan() должен работать, состояние считается с нуля.
    assert len(строки) == 1
    assert строки[0]["state"] == "не разобрано"


def test_замена_файла_снимает_записанную_неудачу(tmp_path):
    """Исправленный архив под тем же именем — не тот же архив.

    Неудача пишется по имени файла и переживает рестарт (это задумано), но
    привязана к содержимому: иначе выйти из «разбор не удался» можно было бы
    только переименованием файла или правкой `incoming-state.json`.
    """
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    сканер = IncomingScanner(registry)
    сканер.note_failure(файл, "битый архив")
    assert сканер.scan()[0]["state"] == STATE_FAILED

    # Кладём под тем же именем другое содержимое — как `cp` поверх.
    состарить(_архив(registry, "в.zip", b"PK\x05\x06" + b"\0" * 19))

    assert IncomingScanner(registry).scan()[0]["state"] == STATE_NEW


def test_старый_формат_неудачи_не_роняет_показ(tmp_path):
    """`failures` со строкой вместо словаря — формат до привязки к хешу."""
    registry = _реестр(tmp_path)
    состарить(_архив(registry, "в.zip"))
    (registry.data_dir / "incoming-state.json").write_text(
        '{"digests": {}, "failures": {"в.zip": "битый архив"}}', encoding="utf-8"
    )

    строки = IncomingScanner(registry).scan()

    assert строки[0]["state"] == STATE_FAILED
    assert "битый архив" in строки[0]["detail"]


def test_свежий_файл_не_хешируется_и_объясняется(tmp_path, monkeypatch):
    """Пока идёт `cp`, sha256 считать нечего: он устареет к концу копирования."""
    registry = _реестр(tmp_path)
    _архив(registry, "в.zip")  # без `состарить`: файл только что записан
    считали = []
    monkeypatch.setattr(
        "mcp1c.incoming._sha256_файла",
        lambda путь: считали.append(путь) or "x",
    )

    строки = IncomingScanner(registry).scan()

    assert считали == []
    assert строки[0]["state"] == STATE_NEW
    assert "копируется" in строки[0]["detail"]
    assert строки[0]["settling"] is True


def test_метка_в_будущем_не_считается_копированием(tmp_path):
    """`cp -p`, `rsync -t`, перекос часов — и файл навсегда «копируется».

    Односторонняя проверка возраста давала тупик того же класса, что и
    неснимаемая неудача: ни кнопки, ни разбора, выйти через интерфейс нельзя.
    """
    import os
    import time

    registry = _реестр(tmp_path)
    файл = _архив(registry, "в.zip")
    вперёд = time.time() + 3600
    os.utime(файл, (вперёд, вперёд))

    строки = IncomingScanner(registry).scan()

    assert строки[0]["settling"] is False
    assert строки[0]["state"] == STATE_NEW
    assert строки[0]["detail"] == ""
