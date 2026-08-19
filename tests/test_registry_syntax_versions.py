"""Справки нескольких версий в одном реестре.

Справка описывает только свою версию, поэтому на конфигурации 8.3.5 свежая
справка ошибается в 730 местах — измерено на настоящих файлах. Реестр держит
разобранную справку каждой версии отдельно и собирает из них слитый вид.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp1c.registry import Registry, RegistryError
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem, SyntaxVariant

from conftest import build_configuration, write_export


def справка(directory, platform: str, *, имена: tuple[str, ...], сигнатура: str = ""):
    """Разобранная справка версии — то, что реестр принимает вместо `.hbk`."""
    directory.mkdir(parents=True, exist_ok=True)
    index = SyntaxIndex(platforms=[platform] if platform else [], source="test")
    for имя in имена:
        item = SyntaxItem(
            id=f"objects/Глобальный контекст/methods/{имя}",
            kind="method",
            name_ru=имя,
            parent_ru="Глобальный контекст",
            description=f"Описание {имя}",
        )
        if сигнатура:
            item.variants = [SyntaxVariant(signature=сигнатура)]
        index.add(item)
    имя_файла = f"{platform or 'без-версии'}.json.gz"
    return save_syntax(index, directory / имя_файла)


def test_справки_разных_версий_живут_рядом(tmp_path):
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"

    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти", "КаноническаяЗаписьXML")))
    registry.add_syntax(справка(incoming, "8.3.27.2130", имена=("Найти", "СтрНайти")))

    assert sorted(registry.sources) == ["syntax-8.3.27.2130", "syntax-8.3.5.1570"]
    имена = {item.name_ru for item in registry.syntax.syntax.items.values()}
    assert имена == {"Найти", "СтрНайти", "КаноническаяЗаписьXML"}


def test_элемент_только_из_старой_справки_ищется(tmp_path):
    """Слитый вид должен попасть в поисковый индекс, а не остаться в модели."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"

    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("КаноническаяЗаписьXML",)))
    registry.add_syntax(справка(incoming, "8.3.27.2130", имена=("СтрНайти",)))

    hits = registry.syntax.index.search("КаноническаяЗаписьXML", limit=5)
    assert [hit.doc.payload.name_ru for hit in hits][:1] == ["КаноническаяЗаписьXML"]


def test_справка_той_же_версии_заменяет_прежнюю(tmp_path):
    """Перезагрузка той же версии — это исправление, а не вторая справка."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"

    registry.add_syntax(справка(incoming, "8.3.27.2130", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.27.2130", имена=("Найти", "СтрНайти")))

    assert sorted(registry.sources) == ["syntax-8.3.27.2130"]
    имена = {item.name_ru for item in registry.syntax.syntax.items.values()}
    assert имена == {"Найти", "СтрНайти"}


def test_справка_без_версии_не_принимается(tmp_path):
    """В справке 8.3.5 нет ни одной отметки «начиная с версии» — 0 страниц из
    18 936. Вывести версию из данных нечем, а без неё границы `until`
    выставить не по чему."""
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError, match="[Вв]ерси"):
        registry.add_syntax(справка(tmp_path / "incoming", "", имена=("Найти",)))

    assert registry.syntax is None
    assert registry.sources == {}


def test_удаление_справки_пересобирает_слитый_вид(tmp_path):
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("КаноническаяЗаписьXML",)))
    registry.add_syntax(справка(incoming, "8.3.27.2130", имена=("СтрНайти",)))

    registry.remove("syntax-8.3.5.1570")

    имена = {item.name_ru for item in registry.syntax.syntax.items.values()}
    assert имена == {"СтрНайти"}
    assert registry.syntax.syntax.platforms == ["8.3.27.2130"]


def test_конфигурация_не_видит_элемент_удалённый_до_её_версии(tmp_path):
    """Раньше фильтр включался только когда справка новее конфигурации: у
    одной справки других поводов не было. В слитом виде появились элементы,
    которых в версии конфигурации уже нет, — фильтровать нужно всегда."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("КаноническаяЗаписьXML",)))
    registry.add_syntax(справка(incoming, "8.3.27.2130", имена=("СтрНайти",)))
    registry.add_configuration(write_export(incoming, build_configuration()))

    context = registry.resolve("ТестоваяКонфигурация")
    keep = context.syntax_filter()
    видно = {
        item.name_ru
        for item in registry.syntax.syntax.items.values()
        if keep(item)
    }

    # Конфигурация на 8.3.23: `КаноническаяЗаписьXML` жила до 8.3.5.
    assert видно == {"СтрНайти"}


