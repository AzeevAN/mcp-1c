from mcp1c.query_parser import ARTICLE_BYTES, looks_like_query_help, parse_page, parse_pages
from mcp1c.syntax_model import (
    KIND_QUERY_ARTICLE,
    KIND_QUERY_FUNCTION,
    KIND_QUERY_KEYWORD,
)

ФУНКЦИЯ = """<HTML><HEAD></HEAD><BODY>
<H1 class="">Функция КОНЕЦПЕРИОДА</H1>
<P>КОНЕЦПЕРИОДА (&lt;Дата периода&gt;, &lt;Тип периода&gt;)</P>
<BLOCKQUOTE>
<P>&lt;Дата периода&gt; – выражение типа ДАТА, указывающего дату периода;</P>
<P>&lt;Тип периода&gt; - тип периода, одно из: МИНУТА, ЧАС, ДЕНЬ.</P></BLOCKQUOTE>
<P>Функция предназначена для получения конца периода.</P>
</BODY></HTML>""".encode("utf-8")


def test_функция_разбирается_с_сигнатурой_и_параметрами():
    index = parse_pages({"ENDOFPERIOD": ФУНКЦИЯ})
    item = index.items["query/ENDOFPERIOD"]

    assert item.kind == KIND_QUERY_FUNCTION
    assert item.name_ru == "КОНЕЦПЕРИОДА"
    assert item.name_en == "ENDOFPERIOD"
    assert item.variants[0].signature.startswith("КОНЕЦПЕРИОДА")
    assert [p.name for p in item.variants[0].params] == ["Дата периода", "Тип периода"]
    # Версий у языка запросов нет — иначе фильтр по версии спрячет элемент.
    assert item.since == "" and item.until == ""


def test_описание_функции_не_дублирует_сигнатуру_и_параметры():
    """Абзацы внутри BLOCKQUOTE уже разобраны в variants[0].params — второй раз
    сырым текстом с угловыми скобками в description попадать не должны."""
    index = parse_pages({"ENDOFPERIOD": ФУНКЦИЯ})
    item = index.items["query/ENDOFPERIOD"]

    assert item.description == "Функция предназначена для получения конца периода."


МНОГОСТРОЧНАЯ_СИГНАТУРА = """<HTML><BODY>
<H1 class="">Функция ДОБАВИТЬКДАТЕ</H1>
<P>ДОБАВИТЬКДАТЕ(&lt;Дата&gt;,
&lt;Тип периода&gt;, &lt;Количество периодов&gt;)</P>
<BLOCKQUOTE>
<P>&lt;Дата&gt; – дата, которую нужно изменить;</P>
<P>&lt;Тип периода&gt; – тип периода;</P>
<P>&lt;Количество периодов&gt; – количество периодов, на которое изменяется дата.</P>
</BLOCKQUOTE>
</BODY></HTML>""".encode("utf-8")


МЕСЯЦ = """<HTML><BODY>
<H1 class="">Функция МЕСЯЦ</H1>
<P>Данная функция предназначена для вычисления номера месяца из значения типа
ДАТА. Номер месяца находится в диапазоне 1 – 12.</P>
<P>Параметр функции – это выражение, имеющее тип ДАТА.</P>
</BODY></HTML>""".encode("utf-8")


def test_функция_без_сигнатуры_распознаётся_по_заголовку():
    """У 22 функций из 52 («Функция МЕСЯЦ», «Функция ГОД», агрегатные вроде
    «Агрегатная функция СРЕДНЕЕ») формальной записи ИМЯ(параметры) на странице
    нет вовсе — только проза. Вид определяет заголовок, а не сигнатура: иначе
    эти функции молча теряются."""
    index = parse_pages({"MONTH": МЕСЯЦ})
    item = index.items["query/MONTH"]

    assert item.kind == KIND_QUERY_FUNCTION
    assert item.name_ru == "МЕСЯЦ"
    assert item.variants[0].signature == ""
    assert item.variants[0].params == []


СИНУС = """<HTML><BODY>
<H1>Функция Sin</H1><PRE>Sin (&lt;Число&gt;)</PRE>
<P>Данная функция вычисляет синус числа в радианах.</P>
<BLOCKQUOTE>
<P>&lt;Число&gt; – число, для которого вычисляется синус.</P>
</BLOCKQUOTE>
</BODY></HTML>""".encode("utf-8")


def test_функция_с_сигнатурой_в_pre_разбирается():
    """У 27 функций из 52 (математические и строковые — Sin, ACos, Left, …)
    сигнатура стоит в `<PRE>` сразу после `<H1>`, а не в первом абзаце."""
    index = parse_pages({"Sin": СИНУС})
    item = index.items["query/Sin"]

    assert item.kind == KIND_QUERY_FUNCTION
    assert item.variants[0].signature == "Sin (<Число>)"
    assert [p.name for p in item.variants[0].params] == ["Число"]


