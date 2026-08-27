"""Ответы MCP-инструментов знают про язык запросов.

`search_syntax`/`get_syntax` находят элементы обоих доменов через
`registry.py` и `LoadedSyntax`. Но
текст ответов — снимок понятий «до языка запросов»: отказ без справки
говорит только про `shcntx_ru.hbk`, статья по языку запросов не урезается в
поиске, а `list_configurations` источник вовсе не упоминает. Эти пробелы и
проверяет файл.
"""

from __future__ import annotations

from mcp1c.registry import KIND_SYNTAX, Registry, RegistryError
from mcp1c.store import save_syntax
from mcp1c.syntax_model import (
    KIND_QUERY_ARTICLE,
    KIND_QUERY_FUNCTION,
    KIND_QUERY_TABLE,
    SyntaxIndex,
    SyntaxItem,
    SyntaxVariant,
)
from mcp1c.tools import SNIPPET_CHARS, get_syntax, list_configurations, search_syntax

from conftest import (
    build_configuration,
    query_hbk_stub,
    write_export,
    write_syntax,
)


def _реестр_с_языком_и_платформой(tmp_path):
    """Все три источника разом: язык запросов, справка платформы, конфигурация."""
    incoming = tmp_path / "incoming"
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(incoming))
    registry.add_syntax(write_syntax(incoming, platform="8.3.27.2130"))
    config = build_configuration()
    config.platform = "8.3.27.2130"
    registry.add_configuration(write_export(incoming, config))
    return registry


# --------------------------------------------------------------- без справки платформы


def test_сервер_отвечает_когда_загружен_только_язык_запросов(tmp_path):
    """Источник подан как самостоятельный: требовать рядом 40 МБ справки нельзя."""
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))

    ответ = search_syntax(registry, "соединение")

    assert "Левое внешнее соединение" in ответ
    assert "Справка платформы не подключена" not in ответ


def test_конфигурация_и_язык_запросов_без_справки_платформы_не_падают(tmp_path):
    """Замер 2026-08-17: `facts_for` вызывается и для элементов языка запросов,
    а на пустом наборе платформ (`self.syntax.syntax.platforms == []`) падает
    `AttributeError('signature')` — сервер не отвечает вовсе там, где по
    публичному контракту обязан работать без справки платформы. Найдено при
    проверке независимого источника языка запросов."""
    incoming = tmp_path / "incoming"
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(incoming))
    config = build_configuration()
    registry.add_configuration(write_export(incoming, config))

    выдача = search_syntax(registry, "соединение")
    карточка = get_syntax(registry, "КонецПериода")

    assert "Левое внешнее соединение" in выдача
    assert "КОНЕЦПЕРИОДА" in карточка


