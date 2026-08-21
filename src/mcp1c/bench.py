"""Стенд для замеров качества поиска.

Без цифр «сделали лучше» — это мнение. Здесь наборы запросов с известными
правильными ответами и метрики поверх них.

Набор собирается двумя способами:

* **автоматически из данных** — синоним объекта как запрос, сам объект как
  ожидаемый ответ. Это основной сценарий: агент формулирует по-человечески,
  сервер обязан выдать точное имя;
* **вручную** — трудные случаи, где автоматика бессильна: чужая терминология,
  опечатки, вопросы «где хранится …».

Ручные наборы лежат в `tests/queries/*.json` и пополняются по мере встреч
с реальными промахами.

Меряется четыре вещи, и три из них появились потому, что руками их считать
приходилось всё равно:

* **P@1, P@3, P@5, P@10 и MRR** — попал ли правильный ответ в первые `k`;
* **чужой домен первым** — ждали язык запросов, получили платформу или
  наоборот. Так руками считали «магнит выигрывает 5 вопросов из 27»;
* **отрыв первого от второго**, медианой — «уверенно попали или чудом».
  Ничья даёт ноль, и такой результат нельзя выдавать за победу правила;
* **сверка пометок** — говорит ли `note` в наборе то же, что вышло. Пометки
  устаревают молча: после слоя поисковых ключей восемь записей «ИЗВЕСТНЫЙ
  ПРОМАХ» остались на запросах, которые давно первые.

Прогон сохраняется (`--save`) и сравнивается со следующим (`--baseline`),
который называет **поимённо**, кто сменил место. Разовые скрипты для этого
писались трижды за две сессии, и каждый раз выбрасывались.

Запуск::

    PYTHONPATH=src .venv/bin/python -m mcp1c.bench \\
        --data data --config РозницаДляКазахстана \\
        --auto --sets query-language,roznica-metadata,modules-procedures \\
        --check-notes

Все ручные наборы используют schema v1 с явным ``domain``: ``syntax``,
``metadata`` или ``procedures``. Имя файла не выбирает индекс. Для процедур
публичный ``expected`` содержит bare-имя BSL без имени модуля; стенд
разворачивает его во все точные адреса выбранного корпуса без учёта регистра.

Порогов в assert здесь нет намеренно: наборы запросов — не тесты. Проценты
ломались бы от каждой правки словаря, поэтому стенд печатает цифры, а решение
принимает человек. Код 1 означает расхождение пометок, код 2 — что замер
целиком не состоялся: набор, выбранный корпус или индекс негодны. Качество
поиска остаётся цифрой, а не порогом CI.
"""

from __future__ import annotations

import json
import random
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .search import SearchIndex
from .syntax_model import QUERY_LANGUAGE_KINDS

# Наборы лежат в репозитории и **в образ не входят** (`tests/` в
# `.dockerignore`). Мерить нужно из рабочей копии, а не из контейнера:
# `PYTHONPATH=src .venv/bin/python -m mcp1c.bench`. На эти грабли уже
# наступали — стенд отчитался «0 запросов, 0% первым», и ноль уехал в доску
# задач как настоящий результат.
QUERIES_DIR = Path(__file__).resolve().parents[2] / "tests" / "queries"
SET_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
DOMAINS = frozenset({"syntax", "metadata", "procedures"})


@dataclass(slots=True)
class Case:
    query: str
    expected: list[str]
    note: str = ""
    # Машинная фиксация текущего baseline используется только вместе с
    # --check-notes. Это не порог качества pytest: изменение места требует
    # человека обновить объяснение в публичном наборе.
    expected_miss: bool = False
    expected_rank: int | None = None


@dataclass(frozen=True, slots=True)
class Suite:
    """Один именованный набор запросов с явно заданным поисковым доменом."""

    name: str
    domain: str
    cases: list[Case]


