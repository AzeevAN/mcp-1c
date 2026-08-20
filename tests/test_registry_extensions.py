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

    Набор признаков — из брифа задачи, проверен на живой паре «конфигурация +
    её расширение» (CHANGELOG → «Найдено»): у расширения есть `ObjectBelonging`
    и `ConfigurationExtensionPurpose`, непустой `NamePrefix`, и нет
    `CompatibilityMode`. У конфигурации наоборот. Имена в тестах здесь —
    нейтральные, не имена реальных внедрений (см. `AGENTS.md`).
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
    name: str = "РасширениеА",
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

    assert источник.id == "Розница:ext:РасширениеА"
    assert источник.kind == KIND_EXTENSION
    assert "Розница:modules" not in registry.sources


def test_конфигурация_потом_расширение_оба_источника_целы(tmp_path):
    """Главный тест: модули конфигурации не стираются расширением."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")

    registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")

    assert registry.sources["Розница:modules"].kind == KIND_MODULES
    assert registry.sources["Розница:ext:РасширениеА"].kind == KIND_EXTENSION
    модуль_конфигурации = (
        registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    )
    модуль_расширения = (
        registry.extensions_dir / "Розница" / "РасширениеА" / "Catalogs/Р/Ext/ObjectModule.bsl"
    )
    assert модуль_конфигурации.is_file()
    assert модуль_расширения.is_file()
    assert registry.modules_dir.resolve() != registry.extensions_dir.resolve()


def test_расширение_потом_конфигурация_оба_источника_целы(tmp_path):
    """Обратный порядок — тот же результат."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")

    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")

    assert registry.sources["Розница:ext:РасширениеА"].kind == KIND_EXTENSION
    assert registry.sources["Розница:modules"].kind == KIND_MODULES
    assert (registry.extensions_dir / "Розница" / "РасширениеА").is_dir()
    assert (registry.modules_dir / "Розница").is_dir()


def test_два_расширения_одновременно(tmp_path):
    registry = _реестр_с_конфигурацией(tmp_path)

    registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="а.zip", name="РасширениеА"), configuration="Розница"
    )
    registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="б.zip", name="Мобильный"),
        configuration="Розница",
    )

    assert registry.sources["Розница:ext:РасширениеА"].kind == KIND_EXTENSION
    assert registry.sources["Розница:ext:Мобильный"].kind == KIND_EXTENSION
    assert (registry.extensions_dir / "Розница" / "РасширениеА").is_dir()
    assert (registry.extensions_dir / "Розница" / "Мобильный").is_dir()


def test_снятие_расширения_сносит_только_свой_каталог(tmp_path):
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    первое = registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="а.zip", name="РасширениеА"), configuration="Розница"
    )
    registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="б.zip", name="Мобильный"),
        configuration="Розница",
    )

    registry.remove(первое.id)

    assert not (registry.extensions_dir / "Розница" / "РасширениеА").exists()
    assert (registry.extensions_dir / "Розница" / "Мобильный").is_dir()
    assert (registry.modules_dir / "Розница").is_dir()
    assert "Розница:ext:РасширениеА" not in registry.sources
    assert "Розница:ext:Мобильный" in registry.sources
    assert "Розница:modules" in registry.sources


def test_источник_расширения_переживает_перезапуск(tmp_path):
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")
    registry.save()

    заново = Registry(tmp_path / "data")
    проблемы = заново.restore()

    assert проблемы == []
    assert "Розница:ext:РасширениеА" in заново.sources
    assert заново.sources["Розница:ext:РасширениеА"].kind == KIND_EXTENSION
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
    оригинал = _выгрузка_расширения(tmp_path, файл="оригинал.zip", name="РасширениеА")
    копия = tmp_path / "РасширениеА-копия.zip"
    копия.write_bytes(оригинал.read_bytes())

    первый = registry.add_modules(оригинал, configuration="Розница")
    второй = registry.add_modules(копия, configuration="Розница")

    ключи_расширений = [
        идентификатор
        for идентификатор in registry.sources
        if идентификатор.startswith("Розница:ext:")
    ]
    assert ключи_расширений == ["Розница:ext:РасширениеА"]
    assert первый.id == второй.id == "Розница:ext:РасширениеА"
    assert второй.origin == "РасширениеА-копия.zip"
    каталог = registry.extensions_dir / "Розница" / "РасширениеА"
    файлы = [p for p in каталог.rglob("*") if p.is_file()]
    assert len(файлы) == второй.items_total == 1


