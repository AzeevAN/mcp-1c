"""Старая разметка справки платформы: разделы на `div`, а не на `p`.

Классы те же (`V8SH_chapter`, `V8SH_title`, `V8SH_heading`), но теги другие.
Справка от 8.3.5 размечает раздел так::

    <div class="V8SH_chapter"> <p style="...">Описание:</div>Текст...

а от 8.3.27 — так::

    <p class="V8SH_chapter">Описание:</p>Текст...

Парсер искал строго `<p class="V8SH_chapter">`, поэтому на старой справке
доставал имена и не доставал ничего больше: 18 936 элементов и ноль описаний.
Такая справка отвергалась при загрузке — а она единственно верная для
конфигураций на 8.3.5, где новых методов попросту нет.

Разметка здесь синтетическая: структура повторена, тексты свои. Контент фирмы
«1С» и локальные данные установки в репозиторий не входят.
"""

from __future__ import annotations

from mcp1c.syntax_parser import parse_page

СТАРАЯ = """<html><head><meta charset="utf-8"></head><body>
<h1 class="V8SH_pagetitle">ТестовыйОбъект.ТестовоеСвойство (TestObject.TestProperty)</h1>
<div class="V8SH_title">ТестовыйОбъект (TestObject)</div>
<div class="V8SH_heading">ТестовоеСвойство (TestProperty)</div>
<div class="V8SH_chapter"> <p style="margin-top: 3px">Использование:</div>Чтение и запись.
<div class="V8SH_chapter"> <p style="margin-top: 3px">Описание:</div>Тип: Строка.
<br>Содержит проверочное значение.<br>
<div class="V8SH_chapter"> <p style="margin-top: 3px">Доступность: </div>Тонкий клиент, сервер.
</body></html>"""

НОВАЯ = """<html><head><meta charset="utf-8"></head><body>
<h1 class="V8SH_pagetitle">ТестовыйОбъект.ТестовоеСвойство (TestObject.TestProperty)</h1>
<p class="V8SH_title">ТестовыйОбъект (TestObject)</p>
<p class="V8SH_heading">ТестовоеСвойство (TestProperty)</p>
<p class="V8SH_chapter">Использование:</p>Чтение и запись.
<p class="V8SH_chapter">Описание:</p>Тип: Строка.
<br>Содержит проверочное значение.<br>
<p class="V8SH_chapter">Доступность: </p>Тонкий клиент, сервер.
</body></html>"""


def test_старая_разметка_даёт_описание():
    item = parse_page("objects/catalog1/TestProperty1.html", СТАРАЯ.encode("utf-8"))

    assert item is not None
    assert item.name_ru == "ТестовоеСвойство"
    assert item.name_en == "TestProperty"
    assert item.parent_ru == "ТестовыйОбъект"
    assert "проверочное значение" in item.description


def test_новая_разметка_не_сломана():
    """Регресс-проверка: та же страница на `p` разбирается как прежде."""
    item = parse_page("objects/catalog1/TestProperty1.html", НОВАЯ.encode("utf-8"))

    assert item is not None
    assert item.name_ru == "ТестовоеСвойство"
    assert "проверочное значение" in item.description


def test_обе_разметки_дают_одно_и_то_же():
    старый = parse_page("objects/catalog1/TestProperty1.html", СТАРАЯ.encode("utf-8"))
    новый = parse_page("objects/catalog1/TestProperty1.html", НОВАЯ.encode("utf-8"))

    assert (старый.name_ru, старый.name_en) == (новый.name_ru, новый.name_en)
    assert старый.description == новый.description
    assert старый.availability == новый.availability