@dataclass(slots=True)
class CaseResult:
    """Что получилось по одному запросу. Из этого считаются все метрики.

    Хранится поимённо, а не свёрнутым в счётчики: сравнение двух прогонов
    должно называть, **кто** сменил место, а не только «стало на три больше».
    Разовые скрипты для этого писались трижды за две сессии.
    """

    query: str
    expected: list[str]
    # Имя набора и домен входят в устойчивый ключ baseline. Один и тот же
    # текст запроса закономерно встречается в справке, метаданных и коде.
    suite: str = ""
    domain: str = ""
    got: list[str] = field(default_factory=list)
    # Место правильного ответа, 0 — первое. `None` — не нашёлся в пределах
    # `limit`.
    rank: int | None = None
    # Отрыв первого от второго, долей от счёта первого. Отвечает на «уверенно
    # попали или чудом»: ничья 41,107076 против 41,107076 даёт ноль, и такой
    # результат нельзя выдавать за победу правила.
    separation: float = 0.0
    # Первым пришёл элемент чужого домена: ждали язык запросов — получили
    # платформу, или наоборот. Ровно та мера, которой руками считали «магнит
    # выигрывает 5 вопросов из 27».
    foreign_first: bool = False
    note: str = ""
    expected_miss: bool = False
    expected_rank: int | None = None

    def to_dict(self) -> dict:
        # Выдача сохраняется только там, где она о чём-то говорит: у запроса,
        # взявшего первое место, она и так известна из `expected`. На
        # автоматических наборах это разница между 74 МБ и полутора: 61 тысяча
        # запросов по десять идентификаторов в каждом.
        got = [] if self.rank == 0 else self.got[:3]
        payload = {
            "suite": self.suite,
            "domain": self.domain,
            "query": self.query,
            "expected": list(self.expected),
            "got": got,
            "rank": self.rank,
            "separation": round(self.separation, 6),
            "foreign_first": self.foreign_first,
            "note": self.note,
        }
        # Автоматические наборы дают десятки тысяч строк. Пустые ожидания
        # baseline не должны раздувать каждый сохранённый результат.
        if self.expected_miss:
            payload["expected_miss"] = True
        if self.expected_rank is not None:
            payload["expected_rank"] = self.expected_rank
        return payload

    @classmethod
    def from_dict(cls, raw: dict) -> "CaseResult":
        return cls(
            suite=raw["suite"],
            domain=raw["domain"],
            query=raw["query"],
            expected=list(raw.get("expected") or []),
            got=list(raw.get("got") or []),
            rank=raw.get("rank"),
            separation=raw.get("separation", 0.0),
            foreign_first=raw.get("foreign_first", False),
            note=raw.get("note", ""),
            expected_miss=raw.get("expected_miss", False),
            expected_rank=raw.get("expected_rank"),
        )


@dataclass(slots=True)
class Report:
    results: list[CaseResult] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return len(self.results)

    def rate(self, value: int) -> float:
        return value / self.total if self.total else 0.0

    def at(self, k: int) -> int:
        """Сколько запросов дали правильный ответ в пределах первых `k`."""
        return sum(1 for r in self.results if r.rank is not None and r.rank < k)

    @property
    def hit1(self) -> int:
        return self.at(1)

    @property
    def hit5(self) -> int:
        return self.at(5)

    @property
    def hit10(self) -> int:
        return self.at(10)

    @property
    def mrr(self) -> float:
        return sum(1 / (r.rank + 1) for r in self.results if r.rank is not None)

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if r.rank is None]

    @property
    def foreign_first(self) -> int:
        return sum(1 for r in self.results if r.foreign_first)

    @property
    def separation(self) -> float:
        """Медиана отрыва по тем запросам, где ответ пришёл первым.

        Медиана, а не среднее: один запрос с отрывом в разы (точное совпадение
        имени даёт множитель 12) утащил бы среднее и спрятал бы то, что
        остальные победы шаткие.
        """
        значения = sorted(r.separation for r in self.results if r.rank == 0)
        if not значения:
            return 0.0
        середина = len(значения) // 2
        if len(значения) % 2:
            return значения[середина]
        return (значения[середина - 1] + значения[середина]) / 2

    def render(self, title: str, show_failures: int = 10) -> str:
        if not self.total:
            return f"=== {title} ===\nзапросов        : 0"
        lines = [
            f"=== {title} ===",
            f"запросов        : {self.total}",
            f"P@1             : {self.hit1:>5}  {self.rate(self.hit1):6.1%}",
            f"P@3             : {self.at(3):>5}  {self.rate(self.at(3)):6.1%}",
            f"P@5             : {self.hit5:>5}  {self.rate(self.hit5):6.1%}",
            f"P@10            : {self.hit10:>5}  {self.rate(self.hit10):6.1%}",
            f"MRR             : {self.mrr / self.total:.3f}",
            f"чужой домен 1-м : {self.foreign_first:>5}  "
            f"{self.rate(self.foreign_first):6.1%}",
            f"отрыв (медиана) : {self.separation:6.1%}",
            f"время           : {self.elapsed * 1000:.0f} мс "
            f"({self.elapsed / self.total * 1000:.2f} мс на запрос)",
        ]
        промахи = self.failures
        if промахи and show_failures:
            lines.append("")
            lines.append(f"промахи ({len(промахи)}), первые {show_failures}:")
            for r in промахи[:show_failures]:
                lines.append(f"  «{r.query}»")
                lines.append(f"    ждали : {', '.join(r.expected)}")
                lines.append(f"    дали  : {', '.join(r.got[:3]) or '—'}")
        return "\n".join(lines)


