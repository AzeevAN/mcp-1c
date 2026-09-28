"""Дата появления и рекомендация замены остаются разными фактами."""

from __future__ import annotations

from mcp1c.registry import Registry
from mcp1c.store import load_syntax, save_syntax
from mcp1c.syntax_merge import merge_syntax
from mcp1c.syntax_model import SyntaxIndex
from mcp1c.syntax_parser import parse_page
from mcp1c.tools import get_syntax, search_syntax

from conftest import build_configuration, write_export


OLD_ID = "objects/Archive/Writer/methods/GetData"
NEW_ID = "objects/Archive/NewWriter/methods/GetData"


def _page(*, deprecated: bool) -> bytes:
    warning = (
        '<div class="__DEPRECATED_SHOW_STYLE__">'
        '<p class="not_used">Не рекомендуется использовать, начиная с версии 8.3.26.</p>'
        '<p>Рекомендуется использовать:</p><ul><li>'
        '<a href="v8help://SyntaxHelperContext/' + NEW_ID + '.html">ПолучитьДанные</a>'
        '</li></ul></div>'
        if deprecated else ""
    )
    return (
        '<h1 class="V8SH_pagetitle">ЗаписьАрхива.ПолучитьДанные</h1>'
        '<p class="V8SH_title">ЗаписьАрхива (ArchiveWriter)</p>'
        '<p class="V8SH_heading">ПолучитьДанные (GetData)</p>'
        + warning
        + '<div class="__SINCE_SHOW_STYLE__">'
        '<p class="not_used">Доступен, начиная с версии 8.3.10.</p></div>'
        '<p class="V8SH_chapter">Синтаксис:</p>ПолучитьДанные()'
        '<a C="https://example.invalid/method">Методическая информация</a>'
    ).encode()


def test_порог_устаревания_не_подменяет_дату_появления():
    item = parse_page(OLD_ID + ".html", _page(deprecated=True))

    assert item is not None
    assert item.since == "8.3.10"
    assert item.deprecations[0].since == "8.3.26"
    assert [link.href for link in item.deprecations[0].replacements] == [
        "v8help://SyntaxHelperContext/" + NEW_ID + ".html"
    ]
    assert [link.section for link in item.link_snapshots[0].links] == ["replacement"]


def test_устаревание_и_замена_видны_только_в_своей_версии_справки(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    paths = []
    for platform, deprecated in (("8.3.19.1417", False), ("8.3.26.15", True)):
        index = SyntaxIndex(platforms=[platform])
        old = parse_page(OLD_ID + ".html", _page(deprecated=deprecated))
        assert old is not None
        old.description = "Тестовое описание метода записи."
        old.deprecations[:] = [
            type(entry)(platform, entry.since, entry.source_id, entry.replacements)
            for entry in old.deprecations
        ]
        old.link_snapshots[0].platform = platform
        index.add(old)
        if deprecated:
            new = parse_page(
                NEW_ID + ".html",
                ('<h1 class="V8SH_pagetitle">New</h1>'
                 '<p class="V8SH_title">ЗаписьФайлаАрхива (ArchiveFileWriter)</p>'
                 '<p class="V8SH_heading">ПолучитьДанные (GetData)</p>'
                 '<div class="__SINCE_SHOW_STYLE__">'
                 '<p class="not_used">Доступен, начиная с версии 8.3.26.</p></div>').encode(),
            )
            assert new is not None
            new.description = "Тестовое описание нового метода записи."
            index.add(new)
        paths.append(save_syntax(index, incoming / f"{platform}.json.gz"))

    registry = Registry(tmp_path / "data")
    for path in reversed(paths):
        registry.add_syntax(path)
    for name, platform, mode in (
        ("Старая", "8.3.19.1417", ""),
        ("Новая", "8.3.26.15", "Version8_3_10"),
    ):
        config = build_configuration(name)
        config.platform = platform
        registry.add_configuration(write_export(incoming, config))
        # schema v1 не переносит CompatibilityMode; это отдельное поле Source B.
        registry.configurations[name].config.compatibility_mode = mode

    old_answer = get_syntax(registry, "ЗаписьАрхива.ПолучитьДанные", "Старая")
    new_answer = get_syntax(registry, "ЗаписьАрхива.ПолучитьДанные", "Новая")
    assert "с версии платформы **8.3.10**" in old_answer
    assert "Не рекомендуется" not in old_answer
    assert "с версии платформы **8.3.10**" in new_answer
    assert "Не рекомендуется использовать с версии 8.3.26" in new_answer
    assert "Рекомендуемая замена: `ЗаписьФайлаАрхива.ПолучитьДанные`" in new_answer
    assert "Компиляция в данном режиме — unknown" in new_answer
    assert "не рекомендуется с 8.3.26" in search_syntax(
        registry, "ПолучитьДанные", "Новая", kind="method"
    )

    registry.configurations["Новая"].config.platform = ""
    unknown_platform = get_syntax(registry, "ЗаписьАрхива.ПолучитьДанные", "Новая")
    assert "версия платформы неизвестна" in unknown_platform
    assert "справка сопоставлена по версии платформы" not in unknown_platform
    assert "Не рекомендуется использовать" not in unknown_platform
    assert "Рекомендуемая замена" not in unknown_platform
    assert "не рекомендуется с 8.3.26" not in search_syntax(
        registry, "ПолучитьДанные", "Новая", kind="method"
    )

    merged = registry.syntax.syntax
    saved = save_syntax(merged, incoming / "merged.json.gz")
    restored = load_syntax(saved)
    item = restored.items[OLD_ID]
    assert [entry.platform for entry in item.deprecations] == ["8.3.26.15"]
    assert item.deprecations[0].replacements[0].target_id == NEW_ID


def test_замена_переносится_если_устаревшая_страница_есть_только_в_старой_справке():
    old = SyntaxIndex(platforms=["8.3.26.15"])
    item = parse_page(OLD_ID + ".html", _page(deprecated=True))
    target = parse_page(
        NEW_ID + ".html",
        ('<h1 class="V8SH_pagetitle">ЗаписьФайлаАрхива.ПолучитьДанные</h1>'
         '<p class="V8SH_title">ЗаписьФайлаАрхива (ArchiveFileWriter)</p>'
         '<p class="V8SH_heading">ПолучитьДанные (GetData)</p>').encode(),
    )
    assert item is not None and target is not None
    item.deprecations[0].platform = "8.3.26.15"
    old.add(item)
    old.add(target)
    new = SyntaxIndex(platforms=["8.3.27.2130"])
    new.add(target)

    merged = merge_syntax([new, old])
    warning = merged.items[OLD_ID].deprecations[0]
    assert warning.platform == "8.3.26.15"
    assert warning.replacements[0].target_id == NEW_ID
