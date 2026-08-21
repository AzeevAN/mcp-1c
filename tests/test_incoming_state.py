"""Шесть состояний файла и кэш хеша."""
import json
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import build_configuration, modules_configuration_xml, состарить, write_export
from mcp1c.incoming import (
    STATE_FAILED,
    STATE_NEW,
    STATE_READY,
    STATE_STALE,
    STATE_UPDATED,
    IncomingScanner,
)
from mcp1c.intake import SELECTION_VERSION
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
    """Источник, разобранный нынешним правилом отбора (`selection_version`
    равен текущей `SELECTION_VERSION`), — «разобрано», а не «отбор устарел»."""
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    сканер = IncomingScanner(registry)
    хеш = сканер.digest(файл)
    registry.sources["Р:modules"] = Source(
        id="Р:modules",
        kind=KIND_MODULES,
        origin="в.zip",
        sha256=хеш,
        selection_version=SELECTION_VERSION,
    )

    строки = сканер.scan()

    assert строки[0]["state"] == STATE_READY


def test_версия_отбора_меньше_текущей_даёт_устарел(tmp_path):
    """Источник, разобранный более старым правилом отбора, — «отбор устарел»,
    а не «разобрано»: `_состояние` сравнивает `selection_version` источника
    с текущей `SELECTION_VERSION` напрямую, без запасного значения."""
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    сканер = IncomingScanner(registry)
    хеш = сканер.digest(файл)
    registry.sources["Р:modules"] = Source(
        id="Р:modules",
        kind=KIND_MODULES,
        origin="в.zip",
        sha256=хеш,
        selection_version=SELECTION_VERSION - 1,
    )

    строки = сканер.scan()

    assert строки[0]["state"] == STATE_STALE
    assert строки[0]["detail"] == "Р:modules"


def test_предыдущее_правило_отбора_помечено_устаревшим(tmp_path):
    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    сканер = IncomingScanner(registry)
    хеш = сканер.digest(файл)
    registry.sources["Р:modules"] = Source(
        id="Р:modules",
        kind=KIND_MODULES,
        origin="в.zip",
        sha256=хеш,
        selection_version=3,
    )

    строки = сканер.scan()

    assert SELECTION_VERSION == 4
    assert строки[0]["state"] == STATE_STALE


