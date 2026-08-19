"""Соединение языка запросов с элементами платформы в поиске.

Слитый вид (`LoadedSyntax.syntax`) остаётся только справкой платформы — этого
требует `merge_syntax` (падает `ValueError` на индексе без версии). Язык
запросов подмешивается позже и только в поисковый индекс и таблицу имён:
агент задаёт один вопрос и не должен знать, в каком из двух источников искать.
"""

from __future__ import annotations

from mcp1c.registry import Registry

from conftest import query_hbk_stub, write_syntax, build_configuration, write_export


def test_поиск_находит_и_платформу_и_язык_запросов(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.27.2130"))

    hits = registry.syntax.index.search("соединение", limit=10)

    assert any(h.doc.payload.id == "query/LEFTJOIN" for h in hits)


def test_точное_имя_находит_элемент_языка_запросов(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.27.2130"))

    найдено = registry.syntax.find_exact("конецпериода")

    assert [i.id for i in найдено] == ["query/ENDOFPERIOD"]


def test_слитый_вид_остаётся_только_платформой(tmp_path):
    """Ключевое требование задачи: язык запросов не идёт в `merge_syntax`.

    Проверяем это не по побочному эффекту (поиск находит элемент), а прямо —
    элемента языка запросов не должно быть среди `syntax.syntax.items`, куда
    смотрит `syntax_coverage()`, `_save_merged` и фильтр версии платформы.
    """
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.27.2130"))

    assert "query/ENDOFPERIOD" not in registry.syntax.syntax.items
    assert registry.syntax.query is not None
    assert "query/ENDOFPERIOD" in registry.syntax.query.items


def test_язык_запросов_поднимается_после_перезапуска(tmp_path):
    """Дефект такого рода уже был: индекс терял факты при чтении с диска."""
    первый = Registry(tmp_path / "data")
    первый.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    первый.save()

    второй = Registry(tmp_path / "data")
    второй.startup()

    assert второй.query_source is not None
    assert второй.syntax is not None
    assert второй.syntax.find_exact("конецпериода")


def test_кэш_поиска_переживает_уборку_на_старте_без_справок_платформы(tmp_path):
    """Без единой справки платформы `LoadedSyntax.source` — сам источник
    языка запросов, и кэш поиска/таблицы имён ложится под его id
    (`syntax-query.*`). `CACHE_KINDS` обязан знать о виде `KIND_QUERY` —
    иначе `sweep_syntax`... то есть `index_cache.sweep`, вызываемый следом за
    `restore()` в `startup()`, счёл бы этот кэш ничьим и снёс его же в
    момент, когда тот только что был построен.
    """
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.startup()

    кэш = sorted((registry.cache_dir).glob("syntax-query.*"))
    assert кэш, "кэш поиска по языку запросов не был построен"

    registry.startup()

    assert sorted((registry.cache_dir).glob("syntax-query.*")) == кэш


def test_язык_запросов_подхватывается_сразу_без_перезапуска(tmp_path):
    """Добавление языка запросов к уже собранному индексу справок обязано
    отразиться сразу же — иначе `_fingerprint` не заметил правки и поднял
    прежний кэш, хотя источники в реестре уже другие."""
    registry = Registry(tmp_path / "data")
    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.27.2130"))
    assert registry.syntax.find_exact("конецпериода") == []

    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))

    найдено = registry.syntax.find_exact("конецпериода")
    assert [i.id for i in найдено] == ["query/ENDOFPERIOD"]


def test_элемент_языка_запросов_доступен_на_старой_платформе(tmp_path):
    """Фильтр версии платформы элементы языка запросов не трогает: у них нет
    `since`, а `until` им проставляет только слияние, в котором они не
    участвуют."""
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.27.2130"))
    config = build_configuration()
    config.platform = "8.3.5.1570"
    registry.add_configuration(write_export(tmp_path / "incoming", config))

    item = registry.syntax.find_exact("конецпериода")[0]

    assert item.available_in("8.3.5.1570")
    assert item.until == ""


def test_удаление_языка_запросов_убирает_его_из_поиска_без_перезапуска(tmp_path):
    """Удаление источника обязано пересобрать `syntax` сразу же — иначе его
    элементы останутся находимыми до перезапуска, хотя источника в реестре
    уже нет."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    запрос = registry.add_syntax(query_hbk_stub(incoming))
    registry.add_syntax(write_syntax(incoming, platform="8.3.27.2130"))
    assert registry.syntax.find_exact("конецпериода")

    registry.remove(запрос.id)

    assert registry.syntax.find_exact("конецпериода") == []


def test_язык_запросов_не_мешает_слиянию_нескольких_версий(tmp_path):
    """Язык запросов добавлен первым — сборка слитого вида двух версий
    платформы после этого не должна ломаться и не должна терять ни одну из
    них.

    Единственный тест в файле, где `merge_syntax` реально вызывается (при
    одной справке версии слияние не выполняется вовсе) — поэтому здесь же, а
    не в тесте с одной справкой, проверяется главный инвариант: слияние
    справок платформы не задевает элемент языка запросов вовсе — ни `until`
    ему не проставляет, ни из старой версии не прячет."""
    registry = Registry(tmp_path / "data")
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming"))
    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.27.2130"))
    registry.add_syntax(write_syntax(tmp_path / "incoming", platform="8.3.5.1570"))

    assert sorted(registry.syntax.syntax.platforms) == ["8.3.27.2130", "8.3.5.1570"]
    найдено = registry.syntax.find_exact("конецпериода")
    assert [i.id for i in найдено] == ["query/ENDOFPERIOD"]

    элемент = найдено[0]
    assert элемент.until == ""
    assert элемент.available_in("8.3.5.1570")