def _domain(kind: str) -> str:
    """Домен элемента: язык запросов против всего остального.

    Грубее, чем вид, и намеренно: мера отвечает на «попали ли вообще не в ту
    справку», а не «какого вида элемент». Различать метод и свойство здесь
    незачем — оба из справки платформы.
    """
    return "query" if kind in QUERY_LANGUAGE_KINDS else "platform"


def run(
    index: SearchIndex,
    cases: list[Case],
    limit: int = 10,
    *,
    suite: str = "",
    domain: str = "",
) -> Report:
    report = Report()
    started = time.perf_counter()

    for case in cases:
        hits = index.search(case.query, limit=limit)
        got = [hit.doc.id for hit in hits]
        expected = set(case.expected)
        rank = next((i for i, doc_id in enumerate(got) if doc_id in expected), None)

        separation = 0.0
        if len(hits) > 1 and hits[0].score:
            separation = (hits[0].score - hits[1].score) / hits[0].score

        # Домен считается только когда первым пришёл не тот элемент: у
        # правильного ответа домен по определению совпадает.
        foreign = False
        if hits and rank != 0:
            ждали = {
                _domain(doc.kind)
                for doc in (index.docs.get(i) for i in case.expected)
                if doc is not None
            }
            foreign = bool(ждали) and _domain(hits[0].doc.kind) not in ждали

        report.results.append(
            CaseResult(
                suite=suite,
                domain=domain,
                query=case.query,
                expected=list(case.expected),
                got=got,
                rank=rank,
                separation=separation,
                foreign_first=foreign,
                note=case.note,
                expected_miss=case.expected_miss,
                expected_rank=case.expected_rank,
            )
        )

    report.elapsed = time.perf_counter() - started
    return report


# --------------------------------------------------------------- наборы


def cases_from_configuration(config, sample: int = 0, seed: int = 1) -> list[Case]:
    """Синоним объекта -> сам объект."""
    cases = [
        Case(query=obj.synonym, expected=[obj.full_name], note="синоним")
        for obj in config.objects.values()
        if obj.synonym and obj.synonym != obj.name
    ]
    if sample and sample < len(cases):
        random.Random(seed).shuffle(cases)
        cases = cases[:sample]
    return cases


def cases_from_syntax(syntax, sample: int = 0, seed: int = 1) -> list[Case]:
    """Полное имя элемента справки -> элемент, на русском и на английском.

    Для членов объектов запрос обязан быть квалифицированным: `OnWrite` есть у
    десятков объектов, и требовать конкретный — значит мерить невозможное.
    Неоднозначные запросы проверяет `cases_from_syntax_ambiguous`.
    """
    # Полное имя не уникально: `Ссылка` есть и у объекта, и у таблицы запроса.
    # Правильным считается любой элемент с этим именем.
    by_full: dict[str, list[str]] = {}
    for item in syntax.items.values():
        for key in (item.full_ru, item.full_en):
            if key:
                by_full.setdefault(key, []).append(item.id)

    cases: list[Case] = []
    for item in syntax.items.values():
        ru = item.full_ru if item.parent_ru else item.name_ru
        en = item.full_en if item.parent_en else item.name_en
        if ru:
            cases.append(Case(query=ru, expected=by_full.get(ru, [item.id]), note="рус"))
        if en and en != ru:
            cases.append(Case(query=en, expected=by_full.get(en, [item.id]), note="англ"))
    if sample and sample < len(cases):
        random.Random(seed).shuffle(cases)
        cases = cases[:sample]
    return cases


