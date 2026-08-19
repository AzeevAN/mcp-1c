"""Модель синтакс-помощника платформы 1С.

Единица знания — `SyntaxItem`: объект, метод, свойство, событие или значение
перечисления платформы. Ключевые для генерации кода поля, которых нет в
похожих проектах:

* `availability` — контекст выполнения (сервер, тонкий клиент, веб-клиент…).
  Без него агент вставит серверный метод в клиентскую процедуру, и это не
  предупреждение, а ошибка компиляции.
* `since` — версия платформы, начиная с которой элемент существует. Позволяет
  одной справкой от свежей платформы корректно обслуживать старые
  конфигурации: то, чего в их версии ещё нет, просто не попадает в выдачу.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from functools import lru_cache

# Канонические значения доступности. В справке они пишутся вразнобой
# («Сервер» и «сервер», «Толстый клиент» и «толстый клиент»).
AVAILABILITY = {
    "тонкий клиент": "ТонкийКлиент",
    "веб-клиент": "ВебКлиент",
    "толстый клиент": "ТолстыйКлиент",
    "сервер": "Сервер",
    "внешнее соединение": "ВнешнееСоединение",
    "мобильное приложение (клиент)": "МобильноеПриложениеКлиент",
    "мобильное приложение (сервер)": "МобильноеПриложениеСервер",
    "мобильный клиент": "МобильныйКлиент",
    "мобильный автономный сервер": "МобильныйАвтономныйСервер",
}

KIND_METHOD = "method"
KIND_PROPERTY = "property"
KIND_EVENT = "event"
KIND_OBJECT = "object"
KIND_CONSTRUCTOR = "constructor"
# Раздел tables/ — таблицы и поля языка запросов. Отдельный домен: у полей нет
# объекта-родителя, а имена (Ссылка, НомерСтроки, Активность) совпадают с
# десятками свойств объектов. Без своего вида они перемешиваются с ними.
KIND_QUERY_TABLE = "query_table"
KIND_QUERY_FIELD = "query_field"
# Язык запросов — отдельный источник (`shquery_ru.hbk`). Виды свои, потому что
# это другой домен: у страниц нет ни версии появления, ни родительского
# объекта, а имена наполовину совпадают с именами платформы (57 из 127).
KIND_QUERY_FUNCTION = "query_function"
KIND_QUERY_KEYWORD = "query_keyword"
KIND_QUERY_ARTICLE = "query_article"

# Виды из источника языка запросов — те, у которых нет версии по устройству
# формата. Набор живёт здесь, рядом с самими видами, а не у потребителей:
# он нужен и поиску (потолок описания, подъём по обороту), и инструментам
# (фильтр версии), и четвёртый вид иначе добавили бы в одном месте, забыв
# про второе.
#
# Не путать с `KIND_QUERY_TABLE`/`KIND_QUERY_FIELD`: префикс `query_` тот же,
# но это страницы `tables/` из справки платформы — у них версия есть, они
# участвуют в слиянии, и считать её обязаны по версии конфигурации.
QUERY_LANGUAGE_KINDS = frozenset(
    {KIND_QUERY_FUNCTION, KIND_QUERY_KEYWORD, KIND_QUERY_ARTICLE}
)

KIND_TITLES = {
    KIND_METHOD: "Метод",
    KIND_PROPERTY: "Свойство",
    KIND_EVENT: "Событие",
    KIND_OBJECT: "Объект",
    KIND_CONSTRUCTOR: "Конструктор",
    KIND_QUERY_TABLE: "Таблица запроса",
    KIND_QUERY_FIELD: "Поле таблицы запроса",
    KIND_QUERY_FUNCTION: "Функция запроса",
    KIND_QUERY_KEYWORD: "Ключевое слово запроса",
    KIND_QUERY_ARTICLE: "Статья по языку запросов",
}


def release(version: tuple[int, ...]) -> tuple[int, ...]:
    """Релиз платформы без номера сборки: (8,3,5,1570) -> (8,3,5).

    Справка приходит сборкой, конфигурация живёт на своей, а описывают они один
    и тот же релиз. Сравнение по четырём числам делало бы справку 8.3.5.1570
    неприменимой к конфигурации 8.3.5.1234.
    """
    return version[:3]


@lru_cache(maxsize=4096)
def parse_version(text: str) -> tuple[int, ...]:
    """«8.3.27» -> (8, 3, 27). Пустая строка -> (), сортируется раньше всех."""
    parts = []
    for chunk in text.strip().split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


@dataclass(slots=True)
class SyntaxParam:
    name: str
    required: bool = True
    types: list[str] = field(default_factory=list)
    description: str = ""
    default: str = ""

    def render(self) -> str:
        types = ", ".join(self.types) if self.types else "?"
        mark = "" if self.required else ", необязательный"
        return f"{self.name}: {types}{mark}"


@dataclass(slots=True)
class SyntaxVariant:
    """Вариант синтаксиса. У многих методов их несколько."""

    title: str = ""
    signature: str = ""
    params: list[SyntaxParam] = field(default_factory=list)
    returns: list[str] = field(default_factory=list)
    returns_description: str = ""
    description: str = ""


@dataclass(slots=True)
class SyntaxFacts:
    """Что было известно об элементе из справки более старой версии.

    Записывается только расхождение с базовой справкой: совпадающее хранить
    незачем, а различается мало — 167 сигнатур и 600 наборов доступности на
    четыре версии.
    """

    platform: str
    signature: str = ""
    availability: list[str] = field(default_factory=list)
    # Имя в той версии, если оно с тех пор изменилось.
    name_ru: str = ""


# Место таблицы в потоке абзацев описания. Таблица уезжает в отдельное поле,
# но её место в тексте — часть смысла: на странице с двумя примерами обе
# таблицы в хвосте не дают понять, какая к какому примеру относится. Нулевой
# байт в тексте справки не встречается, разбор и поиск его не трогают.
МЕТКА_ТАБЛИЦЫ = "\x00таблица-{номер}\x00"
_RE_МЕТКА_ТАБЛИЦЫ = re.compile(r"\x00таблица-\d+\x00")


def без_меток(описание: str) -> str:
    """Описание без служебных меток — то, что видит поиск и человек.

    Метка занимает абзац целиком, поэтому после неё остаётся пустая строка:
    её тоже убираем, иначе в тексте зияет разрыв там, где стояла таблица.

    Схлопывание — только там, где метка правда была. Зовётся эта функция на
    каждой карточке, в том числе платформенной, где меток не бывает вовсе, а
    пропуски в тексте авторские: у 14 элементов справки идут три и больше
    переводов строки подряд, и трогать их ради своей задачи нечего.
    """
    без, снято = _RE_МЕТКА_ТАБЛИЦЫ.subn("", описание)
    if not снято:
        return описание
    return re.sub(r"\n{3,}", "\n\n", без).strip()


@dataclass(slots=True)
class SyntaxTable:
    """Таблица со страницы справки: показывается, но не индексируется.

    Ячейки в этой справке размечены абзацами внутри `<TD>`, поэтому без
    отдельного разбора каждая становится своим абзацем описания, и карточка
    печатает таблицу столбцом значений. Отдельное поле разводит показ и поиск:
    карточка таблицу рисует, поисковый индекс по ней не матчится.
    """

    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


@dataclass(slots=True)
class SyntaxItem:
    id: str
    kind: str
    name_ru: str = ""
    name_en: str = ""
    parent_ru: str = ""
    parent_en: str = ""
    description: str = ""
    availability: list[str] = field(default_factory=list)
    since: str = ""
    # Последняя версия платформы, в справке которой элемент ещё описан. Пусто —
    # он есть в самой свежей из загруженных справок. Заполняется слиянием:
    # одна справка про исчезновение ничего сказать не может.
    until: str = ""
    # Расхождения со старыми справками, от старых версий к новым.
    older: list[SyntaxFacts] = field(default_factory=list)
    variants: list[SyntaxVariant] = field(default_factory=list)
    tables: list[SyntaxTable] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    see_also: list[str] = field(default_factory=list)
    members: dict[str, list[str]] = field(default_factory=dict)
    values: list[str] = field(default_factory=list)
    readonly: bool | None = None
    note: str = ""

    @property
    def full_ru(self) -> str:
        return f"{self.parent_ru}.{self.name_ru}" if self.parent_ru else self.name_ru

    @property
    def address(self) -> str:
        """Как назвать элемент, чтобы `get_syntax` отдал именно его.

        У платформенного элемента есть владелец: `Глобальный контекст.СтрНайти`.
        У элемента языка запросов владельца нет вовсе, и голое имя неоднозначно —
        `СтрНайти` есть и там и там. Отсюда квалификатор `Запрос.`.

        Свойство, а не помощник в одном модуле: адрес печатают карточка
        одноимённых, выдача поиска и дашборд, и разъехаться им нельзя — на
        живом дашборде это и заметили (2026-08-19).
        """
        if self.kind in QUERY_LANGUAGE_KINDS:
            return f"Запрос.{self.name_ru}"
        return self.full_ru

    @property
    def full_en(self) -> str:
        return f"{self.parent_en}.{self.name_en}" if self.parent_en else self.name_en

    @property
    def since_tuple(self) -> tuple[int, ...]:
        return parse_version(self.since)

    @property
    def until_tuple(self) -> tuple[int, ...]:
        return parse_version(self.until)

    def available_in(self, platform: str) -> bool:
        """Существует ли элемент в указанной версии платформы."""
        return self.available_in_tuple(parse_version(platform))

    def available_in_tuple(self, target: tuple[int, ...]) -> bool:
        """То же, но с уже разобранной версией — для фильтрации пачками.

        Границы две: `since` прячет то, чего в версии ещё нет, `until` — то,
        чего уже нет. Вторая появляется только при слиянии справок: одна
        справка про исчезновение ничего не знает.
        """
        if not target:
            return True
        target = release(target)
        if self.since and release(self.since_tuple) > target:
            return False
        if self.until and release(self.until_tuple) < target:
            return False
        return True

    def signature(self) -> str:
        if self.variants and self.variants[0].signature:
            return self.variants[0].signature
        return self.name_ru

    def search_terms(self) -> list[str]:
        """Всё, по чему элемент должен находиться."""
        terms = [self.name_ru, self.name_en, self.full_ru, self.full_en]
        terms += [self.parent_ru, self.parent_en]
        return [t for t in terms if t]


def _same(left: "SyntaxFacts", right: "SyntaxFacts") -> bool:
    return (
        left.signature == right.signature
        and left.availability == right.availability
        and left.name_ru == right.name_ru
    )


@dataclass(slots=True)
class Resolution:
    """Что известно об элементе применительно к одной версии платформы.

    `exact` — есть ли на это основание. Точно, когда справка ровно этой версии
    загружена либо когда соседние справки об элементе говорят одно и то же:
    меняться между ними было нечему. Неточно — когда справки расходятся, а
    версия попала между ними; тогда `alternatives` содержит оба состояния, и
    выбирать за агента сервер не должен.
    """

    signature: str = ""
    availability: list[str] = field(default_factory=list)
    # Имя в этой версии: 1С переименовывает, и подсказать нынешнее имя для
    # старой платформы — та же ошибка компиляции, что и лишний параметр.
    name_ru: str = ""
    # Версия справки, из которой взяты факты.
    platform: str = ""
    # Версия, про которую спрашивали. Совпадает с `platform`, когда справка
    # этой версии загружена.
    asked: str = ""
    # Существует ли элемент в этой версии вовсе. Без этого признака ответ про
    # удалённый элемент выглядел бы обычным — с сигнатурой и пометкой «точно».
    available: bool = True
    exact: bool = True
    alternatives: list[SyntaxFacts] = field(default_factory=list)


@dataclass(slots=True)
class SyntaxIndex:
    """Разобранная справка одной или нескольких версий платформы."""

    platforms: list[str] = field(default_factory=list)
    items: dict[str, SyntaxItem] = field(default_factory=dict)
    source: str = ""
    language: str = "ru"
    # Страницы, чью разметку разобрать не удалось. Говорятся вслух при
    # загрузке: разбор, тихо отдавший меньше обычного, неотличим от справки,
    # в которой этого просто нет.
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.items)

    def add(self, item: SyntaxItem) -> None:
        self.items[item.id] = item

    def _chain(self, item: SyntaxItem) -> list[SyntaxFacts]:
        """Состояния элемента по версиям справок, от старых к новым.

        Пустое поле в записи означает «как в базовой справке» — слияние
        хранит только расхождения.
        """
        base_signature = item.variants[0].signature if item.variants else ""
        recorded = {facts.platform: facts for facts in item.older}

        chain: list[SyntaxFacts] = []
        for platform in sorted(set(self.platforms) | set(recorded), key=parse_version):
            if not item.available_in(platform):
                continue
            facts = recorded.get(platform)
            if facts is None:
                # Справка этой версии загружена, а расхождения не записано —
                # значит элемент в ней описан так же, как в базовой.
                chain.append(
                    SyntaxFacts(
                        platform=platform,
                        signature=base_signature,
                        availability=list(item.availability),
                    )
                )
            else:
                chain.append(
                    SyntaxFacts(
                        platform=platform,
                        signature=facts.signature or base_signature,
                        availability=facts.availability or list(item.availability),
                        name_ru=facts.name_ru,
                    )
                )
        return chain

    def help_for(self, platform: str) -> str:
        """Загруженная справка того же релиза — её версия, как она названа.

        Слитый вид собран из нескольких справок, и «какая из них отвечает за
        эту конфигурацию» — не то же самое, что версия объединённого
        источника: та всегда самая свежая.
        """
        target = release(parse_version(platform))
        for known in self.platforms:
            if release(parse_version(known)) == target:
                return known
        return ""

    def has_help_for(self, platform: str) -> bool:
        """Загружена ли справка ровно этого релиза платформы."""
        return bool(self.help_for(platform))

    def facts_for(self, item: SyntaxItem, platform: str) -> Resolution:
        """Что известно об элементе для указанной версии платформы."""
        if not item.available_in(platform):
            # Элемента в этой версии нет: он появился позже или уже удалён.
            # Отдавать сигнатуру нельзя — по ней напишут невыполнимый код.
            return Resolution(
                platform=platform,
                asked=platform,
                available=False,
                exact=self.has_help_for(platform),
            )

        chain = self._chain(item)
        target = release(parse_version(platform))
        for facts in chain:
            if release(parse_version(facts.platform)) == target:
                return Resolution(
                    signature=facts.signature,
                    availability=list(facts.availability),
                    name_ru=facts.name_ru or item.name_ru,
                    platform=facts.platform,
                    asked=platform,
                    exact=True,
                )
        below = [f for f in chain if release(parse_version(f.platform)) < target]
        above = [f for f in chain if release(parse_version(f.platform)) > target]
        lower = below[-1] if below else None
        upper = above[0] if above else None

        # Справок по краям нет или они согласны — брать нечего кроме того, что
        # известно, и сомневаться не в чем.
        if lower is None or upper is None or _same(lower, upper):
            chosen = lower or upper
            return Resolution(
                signature=chosen.signature,
                availability=list(chosen.availability),
                name_ru=chosen.name_ru or item.name_ru,
                platform=chosen.platform,
                asked=platform,
                exact=lower is not None or upper is None,
            )

        return Resolution(
            signature=lower.signature,
            availability=list(lower.availability),
            name_ru=lower.name_ru or item.name_ru,
            platform=lower.platform,
            asked=platform,
            exact=False,
            alternatives=[lower, upper],
        )

    def by_kind(self, kind: str) -> list[SyntaxItem]:
        return [i for i in self.items.values() if i.kind == kind]

    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items.values():
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def hidden_for(self, platform: str) -> int:
        """Сколько элементов не существует в указанной версии платформы."""
        return sum(1 for i in self.items.values() if not i.available_in(platform))

    def derived_platform(self) -> str:
        """Версия платформы, выведенная из самих данных.

        В имени файла `shcntx_ru.hbk` версии нет, а из каталога установки его
        могли скопировать куда угодно. Но справка знает, в какой версии
        появился каждый элемент, — значит она не старше самого свежего из них.
        Для фильтрации этого достаточно, а без версии фильтрация не работает
        вовсе.
        """
        newest = ""
        newest_key: tuple[int, ...] = ()
        for item in self.items.values():
            key = item.since_tuple
            if key > newest_key:
                newest_key, newest = key, item.since
        return newest

    @property
    def max_platform(self) -> str:
        if not self.platforms:
            return ""
        return max(self.platforms, key=parse_version)
