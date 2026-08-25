"""Слой поисковых ключей справки (`search_keys.py`).

Ключи — сочинённый текст поверх разобранной справки, и проверяется здесь
ровно то, что делает его безопасным: привязка по идентификатору, громкий
отчёт о непривязанном, отсутствие ключей в ответе агенту.
"""

from __future__ import annotations

from mcp1c.search import Doc, SearchIndex, index_syntax
from mcp1c.search_keys import (
    PLATFORM_SEARCH_KEYS,
    SEARCH_KEYS,
    coverage,
    keys_text,
    platform_coverage,
)
from mcp1c.syntax_model import (
    KIND_METHOD,
    KIND_QUERY_FUNCTION,
    SyntaxIndex,
    SyntaxItem,
)

# ------------------------------------------------------------------- привязка


def test_ключи_привязываются_к_известным_страницам():
    известные = set(SEARCH_KEYS) | {"query/Sin", "query/Cos"}
    итог = coverage(известные)

    assert итог.total == len(SEARCH_KEYS)
    assert итог.attached == len(SEARCH_KEYS)
    assert итог.lost == []
    assert итог.ok


def test_непривязанный_ключ_попадает_в_потерянные():
    # Справка дала только одну страницу из тех, под которые писались ключи.
    одна = next(iter(SEARCH_KEYS))
    итог = coverage({одна})

    assert итог.attached == 1
    assert len(итог.lost) == len(SEARCH_KEYS) - 1
    assert одна not in итог.lost
    assert not итог.ok


def test_полное_покрытие_молчит_а_потеря_говорит():
    """Отчёт обязан молчать, когда всё в порядке, и называть цифры, когда нет.

    Предупреждение на каждой загрузке приучило бы его не читать.
    """
    assert coverage(set(SEARCH_KEYS)).as_warning() == ""

    текст = coverage(set()).as_warning()
    assert str(len(SEARCH_KEYS)) in текст
    assert "потеряно" in текст


def test_потерянные_показаны_не_все_но_счёт_полный():
    """Список обрезается, число — нет: 116 строк в предупреждении нечитаемы."""
    текст = coverage(set()).as_warning()

    assert "и ещё…" in текст
    assert f"потеряно {len(SEARCH_KEYS)}" in текст


def test_потеря_ключа_метода_платформы_видна_отдельно():
    итог = platform_coverage(set())

    assert итог.total == len(PLATFORM_SEARCH_KEYS)
    assert итог.lost == list(PLATFORM_SEARCH_KEYS)
    assert "методам платформы" in итог.as_warning()
    assert platform_coverage(set(PLATFORM_SEARCH_KEYS)).ok


# --------------------------------------------------------------------- поиск


def _справка(item_id: str, name: str) -> SyntaxIndex:
    index = SyntaxIndex(platforms=[], source="search-keys-test", language="ru")
    index.add(
        SyntaxItem(
            id=item_id,
            kind=KIND_QUERY_FUNCTION,
            name_ru=name,
            name_en=name,
            description="Функция языка запросов.",
        )
    )
    return index


def test_статья_находится_формулировкой_из_ключей():
    """Ноль общих слов с именем — ради этого случая слой и заводится."""
    формулировка = "сколько дней между двумя датами"
    assert формулировка in SEARCH_KEYS["query/DATEDIFF"]
    assert "разностьдат" not in формулировка.lower()

    index = index_syntax(_справка("query/DATEDIFF", "РАЗНОСТЬДАТ"))
    hits = index.search(формулировка, limit=5)

    assert [hit.doc.id for hit in hits] == ["query/DATEDIFF"]


def test_метод_платформы_находится_по_описанию_действия():
    """Пользователь описывает цикл, а в имени метода стоит только «Следующий»."""
    target_id = "objects/catalog213/catalog393/QueryResultSelection/methods/Next556"
    syntax = SyntaxIndex(platforms=[], source="search-keys-test", language="ru")
    syntax.add(
        SyntaxItem(
            id=target_id,
            kind=KIND_METHOD,
            name_ru="Следующий",
            name_en="Next",
            parent_ru="ВыборкаИзРезультатаЗапроса",
            description="Получает следующую запись.",
        )
    )
    syntax.add(
        SyntaxItem(
            id="query/example",
            kind=KIND_QUERY_FUNCTION,
            name_ru="Поля выборки",
            name_en="Selection fields",
            description="Строки, которые попадут в результат запроса.",
        )
    )

    hits = index_syntax(syntax).search(
        "как перебрать строки в выборке результата запроса", limit=5
    )

    assert hits[0].doc.id == target_id


def test_элемент_без_ключей_ищется_как_прежде():
    """У тригонометрии ключей нет — она не должна ни выиграть, ни потерять."""
    assert "query/Sin" not in SEARCH_KEYS
    assert keys_text("query/Sin") == ""

    index = index_syntax(_справка("query/Sin", "Sin"))

    assert [hit.doc.id for hit in index.search("Sin", limit=5)] == ["query/Sin"]


def test_ключи_не_попадают_в_ответ_агенту():
    """Слой работает на попадание в статью, а не на её содержание.

    `SyntaxItem` — то, из чего собирается карточка. Ключи туда не приписаны,
    и правило `data-sources.md` держится именно на этом.
    """
    syntax = _справка("query/DATEDIFF", "РАЗНОСТЬДАТ")
    index_syntax(syntax)  # сборка индекса не должна ничего дописать в элемент

    item = syntax.items["query/DATEDIFF"]
    assert "сколько дней" not in item.description
    assert not hasattr(item, "keys")


# ------------------------------------------------------------ дедупликация


def test_повтор_слова_в_ключах_не_умножает_вес():
    """Иначе вес поля зависел бы от того, сколько раз я написал слово.

    В поле `description` повтор — сигнал и суммируется намеренно; в ключах он
    артефакт стиля: «дата» стоит почти в каждой формулировке про даты.
    """
    один = Doc(id="один", fields={"keys": "дата"})
    пять = Doc(id="пять", fields={"keys": "дата\nдата\nдата\nдата\nдата"})

    index = SearchIndex([один, пять])
    hits = {hit.doc.id: hit.score for hit in index.search("дата", limit=5)}

    assert hits["один"] == hits["пять"]


def test_в_описании_повтор_по_прежнему_копится():
    """Проверка на то, что дедупликация не расползлась на остальные поля."""
    один = Doc(id="один", fields={"description": "дата"})
    пять = Doc(id="пять", fields={"description": "дата дата дата дата дата"})

    index = SearchIndex([один, пять])
    hits = {hit.doc.id: hit.score for hit in index.search("дата", limit=5)}

    assert hits["пять"] > hits["один"]
