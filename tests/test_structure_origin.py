"""Происхождение эффективной структуры из файловых выгрузок расширений.

Тесты используют только синтетические имена и архивы.  Schema v1 намеренно
уже содержит итоговое поле: новый слой объясняет его происхождение, а не
меняет контракт структурной выгрузки.
"""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c.model import Field, MetadataObject
from mcp1c.registry import Registry, RegistryError
from mcp1c.tools import get_object


_NS = "http://v8.1c.ru/8.3/MDClasses"
_CONFIG = "ТестоваяКонфигурация"
_OBJECT = "Справочник.Контрагенты"
_FIELD = "ДополнительныйКод"


def _configuration_xml(
    *,
    extension: str = "",
    catalogs: tuple[str, ...] = ("Контрагенты",),
) -> str:
    if extension:
        properties = (
            f"<Name>{extension}</Name><NamePrefix>{extension}_</NamePrefix>"
            "<ObjectBelonging>Adopted</ObjectBelonging>"
            "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>"
        )
    else:
        properties = (
            f"<Name>{_CONFIG}</Name><NamePrefix/>"
            "<CompatibilityMode>Version8_3_21</CompatibilityMode>"
        )
    children = "".join(f"<Catalog>{name}</Catalog>" for name in catalogs)
    return (
        f'<MetaDataObject xmlns="{_NS}">'
        '<Configuration uuid="00000000-0000-0000-0000-000000000000">'
        f"<Properties>{properties}</Properties>"
        f"<ChildObjects>{children}</ChildObjects>"
        "</Configuration></MetaDataObject>"
    )


def _catalog_xml(
    fields: tuple[str, ...],
    *,
    name: str = "Контрагенты",
    adopted: bool = False,
) -> str:
    belonging = "<ObjectBelonging>Adopted</ObjectBelonging>" if adopted else ""
    attributes = "".join(
        "<Attribute uuid=\"00000000-0000-0000-0000-000000000000\">"
        f"<Properties><Name>{name}</Name></Properties></Attribute>"
        for name in fields
    )
    return (
        f'<MetaDataObject xmlns="{_NS}">'
        '<Catalog uuid="00000000-0000-0000-0000-000000000000">'
        f"<Properties><Name>{name}</Name>{belonging}</Properties>"
        f"<ChildObjects>{attributes}</ChildObjects>"
        "</Catalog></MetaDataObject>"
    )


def _archive(
    tmp_path: Path,
    filename: str,
    *,
    fields: tuple[str, ...],
    extension: str = "",
) -> Path:
    archive = tmp_path / filename
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Configuration.xml", _configuration_xml(extension=extension))
        zf.writestr(
            "Catalogs/Контрагенты.xml",
            _catalog_xml(fields, adopted=bool(extension)),
        )
        zf.writestr(
            "Catalogs/Контрагенты/Ext/ObjectModule.bsl",
            "Процедура Проверка() Экспорт\nКонецПроцедуры\n",
        )
    return archive


def _own_object_archive(tmp_path: Path, name: str = "ВнешнийОбъект") -> Path:
    archive = tmp_path / "own-object.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            _configuration_xml(extension="Дополнение", catalogs=(name,)),
        )
        zf.writestr(
            f"Catalogs/{name}.xml",
            _catalog_xml(("ЛокальныйТекст",), name=name),
        )
        zf.writestr(
            f"Catalogs/{name}/Ext/ObjectModule.bsl",
            "Процедура Проверка() Экспорт\nКонецПроцедуры\n",
        )
    return archive


def _flat_archive(
    tmp_path: Path,
    filename: str,
    *,
    fields: tuple[str, ...],
    extension: str = "",
) -> Path:
    archive = tmp_path / filename
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Configuration.xml", _configuration_xml(extension=extension))
        zf.writestr(
            "Catalog.Контрагенты.xml",
            _catalog_xml(fields, adopted=bool(extension)),
        )
        zf.writestr(
            "Catalog.Контрагенты.ObjectModule.txt",
            "Процедура Проверка() Экспорт\nКонецПроцедуры\n",
        )
    return archive


def _registry(tmp_path: Path) -> Registry:
    config = build_configuration(name=_CONFIG)
    config.objects[_OBJECT].attributes.append(Field(name=_FIELD))
    incoming = tmp_path / "metadata"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(incoming, config))
    return registry


