"""Кэш построенных поисковых индексов на диске.

Построение индексов — 72% старта: справка 1,2 с, реквизиты трёх конфигураций
0,76 с. Данные для этого уже лежат на диске разобранными, платим мы за
токенизацию и постройку постингов, а результат от запуска к запуску один и
тот же.

**Формат — `marshal`, и это осознанное расхождение с `store.py`.** Там gzip +
JSON, потому что разобранная справка — канонический артефакт: единственный
носитель данных, если `.hbk` под рукой нет. Здесь всё наоборот: постинги
производные, восстанавливаются за секунду из уже загруженного, и глазами их
не читают. Замерено на индексе справки:

    json.gz  1037 мс   json 958 мс   marshal 117 мс   построить заново 1266 мс

JSON проигрывает не из-за разбора, а из-за структуры: `doc_id` повторяется в
постингах миллион раз, и JSON пишет каждое вхождение отдельной строкой — 106
МБ текста против 3,4 МБ. `marshal` версии 4 хранит повторы ссылками и на
подъёме переиспользует один объект.

От `pickle`, отвергнутого в `store.py`, отличается тем, что не исполняет код:
собирает только числа, строки, списки и словари.

**Кэш — расходник.** Не сошёлся штамп, побился файл, обновился Python — молча
строим как раньше. Неверный ответ получить нельзя: либо годный кэш, либо
пересборка.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import marshal
import sys
from pathlib import Path
from typing import Iterable

from .search import SearchIndex

CACHE_VERSION = 1

# Уровень 1, а не 6: файл вчетверо меньше несжатого (4,0 МБ против 21,3), а
# запись втрое дешевле, чем на шестом (115 мс против 431). Подъём при этом
# отличается на 19 мс — полтора процента старта.
COMPRESS_LEVEL = 1

_MARSHAL_VERSION = 4


class _Miss(Exception):
    """Кэш не годится. Наружу не выходит — превращается в None."""


def _code_digest(_cache: list[str] = []) -> str:
    """Отпечаток кода, от которого зависит содержимое индексов.

    Берётся весь пакет, а не список «важных» модулей: такой список сгниёт —
    кто-нибудь тронет логику в четвёртом файле и забудет его дописать, а кэш
    молча разойдётся с кодом. Цена грубости — лишняя пересборка после любой
    правки исходников, один раз, полторы секунды. В контейнере код не меняется
    вовсе.
    """
    if _cache:
        return _cache[0]
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.read_bytes())
    _cache.append(digest.hexdigest())
    return _cache[0]


def safe_name(value: str) -> str:
    """Имя из идентификатора источника или конфигурации, годное для пути.

    Имя приходит из манифеста выгрузки, там встречается и косая черта, и
    двоеточие; в имя файла или каталога они не годятся.

    Правило одно на весь проект: тем же именем называется каталог кода
    `data/modules/<Имя>/` (`Registry._modules_root`). Второй способ чистки
    означал бы, что кэш и каталог кода расходятся на первом же необычном имени.

    Точка правилом сохраняется — она нужна в именах вида `Розница 2.3`, — и
    поэтому «..» проходит через чистку неизменным. Проверять, что собранный
    путь лежит там, где задумано, обязан вызывающий: чистка от выхода наружу
    не защищает.
    """
    return "".join(ch if ch.isalnum() or ch in "-_. " else "_" for ch in value)


def path_for(cache_dir: str | Path, source_id: str, kind: str) -> Path:
    """Путь к файлу кэша — детерминированный, без дат и хешей в имени.

    Пересборка обязана перезаписывать тот же файл, а не класть рядом новый:
    иначе каталог растёт молча, и заметят это нескоро — кэш никто не открывает.
    """
    return Path(cache_dir) / f"{safe_name(source_id)}.{kind}"


def _stamp(source_sha256: str, kind: str) -> dict:
    return {
        "cache_version": CACHE_VERSION,
        "kind": kind,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "marshal": _MARSHAL_VERSION,
        "code": _code_digest(),
        "source": source_sha256,
    }


def save_blob(payload, path: str | Path, *, source_sha256: str, kind: str) -> Path | None:
    """Записать что угодно из примитивов. Атомарно: сначала `.tmp`, потом замена.

    Без атомарности прерванная запись оставляет обрезанный файл на месте
    годного, и следующий старт поднимет полуиндекс.

    `None` — записать не удалось. Это не ошибка: кэш расходный.
    """
    target = Path(path)
    header = json.dumps(_stamp(source_sha256, kind), ensure_ascii=False).encode("utf-8")
    blob = gzip.compress(marshal.dumps(payload, _MARSHAL_VERSION), COMPRESS_LEVEL)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(header + b"\n" + blob)
        tmp.replace(target)
    except OSError:
        # Том смонтирован только на чтение, кончилось место, каталог занят —
        # ронять из-за кэша нечего: следующий старт просто построит заново.
        return None
    return target


def load_blob(path: str | Path, *, source_sha256: str, kind: str):
    """Поднять записанное. `None` — кэш не годится, надо строить заново."""
    try:
        return _read_verified(Path(path), source_sha256, kind)
    except _Miss:
        return None


def save(index: SearchIndex, path: str | Path, *, source_sha256: str, kind: str) -> Path | None:
    return save_blob(index.export_state(), path, source_sha256=source_sha256, kind=kind)


def _read_verified(path: Path, source_sha256: str, kind: str) -> dict:
    """Прочитать файл, сверив штамп до разбора содержимого.

    Порядок принципиален: `marshal` на испорченных данных способен уронить
    интерпретатор, и перехватить это уже нельзя. Поэтому чужой блоб до него
    не доходит — сначала заголовок обычным JSON, и только потом всё остальное.
    """
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise _Miss(str(error)) from error

    head, sep, blob = raw.partition(b"\n")
    if not sep:
        raise _Miss("нет заголовка")

    try:
        stamp = json.loads(head.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _Miss(f"заголовок не читается: {error}") from error

    if stamp != _stamp(source_sha256, kind):
        raise _Miss("штамп не совпал")

    try:
        return marshal.loads(gzip.decompress(blob))
    except Exception as error:  # обрезанный или подменённый блоб
        raise _Miss(f"содержимое не читается: {error}") from error


def load(
    path: str | Path,
    payloads: dict,
    *,
    source_sha256: str,
    kind: str,
    synonyms: dict | None = None,
    aliases: dict | None = None,
) -> SearchIndex | None:
    """Поднять индекс из кэша. `None` — кэш не годится, надо строить."""
    state = load_blob(path, source_sha256=source_sha256, kind=kind)
    if state is None:
        return None
    try:
        return SearchIndex.from_state(state, payloads, synonyms=synonyms, aliases=aliases)
    except (KeyError, TypeError, ValueError):
        # Файл прочитался, но в нём не то, что мы кладём. Тоже промах.
        return None


def sweep(cache_dir: str | Path, keep: Iterable[str]) -> list[str]:
    """Убрать чужое: файлы исчезнувших источников и обрывки записи.

    Без уборки каталог растёт при каждом переименовании или удалении
    конфигурации. Сносить безопасно: кэш восстанавливается сам.
    """
    directory = Path(cache_dir)
    if not directory.is_dir():
        return []

    allowed = set(keep)
    removed: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name in allowed:
            continue
        try:
            path.unlink()
        except OSError:
            # Каталог только на чтение. Лишний файл там полежит, старт важнее.
            continue
        removed.append(path.name)
    return removed
