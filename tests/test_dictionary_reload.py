"""Перечитывание словаря: правка применяется, индексы не страдают.

Словарь читается только в момент поиска — на постинги он не влияет. Значит
перечитывание обязано быть подменой двух ссылок, а не пересборкой индексов.
"""

from __future__ import annotations

from mcp1c import registry as registry_module
from mcp1c.registry import Registry

from conftest import build_configuration, write_export
from native_export import загрузить_код


def _registry(tmp_path):
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    zip_path = write_export(incoming, build_configuration())
    registry = Registry(data_dir)
    registry.add_configuration(zip_path)
    return registry


def test_правка_словаря_не_перестраивает_индексы(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    built = {"objects": 0, "fields": 0}
    objects_original = registry_module.index_configuration
    fields_original = registry_module.index_fields
    monkeypatch.setattr(
        registry_module,
        "index_configuration",
        lambda *a, **k: (built.__setitem__("objects", built["objects"] + 1), objects_original(*a, **k))[1],
    )
    monkeypatch.setattr(
        registry_module,
        "index_fields",
        lambda *a, **k: (built.__setitem__("fields", built["fields"] + 1), fields_original(*a, **k))[1],
    )

    registry.reload_dictionary()

    assert built == {"objects": 0, "fields": 0}


def test_новый_псевдоним_действует_после_перечитывания(tmp_path):
    registry = _registry(tmp_path)
    index = registry.configurations["ТестоваяКонфигурация"].index
    assert [h.doc.id for h in index.search("реализация услуг")][0] == (
        "Документ.РеализацияТоваровУслуг"
    )

    registry.dictionary.add_alias(
        "реализация услуг", ["Справочник.Контрагенты"], config="ТестоваяКонфигурация"
    )
    registry.dictionary.save(registry.dictionary_path)
    registry.reload_dictionary()

    hits = registry.configurations["ТестоваяКонфигурация"].index.search("реализация услуг")
    assert [h.doc.id for h in hits][0] == "Справочник.Контрагенты"
    assert hits[0].reason == "псевдоним из словаря"


def test_новый_синоним_действует_после_перечитывания(tmp_path):
    registry = _registry(tmp_path)

    registry.dictionary.add_synonyms(["контрагент", "поставщик"])
    registry.dictionary.save(registry.dictionary_path)
    registry.reload_dictionary()

    hits = registry.configurations["ТестоваяКонфигурация"].index.search("поставщик")
    assert "Справочник.Контрагенты" in [h.doc.id for h in hits]


def test_локальный_синоним_доходит_до_поиска_процедур_без_рестарта(реестр_с_кодом):
    from mcp1c import tools

    registry = реестр_с_кодом

    def найдено() -> bool:
        ответ = tools.search_procedures(registry, "суммирует", config="Пример")
        return "ОбщийМодуль.ОбщийПример::Сложить" in ответ

    assert not найдено()

    registry.mutate_dictionary(
        lambda dictionary: dictionary.add_synonyms(["складывает", "суммирует"])
    )
    assert найдено()

    registry.mutate_dictionary(
        lambda dictionary: dictionary.remove_synonyms(["складывает", "суммирует"])
    )
    assert not найдено()


def test_свежесобранный_поиск_процедур_видит_локальные_синонимы(
    tmp_path, корень_кода, архив_кода
):
    from mcp1c import tools
    from mcp1c.dictionary import Dictionary

    # Словарь задан до загрузки кода: свежая сборка (не подъём из кэша)
    # раньше получала только встроенные синонимы.
    data = tmp_path / "data"
    data.mkdir()
    Dictionary(synonym_groups=[["складывает", "суммирует"]]).save(
        data / "dictionary.json"
    )
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(data)
    configuration = build_configuration(name="Пример")
    registry.add_configuration(write_export(incoming, configuration))
    загрузить_код(registry, архив_кода(корень_кода), configuration="Пример")

    ответ = tools.search_procedures(registry, "суммирует", config="Пример")
    assert "ОбщийМодуль.ОбщийПример::Сложить" in ответ
