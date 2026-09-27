"""Адреса навигации берутся из исходной страницы, а не из подписи ссылки."""

from __future__ import annotations

import pytest

from mcp1c.syntax_links import target_page_id
from mcp1c.syntax_parser import parse_page


def test_относительная_и_контекстная_ссылки_ведут_к_страницам_той_же_справки():
    pages = {
        "objects/Query/methods/Execute",
        "objects/Query/methods/ExecuteBatch",
    }

    assert target_page_id(
        "objects/Query/Item",
        "methods/Execute.html",
        pages,
    ) == "objects/Query/methods/Execute"
    assert target_page_id(
        "objects/Query/Item",
        "v8help://SyntaxHelperContext/objects/Query/methods/ExecuteBatch.html",
        pages,
    ) == "objects/Query/methods/ExecuteBatch"


@pytest.mark.parametrize(
    "href",
    [
        "methods/Missing.html",
        "v8help://SyntaxHelperLanguage/objects/Query/methods/Execute.html",
        "https://example.invalid/Execute.html",
        "../../../../private/Execute.html",
        "v8help://SyntaxHelperContext/../private/Execute.html",
        "v8help://SyntaxHelperContext/objects/Query/methods/Execute.html?mode=1",
        "v8help://SyntaxHelperContext/objects/Query/methods/Execute.html#fragment",
    ],
)
def test_непроверенный_или_внешний_адрес_не_становится_переходом(href: str):
    assert target_page_id(
        "objects/Query/Item",
        href,
        {"objects/Query/methods/Execute"},
    ) == ""


def test_одинаковые_подписи_не_схлопывают_разные_цели_и_методическую_ссылку():
    raw = b"""
    <h1 class="V8SH_pagetitle">Owner</h1>
    <p class="V8SH_chapter">Methods:</p>
    <a href="methods/First.html">Same</a>
    <a href="methods/Second.html">Same</a>
    <a C="v8help://SyntaxHelperLanguage/guide">Methodical information</a>
    <p class="V8SH_chapter">See also:</p>
    <a href="v8help://SyntaxHelperContext/objects/Other/Item.html">Other</a>
    <a href="v8help://SyntaxHelperLanguage/guide.html">Language guide</a>
    """.replace(b"Methods:", "Методы:".encode()).replace(b"See also:", "См. также:".encode())

    item = parse_page("objects/Owner/Item.html", raw)

    assert item is not None
    assert [(link.section, link.label, link.href) for link in item.link_snapshots[0].links] == [
        ("methods", "Same", "methods/First.html"),
        ("methods", "Same", "methods/Second.html"),
        ("see_also", "Other", "v8help://SyntaxHelperContext/objects/Other/Item.html"),
    ]
