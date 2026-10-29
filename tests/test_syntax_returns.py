"""Возвращаемое значение: типы берутся из строки «Тип:», а не из всего текста.

Раньше ссылки по всему разделу попадали в `returns`: у метода, который
возвращает Булево, список получал ещё и «Истина» с «Ложь» из пояснения.
"""

from __future__ import annotations

from mcp1c.render import render_syntax_item
from mcp1c.store import load_syntax, save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem, SyntaxVariant
from mcp1c.syntax_parser import parse_page

_ШАПКА = """<html><body>
<h1 class="V8SH_pagetitle">Тест.Метод (Test.Method)</h1>
<p class="V8SH_title">Тест (Test)</p>
<p class="V8SH_heading">Метод (Method)</p>
<p class="V8SH_chapter">Синтаксис:</p>Метод()
<p class="V8SH_chapter">Возвращаемое значение:</p>"""

_ПОДВАЛ = "\n</body></html>"

_БУЛЕВО = (
    'Тип: <a href="v8help://SyntaxHelperContext/def_Boolean">Булево</a>.<br>'
    'Для значений типа <a href="v8help://SyntaxHelperContext/def_Boolean">Булево</a> '
    'всегда возвращается <a href="v8help://SyntaxHelperContext/def_True">Истина</a>.<br>'
    'Для <a href="v8help://SyntaxHelperContext/def_Undefined">Неопределено</a> и '
    '<a href="v8help://SyntaxHelperContext/def_Null">Null</a> '
    'всегда возвращается <a href="v8help://SyntaxHelperContext/def_False">Ложь</a>.'
)


def _разобрать(фрагмент: str) -> SyntaxVariant:
    item = parse_page("objects/test/methods/Method.html", (_ШАПКА + фрагмент + _ПОДВАЛ).encode())
    assert item is not None
    assert len(item.variants) == 1
    return item.variants[0]


def test_значение_заполнено_возвращает_только_булево():
    variant = _разобрать(_БУЛЕВО)

    assert variant.returns == ["Булево"]
    assert "Для Неопределено и Null всегда возвращается Ложь" in variant.returns_description


def test_несколько_типов_возврата():
    variant = _разобрать(
        'Тип: <a href="v8help://SyntaxHelperContext/a">ДанныеФормыЭлементДерева</a>, '
        '<a href="v8help://SyntaxHelperContext/b">Неопределено</a>.<br>'
        '<a href="v8help://SyntaxHelperContext/b">Неопределено</a> - элемент не найден.'
    )

    assert variant.returns == ["ДанныеФормыЭлементДерева", "Неопределено"]


def test_раздел_без_строки_тип_не_меняется():
    variant = _разобрать(
        'Прежнее значение свойства <a href="v8help://SyntaxHelperContext/s">Строка</a>.'
    )

    assert variant.returns == ["Строка"]


def test_returns_types_без_тип_возвращает_fallback():
    from mcp1c.syntax_parser import returns_types

    fallback = ["Строка", "Число"]

    assert returns_types("Прежнее значение свойства Строка.", fallback) == ["Строка", "Число"]
    assert returns_types("", fallback) == ["Строка", "Число"]


def test_returns_types_парсит_первую_строку():
    from mcp1c.syntax_parser import returns_types

    result = returns_types(
        "Тип: СправочникСсылка.<Имя справочника>, Неопределено.\nописание",
        ["не", "используется"],
    )

    assert result == ["СправочникСсылка.<Имя справочника>", "Неопределено"]


def _индекс_с_возвратом(returns: list[str]) -> SyntaxIndex:
    index = SyntaxIndex(platforms=["8.3.27.2130"], source="test")
    index.add(
        SyntaxItem(
            id="method.Тест.Метод",
            kind="method",
            name_ru="Метод",
            name_en="Method",
            parent_ru="Тест",
            variants=[
                SyntaxVariant(
                    signature="Метод()",
                    returns=returns,
                    returns_description=(
                        "Тип: Булево.\nДля значений типа Булево всегда возвращается Истина."
                    ),
                )
            ],
        )
    )
    return index


def test_старый_индекс_исправляется_при_чтении(tmp_path):
    index = _индекс_с_возвратом(["Булево", "Булево", "Истина"])

    path = save_syntax(index, tmp_path / "8.3.27.2130.json.gz")
    loaded = load_syntax(path)

    assert loaded.items["method.Тест.Метод"].variants[0].returns == ["Булево"]


def test_карточка_выводит_описание_возврата():
    item = _индекс_с_возвратом(["Булево"]).items["method.Тест.Метод"]

    text = render_syntax_item(item, "fields", None)
    lines = text.splitlines()

    assert "Возвращает: Булево" in lines
    assert "  Для значений типа Булево всегда возвращается Истина." in lines
    assert "Тип: Булево." not in text
