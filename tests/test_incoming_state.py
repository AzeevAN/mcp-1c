"""Состояния файла incoming и кэш хеша."""
import json
from pathlib import Path

from conftest import состарить
from mcp1c.incoming import (
    STATE_FAILED,
    STATE_NEW,
    IncomingScanner,
)
from mcp1c.registry import Registry


def _реестр(tmp_path) -> Registry:
    registry = Registry(tmp_path / "data")
    registry.incoming_dir.mkdir(parents=True)
    return registry


def _архив(registry: Registry, имя: str, содержимое: bytes = b"") -> Path:
    """Пустой zip (одна запись «конец центрального каталога») или заданный."""
    путь = registry.incoming_dir / имя
    путь.write_bytes(содержимое or b"PK\x05\x06" + b"\0" * 18)
    return путь


def _записать_неудачу(registry: Registry, файл: Path, причина: str) -> None:
    """Отказ прежнего частичного разбора, как его оставил на диске писатель.

    Писателя больше нет, но записанные им отказы лежат в `incoming-state.json`
    у существующих установок и обязаны читаться.
    """
    import hashlib

    (registry.data_dir / "incoming-state.json").write_text(
        json.dumps(
            {
                "digests": {},
                "failures": {
                    файл.name: {
                        "reason": причина,
                        "sha256": hashlib.sha256(файл.read_bytes()).hexdigest(),
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_незнакомый_файл_не_разобран(tmp_path):
    registry = _реестр(tmp_path)
    состарить(_архив(registry, "в.zip"))

    строки = IncomingScanner(registry).scan()

    assert [s["state"] for s in строки] == [STATE_NEW]


def test_строка_реестра_с_тем_же_хешем_и_именем_не_делает_файл_разобранным(
    tmp_path,
):
    """Сверки с реестром нет: «разобрано», «обновлённая выгрузка» и «отбор
    устарел» не существуют, а неизвестные строки источников не влияют."""
    import hashlib

    from conftest import build_configuration, write_export
    from mcp1c.registry import STATUS_READY, Source

    экспорт = tmp_path / "экспорт"
    экспорт.mkdir()
    registry = Registry(tmp_path / "data")
    zip_файл = write_export(экспорт, build_configuration(name="Розница"))
    registry.add_configuration(zip_файл)
    registry.save()
    хеш = hashlib.sha256(zip_файл.read_bytes()).hexdigest()
    unrelated = Source(
        id="UnrelatedSource", kind="unknown-source-kind", origin=zip_файл.name,
        sha256=хеш, loaded_at="2026-10-23T00:00:00+00:00",
        status=STATUS_READY, stored_path="sources/missing.json",
    )
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    payload["sources"].append(unrelated.to_dict())
    registry.registry_path.write_text(json.dumps(payload), encoding="utf-8")
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    копия = registry.incoming_dir / zip_файл.name
    копия.write_bytes(zip_файл.read_bytes())
    состарить(копия)

    строки = IncomingScanner(Registry(registry.data_dir)).scan()

    assert [(s["name"], s["state"], s["detail"]) for s in строки] == [
        (zip_файл.name, STATE_NEW, "")
    ]


def test_неудача_переживает_пересоздание_сканера(tmp_path):
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    _записать_неудачу(registry, файл, "битый архив")

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
    _записать_неудачу(registry, файл, "битый архив")
    сканер = IncomingScanner(registry)
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


def test_состояние_пишется_на_диск_под_замком(tmp_path, monkeypatch):
    """`digest` зовётся из потоков пула: два сохранения могут разойтись.

    Если запись файла идёт вне замка, на диск ложится более старый снимок.
    Кэш хеша потерю переживёт, а записанный отказ — нет: он и существует
    ради того, чтобы пережить рестарт.
    """
    import threading

    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    сканер = IncomingScanner(registry)
    занят = []
    from mcp1c import fsio

    настоящая_запись = fsio.write_record_atomic

    def под_наблюдением(*args, **kwargs):
        # Проверяем из ЧУЖОГО потока: `RLock` для своего повторно входим.
        def попробовать():
            взят = сканер._замок.acquire(timeout=0.2)
            занят.append(not взят)
            if взят:
                сканер._замок.release()

        поток = threading.Thread(target=попробовать)
        поток.start()
        поток.join()
        return настоящая_запись(*args, **kwargs)

    # Состояние пишется атомарной заменой из общего fsio, а не `write_text`.
    monkeypatch.setattr(fsio, "write_record_atomic", под_наблюдением)

    сканер.digest(файл)

    assert занят and all(занят)