def test_многострочная_сигнатура_разбирается_как_функция():
    """Реальные страницы часто переносят длинную сигнатуру на вторую строку
    исходника ради читаемости — `_text()` перенос сохраняет, и без схлопывания
    пробелов перед сопоставлением такая страница молча уезжает в keyword."""
    index = parse_pages({"ADDTODATE": МНОГОСТРОЧНАЯ_СИГНАТУРА})
    item = index.items["query/ADDTODATE"]

    assert item.kind == KIND_QUERY_FUNCTION
    assert item.variants[0].signature.startswith("ДОБАВИТЬКДАТЕ")
    assert [p.name for p in item.variants[0].params] == [
        "Дата",
        "Тип периода",
        "Количество периодов",
    ]


SELECTION_FIELDS = """<HTML><BODY>
<H1 class="">Описание полей выборки</H1>
<P>Общий текст статьи про поля выборки.</P>
<BLOCKQUOTE>
<P>Текст внутри цитаты, который должен остаться в описании.</P>
</BLOCKQUOTE>
</BODY></HTML>""".encode("utf-8")


def test_ключевое_слово_с_blockquote_сохраняет_текст_в_описании():
    """BLOCKQUOTE вырезается только у функций — там он параметры. У прочих
    видов это обычный содержательный текст (например, описание полей
    выборки, замер на реальном файле: 58 нефункций из 127 с непустым
    BLOCKQUOTE, суммарно 32 899 символов), и терять его нельзя."""
    index = parse_pages({"SelectionFieldsList": SELECTION_FIELDS})
    item = index.items["query/SelectionFieldsList"]

    assert item.kind == KIND_QUERY_KEYWORD
    assert "Текст внутри цитаты, который должен остаться в описании." in item.description


СГРУППИРОВАНОПО = """<HTML><BODY>
<H1 class="">Функция СГРУППИРОВАНОПО</H1>
<P>СГРУППИРОВАНОПО (&lt;Выражение&gt;)</P>
<BLOCKQUOTE>
<P>&lt;Выражение&gt; – столбец или выражение, которое содержит столбец.</P>
</BLOCKQUOTE>
<P>Функция предназначена для различения наборов, по которым ведётся группировка.</P>
<H4 class="">Пример:</H4>
<P>Запрос:</P>
<PRE>ВЫБРАТЬ
	Склад,
	СГРУППИРОВАНОПО(Склад)
ИЗ Документ.Продажа</PRE>
</BODY></HTML>""".encode("utf-8")


def test_сигнатура_в_абзаце_не_подменяется_примером_в_pre_ниже_по_странице():
    """Случай реальной страницы `GROUPING`: сигнатура — первый абзац, а
    `<PRE>` ниже по странице — код примера запроса, не сигнатура. Взять
    `<PRE>` безусловно (без сравнения позиций) подменило бы сигнатуру
    примером."""
    index = parse_pages({"GROUPING": СГРУППИРОВАНОПО})
    item = index.items["query/GROUPING"]

    assert item.kind == KIND_QUERY_FUNCTION
    assert item.variants[0].signature == "СГРУППИРОВАНОПО (<Выражение>)"


СРЕДНЕЕ = """<HTML><BODY>
<H1 class="">Агрегатная функция СРЕДНЕЕ</H1>
<P>Функция вычисляет арифметическое среднее всех попавших в выборку значений поля.</P>
</BODY></HTML>""".encode("utf-8")


def test_агрегатная_функция_распознаётся_по_заголовку():
    """Реальная категория, 5 страниц (`AVG`, `SUM`, `MIN`, `MAX`, `COUNT`) —
    заголовок начинается не с «Функция», а с «Агрегатная функция»."""
    index = parse_pages({"AVG": СРЕДНЕЕ})
    item = index.items["query/AVG"]

    assert item.kind == KIND_QUERY_FUNCTION
    assert item.name_ru == "СРЕДНЕЕ"


def test_вид_и_имя_согласованы_по_границе_слова_в_заголовке():
    """`заголовок.startswith("Функция")` не проверяет границу слова, а
    `_RE_ЗАГОЛОВОК` требует пробел после приставки — рассогласование словило
    бы заголовок вида «ФункцияОсобая» (без пробела после приставки) в
    query_function с неочищенным именем. Оба места должны решать одинаково:
    раз имя не очистилось (приставки нет), то и вид — не функция по
    заголовку."""
    страница = (
        "<HTML><BODY><H1>ФункцияОсобая</H1><P>Просто текст, не по форме заголовка.</P></BODY></HTML>"
    ).encode("utf-8")
    index = parse_pages({"WEIRD": страница})
    item = index.items["query/WEIRD"]

    assert item.kind != KIND_QUERY_FUNCTION
    assert item.name_ru == "ФункцияОсобая"


