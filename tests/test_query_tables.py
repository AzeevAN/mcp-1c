"""Таблицы страниц языка запросов: показ отдельно от поиска.

Разметка справки даёт таблицы двух разных природ, и обращаться с ними
одинаково нельзя. `class=SimplyTable` — настоящая таблица данных (результат
запроса-примера). Таблица без класса, из ячеек с `&nbsp;` и `|`, — рисованная
синтаксическая диаграмма: колонки там держат отступ ветвления, а не смысл.
"""

from mcp1c.query_parser import parse_page, parse_pages
from mcp1c.syntax_model import без_меток

РЕЗУЛЬТАТ_ЗАПРОСА = """<HTML><BODY>
<H1 class="">Итоги по иерархии</H1>
<P>Есть возможность рассчитать итоги по иерархии.</P>
<H4 class="">Результат запроса:</H4>
<BLOCKQUOTE>
<P>
<TABLE class=SimplyTable>
<TBODY>
<TR>
<TD class=NarrowColumn><P><FONT size=1><STRONG>Товар</STRONG>&nbsp;</FONT></P></TD>
<TD class=NarrowColumn><P><FONT size=1><STRONG>Количество</STRONG>&nbsp;</FONT></P></TD></TR>
<TR>
<TD class=NarrowColumn><P><FONT size=1>&nbsp;Сантехника&nbsp;&nbsp;</FONT></P></TD>
<TD class=NarrowColumn><P align=right><FONT size=1>104&nbsp;</FONT></P></TD></TR>
<TR>
<TD class=NarrowColumn><P><FONT size=1>&nbsp;Кран&nbsp;</FONT></P></TD>
<TD class=NarrowColumn><P align=right><FONT size=1>84&nbsp;</FONT></P></TD></TR>
</TBODY></TABLE></P></BLOCKQUOTE>
</BODY></HTML>""".encode("utf-8")


def test_таблица_данных_не_попадает_в_описание():
    """Ячейки размечены абзацами внутри TD: без отдельного разбора каждая
    становится своим абзацем, и карточка печатает таблицу столбцом."""
    item = parse_page("hierarchical_totals.html", РЕЗУЛЬТАТ_ЗАПРОСА)

    assert "Сантехника" not in item.description
    assert "104" not in item.description
    assert без_меток(item.description) == "Есть возможность рассчитать итоги по иерархии."


def test_таблица_данных_разобрана_по_строкам_и_ячейкам():
    item = parse_page("hierarchical_totals.html", РЕЗУЛЬТАТ_ЗАПРОСА)

    assert len(item.tables) == 1
    таблица = item.tables[0]
    assert таблица.header == ["Товар", "Количество"]
    assert таблица.rows == [["Сантехника", "104"], ["Кран", "84"]]


ВЛОЖЕННАЯ = """<HTML><BODY>
<H1 class="">Выборка вложенных таблиц</H1>
<P>Прозаический текст статьи.</P>
<TABLE class=SimplyTable>
<TBODY>
<TR><TD><P><STRONG>Ссылка</STRONG></P></TD><TD><P><STRONG>Состав</STRONG></P></TD></TR>
<TR>
<TD><P>Расходная накладная 00007</P></TD>
<TD><P>
<TABLE class=SimplyTable>
<TBODY>
<TR><TD><P><STRONG>Товар</STRONG></P></TD><TD><P><STRONG>Количество</STRONG></P></TD></TR>
<TR><TD><P>Джинсы женские</P></TD><TD><P>4</P></TD></TR>
</TBODY></TABLE></P></TD></TR>
</TBODY></TABLE>
<P>Абзац после таблицы.</P>
</BODY></HTML>""".encode("utf-8")


def test_вложенная_таблица_разбирается_отдельно_от_внешней():
    """Разбор без учёта вложенности собирает ячейки внутренней таблицы в
    строку внешней. Строки выходят рваной ширины (2 и 3), и таблица данных
    выглядит рисованной диаграммой — обе теряются разом."""
    item = parse_page("nestedtables_fetch.html", ВЛОЖЕННАЯ)

    assert [t.header for t in item.tables] == [
        ["Ссылка", "Состав"],
        ["Товар", "Количество"],
    ]
    assert item.tables[0].rows == [["Расходная накладная 00007", ""]]
    assert item.tables[1].rows == [["Джинсы женские", "4"]]
    assert без_меток(item.description) == (
        "Прозаический текст статьи.\n\nАбзац после таблицы."
    )