def test_отказ_называет_оба_файла_когда_не_подключено_ничего(tmp_path):
    """Конфигурация загружена, ни справки платформы, ни языка запросов нет —
    единственный случай, когда инструменты синтаксиса вправе отказать."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    config = build_configuration()
    registry.add_configuration(write_export(incoming, config))

    try:
        search_syntax(registry, "что угодно")
    except RegistryError as ошибка:
        текст = str(ошибка)
    else:
        raise AssertionError("ожидался RegistryError")

    assert "shcntx_ru.hbk" in текст
    assert "shquery_ru.hbk" in текст


def test_заголовок_поиска_не_путает_пустую_платформу_со_справкой(tmp_path):
    """Замер на реальном `shquery_ru.hbk`: подзаголовок печатал «Справка ,
    конфигурация не выбрана» — пустое имя платформы вместо честного
    «источник — язык запросов»."""
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))

    ответ = search_syntax(registry, "соединение")

    assert "Справка , " not in ответ
    assert "язык запросов" in ответ.lower()


def test_известная_версия_функции_запроса_фильтруется_по_конфигурации(tmp_path):
    """Один элемент из 8.3.20 недоступен на 8.3.5 и доступен на 8.3.23.

    Это наблюдаемый контракт, из-за которого постоянный MCP-текст не вправе
    говорить, что язык запросов фильтром не затрагивается.
    """
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    index = SyntaxIndex(platforms=[], source="query-version-test", language="ru")
    index.add(
        SyntaxItem(
            id="query/StrFind",
            kind=KIND_QUERY_FUNCTION,
            name_ru="СтрНайти",
            name_en="StrFind",
            description="Ищет подстроку в строке запроса",
            since="8.3.20",
        )
    )
    registry = Registry(tmp_path / "data")
    registry.add_syntax(save_syntax(index, incoming / "query-ru.json.gz"))
    for имя, платформа in (
        ("СтараяКонфигурация", "8.3.5.1570"),
        ("НоваяКонфигурация", "8.3.23.1997"),
    ):
        config = build_configuration(name=имя)
        config.platform = платформа
        registry.add_configuration(write_export(incoming, config))

    старая = search_syntax(registry, "СтрНайти", "СтараяКонфигурация")
    новая = search_syntax(registry, "СтрНайти", "НоваяКонфигурация")

    assert "доступного ничего нет" in старая
    assert "`Запрос.СтрНайти` — с 8.3.20" in старая
    assert "доступного ничего нет" not in новая
    assert "- `Запрос.СтрНайти`" in новая
    assert "с 8.3.20" in новая


def _реестр_с_одноимёнными(tmp_path):
    """Одно имя в двух доменах: метод платформы и функция языка запросов.

    Так на живом сервере выглядит `СтрНайти` — он есть и в платформе (с 8.3.6),
    и в языке запросов.
    """
    from mcp1c.store import save_syntax
    from mcp1c.syntax_model import KIND_QUERY_FUNCTION, SyntaxIndex, SyntaxItem

    incoming = tmp_path / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)

    язык = SyntaxIndex(platforms=[], source="query-test", language="ru")
    язык.add(
        SyntaxItem(
            id="query/StrFind",
            kind=KIND_QUERY_FUNCTION,
            name_ru="СтрНайти",
            name_en="StrFind",
            description="Ищет подстроку в строке запроса",
        )
    )
    платформа = SyntaxIndex(platforms=["8.3.27.2130"], source="platform-test")
    платформа.add(
        SyntaxItem(
            id="objects/Глобальный контекст/methods/СтрНайти",
            kind="method",
            name_ru="СтрНайти",
            parent_ru="Глобальный контекст",
            description="Ищет подстроку",
            since="8.3.6",
        )
    )

    registry = Registry(tmp_path / "data")
    registry.add_syntax(save_syntax(язык, incoming / "query-ru.json.gz"))
    registry.add_syntax(save_syntax(платформа, incoming / "8.3.27.2130.json.gz"))
    return registry


def test_одноимённые_показывают_как_достать_каждого(tmp_path):
    """Подсказка «уточните в виде `Объект.Член`» вела в тупик.

    У элемента языка запросов владельца нет, и достать его по имени было
    нечем: `Функция запроса.СтрНайти` не находится, `СтрНайти` возвращает
    список одноимённых снова. Найдено живой проверкой 2026-08-19 на полном
    наборе источников.
    """
    ответ = get_syntax(_реестр_с_одноимёнными(tmp_path), "СтрНайти")

    assert "Глобальный контекст.СтрНайти" in ответ
    assert "Запрос.СтрНайти" in ответ


def _реестр_8_3_5_с_одноимённой(tmp_path):
    """Конфигурация 8.3.5, где платформенный элемент недоступен, а одноимённый
    из языка запросов доступен: у этого элемента нет курируемой границы."""
    registry = _реестр_с_одноимёнными(tmp_path)
    config = build_configuration()
    config.platform = "8.3.5.1570"
    каталог = tmp_path / "конфигурация"
    каталог.mkdir(parents=True, exist_ok=True)
    registry.add_configuration(write_export(каталог, config))
    return registry


def test_отсечённый_однофамилец_не_пропадает_молча(tmp_path):
    """Фильтр версии убрал платформенный элемент, а одноимённый из языка
    запросов остался — и ветка «недоступен, вот замена» не срабатывала вовсе.

    Живой промах 2026-08-19: на конфигурации 8.3.5 запрос `СтрНайти` отдавал
    карточку функции языка запросов и ни слова о том, что платформенная
    `СтрНайти` появилась только в 8.3.6. Агент пишет её в модуль, и код не
    компилируется. Замер на живом реестре: таких имён 419 на 8.3.5, 124 на
    8.3.23, 101 на 8.3.27.
    """
    ответ = get_syntax(_реестр_8_3_5_с_одноимённой(tmp_path), "СтрНайти",
                       "ТестоваяКонфигурация")

    assert "Функция запроса" in ответ, "карточка доступного элемента остаётся"
    assert "Глобальный контекст.СтрНайти" in ответ, "об отсечённом надо сказать"
    assert "8.3.6" in ответ, "названа причина: элемент появился позже"
    assert "Найти" in ответ, "замена из таблицы приложена"


def test_рецепт_замены_не_переезжает_в_язык_запросов(tmp_path):
    """Таблица замен написана про платформу: вместо `СтрНайти` — `Найти`.

    У функции запроса имя то же, а `Найти` — метод глобального контекста, в
    тексте запроса его не написать. Подставить этот рецепт функции языка
    запросов значит выдать невыполнимый совет за проверенный.
    """
    from mcp1c.store import save_syntax
    from mcp1c.syntax_model import KIND_QUERY_FUNCTION, SyntaxIndex, SyntaxItem

    incoming = tmp_path / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    язык = SyntaxIndex(platforms=[], source="query-test", language="ru")
    язык.add(
        SyntaxItem(
            id="query/StrFind",
            kind=KIND_QUERY_FUNCTION,
            name_ru="СтрНайти",
            name_en="StrFind",
            description="Ищет подстроку в строке запроса",
            since="8.3.20",
        )
    )
    registry = Registry(tmp_path / "data")
    registry.add_syntax(save_syntax(язык, incoming / "query-ru.json.gz"))
    config = build_configuration()
    config.platform = "8.3.5.1570"
    registry.add_configuration(write_export(incoming, config))

    ответ = get_syntax(registry, "СтрНайти", "ТестоваяКонфигурация")

    assert "8.3.20" in ответ, "версия появления названа"
    assert "Чем заменить" not in ответ, "рецепт из таблицы платформы сюда не подходит"


def test_квалификатор_запроса_отдаёт_элемент_языка_запросов(tmp_path):
    ответ = get_syntax(_реестр_с_одноимёнными(tmp_path), "Запрос.СтрНайти")

    assert "Одноимённых" not in ответ
    assert "Ищет подстроку в строке запроса" in ответ


def test_без_единой_справки_не_говорится_про_соседние_версии(tmp_path):
    """Раздел «Справки платформы» писал «Ответы по ней собраны из соседних
    версий» и при нуле загруженных справок — соседних нет, ответов по
    платформе нет вовсе. Найдено живой проверкой 2026-08-19 на состоянии
    «язык запросов плюс одна конфигурация»."""
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.add_configuration(write_export(tmp_path / "incoming", build_configuration()))

    ответ = list_configurations(registry)

    assert "соседних версий" not in ответ
    assert "shcntx_ru.hbk" in ответ


def test_заголовок_выдачи_называет_загруженный_источник(tmp_path):
    """Строка `# Справка платформы: «…»` печаталась и когда платформы нет.

    Найдено живой проверкой 2026-08-19: загружен только `shquery_ru.hbk`, а
    выдача поиска представлялась справкой платформы — при том что второй
    строкой честно писала «Только язык запросов».
    """
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))

    ответ = search_syntax(registry, "соединение")

    assert not ответ.startswith("# Справка платформы")
    assert ответ.splitlines()[0].lower().startswith("# язык запросов")


def test_оговорка_не_путает_отсутствие_платформы_с_неопределённой_версией(tmp_path):
    """Прежний текст оговорки говорил «версия справки платформы не
    определена» — неверно: справки платформы нет вовсе, версия ни при чём.
    Тест различает отсутствие источника и неизвестную версию."""
    incoming = tmp_path / "incoming"
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(incoming))
    config = build_configuration()
    registry.add_configuration(write_export(incoming, config))

    ответ = search_syntax(registry, "соединение")

    assert "версия справки платформы не определена" not in ответ.lower()
    assert "shcntx_ru.hbk" in ответ


# --------------------------------------------------------------- query_table/query_field — другой домен


def test_query_table_версионируется_как_платформа(tmp_path):
    """Регрессия: `_is_query_kind` по префиксу `query_` ловил и
    `query_table`/`query_field` — страницы `tables/` из самой справки
    платформы (`syntax_parser.py`), не из `shquery_ru.hbk`. У них, в отличие
    от трёх видов нового источника, версия есть, и `facts_for` обязана
    считать её по версии конфигурации — иначе агент молча получает сигнатуру
    самой свежей справки вместо своей версии."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    старая = SyntaxIndex(platforms=["8.3.5.1570"], source="test-8.3.5.1570")
    старая.add(
        SyntaxItem(
            id="tables/КритерийОтбора",
            kind=KIND_QUERY_TABLE,
            name_ru="КритерийОтбора",
            name_en="SelectionCriterion",
            description="Таблица критерия отбора",
            variants=[SyntaxVariant(signature="КритерийОтбора.<Имя критерия отбора>")],
        )
    )
    новая = SyntaxIndex(platforms=["8.3.27.2130"], source="test-8.3.27.2130")
    новая.add(
        SyntaxItem(
            id="tables/КритерийОтбора",
            kind=KIND_QUERY_TABLE,
            name_ru="КритерийОтбора",
            name_en="SelectionCriterion",
            description="Таблица критерия отбора",
            variants=[SyntaxVariant(signature="КритерийОтбора.<Имя критерия отбора>.<Доп>")],
        )
    )
    путь_старой = save_syntax(старая, incoming / "8.3.5.1570.json.gz")
    путь_новой = save_syntax(новая, incoming / "8.3.27.2130.json.gz")

    registry = Registry(tmp_path / "data")
    # `known_kind` — не угадывание: единственный элемент в каждом синтетическом
    # файле имеет вид `query_table`, и угадывание по содержимому (все элементы
    # `query_*`) приняло бы файл за источник языка запросов. У настоящей
    # справки платформы `query_table` — один вид среди тысяч остальных, там
    # угадывание работает верно; здесь имя вида важнее полноты файла.
    registry.add_syntax(путь_старой, known_kind=KIND_SYNTAX)
    registry.add_syntax(путь_новой, known_kind=KIND_SYNTAX)

    config = build_configuration()
    config.platform = "8.3.5.1570"
    registry.add_configuration(write_export(incoming, config))

    ответ = get_syntax(registry, "КритерийОтбора")

    assert "КритерийОтбора.<Имя критерия отбора>" in ответ
    assert "<Доп>" not in ответ


