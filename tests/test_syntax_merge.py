"""Слияние справок разных версий платформы в один индекс.

Проверяется наблюдаемое поведение слияния: какие элементы попали в индекс,
какие границы версий у них проставлены и какие факты сохранились от старых
справок. Настоящие `.hbk` для этого не нужны — справки собираются из
`SyntaxItem` прямо здесь.
"""

from __future__ import annotations

import pytest

from mcp1c.syntax_merge import merge_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem, SyntaxVariant


def build(platform: str, *items: SyntaxItem) -> SyntaxIndex:
    index = SyntaxIndex(platforms=[platform], source=f"test-{platform}")
    for item in items:
        index.add(item)
    return index


def method(name: str, parent: str = "", **kwargs) -> SyntaxItem:
    return SyntaxItem(
        id=f"{parent}.{name}", kind="method", name_ru=name, parent_ru=parent, **kwargs
    )


def find(index: SyntaxIndex, name: str) -> SyntaxItem:
    for item in index.items.values():
        if item.name_ru == name:
            return item
    raise AssertionError(f"в слитом индексе нет элемента {name}")


def signed(name: str, parent: str, signature: str, **kwargs) -> SyntaxItem:
    item = method(name, parent, **kwargs)
    item.variants = [SyntaxVariant(signature=signature)]
    return item


def test_элемент_пропавший_из_свежей_справки_получает_until():
    old = build("8.3.5", method("ЗагрузитьИзФайла", "ПреобразованиеXSL"))
    new = build("8.3.27", method("Найти", "Глобальный контекст"))

    merged = merge_syntax([old, new])

    assert find(merged, "ЗагрузитьИзФайла").until == "8.3.5"


