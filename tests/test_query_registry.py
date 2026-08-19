"""Приём справки по языку запросов в реестре.

Источник получает свой вид `KIND_QUERY` и живёт отдельно от `syntax_versions`:
`merge_syntax` падает `ValueError` на индексе без версии (`syntax_merge.py`),
а `syntax_coverage()` считал бы источник с пустой версией вечно «лишней»
справкой. При этом источник обязан остаться в `self.sources` — иначе
`sweep_syntax` снесёт его разобранный индекс как никем не заявленный.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp1c.registry import KIND_QUERY, Registry, RegistryError
from mcp1c.store import save_syntax
from mcp1c.syntax_model import KIND_QUERY_FUNCTION, KIND_QUERY_KEYWORD, SyntaxIndex, SyntaxItem

from conftest import query_hbk_stub, write_syntax, write_syntax_without_platform


def test_язык_запросов_принимается_без_версии(tmp_path):
    registry = Registry(tmp_path / "data")

    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))

    assert registry.query_source is not None
    # В `syntax_versions` его быть не должно: там живут справки платформы,
    # и слияние требует у каждой версию.
    assert registry.syntax_versions == {}
    assert registry.syntax_coverage()["unused"] == []


def test_справка_платформы_без_версии_по_прежнему_отвергается(tmp_path):
    """Послабление для языка запросов не должно ослабить проверку справки."""
    registry = Registry(tmp_path / "data")
    путь = write_syntax_without_platform(tmp_path / "incoming")

    with pytest.raises(RegistryError, match="версию платформы"):
        registry.add_syntax(путь)


def test_повторная_загрузка_заменяет_прежний_экземпляр(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming2"))

    assert len([s for s in registry.sources.values() if s.kind == KIND_QUERY]) == 1


def _query_index_без_текстов(directory: Path) -> Path:
    """Скелет языка запросов без текстов — то, что даёт `shquery_root.hbk`.

    По устройству ветка та же, что и защита от `shcntx_root.hbk`
    (`tests/test_registry_syntax.py::_syntax_without_descriptions`): дерево
    страниц и английские идентификаторы на месте, текстов нет вовсе. Вид
    элементов — честный `query_*`, чтобы приём дошёл именно до проверки
    описаний, а не отвалился раньше как «не язык запросов».
    """
    directory.mkdir(parents=True, exist_ok=True)
    index = SyntaxIndex(platforms=[], source="query-root", language="ru")
    index.add(SyntaxItem(id="query/ENDOFPERIOD", kind=KIND_QUERY_FUNCTION, name_en="ENDOFPERIOD"))
    index.add(SyntaxItem(id="query/LEFTJOIN", kind=KIND_QUERY_KEYWORD, name_en="LEFTJOIN"))
    return save_syntax(index, directory / "скелет.json.gz")


def test_языконезависимый_язык_запросов_не_принимается(tmp_path):
    """Тот же класс дефекта, за который проект уже платил на `shcntx_root.hbk`."""
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError, match="shquery_root.hbk"):
        registry.add_syntax(_query_index_без_текстов(tmp_path / "incoming"))

    assert registry.query_source is None
    assert registry.sources == {}


def test_удаление_источника_языка_запросов(tmp_path):
    """`remove()` обязан снять `query_source`, а не только запись в `sources`.

    Ветка `if source.kind == KIND_QUERY` в `remove()` иначе ничем не
    проверена: без неё `self.query_source` остался бы висеть на уже
    удалённом источнике (`self.sources.pop` отработал бы, а ветка
    `syntax_versions.pop(...) is None` вышла бы раньше, чем до
    `query_source` дошли бы руки).
    """
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    запрос = registry.add_syntax(query_hbk_stub(incoming))
    платформа = registry.add_syntax(write_syntax(incoming, platform="8.3.27.2130"))

    registry.remove(запрос.id)

    assert registry.query_source is None
    assert запрос.id not in registry.sources
    # Справка платформы удалением языка запросов не задета.
    assert платформа.id in registry.sources
    assert registry.syntax is not None
    assert registry.syntax.syntax.platforms == ["8.3.27.2130"]


def test_удаление_справки_платформы_не_трогает_язык_запросов(tmp_path):
    """Тот же сценарий в обратную сторону — снятие справки не должна задевать источник запросов.

    Справок платформы не осталось — слитый вид (`registry.syntax.syntax`)
    пустеет, — но `registry.syntax` в `None` не превращается: язык запросов
    самостоятельный источник (см. `docs/query-language-design.md`, «Работа
    без справки платформы») и обязан продолжать находиться сам по себе.
    """
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    запрос = registry.add_syntax(query_hbk_stub(incoming))
    платформа = registry.add_syntax(write_syntax(incoming, platform="8.3.27.2130"))

    registry.remove(платформа.id)

    assert registry.query_source is not None
    assert registry.query_source.id == запрос.id
    assert registry.syntax is not None
    assert registry.syntax.syntax.items == {}
    assert registry.syntax.find_exact("конецпериода")


def test_язык_запросов_не_ломает_сборку_слитого_вида(tmp_path):
    """`merge_syntax` падает на индексе без версии — источник туда попадать не должен."""
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))

    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.27.2130"))
    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.5.1570"))

    assert registry.syntax is not None
    assert sorted(registry.syntax.syntax.platforms) == ["8.3.27.2130", "8.3.5.1570"]


def test_источник_остаётся_после_уборки_на_старте(tmp_path):
    """`sweep_syntax` разрешает файлы `self.sources` по виду и `stored_path`.

    Источник языка запросов регистрируется в `self.sources`, но не в
    `syntax_versions` — если уборка проверяет разрешённые пути только по виду
    `syntax`, разобранный индекс языка запросов будет сметён как ничей.
    """
    data = tmp_path / "data"
    registry = Registry(data)
    # Как и в `test_syntax_sweep.py`: разобранный индекс кладём сразу туда,
    # где его хранит сам реестр — `.gz` реестр не копирует, только берёт.
    свой = query_hbk_stub(data / "index" / "syntax")
    registry.add_syntax(свой)
    registry.save()

    assert свой.exists()

    заново = Registry(tmp_path / "data")
    заново.startup()

    assert свой.exists()
    assert заново.query_source is not None


def test_восстановление_поднимает_источник_по_сохранённому_виду(tmp_path):
    """`restore()` передаёт `known_kind`, а не угадывает вид заново.

    На честном языке запросов (все элементы `query_*`, непустой набор)
    угадывание по содержимому и сохранённый вид всегда совпадают — тест на
    такой фикстуре прошёл бы одинаково что с `known_kind`, что без него, то
    есть не мог бы упасть при регрессе. Различие наблюдаемо только на
    испорченном индексе: один элемент с посторонним `kind` («method» вместо
    `query_*`) — угадывание по содержимому даёт `False` («это не язык
    запросов», версии в нём нет — отказ), а сохранённый `source.kind ==
    "query"` из `registry.json` всё равно приводит к успеху.
    """
    data = tmp_path / "data"
    registry = Registry(data)
    # Как и в `test_источник_остаётся_после_уборки_на_старте`: `.gz` реестр
    # не копирует, только берёт по месту — значит подменять на диске нужно
    # именно этот файл, а не что-то в `incoming/`, которое `stored_path`
    # больше не назовёт.
    испорченный = query_hbk_stub(data / "index" / "syntax")
    registry.add_syntax(испорченный)
    registry.save()

    # Подменяем разобранный индекс на диске: угадывание по содержимому
    # (`item.kind.startswith("query_")` у всех элементов) на нём ошибается,
    # хотя `registry.json` по-прежнему помнит источник как `kind="query"`.
    искажённый = SyntaxIndex(platforms=[], source="query", language="ru")
    искажённый.add(SyntaxItem(id="query/x", kind="method", name_ru="Х", description="текст"))
    save_syntax(искажённый, испорченный)

    # Контроль: без `known_kind` угадывание по этому же файлу действительно
    # ошибается и отвергает его — иначе тест ничего не доказывает.
    with pytest.raises(RegistryError, match="версию платформы"):
        Registry(data).add_syntax(испорченный)

    заново = Registry(data)
    заново.restore()

    assert заново.query_source is not None
    assert заново.syntax_versions == {}


def test_непривязанные_поисковые_ключи_названы_при_загрузке(tmp_path):
    """Слой ключей привязан к страницам по идентификатору, и расхождение молчать не должно.

    Заглушка справки — три страницы вместо 128, то есть ровно тот случай,
    ради которого отчёт и заведён: ключи писались под полный набор, а пришёл
    неполный. Без сообщения поиск по языку запросов просто работал бы хуже
    заявленного, и понять почему было бы нечем.
    """
    registry = Registry(tmp_path / "data")

    source = registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))

    предупреждения = [w for w in source.warnings if "поисковых ключей" in w]
    assert len(предупреждения) == 1
    assert "потеряно" in предупреждения[0]


def test_отчёт_о_ключах_переживает_перезапуск(tmp_path):
    """Что переживает перезапуск — проверяется перезапуском (правило CHANGELOG).

    `restore()` заново прогоняет `add_syntax`, поэтому отчёт обязан появиться
    и на поднятом с диска источнике, а не только при первой загрузке.
    """
    data = tmp_path / "data"
    registry = Registry(data)
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.save()

    заново = Registry(data)
    заново.restore()

    assert заново.query_source is not None
    assert any("поисковых ключей" in w for w in заново.query_source.warnings)