НЕЗАКРЫТАЯ = """<HTML><BODY>
<H1 class="">Битая страница</H1>
<P>Проза до таблицы.</P>
<TABLE class=SimplyTable>
<TBODY>
<TR><TD><STRONG>Код</STRONG></TD></TR>
<TR><TD>1</TD></TR>
</TBODY>
<P>Проза после таблицы.</P>
</BODY></HTML>""".encode("utf-8")


def test_незакрытая_таблица_поднимает_ошибку_а_не_глотает_страницу():
    """Молчаливый фоллбек тут стоит дорого: разбор «до конца тела» съел бы всю
    прозу после таблицы, и страница осталась бы в индексе обрезанной."""
    import pytest

    from mcp1c.query_parser import ОбрывТаблицы

    with pytest.raises(ОбрывТаблицы):
        parse_page("broken.html", НЕЗАКРЫТАЯ)


def test_битая_страница_не_теряется_а_разбирается_без_таблиц():
    """Одна испорченная страница не должна ронять загрузку всей справки: текст
    её нужен и карточке, и поиску. Но и промолчать нельзя."""
    index = parse_pages({"broken.html": НЕЗАКРЫТАЯ})
    item = index.items["query/broken.html"]

    assert "Проза до таблицы." in item.description
    assert "Проза после таблицы." in item.description
    assert item.tables == []


def test_битая_страница_названа_в_предупреждениях_поимённо():
    index = parse_pages({"broken.html": НЕЗАКРЫТАЯ})

    assert len(index.warnings) == 1
    assert "broken.html" in index.warnings[0]


БЕЗ_КЛАССА = """<HTML><BODY>
<H1 class="">Псевдонимы полей</H1>
<P>Проза статьи.</P>
<TABLE>
<TBODY>
<TR><TD><P><STRONG>&nbsp;Товар</STRONG>&nbsp;</P></TD><TD><P><STRONG>&nbsp;Группа</STRONG></P></TD></TR>
<TR><TD><P>&nbsp;Брюки детские</P></TD><TD><P>&nbsp;Одежда</P></TD></TR>
</TBODY></TABLE>
</BODY></HTML>""".encode("utf-8")


def test_таблица_данных_узнаётся_без_класса_по_прямоугольности():
    """`class=SimplyTable` стоит не на всех: 7 настоящих таблиц справки идут
    без него. Признак по разметке один — прямоугольность: у таблицы данных
    все строки одной ширины, у рисованной диаграммы ширины рваные."""
    item = parse_page("pseudonim_fields.html", БЕЗ_КЛАССА)

    assert [t.header for t in item.tables] == [["Товар", "Группа"]]
    assert item.tables[0].rows == [["Брюки детские", "Одежда"]]
    assert без_меток(item.description) == "Проза статьи."


ДИАГРАММА = """<HTML><BODY>
<H1 class="">Описание соединений</H1>
<P>Проза перед диаграммой.</P>
<TABLE width="100%" border=0>
<TBODY>
<TR>
<TD>
<TABLE align=left>
<TBODY>
<TR><TD colSpan=3>&lt;Перечень соединений&gt;</TD></TR>
<TR><TD>&nbsp;</TD><TD><STRONG>|</STRONG></TD><TD>&nbsp;</TD></TR>
<TR><TD>&nbsp;</TD><TD colSpan=2>&lt;Соединение&gt; [&lt;Перечень соединений&gt;]</TD></TR>
<TR><TD>&nbsp;</TD><TD>&nbsp;</TD><TD><STRONG>|</STRONG></TD></TR>
<TR><TD>&nbsp;</TD><TD>&nbsp;</TD><TD>ЛЕВОЕ [ВНЕШНЕЕ] СОЕДИНЕНИЕ</TD></TR>
</TBODY></TABLE></TD></TR>
</TBODY></TABLE>
</BODY></HTML>""".encode("utf-8")


def test_диаграмма_остаётся_лесенкой_а_не_столбцом():
    """Диаграмма — грамматика языка запросов, выбросить её нельзя. Колонок в
    ней нет: ширина ячейки держит уровень ветвления, и он передаётся отступом.
    Печатать её таблицей — тот же столбец значений, ради которого всё и
    затевалось."""
    item = parse_page("JOIN", ДИАГРАММА)

    assert item.tables == []
    assert item.description == (
        "Проза перед диаграммой.\n\n"
        "<Перечень соединений>\n"
        "    |\n"
        "    <Соединение> [<Перечень соединений>]\n"
        "        |\n"
        "        ЛЕВОЕ [ВНЕШНЕЕ] СОЕДИНЕНИЕ"
    )


