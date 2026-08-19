"""Расширение конфигурации — отдельный источник, а не затирание модулей.

Выгрузка расширения и выгрузка конфигурации ложились по одному ключу
`<Конфигурация>:modules` и в один каталог `data/modules/<Конфигурация>/`:
вторая выгрузка стирала первую целиком. `Configuration.xml` расширения и
конфигурации различаются по набору тегов (см. `registry._сведения_о_выгрузке`
и CHANGELOG → «Найдено» про `NamePrefix`), и на этом различии построено
распознавание.
"""
import zipfile
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import (
    KIND_CONFIGURATION,
    KIND_EXTENSION,
    KIND_MODULES,
    Registry,
    RegistryError,
)

_NS = "http://v8.1c.ru/8.3/MDClasses"


def _configuration_xml(
    *,
    name: str,
    prefix: str = "",
    compatibility: str = "",
    belonging: str = "",
    purpose: str = "",
) -> str:
    """`Configuration.xml`, минимальный, но с теми же тегами, что у 1С.

    Набор признаков — из брифа задачи, проверен на живой паре «Розница для
    Казахстана» + её расширение «ЮТД» (CHANGELOG → «Найдено»): у расширения
    есть `ObjectBelonging` и `ConfigurationExtensionPurpose`, непустой
    `NamePrefix`, и нет `CompatibilityMode`. У конфигурации наоборот.
    """
    поля = [f"<Name>{name}</Name>"]
    поля.append(f"<NamePrefix>{prefix}</NamePrefix>" if prefix else "<NamePrefix/>")
    if compatibility:
        поля.append(f"<CompatibilityMode>{compatibility}</CompatibilityMode>")
    if belonging:
        поля.append(f"<ObjectBelonging>{belonging}</ObjectBelonging>")
    if purpose:
        поля.append(
            f"<ConfigurationExtensionPurpose>{purpose}</ConfigurationExtensionPurpose>"
        )
    свойства = "".join(поля)
    return (
        f'<MetaDataObject xmlns="{_NS}">'
        '<Configuration uuid="00000000-0000-0000-0000-000000000000">'
        f"<Properties>{свойства}</Properties>"
        "</Configuration></MetaDataObject>"
    )


def _выгрузка_конфигурации(tmp_path: Path, *, файл: str = "модули.zip", name: str = "Розница") -> Path:
    """Выгрузка в файлы САМОЙ конфигурации — без признаков расширения."""
    путь = tmp_path / файл
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            _configuration_xml(name=name, compatibility="Version8_3_21"),
        )
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")
    return путь


def _выгрузка_расширения(
    tmp_path: Path,
    *,
    файл: str = "расширение.zip",
    name: str = "ЮТД",
    код: str = "Процедура Б() КонецПроцедуры",
) -> Path:
    путь = tmp_path / файл
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            _configuration_xml(
                name=name, prefix=f"{name}_", belonging="Adopted", purpose="AddOn"
            ),
        )
        zf.writestr("Catalogs/Р/Ext/ObjectModule.bsl", код)
    return путь


def _реестр_с_конфигурацией(tmp_path: Path, name: str = "Розница") -> Registry:
    входящее = tmp_path / "in"
    входящее.mkdir(exist_ok=True)
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(входящее, build_configuration(name=name)))
    return registry


def test_ключ_источника_расширения_отдельный(tmp_path):
    registry = _реестр_с_конфигурацией(tmp_path)

    источник = registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")

    assert источник.id == "Розница:ext:ЮТД"
    assert источник.kind == KIND_EXTENSION
    assert "Розница:modules" not in registry.sources


def test_конфигурация_потом_расширение_оба_источника_целы(tmp_path):
    """Главный тест: модули конфигурации не стираются расширением."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")

    registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")

    assert registry.sources["Розница:modules"].kind == KIND_MODULES
    assert registry.sources["Розница:ext:ЮТД"].kind == KIND_EXTENSION
    модуль_конфигурации = (
        registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    )
    модуль_расширения = (
        registry.extensions_dir / "Розница" / "ЮТД" / "Catalogs/Р/Ext/ObjectModule.bsl"
    )
    assert модуль_конфигурации.is_file()
    assert модуль_расширения.is_file()
    assert registry.modules_dir.resolve() != registry.extensions_dir.resolve()


def test_расширение_потом_конфигурация_оба_источника_целы(tmp_path):
    """Обратный порядок — тот же результат."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")

    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")

    assert registry.sources["Розница:ext:ЮТД"].kind == KIND_EXTENSION
    assert registry.sources["Розница:modules"].kind == KIND_MODULES
    assert (registry.extensions_dir / "Розница" / "ЮТД").is_dir()
    assert (registry.modules_dir / "Розница").is_dir()


