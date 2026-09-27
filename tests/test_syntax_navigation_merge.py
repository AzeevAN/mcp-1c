"""Версионные ссылки остаются фактами своих исходных справок."""

from __future__ import annotations

import gzip
import json

from mcp1c.store import load_syntax, save_syntax
from mcp1c.syntax_merge import merge_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem, SyntaxLink, SyntaxLinkSnapshot


def _index(platform: str, *items: SyntaxItem) -> SyntaxIndex:
    result = SyntaxIndex(platforms=[platform], source=f"synthetic-{platform}")
    for item in items:
        result.add(item)
    return result


def _owner(platform: str, name: str, *links: tuple[str, str]) -> SyntaxItem:
    return SyntaxItem(
        id="objects/Owner/Item",
        kind="object",
        name_ru=name,
        name_en="Owner",
        link_snapshots=[
            SyntaxLinkSnapshot(
                platform=platform,
                source_id="objects/Owner/Item",
                links=[
                    SyntaxLink(
                        section="methods",
                        label=label,
                        href=f"methods/{target}.html",
                        target_id=f"objects/Owner/methods/{target}",
                    )
                    for label, target in links
                ],
            )
        ],
    )


def _method(platform: str, name: str, *, since: str = "") -> SyntaxItem:
    return SyntaxItem(
        id=f"objects/Owner/methods/{name}",
        kind="method",
        name_ru=name,
        name_en=name,
        parent_ru="Владелец",
        parent_en="Owner",
        since=since,
        link_snapshots=[
            SyntaxLinkSnapshot(platform=platform, source_id=f"objects/Owner/methods/{name}")
        ],
    )


def _links(item: SyntaxItem) -> dict[str, list[tuple[str, str]]]:
    return {
        snapshot.platform: [(link.label, link.target_id) for link in snapshot.links]
        for snapshot in item.link_snapshots
    }


def _empty_owner(platform: str) -> SyntaxItem:
    item = _owner(platform, "Владелец")
    item.link_snapshots = []
    item.empty_link_mask = 1
    return item


def test_четыре_ссылки_и_пять_ссылок_не_смешиваются_между_версиями():
    old_names = ("Первый", "Второй", "Третий", "Четвертый")
    old = _index(
        "8.3.5",
        _owner("8.3.5", "Владелец", *((name, name) for name in old_names)),
        *(_method("8.3.5", name) for name in old_names),
    )
    new = _index(
        "8.3.19",
        _owner(
            "8.3.19", "Владелец", *((name, name) for name in (*old_names, "Пятый"))
        ),
        *(_method("8.3.19", name) for name in old_names),
        _method("8.3.19", "Пятый", since="8.3.8"),
    )

    merged = merge_syntax([old, new])
    owner = next(item for item in merged.items.values() if item.kind == "object")
    fifth = next(item for item in merged.items.values() if item.name_ru == "Пятый")

    assert {platform: len(links) for platform, links in _links(owner).items()} == {
        "8.3.5": 4,
        "8.3.19": 5,
    }
    assert fifth.since == "8.3.8"
    assert fifth.available_in("8.3.5") is False
    assert fifth.available_in("8.3.19") is True


def test_переименование_владельца_не_отрывает_ссылку_старой_справки():
    old = _index(
        "8.3.5",
        _owner("8.3.5", "УправляемаяФорма", ("Открыть", "Open")),
        SyntaxItem(
            id="objects/Owner/methods/Open",
            kind="method",
            name_ru="Открыть",
            name_en="Open",
            parent_ru="УправляемаяФорма",
            parent_en="ManagedForm",
        ),
    )
    new = _index(
        "8.3.27",
        _owner("8.3.27", "ФормаКлиентскогоПриложения", ("Открыть", "Open")),
        SyntaxItem(
            id="objects/Owner/methods/Open",
            kind="method",
            name_ru="Открыть",
            name_en="Open",
            parent_ru="ФормаКлиентскогоПриложения",
            parent_en="ClientApplicationForm",
        ),
    )

    merged = merge_syntax([old, new])
    owner = next(item for item in merged.items.values() if item.kind == "object")
    method = next(item for item in merged.items.values() if item.kind == "method")

    assert len([item for item in merged.items.values() if item.kind == "method"]) == 1
    assert _links(owner) == {
        "8.3.5": [("Открыть", method.id)],
        "8.3.27": [("Открыть", method.id)],
    }