def test_кириллический_текст_считается_по_декодированным_символам_а_не_байтам():
    """Кириллица в UTF-8 занимает два байта на символ. Если порог статьи
    сравнивать с байтами сырой страницы, а не с длиной декодированного текста,
    страница вдвое короче нужного объёма уже становится статьёй."""
    текст = "текст " * 1000  # 6000 символов, но 11000 байт в UTF-8
    страница = (
        "<HTML><BODY><H1>Слово</H1><P>" + текст + "</P></BODY></HTML>"
    ).encode("utf-8")
    assert len(страница) > ARTICLE_BYTES  # по байтам — уже за порогом

    index = parse_pages({"WORD": страница})
    assert index.items["query/WORD"].kind == KIND_QUERY_KEYWORD


def test_present_html_не_становится_статьёй():
    """`present.html` — двуязычная таблица терминов рус/англ (см.
    `docs/query-language-design.md`, раздел «Разбор»), а не статья. Извлечение
    пары терминов из неё намеренно не извлекаются."""
    страница = (
        "<HTML><BODY><H1>Соответствие терминов</H1><P>"
        + "термин " * 3000
        + "</P></BODY></HTML>"
    ).encode("utf-8")
    index = parse_pages({"present.html": страница})

    assert len(index) == 0


def test_параметр_с_длинным_тире_распознаётся():
    страница = (
        "<HTML><BODY><H1>Функция ПОДСТРОКА</H1>"
        "<P>ПОДСТРОКА(&lt;Строка&gt;)</P>"
        "<BLOCKQUOTE><P>&lt;Строка&gt; — исходная строка.</P></BLOCKQUOTE>"
        "</BODY></HTML>"
    ).encode("utf-8")
    index = parse_pages({"SUBSTRING": страница})
    params = index.items["query/SUBSTRING"].variants[0].params

    assert params[0].name == "Строка"
    assert params[0].description == "исходная строка."


def test_страница_без_заголовка_не_становится_элементом():
    страница = "<HTML><BODY><P>Без H1.</P></BODY></HTML>".encode("utf-8")
    assert parse_page("no_title.html", страница) is None


КЛЮЧЕВОЕ = """<HTML><BODY>
<H1 class="">Левое внешнее соединение</H1>
<P><STRONG>ЛЕВОЕ [ВНЕШНЕЕ] СОЕДИНЕНИЕ</STRONG> означает, что в результат надо
включить комбинации записей из обеих таблиц.</P>
</BODY></HTML>""".encode("utf-8")


def test_ключевое_слово_и_статья_различаются_размером():
    длинная = ("<HTML><BODY><H1>Итоги по иерархии</H1><P>"
               + "текст " * 3000 + "</P></BODY></HTML>").encode("utf-8")
    index = parse_pages({"LEFTJOIN": КЛЮЧЕВОЕ, "hierarchical_totals.html": длинная})

    assert index.items["query/LEFTJOIN"].kind == KIND_QUERY_KEYWORD
    assert index.items["query/hierarchical_totals.html"].kind == KIND_QUERY_ARTICLE
    assert "ЛЕВОЕ" in index.items["query/LEFTJOIN"].description


def test_служебные_файлы_пропускаются():
    index = parse_pages({"__categories__": b"{208,...}", "struct_For.st": b"{1,{2,"})

    assert len(index) == 0


def test_справка_языка_запросов_узнаётся_по_секциям():
    assert looks_like_query_help(
        ["__categories__", "SELECTSection", "LEFTJOIN", "root.html"]
    )


def test_соседняя_справка_за_язык_запросов_не_принимается():
    """Форма `shlang_ru.hbk`: `__categories__` есть, страниц-секций нет.

    Замер 2026-08-17: у него 0 страниц с `Section` против 3 у `shquery_ru.hbk`.
    """
    assert not looks_like_query_help(
        ["__categories__", "struct_For.st", "def_Number", "index"]
    )


def test_справка_синтакс_помощника_за_язык_запросов_не_принимается():
    assert not looks_like_query_help(["objects/Массив.html", "objects/catalog.html"])


СЛИТОЕ_ИМЯ = """<HTML><HEAD></HEAD><BODY>
<H1 class="">Функция СтрНайти(StrFind)</H1>
<P>СтрНайти (&lt;Строка&gt;, &lt;ПодстрокаПоиска&gt;)</P>
<P>Функция предназначена для поиска подстроки.</P>
</BODY></HTML>""".encode("utf-8")