# --------------------------------------------------------- ревью: критично 1
#
# `_сведения_о_выгрузке` читала строго `zf.read("Configuration.xml")` из
# корня архива. Расширение, упакованное командой `zip -r архив.zip папка`
# (обычный способ упаковки — `intake.extract`/`safe_target` вложенность и так
# поддерживают), корневого `Configuration.xml` не имеет: `KeyError` тихо
# читался как «это конфигурация», и `add_modules` шёл в разрушительную ветку
# модулей. Ревью воспроизвело потерю кода конфигурации на такой упаковке.


def _выгрузка_расширения_в_папке(
    tmp_path: Path,
    *,
    файл: str = "расширение.zip",
    папка: str = "РасширениеА",
    name: str = "РасширениеА",
    код: str = "Процедура Д() КонецПроцедуры",
) -> Path:
    """Раскладка `zip -r архив.zip папка`: `Configuration.xml` не в корне, а
    в единственном каталоге верхнего уровня. Так же устроена настоящая
    плоская выгрузка 8.3.5 (`__data/utd/UtdConfig.zip`, проверено вручную:
    единственная запись-каталог верхнего уровня, `Configuration.xml` внутри
    неё, ни одного файла прямо в корне) — не гипотетический случай.
    """
    путь = tmp_path / файл
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr(f"{папка}/", "")
        zf.writestr(
            f"{папка}/Configuration.xml",
            _configuration_xml(
                name=name, prefix=f"{name}_", belonging="Adopted", purpose="AddOn"
            ),
        )
        zf.writestr(f"{папка}/Catalogs/Р/Ext/ObjectModule.bsl", код)
    return путь


def test_расширение_упакованное_вместе_с_папкой_распознаётся(tmp_path):
    """Критично, ревью: распознавание обязано искать `Configuration.xml` и в
    единственном каталоге верхнего уровня, а не только в корне архива."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    модуль_конфигурации = (
        registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    )
    assert модуль_конфигурации.is_file()

    источник = registry.add_modules(
        _выгрузка_расширения_в_папке(tmp_path), configuration="Розница"
    )

    assert источник.kind == KIND_EXTENSION
    assert источник.id == "Розница:ext:РасширениеА"
    # Главное: код типовой конфигурации цел — не стёрт веткой модулей.
    assert модуль_конфигурации.is_file()
    assert (registry.modules_dir / "Розница").is_dir()


def _выгрузка_с_неполными_признаками(
    tmp_path: Path, *, файл: str = "подозрительный.zip"
) -> Path:
    """`ObjectBelonging` есть, `ConfigurationExtensionPurpose` и `NamePrefix`
    — нет: сильный признак расширения виден, но набор неполный. Ни на
    расширение (не все условия), ни на конфигурацию (у неё нет ObjectBelonging
    вовсе) это не тянет."""
    путь = tmp_path / файл
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr(
            "Configuration.xml", _configuration_xml(name="Т", belonging="Adopted")
        )
        zf.writestr("Catalogs/Р/Ext/ObjectModule.bsl", "Процедура Е() КонецПроцедуры")
    return путь


def test_неполный_набор_признаков_расширения_отклоняется(tmp_path):
    """Критично, ревью: цена ошибок несимметрична — ложное «конфигурация»
    стоит весь разобранный код. Частичное совпадение признаков отказывает
    явно, а не молча падает в ветку модулей."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    модуль_конфигурации = (
        registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    )

    with pytest.raises(RegistryError, match="неполный"):
        registry.add_modules(
            _выгрузка_с_неполными_признаками(tmp_path), configuration="Розница"
        )

    assert модуль_конфигурации.is_file()
    assert not registry.extensions_dir.exists() or not list(
        registry.extensions_dir.rglob("*")
    )


