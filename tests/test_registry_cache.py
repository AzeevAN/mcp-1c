"""Реестр и кэш индексов: что происходит между стартами.

Проверяется наблюдаемое поведение — построен индекс заново или поднят, есть
файлы кэша или нет, — а не внутреннее устройство кэша (оно в
`test_index_cache.py`).
"""

from __future__ import annotations

import json

import pytest

from mcp1c import index_cache, registry as registry_module
from mcp1c.registry import Registry, RegistryError

from conftest import build_configuration, write_export, write_syntax


@pytest.fixture
def export(tmp_path):
    """Выгрузка schema v1 в каталоге, который реестр считает своим."""
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    data_dir.mkdir()
    return data_dir, write_export(incoming, build_configuration())


@pytest.fixture
def spy_build(monkeypatch):
    """Счётчик построений индексов — их и должен избегать кэш."""
    counters = {"objects": 0, "fields": 0}
    original_objects = registry_module.index_configuration
    original_fields = registry_module.index_fields

    def count_objects(*args, **kwargs):
        counters["objects"] += 1
        return original_objects(*args, **kwargs)

    def count_fields(*args, **kwargs):
        counters["fields"] += 1
        return original_fields(*args, **kwargs)

    monkeypatch.setattr(registry_module, "index_configuration", count_objects)
    monkeypatch.setattr(registry_module, "index_fields", count_fields)
    return counters


def test_первая_загрузка_сохраняет_кэш(export):
    data_dir, zip_path = export
    registry = Registry(data_dir)

    registry.add_configuration(zip_path)

    files = {p.name for p in (data_dir / "index" / "cache").iterdir()}
    assert files == {"ТестоваяКонфигурация.objects", "ТестоваяКонфигурация.fields"}


def test_повторная_загрузка_не_строит_индексы_заново(export, spy_build):
    data_dir, zip_path = export
    Registry(data_dir).add_configuration(zip_path)
    assert spy_build == {"objects": 1, "fields": 1}

    Registry(data_dir).add_configuration(zip_path)

    assert spy_build == {"objects": 1, "fields": 1}, "индексы построены во второй раз"


def test_поднятый_из_кэша_индекс_ищет_так_же(export):
    data_dir, zip_path = export
    первый = Registry(data_dir)
    первый.add_configuration(zip_path)
    второй = Registry(data_dir)
    второй.add_configuration(zip_path)

    for query in ("контрагенты", "номер телефона", "реализация товаров и услуг"):
        было = [h.doc.id for h in первый.configurations["ТестоваяКонфигурация"].index.search(query)]
        стало = [h.doc.id for h in второй.configurations["ТестоваяКонфигурация"].index.search(query)]
        assert стало == было, f"выдача разошлась на запросе «{query}»"

        было_поля = [
            h.doc.id for h in первый.configurations["ТестоваяКонфигурация"].field_index.search(query)
        ]
        стало_поля = [
            h.doc.id for h in второй.configurations["ТестоваяКонфигурация"].field_index.search(query)
        ]
        assert стало_поля == было_поля


def test_разные_конфигурации_с_одинаковым_именем_архива_переживают_рестарт(
    tmp_path,
):
    data_dir = tmp_path / "data"
    архивы = []
    ожидаемые_хеши = {}

    for каталог, имя in (("первый", "КонфигурацияА"), ("второй", "КонфигурацияБ")):
        incoming = tmp_path / каталог
        incoming.mkdir()
        config = build_configuration()
        config.name = имя
        archive = write_export(incoming, config)
        archive = archive.replace(incoming / "same.zip")
        архивы.append(archive)

    registry = Registry(data_dir)
    for archive in архивы:
        source = registry.add_configuration(archive)
        ожидаемые_хеши[source.id] = source.sha256
    registry.save()

    restarted = Registry(data_dir)
    problems = restarted.restore()

    assert problems == []
    assert set(restarted.configurations) == {"КонфигурацияА", "КонфигурацияБ"}
    assert {
        source_id: restarted.sources[source_id].sha256
        for source_id in ожидаемые_хеши
    } == ожидаемые_хеши
    assert len({restarted.sources[name].stored_path for name in ожидаемые_хеши}) == 2


def test_сохранение_источника_не_следует_по_символической_ссылке(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    archive = write_export(incoming, build_configuration())
    registry = Registry(tmp_path / "data")
    source = registry.add_configuration(archive)
    stored = registry.data_dir / source.stored_path

    outside = tmp_path / "outside.zip"
    outside.write_bytes("не трогать".encode())
    stored.unlink()
    stored.symlink_to(outside)

    replaced = registry.add_configuration(archive)
    replaced_path = registry.data_dir / replaced.stored_path

    assert outside.read_bytes() == "не трогать".encode()
    assert not replaced_path.is_symlink()
    assert replaced_path.read_bytes() == archive.read_bytes()


def test_сохранение_источника_не_следует_по_ссылке_каталога(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    archive = write_export(incoming, build_configuration())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_dir / "sources").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RegistryError, match="безопасно сохранить"):
        Registry(data_dir).add_configuration(archive)

    assert list(outside.iterdir()) == []


