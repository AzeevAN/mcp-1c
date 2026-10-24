"""Регресс M12: один разбор модуля без изменения четырёх индексов.

Эталон строится отдельными существующими построителями, без нового
совместного прохода Registry. Сравниваются тела подписанного кэша:
служебные штампы и сжатие не относятся к содержимому индексов.
"""

from collections import Counter
import sys
import weakref

from mcp1c import bsl_lex, index_cache, modules_index
from mcp1c.registry import Registry


_ВИДЫ_КЭША = (
    "modules-toc",
    "modules-calls",
    "modules-forms",
    "modules-search",
)


def _прочитать_состояния(каталог, источник):
    сигнатура = f"{источник.sha256}:selection={источник.selection_version}"
    состояния = {}
    for вид in _ВИДЫ_КЭША:
        состояние = index_cache.load_blob(
            index_cache.path_for(каталог, источник.id, вид),
            source_sha256=сигнатура,
            kind=вид,
        )
        assert isinstance(состояние, dict), f"Не записан кэш {вид}"
        состояния[вид] = состояние
    return состояния


def test_холодный_registry_разбирает_каждый_модуль_один_раз_без_потерь(
    tmp_path, корень_кода, реестр_из_кода, monkeypatch
):
    # В исходной фикстуре внутренний метод вызывает экспортный, а объектный
    # модуль — тот же экспортный метод с квалификатором. Дополняем обратным
    # локальным вызовом: потеря неэкспортных целей теперь тоже наблюдаема.
    общий = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    общий.write_text(
        общий.read_text(encoding="utf-8")
        + "\nПроцедура ВызватьВнутреннюю() Экспорт\n"
        + "\tВнутренняя();\n"
        + "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)
    загружено = реестр.resolve("Пример").modules
    assert загружено is not None and загружено.каталог is not None
    источник = загружено.source
    корень = загружено.корень
    каталог = загружено.каталог

    # Native-каталог важен и для форм: дерево исходной фикстуры не содержит
    # всех дескрипторов, которые создал обычный путь приёма полной выгрузки.
    оглавление = modules_index.Оглавление.построить(корень, каталог=каталог)
    эталон = modules_index.Индексы(
        оглавление=оглавление,
        вызовы=modules_index.Вызовы.построить(
            корень, оглавление, каталог=каталог
        ),
        формы=modules_index.Формы.построить(корень, каталог=каталог),
        поиск=modules_index.построить_поиск(
            оглавление, корень, каталог=каталог
        ),
        каталог=каталог,
    )
    эталонный_кэш = tmp_path / "эталонный-кэш"
    modules_index.сохранить_индексы(
        реестр,
        источник.id,
        эталон,
        source_sha256=источник.sha256,
        selection_version=источник.selection_version,
        cache_dir=эталонный_кэш,
    )
    ожидаемые_состояния = _прочитать_состояния(эталонный_кэш, источник)

    # Явные проверки не дают пустому/обеднённому эталону незаметно пройти
    # сравнение с таким же обеднённым результатом нового пути.
    внутренняя = оглавление.по_имени("Внутренняя")
    assert len(внутренняя) == 1 and not внутренняя[0].экспорт
    assert any(
        место.модуль == место.цель == "ОбщийМодуль.ОбщийПример"
        for место in эталон.вызовы.места("Внутренняя")
    )
    assert {
        (место.модуль, место.цель)
        for место in эталон.вызовы.места("Сложить")
    } == {
        ("ОбщийМодуль.ОбщийПример", "ОбщийМодуль.ОбщийПример"),
        ("Документ.Пример.МодульОбъекта", "ОбщийМодуль.ОбщийПример"),
    }
    assert эталон.формы.обработчик(
        "Справочник.Пример.Форма.ФормаЭлемента", "ПриОткрытии"
    ) == ("OnOpen",)
    assert set(ожидаемые_состояния["modules-search"]["doc_ids"]) == {
        "ОбщийМодуль.ОбщийПример::Сложить",
        "ОбщийМодуль.ОбщийПример::ВызватьВнутреннюю",
    }

    тексты = [
        modules_index.read_bsl(корень, entry.address, entry.locator)
        for entry in каталог.entries.values()
        if entry.locator is not None and entry.locator.kind != "compiled"
    ]
    assert len(тексты) == 3
    assert len(set(тексты)) == len(тексты), "Счётчик различает модули по тексту"

    # Удаляется только расходный кэш синтетического поколения в tmp_path;
    # startup заново проходит штатный native restore, включая все guards.
    for вид in _ВИДЫ_КЭША:
        путь = index_cache.path_for(реестр.cache_dir, источник.id, вид)
        assert путь.is_file()
        путь.unlink()

    оригинал = bsl_lex.разобрать
    разборы = Counter()

    def считать_разбор(текст, *args, **kwargs):
        разборы[текст] += 1
        return оригинал(текст, *args, **kwargs)

    # from-import сохраняет identity функции. Подмена только bsl_lex не
    # заметила бы старые aliases в modules_index; перечисляем все уже
    # загруженные aliases той же функции, не привязываясь к новому helper.
    for имя, модуль in tuple(sys.modules.items()):
        if not имя.startswith("mcp1c.") or модуль is None:
            continue
        for атрибут, значение in tuple(vars(модуль).items()):
            if значение is оригинал:
                monkeypatch.setattr(модуль, атрибут, считать_разбор)

    холодный = Registry(реестр.data_dir)
    assert холодный.startup() == []
    готово = холодный.resolve("Пример").modules
    assert готово is not None
    assert _прочитать_состояния(холодный.cache_dir, источник) == ожидаемые_состояния
    assert разборы == Counter({текст: 1 for текст in тексты}), {
        "всего_разборов": sum(разборы.values()),
        "разборов_по_модулю": sorted(разборы.values()),
    }


def test_лимит_reuse_освобождает_накопленные_вызовы_и_повторяет_разбор(
    tmp_path, monkeypatch
):
    from mcp1c.module_content import ModuleLocator

    тексты = (
        "// Первая шапка\nПроцедура Первая() Экспорт\nВторая();\nКонецПроцедуры\n",
        "// Вторая шапка\nПроцедура Вторая() Экспорт\n"
        + "\n".join(f"ПлатформенныйМетод{номер}();" for номер in range(500))
        + "\nПервая();\nКонецПроцедуры\n",
    )
    локаторы = []
    for номер, текст in enumerate(тексты):
        путь = tmp_path / f"module-{номер}.bsl"
        путь.write_text(текст, encoding="utf-8")
        локаторы.append(ModuleLocator.file(путь.name))
    оригинал = modules_index.разобрать
    ожидаемые = [
        [вызов for процедура in оригинал(текст) for вызов in процедура.вызовы]
        for текст in тексты
    ]
    разборы = Counter()

    def считать(текст):
        разборы[текст] += 1
        return оригинал(текст)

    monkeypatch.setattr(modules_index, "разобрать", считать)
    monkeypatch.setattr(modules_index, "_COLD_CALL_REUSE_MAX_BYTES", 2048, raising=False)
    with modules_index._ColdParseReuse(tmp_path) as разбор:
        разбор.parse("Первый", локаторы[0])
        массив = weakref.ref(разбор._calls["Первый"])
        разбор.parse("Второй", локаторы[1])
        # Освобождение проверяется до этапа calls, когда и возникал рост.
        assert массив() is None, "Превышение бюджета держит старые массивы вызовов"
        assert not разбор._calls and not разбор._strings and not разбор._string_ids
        assert list(разбор.calls("Первый", локаторы[0])) == ожидаемые[0]
        assert list(разбор.calls("Второй", локаторы[1])) == ожидаемые[1]
        разбор.finish_calls()
        assert разбор.pop_headers("Первый", 1) == [(0, "// Первая шапка")]
        assert разбор.pop_headers("Второй", 1) == [(0, "// Вторая шапка")]
    assert разборы == Counter({текст: 2 for текст in тексты})


def test_переполнение_reuse_не_меняет_четыре_индекса(
    корень_кода, monkeypatch
):
    каталог = modules_index._единый_каталог(корень_кода, None)
    оглавление = modules_index.Оглавление.построить(корень_кода, каталог=каталог)
    вызовы = modules_index.Вызовы.построить(корень_кода, оглавление, каталог=каталог)
    формы = modules_index.Формы.построить(корень_кода, каталог=каталог)
    поиск = modules_index.построить_поиск(оглавление, корень_кода, каталог=каталог)
    monkeypatch.setattr(modules_index, "_COLD_CALL_REUSE_MAX_BYTES", 0, raising=False)

    итог = Registry._построить_индекс_кода("Тест", корень_кода, каталог=каталог)

    assert итог.оглавление._состояние() == оглавление._состояние()
    assert итог.вызовы._состояние() == вызовы._состояние()
    assert итог.формы._состояние() == формы._состояние()
    assert итог.поиск.export_state() == поиск.export_state()


def test_quota_останавливает_копирование_текущего_большого_модуля(
    tmp_path, monkeypatch
):
    from array import array
    from mcp1c.module_content import ModuleLocator

    скопировано = 0

    class НаблюдаемыйМассив(array):
        def append(self, value):
            nonlocal скопировано
            скопировано += 1
            super().append(value)

        def extend(self, values):
            nonlocal скопировано
            values = tuple(values)
            скопировано += len(values)
            super().extend(values)

    текст = (
        "Процедура Большая()\n"
        + "\n".join(f"ПлатформенныйМетод{номер}();" for номер in range(500))
        + "\nКонецПроцедуры\n"
    )
    (tmp_path / "large.bsl").write_text(текст, encoding="utf-8")
    monkeypatch.setattr(modules_index, "array", НаблюдаемыйМассив)
    monkeypatch.setattr(modules_index, "_COLD_CALL_REUSE_MAX_BYTES", 2048, raising=False)
    with modules_index._ColdParseReuse(tmp_path) as разбор:
        разбор.parse("Большой", ModuleLocator.file("large.bsl"))
        assert 0 < скопировано < 500 * 3, (
            "Reuse сначала скопировал вызовы всего модуля и лишь затем проверил бюджет"
        )