def cases_from_syntax_ambiguous(syntax, sample: int = 0, seed: int = 1) -> list[Case]:
    """Голое имя члена -> любой элемент с таким именем.

    Так агент спрашивает чаще всего: «ПриЗаписи», «Найти», «Количество».
    Правильным считается любой одноимённый элемент — выбрать между ними без
    контекста невозможно, и задача поиска здесь в том, чтобы вообще попасть в
    нужную группу.
    """
    by_name: dict[str, list[str]] = {}
    for item in syntax.items.values():
        if item.name_ru:
            by_name.setdefault(item.name_ru, []).append(item.id)
    cases = [
        Case(query=name, expected=ids, note=f"одноимённых: {len(ids)}")
        for name, ids in by_name.items()
    ]
    if sample and sample < len(cases):
        random.Random(seed).shuffle(cases)
        cases = cases[:sample]
    return cases


def load_cases(path: str | Path) -> Suite:
    """Читает единственную явную схему набора запросов.

    Старый корневой JSON-массив намеренно не поддерживается: домен из имени
    файла угадывался и мог незаметно направить запросы не в тот индекс.
    """
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(
            f"Набор {path.name} использует старый формат. Требуются поля "
            "schema_version, domain и cases."
        )
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SET_SCHEMA_VERSION
    ):
        raise ValueError(
            f"Набор {path.name}: schema_version должна быть "
            f"{SET_SCHEMA_VERSION}."
        )
    domain = payload.get("domain")
    if domain not in DOMAINS:
        raise ValueError(
            f"Набор {path.name}: domain должна быть одной из "
            f"{', '.join(sorted(DOMAINS))}."
        )
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"Набор {path.name} не содержит запросов в cases.")

    cases: list[Case] = []
    запросы: set[str] = set()
    for номер, item in enumerate(raw_cases, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Набор {path.name}, cases[{номер}]: нужна запись.")
        query = item.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"Набор {path.name}, cases[{номер}]: query должна быть "
                "непустой строкой."
            )
        ключ = query.strip().casefold()
        if ключ in запросы:
            raise ValueError(
                f"Набор {path.name}: запрос «{query}» повторяется без учёта "
                "регистра."
            )
        запросы.add(ключ)
        expected = item.get("expected")
        if (
            not isinstance(expected, list)
            or not expected
            or any(
                not isinstance(value, str) or not value.strip()
                for value in expected
            )
        ):
            raise ValueError(
                f"Набор {path.name}, cases[{номер}]: expected должен быть "
                "непустым массивом строк."
            )
        note = item.get("note", "")
        if not isinstance(note, str):
            raise ValueError(
                f"Набор {path.name}, cases[{номер}]: note должна быть строкой."
            )
        expected_miss = item.get("expected_miss", False)
        expected_rank = item.get("expected_rank")
        if type(expected_miss) is not bool:
            raise ValueError(
                f"Набор {path.name}, cases[{номер}]: expected_miss должен "
                "быть true или false."
            )
        if expected_rank is not None and (
            type(expected_rank) is not int or expected_rank < 0
        ):
            raise ValueError(
                f"Набор {path.name}, cases[{номер}]: expected_rank должен "
                "быть целым числом от нуля."
            )
        if expected_miss and expected_rank is not None:
            raise ValueError(
                f"Набор {path.name}, cases[{номер}]: expected_miss и "
                "expected_rank взаимоисключающие."
            )
        cases.append(
            Case(
                query=query.strip(),
                expected=list(dict.fromkeys(value.strip() for value in expected)),
                note=note,
                expected_miss=expected_miss,
                expected_rank=expected_rank,
            )
        )
    return Suite(name=path.stem, domain=domain, cases=cases)


