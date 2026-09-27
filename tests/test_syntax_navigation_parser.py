"""Связи карточки сохраняют исходную страницу и не захватывают внешний футер."""

from __future__ import annotations

import gzip
import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from mcp1c.store import load_syntax, save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxLink, SyntaxLinkSnapshot
from mcp1c.syntax_parser import parse_hbk, parse_page


@pytest.mark.parametrize("tag", ["div", "p"])
def test_ссылки_разделов_сохраняют_подписи_и_адреса(tag):
    source = f"""<h1 class="V8SH_pagetitle">ТестовыйОбъект</h1>
<{tag} class="V8SH_chapter">Методы:</{tag}>
<a href="methods/Первый.html">Первый</a>
<a href="methods/Второй.html?x=1&amp;y=2">Второй</a>
<{tag} class="V8SH_chapter">Свойства:</{tag}>
<a href="properties/Код.html">Код</a>
<{tag} class="V8SH_chapter">События:</{tag}>
<a href="events/ПриЗаписи.html">ПриЗаписи</a>
<{tag} class="V8SH_chapter">Конструкторы:</{tag}>
<a href="constructors/Новый.html">Новый</a>
<{tag} class="V8SH_chapter">Элементы коллекции:</{tag}>
<a href="collection/Элемент.html">Элемент</a>
<{tag} class="V8SH_chapter">См. также:</{tag}>
<a href="v8help://SyntaxHelperContext/objects/Другой.html">Другой</a>
<a href="https://example.invalid/help">Внешняя справка</a>
<a C="https://example.invalid/method">Методическая информация</a>
"""
    item = parse_page("objects/ТестовыйОбъект.html", source.encode())

    assert item is not None
    assert item.members == {
        "methods": ["Первый", "Второй"],
        "properties": ["Код"],
        "events": ["ПриЗаписи"],
        "constructors": ["Новый"],
        "collection": ["Элемент"],
    }
    assert item.see_also == ["v8help://SyntaxHelperContext/objects/Другой.html"]
    assert item.link_snapshots == [
        SyntaxLinkSnapshot(
            platform="",
            source_id="objects/ТестовыйОбъект",
            links=[
                SyntaxLink("methods", "Первый", "methods/Первый.html"),
                SyntaxLink("methods", "Второй", "methods/Второй.html?x=1&amp;y=2"),
                SyntaxLink("properties", "Код", "properties/Код.html"),
                SyntaxLink("events", "ПриЗаписи", "events/ПриЗаписи.html"),
                SyntaxLink("constructors", "Новый", "constructors/Новый.html"),
                SyntaxLink("collection", "Элемент", "collection/Элемент.html"),
                SyntaxLink("see_also", "Другой", "v8help://SyntaxHelperContext/objects/Другой.html"),
            ],
        )
    ]


def test_пустой_снимок_отличим_от_старого_индекса():
    source = b'<h1 class="V8SH_pagetitle">Empty</h1><a C="https://example.invalid">Method</a>'
    item = parse_page("objects/Empty.html", source)

    assert item is not None
    assert item.link_snapshots == [SyntaxLinkSnapshot(platform="", source_id="objects/Empty")]


def test_пустая_страница_архива_помечается_без_снимка(monkeypatch):
    import mcp1c.syntax_parser as parser

    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("objects/A.html", '<h1 class="V8SH_pagetitle">A</h1>')

    class Archive:
        def __enter__(self):
            self.zip = ZipFile(BytesIO(buffer.getvalue()))
            return self.zip

        def __exit__(self, *_):
            self.zip.close()

    monkeypatch.setattr(parser, "open_file_storage", lambda _: Archive())
    index = parse_hbk("unused.hbk", platform="8.3.27.2130")

    assert index.platforms == ["8.3.27.2130"]
    assert index.items["objects/A"].link_snapshots == []
    assert index.items["objects/A"].empty_link_mask == 1


def test_снимок_и_маска_переживают_хранение_а_старый_индекс_остаётся_неизвестным(tmp_path):
    item = parse_page(
        "objects/A.html",
        b'<h1 class="V8SH_pagetitle">A</h1><p class="V8SH_chapter">Methods:</p>',
    )
    assert item is not None
    item.link_snapshots[0].platform = "8.3.27"
    item.link_snapshots[0].links.append(SyntaxLink("methods", "B", "methods/B.html", "objects/A/methods/B"))
    item.empty_link_mask = 1
    index = SyntaxIndex(platforms=["8.3.19", "8.3.27"])
    index.add(item)
    path = save_syntax(index, tmp_path / "syntax.json.gz")

    restored = load_syntax(path)
    assert restored.items[item.id].link_snapshots == item.link_snapshots
    assert restored.items[item.id].empty_link_mask == 1

    with gzip.open(path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    compact = payload["items"][0]["link_snapshots"][0]
    payload["items"][0]["link_snapshots"] = [{
        "platform": compact[0],
        "source_id": compact[1],
        "links": [{
            "section": link[0], "label": link[1],
            "href": link[2], "target_id": link[3],
        } for link in compact[2]],
    }]
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream)
    assert load_syntax(path).items[item.id].link_snapshots == item.link_snapshots

    payload["items"][0].pop("link_snapshots")
    payload["items"][0].pop("empty_link_mask")
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream)

    legacy = load_syntax(path).items[item.id]
    assert legacy.link_snapshots == []
    assert legacy.empty_link_mask == 0