def test_startup_не_переразбирает_selection_v3_из_incoming_автоматически(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    data_dir = tmp_path / "data"
    registry = Registry(data_dir)
    registry.add_configuration(
        write_export(input_dir, build_configuration(name="Розница"))
    )
    archive = input_dir / "modules.zip"
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr("Configuration.xml", modules_configuration_xml())
        opened.writestr(
            "Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура А() КонецПроцедуры",
        )
    registry.add_modules(archive, configuration="Розница")
    registry.save()

    raw = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    for source in raw["sources"]:
        if source["id"] == "Розница:modules":
            source["selection_version"] = 3
    registry.registry_path.write_text(json.dumps(raw), encoding="utf-8")
    incoming = data_dir / "incoming"
    incoming.mkdir(exist_ok=True)
    copied = incoming / archive.name
    copied.write_bytes(archive.read_bytes())
    состарить(copied)

    restored = Registry(data_dir)
    calls = []
    monkeypatch.setattr(
        restored,
        "add_modules",
        lambda *_args, **_kwargs: calls.append(True),
    )

    restored.startup()

    assert calls == []
    assert restored.sources["Розница:modules"].selection_version == 3
    assert IncomingScanner(restored).scan()[0]["state"] == STATE_STALE


def test_явный_reparse_selection_v4_добавляет_дескриптор_и_form_bin(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(input_dir, build_configuration(name="Розница"))
    )
    old_archive = input_dir / "old.zip"
    with zipfile.ZipFile(old_archive, "w") as opened:
        opened.writestr("Configuration.xml", modules_configuration_xml())
        opened.writestr(
            "Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура Старая() КонецПроцедуры",
        )
    registry.add_modules(old_archive, configuration="Розница")
    registry.save()
    root = registry.modules["Розница:modules"].корень

    new_archive = input_dir / "new.zip"
    with zipfile.ZipFile(new_archive, "w") as opened:
        opened.writestr("Configuration.xml", modules_configuration_xml())
        opened.writestr(
            "Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура Новая() КонецПроцедуры",
        )
        opened.writestr("Catalogs/Т/Forms/Основная.xml", "descriptor")
        opened.writestr("Catalogs/Т/Forms/Основная/Ext/Form.bin", b"container")

    source = registry.add_modules(new_archive, configuration="Розница")

    assert source.selection_version == 4
    assert (root / "Catalogs/Т/Forms/Основная.xml").read_text() == "descriptor"
    assert (root / "Catalogs/Т/Forms/Основная/Ext/Form.bin").read_bytes() == b"container"
    assert "Новая" in (root / "Catalogs/Т/Ext/ObjectModule.bsl").read_text()
    persisted = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    persisted_source = next(
        item for item in persisted["sources"] if item["id"] == "Розница:modules"
    )
    assert persisted_source["sha256"] == source.sha256
    assert persisted_source["origin"] == "new.zip"
    assert persisted_source["selection_version"] == 4

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.sources["Розница:modules"].sha256 == source.sha256


def test_отказ_save_при_reparse_возвращает_старый_корень_и_source(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(input_dir, build_configuration(name="Розница"))
    )

    def archive(path: Path, procedure: str) -> Path:
        with zipfile.ZipFile(path, "w") as opened:
            opened.writestr("Configuration.xml", modules_configuration_xml())
            opened.writestr(
                "Catalogs/Т/Ext/ObjectModule.bsl",
                f"Процедура {procedure}() КонецПроцедуры",
            )
        return path

    old = registry.add_modules(
        archive(input_dir / "old.zip", "Старая"), configuration="Розница"
    )
    registry.save()
    root = registry.modules[old.id].корень
    persisted_before = registry.registry_path.read_bytes()
    monkeypatch.setattr(registry, "save", lambda: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(Exception):
        registry.add_modules(
            archive(input_dir / "new.zip", "Новая"), configuration="Розница"
        )

    assert registry.sources[old.id] is old
    assert registry.modules[old.id].source is old
    assert "Старая" in (root / "Catalogs/Т/Ext/ObjectModule.bsl").read_text()
    assert registry.registry_path.read_bytes() == persisted_before


def test_отказ_публикации_расходного_кэша_не_маскирует_успешный_reparse(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(input_dir, build_configuration(name="Розница"))
    )
    archive = input_dir / "modules.zip"
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr("Configuration.xml", modules_configuration_xml())
        opened.writestr(
            "Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура Новая() КонецПроцедуры",
        )

    original_iterdir = Path.iterdir

    def iterdir(path):
        if path.name.startswith(".modules-cache.tmp-"):
            raise OSError("cache unavailable")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    source = registry.add_modules(archive, configuration="Розница")

    assert registry.sources[source.id] is source
    persisted = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert any(item["sha256"] == source.sha256 for item in persisted["sources"])


def test_startup_откатывает_рокировку_оборванную_до_registry_json(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(input_dir, build_configuration(name="Розница"))
    )
    archive = input_dir / "modules.zip"
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr("Configuration.xml", modules_configuration_xml())
        opened.writestr(
            "Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура Старая() КонецПроцедуры",
        )
    old = registry.add_modules(archive, configuration="Розница")
    old.selection_version = 3
    registry.save()
    root = registry.modules[old.id].корень
    temporary = root.parent / f".{root.name}.tmp-crash"
    shutil.copytree(root, temporary)
    module = temporary / "Catalogs/Т/Ext/ObjectModule.bsl"
    module.write_text("Процедура Новая() КонецПроцедуры", encoding="utf-8")
    new = replace(old, selection_version=4)

    registry._начать_рокировку_кода(new, root, temporary)
    registry._swap_code(archive, root, temporary)
    # Имитируем SIGKILL: registry.json остался v3, finally не выполнялся.

    restarted = Registry(registry.data_dir)
    restarted.startup()

    assert "Старая" in (root / "Catalogs/Т/Ext/ObjectModule.bsl").read_text()
    assert restarted.sources[old.id].selection_version == 3
    assert not restarted._module_swap_path.exists()


def test_startup_завершает_рокировку_если_registry_json_уже_новый(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(input_dir, build_configuration(name="Розница"))
    )
    archive = input_dir / "modules.zip"
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr("Configuration.xml", modules_configuration_xml())
        opened.writestr(
            "Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура Старая() КонецПроцедуры",
        )
    old = registry.add_modules(archive, configuration="Розница")
    old.selection_version = 3
    registry.save()
    root = registry.modules[old.id].корень
    temporary = root.parent / f".{root.name}.tmp-crash"
    shutil.copytree(root, temporary)
    (temporary / "Catalogs/Т/Ext/ObjectModule.bsl").write_text(
        "Процедура Новая() КонецПроцедуры", encoding="utf-8"
    )
    new = replace(old, selection_version=4)

    registry._начать_рокировку_кода(new, root, temporary)
    detached = registry._swap_code(archive, root, temporary)
    registry.sources[new.id] = new
    registry.save()
    assert detached is not None and detached.exists()
    # Имитируем SIGKILL после registry.json, но до удаления journal/.old.

    restarted = Registry(registry.data_dir)
    restarted.startup()

    assert "Новая" in (root / "Catalogs/Т/Ext/ObjectModule.bsl").read_text()
    assert restarted.sources[new.id].selection_version == 4
    assert not detached.exists()
    assert not restarted._module_swap_path.exists()


def test_startup_подметает_staging_каталог_кэша_после_sigkill(tmp_path):
    registry = Registry(tmp_path / "data")
    stale = registry.cache_dir / ".modules-cache.tmp-crash"
    stale.mkdir(parents=True)
    (stale / "Пример_modules.modules-toc").write_bytes(b"large cache")

    registry.startup()

    assert not stale.exists()


@pytest.mark.parametrize(
    "marker,protected",
    [
        (
            {
                "version": 1,
                "source_id": "Пример:modules",
                "new_sha256": "new",
                "new_selection_version": 4,
                "root": ".",
                "temporary": ".tmp-crash",
                "detached": None,
            },
            "keep.txt",
        ),
        (
            {
                "version": 1,
                "source_id": "Пример:modules",
                "new_sha256": "new",
                "new_selection_version": 4,
                "root": "modules",
                "temporary": "modules/.modules.tmp-crash",
                "detached": None,
            },
            "modules/keep.txt",
        ),
        (
            {
                "version": 1,
                "source_id": "Пример:modules",
                "new_sha256": "new",
                "new_selection_version": 4,
                "root": "modules/Другой",
                "temporary": "modules/.Другой.tmp-crash",
                "detached": None,
            },
            "modules/Другой/keep.txt",
        ),
        (["не", "объект"], "keep.txt"),
    ],
    ids=["data-root", "modules-root", "other-source", "json-list"],
)
def test_битый_wal_не_удаляет_чужие_каталоги(marker, protected, tmp_path):
    registry = Registry(tmp_path / "data")
    sentinel = registry.data_dir / protected
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep", encoding="utf-8")
    registry._module_swap_path.write_text(
        json.dumps(marker, ensure_ascii=False), encoding="utf-8"
    )

    problems = registry._восстановить_рокировку_кода()

    assert problems
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_неисправимый_wal_блокирует_все_источники_кода_на_startup(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(input_dir, build_configuration(name="Розница"))
    )
    archive = input_dir / "modules.zip"
    with zipfile.ZipFile(archive, "w") as opened:
        opened.writestr("Configuration.xml", modules_configuration_xml())
        opened.writestr(
            "Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура Старая() КонецПроцедуры",
        )
    source = registry.add_modules(archive, configuration="Розница")
    registry._module_swap_path.write_text("[]", encoding="utf-8")
    registry_before = registry.registry_path.read_bytes()
    cache_before = {
        path.name: path.read_bytes()
        for path in registry.cache_dir.iterdir()
        if path.is_file()
    }

    restarted = Registry(registry.data_dir)
    problems = restarted.startup()

    assert problems
    assert source.id not in restarted.sources
    assert source.id not in restarted.modules
    assert registry.registry_path.read_bytes() == registry_before
    assert {
        path.name: path.read_bytes()
        for path in registry.cache_dir.iterdir()
        if path.is_file()
    } == cache_before

    registry._module_swap_path.unlink()
    recovered = Registry(registry.data_dir)
    assert recovered.startup() == []
    assert source.id in recovered.sources


def test_запись_без_selection_version_из_registry_json_после_restore_устарела(
    tmp_path,
):
    """Запись, сделанная до появления поля `selection_version` (обычный
    `registry.json` тех версий кода), при чтении не должна выглядеть свежей.

    `from_dict` для отсутствующего ключа обязан поставить 0, а не текущую
    `SELECTION_VERSION` — соврать «отбор свежий» про запись, о которой ничего
    не известно, означало бы, что человек никогда не увидит «переразобрать»
    для кода, который никогда не проходил через нынешнее правило отбора.
    """
    входящее = tmp_path / "in"
    входящее.mkdir()
    данные = tmp_path / "data"
    registry = Registry(данные)
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))
    архив = tmp_path / "модули.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml())
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")
    registry.add_modules(архив, configuration="Розница")
    registry.save()

    # Имитируем старую запись: убираем ключ selection_version из sources —
    # так выглядит registry.json, сделанный до появления этого поля.
    сырое = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    for источник in сырое["sources"]:
        if источник["id"] == "Розница:modules":
            assert "selection_version" in источник
            del источник["selection_version"]
    registry.registry_path.write_text(json.dumps(сырое), encoding="utf-8")

    заново = Registry(данные)
    assert заново.restore() == []
    assert заново.sources["Розница:modules"].selection_version == 0

    заново.incoming_dir.mkdir(parents=True, exist_ok=True)
    копия = заново.incoming_dir / "модули.zip"
    копия.write_bytes(архив.read_bytes())
    состарить(копия)

    строки = IncomingScanner(заново).scan()

    assert строки[0]["state"] == STATE_STALE


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


def test_состояние_пишется_на_диск_под_замком(tmp_path, monkeypatch):
    """`note_failure` зовётся из потоков пула: два сохранения могут разойтись.

    Если запись файла идёт вне замка, на диск ложится более старый снимок.
    Кэш хеша потерю переживёт, а записанный отказ — нет: он и существует
    ради того, чтобы пережить рестарт.
    """
    import threading

    registry = _реестр(tmp_path)
    файл = состарить(_архив(registry, "в.zip"))
    сканер = IncomingScanner(registry)
    занят = []
    настоящая_запись = Path.write_text

    def под_наблюдением(self, *args, **kwargs):
        # Проверяем из ЧУЖОГО потока: `RLock` для своего повторно входим.
        def попробовать():
            взят = сканер._замок.acquire(timeout=0.2)
            занят.append(not взят)
            if взят:
                сканер._замок.release()

        поток = threading.Thread(target=попробовать)
        поток.start()
        поток.join()
        return настоящая_запись(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", под_наблюдением)

    сканер.note_failure(файл, "битый архив")

    assert занят and all(занят)