def load_curated(name: str) -> Suite:
    """Живой набор запросов по имени без расширения: `query-language`.

    Отсутствие файла — ошибка, а не пустой набор. Молчаливый ноль здесь хуже
    всего: стенд отчитается «0 запросов, 0% первым», и цифра уедет в доску
    задач как настоящая. Один раз уже стоило замера, снятого с опечатки в
    имени.
    """
    path = QUERIES_DIR / f"{name.removesuffix('.json')}.json"
    if not path.exists():
        доступные = (
            ", ".join(sorted(p.stem for p in QUERIES_DIR.glob("*.json")))
            if QUERIES_DIR.exists()
            else "каталога нет — наборы не входят в образ, мерить нужно из репозитория"
        )
        raise ValueError(f"Набор `{name}` не найден. Есть: {доступные}")
    return load_cases(path)


# ------------------------------------------------------- сохранение прогонов


def save_report(report: Report, path: str | Path, *, title: str = "") -> Path:
    """Прогон на диск, чтобы следующий было с чем сравнить.

    Дата в имя файла не зашивается: её кладёт вызывающий. Здесь — только
    содержимое, иначе тесты пришлось бы привязывать ко времени.
    """
    if any(
        result.domain not in DOMAINS or not result.suite
        for result in report.results
    ):
        raise ValueError(
            "Каждый результат отчёта должен содержать suite и корректный domain."
        )
    path = Path(path)
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "title": title,
        "elapsed": report.elapsed,
        "results": [r.to_dict() for r in report.results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    временный: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            временный = Path(stream.name)
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        временный.replace(path)
        временный = None
    finally:
        if временный is not None:
            try:
                временный.unlink(missing_ok=True)
            except OSError:
                # Исходный отчёт не подменён. Ошибку записи выше вернёт CLI;
                # расходный временный файл не должен скрывать её второй.
                pass
    return path


def load_report(path: str | Path) -> Report:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != REPORT_SCHEMA_VERSION
    ):
        raise ValueError(
            "Отчёт имеет неподдерживаемую schema_version; перезапустите стенд "
            "и сохраните новый baseline."
        )
    try:
        results = [CaseResult.from_dict(r) for r in payload.get("results") or []]
    except (KeyError, TypeError) as error:
        raise ValueError(
            "Отчёт schema v1 повреждён: нет identity результата."
        ) from error
    if any(result.domain not in DOMAINS or not result.suite for result in results):
        raise ValueError(
            "Отчёт schema v1 содержит неизвестный domain или пустой suite."
        )
    return Report(results=results, elapsed=payload.get("elapsed", 0.0))


@dataclass(slots=True)
class Move:
    """Один запрос, сменивший место между прогонами."""

    suite: str
    domain: str
    query: str
    before: int | None
    after: int | None

    @property
    def better(self) -> bool:
        """Вверх — это к первому месту; `None` считается хуже любого места."""
        if self.after is None:
            return False
        return self.before is None or self.after < self.before

    def render(self) -> str:
        def место(rank: int | None) -> str:
            return "промах" if rank is None else str(rank + 1)

        знак = "+" if self.better else "-"
        return (
            f"  {знак} [{self.domain}/{self.suite}] «{self.query}»: "
            f"{место(self.before)} -> {место(self.after)}"
        )


def compare(before: Report, after: Report) -> list[Move]:
    """Кто именно сменил место. Запросы сверяются по тексту, а не по порядку.

    Порядок в наборе меняется при любой правке файла, а привязка по индексу
    молча сравнивала бы разные запросы — и показывала бы движение там, где
    его нет.
    """
    старые = {(r.suite, r.domain, r.query): r.rank for r in before.results}
    ходы = [
        Move(
            suite=r.suite,
            domain=r.domain,
            query=r.query,
            before=старые[(r.suite, r.domain, r.query)],
            after=r.rank,
        )
        for r in after.results
        if (r.suite, r.domain, r.query) in старые
        and старые[(r.suite, r.domain, r.query)] != r.rank
    ]
    # Сначала ухудшения: регресс важнее прочитать, чем выигрыш.
    return sorted(ходы, key=lambda m: (m.better, m.domain, m.suite, m.query))


# ------------------------------------------------------------ сверка пометок

# Пометка обязана начинаться с места, иначе сверять нечего. Разбирается только
# начало строки: остальное — человеческий текст про причину и лечение.
_ПОМЕТКА_ПРОМАХ = "ИЗВЕСТНЫЙ ПРОМАХ"
_ПОМЕТКА_ПЕРВОЕ = "первое место"


