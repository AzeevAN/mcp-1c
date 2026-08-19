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
        --auto --sets query-language --check-notes

Порогов в assert здесь нет намеренно: наборы запросов — не тесты. Проценты
ломались бы от каждой правки словаря, поэтому стенд печатает цифры, а решение
принимает человек. Ненулевой код возврата — только на расхождении пометок:
это не качество поиска, а враньё в файле.
"""

from __future__ import annotations

import json
import random
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

@dataclass(slots=True)
class Case:
    query: str
    expected: list[str]
    note: str = ""


@dataclass(slots=True)
class CaseResult:
    """Что получилось по одному запросу. Из этого считаются все метрики.

    Хранится поимённо, а не свёрнутым в счётчики: сравнение двух прогонов
    должно называть, **кто** сменил место, а не только «стало на три больше».
    Разовые скрипты для этого писались трижды за две сессии.
    """

    query: str
    expected: list[str]
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

    def to_dict(self) -> dict:
        # Выдача сохраняется только там, где она о чём-то говорит: у запроса,
        # взявшего первое место, она и так известна из `expected`. На
        # автоматических наборах это разница между 74 МБ и полутора: 61 тысяча
        # запросов по десять идентификаторов в каждом.
        got = [] if self.rank == 0 else self.got[:3]
        return {
            "query": self.query,
            "expected": list(self.expected),
            "got": got,
            "rank": self.rank,
            "separation": round(self.separation, 6),
            "foreign_first": self.foreign_first,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "CaseResult":
        return cls(
            query=raw["query"],
            expected=list(raw.get("expected") or []),
            got=list(raw.get("got") or []),
            rank=raw.get("rank"),
            separation=raw.get("separation", 0.0),
            foreign_first=raw.get("foreign_first", False),
            note=raw.get("note", ""),
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


def run(index: SearchIndex, cases: list[Case], limit: int = 10) -> Report:
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
                query=case.query,
                expected=list(case.expected),
                got=got,
                rank=rank,
                separation=separation,
                foreign_first=foreign,
                note=case.note,
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


def load_cases(path: str | Path) -> list[Case]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Case(
            query=item["query"],
            expected=list(item["expected"]),
            note=item.get("note", ""),
        )
        for item in payload
    ]


def load_curated(name: str) -> list[Case]:
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
        raise FileNotFoundError(f"Набор `{name}` не найден. Есть: {доступные}")
    return load_cases(path)


# ------------------------------------------------------- сохранение прогонов


def save_report(report: Report, path: str | Path, *, title: str = "") -> Path:
    """Прогон на диск, чтобы следующий было с чем сравнить.

    Дата в имя файла не зашивается: её кладёт вызывающий. Здесь — только
    содержимое, иначе тесты пришлось бы привязывать ко времени.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "title": title,
        "elapsed": report.elapsed,
        "results": [r.to_dict() for r in report.results],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def load_report(path: str | Path) -> Report:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Report(
        results=[CaseResult.from_dict(r) for r in payload.get("results") or []],
        elapsed=payload.get("elapsed", 0.0),
    )


@dataclass(slots=True)
class Move:
    """Один запрос, сменивший место между прогонами."""

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
        return f"  {знак} «{self.query}»: {место(self.before)} -> {место(self.after)}"


def compare(before: Report, after: Report) -> list[Move]:
    """Кто именно сменил место. Запросы сверяются по тексту, а не по порядку.

    Порядок в наборе меняется при любой правке файла, а привязка по индексу
    молча сравнивала бы разные запросы — и показывала бы движение там, где
    его нет.
    """
    старые = {r.query: r.rank for r in before.results}
    ходы = [
        Move(query=r.query, before=старые[r.query], after=r.rank)
        for r in after.results
        if r.query in старые and старые[r.query] != r.rank
    ]
    # Сначала ухудшения: регресс важнее прочитать, чем выигрыш.
    return sorted(ходы, key=lambda m: (m.better, m.query))


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
        if not note:
            continue
        место = "промах" if r.rank is None else str(r.rank + 1)
        if note.startswith(_ПОМЕТКА_ПРОМАХ) and r.rank == 0:
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


def _наборы(имена: list[str]) -> dict[str, list[Case]]:
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
        "--config", default=None, help="конфигурация для замера по метаданным"
    )
    parser.add_argument(
        "--sets",
        default="",
        help="ручные наборы через запятую, без расширения: query-language",
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

    наборы: dict[str, list[Case]] = {}
    if args.sets:
        наборы.update(_наборы([s.strip() for s in args.sets.split(",") if s.strip()]))
    if args.auto:
        if context.syntax is None:
            parser.error("--auto нечем мерить: справка не загружена")
        syntax = context.syntax.syntax
        наборы["точные имена справки"] = cases_from_syntax(syntax)
        наборы["одноимённые"] = cases_from_syntax_ambiguous(syntax)

    расхождения: list[str] = []
    отчёты: dict[str, Report] = {}
    for имя, cases in наборы.items():
        # Ручной набор по языку запросов ищется по справке, набор по
        # метаданным — по конфигурации. Индекс выбирается по тому, что в
        # наборе ожидают, а не по имени файла: имя врёт легко.
        index = _индекс_под_набор(context, cases)
        отчёт = run(index, cases, limit=args.limit)
        отчёты[имя] = отчёт
        print(отчёт.render(имя, show_failures=5))
        print()
        if args.check_notes:
            расхождения += check_notes(отчёт)

    if args.baseline:
        прежний = load_report(args.baseline)
        свежий = Report(results=[r for о in отчёты.values() for r in о.results])
        ходы = compare(прежний, свежий)
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

    if args.save:
        общий = Report(
            results=[r for о in отчёты.values() for r in о.results],
            elapsed=sum(о.elapsed for о in отчёты.values()),
        )
        путь = save_report(общий, args.save, title=", ".join(отчёты))
        print(f"прогон записан: {путь}")

    # Ненулевой код только на расхождении пометок: качество поиска — цифра для
    # человека, а не порог для CI. Пороги в процентах ломались бы от каждой
    # правки словаря, это записано в AGENTS.md.
    return 1 if расхождения else 0


def _индекс_под_набор(context, cases: list[Case]) -> SearchIndex:
    """Какой индекс отвечает на этот набор — справка или метаданные."""
    по_справке = any(
        doc_id.startswith(("query/", "objects/"))
        for case in cases
        for doc_id in case.expected
    )
    if по_справке:
        if context.syntax is None:
            raise SystemExit("набор спрашивает справку, а она не загружена")
        return context.syntax.index
    if context.configuration is None:
        raise SystemExit("набор спрашивает метаданные, а конфигурация не загружена")
    return context.configuration.index


if __name__ == "__main__":
    raise SystemExit(main())
