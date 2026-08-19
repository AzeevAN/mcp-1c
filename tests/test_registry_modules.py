"""Источник модулей не должен вытеснять метаданные той же конфигурации."""
import zipfile
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import KIND_CONFIGURATION, KIND_MODULES, Registry


def _выгрузка_в_файлы(tmp_path: Path) -> Path:
    путь = tmp_path / "модули.zip"
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")
    return путь


def test_ключ_источника_модулей_отдельный(tmp_path):
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    метаданные = registry.add_configuration(
        write_export(входящее, build_configuration(name="Розница"))
    )

    модули = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    assert метаданные.id == "Розница"
    assert модули.id == "Розница:modules"
    assert модули.kind == KIND_MODULES
    # Метаданные на месте: ключи разные, вытеснения не произошло.
    assert registry.sources["Розница"].kind == KIND_CONFIGURATION
    assert "Розница" in registry.configurations


def test_код_лёг_в_свой_каталог(tmp_path):
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))

    registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    лежит = registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    assert лежит.is_file()


def test_исходник_не_копируется(tmp_path):
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))

    registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    # `keep_source=True` положил бы второй экземпляр архива на том.
    скопировано = list((registry.sources_dir).rglob("модули.zip"))
    assert скопировано == []


def test_источник_модулей_переживает_перезапуск(tmp_path):
    входящее = tmp_path / "in"
    входящее.mkdir()
    данные = tmp_path / "data"
    registry = Registry(данные)
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))
    registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")
    registry.save()

    заново = Registry(данные)
    проблемы = заново.restore()

    assert проблемы == []
    assert "Розница:modules" in заново.sources
    assert заново.sources["Розница:modules"].kind == KIND_MODULES
    # Метаданные конфигурации тоже восстановлены — не только модули.
    assert заново.sources["Розница"].kind == KIND_CONFIGURATION


def _другая_выгрузка(tmp_path: Path) -> Path:
    """Вторая выгрузка той же конфигурации: прежнего модуля в ней уже нет."""
    путь = tmp_path / "модули2.zip"
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("Catalogs/Д/Ext/ObjectModule.bsl", "Процедура Б() КонецПроцедуры")
    return путь


def _реестр_с_конфигурацией(tmp_path: Path) -> Registry:
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))
    return registry