def check_notes(report: Report) -> list[str]:
    """Расхождения между тем, что пометка обещает, и тем, что вышло.

    Пометки устаревают молча и незаметно: после слоя поисковых ключей восемь
    записей «ИЗВЕСТНЫЙ ПРОМАХ» остались на запросах, которые уже первые, и
    файл описывал состояние, которого больше нет. Правились руками; здесь то
    же самое делает стенд в день прогона.
    """
    расхождения = []
    for r in report.results:
        note = r.note.strip()
        место = "промах" if r.rank is None else str(r.rank + 1)
        if r.expected_miss and r.rank is not None:
            расхождения.append(
                f"«{r.query}»: ожидался промах, а место {место}"
            )
        elif r.expected_rank is not None and r.rank != r.expected_rank:
            ожидалось = r.expected_rank + 1
            расхождения.append(
                f"«{r.query}»: ожидалось {ожидалось}, а место {место}"
            )
        elif note.startswith(_ПОМЕТКА_ПРОМАХ) and r.rank == 0:
            расхождения.append(
                f"«{r.query}»: пометка говорит «{_ПОМЕТКА_ПРОМАХ}», "
                f"а запрос первый"
            )
        elif note.startswith(_ПОМЕТКА_ПЕРВОЕ) and r.rank != 0:
            расхождения.append(
                f"«{r.query}»: пометка говорит «{_ПОМЕТКА_ПЕРВОЕ}», "
                f"а место {место}"
            )
    return расхождения


# --------------------------------------------------------------- точка входа


def build_index(data_dir: str | Path, config: str | None = None):
    """Индекс, по которому меряем: тот же, что отдаёт сервер.

    Собирается из реестра, а не отдельной сборкой: замер по индексу, который
    строится не так, как рабочий, отвечает не на тот вопрос.
    """
    from .registry import Registry

    registry = Registry(Path(data_dir))
    registry.restore()
    context = registry.resolve(config, require_configuration=False)
    return registry, context


def _procedure_cases(loaded, suite: Suite) -> list[Case]:
    """Разворачивает bare-имена BSL в точные адреса текущего корпуса.

    BSL нечувствителен к регистру, а имя не уникально между модулями. Поэтому
    публичный набор хранит безопасное имя процедуры, а стенд считает верным
    любой точный адрес с этим именем. Строка с ``::`` — точный адрес для
    синтетических и локальных наборов.
    """
    результат: list[Case] = []
    for case in suite.cases:
        addresses: list[str] = []
        for expected in case.expected:
            if "::" in expected:
                # Канонический точный адрес — обычный O(1) lookup. Fallback
                # нужен только синтетическому case-insensitive адресу и
                # ограничен одноимёнными записями оглавления, а не всем
                # 49-тысячным SearchIndex.
                найден = loaded.поиск.docs.get(expected)
                if найден is not None:
                    найденные = [найден.id]
                else:
                    модуль, _, имя = expected.rpartition("::")
                    найденные = [
                        address
                        for запись in loaded.оглавление.по_имени(имя)
                        if запись.экспорт
                        and запись.модуль.casefold() == модуль.casefold()
                        and (
                            address := f"{запись.модуль}::{запись.имя}"
                        ) in loaded.поиск.docs
                    ]
            else:
                найденные = [
                    address
                    for запись in loaded.оглавление.по_имени(expected)
                    if запись.экспорт
                    and (
                        address := f"{запись.модуль}::{запись.имя}"
                    ) in loaded.поиск.docs
                ]
            if not найденные:
                raise ValueError(
                    f"Набор {suite.name}: ожидаемая процедура `{expected}` "
                    "не найдена в выбранном корпусе кода."
                )
            addresses.extend(найденные)
        результат.append(
            Case(
                query=case.query,
                expected=list(dict.fromkeys(addresses)),
                note=case.note,
                expected_miss=case.expected_miss,
                expected_rank=case.expected_rank,
            )
        )
    return результат