ДИАГРАММА_ВЕТВЛЕНИЕ = """<HTML><BODY>
<H1 class="">Описание источников</H1>
<P>Проза.</P>
<TABLE border=0>
<TBODY>
<TR><TD colSpan=4>&lt;Описание источника&gt;</TD></TR>
<TR><TD>&nbsp;</TD><TD><STRONG>|</STRONG></TD><TD><STRONG>|</STRONG></TD><TD>&nbsp;</TD></TR>
<TR><TD>&nbsp;</TD><TD><STRONG>|</STRONG></TD><TD colSpan=2>&lt;Соединение&gt;</TD></TR>
</TBODY></TABLE>
</BODY></HTML>""".encode("utf-8")


def test_ячейки_коннекторы_не_слипаются_с_содержимым_строки():
    """Вертикальная черта в ячейке — нарисованная линия, а не текст. Склеенная
    с содержимым через пробел, она читается как часть грамматики: строка вида
    «| | | | ЛЕВОЕ СОЕДИНЕНИЕ» выглядит перечислением, которого в языке нет."""
    item = parse_page("FromStatement", ДИАГРАММА_ВЕТВЛЕНИЕ)

    assert item.description == (
        "Проза.\n\n"
        "<Описание источника>\n"
        "    |\n"
        "    <Соединение>"
    )


def test_карточка_печатает_таблицу_таблицей():
    """Ради этого всё и затевалось: раньше карточка печатала таблицу столбцом
    значений — «Товар / Количество / Сантехника / 104» подряд."""
    from mcp1c.render import render_syntax_item

    item = parse_page("hierarchical_totals.html", РЕЗУЛЬТАТ_ЗАПРОСА)
    карточка = render_syntax_item(item)

    assert "| Товар | Количество |" in карточка
    assert "| --- | --- |" in карточка
    assert "| Сантехника | 104 |" in карточка


def test_беглый_взгляд_таблиц_не_показывает():
    """`brief` — беглый взгляд: заголовок, доступность, сигнатура. Таблица
    результата запроса на страницу в двадцать строк там неуместна."""
    from mcp1c.render import BRIEF, render_syntax_item

    item = parse_page("hierarchical_totals.html", РЕЗУЛЬТАТ_ЗАПРОСА)

    assert "| Товар |" not in render_syntax_item(item, BRIEF)


def test_черта_внутри_ячейки_не_ломает_разметку_таблицы():
    """`|` в значении разорвал бы markdown-строку на лишние колонки."""
    from mcp1c.render import render_syntax_item
    from mcp1c.syntax_model import KIND_QUERY_ARTICLE, SyntaxItem, SyntaxTable

    from mcp1c.syntax_model import МЕТКА_ТАБЛИЦЫ

    item = SyntaxItem(
        id="query/x",
        kind=KIND_QUERY_ARTICLE,
        name_ru="Проверка",
        description=МЕТКА_ТАБЛИЦЫ.format(номер=0),
        tables=[SyntaxTable(header=["Знак"], rows=[["a | b"]])],
    )

    assert "| a \\| b |" in render_syntax_item(item)


def test_дашборд_рисует_markdown_таблицу_таблицей():
    """`render_markdown` заявлял, что таблиц в выводе `render.py` не бывает.
    С разделением показа и поиска они там появились, и без разбора страница
    печатала бы разметку сырыми абзацами: «| Товар | Количество |»."""
    from mcp1c.dashboard import render_markdown

    html = render_markdown(
        "| Товар | Количество |\n| --- | --- |\n| Сантехника | 104 |"
    )

    assert "<table>" in html
    assert "<th>Товар</th><th>Количество</th>" in html
    assert "<td>Сантехника</td><td>104</td>" in html
    assert "| Товар |" not in html


def test_дашборд_не_принимает_за_таблицу_строку_без_разделителя():
    """Строка с чертой — не таблица: в лесенке грамматики черта стоит
    в каждой второй строке, и печатать её пустой таблицей нельзя."""
    from mcp1c.dashboard import render_markdown

    html = render_markdown("<Выражение>[.<Группа полей>]|<Описание пустой таблицы>")

    assert "<table>" not in html


def test_таблицы_переживают_сохранение_индекса(tmp_path):
    """Реестр держит разобранную справку на диске и поднимает её оттуда.
    Поле, не попавшее в сохранение, теряется молча: карточка после
    перезапуска снова остаётся без таблиц, и отличить это не от чего."""
    from mcp1c.store import load_syntax, save_syntax

    index = parse_pages({"hierarchical_totals.html": РЕЗУЛЬТАТ_ЗАПРОСА})
    поднятый = load_syntax(save_syntax(index, tmp_path / "query.json.gz"))
    таблицы = поднятый.items["query/hierarchical_totals.html"].tables

    assert [t.header for t in таблицы] == [["Товар", "Количество"]]
    assert таблицы[0].rows == [["Сантехника", "104"], ["Кран", "84"]]