# --------------------------------------------------------------- статья: целиком vs урезанно


def test_статья_в_поиске_урезана_а_по_имени_целиком(tmp_path):
    registry = _реестр_с_языком_и_платформой(tmp_path)

    выдача = search_syntax(registry, "итоги по иерархии")
    карточка = get_syntax(registry, "Итоги по иерархии")

    assert "Первый абзац статьи." in выдача
    assert "Продолжение." not in выдача
    assert len(карточка) > len(выдача)
    assert "Продолжение." in карточка


def test_длинный_первый_абзац_обрезается_по_snippet_chars(tmp_path):
    """Первый абзац сам по себе может быть длиннее лимита — самая большая
    настоящая статья весит 24,8 КБ. Обрезка обязана сработать и внутри
    абзаца, не только между абзацами."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    index = SyntaxIndex(platforms=[], source="query-test", language="ru")
    длинный_абзац = "Слово. " * 200  # 1400 символов одним абзацем, без \n\n
    index.add(
        SyntaxItem(
            id="query/long.html",
            kind=KIND_QUERY_ARTICLE,
            name_ru="Длинная статья",
            name_en="long",
            description=длинный_абзац,
        )
    )
    save_syntax(index, incoming / "query-ru.json.gz")
    registry = Registry(tmp_path / "data")
    registry.add_syntax(incoming / "query-ru.json.gz")

    выдача = search_syntax(registry, "Длинная статья")

    assert длинный_абзац not in выдача
    строки = выдача.splitlines()
    номер = next(i for i, строка in enumerate(строки) if "Статья по языку запросов" in строка)
    сниппет = строки[номер + 1]
    # Пустая строка тоже «не длиннее лимита» — проверка длины одна не отличает
    # обрезанный сниппет от отсутствующего вовсе. Обязаны быть и текст, и
    # обрезка ровно по границе (без «…» абзац короче лимита и не обрежется).
    assert сниппет.strip()
    assert сниппет.strip().startswith("Слово.")
    assert сниппет.strip().endswith("…")
    # Запас — на многоточие обрезки и отступ строки, не на второй абзац.
    assert len(сниппет) <= SNIPPET_CHARS + 10


# --------------------------------------------------------------- совпадение имён


def test_одноимённые_из_разных_доменов_разводятся_видом(tmp_path):
    """57 имён из 127 совпадают с платформой — подмена домена недопустима."""
    incoming = tmp_path / "incoming"
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(incoming))  # даёт функцию «КОНЕЦПЕРИОДА»

    метод = SyntaxIndex(platforms=["8.3.27.2130"], source="test")
    метод.add(
        SyntaxItem(
            id="global/КонецПериода",
            kind="method",
            name_ru="КонецПериода",
            name_en="EndOfPeriod",
            description="Возвращает дату конца периода",
        )
    )
    registry.add_syntax(save_syntax(метод, incoming / "8.3.27.2130.json.gz"))

    config = build_configuration()
    config.platform = "8.3.27.2130"
    registry.add_configuration(write_export(incoming, config))

    ответ = get_syntax(registry, "КонецПериода")

    # Строка целиком, а не раздельные подстроки «Метод»/«Функция запроса»:
    # раздельные проверки проходят и если подписи у кандидатов перепутать
    # местами — раздельные проверки не поймали бы такую перестановку.
    assert "- `КонецПериода` — Метод" in ответ
    # Адрес элемента языка запросов печатается с квалификатором `Запрос.` —
    # иначе его нечем назвать при повторном вызове.
    assert "- `Запрос.КОНЕЦПЕРИОДА` — Функция запроса" in ответ


# --------------------------------------------------------------- обзор


def test_обзор_называет_язык_запросов_подключённым(tmp_path):
    registry = _реестр_с_языком_и_платформой(tmp_path)

    ответ = list_configurations(registry)

    assert "Язык запросов: подключён" in ответ


def test_обзор_называет_язык_запросов_не_подключённым(tmp_path):
    incoming = tmp_path / "incoming"
    registry = Registry(tmp_path / "data")
    registry.add_syntax(write_syntax(incoming, platform="8.3.27.2130"))
    config = build_configuration()
    config.platform = "8.3.27.2130"
    registry.add_configuration(write_export(incoming, config))

    ответ = list_configurations(registry)

    assert "Язык запросов: не подключён" in ответ


def test_обзор_без_конфигураций_не_врёт_про_язык_запросов(tmp_path):
    """Замер: без конфигураций ветка `list_configurations` считала страницы
    языка запросов по слитой справке платформы (пустой в этом сценарии) и
    печатала `**` — пустое имя платформы — и «0 элементов». Оба числа лживы:
    язык запросов загружен и отвечает на вопросы."""
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))

    ответ = list_configurations(registry)

    assert "0 элементов" not in ответ
    assert "****" not in ответ  # пустое имя платформы между парой `**`
    assert "язык запросов" in ответ.lower()