def test_сигнатура_старой_справки_сохраняется_как_версионный_факт():
    old = build("8.3.5", signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(ИмяФайлаXML, ТипКодировки)"))
    new = build(
        "8.3.27",
        signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(ИмяФайлаXML, ТипКодировки, ДобавлятьBOM)"),
    )

    item = find(merge_syntax([old, new]), "ОткрытьФайл")

    assert [(f.platform, f.signature) for f in item.older] == [
        ("8.3.5", "ОткрытьФайл(ИмяФайлаXML, ТипКодировки)")
    ]


def test_доступность_старой_справки_сохраняется_как_версионный_факт():
    old = build(
        "8.3.5",
        method("ЗаписатьXML", "Глобальный контекст", availability=["Сервер", "ТолстыйКлиент"]),
    )
    new = build(
        "8.3.27",
        method(
            "ЗаписатьXML",
            "Глобальный контекст",
            availability=["Сервер", "ТолстыйКлиент", "ТонкийКлиент"],
        ),
    )

    item = find(merge_syntax([old, new]), "ЗаписатьXML")

    assert [(f.platform, f.availability) for f in item.older] == [
        ("8.3.5", ["Сервер", "ТолстыйКлиент"])
    ]


def test_граница_берётся_по_самой_свежей_справке_где_элемент_есть():
    старая = build("8.3.5", method("Закрыть", "КаноническаяЗаписьXML"))
    средняя = build("8.3.19", method("Закрыть", "КаноническаяЗаписьXML"))
    свежая = build("8.3.27", method("Найти", "Глобальный контекст"))

    item = find(merge_syntax([старая, средняя, свежая]), "Закрыть")

    assert item.until == "8.3.19"


def test_переименованный_элемент_опознаётся_по_английскому_имени():
    """1С переименовывает и правит опечатки: `Жирный` стал `Полужирным`.

    Английское имя при этом осталось `Bold` — значит элемент тот же, и в
    индексе он должен быть один, со старым именем в версионных фактах.
    """
    old = build("8.3.5", SyntaxItem(id="o", kind="property", name_ru="Жирный", name_en="Bold", parent_ru="Шрифт", parent_en="Font"))
    new = build("8.3.27", SyntaxItem(id="n", kind="property", name_ru="Полужирный", name_en="Bold", parent_ru="Шрифт", parent_en="Font"))

    merged = merge_syntax([old, new])

    assert len(merged) == 1
    item = find(merged, "Полужирный")
    assert [(f.platform, f.name_ru) for f in item.older] == [("8.3.5", "Жирный")]
    # У свойства сигнатуры нет: `signature()` отдаёт имя, и оно не должно
    # попасть в факты как сигнатура — иначе агент увидит «раньше вызывалось
    # Жирный()» там, где вызова не было вовсе.
    assert item.older[0].signature == ""


def test_перенос_раздела_справки_не_выглядит_удалением():
    """`Встроенные функции языка` в 8.3.27 нет — 68 функций переехали в
    `Глобальный контекст`. Без таблицы переносов они выглядят удалёнными."""
    old = build("8.3.5", method("Сред", "Встроенные функции языка"))
    new = build("8.3.27", method("Сред", "Глобальный контекст"))

    merged = merge_syntax([old, new])

    assert len(merged) == 1
    assert find(merged, "Сред").until == ""


def test_граница_until_отсекает_элемент_на_новой_платформе():
    """`КаноническаяЗаписьXML` работает на 8.3.5 и отсутствует в 8.3.27.

    Фильтр обязан работать в обе стороны: `since` прячет то, чего ещё нет,
    `until` — то, чего уже нет.
    """
    old = build("8.3.5", method("Закрыть", "КаноническаяЗаписьXML"))
    new = build("8.3.27", method("Найти", "Глобальный контекст"))

    item = find(merge_syntax([old, new]), "Закрыть")

    assert item.available_in("8.3.5") is True
    assert item.available_in("8.3.27") is False


def test_под_версию_со_своей_справкой_факты_точные():
    old = build("8.3.5", signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(ИмяФайлаXML, ТипКодировки)"))
    new = build(
        "8.3.27",
        signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(ИмяФайлаXML, ТипКодировки, ДобавлятьBOM)"),
    )
    merged = merge_syntax([old, new])
    item = find(merged, "ОткрытьФайл")

    под_старую = merged.facts_for(item, "8.3.5")
    под_новую = merged.facts_for(item, "8.3.27")

    assert под_старую.signature == "ОткрытьФайл(ИмяФайлаXML, ТипКодировки)"
    assert под_старую.exact is True
    assert под_новую.signature == "ОткрытьФайл(ИмяФайлаXML, ТипКодировки, ДобавлятьBOM)"
    assert под_новую.exact is True


def test_под_версию_между_справками_факты_помечены_неточными():
    """Конфигурация 8.3.21, справки 8.3.19 и 8.3.24, сигнатура между ними
    менялась. Выбирать за агента нельзя — отдаём оба состояния."""
    средняя = build("8.3.19", signed("ПодписатьАсинх", "МенеджерКриптографии", "ПодписатьАсинх(Данные, Сертификат)"))
    свежая = build("8.3.24", signed("ПодписатьАсинх", "МенеджерКриптографии", "ПодписатьАсинх(Данные, Сертификат, ТипПодписи)"))
    merged = merge_syntax([средняя, свежая])
    item = find(merged, "ПодписатьАсинх")

    resolution = merged.facts_for(item, "8.3.21")

    assert resolution.exact is False
    assert [(f.platform, f.signature) for f in resolution.alternatives] == [
        ("8.3.19", "ПодписатьАсинх(Данные, Сертификат)"),
        ("8.3.24", "ПодписатьАсинх(Данные, Сертификат, ТипПодписи)"),
    ]


def test_под_версию_между_справками_без_расхождений_факты_точные():
    """Если соседние справки говорят одно и то же, меняться между ними было
    нечему — пометка «неточно» тут только приучала бы её пролистывать."""
    средняя = build("8.3.19", signed("Найти", "Глобальный контекст", "Найти(Строка, Подстрока)"))
    свежая = build("8.3.24", signed("Найти", "Глобальный контекст", "Найти(Строка, Подстрока)"))
    merged = merge_syntax([средняя, свежая])
    item = find(merged, "Найти")

    resolution = merged.facts_for(item, "8.3.21")

    assert resolution.exact is True
    assert resolution.signature == "Найти(Строка, Подстрока)"


def test_одноимённые_элементы_старой_справки_не_теряются():
    """В справке 8.3.5 176 ключей неоднозначны — это поля таблиц запросов
    (`Регистратор`, `Ссылка`, `НомерСтроки`) без владельца. Схлопывать их по
    имени нельзя: каждая страница описывает своё поле."""
    old = build(
        "8.3.5",
        SyntaxItem(id="tables/РегистрНакопления/fields/Регистратор", kind="query_field", name_ru="Регистратор"),
        SyntaxItem(id="tables/РегистрСведений/fields/Регистратор", kind="query_field", name_ru="Регистратор"),
    )
    new = build("8.3.27", method("Найти", "Глобальный контекст"))

    merged = merge_syntax([old, new])

    assert len(merged) == 3


def test_старый_элемент_не_затирает_базовый_при_совпадении_идентификатора():
    """Путь страницы между версиями совпадает лишь у 70,9% элементов, и на
    одном и том же пути может оказаться другой элемент. Складывать их в индекс
    по этому пути — значит потерять описание из свежей справки."""
    old = build("8.3.5", SyntaxItem(id="objects/catalog1/Item", kind="object", name_ru="ОписаниеОповещения", name_en="NotifyDescription"))
    new = build("8.3.27", SyntaxItem(id="objects/catalog1/Item", kind="object", name_ru="СтандартнаяКоманда", name_en="StandardCommand"))

    merged = merge_syntax([old, new])

    assert len(merged) == 2
    assert find(merged, "СтандартнаяКоманда").until == ""
    assert find(merged, "ОписаниеОповещения").until == "8.3.5"


def test_одноимённые_элементы_сопоставляются_по_пути_страницы():
    """Полей `<Имя измерения>` в справке десятки, имя у них одно на всех.

    По имени их не различить, но путь страницы у одного и того же поля между
    версиями совпадает — иначе каждая старая справка добавляет свою копию, и
    индекс пухнет на тысячи призраков с ложной границей `until`.
    """

    def поле(path: str) -> SyntaxItem:
        return SyntaxItem(id=path, kind="query_field", name_ru="<Имя измерения>")

    old = build(
        "8.3.5",
        поле("tables/РегистрНакопленияОбороты/fields/Измерение"),
        поле("tables/РегистрСведенийСрезПоследних/fields/Измерение"),
    )
    new = build(
        "8.3.27",
        поле("tables/РегистрНакопленияОбороты/fields/Измерение"),
        поле("tables/РегистрСведенийСрезПоследних/fields/Измерение"),
    )

    merged = merge_syntax([old, new])

    assert len(merged) == 2
    assert [item.until for item in merged.items.values()] == ["", ""]


def test_справка_сборки_покрывает_конфигурацию_того_же_релиза():
    """Справка приходит сборкой (8.3.5.1570), конфигурация живёт на своей
    (8.3.5.1234). Это один релиз платформы, и факты у него общие."""
    old = build("8.3.5.1570", signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(ИмяФайлаXML)"))
    new = build("8.3.27.2130", signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(ИмяФайлаXML, ДобавлятьBOM)"))
    merged = merge_syntax([old, new])
    item = find(merged, "ОткрытьФайл")

    resolution = merged.facts_for(item, "8.3.5.1234")

    assert resolution.exact is True
    assert resolution.signature == "ОткрытьФайл(ИмяФайлаXML)"


def test_слияние_не_портит_исходные_справки():
    """Индексы версий живут своей жизнью — реестр держит их и пересобирает
    слитый вид при каждой загрузке. Если слияние правит их на месте, второй
    вызов удвоит факты, а разобранная справка перестанет быть собой."""
    old = build("8.3.5", signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(ИмяФайлаXML)"))
    new = build("8.3.27", signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(ИмяФайлаXML, ДобавлятьBOM)"))

    first = merge_syntax([old, new])
    second = merge_syntax([old, new])

    assert len(find(first, "ОткрытьФайл").older) == 1
    assert len(find(second, "ОткрытьФайл").older) == 1
    assert find(new, "ОткрытьФайл").older == []


def test_под_версию_где_элемента_нет_факты_не_выдаются():
    """`КаноническаяЗаписьXML` живёт до 8.3.5. Ответ «вот сигнатура, точно» для
    8.3.27 — это ошибка компиляции у агента: элемента там нет вовсе."""
    old = build("8.3.5", signed("Закрыть", "КаноническаяЗаписьXML", "Закрыть()"))
    new = build("8.3.27", method("Найти", "Глобальный контекст"))
    merged = merge_syntax([old, new])
    item = find(merged, "Закрыть")

    resolution = merged.facts_for(item, "8.3.27")

    assert resolution.available is False
    assert resolution.signature == ""


def test_прежнее_имя_отдаётся_вместе_с_фактами_версии():
    """Под 8.3.5 свойство называлось `Жирный`. Отдать факты этой версии, но имя
    оставить нынешнее — значит подсказать агенту несуществующее имя."""
    old = build("8.3.5", SyntaxItem(id="o", kind="property", name_ru="Жирный", name_en="Bold", parent_ru="Шрифт", parent_en="Font"))
    new = build("8.3.27", SyntaxItem(id="n", kind="property", name_ru="Полужирный", name_en="Bold", parent_ru="Шрифт", parent_en="Font"))
    merged = merge_syntax([old, new])
    item = find(merged, "Полужирный")

    assert merged.facts_for(item, "8.3.5").name_ru == "Жирный"
    assert merged.facts_for(item, "8.3.27").name_ru == "Полужирный"


def test_переименование_между_справками_делает_промежуточную_версию_неточной():
    old = build("8.3.19", SyntaxItem(id="o", kind="property", name_ru="Жирный", name_en="Bold", parent_ru="Шрифт", parent_en="Font"))
    new = build("8.3.24", SyntaxItem(id="n", kind="property", name_ru="Полужирный", name_en="Bold", parent_ru="Шрифт", parent_en="Font"))
    merged = merge_syntax([old, new])
    item = find(merged, "Полужирный")

    assert merged.facts_for(item, "8.3.21").exact is False


def test_справка_без_версии_к_слиянию_не_принимается():
    """Пустая версия попала бы в `until` и означала бы «элемент актуален» —
    ровно противоположное тому, что есть на самом деле."""
    без_версии = SyntaxIndex(source="test")
    без_версии.add(method("СтароеИмя", "Глобальный контекст"))
    new = build("8.3.27", method("Найти", "Глобальный контекст"))

    with pytest.raises(ValueError, match="версия"):
        merge_syntax([без_версии, new])


def test_слияние_пустого_списка_не_принимается():
    with pytest.raises(ValueError, match="справк"):
        merge_syntax([])


def test_слияние_по_одной_справке_сохраняет_все_версии():
    """Справки можно сливать по одной, освобождая разобранную сразу после —
    иначе в памяти окажутся все сразу. Список версий при этом обязан
    накапливаться: по нему решается, есть ли справка нужного релиза.
    """
    старая = build("8.3.5", method("Найти", "Глобальный контекст"))
    средняя = build("8.3.19", method("Найти", "Глобальный контекст"))
    свежая = build("8.3.27", method("Найти", "Глобальный контекст"))

    попарно = merge_syntax([старая, merge_syntax([средняя, свежая])])

    assert попарно.platforms == ["8.3.5", "8.3.19", "8.3.27"]
    assert попарно.has_help_for("8.3.19") is True


def test_слияние_по_одной_равно_слиянию_разом():
    """Реестр сливает справки по одной, чтобы не держать их все в памяти.
    Результат обязан совпадать с слиянием разом — иначе экономия памяти
    меняет ответы."""

    def набор():
        старая = build(
            "8.3.5",
            signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(А)"),
            method("Закрыть", "КаноническаяЗаписьXML"),
        )
        средняя = build(
            "8.3.19",
            signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(А, Б)"),
            method("ТолькоВСередине", "ЗаписьXML"),
        )
        свежая = build("8.3.27", signed("ОткрытьФайл", "ЗаписьXML", "ОткрытьФайл(А, Б, В)"))
        return старая, средняя, свежая

    старая, средняя, свежая = набор()
    разом = merge_syntax([старая, средняя, свежая])
    старая, средняя, свежая = набор()
    попарно = merge_syntax([старая, merge_syntax([средняя, свежая])])

    def снимок(index):
        return sorted(
            (
                item.full_ru,
                item.since,
                item.until,
                tuple((f.platform, f.signature, f.name_ru) for f in item.older),
            )
            for item in index.items.values()
        )

    assert снимок(попарно) == снимок(разом)
    assert попарно.platforms == разом.platforms


def test_совпадающий_элемент_версионных_фактов_не_накапливает():
    old = build("8.3.5", signed("Найти", "Глобальный контекст", "Найти(Строка, Подстрока)"))
    new = build("8.3.27", signed("Найти", "Глобальный контекст", "Найти(Строка, Подстрока)"))

    item = find(merge_syntax([old, new]), "Найти")

    assert item.older == []
    assert item.until == ""