def test_предупреждения_переживают_сохранение_индекса(tmp_path):
    """Иначе разбор говорит о битой странице один раз, при первой загрузке,
    а после перезапуска сервера справка выглядит целой."""
    from mcp1c.store import load_syntax, save_syntax

    index = parse_pages({"broken.html": НЕЗАКРЫТАЯ})
    поднятый = load_syntax(save_syntax(index, tmp_path / "query.json.gz"))

    assert поднятый.warnings == index.warnings
    assert "broken.html" in поднятый.warnings[0]


def test_реестр_называет_битые_страницы_при_загрузке(tmp_path):
    """Молчащая деградация дороже отказа: без этого справка с испорченными
    страницами грузится как здоровая, а карточки у части статей беднее."""
    from mcp1c.registry import Registry
    from mcp1c.store import save_syntax

    index = parse_pages({"broken.html": НЕЗАКРЫТАЯ})
    входящее = tmp_path / "incoming"
    входящее.mkdir(parents=True)
    save_syntax(index, входящее / "query-ru.json.gz")

    registry = Registry(tmp_path / "data")
    registry.add_syntax(входящее / "query-ru.json.gz")

    предупреждения = registry.query_source.warnings
    assert any("broken.html" in текст for текст in предупреждения)


ДВА_ПРИМЕРА = """<HTML><BODY>
<H1 class="">Итоги по иерархии</H1>
<P>Первый способ.</P>
<TABLE class=SimplyTable><TBODY>
<TR><TD><STRONG>А</STRONG></TD></TR><TR><TD>1</TD></TR>
</TBODY></TABLE>
<P>Второй способ.</P>
<TABLE class=SimplyTable><TBODY>
<TR><TD><STRONG>Б</STRONG></TD></TR><TR><TD>2</TD></TR>
</TBODY></TABLE>
</BODY></HTML>""".encode("utf-8")


def test_таблица_печатается_на_своём_месте_а_не_в_конце():
    """На странице с двумя примерами обе таблицы в хвосте карточки не дают
    понять, какая к какому примеру относится."""
    from mcp1c.render import render_syntax_item

    карточка = render_syntax_item(parse_page("hierarchical_totals.html", ДВА_ПРИМЕРА))
    первая = карточка.index("| А |")
    вторая = карточка.index("| Б |")

    assert карточка.index("Первый способ.") < первая < карточка.index("Второй способ.")
    assert вторая > карточка.index("Второй способ.")


def test_метка_таблицы_не_видна_в_карточке():
    from mcp1c.render import BRIEF, render_syntax_item

    item = parse_page("hierarchical_totals.html", ДВА_ПРИМЕРА)

    assert "\x00" not in render_syntax_item(item)
    assert "\x00" not in render_syntax_item(item, BRIEF)


def test_метка_таблицы_не_попадает_в_поисковый_индекс():
    """Иначе служебное слово становится токеном на трети страниц справки."""
    from mcp1c.search import index_syntax

    index = index_syntax(parse_pages({"hierarchical_totals.html": ДВА_ПРИМЕРА}))
    поле = index.docs["query/hierarchical_totals.html"].fields["description"]

    assert "\x00" not in поле
    assert "таблица" not in поле.lower()


def test_страница_источников_называет_битые_страницы(tmp_path):
    """Предупреждение, видимое только в строке вывода CLI при загрузке,
    прокручивается и теряется. Человек смотрит «Источники» — там оно и
    должно стоять, иначе обещание «молча не деградируем» не выполняется."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from mcp1c import dashboard
    from mcp1c.registry import Registry
    from mcp1c.store import save_syntax

    входящее = tmp_path / "incoming"
    входящее.mkdir(parents=True)
    save_syntax(
        parse_pages({"broken.html": НЕЗАКРЫТАЯ}), входящее / "query-ru.json.gz"
    )
    registry = Registry(tmp_path / "data")
    registry.add_syntax(входящее / "query-ru.json.gz")
    client = TestClient(Starlette(routes=dashboard.routes(registry)))

    страница = client.get("/sources").text

    assert "broken.html" in страница


def test_описание_без_меток_не_меняется_ни_на_байт():
    """`без_меток` зовётся на каждой карточке, в том числе платформенной, где
    меток не бывает. Схлопывать там пустые строки — менять чужой вывод ради
    своей задачи: у 14 элементов справки такие пропуски есть, и они авторские."""
    исходное = "Первый абзац.\n\n\nВторой абзац.\n\n\n\nТретий."

    assert без_меток(исходное) == исходное