ИМЯ_ЧЕРЕЗ_ПРОБЕЛ = """<HTML><HEAD></HEAD><BODY>
<H1 class="">Функция Строка (String)</H1>
<P>Строка (&lt;Значение&gt;)</P>
<P>Функция приводит значение к строке.</P>
</BODY></HTML>""".encode("utf-8")


def test_английское_имя_не_остаётся_внутри_русского():
    """Заголовок страницы «Функция СтрНайти(StrFind)» давал имя целиком.

    Английское имя и так лежит в `name_en` (берётся из имени файла), а внутри
    русского оно ломает точное совпадение: `get_syntax("СтрНайти")` отвечал
    «Точного совпадения нет» и предлагал в подсказках сам `СтрНайти(StrFind)`.
    На реальном `shquery_ru.hbk` так названы 8 функций из 127, среди них
    ходовые `Лев`, `Прав`, `ВРег`, `НРег`, `СтрЗаменить`.
    """
    index = parse_pages({"StrFind": СЛИТОЕ_ИМЯ})
    item = index.items["query/StrFind"]

    assert item.name_ru == "СтрНайти"
    assert item.name_en == "StrFind"


def test_английское_имя_через_пробел_тоже_убирается():
    index = parse_pages({"String": ИМЯ_ЧЕРЕЗ_ПРОБЕЛ})

    assert index.items["query/String"].name_ru == "Строка"


def test_функции_из_релиза_8_3_20_получают_версию():
    """Справка по языку запросов версий не содержит вовсе — проверено на файле:
    ноль упоминаний «8.3.x» и «начиная с версии» на 129 страницах. Но 1С
    публикует список добавленного в «Что нового», и без него сервер показывает
    конфигурации 8.3.5 функции, которых в её платформе нет.

    Таблица курируемая — как таблица замен и таблица переименований.
    """
    index = parse_pages({"StrFind": СЛИТОЕ_ИМЯ})

    assert index.items["query/StrFind"].since == "8.3.20"


def test_старой_функции_версию_не_приписываем():
    """`КОНЕЦПЕРИОДА` в языке запросов была всегда: приписать ей 8.3.20 значит
    спрятать её от всех конфигураций старше — соврать в другую сторону."""
    index = parse_pages({"ENDOFPERIOD": ФУНКЦИЯ})

    assert index.items["query/ENDOFPERIOD"].since == ""


def test_таблица_версий_знает_не_только_8_3_20():
    """`УНИКАЛЬНЫЙИДЕНТИФИКАТОР(UUID)` появился в языке запросов в 8.3.22 —
    отдельным релизом от строковых и математических функций."""
    страница = """<HTML><HEAD></HEAD><BODY>
<H1 class="">Функция УНИКАЛЬНЫЙИДЕНТИФИКАТОР(UUID)</H1>
<P>УНИКАЛЬНЫЙИДЕНТИФИКАТОР()</P>
<P>Возвращает уникальный идентификатор.</P>
</BODY></HTML>""".encode("utf-8")

    index = parse_pages({"UUID": страница})
    item = index.items["query/UUID"]

    assert item.name_ru == "УНИКАЛЬНЫЙИДЕНТИФИКАТОР"
    assert item.since == "8.3.22"


def test_версия_берётся_по_английскому_имени_страницы():
    """Ключ таблицы — имя файла внутри контейнера, а не русское имя.

    `АВТОНОМЕРЗАПИСИ` лежит на странице `RECORDAUTONUMBER`,
    `СГРУППИРОВАНОПО` — на `GROUPING`. Русские имена в заголовках у части
    функций слипшиеся, английские — всегда чистые.
    """
    страница = """<HTML><HEAD></HEAD><BODY>
<H1 class="">Функция АВТОНОМЕРЗАПИСИ</H1>
<P>АВТОНОМЕРЗАПИСИ()</P>
<P>Возвращает номер записи в наборе.</P>
</BODY></HTML>""".encode("utf-8")

    index = parse_pages({"RECORDAUTONUMBER": страница})

    assert index.items["query/RECORDAUTONUMBER"].since == "8.3.13"


def test_версия_ставится_и_конструкции_а_не_только_функции():
    """`ДОБАВИТЬ <Имя временной таблицы>` — ключевое слово, а не функция, и
    появилось в 8.3.25. Таблица версий не привязана к виду элемента."""
    страница = """<HTML><HEAD></HEAD><BODY>
<H1 class="">Добавление новых строк в существующую временную таблицу</H1>
<P>ВЫБРАТЬ &lt;Список выборки запроса&gt;</P>
<P>ДОБАВИТЬ &lt;Имя временной таблицы&gt;</P>
</BODY></HTML>""".encode("utf-8")

    index = parse_pages({"temp_ADD": страница})

    assert index.items["query/temp_ADD"].since == "8.3.25"