def _выгрузка_без_configuration_xml(
    tmp_path: Path, *, файл: str = "без_ключа.zip"
) -> Path:
    путь = tmp_path / файл
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Catalogs/Р/Ext/ObjectModule.bsl", "Процедура Ж() КонецПроцедуры")
    return путь


def test_архив_без_configuration_xml_отклоняется(tmp_path):
    """Критично, ревью: не нашли `Configuration.xml` — отказ с объяснением,
    а не молчаливое «считаем конфигурацией». Архивы совсем без модулей и форм
    (например, выгрузка структуры метаданных) отсекает более ранняя проверка
    `_отбираемых_членов` со своим текстом — до этой ветки не доходит."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    модуль_конфигурации = (
        registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    )

    with pytest.raises(RegistryError, match="Configuration.xml"):
        registry.add_modules(
            _выгрузка_без_configuration_xml(tmp_path), configuration="Розница"
        )

    assert модуль_конфигурации.is_file()


def test_configuration_xml_неправдоподобно_большой_отклоняется(tmp_path, monkeypatch):
    """Критично, ревью, пункт 4: объявленный размер из центрального каталога
    проверяется до `zf.read` — иначе он тянет в память что дадут."""
    import mcp1c.registry as registry_module

    monkeypatch.setattr(registry_module, "_MAX_CONFIGURATION_XML_SIZE", 100)
    registry = _реестр_с_конфигурацией(tmp_path)

    with pytest.raises(RegistryError, match="весит"):
        registry.add_modules(_выгрузка_расширения(tmp_path), configuration="Розница")


# --------------------------------------------------------- ревью: критично 2


def test_имя_расширения_точка_не_сносит_остальные_расширения(tmp_path):
    """Критично, ревью: `safe_name(".")` == "." и `pathlib` схлопывает его
    при `resolve()`. Без явного отказа второй уровень пути
    `extensions_dir/<Конфигурация>/.` резолвится в САМ каталог конфигурации
    (существующий, чужой), а не в `extensions_dir` — и проверка на
    принадлежность корню это пропускает. Ревью воспроизвело: были два
    расширения, разбор архива с `Name = .` оставил на диске только своё,
    оба прежних источника остались в реестре и указывают на пустоту."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="а.zip", name="РасширениеА"),
        configuration="Розница",
    )
    registry.add_modules(
        _выгрузка_расширения(tmp_path, файл="б.zip", name="РасширениеБ"),
        configuration="Розница",
    )

    ловушка = _выгрузка_расширения(tmp_path, файл="в.zip", name=".")
    with pytest.raises(RegistryError):
        registry.add_modules(ловушка, configuration="Розница")

    assert (registry.extensions_dir / "Розница" / "РасширениеА").is_dir()
    assert (registry.extensions_dir / "Розница" / "РасширениеБ").is_dir()
    assert "Розница:ext:РасширениеА" in registry.sources
    assert "Розница:ext:РасширениеБ" in registry.sources


# --------------------------------------------------------------- ревью: важно 3


def test_разные_сырые_имена_с_одним_очищенным_значением_дают_один_источник(tmp_path):
    """Важно, ревью: `Name = a/b` и `Name = a:b` дают один и тот же каталог
    (`index_cache.safe_name` схлопывает и `/`, и `:` в `_`) — ключ обязан
    строиться из ТОГО ЖЕ очищенного имени, что и каталог, иначе второй разбор
    тихо переписывает файлы первого, а оба источника остаются в реестре и
    врут счётчиком."""
    registry = _реестр_с_конфигурацией(tmp_path)

    первый = registry.add_modules(
        _выгрузка_расширения(
            tmp_path, файл="а.zip", name="a/b", код="Процедура П1() КонецПроцедуры"
        ),
        configuration="Розница",
    )
    второй = registry.add_modules(
        _выгрузка_расширения(
            tmp_path, файл="б.zip", name="a:b", код="Процедура П2() КонецПроцедуры"
        ),
        configuration="Розница",
    )

    assert первый.id == второй.id == "Розница:ext:a_b"
    ключи_расширений = [
        идентификатор
        for идентификатор in registry.sources
        if идентификатор.startswith("Розница:ext:")
    ]
    assert ключи_расширений == ["Розница:ext:a_b"]
    assert (registry.extensions_dir / "Розница" / "a_b").is_dir()

