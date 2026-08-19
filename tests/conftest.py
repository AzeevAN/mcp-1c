"""Общие приспособления для тестов.

Тесты не зависят от содержимого `data/`: проприетарных выгрузок и справки в
репозитории нет, а тест, который без них не запускается, бесполезен.
Всё нужное собирается здесь — маленькое и синтетическое.
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp1c.model import Configuration, Field, MetadataObject, TabularPart
from mcp1c.search import Doc, SearchIndex
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem, SyntaxVariant


# Клиенты, у которых открыт портал: закрываются после теста автоматически.
_ОТКРЫТЫЕ_КЛИЕНТЫ: list[TestClient] = []


def живой_клиент(app) -> TestClient:
    """`TestClient` с одним event loop на весь тест.

    Без `with` starlette поднимает новый blocking portal **на каждый запрос** и
    закрывает его сразу после ответа (`testclient.py`, `_portal_factory`: при
    `self.portal is None` портал живёт ровно один вызов). Дашборд планирует
    фоновый разбор через `asyncio.create_task` — задача попадает на этот
    умирающий loop и отменяется вместе с ним.

    Работу при этом доделывает сырой поток из `run_in_threadpool`: он к отмене
    asyncio нечувствителен. Поэтому локально всё зелёное — поток успевает
    домутировать реестр раньше, чем истечёт опрос. На медленном раннере не
    успевает, и падают разные тесты от прогона к прогону.

    Найдено 2026-08-19 первым же прогоном CI: 3-4 падения из 432 на
    ubuntu-latest при 432 зелёных локально.
    """
    client = TestClient(app)
    client.__enter__()
    _ОТКРЫТЫЕ_КЛИЕНТЫ.append(client)
    return client


@pytest.fixture(autouse=True)
def _закрыть_живых_клиентов():
    """Портал — поток с event loop, и оставлять его открытым нельзя.

    Клиенты создаются внутри тестов хелперами `client_for`, а не фикстурой,
    поэтому закрываются здесь: иначе на 432 тестах накопились бы сотни
    брошенных потоков, конкурирующих за GIL.
    """
    yield
    while _ОТКРЫТЫЕ_КЛИЕНТЫ:
        _ОТКРЫТЫЕ_КЛИЕНТЫ.pop().__exit__(None, None, None)


def состарить(путь: Path) -> Path:
    """Отодвинуть mtime в прошлое — файл считается дописанным.

    Приём не берёт архив, изменённый только что: признак «файл ещё копируется»
    (`incoming.SETTLE_SECONDS`) существует ровно потому, что `cp` полутора
    гигабайт идёт минуты, а файл виден с первой секунды. Тесты создают архивы
    прямо перед проверкой, поэтому возраст назначается явно — ждать по пять
    секунд в каждом тесте было бы нечестной платой.
    """
    давно = time.time() - 3600
    os.utime(путь, (давно, давно))
    return путь


@pytest.fixture
def sample_index() -> SearchIndex:
    """Индекс на трёх документах — достаточно, чтобы поймать порядок выдачи."""
    return SearchIndex(
        [
            Doc(
                id="Справочник.Номенклатура",
                kind="Справочник",
                payload="номенклатура",
                exact_keys=["Справочник.Номенклатура"],
                boost=1.0,
                fields={"name": "Номенклатура", "synonym": "Номенклатура", "kind": "Справочник"},
            ),
            Doc(
                id="Документ.РеализацияТоваровУслуг",
                kind="Документ",
                payload="реализация",
                exact_keys=["Документ.РеализацияТоваровУслуг"],
                boost=1.0,
                fields={
                    "name": "РеализацияТоваровУслуг",
                    "synonym": "Реализация товаров и услуг",
                    "kind": "Документ",
                },
            ),
            Doc(
                id="Обработка.ЗагрузкаНоменклатуры",
                kind="Обработка",
                payload="загрузка",
                exact_keys=["Обработка.ЗагрузкаНоменклатуры"],
                boost=0.3,
                fields={
                    "name": "ЗагрузкаНоменклатуры",
                    "synonym": "Загрузка номенклатуры",
                    "kind": "Обработка",
                },
            ),
        ]
    )


@pytest.fixture
def sample_payloads() -> dict[str, str]:
    return {
        "Справочник.Номенклатура": "номенклатура",
        "Документ.РеализацияТоваровУслуг": "реализация",
        "Обработка.ЗагрузкаНоменклатуры": "загрузка",
    }


def build_configuration(name: str = "ТестоваяКонфигурация") -> Configuration:
    """Конфигурация из двух объектов с реквизитами и табличной частью."""
    config = Configuration(name=name, synonym="Тестовая", version="1.0", platform="8.3.23.1997")
    catalog = MetadataObject(
        full_name="Справочник.Контрагенты",
        kind="Справочник",
        name="Контрагенты",
        synonym="Контрагенты",
        attributes=[
            Field(name="ИНН", synonym="ИНН"),
            Field(name="Телефон", synonym="Номер телефона"),
        ],
        tabular_parts=[
            TabularPart(
                name="КонтактнаяИнформация",
                synonym="Контактная информация",
                attributes=[Field(name="Представление", synonym="Представление")],
            )
        ],
    )
    document = MetadataObject(
        full_name="Документ.РеализацияТоваровУслуг",
        kind="Документ",
        name="РеализацияТоваровУслуг",
        synonym="Реализация товаров и услуг",
        attributes=[Field(name="Контрагент", synonym="Контрагент", types=["Справочник.Контрагенты"])],
    )
    config.objects = {catalog.full_name: catalog, document.full_name: document}
    return config


def write_export(directory: Path, config: Configuration) -> Path:
    """Собрать выгрузку schema v1 (JSON) из модели — реальный вход загрузчика."""
    objects = []
    for obj in config.objects.values():
        objects.append(
            {
                "full_name": obj.full_name,
                "type": obj.kind,
                "name": obj.name,
                "synonym": obj.synonym,
                # Ключ типа в schema v1 — `type`, не `types`. Фикстура писала `types`,
                # загрузчик молча получал пустой список, и рёбра графа через него
                # не проверял ни один тест (найдено 2026-08-18).
                "attributes": [{"name": f.name, "synonym": f.synonym, "type": f.types} for f in obj.attributes],
                "tabular_parts": [
                    {
                        "name": part.name,
                        "synonym": part.synonym,
                        "attributes": [
                            {"name": f.name, "synonym": f.synonym, "type": f.types}
                            for f in part.attributes
                        ],
                    }
                    for part in obj.tabular_parts
                ],
            }
        )

    manifest = {
        "schema_version": "1",
        "format": "json",
        "exporter_version": "test",
        "name": config.name,
        "synonym": config.synonym,
        "version": config.version,
        "platform": config.platform,
        "exported_at": "2026-08-15T00:00:00",
        "objects_total": len(objects),
        "truncated": False,
        "predefined_available": True,
        "files": [{"path": "objects/all.001.json", "type": "Справочник", "count": len(objects)}],
    }

    target = directory / f"СтруктураКонфигурации_{config.name}.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("objects/all.001.json", json.dumps({"objects": objects}, ensure_ascii=False))
    return target


def build_syntax(platform: str = "8.3.99.1") -> SyntaxIndex:
    """Справка из трёх элементов: метод, его член и свойство."""
    index = SyntaxIndex(platforms=[platform], language="ru", source="test")
    index.add(
        SyntaxItem(
            id="method.СтрНайти",
            kind="method",
            name_ru="СтрНайти",
            name_en="StrFind",
            description="Находит вхождение подстроки",
            since="8.3.6",
        )
    )
    index.add(
        SyntaxItem(
            id="object.ЗаписьJSON",
            kind="object",
            name_ru="ЗаписьJSON",
            name_en="JSONWriter",
            description="Запись данных в формате JSON",
            since="8.3.6",
        )
    )
    index.add(
        SyntaxItem(
            id="property.ЗаписьJSON.ЗаписатьНачалоОбъекта",
            kind="method",
            name_ru="ЗаписатьНачалоОбъекта",
            name_en="WriteStartObject",
            parent_ru="ЗаписьJSON",
            parent_en="JSONWriter",
            description="Записывает начало объекта",
        )
    )
    return index


def build_syntax_registry(tmp_path: Path, items: list[SyntaxItem], platform: str):
    """Реестр из одной справки и одной конфигурации на указанной платформе.

    Собирается здесь, а не в каждом наборе: проверок «что сервер отвечает по
    версии конфигурации» уже несколько, и справка для них нужна одна и та же —
    с проставленными вручную `since` и `until`.
    """
    from mcp1c.registry import Registry

    incoming = tmp_path / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    index = SyntaxIndex(platforms=["8.3.27.2130"], source="test")
    for item in items:
        index.add(item)

    registry = Registry(tmp_path / "data")
    registry.add_syntax(save_syntax(index, incoming / "8.3.27.2130.json.gz"))
    config = build_configuration()
    config.platform = platform
    registry.add_configuration(write_export(incoming, config))
    # Записываем `registry.json`: CLI поднимает реестр с диска заново, и без
    # этого он видит пустой каталог.
    registry.save()
    return registry


def write_syntax(directory: Path, platform: str = "8.3.99.1") -> Path:
    """Сохранённый индекс справки — то, что реестр принимает вместо .hbk."""
    directory.mkdir(parents=True, exist_ok=True)
    return save_syntax(build_syntax(platform), directory / f"{platform}.json.gz")


def write_syntax_without_platform(directory: Path) -> Path:
    """Справка платформы, у которой не определена версия.

    Так выглядят старые платформы (в справке 8.3.5 нет ни одной отметки
    «начиная с версии» — версию неоткуда вывести и из данных). Проверка
    версии обязана отвергать такой файл, в отличие от языка запросов, где
    версии нет по устройству формата.
    """
    directory.mkdir(parents=True, exist_ok=True)
    index = build_syntax()
    index.platforms = []
    return save_syntax(index, directory / "без-версии.json.gz")


def build_query_index() -> SyntaxIndex:
    """Справка по языку запросов из трёх страниц: функция, слово, статья."""
    from mcp1c.syntax_model import (
        KIND_QUERY_ARTICLE,
        KIND_QUERY_FUNCTION,
        KIND_QUERY_KEYWORD,
    )

    index = SyntaxIndex(platforms=[], source="query-test", language="ru")
    index.add(
        SyntaxItem(
            id="query/ENDOFPERIOD",
            kind=KIND_QUERY_FUNCTION,
            name_ru="КОНЕЦПЕРИОДА",
            name_en="ENDOFPERIOD",
            description="Возвращает конец периода",
            variants=[SyntaxVariant(signature="КОНЕЦПЕРИОДА (<Дата периода>, <Тип периода>)")],
        )
    )
    index.add(
        SyntaxItem(
            id="query/LEFTJOIN",
            kind=KIND_QUERY_KEYWORD,
            name_ru="Левое внешнее соединение",
            name_en="LEFTJOIN",
            description="ЛЕВОЕ ВНЕШНЕЕ СОЕДИНЕНИЕ включает записи левой таблицы",
        )
    )
    index.add(
        SyntaxItem(
            id="query/hierarchical_totals.html",
            kind=KIND_QUERY_ARTICLE,
            name_ru="Итоги по иерархии",
            name_en="hierarchical_totals",
            description="Первый абзац статьи.\n\n" + "Продолжение. " * 200,
        )
    )
    return index


def query_hbk_stub(directory: Path) -> Path:
    """Разобранный индекс языка запросов на диске — то, что примет реестр.

    Настоящий контейнер 1С в тестах не собрать, поэтому приём должен уметь
    принять и уже разобранный индекс — тем же путём, каким принимается
    разобранная справка платформы (`registry.add_syntax` умеет `.json.gz`).
    """
    directory.mkdir(parents=True, exist_ok=True)
    return save_syntax(build_query_index(), directory / "query-ru.json.gz")


@pytest.fixture(autouse=True)
def чистый_журнал_загрузок():
    """Журнал заданий дашборда живёт в модуле и протекал бы между тестами.

    Он намеренно глобальный — это состояние процесса, а не данных, — поэтому
    изоляцию обеспечивает фикстура, а не устройство модуля.
    """
    from mcp1c import dashboard

    dashboard._JOBS.clear()
    yield
    dashboard._JOBS.clear()
