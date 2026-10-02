"""Старт владельца дочищает осиротевшие каталоги кода в modules/ и extensions/.

`.<имя>.retired-*` остаётся, если процесс погиб между отставкой каталога в
`remove` и его `rmtree`; `.<имя>.tmp-*` — от рокировки прежнего приёма кода.
Раньше их подбирал следующий `add_modules`, которого больше нет, и каталоги
копились до ручной уборки.
"""

from __future__ import annotations

from pathlib import Path

from mcp1c.registry import Registry


def _каталог(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "Module.bsl").write_text("Процедура А()\nКонецПроцедуры\n", encoding="utf-8")
    return path


def _сироты(data: Path) -> dict[str, Path]:
    return {
        "modules_retired": _каталог(data / "modules" / ".Розница.retired-abc"),
        "modules_tmp": _каталог(data / "modules" / ".Розница.tmp-xyz"),
        "ext_retired": _каталог(data / "extensions" / "Розница" / ".Доп.retired-1"),
        "ext_tmp": _каталог(data / "extensions" / "Розница" / ".Доп.tmp-2"),
    }


def _сохраняемые(data: Path) -> dict[str, Path]:
    return {
        # Отставленная копия после отказа отката — решение человека.
        "old": _каталог(data / "modules" / ".Розница.old-tok"),
        "живой": _каталог(data / "modules" / "Розница"),
        "живое_расширение": _каталог(data / "extensions" / "Розница" / "Доп"),
        # Похоже по имени, но это файл, а не каталог-сирота.
        "файл": data / "modules" / ".заметка.tmp-1",
    }


def test_старт_владельца_удаляет_retired_и_tmp_каталоги_кода(tmp_path):
    data = tmp_path / "data"
    сироты = _сироты(data)
    сохраняемые = _сохраняемые(data)
    сохраняемые["файл"].write_text("x", encoding="utf-8")

    Registry(data).startup()

    assert [имя for имя, путь in сироты.items() if путь.exists()] == []
    assert [имя for имя, путь in сохраняемые.items() if not путь.exists()] == []