# --------------------------------------------------------- ревью: важно 1
#
# `_сведения_о_выгрузке` при ET.ParseError и при отсутствующих Properties
# по-прежнему давала мягкое (False, "") — то есть «это конфигурация», а
# дальше add_modules сносил уже разобранный код в ветке модулей. Причина
# была в форме фикстур: синтетические архивы тестов несли Configuration.xml
# = "<x/>", и мягкий возврат существовал ради них. Починено положительным
# правилом: в ветку модулей уходим только при непустом CompatibilityMode, во
# всех остальных случаях — отказ.


def _выгрузка_с_обрезанным_configuration_xml(
    tmp_path: Path, *, файл: str = "обрезанный.zip"
) -> Path:
    """Валидный архив (CRC сходится с содержимым), но Configuration.xml
    оборван на середине — так выглядит выгрузка, прерванная на записи."""
    путь = tmp_path / файл
    полный = _configuration_xml(
        name="Доп", prefix="Доп_", belonging="Adopted", purpose="AddOn"
    )
    обрезанный = полный[: len(полный) // 2]
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", обрезанный)
        zf.writestr("Catalogs/Р/Ext/ObjectModule.bsl", "Процедура И() КонецПроцедуры")
    return путь


def _выгрузка_с_чужим_namespace(
    tmp_path: Path, *, файл: str = "чужой_namespace.zip"
) -> Path:
    """Configuration.xml валиден как XML, но в пространстве имён 8.2, а не
    8.3 — `_NS_MDCLASSES` его не найдёт, `Properties is None`."""
    путь = tmp_path / файл
    ns82 = "http://v8.1c.ru/8.2/MDClasses"
    xml = (
        f'<MetaDataObject xmlns="{ns82}">'
        '<Configuration uuid="00000000-0000-0000-0000-000000000000">'
        "<Properties><Name>Доп</Name><NamePrefix>Доп_</NamePrefix>"
        "<ObjectBelonging>Adopted</ObjectBelonging>"
        "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>"
        "</Properties></Configuration></MetaDataObject>"
    )
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", xml)
        zf.writestr("Catalogs/Р/Ext/ObjectModule.bsl", "Процедура К() КонецПроцедуры")
    return путь


def _выгрузка_с_пустым_configuration_xml(
    tmp_path: Path, *, файл: str = "пустой.zip"
) -> Path:
    путь = tmp_path / файл
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", "")
        zf.writestr("Catalogs/Р/Ext/ObjectModule.bsl", "Процедура Л() КонецПроцедуры")
    return путь


def test_нечитаемый_манифест_отказывает_а_не_сносит_конфигурацию(tmp_path):
    """Важно, ревью: обрезанный, из чужого namespace и нулевой длины
    Configuration.xml — во всех трёх случаях отказ с объяснением, код
    конфигурации цел."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    модуль_конфигурации = (
        registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    )

    for построить, файл in (
        (_выгрузка_с_обрезанным_configuration_xml, "обрезанный.zip"),
        (_выгрузка_с_чужим_namespace, "чужой_ns.zip"),
        (_выгрузка_с_пустым_configuration_xml, "пустой.zip"),
    ):
        with pytest.raises(RegistryError):
            registry.add_modules(построить(tmp_path, файл=файл), configuration="Розница")
        assert модуль_конфигурации.is_file(), f"{файл}: код конфигурации задет"


# --------------------------------------------------------- ревью: важно 2


def _испортить_crc_в_центральном_каталоге(архив: Path, имя_члена: str) -> None:
    """Портит CRC-32 записи `имя_члена` в центральном каталоге zip — архив
    остаётся структурно валиден (центральный каталог на месте, `infolist()`
    работает), но чтение члена ловит несовпадение CRC (`BadZipFile`). Так
    выглядит выгрузка, у которой побилась ровно одна запись."""
    данные = bytearray(архив.read_bytes())
    сигнатура = b"PK\x01\x02"
    имя_байты = имя_члена.encode()
    позиция = 0
    while True:
        позиция = данные.find(сигнатура, позиция)
        if позиция == -1:
            raise AssertionError(f"запись {имя_члена!r} не найдена в архиве")
        длина_имени = int.from_bytes(данные[позиция + 28 : позиция + 30], "little")
        начало_имени = позиция + 46
        if данные[начало_имени : начало_имени + длина_имени] == имя_байты:
            смещение = позиция + 16
            данные[смещение : смещение + 4] = (
                int.from_bytes(данные[смещение : смещение + 4], "little") ^ 0xFFFFFFFF
            ).to_bytes(4, "little")
            break
        позиция += 4
    архив.write_bytes(bytes(данные))


def test_битый_crc_configuration_xml_даёт_понятную_ошибку(tmp_path):
    """Важно, ревью: `_сведения_о_выгрузке` больше не ловила `BadZipFile` —
    архив с валидным центральным каталогом, но битым CRC именно у
    Configuration.xml, ронял голое исключение вместо объяснения."""
    registry = _реестр_с_конфигурацией(tmp_path)
    архив = _выгрузка_расширения(tmp_path, файл="битый_crc.zip")
    _испортить_crc_в_центральном_каталоге(архив, "Configuration.xml")

    with pytest.raises(RegistryError, match="не читается"):
        registry.add_modules(архив, configuration="Розница")


# --------------------------------------------------------------- ревью: мелочь 3


def _выгрузка_расширения_как_finder(
    tmp_path: Path, *, файл: str = "finder.zip", папка: str = "Доп", name: str = "Доп"
) -> Path:
    """Имитация архива, собранного Finder («Сжать объекты») на macOS:
    обёртка-папка плюс служебный `__MACOSX/` (с копией ресурсной вилки РОВНО
    настоящего модуля — `._ObjectModule.bsl`, тем же суффиксом, что и
    оригинал) и `.DS_Store` в корне."""
    путь = tmp_path / файл
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr(".DS_Store", b"\x00\x00\x00\x00")
        zf.writestr("__MACOSX/", "")
        zf.writestr(f"__MACOSX/{папка}/", "")
        zf.writestr(f"__MACOSX/{папка}/._Configuration.xml", b"\x00\x05\x16\x07\x00\x02")
        zf.writestr(f"__MACOSX/{папка}/Catalogs/Р/Ext/._ObjectModule.bsl", b"\x00\x05\x16\x07\x00\x02")
        zf.writestr(f"{папка}/", "")
        zf.writestr(
            f"{папка}/Configuration.xml",
            _configuration_xml(
                name=name, prefix=f"{name}_", belonging="Adopted", purpose="AddOn"
            ),
        )
        zf.writestr(f"{папка}/Catalogs/Р/Ext/ObjectModule.bsl", "Процедура М() КонецПроцедуры")
    return путь


def test_архив_упакованный_finder_распознаётся(tmp_path):
    """Мелочь 3, ревью: `__MACOSX/` и `.DS_Store` не должны мешать найти
    единственный каталог верхнего уровня — владелец работает на macOS, и
    «Сжать объекты» через Finder добавляет их всегда."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    модуль_конфигурации = (
        registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    )

    источник = registry.add_modules(
        _выгрузка_расширения_как_finder(tmp_path), configuration="Розница"
    )

    assert источник.kind == KIND_EXTENSION
    assert источник.id == "Розница:ext:Доп"
    assert модуль_конфигурации.is_file()


