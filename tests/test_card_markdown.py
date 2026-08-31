"""Разбор markdown в карточке дашборда.

`render.py` отдаёт markdown, и это правильный формат: его получает агент по
MCP, там разметка несёт структуру — обратные кавычки выделяют имя, которое
надо скопировать в код без искажений, `>` помечает оговорку про источник.
Разбирать её незачем ни агенту, ни CLI: браузер — единственное место, где
символы печатаются буквально.

Поэтому разбор живёт здесь, а не в `render.py`, и покрывает ровно то, что
`render.py` порождает: заголовки, списки, код, жирный, цитаты, разделители.
Ни таблиц, ни вложенных списков он не выдаёт — их и не разбираем.

Рядом остаётся переключатель «как есть»: дашборд — инструмент проверки, и
человек должен уметь увидеть буквально тот текст, который ушёл агенту.
"""

from __future__ import annotations

from mcp1c.dashboard_backend import render_markdown


def test_заголовки_разных_уровней():
    html = render_markdown("# Метод: СтрНайти\n## Параметры\n### Вариант 1")

    assert "<h1>Метод: СтрНайти</h1>" in html
    assert "<h2>Параметры</h2>" in html
    assert "<h3>Вариант 1</h3>" in html


def test_имя_в_обратных_кавычках_становится_кодом():
    """Точное имя объекта — то, что копируют в код; выделяем его явно."""
    html = render_markdown("Полное имя: `Документ.ЧекККМ`")

    assert "<code>Документ.ЧекККМ</code>" in html
    assert "`" not in html


def test_жирный_текст():
    html = render_markdown("**Чек ККМ**")

    assert "<b>Чек ККМ</b>" in html
    assert "**" not in html


def test_список():
    html = render_markdown("- `ИНН` — Строка\n- `КПП` — Строка")

    assert html.count("<li>") == 2
    assert "<ul>" in html and "</ul>" in html


def test_цитата_с_оговоркой():
    """Оговорки про источник данных — то, что нельзя пролистать."""
    html = render_markdown("> **Оговорки**\n> - справка старее конфигурации")

    assert "<blockquote>" in html
    assert "справка старее конфигурации" in html


def test_разделитель():
    assert "<hr>" in render_markdown("текст\n\n---\n\nещё текст")


def test_разметка_не_ломает_экранирование():
    """Главное требование: содержимое приходит из чужих данных."""
    html = render_markdown("# <script>alert(1)</script>\n- `<img onerror=x>`")

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html


def test_звёздочка_в_обычном_тексте_не_считается_разметкой():
    html = render_markdown("Длина * 2 символа")

    assert "Длина * 2 символа" in html


def test_пустой_текст():
    assert render_markdown("") == ""