def test_два_расширения_одновременно(tmp_path):
    registry = _реестр_с_конфигурацией(tmp_path)

    registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="а.zip", name="ЮТД"), configuration="Розница"
    )
    registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="б.zip", name="Мобильный"),
        configuration="Розница",
    )

    assert registry.sources["Розница:ext:ЮТД"].kind == KIND_EXTENSION
    assert registry.sources["Розница:ext:Мобильный"].kind == KIND_EXTENSION
    assert (registry.extensions_dir / "Розница" / "ЮТД").is_dir()
    assert (registry.extensions_dir / "Розница" / "Мобильный").is_dir()


def test_снятие_расширения_сносит_только_свой_каталог(tmp_path):
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    первое = registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="а.zip", name="ЮТД"), configuration="Розница"
    )
    registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="б.zip", name="Мобильный"),
        configuration="Розница",
    )

    registry.remove(первое.id)

    assert not (registry.extensions_dir / "Розница" / "ЮТД").exists()
    assert (registry.extensions_dir / "Розница" / "Мобильный").is_dir()
    assert (registry.modules_dir / "Розница").is_dir()
    assert "Розница:ext:ЮТД" not in registry.sources
    assert "Розница:ext:Мобильный" in registry.sources
    assert "Розница:modules" in registry.sources


def test_источник_расширения_переживает_перезапуск(tmp_path):
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")
    registry.save()

    заново = Registry(tmp_path / "data")
    проблемы = заново.restore()

    assert проблемы == []
    assert "Розница:ext:ЮТД" in заново.sources
    assert заново.sources["Розница:ext:ЮТД"].kind == KIND_EXTENSION
    # Метаданные конфигурации тоже восстановлены — не только расширение.
    assert заново.sources["Розница"].kind == KIND_CONFIGURATION


def test_имя_расширения_с_необычными_символами_не_уводит_за_extensions_dir(tmp_path):
    """Имя расширения берётся из `Name` внутри выгрузки — доверять ему нельзя."""
    registry = _реестр_с_конфигурацией(tmp_path)
    сосед = registry.incoming_dir
    сосед.mkdir(parents=True, exist_ok=True)
    (сосед / "не трогать.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)

    чистый_побег = _выгрузка_расширения(tmp_path, файл="а.zip", name="..")
    with pytest.raises(RegistryError):
        registry.add_modules(чистый_побег, configuration="Розница")

    # Косая черта не создаёт вложенности: имя чистится до одного сегмента,
    # и результат остаётся внутри extensions_dir.
    вложенный = _выгрузка_расширения(tmp_path, файл="б.zip", name="../incoming")
    registry.add_modules(вложенный, configuration="Розница")

    assert (сосед / "не трогать.zip").is_file()
    легло = sorted(p.name for p in (registry.extensions_dir / "Розница").iterdir())
    assert легло == [".._incoming"]


def test_sweep_не_сносит_кэш_нового_вида(tmp_path):
    """`KIND_EXTENSION` обязан быть в `CACHE_KINDS` — иначе кэш сочли бы ничьим."""
    from mcp1c import index_cache

    registry = _реестр_с_конфигурацией(tmp_path)
    источник = registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")
    registry.cache_dir.mkdir(parents=True, exist_ok=True)
    кэш = registry._cache_path(источник.id, "modules")
    кэш.write_bytes(b"stop")

    убрано = index_cache.sweep(registry.cache_dir, registry._cached_names())

    assert кэш.name not in убрано
    assert кэш.is_file()


def test_две_копии_одного_расширения_под_разными_именами_один_источник(tmp_path):
    """Личность расширения — тег `Name` внутри выгрузки, а не имя файла.

    Человек кладёт в incoming/ один и тот же архив расширения дважды, просто
    переименовав файл. Ожидание — источник один: второй разбор переразбирает
    то же расширение (тот же ключ, тот же каталог), `origin` обновляется на
    имя последнего разобранного файла.
    """
    registry = _реестр_с_конфигурацией(tmp_path)
    оригинал = _выгрузка_расширения(tmp_path, файл="retailExt.zip", name="ЮТД")
    копия = tmp_path / "ЮТД-копия.zip"
    копия.write_bytes(оригинал.read_bytes())

    первый = registry.add_modules(оригинал, configuration="Розница")
    второй = registry.add_modules(копия, configuration="Розница")

    ключи_расширений = [
        идентификатор
        for идентификатор in registry.sources
        if идентификатор.startswith("Розница:ext:")
    ]
    assert ключи_расширений == ["Розница:ext:ЮТД"]
    assert первый.id == второй.id == "Розница:ext:ЮТД"
    assert второй.origin == "ЮТД-копия.zip"
    каталог = registry.extensions_dir / "Розница" / "ЮТД"
    файлы = [p for p in каталог.rglob("*") if p.is_file()]
    assert len(файлы) == второй.items_total == 1