def test_restore_отклоняет_сохранённый_источник_с_другим_хешем(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    original = write_export(incoming, build_configuration())
    registry = Registry(tmp_path / "data")
    source = registry.add_configuration(original)
    registry.save()

    changed_config = build_configuration()
    changed_config.name = "ДругаяКонфигурация"
    changed_dir = tmp_path / "changed"
    changed_dir.mkdir()
    changed = write_export(changed_dir, changed_config)
    (registry.data_dir / source.stored_path).write_bytes(changed.read_bytes())

    restarted = Registry(registry.data_dir)
    problems = restarted.restore()

    assert any("контрольная сумма" in problem for problem in problems)
    assert restarted.configurations == {}


def test_restore_требует_совпадения_id_реестра_и_сохранённого_источника(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    archive = write_export(incoming, build_configuration())
    registry = Registry(tmp_path / "data")
    registry.add_configuration(archive)
    registry.save()

    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    payload["sources"][0]["id"] = "ПодменённаяКонфигурация"
    registry.registry_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    restarted = Registry(registry.data_dir)
    problems = restarted.restore()

    assert any("идентификатор" in problem for problem in problems)
    assert restarted.configurations == {}


def test_перевыгрузка_источника_заставляет_строить_заново(export, spy_build, tmp_path):
    data_dir, zip_path = export
    Registry(data_dir).add_configuration(zip_path)

    # Та же конфигурация, но объектов стало больше — файл другой, хеш другой.
    config = build_configuration()
    config.objects.pop("Документ.РеализацияТоваровУслуг")
    новая = write_export(tmp_path / "incoming", config)

    Registry(data_dir).add_configuration(новая)

    assert spy_build == {"objects": 2, "fields": 2}


def test_недоступный_для_записи_кэш_не_ломает_старт(export):
    """Том смонтирован только на чтение — это не повод не работать.

    Кэш расходный: не записался — значит следующий старт снова построит.
    Уронить из-за него сервер нельзя.
    """
    data_dir, zip_path = export
    (data_dir / "index").mkdir(parents=True)
    (data_dir / "index" / "cache").write_text("это файл, а не каталог", encoding="utf-8")

    registry = Registry(data_dir)
    registry.add_configuration(zip_path)

    hits = registry.configurations["ТестоваяКонфигурация"].index.search("контрагенты")
    assert [h.doc.id for h in hits][0] == "Справочник.Контрагенты"


def test_справка_поднимается_из_кэша(export, monkeypatch):
    """Индекс справки — самая дорогая часть старта, 1,2 с из трёх."""
    data_dir, _ = export
    syntax_path = write_syntax(data_dir / "index" / "syntax")

    Registry(data_dir).add_syntax(syntax_path)

    built = {"count": 0}
    original = registry_module.index_syntax

    def count(*args, **kwargs):
        built["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(registry_module, "index_syntax", count)
    registry = Registry(data_dir)
    registry.add_syntax(syntax_path)

    assert built["count"] == 0, "индекс справки построен заново"
    assert [h.doc.id for h in registry.syntax.index.search("СтрНайти")][0] == "method.СтрНайти"


def test_словарь_имён_справки_поднимается_из_кэша(export, monkeypatch):
    """Без него точное совпадение искалось перебором всех элементов."""
    data_dir, _ = export
    syntax_path = write_syntax(data_dir / "index" / "syntax")
    Registry(data_dir).add_syntax(syntax_path)

    built = {"count": 0}
    original = registry_module._build_name_lookup

    def count(*args, **kwargs):
        built["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(registry_module, "_build_name_lookup", count)
    registry = Registry(data_dir)
    registry.add_syntax(syntax_path)

    assert built["count"] == 0, "словарь имён построен заново"
    found = registry.syntax.find_exact("ЗаписьJSON.ЗаписатьНачалоОбъекта")
    assert [item.id for item in found] == ["property.ЗаписьJSON.ЗаписатьНачалоОбъекта"]


def test_удаление_источника_уносит_его_кэш(export):
    data_dir, zip_path = export
    registry = Registry(data_dir)
    registry.add_configuration(zip_path)

    registry.remove("ТестоваяКонфигурация")

    assert list((data_dir / "index" / "cache").iterdir()) == []


def test_старт_убирает_кэш_забытых_источников(export):
    data_dir, zip_path = export
    registry = Registry(data_dir)
    registry.add_configuration(zip_path)
    registry.save()
    мусор = index_cache.path_for(data_dir / "index" / "cache", "КонфигурацияКоторойНет", "objects")
    мусор.write_bytes(b"lost")

    Registry(data_dir).startup()

    assert not мусор.exists()
    assert index_cache.path_for(data_dir / "index" / "cache", "ТестоваяКонфигурация", "objects").exists()