def test_мусор_finder_не_попадает_в_отобранный_код(tmp_path):
    """Мелочь, ревью: `intake.is_wanted` не знал про `__MACOSX/` —
    `__MACOSX/.../._ObjectModule.bsl` (копия ресурсной вилки настоящего
    модуля) проходила отбор как настоящий `.bsl`. Ревью воспроизвело:
    `items_total=2` на архиве с одним настоящим модулем, и рядом с ним на
    диске лежал двоичный AppleDouble-файл."""
    registry = _реестр_с_конфигурацией(tmp_path)

    источник = registry.add_modules(
        _выгрузка_расширения_как_finder(tmp_path), configuration="Розница"
    )

    assert источник.items_total == 1
    каталог = registry.extensions_dir / "Розница" / "Доп"
    файлы = sorted(p.name for p in каталог.rglob("*") if p.is_file())
    assert файлы == ["ObjectModule.bsl"]


# --------------------------------------------------------------- ре-ревью: критично


def test_расширение_с_compatibilitymode_не_считается_конфигурацией(tmp_path):
    """КРИТИЧНО, ре-ревью: регресс того самого класса, ради которого начат
    этот круг. Манифест со всеми четырьмя признаками расширения
    (`ObjectBelonging`, `ConfigurationExtensionPurpose`, непустой
    `NamePrefix`) плюс непустым `CompatibilityMode` обязан отказывать —
    порядок проверок сначала смотрит сильные признаки расширения и только
    при их отсутствии решает по `CompatibilityMode`. Раньше `CompatibilityMode`
    проверялся первым и без всякого отказа тихо считал такой архив
    конфигурацией — а дальше ветка модулей сносила уже разобранный код."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    модуль_конфигурации = (
        registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    )
    assert модуль_конфигурации.is_file()

    архив = tmp_path / "регресс.zip"
    xml = _configuration_xml(
        name="Доп",
        prefix="Доп_",
        belonging="Adopted",
        purpose="AddOn",
        compatibility="Version8_3_12",
    )
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Configuration.xml", xml)
        zf.writestr("Catalogs/Р/Ext/ObjectModule.bsl", "Процедура Н() КонецПроцедуры")

    with pytest.raises(RegistryError):
        registry.add_modules(архив, configuration="Розница")

    # Модули конфигурации не тронуты, ветка расширения не заведена.
    assert модуль_конфигурации.is_file()
    assert "Розница:modules" in registry.sources
    assert not any(i.startswith("Розница:ext:") for i in registry.sources)


def test_ошибка_распаковки_не_уносит_прежний_разбор(tmp_path):
    """ВАЖНО, ре-ревью: снос каталога раньше шёл ДО распаковки — любая
    ошибка `intake.extract` (битый CRC у МОДУЛЯ, не у манифеста; кончившееся
    место; права) оставляла каталог пустым или полупустым, а источник в
    реестре продолжал висеть со старым `items_total` — реестр и диск
    расходились. Теперь распаковка идёт во временный каталог рядом с
    корнем, замена — только после успеха."""
    registry = _реестр_с_конфигурацией(tmp_path)
    годный = registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    прежний_модуль = registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    assert прежний_модуль.is_file()

    битый = tmp_path / "битый_модуль.zip"
    with zipfile.ZipFile(битый, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            _configuration_xml(name="Розница", compatibility="Version8_3_21"),
        )
        zf.writestr("Catalogs/Д/Ext/ObjectModule.bsl", "Процедура О() КонецПроцедуры")
    _испортить_crc_в_центральном_каталоге(битый, "Catalogs/Д/Ext/ObjectModule.bsl")

    with pytest.raises(zipfile.BadZipFile):
        registry.add_modules(битый, configuration="Розница")

    # Прежний разбор цел на диске — распаковка провалилась ДО замены
    # каталога, а не после сноса старого.
    assert прежний_модуль.is_file()
    новый_модуль = registry.modules_dir / "Розница" / "Catalogs/Д/Ext/ObjectModule.bsl"
    assert not новый_модуль.exists()
    # И реестр не соврал: запись — та же, что и была.
    assert registry.sources["Розница:modules"].items_total == годный.items_total
    assert registry.sources["Розница:modules"].sha256 == годный.sha256
    # Мусора-огрызка временного каталога рядом не осталось.
    остатки = sorted(p.name for p in registry.modules_dir.iterdir())
    assert остатки == ["Розница"]


def test_второе_переименование_не_удалось_прежний_разбор_восстановлен(tmp_path, monkeypatch):
    """ВАЖНО 1, ре-ревью: рокировка (`корень -> отставленный`, затем
    `временный -> корень`) — точка невозврата ОДНО переименование, не снос.
    Если второе переименование не удалось (место кончилось между первым
    `rename` и вторым — не гипотетика), отставленный каталог обязан
    вернуться на место корня: прежний разбор снова цел, реестр и диск
    согласованы."""
    from pathlib import Path

    registry = _реестр_с_конфигурацией(tmp_path)
    годный = registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    прежний_модуль = registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    assert прежний_модуль.is_file()

    настоящий_rename = Path.rename

    def подмена(self, цель):
        # Перехватываем только переименование ВРЕМЕННОГО каталога на место
        # корня — не первую рокировку («корень -> отставленный») и не откат
        # («отставленный -> корень»), у них другое имя источника.
        if ".tmp-" in self.name:
            raise OSError("нет места (симуляция)")
        return настоящий_rename(self, цель)

    monkeypatch.setattr(Path, "rename", подмена)

    новая = tmp_path / "новая.zip"
    with zipfile.ZipFile(новая, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            _configuration_xml(name="Розница", compatibility="Version8_3_21"),
        )
        zf.writestr("Catalogs/Д/Ext/ObjectModule.bsl", "Процедура П() КонецПроцедуры")

    with pytest.raises(OSError):
        registry.add_modules(новая, configuration="Розница")

    # Прежний разбор на месте — рокировка откатилась.
    assert прежний_модуль.is_file()
    новый_модуль = registry.modules_dir / "Розница" / "Catalogs/Д/Ext/ObjectModule.bsl"
    assert not новый_модуль.exists()
    assert registry.sources["Розница:modules"].items_total == годный.items_total
    assert registry.sources["Розница:modules"].sha256 == годный.sha256
    # Ни временного, ни отставленного каталога рядом не осталось.
    остатки = sorted(p.name for p in registry.modules_dir.iterdir())
    assert остатки == ["Розница"]


def test_режим_каталога_не_сужается_после_переразбора(tmp_path):
    """ВАЖНО 2, ре-ревью: `tempfile.mkdtemp` создаёт каталог с правами 0700,
    и без явного выравнивания эти права молча достались бы каталогу кода
    после рокировки — раньше его создавал `mkdir` внутри `extract()` и он
    получал обычные 0755. На bind-mount человек на хосте, читавший каталог
    свободно, после переразбора вдруг перестал бы туда заходить."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    корень = registry.modules_dir / "Розница"
    assert oct(корень.stat().st_mode & 0o777) == "0o755"

    # Владелец сузил права каталога руками (bind-mount, свои соображения) —
    # переразбор не имеет права отменить это молча ни в одну, ни в другую
    # сторону: сохраняем ровно то, что было.
    корень.chmod(0o750)

    registry.add_modules(
        _выгрузка_конфигурации(tmp_path, файл="модули2.zip"), configuration="Розница"
    )

    assert oct(корень.stat().st_mode & 0o777) == "0o750"