def test_соотношение_версий_считается_по_наличию_справки_нужного_релиза(tmp_path):
    """Конфигурация на 8.3.5, загружены справки 8.3.5 и 8.3.23.

    Соотношение считалось по самой свежей справке, и выходило «справка новее» —
    с оговоркой про скрытые элементы. Но справка этой версии есть, ответ по ней
    точный, и оговорка тут только сбивает.
    """
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("Найти", "СтрНайти")))
    config = build_configuration()
    config.platform = "8.3.5.1570"
    registry.add_configuration(write_export(incoming, config))

    context = registry.resolve(config.name)

    assert context.syntax_relation == "exact"
    assert context.notes(critical_only=True) == []


def test_фильтр_работает_и_когда_справка_ровно_этой_версии(tmp_path):
    """Прежде фильтр включался только для справки новее конфигурации.

    Со слитым видом этого мало: конфигурация 8.3.27 при загруженной справке
    8.3.27 получает соотношение «точно», а в индексе лежит `КаноническаяЗаписьXML`
    из справки 8.3.5 — метод, которого в 8.3.27 нет.
    """
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("КаноническаяЗаписьXML",)))
    registry.add_syntax(справка(incoming, "8.3.27.2130", имена=("СтрНайти",)))
    config = build_configuration()
    config.platform = "8.3.27.2130"
    registry.add_configuration(write_export(incoming, config))

    context = registry.resolve(config.name)
    keep = context.syntax_filter()

    assert context.syntax_relation == "exact"
    видно = {item.name_ru for item in registry.syntax.syntax.items.values() if keep(item)}
    assert видно == {"СтрНайти"}


def test_фильтр_работает_и_когда_справка_старее_конфигурации(tmp_path):
    """Конфигурация новее всех загруженных справок. Про элемент, пропавший
    между ними, известно, что он мёртв, — показывать его нельзя и здесь."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("КаноническаяЗаписьXML",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("СтрНайти",)))
    config = build_configuration()
    config.platform = "8.3.27.2130"
    registry.add_configuration(write_export(incoming, config))

    context = registry.resolve(config.name)
    keep = context.syntax_filter()

    assert context.syntax_relation == "older"
    видно = {item.name_ru for item in registry.syntax.syntax.items.values() if keep(item)}
    assert видно == {"СтрНайти"}


def test_инструменты_отвечают_пока_идёт_сборка_слитого_вида(tmp_path, monkeypatch):
    """Разбор и сборка идут в фоне, а инструменты обязаны отвечать всё это
    время по уже загруженному. Держать замок на дорогой работе — значит
    остановить сервер на секунду при каждой загрузке справки."""
    import threading

    from mcp1c import registry as registry_module

    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_configuration(write_export(incoming, build_configuration()))

    сборка_началась = threading.Event()
    отпустить = threading.Event()
    настоящее_слияние = registry_module.merge_syntax

    def медленное_слияние(indexes):
        сборка_началась.set()
        assert отпустить.wait(timeout=5), "основной поток не дошёл до resolve"
        return настоящее_слияние(indexes)

    monkeypatch.setattr(registry_module, "merge_syntax", медленное_слияние)

    загрузка = threading.Thread(
        target=registry.add_syntax,
        args=(справка(incoming, "8.3.27.2130", имена=("СтрНайти",)),),
    )
    загрузка.start()
    try:
        assert сборка_началась.wait(timeout=5), "сборка не началась"
        # Замок свободен: инструмент отвечает по уже загруженному.
        context = registry.resolve("ТестоваяКонфигурация")
        assert context.syntax is not None
    finally:
        отпустить.set()
        загрузка.join(timeout=10)

    assert registry.syntax.syntax.platforms == ["8.3.5.1570", "8.3.27.2130"]


def test_восстановление_собирает_слитый_вид_один_раз(tmp_path):
    """`restore` поднимает источники по одному. Пересборка на каждом означает
    квадрат работы: при пяти справках — пятнадцать сборок вместо пяти."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("СтрНайти",)))
    registry.add_syntax(справка(incoming, "8.3.27.2130", имена=("СтрРазделить",)))
    registry.save()

    поднятый = Registry(tmp_path / "data")
    сборок = 0
    настоящая = поднятый._prepare_syntax

    def считать(versions, preloaded=None, *args, **kwargs):
        # `*args, **kwargs` — задача 3 добавила `_prepare_syntax` параметры
        # для языка запросов (`query_source`, `preloaded_query`); подмена
        # считает вызовы, а не проверяет сигнатуру, поэтому пробрасывает их
        # как есть, не называя по имени.
        nonlocal сборок
        сборок += 1
        return настоящая(versions, preloaded, *args, **kwargs)

    поднятый._prepare_syntax = считать
    поднятый.restore()

    assert сборок == 1
    assert поднятый.syntax.syntax.platforms == ["8.3.5.1570", "8.3.23.1997", "8.3.27.2130"]


