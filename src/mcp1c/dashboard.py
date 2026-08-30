"""Дашборд: реестр, загрузка источников, прогон запросов.

Отдаёт список маршрутов, а не приложение: `server.py` монтирует их рядом с
`/health`, тесты вешают на голый `Starlette`. Про MCP модуль не знает — тот же
приём, что уже применён к `tools.py`.

Разметка собирается строками. Шага сборки нет намеренно: четыре экрана не
стоят npm в образе, а дашборд обязан работать и с выключенным JS.

Спецификация — docs/dashboard-design.md.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import tempfile
import traceback
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from . import tools
from .auth import same_token
from .dictionary import SOURCE_BUILTIN as DICT_BUILTIN
from .graph_view import DEFAULT_LIMIT as DEFAULT_GRAPH_LIMIT
from .graph_view import Neighbourhood, bounds, neighbourhood
from .loader import ExportError
from .registry import (
    KIND_EXTENSION,
    KIND_EXTENSION_RUNTIME,
    KIND_MODULES,
    Registry,
    RegistryError,
)
from .reference_provider import (
    MAX_REFERENCE_DB_BYTES,
    ReferenceService,
    ReferenceValidationError,
)
from .render import DETAIL_LEVELS
from .search import FIELD_KIND_TITLES, MAX_QUERY_CHARS
from .syntax_model import KIND_TITLES
from .v8container import V8ContainerError

PAGE_TITLE = "Структура конфигураций 1С"
COOKIE = "mcp1c_session"

# Справка весит 60-70 МБ, крупная выгрузка — единицы. Запас десятикратный; без
# потолка первый же случайный файл на гигабайты положит контейнер по памяти.
MAX_UPLOAD = 500 * 1024 * 1024
CHUNK = 1024 * 1024
MAX_QUERY_PHRASES = 32
MAX_UPLOAD_FIELDS = 1
MAX_UPLOAD_FIELD_SIZE = 4 * 1024

# Сессии живут в памяти процесса и умирают с перезапуском. Отдельного хранилища
# не заводим: вход — это одна вставка токена.
# Сессия -> уровень доступа. Уровня два: читатель видит страницы, но не формы
# правки; администратор может всё. Разделены потому, что токен чтения лежит в
# конфиге каждого MCP-клиента и утекает вместе с ним.
LEVEL_READ = "read"
LEVEL_ADMIN = "admin"
_SESSIONS: dict[str, str] = {}

_STYLE = """
body { font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 60rem;
       padding: 1rem 1.5rem 4rem; color: #1b1b1b; }
nav { margin-bottom: 1.5rem; }
nav a { margin-right: 1rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #ddd;
         vertical-align: top; }
.note { background: #fff8e1; padding: .5rem .8rem; margin: .3rem 0; }
.error { background: #ffebee; padding: .5rem .8rem; margin: .5rem 0; }
.upload { margin: .8rem 0; }
.upload progress { width: 100%; height: 1.1rem; display: block; margin: .3rem 0; }
.upload span { font-size: 13px; color: #555; }
.warn { background: #fff8e1; color: #7a5200; }
.card { background: #fafafa; padding: 1rem; border: 1px solid #ddd;
        white-space: pre-wrap; word-break: break-word; font-size: 13px; }
/* Блоки кода внутри разобранной карточки. `pre.card` — это режим «как есть»,
   у него своё оформление, поэтому исключён. */
pre:not(.card) { background: #f1f1f1; padding: .6rem .8rem; margin: .6rem 0;
                 overflow-x: auto; white-space: pre; }
pre:not(.card) code { font: 13px/1.45 ui-monospace, Menlo, Consolas, monospace; }
.graph { border: 1px solid #ddd; background: #fcfcfc; width: 100%;
         height: 34rem; cursor: grab; touch-action: none; }
.graph:active { cursor: grabbing; }
.graph text { font: 11px system-ui, sans-serif; fill: #333; }
.graph .edge { stroke: #b9b9b9; stroke-width: 1.2; }
.graph .edge.in { stroke-dasharray: 4 3; }
.graph a:hover circle { stroke: #1b1b1b; stroke-width: 2.5; }
.graph .subject { stroke: #1b1b1b; stroke-width: 2.5; }
.legend span { display: inline-block; margin-right: .9rem; font-size: 13px; }
.legend i { display: inline-block; width: .7rem; height: .7rem;
            border-radius: 50%; margin-right: .25rem; vertical-align: middle; }
"""

# Цвет по виду объекта. Не украшение: на картинке из шестидесяти узлов вид —
# первое, что нужно различать («документ окружён регистрами»), а читать
# шестьдесят подписей глазами дороже, чем увидеть цвет.
KIND_COLORS = {
    "Справочник": "#4c8ed9",
    "Документ": "#e0803c",
    "РегистрСведений": "#5aa469",
    "РегистрНакопления": "#3f8f6b",
    "РегистрБухгалтерии": "#2f7d5e",
    "РегистрРасчета": "#7fae55",
    "ПланСчетов": "#9b6bbf",
    "ПланВидовХарактеристик": "#8a72c4",
    "ПланВидовРасчета": "#a37fd0",
    "ПланОбмена": "#b07f9e",
    "Перечисление": "#c9a227",
    "Константа": "#9a9a9a",
    "ОбщийМодуль": "#7d7d7d",
    "Обработка": "#a8a8a8",
    "Отчет": "#a8a8a8",
    "БизнесПроцесс": "#c07b7b",
    "Задача": "#c07b7b",
}
KIND_FALLBACK = "#8f8f8f"


def _admin_token() -> str:
    """Читается на каждый запрос, а не при импорте: так его подменяют тесты."""
    return os.environ.get("ADMIN_TOKEN", "")


def _api_token() -> str:
    """Токен чтения. Пуст — читать может кто угодно, как было всегда."""
    return os.environ.get("API_TOKEN", "")


def _token_from_headers(request: Request) -> str:
    """Токен из заголовка: `X-Api-Token` или `Authorization: Bearer`.

    Заголовки HTTP кодируются latin-1, поэтому кириллический токен сюда
    физически не доходит — через форму входа он работает, через заголовок нет.
    """
    direct = request.headers.get("x-api-token", "")
    if direct:
        return direct
    auth = request.headers.get("authorization", "")
    prefix = "bearer "
    return auth[len(prefix):] if auth.lower().startswith(prefix) else ""


def _session_level(request: Request) -> str | None:
    """Уровень серверной браузерной сессии без раскрытия самой cookie."""
    session = request.cookies.get(COOKIE, "")
    return _SESSIONS.get(session) if session else None


def _authorized(request: Request) -> bool:
    """Право записи: загрузка источников, удаление, правка словаря."""
    if _session_level(request) == LEVEL_ADMIN:
        return True
    return same_token(_token_from_headers(request), _admin_token())


def _origin_key(value: str, *, allow_path: bool) -> tuple[str, str, int] | None:
    """Канонический origin без зависимости от написания стандартного порта."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        scheme not in ("http", "https")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and (parsed.path not in ("", "/") or parsed.query))
        or parsed.fragment
    ):
        return None
    return scheme, hostname, port or (443 if scheme == "https" else 80)


def _cookie_mutation_is_same_origin(request: Request) -> bool:
    """Не дать браузерной cookie авторизовать запись с соседнего host.

    Явный админский заголовок не является фоновым полномочием браузера и потому
    не требует CSRF-проверки, даже если клиент заодно прислал cookie.
    """
    session = request.cookies.get(COOKIE, "")
    if not session or session not in _SESSIONS:
        return True
    if same_token(_token_from_headers(request), _admin_token()):
        return True

    origin = request.headers.get("origin", "")
    source = origin or request.headers.get("referer", "")
    if not source:
        return False
    expected = _origin_key(str(request.base_url), allow_path=True)
    actual = _origin_key(source, allow_path=not bool(origin))
    return expected is not None and actual == expected


def _csrf_denied(request: Request) -> PlainTextResponse | None:
    if _cookie_mutation_is_same_origin(request):
        return None
    return PlainTextResponse(
        "Cookie-сессия не подтверждена заголовком Origin или Referer этого сервера.",
        status_code=403,
    )


class _UploadTooLarge(MultiPartException):
    """File-part пересёк границу до следующей записи в spool."""


class _LimitedUploadParser(MultiPartParser):
    """Multipart с одним файлом и ограниченным временным spool-файлом."""

    def __init__(self, request: Request, file_limit: int) -> None:
        super().__init__(
            request.headers,
            request.stream(),
            max_files=1,
            max_fields=MAX_UPLOAD_FIELDS,
            max_part_size=MAX_UPLOAD_FIELD_SIZE,
        )
        self.file_limit = file_limit
        self._current_file_size = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_size += end - start
            if self._current_file_size > self.file_limit:
                raise _UploadTooLarge(
                    f"File-part превысил предел {self.file_limit} байт."
                )
        super().on_part_data(data, start, end)


async def _limited_upload_form(
    request: Request,
    file_limit: int | None = None,
    *,
    allowed_fields: frozenset[str] = frozenset({"allow_truncated"}),
) -> FormData:
    """Разобрать загрузку, не принимая лишние файлы и поля."""
    if not request.headers.get("content-type", "").lower().startswith(
        "multipart/form-data"
    ):
        raise MultiPartException("Ожидалась multipart/form-data.")
    parser = _LimitedUploadParser(
        request,
        MAX_UPLOAD if file_limit is None else file_limit,
    )
    form = await parser.parse()
    items = form.multi_items()
    files = [(name, value) for name, value in items if isinstance(value, UploadFile)]
    fields = [(name, value) for name, value in items if not isinstance(value, UploadFile)]
    if len(files) != 1 or files[0][0] != "file":
        await form.close()
        raise MultiPartException("Ожидался ровно один file-part с именем file.")
    if any(name not in allowed_fields for name, _ in fields):
        await form.close()
        raise MultiPartException("Получено неизвестное поле multipart.")
    return form


def can_read(request: Request) -> bool:
    """Право чтения: страницы дашборда и инструменты MCP.

    Админский токен годится и здесь — иначе владельцу пришлось бы держать в
    клиенте два заголовка вместо одного.
    """
    expected = _api_token()
    if not expected:
        return True
    if _session_level(request) is not None:
        return True
    given = _token_from_headers(request)
    return same_token(given, expected) or same_token(given, _admin_token())


# Что сейчас грузится. Живёт в памяти процесса и умирает с перезапуском —
# как и сессии: это состояние минуты, а не факт о данных. Список короткий,
# показывается только вошедшему: имя файла тоже говорит, что за база.
JOB_READING = "принимается"
JOB_PARSING = "разбирается"
JOB_DONE = "готово"
JOB_FAILED = "ошибка"
_JOBS: list[dict] = []
# Ссылки на фоновые задачи. `asyncio` держит только слабую ссылку, и задача
# без сильной может быть собрана сборщиком мусора в любой момент до
# завершения — с виду это «разбор молча не случился». Множество живёт ровно
# столько, сколько сама задача: `add_done_callback` убирает запись.
_ФОНОВЫЕ: set[asyncio.Task] = set()
# Сколько завершённых заданий держать на экране. Нужны, чтобы человек увидел
# результат, вернувшись через минуту; копить их незачем.
JOBS_KEPT = 10


def _объём(байт: int) -> str:
    """Размер человеку: мелкий файл в мегабайтах — это «0.0 МБ», то есть ничего."""
    if байт < 1024 * 1024:
        return f"{байт / 1024:.0f} КБ"
    return f"{байт / 1024 / 1024:.1f} МБ"


def _start_job(name: str, size: int) -> dict:
    job = {"name": name, "size": size, "state": JOB_READING, "error": "", "result": ""}
    _JOBS.append(job)
    завершённые = [j for j in _JOBS if j["state"] in (JOB_DONE, JOB_FAILED)]
    for лишнее in завершённые[:-JOBS_KEPT]:
        _JOBS.remove(лишнее)
    return job


def _index_source(
    registry: Registry, path: Path, suffix: str, *, allow_truncated: bool = False
) -> None:
    """Выполняется в отдельном потоке: разбор .hbk занимает около 4 секунд."""
    if suffix == ".hbk":
        registry.add_syntax(path)
    elif suffix == ".json":
        registry.add_extension_runtime(path)
    else:
        registry.add_configuration(path, allow_truncated=allow_truncated)
    registry.save()


def _run_job(
    registry: Registry,
    job: dict,
    directory: str,
    path: Path,
    suffix: str,
    *,
    allow_truncated: bool = False,
) -> None:
    """Разбор в фоне. Ошибку кладём в задание: ответа, куда её вернуть, уже нет."""
    job["state"] = JOB_PARSING
    try:
        _index_source(
            registry, path, suffix, allow_truncated=allow_truncated
        )
    except (ExportError, RegistryError, V8ContainerError, ValueError) as error:
        job["state"] = JOB_FAILED
        job["error"] = str(error)
    except Exception as error:  # фоновая задача не должна ронять процесс молча
        job["state"] = JOB_FAILED
        job["error"] = f"{type(error).__name__}: {error}"
        # На странице остаётся одна строка — больше туда не влезет. Но без
        # стека неожиданную ошибку разбирать не по чему: имя класса и текст
        # не говорят, где именно упало. Один раз это уже стоило отдельного
        # расследования, поэтому стек уходит в лог контейнера.
        traceback.print_exc()
    else:
        job["state"] = JOB_DONE
    finally:
        # `directory` — всегда наш временный каталог из `upload`: другой
        # вызывающей стороны у `_run_job` нет. Разбор выгрузки из `incoming/`
        # идёт отдельной функцией `_run_incoming`, и она не удаляет ничего
        # вовсе — исходник принадлежит человеку, а каталог `incoming/` сервер
        # трогать не вправе.
        shutil.rmtree(directory, ignore_errors=True)


_СКАНЕРЫ: dict[str, "IncomingScanner"] = {}


def _scanner(registry: Registry) -> "IncomingScanner":
    """Сканер один на каталог данных: множество `running` живёт в памяти, и
    страница обязана видеть то же самое, что обработчик разбора."""
    from .incoming import IncomingScanner

    ключ = str(registry.data_dir)
    if ключ not in _СКАНЕРЫ:
        _СКАНЕРЫ[ключ] = IncomingScanner(registry)
    return _СКАНЕРЫ[ключ]


def _configuration_for(registry: Registry, архив: Path) -> str:
    """Определение конфигурации — по единственной загруженной, иначе отказ с
    объяснением (привязка по манифесту — работа провайдера, разведка раздел 5)."""
    имена = registry.snapshot().configuration_names
    if len(имена) == 1:
        return имена[0]
    if not имена:
        # Причина здесь другая, чем при нескольких: привязывать не к чему.
        # Код ложится в каталог по имени конфигурации и учитывается ключом
        # `<Имя>:modules` — без метаданных этого имени взять неоткуда.
        raise RegistryError(
            f"{архив.name}: не загружено ни одной конфигурации — сначала "
            "загрузите выгрузку структуры (СтруктураКонфигурации_*.zip), "
            "к ней и привязывается код."
        )
    raise RegistryError(
        f"{архив.name}: загружено {len(имена)} конфигураций — выберите "
        "нужную в форме рядом с кнопкой."
    )


def _run_incoming(
    registry: Registry,
    сканер,
    job: dict,
    архив: Path,
    конфигурация: str | None = None,
) -> None:
    """Разбор выгрузки из `incoming/`. Исходник остаётся на месте.

    `конфигурация` — уже проверенный обработчиком выбор человека (форма,
    поле `configuration`). Пустая строка или `None` — поле не прислали или
    человек оставил его пустым, тогда решает `_configuration_for` сама, как
    и раньше.
    """
    job["state"] = JOB_PARSING
    try:
        имя_конфигурации = конфигурация or _configuration_for(registry, архив)
        registry.add_modules(архив, configuration=имя_конфигурации)
    except (ExportError, RegistryError, V8ContainerError, ValueError) as error:
        # Известная ошибка проекта — это сообщение человеку, и имя класса ему
        # ничего не добавляет: «загружено 2 конфигураций» он поймёт, а
        # «RegistryError:» перед этим — нет. `_run_job` делит ошибки так же.
        job["state"] = JOB_FAILED
        job["error"] = str(error)
        сканер.note_failure(архив, job["error"])
    except Exception as error:
        job["state"] = JOB_FAILED
        job["error"] = f"{type(error).__name__}: {error}"
        сканер.note_failure(архив, job["error"])
        traceback.print_exc()
    else:
        job["state"] = JOB_DONE
        сканер.clear_failure(архив)
    finally:
        сканер.finish(архив.name)


def _layout(title: str, body: str, *, refresh: int = 0) -> HTMLResponse:
    обновление = f"<meta http-equiv=refresh content={refresh}>" if refresh else ""
    return HTMLResponse(
        f"<!doctype html><html lang=ru><meta charset=utf-8>{обновление}"
        f"<title>{escape(title)}</title><style>{_STYLE}</style>"
        f"<nav><a href=/>Обзор</a><a href=/sources>Источники</a>"
        f"<a href=/queries>Запросы</a><a href=/graph>Связи</a>"
        f"<a href=/dictionary>Словарь</a></nav>{body}"
    )


def _overview_page(registry: Registry) -> HTMLResponse:
    rows = registry.overview()
    if not rows:
        return _layout(PAGE_TITLE, "<p>Не загружено ни одной конфигурации.</p>")

    parts = [
        "<table><tr><th>Конфигурация<th>Версия<th>Платформа<th>Объектов"
        "<th>Связей</tr>"
    ]
    notes: list[str] = []
    for row in rows:
        parts.append(
            f"<tr><td>{escape(row['name'])}<td>{escape(row['version'])}"
            f"<td>{escape(row['platform'])}<td>{row['objects']}<td>{row['edges']}</tr>"
        )
        for note in row["notes"]:
            notes.append(f"<div class=note>{escape(row['name'])}: {escape(note)}</div>")
    parts.append("</table>")
    parts.extend(notes)
    parts.extend(_coverage_block(registry))
    return _layout(PAGE_TITLE, "".join(parts))


def _coverage_block(registry: Registry) -> list[str]:
    """Каких справок не хватает под загруженные конфигурации.

    Здесь это видно раньше всего: человек смотрит обзор, чтобы понять,
    отвечает сервер по его платформе или по соседней.
    """
    покрытие = registry.syntax_coverage()
    if not покрытие["loaded"] and not покрытие["missing"]:
        return []

    parts = ["<h2>Справки платформы</h2>"]
    if покрытие["loaded"]:
        parts.append(f"<p>Загружены: {escape(', '.join(покрытие['loaded']))}.</p>")
    else:
        parts.append("<p>Не загружено ни одной справки.</p>")

    for пропуск in покрытие["missing"]:
        конфигурации = escape(", ".join(пропуск["configurations"]))
        parts.append(
            f"<div class=note>Не хватает справки {escape(пропуск['platform'])} — "
            f"на ней работает {конфигурации}. Ответы собраны из соседних версий: "
            "наличие элементов отфильтровано, сигнатуры и доступность могут "
            "отличаться.</div>"
        )
    for лишняя in покрытие["unused"]:
        parts.append(
            f"<div class=note>Справка {escape(лишняя)} не используется: "
            "конфигураций на этой платформе нет.</div>"
        )
    return parts


SCOPES = {
    "objects": "объектам",
    "fields": "реквизитам",
    "syntax": "справке платформы",
}


def _run_queries(
    registry: Registry, config: str | None, scope: str, phrases: list[str]
) -> list[tuple[str, list, list]]:
    """Прогон фраз по выбранному индексу. Пять попаданий на фразу.

    Возвращает ещё и то, что отсеял фильтр версии. Молча отбрасывать нельзя:
    человек прочтёт «ничего не найдено» там, где верный вывод — «метод есть,
    но не в этой версии платформы».
    """
    context = registry.resolve(config, require_configuration=(scope != "syntax"))

    if scope == "syntax":
        if context.syntax is None:
            raise RegistryError("Справка платформы не подключена.")
        index = context.syntax.index
        # Фильтр по версии обязателен: без него дашборд покажет методы,
        # которых в этой конфигурации нет. Смысл фильтрации в том, что
        # предупреждение человек пропустит, а отсутствие в выдаче — нет.
        keep = context.syntax_filter()
    else:
        index = (
            context.configuration.index
            if scope == "objects"
            else context.configuration.field_index
        )
        keep = None

    result: list[tuple[str, list, list]] = []
    for phrase in phrases:
        hits = index.search(phrase, limit=20 if keep else 5)
        hidden: list = []
        if keep is not None:
            hidden = [hit for hit in hits if not keep(hit.doc.payload)]
            hits = [hit for hit in hits if keep(hit.doc.payload)]
        result.append((phrase, hits[:5], hidden[:5]))
    return result


_RE_CODE = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text: str) -> str:
    """Внутристрочная разметка. Экранирование — до неё, а не после.

    Порядок важен: сначала гасим угловые скобки, потом ставим свои теги.
    Наоборот — и `<code>` из нашей же разметки уехал бы в `&lt;code&gt;`.
    """
    готово = escape(text)
    готово = _RE_CODE.sub(r"<code>\1</code>", готово)
    return _RE_BOLD.sub(r"<b>\1</b>", готово)


def _блок_кода(строки: list[str], начало: int) -> tuple[str, int]:
    """Ограждённый блок в `<pre><code>` и номер первой строки после него.

    Язык после тройки отбрасывается: подсветки на странице нет. Незакрытый
    блок доходит до конца текста — остаток страницы при этом виден, просто
    внутри `<pre>`, а не абзацами.
    """
    конец = начало + 1
    while конец < len(строки) and not строки[конец].strip().startswith("```"):
        конец += 1
    тело = "\n".join(строки[начало + 1 : конец])
    return f"<pre><code>{escape(тело)}</code></pre>", конец + 1


def _ячейки_строки(строка: str) -> list[str] | None:
    """Ячейки markdown-строки таблицы. `None`, если это не строка таблицы."""
    голая = строка.strip()
    if not голая.startswith("|") or not голая.endswith("|"):
        return None
    # Экранированная черта — значение, а не граница ячейки.
    return [
        ячейка.strip().replace("\x00", "|")
        for ячейка in голая[1:-1].replace("\\|", "\x00").split("|")
    ]


def _таблица_markdown(строки: list[str], начало: int) -> tuple[str, int] | None:
    """Таблица, начинающаяся на этой строке. `None`, если её здесь нет.

    Возвращает готовый HTML и номер первой строки после таблицы.
    """
    шапка = _ячейки_строки(строки[начало])
    if шапка is None or начало + 1 >= len(строки):
        return None
    разделитель = _ячейки_строки(строки[начало + 1])
    if разделитель is None or not разделитель:
        return None
    if not all(set(ячейка) <= set("-: ") and "-" in ячейка for ячейка in разделитель):
        return None

    части = ["<tr>" + "".join(f"<th>{_inline(я)}</th>" for я in шапка) + "</tr>"]
    номер = начало + 2
    while номер < len(строки):
        ячейки = _ячейки_строки(строки[номер])
        if ячейки is None:
            break
        части.append("<tr>" + "".join(f"<td>{_inline(я)}</td>" for я in ячейки) + "</tr>")
        номер += 1
    return "<table>" + "".join(части) + "</table>", номер


def render_markdown(text: str) -> str:
    """Markdown из `render.py` в HTML — ровно то, что он порождает.

    Вложенных списков в его выводе нет, поэтому и здесь их нет: разбирать то,
    чего не бывает, значит держать код, который никто не проверяет. Замер
    вывода: 308 пунктов списка, 296 фрагментов кода, 34 заголовка, 6 жирных,
    2 цитаты.

    Ограждённые блоки (```` ```bsl ````) печатались абзацами с самого начала:
    сигнатуры и примеры теряли моноширинный вид и отступы, а карточка справки
    из них и состоит. Внутри блока разметка не разбирается — решётка и дефис
    там часть кода. Незакрытый блок доводится до конца текста, но остаток
    страницы не теряется: он остаётся видимым внутри `<pre>`.

    Таблицы появились вместе с разделением показа и поиска в справке языка
    запросов: страница отдаёт их отдельным полем, и карточка печатает их
    markdown-таблицей. Признаём таблицей только пару «строка с чертами плюс
    строка-разделитель»: одиночная черта в строке — обычный знак, в лесенке
    грамматики она стоит в каждой второй строке.
    """
    строки = text.splitlines()
    куски: list[str] = []
    список: list[str] = []
    цитата: list[str] = []
    номер = 0

    def закрыть_список() -> None:
        if список:
            куски.append("<ul>" + "".join(f"<li>{i}</li>" for i in список) + "</ul>")
            список.clear()

    def закрыть_цитату() -> None:
        if цитата:
            куски.append("<blockquote>" + "<br>".join(цитата) + "</blockquote>")
            цитата.clear()

    while номер < len(строки):
        строка = строки[номер]
        номер += 1
        голая = строка.rstrip()

        if голая.startswith("```"):
            закрыть_список()
            закрыть_цитату()
            разметка, номер = _блок_кода(строки, номер - 1)
            куски.append(разметка)
            continue

        таблица = _таблица_markdown(строки, номер - 1)
        if таблица is not None:
            разметка, следующая = таблица
            закрыть_список()
            куски.append(разметка)
            номер = следующая
            continue

        if голая.startswith("> ") or голая == ">":
            # Пустая строка внутри цитаты пишется одним знаком `>` без пробела.
            # Без этой ветки цитата разваливалась на куски с абзацами `&gt;`
            # между ними — видно на оговорке про неограниченные строки.
            закрыть_список()
            цитата.append(_inline(голая[2:].lstrip("- ")))
            continue
        закрыть_цитату()

        if голая.strip() == "---":
            закрыть_список()
            куски.append("<hr>")
        elif голая.startswith("#"):
            закрыть_список()
            уровень = len(голая) - len(голая.lstrip("#"))
            куски.append(f"<h{уровень}>{_inline(голая[уровень:].strip())}</h{уровень}>")
        elif голая.startswith("- ") or голая.startswith("* "):
            список.append(_inline(голая[2:]))
        elif голая.strip():
            закрыть_список()
            куски.append(f"<p>{_inline(голая)}</p>")
        else:
            закрыть_список()

    закрыть_список()
    закрыть_цитату()
    return "".join(куски)


def _card_link(scope: str, config: str, hit) -> str:
    """Куда ведёт имя в таблице результатов.

    У реквизита своей карточки нет — ведём на объект-владелец. У элемента
    справки идентификатор в индексе служебный (`objects/catalog63/…`), а
    `get_syntax` принимает имя, поэтому берём `full_ru`.
    """
    if scope == "syntax":
        # Точный адрес сохраняет владельца и не смешивает одноимённые элементы.
        name = getattr(hit.doc.payload, "address", "") or hit.doc.id
        page = "/syntax"
    elif scope == "fields":
        name = getattr(hit.doc.payload, "object_full_name", "") or hit.doc.id
        page = "/object"
    else:
        name = hit.doc.id
        page = "/object"
    return f"{page}?config={quote(config)}&name={quote(name)}"


# ------------------------------------------------------------- граф связей

# Перетаскивание и зум. Сорок строк без библиотек: раскладку уже посчитал
# сервер, клиенту остаётся двигать `viewBox`. Ради этого тянуть d3 (250 КБ) или
# сборку React значило бы завести второй язык и шаг сборки в проекте, где
# дашборд — 1 155 строк на голом HTML без единого внешнего файла.
_UPLOAD_JS = """
/* Показ передачи файла.
 *
 * Сервер хода передачи не видит: `await request.form()` возвращает управление
 * только когда тело пришло целиком, и задание заводится уже после этого. На
 * localhost это незаметно, на удалённом сервере человек жмёт «Загрузить» и
 * получает пустой экран на всё время заливки.
 *
 * Отправлено байт знает только браузер — `xhr.upload.onprogress`. Форма
 * остаётся обычной: без JS submit уходит как раньше, просто без индикатора.
 */
(function () {
  var форма = document.getElementById("upload-form");
  if (!форма || !window.XMLHttpRequest || !window.FormData) return;

  var поле = форма.querySelector("input[type=file]");
  var кнопка = форма.querySelector("button");
  var показ = document.getElementById("upload-progress");
  var полоса = document.getElementById("upload-bar");
  var строка = document.getElementById("upload-text");
  var обычныйПредел = parseInt(форма.getAttribute("data-limit"), 10);
  var справочныйПредел = parseInt(форма.getAttribute("data-reference-limit"), 10);
  var справкаУправляется = форма.getAttribute("data-reference-managed") === "1";

  /* Мелкий файл в мегабайтах — это «0,0 из 0,0 МБ»: единица не подходит. */
  function объём(байт) {
    if (байт < 1048576) return Math.round(байт / 1024) + " КБ";
    return (байт / 1048576).toFixed(1).replace(".", ",") + " МБ";
  }

  function сказать(текст) { строка.textContent = текст; показ.hidden = false; }

  function отказ(текст) {
    сказать(текст);
    полоса.hidden = true;
    поле.disabled = false;
    кнопка.disabled = false;
  }

  форма.addEventListener("submit", function (событие) {
    var файл = поле.files && поле.files[0];
    if (!файл) return;                     /* пустое поле — пусть скажет браузер */
    var справочнаяБаза = файл.name.toLowerCase().endsWith(".sqlite3");
    var предел = справочнаяБаза ? справочныйПредел : обычныйПредел;

    if (справочнаяБаза && !справкаУправляется) {
      событие.preventDefault();
      отказ("SQLite подключена через внешний путь; загрузкой управляет оператор.");
      return;
    }

    /* Предел проверяется и на сервере, но там — после приёма: к моменту
     * отказа трафик уже потрачен. Здесь размер известен до отправки. */
    if (предел && файл.size > предел) {
      событие.preventDefault();
      отказ("Файл " + объём(файл.size) + ", предел " + объём(предел) + ".");
      return;
    }

    событие.preventDefault();

    /* Данные собираются ДО выключения поля: выключенные элементы формы в
     * FormData не попадают, и на сервер уходит пустой запрос. Проверено в
     * браузере 2026-08-19: файл молча терялся, сервер отвечал «файл не
     * выбран», задание не заводилось вовсе. */
    var данные = new FormData(форма);
    if (справочнаяБаза) данные.delete("allow_truncated");

    поле.disabled = true;
    кнопка.disabled = true;
    полоса.hidden = false;
    полоса.value = 0;
    сказать("Передача началась…");

    var начало = Date.now();
    var xhr = new XMLHttpRequest();
    xhr.open(
      "POST",
      справочнаяБаза ? "/api/v1/reference/upload" : форма.getAttribute("action")
    );

    xhr.upload.onprogress = function (событие) {
      if (!событие.lengthComputable) {
        сказать("Передано " + объём(событие.loaded) + "…");
        return;
      }
      var доля = событие.loaded / событие.total;
      полоса.value = Math.round(доля * 100);
      var строки = [
        Math.round(доля * 100) + "% — " + объём(событие.loaded) +
        " из " + объём(событие.total)
      ];
      /* Скорость и остаток врут на первых долях секунды и на мелких файлах:
       * показываем, только когда цифра уже что-то значит. */
      var секунд = (Date.now() - начало) / 1000;
      if (секунд >= 1 && событие.loaded > 1048576) {
        var скорость = событие.loaded / секунд;
        строки.push(объём(скорость) + "/с");
        var осталось = (событие.total - событие.loaded) / скорость;
        if (осталось >= 1) строки.push("осталось " + Math.ceil(осталось) + " с");
      }
      сказать(строки.join(", "));
    };

    /* Байты ушли, но ответа ещё нет: сервер дочитывает буфер, кладёт файл на
     * диск и ставит задание. Молчать здесь нельзя — это опять пустой экран. */
    xhr.upload.onload = function () {
      полоса.removeAttribute("value");     /* неопределённый прогресс */
      сказать(
        справочнаяБаза
          ? "Файл передан. Сервер проверяет каноническую базу…"
          : "Файл передан. Сервер принимает и ставит в разбор…"
      );
    };

    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 400) {
        window.location.replace("/sources");
      } else {
        отказ("Сервер отказал, код " + xhr.status + ". Обновите страницу.");
      }
    };
    xhr.onerror = function () { отказ("Связь оборвалась. Файл не передан."); };
    xhr.onabort = function () { отказ("Передача прервана."); };

    xhr.send(данные);
  });
})();
"""


_GRAPH_JS = """
(function(){
  var svg=document.getElementById('graph'); if(!svg) return;
  var vb=svg.getAttribute('viewBox').split(' ').map(Number), тащим=null;
  function применить(){ svg.setAttribute('viewBox', vb.join(' ')); }
  svg.addEventListener('pointerdown', function(e){
    тащим={x:e.clientX, y:e.clientY}; svg.setPointerCapture(e.pointerId); });
  svg.addEventListener('pointerup', function(){ тащим=null; });
  svg.addEventListener('pointermove', function(e){
    if(!тащим) return;
    var k=vb[2]/svg.clientWidth;
    vb[0]-=(e.clientX-тащим.x)*k; vb[1]-=(e.clientY-тащим.y)*k;
    тащим={x:e.clientX, y:e.clientY}; применить(); });
  svg.addEventListener('wheel', function(e){
    e.preventDefault();
    var k=e.deltaY>0?1.1:0.9, r=svg.getBoundingClientRect();
    var mx=(e.clientX-r.left)/r.width, my=(e.clientY-r.top)/r.height;
    vb[0]+=vb[2]*mx*(1-k); vb[1]+=vb[3]*my*(1-k);
    vb[2]*=k; vb[3]*=k; применить(); }, {passive:false});
})();
"""


def _graph_svg(область: Neighbourhood, config: str, limit: int) -> str:
    """Окрестность в инлайновый SVG. Никаких файлов и никакой сборки."""
    слева, сверху, ширина, высота = bounds(область)
    части = [
        f"<svg id=graph class=graph viewBox='{слева:.0f} {сверху:.0f} "
        f"{ширина:.0f} {высота:.0f}' xmlns='http://www.w3.org/2000/svg'>",
        # Стрелка показывает направление ссылки. Без неё «Склады — ЧекККМ»
        # не отвечает, кто на кого ссылается, а это половина смысла ребра.
        "<defs><marker id=arrow viewBox='0 0 8 8' refX=7 refY=4 markerWidth=6 "
        "markerHeight=6 orient=auto-start-reverse>"
        "<path d='M0 0 L8 4 L0 8 z' fill='#b9b9b9'/></marker></defs>",
    ]

    for узел, связь in zip(область.nodes, область.links):
        наружу = связь.outgoing
        класс = "edge" if наружу else "edge in"
        # Стрелка всегда указывает на цель ссылки, а не на край линии.
        x1, y1, x2, y2 = (
            (область.subject.x, область.subject.y, узел.x, узел.y)
            if наружу
            else (узел.x, узел.y, область.subject.x, область.subject.y)
        )
        части.append(
            f"<line class='{класс}' x1={x1:.0f} y1={y1:.0f} x2={x2:.0f} "
            f"y2={y2:.0f} marker-end='url(#arrow)'><title>{escape(связь.title)}"
            "</title></line>"
        )

    def кружок(узел, радиус: float, свой: bool = False) -> str:
        цвет = KIND_COLORS.get(узел.kind, KIND_FALLBACK)
        адрес = f"/graph?config={quote(config)}&name={quote(узел.name)}&limit={limit}"
        подсказка = f"{узел.name} · связей {узел.degree}"
        класс = " class=subject" if свой else ""
        return (
            f"<a href='{адрес}'><title>{escape(подсказка)}</title>"
            f"<circle{класс} cx={узел.x:.0f} cy={узел.y:.0f} r={радиус} "
            f"fill='{цвет}'/>"
            f"<text x={узел.x:.0f} y={узел.y + радиус + 13:.0f} "
            f"text-anchor=middle>{escape(узел.short[:26])}</text></a>"
        )

    for узел in область.nodes:
        части.append(кружок(узел, 9.0))
    части.append(кружок(область.subject, 15.0, свой=True))
    части.append("</svg>")
    return "".join(части)


def _graph_page(registry: Registry, config: str, name: str, limit: int) -> HTMLResponse:
    # Первая по алфавиту, если не выбрана: страница без выбранной конфигурации
    # ничего показать не может, а отказ вместо картинки — тупик. Тот же приём,
    # что на «Запросах» и «Словаре».
    известные = registry.snapshot().configuration_names
    if config not in известные:
        config = известные[0] if известные else ""

    if not config:
        return _layout(
            "Граф связей",
            "<p>Не загружено ни одной конфигурации — граф строить не по чему. "
            "Загрузите выгрузку структуры на странице "
            "<a href=/sources>Источники</a>.</p>",
        )

    try:
        context = registry.resolve(config)
    except RegistryError as ошибка:
        # Форма остаётся на странице всегда: отказ без неё не даёт исправить
        # то, на что он жалуется.
        return _layout(
            "Граф связей",
            _graph_form(registry, config, name, limit)
            + f"<div class=error>{escape(str(ошибка))}</div>",
        )

    подпись = f"Граф связей — {context.name}"
    if not name:
        # Пустая форма без единого слова читается как «страница не доделана».
        # Одна строка объясняет, что тут вообще происходит и что вводить.
        return _layout(
            подпись,
            _graph_form(registry, context.name, "", limit)
            + "<p>Окрестность объекта: кто на него ссылается и на что ссылается "
            "он. Введите полное имя — <code>Документ.ЧекККМ</code>, "
            "<code>Справочник.Номенклатура</code>, "
            "<code>РегистрНакопления.ТоварыНаСкладах</code>. Имя можно взять "
            "со страницы <a href=/queries>Запросы</a>.</p>",
        )

    if name not in context.configuration.config.objects:
        # Похожие имена, как это делает `get_object`: имя объекта в 1С длинное
        # и опечатка в нём — самый частый способ сюда попасть. Отказ без
        # подсказки заставляет уходить на другую страницу за именем.
        похожие = context.configuration.index.search(name, limit=5)
        подсказка = "".join(
            f"<li><a href='/graph?config={quote(context.name)}"
            f"&name={quote(hit.doc.id)}&limit={limit}'>"
            f"{escape(hit.doc.id)}</a></li>"
            for hit in похожие
        )
        тело = _graph_form(registry, context.name, name, limit) + (
            f"<div class=error>В конфигурации {escape(context.name)} нет объекта "
            f"<code>{escape(name)}</code>.</div>"
        )
        if подсказка:
            тело += f"<p>Возможно, имелось в виду:</p><ul>{подсказка}</ul>"
        return _layout(подпись, тело)

    область = neighbourhood(context.configuration.graph, name, limit=limit)
    части = [_graph_form(registry, context.name, name, limit)]

    if not область.total:
        # Изолированный объект — не ошибка и не пустая страница: сказать об
        # этом прямо полезнее, чем показать холст с одной точкой без пояснений.
        части.append(
            f"<p><code>{escape(name)}</code> ни на что не ссылается и на него "
            "не ссылается никто. Так выглядят константы и объекты, связи "
            "которых живут в формах и схемах компоновки — их выгрузка пока не "
            "собирает.</p>"
        )
        return _layout(подпись, "".join(части))

    показано = (
        f"показано {область.shown} из {область.total}"
        if область.truncated
        else f"связей {область.total}"
    )
    части.append(
        f"<p><b>{escape(name)}</b> · {показано} · "
        f"<a href='/object?config={quote(context.name)}&name={quote(name)}'>"
        "карточка объекта</a></p>"
    )
    if область.truncated:
        части.append(
            "<div class=note>Показаны самые связанные соседи. Остальные не "
            "потерялись — поднимите предел, если нужны все.</div>"
        )

    части.append(_graph_svg(область, context.name, limit))
    части.append(
        "<p>Тянуть — перетаскиванием, масштаб — колесом. Клик по узлу строит "
        "граф вокруг него, наведение показывает, через что идёт связь. "
        "Сплошная стрелка — объект ссылается наружу, пунктир — ссылаются "
        "на него.</p>"
    )
    части.append(_graph_legend(область))
    части.append(f"<script>{_GRAPH_JS}</script>")
    return _layout(подпись, "".join(части))


def _graph_legend(область: Neighbourhood) -> str:
    виды = sorted({узел.kind for узел in область.nodes} | {область.subject.kind})
    метки = [
        f"<span><i style='background:{KIND_COLORS.get(вид, KIND_FALLBACK)}'></i>"
        f"{escape(вид or '—')}</span>"
        for вид in виды
    ]
    return f"<p class=legend>{''.join(метки)}</p>"


def _graph_form(
    registry: Registry, config: str, name: str, limit: int = 0
) -> str:
    """Форма страницы: конфигурация, объект, предел.

    Конфигурация выбирается здесь, а не приходит скрытым полем. Скрытым она и
    была — и по ссылке из навигации, где `config` пустой, страница упиралась в
    «укажите нужную явно» без единого способа её указать. Тупик нашёл владелец
    на живом дашборде; тестами он не ловился, потому что все они ходили с уже
    заданным именем конфигурации.

    Предел не спрятан в коде, потому что правильного значения нет. Тридцать
    читаются на экране целиком, сто пятьдесят требуют зума, но показывают всю
    картину сразу — что нужнее, знает только человек перед экраном.
    """
    выбранный = limit or DEFAULT_GRAPH_LIMIT
    пределы = "".join(
        f"<option value={n}{' selected' if n == выбранный else ''}>{n}</option>"
        for n in (15, 30, 60, 150, 400)
    )
    конфигурации = "".join(
        f"<option{' selected' if имя == config else ''}>{escape(имя)}</option>"
        for имя in registry.snapshot().configuration_names
    )
    return (
        "<form method=get action=/graph>"
        f"<p>конфигурация: <select name=config>{конфигурации}</select></p>"
        f"<p><input name=name value='{escape(name)}' size=46 "
        "placeholder='Документ.ЧекККМ'> "
        f"<select name=limit>{пределы}</select> соседей "
        "<button type=submit>Показать</button></p></form>"
    )


def _card_page(
    registry: Registry,
    kind: str,
    config: str,
    name: str,
    detail: str,
    raw: bool = False,
) -> HTMLResponse:
    """Карточка объекта или элемента платформы — та же, что видит агент.

    Текст берётся у `tools` без изменений: это буквально тот ответ, который
    уходит по MCP. Markdown разбирается только здесь — ни агенту, ни CLI это
    не нужно, символы мешают лишь в HTML. Конвертер свой, на полсотни строк:
    `render.py` порождает шесть конструкций, и тянуть ради них зависимость
    незачем.

    Переключатель «как есть» показывает исходный текст. Он не для красоты:
    дашборд — инструмент проверки, и поехавшую разметку в разобранном виде
    было бы не видно.
    """
    if detail not in DETAIL_LEVELS:
        detail = "fields"
    try:
        text = _card_text(registry, kind, config, name, detail)
    except RegistryError as error:
        text = str(error)

    levels = " ".join(
        f"<a href='?config={quote(config)}&name={quote(name)}&detail={level}'>"
        f"{'<b>' if level == detail else ''}{level}{'</b>' if level == detail else ''}</a>"
        for level in DETAIL_LEVELS
    )
    # Разобранный markdown читается глазами, сырой показывает буквально то, что
    # ушло агенту. Второе нужно не реже первого: дашборд — инструмент проверки,
    # и поехавшая разметка в разобранном виде осталась бы незаметной.
    адрес = f"?config={quote(config)}&name={quote(name)}&detail={detail}"
    переключатель = (
        f"<a href='{адрес}'>разобрать</a>"
        if raw
        else f"<a href='{адрес}&raw=1'>как есть</a>"
    )
    карточка = (
        f"<pre class=card>{escape(text)}</pre>"
        if raw
        else f"<div class=card>{render_markdown(text)}</div>"
    )
    body = f"<p>подробность: {levels} · {переключатель}</p>{карточка}"
    return _layout(name, body)


def _card_text(
    registry: Registry,
    kind: str,
    config: str,
    name: str,
    detail: str,
) -> str:
    """Буквальный текст карточки для classic UI и JSON API SPA."""
    if kind == "syntax":
        return tools.get_syntax(registry, name, config or None, detail)
    return tools.get_object(registry, name, config or None, detail)


def _kind_title(scope: str, kind: str) -> str:
    """Подпись вида найденного элемента — той же таблицей, что видит агент.

    Для `fields` берётся та же подпись, что печатает CLI и MCP
    (`FIELD_KIND_TITLES`); для `objects` вид уже человеческое слово
    («Справочник», «Документ») и не нуждается в замене.
    """
    if scope == "syntax":
        return KIND_TITLES.get(kind, kind)
    if scope == "fields":
        return FIELD_KIND_TITLES.get(kind, kind)
    return kind


def _queries_page(
    registry: Registry,
    *,
    config: str = "",
    scope: str = "objects",
    phrases_text: str = "",
    results: list[tuple[str, list, list]] | None = None,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    options = "".join(
        f"<option{' selected' if name == config else ''}>{escape(name)}</option>"
        for name in registry.snapshot().configuration_names
    )
    scope_inputs = "".join(
        f"<label><input type=radio name=scope value={key}"
        f"{' checked' if key == scope else ''}> по {title}</label> "
        for key, title in SCOPES.items()
    )

    parts = [
        "<h2>Прогон запросов</h2>",
        "<form method=post action=/queries>",
        f"<p>конфигурация: <select name=config>{options}</select></p>",
        f"<p>{scope_inputs}</p>",
        "<p><textarea name=phrases rows=8 cols=70 "
        f"placeholder='одна фраза на строку'>{escape(phrases_text)}</textarea></p>",
        f"<p class=note>За один прогон — не более {MAX_QUERY_PHRASES} фраз.</p>",
        "<button>Прогнать</button></form>",
    ]
    if error:
        parts.append(f"<div class=error>{escape(error)}</div>")

    for phrase, hits, hidden in results or []:
        parts.append(f"<h3>{escape(phrase)}</h3>")
        if not hits and not hidden:
            parts.append("<p>ничего не найдено</p>")
            continue
        if not hits:
            parts.append("<p>в этой версии платформы — ничего</p>")
        # Ссылка на словарь с предзаполненной фразой: от промаха до лечения
        # один переход, ради этого страница и заводилась.
        if scope != "syntax":
            fix = f"/dictionary?config={quote(config)}&phrase={quote(phrase)}"
            parts.append(
                f"<p><a href='{escape(fix)}'>не то — завести псевдоним</a></p>"
            )
        parts.append("<table><tr><th>#<th>Что нашлось<th>Вид<th>Оценка<th>Почему</tr>")
        for position, hit in enumerate(hits, start=1):
            link = _card_link(scope, config, hit)
            title = (
                getattr(hit.doc.payload, "address", "") or hit.doc.id
                if scope == "syntax"
                else hit.doc.id
            )
            вид = _kind_title(scope, hit.doc.kind)
            parts.append(
                f"<tr><td>{position}"
                f"<td><a href='{escape(link)}'>{escape(title)}</a>"
                f"<td>{escape(вид)}"
                f"<td>{hit.score:.1f}<td>{escape(hit.reason or '—')}</tr>"
            )
        parts.append("</table>")
        parts.extend(_hidden_block(hidden))
    page = _layout("Запросы", "".join(parts))
    page.status_code = status_code
    return page


def _hidden_block(hidden: list) -> list[str]:
    """Что отсеял фильтр версии и по какой причине.

    Причины противоположные — элемент ещё не появился или его уже нет, — и
    сливать их в одну строку нельзя: агенту в первом случае поможет
    обновление платформы, во втором оно же всё сломает.
    """
    if not hidden:
        return []

    parts = ["<h4>Скрыто фильтром версии</h4><ul>"]
    for hit in hidden:
        item = hit.doc.payload
        причина = escape(_hidden_reason(item))
        имя = escape(getattr(item, "address", "") or hit.doc.id)
        parts.append(f"<li><code>{имя}</code> — {причина}</li>")
    parts.append("</ul>")
    return parts


def _hidden_reason(item) -> str:
    """Единое человекочитаемое объяснение фильтра версии для classic и SPA."""
    if item.until:
        return f"описан по версию {item.until} включительно, дальше его нет"
    if item.since:
        return f"появился в {item.since}"
    return "недоступен в этой версии"


def _login_form() -> str:
    if not _admin_token() and not _api_token():
        # Не «закрыто», а «не существует»: без токенов ручек записи нет вовсе.
        return (
            "<h2>Загрузка недоступна</h2>"
            "<p>Не задан <code>ADMIN_TOKEN</code>. Задайте его в окружении "
            "сервера — тогда появятся загрузка и удаление источников.</p>"
        )
    подсказка = (
        "<p>Токен администратора открывает загрузку источников и правку "
        "словаря. Токен чтения (<code>API_TOKEN</code>) — только просмотр.</p>"
        if _api_token() and _admin_token()
        else "<p>Изменение источников требует токена администратора.</p>"
    )
    return (
        "<h2>Вход</h2>"
        + подсказка
        + "<form method=post action=/login>"
        "<input type=password name=token required> <button>Войти</button></form>"
    )


# Виды остальных источников (`configuration`, `syntax`) показывались и
# показываются сырым словом — это устоявшееся поведение, менять его не
# просят. `modules` и `extension` печатались латиницей и человеку ничего не
# говорили — особенно важно для расширения: без подписи строка
# `<Конфигурация>:ext:<Имя>` выглядит как ещё один модульный источник, а не
# как отдельная сущность.
_SOURCE_KIND_TITLES = {
    KIND_MODULES: "Модули",
    KIND_EXTENSION: "Расширение",
    KIND_EXTENSION_RUNTIME: "Снимок активности расширений",
}


@dataclass(frozen=True, slots=True)
class _OrphanRow:
    relative: str
    size: int


@dataclass(frozen=True, slots=True)
class _IncomingRow:
    name: str
    size: int
    state: str
    detail: str
    settling: bool


@dataclass(frozen=True, slots=True)
class _JobRow:
    name: str
    size: int
    state: str
    error: str


@dataclass(frozen=True, slots=True)
class _SourcesPageData:
    sources: tools.SourcesSnapshot
    sources_error: str
    orphans: tuple[_OrphanRow, ...]
    incoming: tuple[_IncomingRow, ...]
    incoming_exists: bool
    incoming_dir: str
    jobs: tuple[_JobRow, ...]
    authorized: bool


def _prepare_sources_page(
    registry: Registry,
    *,
    authorized: bool,
) -> _SourcesPageData:
    """Снять данные, пройти диск и только затем подтвердить поколение."""

    def collect(
        sources: tools.SourcesSnapshot, sources_error: str = ""
    ) -> _SourcesPageData:
        orphans = tuple(
            _OrphanRow(path.relative_to(registry.data_dir).as_posix(), size)
            for path, size in registry.orphan_sources()
        )
        if authorized:
            incoming = tuple(
                _IncomingRow(
                    name=str(row["name"]),
                    size=int(row["size"]),
                    state=str(row["state"]),
                    detail=str(row["detail"]),
                    settling=bool(row.get("settling")),
                )
                for row in _scanner(registry).scan()
            )
            incoming_exists = registry.incoming_dir.is_dir()
        else:
            incoming = ()
            incoming_exists = False
        jobs = tuple(
            _JobRow(
                name=str(job["name"]),
                size=int(job["size"]),
                state=str(job["state"]),
                error=str(job["error"]),
            )
            for job in _JOBS
        )
        return _SourcesPageData(
            sources=sources,
            sources_error=sources_error,
            orphans=orphans,
            incoming=incoming,
            incoming_exists=incoming_exists,
            incoming_dir=str(registry.incoming_dir),
            jobs=jobs,
            authorized=authorized,
        )

    last: _SourcesPageData | None = None
    for _ in range(2):
        prepared = tools._capture_sources_snapshot(registry)
        if prepared is None:
            continue
        sources, capture = prepared
        last = collect(sources)
        if tools._sources_snapshot_is_current(registry, capture):
            return last

    error = (
        "Источники изменились дважды; повторите запрос после завершения загрузки."
    )
    empty = tools.SourcesSnapshot((), (), ())
    if last is None:
        return collect(empty, error)
    return _SourcesPageData(
        sources=empty,
        sources_error=error,
        orphans=last.orphans,
        incoming=last.incoming,
        incoming_exists=last.incoming_exists,
        incoming_dir=last.incoming_dir,
        jobs=last.jobs,
        authorized=last.authorized,
    )


def _sources_page(
    data: _SourcesPageData,
    *,
    error: str = "",
    reference: dict | None = None,
) -> HTMLResponse:
    """Чистая отрисовка заранее собранного снимка страницы «Источники»."""
    authorized = data.authorized
    parts: list[str] = []
    if error:
        parts.append(f"<div class=error>{escape(error)}</div>")

    parts.append(
        "<h2>Загружено</h2><table><tr><th>Источник<th>Вид<th>Платформа"
        "<th>Элементов<th></tr>"
    )
    for source in data.sources.sources:
        button = (
            f"<form method=post action=/sources/remove>"
            f"<input type=hidden name=id value='{escape(source.id)}'>"
            f"<button>удалить</button></form>"
            if authorized
            else ""
        )
        вид = _SOURCE_KIND_TITLES.get(source.kind, source.kind)
        parts.append(
            f"<tr><td>{escape(source.id)}<td>{escape(вид)}"
            f"<td>{escape(source.platform or '—')}<td>{source.items_total}"
            f"<td>{button}</tr>"
        )
        # Источник, разобравшийся не полностью, снаружи выглядит здоровым:
        # счётчик элементов на месте, статус `ready`. Замечания печатались
        # только строкой CLI при загрузке — она прокручивается и теряется, а
        # человек смотрит сюда.
        for предупреждение in source.warnings:
            parts.append(
                f"<tr><td colspan=5 class=warn>! {escape(предупреждение)}</tr>"
            )
    parts.append("</table>")

    if reference is not None:
        active = reference["active"]
        pending = reference.get("pending")
        shown = pending or active
        labels = {
            "disabled": "выключена",
            "missing": "не загружена",
            "untrusted": "подпись не подтверждена",
            "incompatible": "несовместима",
            "corrupt": "повреждена",
            "ready": "активна",
            "pending_restart": "ожидает перезапуска",
        }
        state = labels.get(shown["state"], shown["state"])
        parts.append(
            "<h2>Локальная общая справка</h2>"
            "<p>Опциональная каноническая база добавляет только "
            "<code>search_reference</code> и <code>get_reference</code>. "
            "Основной MCP работает и без неё.</p>"
            f"<p><b>Состояние: {escape(state)}.</b> "
            f"{escape(shown['message'])}</p>"
        )
        if authorized:
            details = []
            if shown.get("items"):
                details.append(f"элементов: {shown['items']}")
            if shown.get("schema_version"):
                details.append(f"schema: {escape(shown['schema_version'])}")
            if shown.get("index_cache"):
                details.append(f"индекс: {escape(shown['index_cache'])}")
            if shown.get("signature"):
                details.append(f"подпись: {escape(shown['signature'])}")
            if details:
                parts.append(f"<p>{' · '.join(details)}</p>")
            if reference.get("managed_upload"):
                parts.append(
                    "<p>Для установки или замены выберите <code>.sqlite3</code> "
                    "в общей форме «Загрузить» ниже. После успешной проверки "
                    "перезапустите сервер и MCP-клиент.</p>"
                )
                if reference.get("managed_file_present"):
                    parts.append(
                        "<form method=post action=/api/v1/reference/remove>"
                        "<label>Для удаления введите "
                        "<code>reference.sqlite3</code>: "
                        "<input name=confirmation required "
                        "pattern='reference\\.sqlite3'></label> "
                        "<button>Удалить общую базу</button></form>"
                    )
                if pending is not None:
                    if reference.get("restart_available"):
                        parts.append(
                            "<form method=post action=/api/v1/server/restart>"
                            "<button>Перезапустить сервер и применить изменение"
                            "</button></form>"
                        )
                    else:
                        parts.append(
                            "<p class=warn>Перезапуск из дашборда выключен; "
                            "изменение должен применить оператор сервера.</p>"
                        )
            else:
                parts.append(
                    "<p class=warn>Dashboard upload выключен: база подключена "
                    "через внешний <code>MCP1C_REFERENCE_DB</code>.</p>"
                )

    if data.sources.code or data.sources_error or data.sources.configuration_names:
        parts.append(
            "<h2>Индексы кода</h2>"
            "<table><tr><th>Конфигурация<th>Корпус<th>Состояние</tr>"
        )
        if data.sources_error:
            parts.append(
                f"<tr><td colspan=3 class=warn>! {escape(data.sources_error)}</tr>"
            )
        elif data.sources.code:
            for строка in data.sources.code:
                parts.append(
                    f"<tr><td>{escape(строка.configuration)}"
                    f"<td>{escape(строка.corpus)}"
                    f"<td>{escape(строка.state)}</tr>"
                )
                if строка.coverage is not None:
                    if строка.coverage.has_limitations:
                        parts.append(
                            "<tr class=warn><td colspan=3>"
                            "ВНИМАНИЕ: покрытие кода неполно; нулевой счётчик "
                            "не доказывает отсутствие скрытых данных.</tr>"
                        )
                    parts.append(
                        "<tr><td colspan=3>"
                        + _coverage_tables(строка.coverage)
                        + "</tr>"
                    )
                    if строка.journal:
                        parts.append(
                            "<tr><td colspan=3>Детальный разбор сохранён в "
                            f"<code>data/{escape(строка.journal)}</code>.</tr>"
                        )
                    else:
                        parts.append(
                            "<tr class=warn><td colspan=3>Детальный журнал "
                            "покрытия недоступен.</tr>"
                        )
        else:
            parts.append("<tr><td colspan=3>Конфигурации не загружены.</tr>")
        parts.append("</table>")

    if authorized and data.jobs:
        # Имя файла говорит, что за база, поэтому список виден только вошедшему.
        # Журнал за время жизни процесса. Убирается кнопкой: записи копятся и
        # мешают смотреть на то, что происходит сейчас.
        закончены = any(j.state in (JOB_DONE, JOB_FAILED) for j in data.jobs)
        очистка = (
            "<form method=post action=/sources/jobs/clear style='display:inline'>"
            "<button>очистить завершённые</button></form>"
            if закончены
            else ""
        )
        parts.append(f"<h2>Загрузка</h2>{очистка}<table>"
                     "<tr><th>Файл<th>Размер<th>Состояние</tr>")
        for job in reversed(data.jobs):
            подробность = (
                f" — {escape(job.error)}" if job.state == JOB_FAILED else ""
            )
            parts.append(
                f"<tr><td>{escape(job.name)}"
                f"<td>{_объём(job.size)}"
                f"<td>{escape(job.state)}{подробность}</tr>"
            )
        parts.append("</table>")

    orphans = data.orphans
    if orphans:
        # Справка одна на процесс, и прежняя выбывает из реестра при замене —
        # а файл остаётся лежать. Молча занятые сотни мегабайт человек не
        # найдёт; удалять за него нельзя: справку от снятой с поддержки
        # платформы взять заново негде.
        весом = sum(orphan.size for orphan in orphans) / 1024 / 1024
        parts.append(
            f"<h2>Исходные файлы — {весом:.0f} МБ</h2>"
            "<p>Сервер работает по разобранным индексам, поэтому для ответов "
            "эти файлы не нужны: они понадобятся, только если индекс придётся "
            "строить заново. Здесь и исходник действующей справки, и остатки "
            "прежних загрузок — реестр их не различает.</p>"
            "<p><b>Прежде чем удалять, убедитесь, что файл можно взять "
            "снова.</b> Справку от снятой с поддержки платформы скачать уже "
            "негде.</p>"
            "<table><tr><th>Файл<th>Размер<th></tr>"
        )
        for orphan in orphans:
            relative = orphan.relative
            size = orphan.size
            button = (
                "<form method=post action=/sources/forget>"
                f"<input type=hidden name=path value='{escape(relative)}'>"
                "<button>удалить файл</button></form>"
                if authorized
                else ""
            )
            parts.append(
                f"<tr><td>{escape(relative)}"
                f"<td>{size / 1024 / 1024:.0f} МБ<td>{button}</tr>"
            )
        parts.append("</table>")

    if authorized:
        # Имя файла говорит, что за база, — блок виден только вошедшему, как
        # и журнал заданий. В «Исходные файлы» эти файлы не подмешиваются: там
        # заголовок «для ответов не нужны», а для неразобранной выгрузки это
        # неправда.
        from .incoming import (
            STATE_FAILED,
            STATE_NEW,
            STATE_STALE,
            STATE_UPDATED,
        )

        # Список тот же, что в таблице «Загружено»: у источника конфигурации
        # `id` — это её имя (`add_configuration`), сортировка по `id` там и
        # сортировка имён здесь совпадают.
        имена_конфигураций = data.sources.configuration_names
        if data.incoming:
            parts.append("<h2>Входящие выгрузки</h2><table>"
                         "<tr><th>Файл<th>Размер<th>Состояние</tr>")
            for строка in data.incoming:
                # «Разбор не удался» — не тупик: постановка (§2) назначает ему
                # то же действие. Без кнопки исправленный архив разобрать было
                # бы нечем, кроме переименования файла.
                # Конфигураций ноль — привязывать код не к чему, кнопки нет
                # вовсе (нынешний отказ человек увидел бы только после нажатия).
                можно = (
                    строка.state in (
                        STATE_NEW,
                        STATE_UPDATED,
                        STATE_STALE,
                        STATE_FAILED,
                    )
                    and not строка.settling
                    and bool(имена_конфигураций)
                )
                # «Переразобрать» там, где прежний разбор перетирается: человек
                # должен понимать, что делает с уже лежащим на диске кодом.
                подпись = (
                    "переразобрать"
                    if строка.state in (STATE_UPDATED, STATE_STALE)
                    else "разобрать"
                )
                # Выбор конфигурации — только когда есть из чего выбирать:
                # при одной загруженной лишний выбор из одного варианта только
                # мешает, `_configuration_for` и так возьмёт единственную.
                выбор = (
                    "<select name=configuration>"
                    + "".join(
                        f"<option>{escape(имя)}</option>" for имя in имена_конфигураций
                    )
                    + "</select> "
                    if можно and len(имена_конфигураций) > 1
                    else ""
                )
                кнопка = (
                    "<form method=post action=/sources/incoming/parse "
                    "style='display:inline'>"
                    f"<input type=hidden name=name value='{escape(строка.name)}'>"
                    f"{выбор}"
                    f"<button>{подпись}</button></form>"
                    if можно
                    else ""
                )
                подробность = (
                    f" — {escape(строка.detail)}" if строка.detail else ""
                )
                parts.append(
                    f"<tr><td>{escape(строка.name)}"
                    f"<td>{_объём(строка.size)}"
                    f"<td>{escape(строка.state)}{подробность} {кнопка}</tr>"
                )
            parts.append("</table>")
        elif data.incoming_exists:
            # Пустой каталог — тоже сведение: без этого блока человек не видит
            # ни того, что приём есть, ни того, куда класть архив.
            parts.append(
                "<h2>Входящие выгрузки</h2>"
                f"<p>Пусто. Положите выгрузку конфигурации в файлы (.zip) в "
                f"<code>{escape(data.incoming_dir)}</code> — она "
                "появится здесь с кнопкой «разобрать». Сканируется только сам "
                "каталог, без вложенных подкаталогов.</p>"
            )

    if authorized:
        reference_limit = (
            reference["limits"]["upload_bytes"] if reference is not None else 0
        )
        reference_managed = int(
            bool(reference is not None and reference.get("managed_upload"))
        )
        parts.append(
            "<h2>Загрузить</h2>"
            # `data-limit` — тот же MAX_UPLOAD числом: браузер знает размер
            # файла до отправки и отказывает сразу, а не после того, как
            # полтерабайта трафика уже потрачены на серверную проверку.
            f"<form id=upload-form data-limit={MAX_UPLOAD} "
            f"data-reference-limit={reference_limit} "
            f"data-reference-managed={reference_managed} "
            "method=post action=/sources enctype=multipart/form-data>"
            "<input type=file name=file accept='.zip,.hbk,.json,.sqlite3' required> "
            "<button>Загрузить</button>"
            "<label><input type=checkbox name=allow_truncated value=1> "
            "явно опубликовать тестовую неполную выгрузку "
            "<code>truncated=true</code></label>"
            # Индикатор скрыт до начала передачи и наполняется скриптом.
            # Без JS остаётся скрытым, а форма работает как обычная.
            "<div id=upload-progress class=upload hidden>"
            "<progress id=upload-bar max=100 value=0 hidden></progress>"
            "<span id=upload-text></span></div>"
            # Имя файла названо прямо: в каталоге установки платформы лежат
            # 38 файлов `.hbk`, и без подсказки человек берёт наугад соседний.
            "<p>Принимаются четыре вида файлов. Для Registry предел "
            f"{MAX_UPLOAD // 1024 // 1024} МиБ, для SQLite общей справки — "
            f"{reference_limit // 1024 // 1024} МиБ.</p>"
            "<ul>"
            "<li><b>Выгрузка структуры</b> — <code>.zip</code>, который "
            "делает обработка <code>ВыгрузкаСтруктуры</code>.</li>"
            "<li><b>Активность расширений</b> — "
            "<code>СнимокРасширений_*.json</code>, который делает отдельная "
            "обработка снимка в текущем сеансе.</li>"
            "<li><b>Справка платформы</b> — файл <code>shcntx_ru.hbk</code> "
            "из каталога установки 1С:<br>"
            "<code>/opt/1cv8/&lt;версия&gt;/shcntx_ru.hbk</code><br>"
            "<code>C:\\Program Files\\1cv8\\&lt;версия&gt;\\bin\\shcntx_ru.hbk</code>"
            "<br>Имя должно совпадать целиком.</li>"
            "<li><b>Общая справка</b> — каноническая <code>.sqlite3</code> "
            "schema v1. Она полностью проверяется и становится активной "
            "после перезапуска сервера.</li>"
            "</ul>"
            "<p>Рядом лежат сотни файлов <code>.hbk</code> (38 справок × "
            "языки), и похожие есть: <code>shcntx_root.hbk</code> — та же "
            "справка платформы без текстов, одни английские идентификаторы; "
            "<code>shlang_ru.hbk</code> — справка по встроенному языку, "
            "другой формат страниц, не подходит. По размеру их не "
            "отличить.</p>"
            "<p>Передача файла показывается полосой здесь же. Разбор идёт в "
            "фоне: страница ответит сразу, а состояние покажет в разделе "
            "«Загрузка» выше.</p></form>"
            f"<script>{_UPLOAD_JS}</script>"
        )
    else:
        parts.append(_login_form())

    # Пока работа идёт — страница обновляет себя сама. Обычный `meta refresh`,
    # а не JS: дашборд обязан работать с выключенным JS. Работа кончилась —
    # обновление прекращается, иначе страница дёргалась бы вечно.
    работает = any(j.state in (JOB_READING, JOB_PARSING) for j in data.jobs)
    return _layout("Источники", "".join(parts), refresh=2 if работает else 0)


def _coverage_percent(count: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{count * 100 / total:.1f}%".replace(".", ",")


def _coverage_table(
    title: str,
    rows: tuple[tuple[str, int, int], ...],
) -> str:
    parts = [
        f'<table aria-label="Таблица покрытия: {escape(title)}">'
        f"<caption>Таблица покрытия: {escape(title)}</caption>"
        "<tr><th>Состояние<th>Количество<th>Доля</tr>"
    ]
    for label, count, total in rows:
        parts.append(
            f"<tr><td>{escape(label)}<td>{count} из {total}"
            f"<td>{_coverage_percent(count, total)}</tr>"
        )
    parts.append("</table>")
    return "".join(parts)


def _coverage_tables(coverage: tools.CodeCoverage) -> str:
    modules = coverage.modules_total
    procedures = coverage.procedures_total
    forms = coverage.forms_total
    return "".join(
        (
            _coverage_table(
                "модули и процедуры",
                (
                    ("Модули всего", modules, modules),
                    ("С доступным исходником", coverage.modules_source_available, modules),
                    ("Пустые", coverage.modules_empty, modules),
                    ("Частично разобраны", coverage.modules_partial, modules),
                    ("Не прочитаны", coverage.modules_unreadable, modules),
                    ("Конфликтуют", coverage.modules_conflict, modules),
                    (
                        "Скомпилированы без исходника",
                        coverage.modules_compiled_without_source,
                        modules,
                    ),
                    ("Процедуры всего", procedures, procedures),
                    (
                        "Процедуры разобраны полностью",
                        coverage.procedures_full,
                        procedures,
                    ),
                    (
                        "Процедуры разобраны частично",
                        coverage.procedures_partial,
                        procedures,
                    ),
                ),
            ),
            _coverage_table(
                "структуры форм",
                (
                    ("Формы всего", forms, forms),
                    ("Полностью разобраны", coverage.form_structures_full, forms),
                    ("Частично разобраны", coverage.form_structures_partial, forms),
                    ("Недоступны", coverage.form_structures_unread, forms),
                ),
            ),
            _coverage_table(
                "модули форм",
                (
                    ("Формы всего", forms, forms),
                    ("Модуль прочитан", coverage.form_modules_read, forms),
                    ("Модуль пуст", coverage.form_modules_empty, forms),
                    ("Модуль отсутствует", coverage.form_modules_missing, forms),
                    ("Модуль не прочитан", coverage.form_modules_unread, forms),
                ),
            ),
        )
    )


def _dictionary_page(
    registry: Registry,
    *,
    config: str = "",
    authorized: bool = False,
    error: str = "",
    phrase: str = "",
    targets: str = "",
) -> HTMLResponse:
    """Словарь: что правило говорит и откуда оно взялось.

    Происхождение показывается всегда, а не только в CLI: с него начинается
    разбор «почему поиск так себя ведёт» — встроенное правило чинят в
    `synonyms.py` через обычную проверку кода, локальное правят здесь же.
    """
    names = registry.snapshot().configuration_names
    if config not in names:
        config = names[0] if names else ""

    options = "".join(
        f"<option{' selected' if name == config else ''}>{escape(name)}</option>"
        for name in names
    )
    parts = [
        "<h2>Словарь</h2>",
        "<p>Слова человека против имён в конфигурации. Синонимы заменяют слово "
        "на слово, псевдоним указывает на конкретный объект — им лечат промах, "
        "который ранжированием не достать.</p>",
        f"<form method=get action=/dictionary><p>конфигурация: "
        f"<select name=config onchange='this.form.submit()'>{options}</select> "
        f"<button>показать</button></p></form>",
    ]
    if error:
        parts.append(f"<div class=error>{escape(error)}</div>")

    parts.append("<h3>Псевдонимы</h3>")
    parts.append("<table><tr><th>Фраза<th>Объекты<th>Происхождение<th></tr>")
    for alias, (objects, source) in sorted(
        registry.dictionary.aliases_with_source(config or None).items()
    ):
        # Встроенные правила лежат в коде и меняются вместе с поставкой — кнопки у
        # них нет намеренно, иначе удаление тихо разошлось бы с поставкой.
        local = source != DICT_BUILTIN
        button = (
            "<form method=post action=/dictionary/alias/remove>"
            f"<input type=hidden name=phrase value='{escape(alias)}'>"
            f"<input type=hidden name=config value='{escape(config)}'>"
            "<button>удалить</button></form>"
            if authorized and local
            else ""
        )
        parts.append(
            f"<tr><td>{escape(alias)}<td>{escape(', '.join(objects))}"
            f"<td>{escape(source)}<td>{button}</tr>"
        )
    parts.append("</table>")

    groups = registry.dictionary.synonym_groups
    parts.append(f"<h3>Свои группы синонимов — {len(groups)}</h3>")
    if groups:
        parts.append("<table><tr><th>Слова одной группы<th></tr>")
        for group in groups:
            # Группа опознаётся по составу, а не по номеру: номер сдвинется от
            # правки соседей, а состав — то, что человек видит в этой строке.
            button = (
                "<form method=post action=/dictionary/synonyms/remove>"
                f"<input type=hidden name=words value='{escape(' '.join(group))}'>"
                "<button>снять</button></form>"
                if authorized
                else ""
            )
            parts.append(f"<tr><td>{escape(', '.join(group))}<td>{button}</tr>")
        parts.append("</table>")
    stats = registry.dictionary.stats()
    parts.append(
        f"<p>Встроенных групп {stats['встроенных групп синонимов']}, "
        f"встроенных псевдонимов {stats['встроенных псевдонимов']} — они в коде "
        "и меняются вместе с поставкой, отсюда не видны как строки.</p>"
    )

    if authorized:
        parts.append(
            "<h3>Завести псевдоним</h3>"
            "<form method=post action=/dictionary/alias>"
            f"<p>фраза: <input name=phrase size=40 required value='{escape(phrase)}'></p>"
            f"<p>объекты: <input name=targets size=60 required value='{escape(targets)}' "
            "placeholder='Справочник.Контрагенты, через запятую или пробел'></p>"
            f"<p>область: <select name=config>"
            f"<option value='{escape(config)}'>только {escape(config) or '—'}</option>"
            "<option value=''>все конфигурации</option></select></p>"
            "<button>Завести</button>"
            "<p>Область по умолчанию — эта конфигурация: то, что в одной базе "
            "физлицами зовут пользователей, знание про эту базу, а не про 1С.</p>"
            "</form>"
            "<h3>Завести группу синонимов</h3>"
            "<form method=post action=/dictionary/synonyms>"
            "<p><input name=words size=60 required "
            "placeholder='возчик перевозчик экспедитор'></p>"
            "<button>Завести</button>"
            "<p>Синонимы общие для всех конфигураций: слово заменяется словом "
            "независимо от того, как названы объекты.</p></form>"
        )
    else:
        parts.append(_login_form())
    return _layout("Словарь", "".join(parts))


def _apply_dictionary_change(registry: Registry, mutation):
    """Записать словарь и подхватить его без перезапуска.

    Пересборки индексов не требуется: ни синонимы, ни псевдонимы не участвуют
    в построении постингов, они читаются в момент поиска.
    """
    return registry.mutate_dictionary(mutation)


class _DictionaryGroupNotFound(Exception):
    """Своей группы нет; это ожидаемый ответ формы, а не ошибка сохранения."""


def _read_denied() -> HTMLResponse:
    """Отказ в чтении. Ни имён конфигураций, ни счётчиков — это уже сведения."""
    page = _layout(
        "Нужен токен",
        "<h2>Доступ закрыт</h2>"
        "<p>Сервер работает под <code>API_TOKEN</code>. Клиенты MCP передают "
        "его заголовком <code>X-Api-Token</code> или "
        "<code>Authorization: Bearer</code>; для браузера — "
        "<a href=/login>вход по токену</a>.</p>",
    )
    return HTMLResponse(page.body, status_code=401)


def routes(
    registry: Registry,
    *,
    reference: ReferenceService | None = None,
    restart_available: bool = False,
) -> list[Route]:
    if reference is None:
        reference = ReferenceService.discover(registry.data_dir)
    def guard_read(handler):
        """Закрывает страницу, пока `API_TOKEN` задан и не предъявлен."""

        async def wrapped(request: Request):
            if not can_read(request):
                return _read_denied()
            return await handler(request)

        wrapped.__name__ = handler.__name__
        return wrapped

    async def overview(request: Request) -> HTMLResponse:
        return _overview_page(registry)

    async def render_sources(
        *, error: str = "", authorized: bool = False, status_code: int = 200
    ) -> HTMLResponse:
        data = await run_in_threadpool(
            _prepare_sources_page, registry, authorized=authorized
        )
        reference_payload = reference.payload(detailed=authorized)
        reference_payload["restart_available"] = restart_available
        page = _sources_page(
            data,
            error=error,
            reference=reference_payload,
        )
        if status_code == 200:
            return page
        return HTMLResponse(page.body, status_code=status_code)

    async def sources(request: Request) -> HTMLResponse:
        authorized = _authorized(request)
        return await render_sources(authorized=authorized)

    async def dictionary_page(request: Request) -> HTMLResponse:
        return _dictionary_page(
            registry,
            config=request.query_params.get("config", ""),
            authorized=_authorized(request),
            phrase=request.query_params.get("phrase", ""),
            targets=request.query_params.get("target", ""),
        )

    def _write_guard(request: Request) -> HTMLResponse | None:
        """Общая охрана правок словаря: без токена ручки не существует."""
        if not _admin_token():
            return PlainTextResponse("Правка словаря выключена: не задан ADMIN_TOKEN.", 404)
        if not _authorized(request):
            page = _dictionary_page(registry, error="Нужен вход администратора.")
            return HTMLResponse(page.body, status_code=403)
        return _csrf_denied(request)

    async def alias_add(request: Request) -> HTMLResponse:
        denied = _write_guard(request)
        if denied is not None:
            return denied

        form = await request.form()
        config = str(form.get("config", "")).strip()
        phrase = str(form.get("phrase", ""))
        # Объекты разделяют запятой или пробелом — как удобнее человеку, а не
        # как удобнее разбору.
        targets = [t for t in str(form.get("targets", "")).replace(",", " ").split() if t]
        try:
            _apply_dictionary_change(
                registry,
                lambda dictionary: dictionary.add_alias(
                    phrase, targets, config or None
                ),
            )
        except ValueError as error:
            return _dictionary_page(
                registry, config=config, authorized=True, error=str(error),
                phrase=phrase, targets=str(form.get("targets", "")),
            )
        return RedirectResponse(f"/dictionary?config={quote(config)}", status_code=303)

    async def alias_remove(request: Request) -> HTMLResponse:
        denied = _write_guard(request)
        if denied is not None:
            return denied

        form = await request.form()
        config = str(form.get("config", "")).strip()
        phrase = str(form.get("phrase", ""))
        # Псевдоним мог быть заведён и для всех конфигураций: снимаем там, где
        # он на самом деле лежит, иначе кнопка «удалить» ничего не делает.
        def remove(dictionary):
            if not dictionary.remove_alias(phrase, config or None):
                dictionary.remove_alias(phrase, None)

        _apply_dictionary_change(registry, remove)
        return RedirectResponse(f"/dictionary?config={quote(config)}", status_code=303)

    async def synonyms_add(request: Request) -> HTMLResponse:
        denied = _write_guard(request)
        if denied is not None:
            return denied

        form = await request.form()
        words = str(form.get("words", "")).replace(",", " ").split()
        try:
            _apply_dictionary_change(
                registry, lambda dictionary: dictionary.add_synonyms(words)
            )
        except ValueError as error:
            return _dictionary_page(registry, authorized=True, error=str(error))
        return RedirectResponse("/dictionary", status_code=303)

    async def synonyms_remove(request: Request):
        denied = _write_guard(request)
        if denied is not None:
            return denied

        form = await request.form()
        words = str(form.get("words", "")).replace(",", " ").split()
        try:
            def remove(dictionary):
                if not dictionary.remove_synonyms(words):
                    raise _DictionaryGroupNotFound

            _apply_dictionary_change(registry, remove)
        except _DictionaryGroupNotFound:
            return _dictionary_page(
                registry,
                authorized=True,
                error="Такой группы нет. Встроенные группы отсюда не снимаются: "
                      "они в коде и меняются вместе с поставкой.",
            )
        return RedirectResponse("/dictionary", status_code=303)

    async def queries_form(request: Request) -> HTMLResponse:
        names = registry.snapshot().configuration_names
        return _queries_page(registry, config=names[0] if names else "")

    async def queries_run(request: Request) -> HTMLResponse:
        form = await request.form()
        config = str(form.get("config", ""))
        scope = str(form.get("scope", "objects"))
        if scope not in SCOPES:
            scope = "objects"
        phrases_text = str(form.get("phrases", ""))
        phrases = [line.strip() for line in phrases_text.splitlines() if line.strip()]

        if not phrases:
            return _queries_page(
                registry, config=config, scope=scope,
                error="Не указано ни одной фразы.",
            )
        if len(phrases) > MAX_QUERY_PHRASES:
            return _queries_page(
                registry, config=config, scope=scope,
                phrases_text=phrases_text,
                error=f"За один прогон принимается не более {MAX_QUERY_PHRASES} фраз.",
                status_code=422,
            )
        if any(len(phrase) > MAX_QUERY_CHARS for phrase in phrases):
            return _queries_page(
                registry, config=config, scope=scope,
                phrases_text=phrases_text,
                error=(
                    "Каждая поисковая фраза должна содержать не более "
                    f"{MAX_QUERY_CHARS} символов."
                ),
                status_code=422,
            )
        try:
            results = _run_queries(registry, config or None, scope, phrases)
        except RegistryError as error:
            return _queries_page(
                registry, config=config, scope=scope,
                phrases_text=phrases_text, error=str(error),
            )
        except ValueError as error:
            return _queries_page(
                registry, config=config, scope=scope,
                phrases_text=phrases_text, error=str(error), status_code=422,
            )
        return _queries_page(
            registry, config=config, scope=scope,
            phrases_text=phrases_text, results=results,
        )

    async def object_card(request: Request) -> HTMLResponse:
        params = request.query_params
        return _card_page(
            registry,
            "object",
            params.get("config", ""),
            params.get("name", ""),
            params.get("detail", "fields"),
            raw=params.get("raw") == "1",
        )

    async def graph_page(request: Request) -> HTMLResponse:
        params = request.query_params
        try:
            предел = int(params.get("limit") or 0)
        except ValueError:
            предел = 0
        return _graph_page(
            registry,
            params.get("config", ""),
            params.get("name", "").strip(),
            limit=max(1, min(предел, 400)) if предел else DEFAULT_GRAPH_LIMIT,
        )

    async def syntax_card(request: Request) -> HTMLResponse:
        params = request.query_params
        return _card_page(
            registry,
            "syntax",
            params.get("config", ""),
            params.get("name", ""),
            params.get("detail", "fields"),
            raw=params.get("raw") == "1",
        )

    async def login_form(request: Request) -> HTMLResponse:
        """Страница входа. Открыта всегда — иначе войти неоткуда."""
        return _layout("Вход", _login_form())

    async def login(request: Request):
        if not _admin_token() and not _api_token():
            return PlainTextResponse(
                "Вход выключен: не задан ни ADMIN_TOKEN, ни API_TOKEN.", 404
            )
        form = await request.form()
        given = str(form.get("token", ""))
        # Админский токен проверяется первым: он же годится и для чтения, и
        # порядок решает, какой уровень получит сессия при совпадающих
        # значениях переменных.
        if same_token(given, _admin_token()):
            level = LEVEL_ADMIN
        elif same_token(given, _api_token()):
            level = LEVEL_READ
        else:
            page = _layout("Вход", "<div class=error>Неверный токен.</div>" + _login_form())
            return HTMLResponse(page.body, status_code=403)

        session = secrets.token_urlsafe(32)
        _SESSIONS[session] = level
        response = RedirectResponse("/sources" if level == LEVEL_ADMIN else "/", 303)
        response.set_cookie(
            COOKIE,
            session,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    async def logout(request: Request):
        denied = _csrf_denied(request)
        if denied is not None:
            return denied
        _SESSIONS.pop(request.cookies.get(COOKIE, ""), None)
        response = RedirectResponse("/sources", status_code=303)
        response.delete_cookie(
            COOKIE,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    async def upload(request: Request):
        if not _admin_token():
            return PlainTextResponse("Загрузка выключена: не задан ADMIN_TOKEN.", 404)
        if not _authorized(request):
            return await render_sources(
                error="Нужен вход администратора.", status_code=403
            )
        denied = _csrf_denied(request)
        if denied is not None:
            return denied

        try:
            form = await _limited_upload_form(request)
        except _UploadTooLarge:
            return await render_sources(
                error=f"Файл больше {MAX_UPLOAD // 1024 // 1024} МБ.",
                authorized=True,
                status_code=413,
            )
        except MultiPartException:
            return await render_sources(
                error=(
                    "Некорректная multipart-форма: разрешены один файл "
                    "`file` и флаг `allow_truncated`."
                ),
                authorized=True,
                status_code=400,
            )

        uploaded = form.get("file")
        allow_truncated = str(form.get("allow_truncated", "")) == "1"
        if not isinstance(uploaded, UploadFile) or not uploaded.filename:
            await form.close()
            return await render_sources(
                error="Файл не выбран.", authorized=True, status_code=400
            )

        # Имя приходит от клиента: берём только последний сегмент, иначе из
        # «../../etc/passwd» сложится путь наружу временного каталога.
        name = Path(uploaded.filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in (".zip", ".hbk", ".json", ".sqlite3"):
            await form.close()
            return await render_sources(
                error="Принимаются только .zip, .hbk, .json и .sqlite3",
                authorized=True,
            )

        reference_upload = suffix == ".sqlite3"
        if reference_upload and not reference.managed_upload_available:
            await form.close()
            return await render_sources(
                error="Загрузка SQLite выключена: база подключена по внешнему пути.",
                authorized=True,
                status_code=409,
            )

        # Registry-файл остаётся фоновой задаче. SQLite ставится синхронно,
        # поэтому её staging лежит рядом с целевым файлом для атомарной замены
        # и удаляется до ответа.
        if reference_upload:
            reference.managed_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.mkdtemp(
                dir=reference.managed_path.parent,
                prefix=".reference-upload-",
            )
        else:
            tmp = tempfile.mkdtemp()
        target = Path(tmp) / name
        job = None if reference_upload else _start_job(name, 0)
        limit = MAX_REFERENCE_DB_BYTES if reference_upload else MAX_UPLOAD
        size = 0
        try:
            with target.open("wb") as out:
                while True:
                    chunk = await uploaded.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if job is not None:
                        job["size"] = size
                    if size > limit:
                        raise _UploadTooLarge
                    out.write(chunk)
                if reference_upload:
                    out.flush()
                    os.fsync(out.fileno())
        except _UploadTooLarge:
            shutil.rmtree(tmp, ignore_errors=True)
            if job is not None:
                _JOBS.remove(job)
            await form.close()
            return await render_sources(
                error=f"Файл больше {limit // 1024 // 1024} МБ.",
                authorized=True,
                status_code=413,
            )
        await form.close()

        if reference_upload:
            try:
                await run_in_threadpool(reference.install_candidate, target)
            except ReferenceValidationError as error:
                return await render_sources(
                    error=str(error), authorized=True, status_code=422
                )
            except OSError:
                return await render_sources(
                    error="Не удалось сохранить каноническую базу.",
                    authorized=True,
                    status_code=500,
                )
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return RedirectResponse("/sources", status_code=303)

        # Разбор уходит в фон, ответ отдаётся сразу. Справка разбирается около
        # пяти секунд, и всё это время браузер стоял на белом экране, не
        # показывая, идёт работа или всё зависло. Ошибку теперь возвращать
        # некуда — она ложится в задание и видна на этой же странице.
        задача = asyncio.create_task(
            run_in_threadpool(
                _run_job,
                registry,
                job,
                tmp,
                target,
                suffix,
                allow_truncated=allow_truncated,
            )
        )
        _ФОНОВЫЕ.add(задача)
        задача.add_done_callback(_ФОНОВЫЕ.discard)
        return RedirectResponse("/sources", status_code=303)

    async def parse_incoming(request: Request):
        """Разобрать выгрузку из `incoming/`. Запись — значит `ADMIN_TOKEN`."""
        if not _admin_token():
            return PlainTextResponse("Разбор выключен: не задан ADMIN_TOKEN.", 404)
        if not _authorized(request):
            return await render_sources(
                error="Нужен вход администратора.", status_code=403
            )
        denied = _csrf_denied(request)
        if denied is not None:
            return denied

        from . import intake
        from .incoming import SETTLE_SECONDS

        form = await request.form()
        имя = Path(str(form.get("name", ""))).name
        сканер = _scanner(registry)
        архив = registry.incoming_dir / имя
        if not имя or not архив.is_file():
            return RedirectResponse("/sources", status_code=303)
        занятые = сканер.running
        if занятые:
            # Два разбора одновременно видели бы одно и то же свободное место
            # и оба прошли бы проверку. Молчаливый редирект выглядел бы как
            # «нажал, и ничего не произошло», поэтому причина ложится в журнал.
            занятость = _start_job(имя, архив.stat().st_size)
            занятость["state"] = JOB_FAILED
            занятость["error"] = (
                "уже идёт разбор другой выгрузки ("
                + ", ".join(sorted(занятые))
                + ") — одновременно разбирается не больше одной; "
                "повторите, когда та закончится"
            )
            return RedirectResponse("/sources", status_code=303)
        if сканер.дописывается(архив):
            # Признак «файл ещё копируется» из постановки (§2): `cp` полутора
            # гигабайт идёт минуты, а файл виден с первой секунды. Разбор
            # недокопированного архива даёт `BadZipFile` и запись неудачи,
            # которую потом надо снимать руками.
            копируется = _start_job(имя, архив.stat().st_size)
            копируется["state"] = JOB_FAILED
            копируется["error"] = (
                f"{имя}: файл изменялся только что — похоже, копирование ещё "
                f"идёт. Повторите через {int(SETTLE_SECONDS)} с после того, "
                "как оно закончится."
            )
            # `note_failure` здесь не зовём намеренно: запись неудачи привязана
            # к хешу, а считать sha256 растущего файла — ровно то, чего этот
            # признак и позволяет не делать.
            return RedirectResponse("/sources", status_code=303)

        job = _start_job(имя, архив.stat().st_size)
        try:
            нужно, _формат = intake.planned_size(архив)
        except Exception as error:
            # Битый архив (не zip, обрезан, нечитаем) валит расчёт размера до
            # фоновой задачи. Без этой ветки задание висело бы в «принимается»
            # навсегда — `_start_job` вычищает только завершённые записи.
            job["state"] = JOB_FAILED
            job["error"] = f"{архив.name}: не похоже на zip-архив ({error})"
            # `note_failure` считает sha256 файла, чтобы привязать отказ к
            # содержимому: на архиве в 1,4 ГБ это секунды, и в цикле событий
            # они остановили бы весь процесс — ровно то, ради чего сканирование
            # уводили в поток. Промах кэша достижим: человек положил
            # исправленный архив и жмёт кнопку со старой страницы.
            await run_in_threadpool(сканер.note_failure, архив, job["error"])
            return RedirectResponse("/sources", status_code=303)
        # `planned_size` выше проверяет только целостность центрального
        # каталога для немедленного ответа формы. Авторитетная проверка места
        # живёт внутри `Registry.add_modules`: там уже выбраны конфигурация,
        # вид источника и точный корень, зарезервировано поколение, поэтому
        # прямой вызов реестра и фоновый путь не могут её обойти.
        del нужно

        # Конфигурацию выбирает человек в форме рядом с кнопкой (поле не
        # обязательно — пустое отдаёт решение `_configuration_for`). Форму
        # присылает человек, и подставить туда можно что угодно; имя уходит
        # в путь на диске (`_modules_root`), поэтому проверяем членство в
        # уже загруженных ДО того, как задание уйдёт в фон — здесь же, где
        # и остальные синхронные отказы этого обработчика.
        конфигурация = str(form.get("configuration", "")).strip()
        if (
            конфигурация
            and конфигурация not in registry.snapshot().configurations
        ):
            job["state"] = JOB_FAILED
            job["error"] = (
                f"конфигурации «{конфигурация}» нет в реестре — выберите "
                "загруженную в форме рядом с кнопкой."
            )
            await run_in_threadpool(сканер.note_failure, архив, job["error"])
            return RedirectResponse("/sources", status_code=303)

        запущен, занятые = сканер.try_start(имя)
        if not запущен:
            job["state"] = JOB_FAILED
            job["error"] = (
                "уже идёт разбор другой выгрузки ("
                + ", ".join(занятые)
                + ") — одновременно разбирается не больше одной; "
                "повторите, когда та закончится"
            )
            return RedirectResponse("/sources", status_code=303)
        задача = asyncio.create_task(
            run_in_threadpool(
                _run_incoming, registry, сканер, job, архив, конфигурация or None
            )
        )
        _ФОНОВЫЕ.add(задача)
        задача.add_done_callback(_ФОНОВЫЕ.discard)
        return RedirectResponse("/sources", status_code=303)

    async def clear_jobs(request: Request):
        """Убрать из журнала завершённые записи.

        Незавершённые не трогаем: фоновая задача ещё пишет в своё задание, и
        выдёргивать его у неё из-под рук нечестно — состояние просто исчезло бы
        с экрана, а работа продолжилась.
        """
        if not _admin_token():
            return PlainTextResponse("Недоступно: не задан ADMIN_TOKEN.", 404)
        if not _authorized(request):
            return await render_sources(
                error="Нужен вход администратора.", status_code=403
            )
        denied = _csrf_denied(request)
        if denied is not None:
            return denied

        for job in [j for j in _JOBS if j["state"] in (JOB_DONE, JOB_FAILED)]:
            _JOBS.remove(job)
        return RedirectResponse("/sources", status_code=303)

    async def forget(request: Request):
        """Удалить файл, который не заявлен ни одним источником."""
        if not _admin_token():
            return PlainTextResponse("Удаление выключено: не задан ADMIN_TOKEN.", 404)
        if not _authorized(request):
            return await render_sources(
                error="Нужен вход администратора.", status_code=403
            )
        denied = _csrf_denied(request)
        if denied is not None:
            return denied

        form = await request.form()
        given = str(form.get("path", ""))
        # Путь приходит от клиента: сверяем не строку, а разрешённый список.
        # Из «../../etc/passwd» иначе сложилась бы дорога наружу каталога.
        orphan_sources = await run_in_threadpool(registry.orphan_sources)
        allowed = {
            path.relative_to(registry.data_dir).as_posix(): path
            for path, _ in orphan_sources
        }
        target = allowed.get(given)
        if target is None:
            return await render_sources(
                error="Такого неиспользуемого файла нет.",
                authorized=True,
            )
        try:
            await run_in_threadpool(target.unlink)
        except OSError as error:
            return await render_sources(error=str(error), authorized=True)
        return RedirectResponse("/sources", status_code=303)

    async def remove(request: Request):
        if not _admin_token():
            return PlainTextResponse("Удаление выключено: не задан ADMIN_TOKEN.", 404)
        if not _authorized(request):
            return await render_sources(
                error="Нужен вход администратора.", status_code=403
            )
        denied = _csrf_denied(request)
        if denied is not None:
            return denied

        form = await request.form()
        try:
            # У источника модулей снятие уносит каталог с кодом — 11 072 файла
            # на живой конфигурации. В цикле событий это остановило бы все
            # страницы и `/health`; словарной операцией `remove` быть перестал.
            await run_in_threadpool(registry.remove, str(form.get("id", "")))
        except RegistryError as error:
            return await render_sources(error=str(error), authorized=True)
        await run_in_threadpool(registry.save)
        return RedirectResponse("/sources", status_code=303)

    return [
        # Чтение закрыто там, где показываются сведения о конфигурациях.
        # Ручки записи проверяют право сами и отвечают 403 — им обёртка не
        # нужна, а 401 вместо 403 сбивал бы с толку: там дело не в чтении.
        Route("/", guard_read(overview), methods=["GET"]),
        Route("/sources", guard_read(sources), methods=["GET"]),
        Route("/sources", upload, methods=["POST"]),
        Route("/sources/remove", remove, methods=["POST"]),
        Route("/sources/forget", forget, methods=["POST"]),
        Route("/sources/jobs/clear", clear_jobs, methods=["POST"]),
        Route("/sources/incoming/parse", parse_incoming, methods=["POST"]),
        Route("/queries", guard_read(queries_form), methods=["GET"]),
        Route("/queries", guard_read(queries_run), methods=["POST"]),
        Route("/object", guard_read(object_card), methods=["GET"]),
        Route("/graph", guard_read(graph_page), methods=["GET"]),
        Route("/syntax", guard_read(syntax_card), methods=["GET"]),
        Route("/dictionary", guard_read(dictionary_page), methods=["GET"]),
        Route("/dictionary/alias", alias_add, methods=["POST"]),
        Route("/dictionary/alias/remove", alias_remove, methods=["POST"]),
        Route("/dictionary/synonyms", synonyms_add, methods=["POST"]),
        Route("/dictionary/synonyms/remove", synonyms_remove, methods=["POST"]),
        # Вход открыт всегда — иначе войти неоткуда.
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
    ]
