"""Лексический поиск по метаданным конфигурации и по справке платформы.

Домен специфический, и это определяет устройство движка.

**Имена — склеенные слова.** `РеализацияТоваровУслуг`, `ЗаписьJSON`,
`люч_Скупка`. Поэтому идентификаторы режутся по границам регистра, и запрос
«реализация товаров» находит объект, в имени которого нет ни одного пробела.

**Русская морфология.** «накладная» и «накладной» — одно слово. Полноценный
стеммер здесь избыточен: хватает усечения токена до общей основы, потому что
различия почти всегда в окончании.

**Синоним — естественная фраза.** «Реализация товаров и услуг» ищется как
обычный текст, а точное имя — как идентификатор. Оба варианта индексируются.

**Двуязычие.** В справке у каждого элемента есть русское и английское имя;
агент может спросить и `СтрНайти`, и `StrFind`.

Векторов здесь нет намеренно: в домене, где запрос почти всегда близок к
идентификатору, лексика даёт лучший результат дешевле. Замеры — `bench.py`.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import snowballstemmer

from .search_keys import keys_text
from .syntax_model import QUERY_LANGUAGE_KINDS, без_меток
from .synonyms import SYNONYMS

# Длина, начиная с которой основа расширяется по префиксу: запрос «номенк»
# должен дотягиваться до «номенклатура». К сведению словоформ отношения не
# имеет — этим занимается `stem()`.
STEM_LENGTH = 6

# Вес поля в общей оценке. Имя важнее синонима, синоним важнее описания.
DEFAULT_FIELD_WEIGHTS = {
    "name": 6.0,
    "name_en": 6.0,
    "synonym": 4.0,
    "parent": 1.5,
    # Имя табличной части — часть адреса, по которому спрашивают:
    # «контактная информация контрагента» описывает не поле, а контейнер.
    "part": 4.0,
    "kind": 1.2,
    # Поисковые ключи языка запросов (`search_keys.py`): формулировки, которыми
    # статью спрашивают. Вес подобран прогоном по трём наборам сразу, и 2.0 —
    # не компромисс, а строгий максимум: живой набор языка запросов даёт 18 из
    # 19 уже здесь, а на 3.0 и выше начинают ехать одноимённые (10 471 -> 10 468
    # первым местом) — статья «ГОД» обходит платформенный «Год». Выигрыша за это
    # не покупается: 4.0 и 6.0 дают на живом наборе 17, то есть хуже.
    "keys": 2.0,
    "description": 0.6,
}

# Поля, где повтор токена — свойство текста, а не сигнал. Остальные поля
# суммируют вес по каждому вхождению намеренно: слово, дважды встреченное в
# описании, там действительно весомее.
DEDUPED_FIELDS = frozenset({"keys"})

def _bounds(spans: dict[str, tuple[int, int]], total: int) -> bytes:
    """Границы срезов подряд: срезы уложены встык, хватает начал."""
    starts = np.fromiter((s for s, _ in spans.values()), dtype=np.int64, count=len(spans))
    return np.append(starts, total).tobytes()


def _spans(keys: list[str], bounds: bytes) -> dict[str, tuple[int, int]]:
    edges = np.frombuffer(bounds, dtype=np.int64)
    return {key: (int(edges[i]), int(edges[i + 1])) for i, key in enumerate(keys)}


# Общие пустышки для полей документа после заморозки: по одному объекту на
# весь процесс вместо пустого словаря и списка на каждый из десятков тысяч
# документов. После заморозки документ — только запись для выдачи, поля в нём
# никто не читает и не пишет.
_NO_FIELDS: dict[str, str] = {}
_NO_KEYS: list[str] = []

_EMPTY_INDEX = np.empty(0, dtype=np.int32)
_EMPTY_WEIGHT = np.empty(0, dtype=np.float64)

QUALITY_EXACT_TOKEN = 1.0
QUALITY_STEM = 0.55
# Подстановка из словаря предметной области весит заметно меньше прямого
# совпадения: «клиент» в 1С означает и покупателя, и клиентскую часть модуля,
# и точное слово обязано побеждать догадку.
QUALITY_SYNONYM = 0.4

# Где реально хранятся данные, а где — служебное. Применяется и к самим
# объектам, и к их реквизитам: `ОпределяемыйТип.Номенклатура` — псевдоним
# типа, а спрашивают почти всегда про `Справочник.Номенклатура`.
OWNER_KIND_BOOST = {
    "Справочник": 1.0,
    "Документ": 1.0,
    "РегистрСведений": 1.0,
    "РегистрНакопления": 1.0,
    "РегистрБухгалтерии": 1.0,
    "РегистрРасчета": 1.0,
    "ПланВидовХарактеристик": 1.0,
    "ПланСчетов": 1.0,
    "Константа": 0.9,
    "Перечисление": 0.9,
    "БизнесПроцесс": 0.8,
    "Задача": 0.8,
    "ПланОбмена": 0.6,
    "ОпределяемыйТип": 0.45,
    "ОбщийМодуль": 0.4,
    "ПодпискаНаСобытие": 0.4,
    "РегламентноеЗадание": 0.4,
    "Отчет": 0.3,
    "Обработка": 0.3,
}



# Насколько сильно длинное имя штрафуется относительно короткого. Запрос
# «заказ клиента» покрывает `ЗаказПокупателя` целиком и лишь треть имени
# `ПричинаОтменыСтрокЗаказаКлиентаEDI` — второе совпадение случайное, хотя
# формально содержит оба слова. Без этого нормирования короткое точное имя
# проигрывает длинному, где нужные слова оказались по соседству.
LENGTH_PENALTY = 0.3

# Во сколько раз точное совпадение всей строки запроса сильнее обычного.
EXACT_BOOST = 12.0

# Потолок описания в индексе. У метода платформы описание короткое, и он не
# бился ни разу — держим его для всех видов, кроме перечисленных ниже.
DESCRIPTION_CAP = 400

# Потолок не действует на виды из `QUERY_LANGUAGE_KINDS` (`syntax_model.py`):
# у них описание — не краткая аннотация, а содержимое страницы целиком
# (страницы языка запросов весят 8-24,8 КБ, то есть при потолке в 400
# символов индексировалось 2-5% текста). Замер: целиком и потолок в 4000
# символов дают одинаковый результат на живом наборе из 19 формулировок
# («язык запросов» — 47,4% -> 52,6% первым, 57,9% -> 73,7% в пятёрке,
# MRR 0,532 -> 0,593), поэтому промежуточного потолка не заводим — 127
# страниц, объём индекса от снятия потолка растёт незаметно.

# Обороты, которых не бывает в именах элементов платформы или конфигурации —
# только в вопросах про язык запросов. Список выверен по автоматическим
# наборам: ни один из них не встретился ни в одном из 61 051 запроса
# (`cases_from_syntax` 50 572 + `cases_from_syntax_ambiguous` 10 479), значит
# подъём по ним не может задеть остальной поиск.
QUERY_LANGUAGE_MARKERS = (
    "в запросе",
    "в тексте запроса",
    "в выборке",
    "языка запросов",
)

# Множитель подъёма для элементов языка запросов при явном обороте в
# вопросе. Замерено на живом наборе из 19 формулировок: подъём даёт первым
# 52,6% -> 57,9%, пятёркой 73,7% -> 78,9%, MRR 0,593 -> 0,639. Множители 1,2,
# 1,5, 2,0 и 3,0 дают одинаковый результат — разрывы между платформой и
# языком запросов невелики, и мягкого толчка хватает. Больший множитель
# ничего не добавляет, но начинает переставлять случаи, где платформенный
# элемент выигрывает уверенно: «как задать параметр в запросе» может быть и
# про `Запрос.УстановитьПараметр` — при отрыве больше 20% он обязан остаться
# первым.
QUERY_LANGUAGE_BOOST = 1.2

_RE_CAMEL = re.compile(
    r"(?<=[а-яa-z0-9])(?=[А-ЯA-Z])|(?<=[А-ЯA-Z])(?=[А-ЯA-Z][а-яa-z])"
)
_RE_SPLIT = re.compile(r"[^0-9A-Za-zА-Яа-яЁё]+")
_RE_DIGIT_EDGE = re.compile(r"(?<=[A-Za-zА-Яа-яЁё])(?=\d)|(?<=\d)(?=[A-Za-zА-Яа-яЁё])")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return text.replace("ё", "е")


_RE_WHITESPACE = re.compile(r"\s+")


def _has_query_language_marker(query: str) -> bool:
    """Есть ли в запросе оборот из `QUERY_LANGUAGE_MARKERS`.

    Ищется подстрокой по нормализованному тексту с одиночными пробелами —
    без привязки к границе слова: обороты составные («в тексте запроса»), и
    падежные варианты внутри них не встречаются.
    """
    text = _RE_WHITESPACE.sub(" ", normalize(query))
    return any(marker in text for marker in QUERY_LANGUAGE_MARKERS)


def split_identifier(text: str) -> list[str]:
    """`РеализацияТоваровУслуг` -> `[реализация, товаров, услуг]`."""
    words: list[str] = []
    for chunk in _RE_SPLIT.split(text):
        if not chunk:
            continue
        for part in _RE_CAMEL.split(chunk):
            for piece in _RE_DIGIT_EDGE.split(part):
                if piece:
                    words.append(normalize(piece))
    return words


def tokenize(text: str) -> list[str]:
    return split_identifier(text)


_STEMMER = snowballstemmer.stemmer("russian")
# Основы кэшируются: словарь имён конфигурации — тысячи уникальных токенов на
# десятки тысяч вхождений, и один и тот же токен стеммится многократно.
_STEM_CACHE: dict[str, str] = {}


def stem(token: str) -> str:
    """Основа слова по алгоритму Snowball.

    Раньше здесь была обрезка до шести символов. Она сводит длинные слова
    («контрагент» и «контрагенты» → «контра»), но короткие оставляет
    разными: «чек», «чека», «чеков» — три разных токена, «вид» и «виды» —
    два. Из-за этого «виды оплат чеков» не находило `ВидыОплатЧекаККМ`
    вовсе: слово «чеков» не совпадало ни с одним документом.
    """
    got = _STEM_CACHE.get(token)
    if got is None:
        got = _STEM_CACHE[token] = _STEMMER.stemWord(token)
    return got


@dataclass(slots=True)
class Doc:
    """Документ для поиска: набор именованных полей плюс полезная нагрузка."""

    id: str
    fields: dict[str, str] = field(default_factory=dict)
    kind: str = ""
    payload: object = None
    # Строки, по которым документ должен находиться целиком, но которые не
    # добавляют токенов: `Документ.РеализацияТоваровУслуг`, английское имя,
    # `ЗаписьJSON.ЗаписатьНачалоОбъекта`. Их токены и так есть в других полях,
    # а лишнее повторение перекашивало бы оценку.
    exact_keys: list[str] = field(default_factory=list)
    # Априорная важность документа. Данные живут в справочниках, документах и
    # регистрах; поля форм обработок и отчётов совпадают по именам, но искомым
    # почти никогда не являются.
    boost: float = 1.0


@dataclass(slots=True)
class Hit:
    doc: Doc
    score: float
    reason: str = ""


class SearchIndex:
    """Обратный индекс с оценкой по покрытию запроса и весам полей."""

    def __init__(
        self,
        docs: Iterable[Doc] | None = None,
        field_weights: dict[str, float] | None = None,
        exact_fields: Iterable[str] = ("name", "synonym"),
        synonyms: dict[str, frozenset[str]] | None = None,
        aliases: dict[str, list[str]] | None = None,
    ):
        self.weights = dict(field_weights or DEFAULT_FIELD_WEIGHTS)
        self.exact_fields = set(exact_fields)
        # Словарь приходит извне и может быть заменён на лету: правка файла
        # плюс перезагрузка реестра, без пересборки образа.
        self.synonyms = SYNONYMS if synonyms is None else synonyms
        self.aliases = aliases or {}
        # Кэш «фраза по основам -> цели». Строится лениво и сбрасывается при
        # подмене таблицы: `reload_dictionary` меняет её на живом индексе.
        self._alias_stems: dict[str, list[str]] | None = None
        self.docs: dict[str, Doc] = {}

        # stem -> doc_id -> накопленный вес поля
        self._by_stem: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        # полный токен -> doc_id -> вес
        self._by_token: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        # точное имя целиком -> список doc_id
        self._by_exact: dict[str, list[str]] = defaultdict(list)
        # сколько слов в имени документа — для нормирования по длине
        self._name_length: dict[str, int] = {}

        # Замороженное представление — то, по чему идёт поиск. Словари выше
        # нужны только на время наполнения: пара «документ, вес» стоит в них
        # около 100 байт против 12 в массивах, а таких пар на трёх
        # конфигурациях со справкой набирается 1,7 млн.
        self._stems: list[str] = []
        self._doc_ids: list[str] = []
        self._position: dict[str, int] = {}
        self._token_span: dict[str, tuple[int, int]] = {}
        self._stem_span: dict[str, tuple[int, int]] = {}
        self._token_docs = _EMPTY_INDEX
        self._token_weights = _EMPTY_WEIGHT
        self._stem_docs = _EMPTY_INDEX
        self._stem_weights = _EMPTY_WEIGHT
        self._exact: dict[str, list[int]] = {}
        self._boost = _EMPTY_WEIGHT
        self._length = _EMPTY_INDEX
        # Лексикографический ранг идентификатора: им разрешаются ничьи, как
        # раньше это делала сортировка по `(-оценка, идентификатор)`.
        self._lex_rank = _EMPTY_INDEX
        self._kind_masks: dict[str, np.ndarray] = {}
        self._dirty = True

        for doc in docs or ():
            self.add(doc)

    # ------------------------------------------------------------- наполнение

    @property
    def aliases(self) -> dict[str, list[str]]:
        return self._aliases

    @aliases.setter
    def aliases(self, value: dict[str, list[str]] | None) -> None:
        self._aliases = value or {}
        self._alias_stems = None

    def _alias_targets(self, query_tokens: list[str]) -> tuple[str, ...]:
        """Цели псевдонима для запроса — с точностью до формы слова.

        Встроенные псевдонимы записаны во множественном числе («контрагенты»,
        «физические лица»), потому что так называются справочники, а спрашивают
        в единственном. Сравнение по основам снимает это расхождение и
        избавляет от перечисления форм в `synonyms.py`.
        """
        точная = self.aliases.get(" ".join(query_tokens))
        if точная:
            return tuple(точная)
        if self._alias_stems is None:
            self._alias_stems = {
                " ".join(stem(t) for t in tokenize(phrase)): list(targets)
                for phrase, targets in self.aliases.items()
            }
        return tuple(self._alias_stems.get(" ".join(stem(t) for t in query_tokens), ()))

    def _register_exact(self, doc_id: str, text: str) -> None:
        key = normalize(text).replace(" ", "")
        if key:
            self._by_exact[key].append(doc_id)

    def add(self, doc: Doc) -> None:
        self._thaw()
        self.docs[doc.id] = doc
        self._dirty = True

        # Точное совпадение целой строки — самый сильный сигнал.
        for text in doc.exact_keys:
            self._register_exact(doc.id, text)

        for field_name, text in doc.fields.items():
            if not text:
                continue
            weight = self.weights.get(field_name, 0.5)

            if field_name in self.exact_fields:
                self._register_exact(doc.id, text)

            tokens = tokenize(text)
            if field_name in DEDUPED_FIELDS:
                # Поле — набор перефразировок одного и того же, и общие слова в
                # них неизбежны: «дата» стоит в четырёх формулировках из пяти.
                # Без снятия повторов вес поля умножался бы на то, сколько раз
                # я написал слово, то есть на случайность авторского стиля, а
                # не на важность. Порядок сохраняем: `dict.fromkeys`, не `set`.
                tokens = list(dict.fromkeys(tokens))
            if field_name in ("name", "name_en"):
                current = self._name_length.get(doc.id)
                length = len(tokens)
                self._name_length[doc.id] = (
                    length if current is None else min(current, length)
                )

            seen: set[str] = set()
            for token in tokens:
                self._by_token[token][doc.id] += weight
                stem_key = stem(token)
                if (field_name, stem_key) not in seen:
                    self._by_stem[stem_key][doc.id] += weight
                    seen.add((field_name, stem_key))  # type: ignore[arg-type]

    def _finalize(self) -> None:
        """Заморозить постинги в массивы. Дальше поиск идёт только по ним.

        Наполнение остаётся словарным: индекс достраивается по документу за
        раз, и массивы для этого не годятся. Заморозка происходит при первом
        запросе после изменения — на справке она стоит около секунды и
        окупается вчетверо меньшей памятью.
        """
        if not self._dirty:
            return

        self._stems = sorted(self._by_stem)
        self._doc_ids = list(self.docs)
        self._position = {doc_id: i for i, doc_id in enumerate(self._doc_ids)}

        # Словарь освобождается сразу после упаковки, не в конце: иначе пик
        # памяти при первичной индексации складывается из обоих представлений.
        self._token_span, self._token_docs, self._token_weights = self._pack(self._by_token)
        self._by_token = defaultdict(lambda: defaultdict(float))
        self._stem_span, self._stem_docs, self._stem_weights = self._pack(self._by_stem)
        self._by_stem = defaultdict(lambda: defaultdict(float))

        # Документ попадает в `_by_exact` по разу на каждое совпавшее поле —
        # имя и синоним бывают одинаковыми. Множитель применяется один раз.
        self._exact = {
            key: list(dict.fromkeys(self._position[d] for d in ids if d in self._position))
            for key, ids in self._by_exact.items()
        }
        self._by_exact = defaultdict(list)

        count = len(self._doc_ids)
        self._boost = np.fromiter(
            (self.docs[d].boost for d in self._doc_ids), dtype=np.float64, count=count
        )
        self._length = np.fromiter(
            (self._name_length.get(d, 1) for d in self._doc_ids), dtype=np.int32, count=count
        )
        self._name_length = {}
        ranks = np.empty(count, dtype=np.int32)
        for rank, index in enumerate(sorted(range(count), key=self._doc_ids.__getitem__)):
            ranks[index] = rank
        self._lex_rank = ranks

        self._kind_masks = {}
        for index, doc_id in enumerate(self._doc_ids):
            doc = self.docs[doc_id]
            mask = self._kind_masks.get(doc.kind)
            if mask is None:
                mask = self._kind_masks[doc.kind] = np.zeros(count, dtype=bool)
            mask[index] = True
            # Тексты документа больше не нужны: поиск читает постинги. На
            # справке это 26 МБ описаний и точных ключей, которые иначе висят
            # до перезапуска. При добавлении документа индекс всё равно
            # восстанавливается из массивов, а не из полей.
            doc.fields = _NO_FIELDS
            doc.exact_keys = _NO_KEYS

        self._dirty = False

    def _pack(self, postings: dict[str, dict[str, float]]):
        """Словарь словарей -> срезы в двух сплошных массивах."""
        total = sum(len(bucket) for bucket in postings.values())
        docs = np.empty(total, dtype=np.int32)
        weights = np.empty(total, dtype=np.float64)
        spans: dict[str, tuple[int, int]] = {}
        position = self._position
        cursor = 0
        for key, bucket in postings.items():
            start = cursor
            for doc_id, weight in bucket.items():
                docs[cursor] = position[doc_id]
                weights[cursor] = weight
                cursor += 1
            spans[key] = (start, cursor)
        return spans, docs, weights

    # ------------------------------------------------------------- выгрузка

    def export_state(self) -> dict:
        """Всё, что нельзя восстановить дёшево, — только примитивы и байты.

        Индекс сам знает, из чего состоит; формат файла и его пригодность —
        забота `index_cache`. Полезная нагрузка документов (`payload`) сюда не
        попадает намеренно: это те же объекты конфигурации или элементы
        справки, которые всё равно загружены, и дублировать их на диск значит
        платить дважды.

        Поля документа (`fields`, `exact_keys`) тоже не сохраняются: они нужны
        только при индексации, поиск их не читает.
        """
        self._finalize()
        return {
            "doc_ids": list(self._doc_ids),
            "kinds": [self.docs[d].kind for d in self._doc_ids],
            "boost": self._boost.tobytes(),
            "length": self._length.tobytes(),
            "token_keys": list(self._token_span),
            "token_bounds": _bounds(self._token_span, self._token_docs.size),
            "token_docs": self._token_docs.tobytes(),
            "token_weights": self._token_weights.tobytes(),
            "stem_keys": list(self._stem_span),
            "stem_bounds": _bounds(self._stem_span, self._stem_docs.size),
            "stem_docs": self._stem_docs.tobytes(),
            "stem_weights": self._stem_weights.tobytes(),
            "exact": {key: list(ids) for key, ids in self._exact.items()},
            "weights": dict(self.weights),
            "exact_fields": sorted(self.exact_fields),
        }

    @classmethod
    def from_state(
        cls,
        state: dict,
        payloads: dict,
        *,
        synonyms: dict[str, frozenset[str]] | None = None,
        aliases: dict[str, list[str]] | None = None,
    ) -> "SearchIndex":
        """Собрать индекс из выгрузки, подключив полезную нагрузку заново.

        Массивы читаются поверх байтов без копирования: постинги только
        читаются, писать в них некому.

        Словарь (`synonyms`, `aliases`) в выгрузке не участвует: он читается
        только в момент поиска, на постинги не влияет и меняется чаще индекса.
        """
        index = cls.__new__(cls)
        index.weights = dict(state["weights"])
        index.exact_fields = set(state["exact_fields"])
        index.synonyms = SYNONYMS if synonyms is None else synonyms
        index.aliases = aliases or {}

        doc_ids = list(state["doc_ids"])
        index._doc_ids = doc_ids
        index._position = {doc_id: i for i, doc_id in enumerate(doc_ids)}
        index.docs = {
            # boost приводится к float намеренно: numpy-скаляр в модели потом
            # всплывает в сериализации ответов и в сравнениях.
            doc_id: Doc(
                id=doc_id,
                kind=kind,
                boost=float(boost),
                payload=payloads.get(doc_id),
                fields=_NO_FIELDS,
                exact_keys=_NO_KEYS,
            )
            for doc_id, kind, boost in zip(
                doc_ids,
                state["kinds"],
                np.frombuffer(state["boost"], dtype=np.float64),
            )
        }

        index._boost = np.frombuffer(state["boost"], dtype=np.float64)
        index._length = np.frombuffer(state["length"], dtype=np.int32)
        index._token_docs = np.frombuffer(state["token_docs"], dtype=np.int32)
        index._token_weights = np.frombuffer(state["token_weights"], dtype=np.float64)
        index._stem_docs = np.frombuffer(state["stem_docs"], dtype=np.int32)
        index._stem_weights = np.frombuffer(state["stem_weights"], dtype=np.float64)
        index._token_span = _spans(state["token_keys"], state["token_bounds"])
        index._stem_span = _spans(state["stem_keys"], state["stem_bounds"])
        index._stems = sorted(index._stem_span)
        index._exact = {key: list(ids) for key, ids in state["exact"].items()}

        count = len(doc_ids)
        ranks = np.empty(count, dtype=np.int32)
        for rank, position in enumerate(sorted(range(count), key=doc_ids.__getitem__)):
            ranks[position] = rank
        index._lex_rank = ranks

        index._kind_masks = {}
        for position, kind in enumerate(state["kinds"]):
            mask = index._kind_masks.get(kind)
            if mask is None:
                mask = index._kind_masks[kind] = np.zeros(count, dtype=bool)
            mask[position] = True

        # Словари наполнения пусты: индекс поднят готовым. Если в него всё же
        # начнут добавлять документы, `add()` восстановит их из массивов.
        index._by_stem = defaultdict(lambda: defaultdict(float))
        index._by_token = defaultdict(lambda: defaultdict(float))
        index._by_exact = defaultdict(list)
        index._name_length = {}
        index._dirty = False
        return index

    def _thaw(self) -> None:
        """Восстановить словари наполнения из массивов.

        Нужно ровно в одном случае: в поднятый из кэша индекс добавляют
        документ. Редкая операция, поэтому платим за неё только тогда.
        """
        if self._by_token or not self._doc_ids:
            return
        doc_ids = self._doc_ids
        for spans, docs, weights, target in (
            (self._token_span, self._token_docs, self._token_weights, self._by_token),
            (self._stem_span, self._stem_docs, self._stem_weights, self._by_stem),
        ):
            for key, (start, end) in spans.items():
                bucket = target[key]
                for offset in range(start, end):
                    bucket[doc_ids[docs[offset]]] = float(weights[offset])
        for key, positions in self._exact.items():
            self._by_exact[key] = [doc_ids[i] for i in positions]
        self._name_length = {
            doc_ids[i]: int(length) for i, length in enumerate(self._length)
        }

    def _idf(self, size: int) -> float:
        """Редкий токен весит больше частого."""
        total = max(len(self._doc_ids), 1)
        return 1.0 + (total / (1 + size)) ** 0.35

    def _score_token(
        self,
        token: str,
        origin_bit: int,
        quality_exact: float,
        quality_stem: float,
        scores: "np.ndarray",
        matched: "np.ndarray",
    ) -> None:
        """Начислить вес документам, где встретился токен.

        `origin_bit` — бит слова запроса, которому засчитывается совпадение.
        Для синонима он отличается от `token`: иначе покрытие запроса
        раздувалось бы за счёт подстановок.

        Покрытие копится битовой маской, а не множеством слов. Слов в запросе
        единицы, а начислений — миллионы: на справке выходило 3,8 млн вызовов
        `set.add` на триста запросов, плюс отдельное множество на каждый из
        нескольких тысяч кандидатов.
        """
        span = self._token_span.get(token)
        if span is not None:
            start, end = span
            docs = self._token_docs[start:end]
            weight = self._idf(end - start) * quality_exact
            scores[docs] += self._token_weights[start:end] * weight
            matched[docs] |= origin_bit

        base = stem(token)
        candidates = [base]
        if len(token) < STEM_LENGTH or base not in self._stem_span:
            # Недописанное слово — «докум» должно цеплять «документ». Условие
            # по длине само по себе недостаточно: «номенк» ровно шесть букв, а
            # основы «номенклатур» не даёт, и раньше это спасало лишь потому,
            # что обрезка обеих строк совпадала. Настоящий признак — основы нет
            # в индексе: тогда префикс остаётся единственным способом попасть.
            candidates = self._prefix_stems(base) or candidates

        for candidate in candidates:
            span = self._stem_span.get(candidate)
            if span is None:
                continue
            start, end = span
            docs = self._stem_docs[start:end]
            weight = self._idf(end - start) * quality_stem
            scores[docs] += self._stem_weights[start:end] * weight
            matched[docs] |= origin_bit

    def _prefix_stems(self, prefix: str, limit: int = 40) -> list[str]:
        """Основы, начинающиеся с префикса, — для коротких запросов."""
        self._finalize()
        start = bisect_left(self._stems, prefix)
        found = []
        for index in range(start, min(start + limit, len(self._stems))):
            if not self._stems[index].startswith(prefix):
                break
            found.append(self._stems[index])
        return found

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        kinds: Iterable[str] | None = None,
        predicate=None,
    ) -> list[Hit]:
        self._finalize()

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        count = len(self._doc_ids)
        scores = np.zeros(count, dtype=np.float64)
        matched = np.zeros(count, dtype=np.uint32)

        # Бит на каждое различное слово запроса. Повторы делят бит: покрытие
        # считается по различным словам, как и раньше по множеству.
        bits: dict[str, int] = {}
        for token in query_tokens:
            if token not in bits:
                bits[token] = 1 << len(bits)

        for token in query_tokens:
            bit = bits[token]
            self._score_token(token, bit, QUALITY_EXACT_TOKEN, QUALITY_STEM,
                              scores, matched)
            # Как называют против того, как названо: «заказ клиента» против
            # `ЗаказПокупателя`. Совпадения по синониму засчитываются исходному
            # слову запроса, чтобы покрытие считалось честно.
            for alias in self.synonyms.get(token, ()):
                self._score_token(alias, bit, QUALITY_SYNONYM,
                                  QUALITY_SYNONYM * 0.7, scores, matched)

        # Псевдоним из словаря: «справочник физлиц» -> конкретные объекты.
        # Это не догадка по тексту, а указание человека, поэтому вес выше всего.
        #
        # Разрешается до проверки на пустую выдачу намеренно. Локальный слой
        # словаря заведён под терминологию внедрения — жаргон, сокращения,
        # свои соглашения; пересечения с именами объектов у них нет, иначе
        # псевдоним был бы не нужен. Требование лексического совпадения
        # отключало бы псевдоним ровно там, где он единственный способ попасть
        # в цель, и молча: запрос возвращал пустоту.
        alias_positions = [
            self._position[doc_id]
            for doc_id in self._alias_targets(query_tokens)
            if doc_id in self._position
        ]
        all_bits = (1 << len(bits)) - 1
        for offset, index in enumerate(alias_positions):
            scores[index] += 1000.0 - offset
            matched[index] = all_bits

        # Точное совпадение всей строки разрешается здесь же, до выхода по
        # пустой выдаче. Слова в идентификаторе разделяет CamelCase, а строчными
        # границы нет: «стрнайти» остаётся одним токеном, которого в индексе не
        # существует, и запрос молча возвращал пустоту — при том что имя
        # совпадает целиком. Тот же дефект однажды чинили для псевдонимов.
        # Затравка даётся только тем, кто иначе не попал бы в выдачу вовсе:
        # у остальных оценка уже набрана токенами, и трогать её нельзя.
        exact = self._exact.get(normalize(query).replace(" ", ""), ())
        for index in exact:
            if scores[index] == 0.0:
                scores[index] = 1.0
                matched[index] = all_bits

        found = np.flatnonzero(scores)
        if found.size == 0:
            return []

        if kinds:
            allowed = np.zeros(count, dtype=bool)
            for kind in kinds:
                mask = self._kind_masks.get(kind)
                if mask is not None:
                    allowed |= mask
            found = found[allowed[found]]
        if predicate is not None:
            # Редкий путь: снаружи предикат сейчас не передаёт никто, отбор
            # делается уже по выдаче. Поэтому здесь обычный цикл.
            keep = np.fromiter(
                (bool(predicate(self.docs[self._doc_ids[i]])) for i in found),
                dtype=bool,
                count=found.size,
            )
            found = found[keep]
        if found.size == 0:
            return []

        # Порядок множителей тот же, что был до перехода на массивы: умножение
        # чисел с плавающей точкой не ассоциативно, и перестановка сдвигала бы
        # последние биты — а на них разрешаются ничьи.
        coverage = np.bitwise_count(matched[found]) / len(bits)
        extra = np.maximum(self._length[found] - len(query_tokens), 0)
        length_factor = 1.0 / (1.0 + LENGTH_PENALTY * extra)
        final = scores[found] * (0.35 + 0.65 * coverage) * self._boost[found] * length_factor

        # Явный оборот из вопроса («в запросе», «языка запросов»…) — признак,
        # которого не бывает в именах элементов, поэтому проверяется один раз
        # на запрос, а не на документ. Маска по видам считается только здесь,
        # под условием: обороты не встречаются ни в одном из 61 051 запроса
        # автоматических наборов, и на подавляющем большинстве запросов эта
        # ветка не выполняется вовсе.
        if _has_query_language_marker(query):
            boost_mask = np.zeros(count, dtype=bool)
            for kind in QUERY_LANGUAGE_KINDS:
                kind_mask = self._kind_masks.get(kind)
                if kind_mask is not None:
                    boost_mask |= kind_mask
            final[boost_mask[found]] *= QUERY_LANGUAGE_BOOST

        # Точное совпадение всей строки запроса — вне конкуренции.
        #
        # Пробовали ослаблять множитель для служебных видов пропорционально
        # весу — `ОпределяемыйТип` перехватывает запросы в единственном числе
        # («контрагент», «склад», «физическое лицо»), потому что именуется в
        # единственном, а справочники во множественном. Не помогло и вышло в
        # минус: ручной набор не сдвинулся (75%), автоматический просел на
        # 0,1 п.п. на всех трёх конфигурациях. Причина не здесь — точный токен
        # весит 1.0 против 0.55 у совпадения по основе, и разрыв набирается
        # до множителя, а не им. Подробности в CHANGELOG.
        is_alias = np.isin(found, np.asarray(alias_positions, dtype=np.int64))
        if exact:
            is_exact = np.isin(found, np.asarray(exact, dtype=np.int64)) & ~is_alias
            final[is_exact] *= EXACT_BOOST
        else:
            is_exact = np.zeros(found.size, dtype=bool)
        final[is_alias] += 10_000.0

        # Ничьи разрешаются идентификатором — как это делала сортировка по
        # `(-оценка, идентификатор)`, пока постинги лежали в словарях.
        order = np.lexsort((self._lex_rank[found], -final))[: min(limit, found.size)]

        hits: list[Hit] = []
        for position in order:
            index = int(found[position])
            reason = ""
            if is_alias[position]:
                reason = "псевдоним из словаря"
            elif is_exact[position]:
                reason = "точное совпадение"
            elif coverage[position] == 1.0:
                reason = "все слова запроса"
            hits.append(
                Hit(
                    doc=self.docs[self._doc_ids[index]],
                    score=float(final[position]),
                    reason=reason,
                )
            )
        return hits


# --------------------------------------------------------------- адаптеры


def index_configuration(
    config,
    weights: dict[str, float] | None = None,
    *,
    synonyms: dict[str, frozenset[str]] | None = None,
    aliases: dict[str, list[str]] | None = None,
) -> SearchIndex:
    """Индекс по объектам метаданных конфигурации."""
    docs = []
    for obj in config.objects.values():
        docs.append(
            Doc(
                id=obj.full_name,
                kind=obj.kind,
                payload=obj,
                exact_keys=[obj.full_name],
                boost=OWNER_KIND_BOOST.get(obj.kind, 0.7),
                fields={
                    "name": obj.name,
                    # Пустой синоним у объекта — обычное дело; тогда для поиска
                    # по-человечески годится только разрезанное имя.
                    "synonym": obj.synonym or "",
                    "kind": obj.kind,
                    "description": obj.comment or "",
                },
            )
        )
    return SearchIndex(docs, weights, synonyms=synonyms, aliases=aliases)


@dataclass(slots=True)
class FieldRef:
    """Поле объекта конфигурации: реквизит, измерение, ресурс, реквизит ТЧ."""

    object_full_name: str
    object_title: str
    path: str
    kind: str
    field: object

    @property
    def full_name(self) -> str:
        return f"{self.object_full_name}.{self.path}"


FIELD_KIND_TITLES = {
    "attribute": "реквизит",
    "dimension": "измерение",
    "resource": "ресурс",
    "tabular": "реквизит табличной части",
}


def iter_field_refs(config) -> Iterable[FieldRef]:
    """Поля всех объектов конфигурации — по одному описанию на поле.

    Вынесено из `index_fields`, потому что нужно дважды: при построении
    индекса и при подъёме его из кэша, где документы уже есть, а полезную
    нагрузку надо собрать заново. На диск она не кладётся — это те же поля
    конфигурации, которая и так загружена.
    """
    for obj in config.objects.values():
        title = obj.synonym or obj.name

        groups = [
            ("attribute", "", obj.attributes),
            ("dimension", "", obj.dimensions),
            ("resource", "", obj.resources),
        ]
        for part in obj.tabular_parts:
            groups.append(("tabular", part.name, part.attributes))

        for kind, part_name, items in groups:
            for item in items:
                path = f"{part_name}.{item.name}" if part_name else item.name
                yield FieldRef(
                    object_full_name=obj.full_name,
                    object_title=title,
                    path=path,
                    kind=kind,
                    field=item,
                )


def index_fields(
    config,
    weights: dict[str, float] | None = None,
    *,
    synonyms: dict[str, frozenset[str]] | None = None,
) -> SearchIndex:
    """Отдельный индекс по реквизитам объектов.

    Отдельный, а не общий с объектами, по двум причинам. Полей втрое больше,
    чем объектов, и в общем индексе они забивали бы выдачу там, где нужен
    объект. И качество поиска по объектам измерено — смешивать индексы значит
    ставить измеренный результат под удар ради непроверенного улучшения.

    Закрывает типовой вопрос «где хранится телефон клиента»: сам объект по
    такому запросу не находится, потому что искомое лежит в его реквизите.
    """
    docs = []
    for ref in iter_field_refs(config):
        obj = config.objects[ref.object_full_name]
        item = ref.field
        part_name = ref.path.rsplit(".", 1)[0] if "." in ref.path else ""
        docs.append(
            Doc(
                id=ref.full_name,
                kind=ref.kind,
                payload=ref,
                exact_keys=[ref.full_name, item.name],
                boost=OWNER_KIND_BOOST.get(obj.kind, 0.7),
                fields={
                    "name": item.name,
                    "synonym": item.synonym or "",
                    # Имя объекта в поле родителя: «телефон контрагента»
                    # должен находиться, хотя слово «контрагент» есть
                    # только у объекта, а «телефон» — только у поля.
                    "part": part_name,
                    "parent": f"{obj.name} {obj.synonym}".strip(),
                    "description": item.comment or "",
                },
            )
        )
    return SearchIndex(docs, weights, exact_fields=(), synonyms=synonyms)


def index_syntax(syntax, weights: dict[str, float] | None = None) -> SearchIndex:
    """Индекс по элементам справки платформы."""
    docs = []
    for item in syntax.items.values():
        docs.append(
            Doc(
                id=item.id,
                kind=item.kind,
                payload=item,
                exact_keys=[
                    key
                    for key in (
                        item.name_ru,
                        item.name_en,
                        item.full_ru,
                        item.full_en,
                    )
                    if key
                ],
                fields={
                    "name": item.name_ru,
                    "name_en": item.name_en,
                    "parent": f"{item.parent_ru} {item.parent_en}".strip(),
                    "kind": item.kind,
                    # Ключи — отдельный слой, а не часть элемента: они сочинены
                    # нами, справка про них ничего не знает. Поэтому берутся по
                    # идентификатору здесь, при сборке индекса, и в `SyntaxItem`
                    # не попадают — правило в `docs/data-sources.md`.
                    "keys": keys_text(item.id),
                    # Метки мест, где стоят таблицы, — разметка карточки, а
                    # не текст справки: попав в индекс, служебное слово стало
                    # бы токеном на трети страниц языка запросов.
                    "description": (
                        без_меток(item.description)
                        if item.kind in QUERY_LANGUAGE_KINDS
                        else item.description[:DESCRIPTION_CAP]
                    ),
                },
            )
        )
    return SearchIndex(docs, weights, exact_fields=())