def _loaded_procedure_index(registry, config: str | None, extension: str | None):
    """Снимок выбранного корпуса без поиска и дискового I/O под замком."""
    from .registry import RegistryError

    context = registry.resolve(config, extension=extension)
    loaded = context.extension if extension is not None else context.modules
    if loaded is None:
        что = f"расширения {extension}" if extension is not None else "конфигурации"
        raise RegistryError(f"Индекс кода {что} не загружен.")
    if not loaded.готов:
        if loaded.source.status == "error":
            # Source.error хранит техническую причину приёма и может
            # содержать абсолютный путь. Стенд публично сообщает состояние,
            # а подробность остаётся на административной странице источника.
            raise RegistryError(
                "Индекс кода не построен; подробность доступна в состоянии "
                "источника."
            )
        обработано, всего = loaded.прогресс
        raise RegistryError(
            "Индекс кода ещё строится: обработано "
            f"{обработано} из {всего} элементов текущего этапа."
        )
    if loaded.поиск is None:
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")
    return context, loaded


def _procedure_snapshot_is_current(registry, context, loaded) -> bool:
    """Короткий CAS identity после прогона; тяжёлая работа сделана без lock."""
    configuration = context.configuration
    if configuration is None:
        return False
    with registry._lock:
        return (
            registry.configurations.get(context.name) is configuration
            and registry.sources.get(configuration.source.id) is configuration.source
            and registry.sources.get(loaded.source.id) is loaded.source
            and registry.modules.get(loaded.source.id) is loaded
        )


def run_procedures(
    registry,
    suite_or_path: Suite | str | Path,
    *,
    config: str | None = None,
    extension: str | None = None,
    limit: int = 10,
) -> Report:
    """Прогоняет набор целиком по одному поколению procedure SearchIndex."""
    from .registry import RegistryError

    suite = (
        suite_or_path
        if isinstance(suite_or_path, Suite)
        else load_cases(suite_or_path)
    )
    if suite.domain != "procedures":
        raise ValueError(
            f"Набор {suite.name}: run_procedures требует domain procedures."
        )
    if not suite.cases:
        raise ValueError(f"Набор {suite.name} не содержит запросов.")

    for _ in range(2):
        context, loaded = _loaded_procedure_index(registry, config, extension)
        cases = _procedure_cases(loaded, suite)
        report = run(
            loaded.поиск,
            cases,
            limit=limit,
            suite=suite.name,
            domain=suite.domain,
        )
        if _procedure_snapshot_is_current(registry, context, loaded):
            return report
    raise RegistryError(
        "Код или конфигурация изменились во время замера дважды; "
        "повторите прогон после завершения загрузки."
    )