def test_коллизия_пути_цели_сохраняет_адрес_старой_карточки():
    path = "objects/Owner/methods/Reused"
    old = _index(
        "8.3.5",
        _owner("8.3.5", "Владелец", ("Старый", "Reused")),
        SyntaxItem(id=path, kind="method", name_ru="Старый", name_en="Old"),
    )
    new = _index(
        "8.3.27",
        _owner("8.3.27", "Владелец", ("Новый", "Reused")),
        SyntaxItem(id=path, kind="method", name_ru="Новый", name_en="New"),
    )

    merged = merge_syntax([old, new])
    owner = next(item for item in merged.items.values() if item.kind == "object")

    assert "objects/Owner/methods/Reused@8.3.5" in merged.items
    assert _links(owner) == {
        "8.3.5": [("Старый", "objects/Owner/methods/Reused@8.3.5")],
        "8.3.27": [("Новый", path)],
    }


def test_попарное_слияние_равно_одноразовому_и_не_меняет_исходники():
    old = _index("8.3.5", _owner("8.3.5", "Владелец", ("A", "A")))
    middle = _index("8.3.19", _owner("8.3.19", "Владелец", ("A", "A"), ("B", "B")))
    new = _index("8.3.27", _owner("8.3.27", "Владелец", ("B", "B")))

    one_shot = merge_syntax([old, middle, new])
    pairwise = merge_syntax([merge_syntax([old, middle]), new])
    one_owner = next(item for item in one_shot.items.values() if item.kind == "object")
    pair_owner = next(item for item in pairwise.items.values() if item.kind == "object")

    assert _links(pair_owner) == _links(one_owner)
    assert _links(old.items["objects/Owner/Item"]) == {
        "8.3.5": [("A", "objects/Owner/methods/A")]
    }
    assert _links(middle.items["objects/Owner/Item"]) == {
        "8.3.19": [
            ("A", "objects/Owner/methods/A"),
            ("B", "objects/Owner/methods/B"),
        ]
    }
    assert _links(new.items["objects/Owner/Item"]) == {
        "8.3.27": [("B", "objects/Owner/methods/B")]
    }


def test_маска_пустых_страниц_переносится_по_версиям_при_слиянии():
    old = _index("8.3.5", _empty_owner("8.3.5"))
    middle = _index("8.3.19", _empty_owner("8.3.19"))
    new = _index(
        "8.3.27",
        _owner("8.3.27", "Владелец", ("Новый", "New")),
        _method("8.3.27", "New", since="8.3.27"),
    )

    one_shot = merge_syntax([old, middle, new])
    pairwise = merge_syntax([merge_syntax([middle, new]), old])
    for merged in (one_shot, pairwise):
        owner = merged.items["objects/Owner/Item"]
        assert merged.platforms == ["8.3.5", "8.3.19", "8.3.27"]
        assert owner.empty_link_mask == 0b011
        assert _links(owner) == {
            "8.3.27": [("Новый", "objects/Owner/methods/New")]
        }
    assert old.items["objects/Owner/Item"].empty_link_mask == 1
    assert middle.items["objects/Owner/Item"].empty_link_mask == 1
    assert new.items["objects/Owner/Item"].empty_link_mask == 0


def test_старый_индекс_без_полей_связей_не_выглядит_пустой_страницей(tmp_path):
    old = _index("8.3.5", _empty_owner("8.3.5"))
    path = save_syntax(old, tmp_path / "old.json.gz")
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    payload["items"][0].pop("link_snapshots")
    payload["items"][0].pop("empty_link_mask")
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream)

    legacy = load_syntax(path)
    middle = _index("8.3.19", _empty_owner("8.3.19"))
    new = _index(
        "8.3.27",
        _owner("8.3.27", "Владелец", ("Новый", "New")),
        _method("8.3.27", "New"),
    )
    merged = merge_syntax([merge_syntax([legacy, middle]), new])
    owner = merged.items["objects/Owner/Item"]

    assert merged.platforms == ["8.3.5", "8.3.19", "8.3.27"]
    assert owner.empty_link_mask == 0b010
    assert _links(owner) == {
        "8.3.27": [("Новый", "objects/Owner/methods/New")]
    }