def test_повторный_старт_не_перечитывает_справки_версий(tmp_path, monkeypatch):
    """Слитый вид — производное, и он кэшируется как остальные производные.

    Иначе каждый старт заново поднимает с диска разобранную справку каждой
    версии и сливает их: на трёх справках это удваивало старт.
    """
    from mcp1c import registry as registry_module

    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("Найти", "СтрНайти")))
    registry.save()

    подъёмов = 0
    настоящий = registry_module.load_syntax

    def считать(path):
        nonlocal подъёмов
        подъёмов += 1
        return настоящий(path)

    monkeypatch.setattr(registry_module, "load_syntax", считать)
    поднятый = Registry(tmp_path / "data")
    поднятый.restore()

    имена = {item.name_ru for item in поднятый.syntax.syntax.items.values()}
    assert имена == {"Найти", "СтрНайти"}
    # Два подъёма — приём самих источников, третий — готовый слитый вид из
    # кэша. Без кэша их было бы четыре: слияние перечитывало бы обе справки.
    assert подъёмов == 3


def test_уборка_не_сносит_кэш_слитого_вида(tmp_path):
    """Уборка сносит разобранные справки, которых не заявил ни один источник.

    Слитый вид не заявлен никем — он производное от всего набора, — и под это
    правило попадал бы каждый старт, обесценивая кэш.
    """
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("СтрНайти",)))
    слитые = sorted((tmp_path / "data/index/syntax").glob("merged-*.json.gz"))
    assert слитые, "кэш слитого вида не сохранён"

    убрано = registry.sweep_syntax()

    assert убрано == []
    assert слитые[0].exists()


def test_кэш_слитого_вида_обесценивается_при_смене_кода(tmp_path, monkeypatch):
    """Правило проекта: производное сверяется с отпечатком кода, а не только с
    исходником. Слияние правится часто, и кэш, переживший правку, будет тихо
    отдавать результат прежней логики — ровно ту ошибку, которую починили."""
    from mcp1c import index_cache

    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("СтрНайти",)))
    registry.save()
    было = sorted(p.name for p in (tmp_path / "data/index/syntax").glob("merged-*"))

    monkeypatch.setattr(index_cache, "_code_digest", lambda: "другой-код")
    поднятый = Registry(tmp_path / "data")
    поднятый.restore()

    стало = sorted(p.name for p in (tmp_path / "data/index/syntax").glob("merged-*"))
    assert стало != было, "имя кэша не зависит от кода — правка слияния его не обесценит"
    имена = {item.name_ru for item in поднятый.syntax.syntax.items.values()}
    assert имена == {"Найти", "СтрНайти"}