def test_провал_первого_переименования_прежний_разбор_цел(tmp_path, monkeypatch):
    """ВАЖНО, ре-ревью: `корень.rename(отставленный)` — первое
    переименование рокировки — тоже должно быть внутри общего try/finally:
    провал оставляет прежний разбор нетронутым и не роняет временный
    каталог рядом."""
    from pathlib import Path

    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    прежний_модуль = registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    assert прежний_модуль.is_file()

    настоящий_rename = Path.rename

    def подмена(self, цель):
        # Перехватываем именно переименование самого корня («Розница» —
        # без ".tmp-" и без ".old-" в имени) — не временный каталог, не
        # откат.
        if self.name == "Розница":
            raise OSError("нет прав (симуляция)")
        return настоящий_rename(self, цель)

    monkeypatch.setattr(Path, "rename", подмена)

    новая = tmp_path / "новая.zip"
    with zipfile.ZipFile(новая, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            _configuration_xml(name="Розница", compatibility="Version8_3_21"),
        )
        zf.writestr("Catalogs/Д/Ext/ObjectModule.bsl", "Процедура Р() КонецПроцедуры")

    with pytest.raises(OSError):
        registry.add_modules(новая, configuration="Розница")

    assert прежний_модуль.is_file()
    остатки = sorted(p.name for p in registry.modules_dir.iterdir())
    assert остатки == ["Розница"]