def test_без_базового_каталога_происхождение_неизвестно(tmp_path):
    registry = _registry(tmp_path)
    registry.add_modules(
        _archive(
            tmp_path,
            "extension.zip",
            fields=("ИНН", "Телефон", _FIELD),
            extension="Дополнение",
        ),
        configuration=_CONFIG,
    )

    answer = get_object(registry, _OBJECT, config=_CONFIG, detail="fields")

    assert "Происхождение структуры: **неизвестно**" in answer
    assert "каталога основной файловой выгрузки" in answer
    assert "объявлен расширением" not in answer


def test_смена_поколения_базы_инвалидирует_дельту_расширения(tmp_path):
    registry = _registry(tmp_path)
    registry.add_modules(
        _archive(tmp_path, "base-old.zip", fields=("ИНН", "Телефон")),
        configuration=_CONFIG,
    )
    registry.add_modules(
        _archive(
            tmp_path,
            "extension.zip",
            fields=("ИНН", "Телефон", _FIELD),
            extension="Дополнение",
        ),
        configuration=_CONFIG,
    )
    before = get_object(registry, _OBJECT, config=_CONFIG, detail="fields")
    assert "объявлен расширением «Дополнение»" in before

    registry.add_modules(
        _archive(
            tmp_path,
            "base-new.zip",
            fields=("ИНН", "Телефон", _FIELD),
        ),
        configuration=_CONFIG,
    )

    after = get_object(registry, _OBJECT, config=_CONFIG, detail="fields")
    assert "Происхождение структуры: **неизвестно**" in after
    assert "поколение основной файловой выгрузки изменилось" in after
    assert "объявлен расширением" not in after


def test_одинаковое_поле_двух_расширений_не_получает_победителя(tmp_path):
    registry = _registry(tmp_path)
    registry.add_modules(
        _archive(tmp_path, "base.zip", fields=("ИНН", "Телефон")),
        configuration=_CONFIG,
    )
    for filename, extension in (
        ("first.zip", "Первое"),
        ("second.zip", "Второе"),
    ):
        registry.add_modules(
            _archive(
                tmp_path,
                filename,
                fields=("ИНН", "Телефон", _FIELD),
                extension=extension,
            ),
            configuration=_CONFIG,
        )

    answer = get_object(registry, _OBJECT, config=_CONFIG, detail="fields")

    field_line = next(line for line in answer.splitlines() if f"`{_FIELD}`" in line)
    assert "источник неоднозначен" in field_line
    assert "«Первое»" in field_line
    assert "«Второе»" in field_line
    assert "объявлен расширением «Первое»" not in field_line


def test_собственный_объект_помечен_рядом_с_полным_именем(tmp_path):
    registry = _registry(tmp_path)
    own_name = "ВнешнийОбъект"
    own_address = f"Справочник.{own_name}"
    own_object = MetadataObject(
        full_name=own_address,
        kind="Справочник",
        name=own_name,
        attributes=[Field(name="ЛокальныйТекст")],
    )
    incoming = tmp_path / "metadata-own"
    incoming.mkdir()
    config = build_configuration(name=_CONFIG)
    config.objects[_OBJECT].attributes.append(Field(name=_FIELD))
    config.objects[own_address] = own_object
    registry.add_configuration(write_export(incoming, config))
    registry.add_modules(
        _archive(tmp_path, "base.zip", fields=("ИНН", "Телефон")),
        configuration=_CONFIG,
    )
    registry.add_modules(_own_object_archive(tmp_path), configuration=_CONFIG)

    answer = get_object(registry, own_address, config=_CONFIG, detail="fields")

    assert f"Полное имя: `{own_address}`" in answer
    assert "Объявлен расширением: «Дополнение»" in answer


def test_ссылочное_поле_не_получает_недоказанную_пометку(tmp_path):
    registry = _registry(tmp_path)
    reference = "СвязанныйОбъект"
    incoming = tmp_path / "metadata-reference"
    incoming.mkdir()
    config = build_configuration(name=_CONFIG)
    config.objects[_OBJECT].attributes.extend(
        [Field(name=_FIELD), Field(name=reference, types=[_OBJECT])]
    )
    registry.add_configuration(write_export(incoming, config))
    registry.add_modules(
        _archive(tmp_path, "base.zip", fields=("ИНН", "Телефон")),
        configuration=_CONFIG,
    )
    registry.add_modules(
        _archive(
            tmp_path,
            "extension.zip",
            fields=("ИНН", "Телефон", _FIELD, reference),
            extension="Дополнение",
        ),
        configuration=_CONFIG,
    )

    answer = get_object(registry, _OBJECT, config=_CONFIG, detail="fields")
    reference_line = next(
        line for line in answer.splitlines() if f"`{reference}`" in line
    )

    assert "объявлен расширением" not in reference_line
    assert "объявлен расширением «Дополнение»" in answer