def test_переразбор_не_оставляет_файлов_прежней_выгрузки(tmp_path):
    """Иначе две выгрузки молча смешиваются: `extract` пишет поверх."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")
    прежний = registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    assert прежний.is_file()

    registry.add_modules(_другая_выгрузка(tmp_path), configuration="Розница")

    assert not прежний.exists()
    новый = registry.modules_dir / "Розница" / "Catalogs/Д/Ext/ObjectModule.bsl"
    assert новый.is_file()


def test_имя_конфигурации_не_уводит_за_modules_dir(tmp_path):
    """Имя берётся из манифеста: там встречается и косая черта, и `..`."""
    from mcp1c.registry import RegistryError

    registry = _реестр_с_конфигурацией(tmp_path)
    архив = _выгрузка_в_файлы(tmp_path)
    сосед = registry.incoming_dir
    сосед.mkdir(parents=True, exist_ok=True)
    (сосед / "не трогать.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)

    # Конфигурация с таким именем в реестр не попадёт — проверяем сам путь.
    registry.configurations["../incoming"] = registry.configurations["Розница"]
    registry.configurations[".."] = registry.configurations["Розница"]

    with pytest.raises(RegistryError):
        registry.add_modules(архив, configuration="..")

    # Косая черта не создаёт вложенности: имя чистится до одного сегмента.
    registry.add_modules(архив, configuration="../incoming")
    assert (сосед / "не трогать.zip").is_file()
    легло = sorted(p.name for p in registry.modules_dir.iterdir())
    assert легло == [".._incoming"]


def test_снятие_источника_удаляет_каталог_с_кодом(tmp_path):
    """351 МБ на живой конфигурации: `orphan_sources` их не покажет."""
    registry = _реестр_с_конфигурацией(tmp_path)
    источник = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")
    корень = registry.modules_dir / "Розница"
    assert корень.is_dir()

    registry.remove(источник.id)

    assert not корень.exists()
    assert источник.id not in registry.sources
    # Каталог приёма рядом — его снятие источника не касается.
    assert not registry.incoming_dir.exists() or registry.incoming_dir.is_dir()


def test_выгрузка_без_модулей_и_форм_отвергается(tmp_path):
    """Ноль отобранных файлов — не «разобрано»: источник не заводится."""
    from mcp1c.registry import RegistryError

    registry = _реестр_с_конфигурацией(tmp_path)
    пустая = tmp_path / "метаданные.zip"
    with zipfile.ZipFile(пустая, "w") as zf:
        zf.writestr("manifest.json", '{"schema_version": "1"}')

    with pytest.raises(RegistryError, match="ни модулей, ни форм"):
        registry.add_modules(пустая, configuration="Розница")

    assert "Розница:modules" not in registry.sources
    assert not (registry.modules_dir / "Розница").exists()


def test_негодный_архив_не_сносит_прежний_разбор(tmp_path):
    """Отказ обязан приходить ДО удаления: иначе ошибочное нажатие стоит 351 МБ.

    Сценарий боевой и предусмотрен самим текстом отказа: человек кладёт в
    `incoming/` выгрузку структуры метаданных вместо выгрузки в файлы и жмёт
    «разобрать». Проверка места не останавливает — отбираемого ноль, нужен
    только запас под индекс, места хватает.
    """
    from mcp1c.registry import RegistryError

    registry = _реестр_с_конфигурацией(tmp_path)
    источник = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")
    корень = registry.modules_dir / "Розница"
    было = sorted(p.relative_to(корень).as_posix() for p in корень.rglob("*"))

    метаданные = tmp_path / "СтруктураКонфигурации_Розница.zip"
    with zipfile.ZipFile(метаданные, "w") as zf:
        zf.writestr("manifest.json", '{"schema_version": "1"}')

    with pytest.raises(RegistryError, match="ни модулей, ни форм"):
        registry.add_modules(метаданные, configuration="Розница")

    # Прежний разбор цел: каталог, файлы в нём и учётная запись.
    assert корень.is_dir()
    assert sorted(p.relative_to(корень).as_posix() for p in корень.rglob("*")) == было
    в_реестре = registry.sources["Розница:modules"]
    assert в_реестре.sha256 == источник.sha256
    assert в_реестре.items_total == источник.items_total


def test_снятие_источника_не_держит_замок_на_время_удаления(tmp_path, monkeypatch):
    """Тот же замок берут `resolve()` и инструменты MCP: 11 072 файла — не миг."""
    import shutil
    import threading

    registry = _реестр_с_конфигурацией(tmp_path)
    источник = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")
    свободен = []
    настоящий_rmtree = shutil.rmtree

    def под_наблюдением(путь, *args, **kwargs):
        # Проверяем из ЧУЖОГО потока: `RLock` повторно входим для своего.
        def попробовать():
            взят = registry._lock.acquire(timeout=2)
            свободен.append(взят)
            if взят:
                registry._lock.release()

        поток = threading.Thread(target=попробовать)
        поток.start()
        поток.join()
        return настоящий_rmtree(путь, *args, **kwargs)

    monkeypatch.setattr("mcp1c.registry.shutil.rmtree", под_наблюдением)

    registry.remove(источник.id)

    assert свободен == [True]
    assert not (registry.modules_dir / "Розница").exists()


def test_архив_с_членами_наружу_не_даёт_пустой_источник(tmp_path):
    """Предпроверка считает имена, `extract` их ещё и санирует.

    Отбираемые члены с `..` и абсолютным путём проходят предпроверку по
    центральному каталогу, но `intake.safe_target` отвергает их при записи —
    на диск не ложится ничего. Без проверки после распаковки завёлся бы
    источник со `status=ready` при пустом каталоге.
    """
    from mcp1c.registry import RegistryError

    registry = _реестр_с_конфигурацией(tmp_path)
    годный = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    злой = tmp_path / "злой.zip"
    with zipfile.ZipFile(злой, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("../наружу/ObjectModule.bsl", "Процедура В() КонецПроцедуры")
        zf.writestr("/tmp/абсолютный/ObjectModule.bsl", "Процедура Г() КонецПроцедуры")

    with pytest.raises(RegistryError, match="ни модулей, ни форм"):
        registry.add_modules(злой, configuration="Розница")

    # Источник не переписан нулём, пустого каталога не осталось.
    assert registry.sources["Розница:modules"].items_total == годный.items_total
    assert registry.sources["Розница:modules"].sha256 == годный.sha256
    assert not (registry.modules_dir / "Розница").exists()
    assert not (tmp_path / "наружу").exists()


def test_негодный_архив_не_хешируется(tmp_path, monkeypatch):
    """sha256 — полный проход по файлу; платить им за отказ незачем."""
    from mcp1c.registry import RegistryError

    registry = _реестр_с_конфигурацией(tmp_path)
    метаданные = tmp_path / "СтруктураКонфигурации_Розница.zip"
    with zipfile.ZipFile(метаданные, "w") as zf:
        zf.writestr("manifest.json", '{"schema_version": "1"}')
    считали = []
    monkeypatch.setattr(
        "mcp1c.registry._sha256", lambda путь: считали.append(путь) or "x"
    )

    with pytest.raises(RegistryError, match="ни модулей, ни форм"):
        registry.add_modules(метаданные, configuration="Розница")

    assert считали == []


def test_платформа_модулей_наследуется_от_конфигурации(tmp_path):
    """Выгрузка в файлы точной сборки платформы не содержит — берём у хозяйки."""
    registry = _реестр_с_конфигурацией(tmp_path)

    модули = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    assert модули.platform == registry.sources["Розница"].platform
    assert модули.platform == "8.3.23.1997"


def test_платформа_модулей_пустая_если_у_конфигурации_пустая(tmp_path):
    """Ничего не выдумываем: нет платформы у конфигурации — нет и у модулей."""
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    конфигурация = build_configuration(name="Розница")
    конфигурация.platform = ""
    registry.add_configuration(write_export(входящее, конфигурация))

    модули = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    assert модули.platform == ""


def test_гонка_снятия_конфигурации_не_роняет_разбор(tmp_path, monkeypatch):
    """Между `extract` (секунды в отдельном потоке) и записью источника
    конфигурацию мог снять параллельный запрос — не голый `KeyError`."""
    from mcp1c import intake

    registry = _реестр_с_конфигурацией(tmp_path)
    настоящий = intake.extract

    def подмена(архив, корень):
        результат = настоящий(архив, корень)
        registry.remove("Розница")
        return результат

    monkeypatch.setattr(intake, "extract", подмена)

    модули = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    assert модули.platform == ""
    assert "Розница:modules" in registry.sources
    assert "Розница" not in registry.configurations
