"""Reload без изменений справок не пересобирает слитый вид.

`POST /admin/reload` вызывает `startup()` → `restore()`: раньше каждая справка
версии читалась с диска заново (`add_syntax`), а слитый вид поднимался из
кэша и обрастал поисковым индексом, словарём имён и таблицами. На трёх
настоящих справках это 2,4 с и основная доля пика RSS reload, хотя справки
не менялись. Наблюдаемое: тот же объект слитого вида и ни одного чтения
разобранной справки; после изменения — пересборка.
"""

from __future__ import annotations

import os

import pytest

from mcp1c import registry as registry_module
from mcp1c.registry import Registry
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxItem

from conftest import build_syntax, write_syntax


@pytest.fixture
def чтения(monkeypatch):
    """Счётчик чтений разобранных справок и слитого вида с диска."""
    вызовы: list[str] = []
    original = registry_module.load_syntax

    def посчитать(path):
        вызовы.append(os.fspath(path))
        return original(path)

    monkeypatch.setattr(registry_module, "load_syntax", посчитать)
    return вызовы


def _две_справки(tmp_path) -> Registry:
    registry = Registry(tmp_path / "data")
    registry.add_syntax(write_syntax(tmp_path / "s1", "8.3.99.1"))
    registry.add_syntax(write_syntax(tmp_path / "s2", "8.3.100.1"))
    registry.startup()
    return registry


def test_reload_без_изменений_не_пересобирает_слитый_вид(tmp_path, чтения):
    registry = _две_справки(tmp_path)
    было = registry.syntax
    чтения.clear()

    registry.startup()

    assert registry.syntax is было
    assert чтения == []
    assert sorted(registry.syntax_versions) == ["syntax-8.3.100.1", "syntax-8.3.99.1"]
    assert registry.syntax.syntax.platforms == ["8.3.99.1", "8.3.100.1"]


def test_reload_после_замены_разобранной_справки_пересобирает(tmp_path, чтения):
    """Другой процесс переразобрал ту же версию: SHA исходника прежний,
    а файл индекса заменён — reload обязан увидеть новое содержимое."""
    registry = _две_справки(tmp_path)
    было = registry.syntax
    stored = registry._absolute(registry.syntax_versions["syntax-8.3.100.1"].stored_path)
    index = build_syntax("8.3.100.1")
    index.add(
        SyntaxItem(
            id="method.НоваяФункция",
            kind="method",
            name_ru="НоваяФункция",
            name_en="NewFunction",
            description="Появилась при переразборе",
        )
    )
    замена = stored.with_name("замена.json.gz")
    save_syntax(index, замена)
    os.replace(замена, stored)

    registry.startup()

    assert registry.syntax is not было
    assert registry.syntax.find_exact("НоваяФункция")


def test_снятие_справки_и_следующий_reload(tmp_path, чтения):
    registry = _две_справки(tmp_path)
    было = registry.syntax

    registry.remove("syntax-8.3.100.1")
    после_снятия = registry.syntax
    assert после_снятия is not было
    assert после_снятия.syntax.platforms == ["8.3.99.1"]
    чтения.clear()

    registry.startup()

    assert registry.syntax is после_снятия
    assert чтения == []
    assert sorted(registry.syntax_versions) == ["syntax-8.3.99.1"]


def test_добавление_справки_после_reload_пересобирает(tmp_path):
    registry = _две_справки(tmp_path)
    registry.startup()
    было = registry.syntax

    registry.add_syntax(write_syntax(tmp_path / "s3", "8.3.101.1"))

    assert registry.syntax is not было
    assert registry.syntax.syntax.platforms == ["8.3.99.1", "8.3.100.1", "8.3.101.1"]
    assert registry.syntax.source.id == "syntax-8.3.101.1"


def test_битая_справка_после_reload_не_переиспользуется(tmp_path):
    """Справка, ставшая ошибкой, не залипает в памяти: ремонт файла
    подхватывается следующим reload."""
    registry = _две_справки(tmp_path)
    stored = registry._absolute(registry.syntax_versions["syntax-8.3.100.1"].stored_path)
    исправная = stored.read_bytes()
    stored.write_bytes(b"not gzip")

    registry.startup()
    assert registry.sources["syntax-8.3.100.1"].status != "ready"

    stored.write_bytes(исправная)
    registry.startup()

    assert registry.sources["syntax-8.3.100.1"].status == "ready"
    assert registry.syntax.syntax.platforms == ["8.3.99.1", "8.3.100.1"]


def test_reload_после_переноса_файла_справки_берёт_новый_путь(tmp_path):
    """`mv` сохраняет inode, размер и mtime; если строка реестра указывает на
    новый путь, запись справки должна взять его, а не прежний."""
    import json

    registry = _две_справки(tmp_path)
    source = registry.syntax_versions["syntax-8.3.100.1"]
    old = registry._absolute(source.stored_path)
    new = old.with_name("перенесённая-8.3.100.1.json.gz")
    old.rename(new)
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    for row in payload["sources"]:
        if row["id"] == "syntax-8.3.100.1":
            row["stored_path"] = registry._relative(new)
    registry.registry_path.write_text(json.dumps(payload), encoding="utf-8")

    registry.startup()

    assert registry.syntax_versions["syntax-8.3.100.1"].stored_path == registry._relative(new)
    assert registry.syntax_versions["syntax-8.3.100.1"].status == "ready"