def test_каталог_переживает_restart_без_исходных_zip(tmp_path):
    registry = _registry(tmp_path)
    base = _archive(tmp_path, "base.zip", fields=("ИНН", "Телефон"))
    extension = _archive(
        tmp_path,
        "extension.zip",
        fields=("ИНН", "Телефон", _FIELD),
        extension="Дополнение",
    )
    registry.add_modules(base, configuration=_CONFIG)
    registry.add_modules(extension, configuration=_CONFIG)
    registry.save()
    base.unlink()
    extension.unlink()

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    answer = get_object(restarted, _OBJECT, config=_CONFIG, detail="fields")

    assert "объявлен расширением «Дополнение»" in answer


def test_плоская_выгрузка_строит_тот_же_семантический_каталог(tmp_path):
    registry = _registry(tmp_path)
    registry.add_modules(
        _flat_archive(tmp_path, "base.zip", fields=("ИНН", "Телефон")),
        configuration=_CONFIG,
    )
    registry.add_modules(
        _flat_archive(
            tmp_path,
            "extension.zip",
            fields=("ИНН", "Телефон", _FIELD),
            extension="Дополнение",
        ),
        configuration=_CONFIG,
    )

    answer = get_object(registry, _OBJECT, config=_CONFIG, detail="fields")

    assert "объявлен расширением «Дополнение»" in answer


def test_служебный_каталог_не_считается_модулем_без_адреса(tmp_path):
    registry = _registry(tmp_path)
    source = registry.add_modules(
        _archive(tmp_path, "base.zip", fields=("ИНН", "Телефон")),
        configuration=_CONFIG,
    )

    coverage = registry.modules[source.id].каталог.coverage

    assert coverage.total_candidates == 1
    assert coverage.unknown_address == 0


def test_повреждённый_каталог_даёт_unknown_а_не_роняет_restore(tmp_path):
    from mcp1c import structure_origin

    root = tmp_path / "code"
    root.mkdir()
    (root / structure_origin.CATALOG_FILE).write_bytes(gzip.compress(b"[]"))

    assert structure_origin.load(root) is None


def test_семантические_адреса_сопоставляются_без_учёта_регистра():
    from mcp1c import structure_origin

    base = structure_origin.StructureCatalog(
        role=structure_origin.ROLE_BASE,
        source_sha256="base",
        base_sha256="",
        complete=True,
        objects=frozenset({"Справочник.Объект"}),
        fields=frozenset({"Справочник.Объект.Код"}),
    )
    raw = structure_origin.DeclaredStructure(
        complete=True,
        objects=frozenset({"справочник.объект"}),
        fields=frozenset(
            {"справочник.объект.код", "справочник.объект.НовоеПоле"}
        ),
    )

    catalog = structure_origin.extension_catalog(raw, "extension", base)

    assert catalog.objects == frozenset()
    assert catalog.fields == frozenset({"справочник.объект.НовоеПоле"})


def test_ошибка_записи_каталога_не_меняет_прежнее_поколение(
    tmp_path, monkeypatch
):
    from mcp1c import structure_origin

    registry = _registry(tmp_path)
    registry.add_modules(
        _archive(tmp_path, "base.zip", fields=("ИНН", "Телефон")),
        configuration=_CONFIG,
    )
    old_archive = _archive(
        tmp_path,
        "extension-old.zip",
        fields=("ИНН", "Телефон", _FIELD),
        extension="Дополнение",
    )
    old_source = registry.add_modules(old_archive, configuration=_CONFIG)
    old_answer = get_object(registry, _OBJECT, config=_CONFIG, detail="fields")

    def fail(*_args, **_kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(structure_origin, "save", fail)
    with pytest.raises(RegistryError):
        registry.add_modules(
            _archive(
                tmp_path,
                "extension-new.zip",
                fields=("ИНН", "Телефон"),
                extension="Дополнение",
            ),
            configuration=_CONFIG,
        )

    assert registry.sources[old_source.id] is old_source
    assert get_object(registry, _OBJECT, config=_CONFIG, detail="fields") == old_answer