def _наборы(имена: list[str]) -> dict[str, Suite]:
    return {имя: load_curated(имя) for имя in имена}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp1c-bench",
        description=(
            "Замер качества поиска. Наборы берутся из репозитория "
            "(`tests/queries/`), в образ они не входят."
        ),
    )
    parser.add_argument("--data", default="data", help="каталог данных сервера")
    parser.add_argument(
        "--config",
        default=None,
        help="конфигурация для замера по метаданным и коду",
    )
    parser.add_argument(
        "--extension",
        default=None,
        help="расширение: отдельный корпус кода для наборов domain=procedures",
    )
    parser.add_argument(
        "--sets",
        default="",
        help=(
            "ручные наборы schema v1 через запятую, без расширения: "
            "query-language,modules-procedures"
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="автоматические наборы по справке: точные имена и одноимённые",
    )
    parser.add_argument("--limit", type=int, default=10, help="глубина выдачи")
    parser.add_argument(
        "--save",
        default=None,
        help=(
            "куда записать прогон (JSON) для сравнения. По уговору — "
            "`data/bench/ГГГГ-ММ-ДД.json`: `data/` вне git, а прогон это "
            "производное от кода и наборов, а не исходник"
        ),
    )
    parser.add_argument(
        "--baseline", default=None, help="прогон, с которым сравнить результат"
    )
    parser.add_argument(
        "--check-notes",
        action="store_true",
        help="сверить пометки в наборе с тем, какое место запрос занял",
    )
    args = parser.parse_args(argv)

    if not args.sets and not args.auto:
        parser.error("нечего мерить: задайте --sets и/или --auto")

    from .registry import RegistryError

    try:
        registry, context = build_index(args.data, args.config)
    except RegistryError as ошибка:
        # Реестр намеренно не выбирает конфигурацию молча, и стенду это
        # правило ослаблять незачем: индекс справки от выбора не зависит, а
        # набор по метаданным — зависит целиком.
        print(f"{ошибка}\nУкажите её флагом --config.")
        return 2
    except OSError:
        print("Стенд не запущен: не удалось прочитать каталог данных.")
        return 2

    try:
        наборы: dict[str, Suite] = {}
        if args.sets:
            наборы.update(
                _наборы([s.strip() for s in args.sets.split(",") if s.strip()])
            )
        if args.auto:
            if context.syntax is None:
                raise ValueError("--auto нечем мерить: справка не загружена")
            syntax = context.syntax.syntax
            наборы["auto-syntax-exact"] = Suite(
                name="auto-syntax-exact",
                domain="syntax",
                cases=cases_from_syntax(syntax),
            )
            наборы["auto-syntax-ambiguous"] = Suite(
                name="auto-syntax-ambiguous",
                domain="syntax",
                cases=cases_from_syntax_ambiguous(syntax),
            )
        if args.extension and not any(
            suite.domain == "procedures" for suite in наборы.values()
        ):
            raise ValueError(
                "--extension применим только к набору domain=procedures."
            )
        пустые = [suite.name for suite in наборы.values() if not suite.cases]
        if пустые:
            raise ValueError(
                "Наборы не содержат запросов: " + ", ".join(sorted(пустые))
            )

        # Сначала полностью готовятся ВСЕ отчёты. Ошибка последнего набора не
        # должна оставлять на stdout или на диске правдоподобный частичный
        # прогон первых наборов.
        отчёты: dict[str, Report] = {}
        for имя, suite in наборы.items():
            if suite.domain == "procedures":
                отчёт = run_procedures(
                    registry,
                    suite,
                    config=args.config,
                    extension=args.extension,
                    limit=args.limit,
                )
            else:
                index = _индекс_под_набор(context, suite)
                отчёт = run(
                    index,
                    suite.cases,
                    limit=args.limit,
                    suite=suite.name,
                    domain=suite.domain,
                )
            отчёты[имя] = отчёт
        ходы = None
        if args.baseline:
            прежний = load_report(args.baseline)
            свежий = Report(
                results=[r for о in отчёты.values() for r in о.results]
            )
            ходы = compare(прежний, свежий)
        сохранённый_путь = None
        if args.save:
            общий = Report(
                results=[r for о in отчёты.values() for r in о.results],
                elapsed=sum(о.elapsed for о in отчёты.values()),
            )
            # Запись предшествует любому отчёту на stdout: отказ диска
            # отменяет весь запуск так же атомарно, как негодный набор.
            сохранённый_путь = save_report(
                общий, args.save, title=", ".join(отчёты)
            )
    except (ValueError, RegistryError) as ошибка:
        print(f"Стенд не запущен: {ошибка}")
        return 2
    except OSError:
        print(
            "Стенд не запущен: не удалось прочитать набор, baseline "
            "или записать отчёт."
        )
        return 2

    расхождения: list[str] = []
    for имя, отчёт in отчёты.items():
        print(отчёт.render(имя, show_failures=5))
        print()
        if args.check_notes:
            расхождения += check_notes(отчёт)

    if args.baseline:
        print("=== сравнение с прошлым прогоном ===")
        if not ходы:
            print("  места не изменились")
        for ход in ходы:
            print(ход.render())
        print()

    if расхождения:
        print("=== пометки разошлись с фактом ===")
        for строка in расхождения:
            print(f"  {строка}")
        print()

    if сохранённый_путь is not None:
        print(f"прогон записан: {сохранённый_путь}")

    # После успешной подготовки код 1 означает только расхождение пометок:
    # качество поиска — цифра для человека, а не порог для CI. Ошибки входа и
    # корпуса уже вернулись выше с кодом 2, не оставив частичного отчёта.
    return 1 if расхождения else 0


def _индекс_под_набор(context, suite: Suite) -> SearchIndex:
    """Индекс выбирается по явному domain, а не содержимому или имени файла."""
    if suite.domain == "syntax":
        if context.syntax is None:
            raise ValueError("набор спрашивает справку, а она не загружена")
        return context.syntax.index
    if suite.domain == "metadata":
        if context.configuration is None:
            raise ValueError(
                "набор спрашивает метаданные, а конфигурация не загружена"
            )
        return context.configuration.index
    raise ValueError(f"Неподдерживаемый domain набора: {suite.domain}.")


if __name__ == "__main__":
    raise SystemExit(main())