def test_испорченная_справка_версии_не_роняет_восстановление(tmp_path):
    """Разобранная справка теперь читается с диска при каждой сборке. Файл
    может пропасть или побиться — старт из-за этого падать не должен: то же
    правило, по которому отдельный источник не роняет запуск."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("СтрНайти",)))
    registry.save()
    for path in (tmp_path / "data/index/syntax").glob("merged-*"):
        path.unlink()
    Path(registry.sources["syntax-8.3.5.1570"].stored_path).write_bytes(b"broken, not gzip")

    поднятый = Registry(tmp_path / "data")
    проблемы = поднятый.restore()

    assert проблемы, "поломка должна быть названа, а не проглочена"
    assert поднятый.syntax is not None
    имена = {item.name_ru for item in поднятый.syntax.syntax.items.values()}
    assert имена == {"СтрНайти"}


def test_сборка_переживает_поломку_одной_справки(tmp_path):
    """Справка версии читается с диска при каждой сборке. Если файл побился
    уже после загрузки, слитый вид собирается из оставшихся, а сломанный
    источник помечается ошибкой — иначе он числится загруженным, а его
    содержимого в ответах нет."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("СтрНайти",)))
    for path in (tmp_path / "data/index/syntax").glob("merged-*"):
        path.unlink()
    Path(registry.sources["syntax-8.3.5.1570"].stored_path).write_bytes(b"broken")

    проблемы = registry._apply_syntax(dict(registry.syntax_versions))

    assert проблемы and "syntax-8.3.5.1570" in проблемы[0]
    имена = {item.name_ru for item in registry.syntax.syntax.items.values()}
    assert имена == {"СтрНайти"}
    assert registry.sources["syntax-8.3.5.1570"].status == "error"


def test_версионные_факты_переживают_перезапуск(tmp_path):
    """Слитый вид кэшируется на диске, и через кэш проходят обе новые вещи —
    граница `until` и версионные факты. Если хранение их теряет, после
    перезапуска сервер снова отдаёт сигнатуру свежей справки всем подряд:
    ровно то, ради чего слияние и делалось."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(
        справка(incoming, "8.3.5.1570", имена=("ОткрытьФайл",), сигнатура="ОткрытьФайл(Имя)")
    )
    registry.add_syntax(
        справка(
            incoming,
            "8.3.27.2130",
            имена=("ОткрытьФайл", "СтрНайти"),
            сигнатура="ОткрытьФайл(Имя, Кодировка)",
        )
    )
    registry.save()

    поднятый = Registry(tmp_path / "data")
    поднятый.restore()

    слитый = поднятый.syntax.syntax
    элемент = next(i for i in слитый.items.values() if i.name_ru == "ОткрытьФайл")
    assert слитый.facts_for(элемент, "8.3.5.1570").signature == "ОткрытьФайл(Имя)"
    пропавший = next(i for i in слитый.items.values() if i.name_ru == "СтрНайти")
    assert пропавший.until == ""


def test_версия_берётся_из_имени_файла(tmp_path):
    """Файл из каталога установки называется `shcntx_ru.hbk` и версии не несёт;
    имя вида `syntax-8.3.5.1570` её несёт, и это дешевле ручного ввода."""
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    путь = справка(incoming, "8.3.5.1570", имена=("Найти",))
    без_версии_внутри = путь.parent / "syntax-8.3.5.1570.json.gz"
    путь.rename(без_версии_внутри)

    source = registry.add_syntax(без_версии_внутри)

    assert source.platform == "8.3.5.1570"


def test_список_называет_справку_той_версии_по_которой_отвечает(tmp_path):
    """`list_configurations` называл справку по объединённому источнику.

    Конфигурация 8.3.5 при загруженных справках 8.3.5 и 8.3.23 получала строку
    «справка 8.3.23.1997 — версия совпадает с конфигурацией». Соотношение
    верное (справка её релиза есть, ответ по ней точный), а номер — чужой, и
    фраза противоречит сама себе. Читающий делает вывод, что фильтр по версии
    не работает, — то есть строка обесценивает ровно ту возможность, ради
    которой слитый индекс и сделан.
    """
    from mcp1c import tools

    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    registry.add_syntax(справка(incoming, "8.3.23.1997", имена=("Найти", "СтрНайти")))
    config = build_configuration()
    config.platform = "8.3.5.1570"
    registry.add_configuration(write_export(incoming, config))

    row = next(r for r in registry.overview() if r["name"] == config.name)

    assert row["syntax_relation"] == "exact"
    assert row["syntax_platform"] == "8.3.5.1570"
    assert "справка 8.3.5.1570 — версия совпадает" in tools.list_configurations(registry)