def test_провал_переименования_при_первом_разборе_не_оставляет_мусора(
    tmp_path, monkeypatch
):
    """ВАЖНО, ре-ревью: ветка первого разбора (`корень` ещё не
    существовал) тоже под защитой — провал переименования не оставляет
    временный каталог висеть навсегда."""
    from pathlib import Path

    registry = _реестр_с_конфигурацией(tmp_path)
    assert not (registry.modules_dir / "Розница").exists()

    настоящий_rename = Path.rename

    def подмена(self, цель):
        if ".tmp-" in self.name:
            raise OSError("нет места (симуляция)")
        return настоящий_rename(self, цель)

    monkeypatch.setattr(Path, "rename", подмена)

    with pytest.raises(OSError):
        registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")

    assert not (registry.modules_dir / "Розница").exists()
    if registry.modules_dir.is_dir():
        assert list(registry.modules_dir.iterdir()) == []


def test_провал_отката_даёт_registryerror_с_обоими_путями(tmp_path, monkeypatch):
    """КРИТИЧНО, ре-ревью: если второе переименование (`временный ->
    корень`) не удалось, а следом не удался и откат (`отставленный ->
    корень`) — природа отказа у обоих одна, «два подряд» не выдумка —
    наверх обязан лететь `RegistryError`, называющий ОБА пути: где лежит
    прежний разбор и где новый. Молча оставлять реестр указывающим в
    пустоту нельзя — это и есть тот дефект, ради которого шли все круги."""
    from pathlib import Path

    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")
    прежний_модуль = registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    assert прежний_модуль.is_file()

    настоящий_rename = Path.rename

    def подмена(self, цель):
        # Валит и второе переименование (временный -> корень), и откат
        # (отставленный -> корень) — оба целятся в корень.
        if ".tmp-" in self.name or ".old-" in self.name:
            raise OSError("нет прав (симуляция)")
        return настоящий_rename(self, цель)

    monkeypatch.setattr(Path, "rename", подмена)

    новая = tmp_path / "новая.zip"
    with zipfile.ZipFile(новая, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            _configuration_xml(name="Розница", compatibility="Version8_3_21"),
        )
        zf.writestr("Catalogs/Д/Ext/ObjectModule.bsl", "Процедура С() КонецПроцедуры")

    with pytest.raises(RegistryError) as инфо:
        registry.add_modules(новая, configuration="Розница")

    текст = str(инфо.value)
    корень = registry.modules_dir / "Розница"
    assert str(корень) in текст

    остатки = sorted(p.name for p in registry.modules_dir.iterdir())
    отставленные = [имя for имя in остатки if ".old-" in имя]
    assert len(отставленные) == 1, остатки
    отставленный_путь = registry.modules_dir / отставленные[0]
    # Отставленный каталог держит прежний разбор физически целым...
    assert (отставленный_путь / "Catalogs/Т/Ext/ObjectModule.bsl").is_file()
    # ...и путь к нему назван в тексте ошибки, чтобы вернуть руками.
    assert str(отставленный_путь) in текст
    # Временного каталога рядом не осталось.
    assert not any(".tmp-" in имя for имя in остатки)


def test_подметание_осиротевшего_tmp_не_трогает_old(tmp_path):
    """Подметание, ре-ревью: осиротевший `.tmp-` от прежнего неудачного
    (убитого) процесса убирается перед новым разбором той же
    конфигурации. `.old-` не трогается: в нём может лежать единственная
    копия прежнего разбора, и решение о ней — за человеком."""
    registry = _реестр_с_конфигурацией(tmp_path)
    registry.add_modules(_выгрузка_конфигурации(tmp_path), configuration="Розница")

    осиротевший_tmp = registry.modules_dir / ".Розница.tmp-старый"
    осиротевший_tmp.mkdir()
    (осиротевший_tmp / "мусор.bsl").write_text("огрызок")
    осиротевший_old = registry.modules_dir / ".Розница.old-старый"
    осиротевший_old.mkdir()
    (осиротевший_old / "прежний.bsl").write_text("единственная копия")

    registry.add_modules(
        _выгрузка_конфигурации(tmp_path, файл="модули2.zip"), configuration="Розница"
    )

    assert not осиротевший_tmp.exists()
    assert осиротевший_old.is_dir()
    assert (осиротевший_old / "прежний.bsl").is_file()
