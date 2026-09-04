"""Общий backend современного дашборда без HTML-представления.

Здесь живут единые правила авторизации, загрузки, фоновых заданий, поиска
и подготовки данных. HTTP API и SPA-маршруты определены в
``dashboard_runtime.py``.
"""

from __future__ import annotations
import asyncio
import os
import re
import shutil
import traceback
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from . import tools
from .auth import same_token
from .intake_v2 import CandidateTransport
from .loader import ExportError
from .registry import (
    KIND_EXTENSION,
    KIND_EXTENSION_RUNTIME,
    KIND_MODULES,
    Registry,
    RegistryError,
)
from .search import FIELD_KIND_TITLES, MAX_QUERY_CHARS
from .syntax_model import KIND_TITLES
from .v8container import V8ContainerError

COOKIE = "mcp1c_session"

MAX_UPLOAD = 500 * 1024 * 1024

CHUNK = 1024 * 1024

MAX_QUERY_PHRASES = 32

MAX_UPLOAD_FIELDS = 1

MAX_UPLOAD_FIELD_SIZE = 4 * 1024

LEVEL_READ = "read"

LEVEL_ADMIN = "admin"

_SESSIONS: dict[str, str] = {}

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

JOB_READING = "принимается"

JOB_PARSING = "разбирается"

JOB_DONE = "готово"

JOB_FAILED = "ошибка"

_JOBS: list[dict] = []

_ФОНОВЫЕ: set[asyncio.Task] = set()

JOBS_KEPT = 10

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
        registry.add_configuration(
            path,
            allow_truncated=allow_truncated,
            source_transport=CandidateTransport.BROWSER,
        )
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
        hits = index.search(
            phrase, limit=5,
            predicate=(lambda doc: keep(doc.payload)) if keep is not None else None,
        )
        hidden: list = []
        if keep is not None:
            # Выборка скрытого не отнимает места у доступных результатов.
            raw = index.search(phrase, limit=20)
            hidden = [hit for hit in raw if not keep(hit.doc.payload)]
        result.append((phrase, hits, hidden[:5]))
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

def _card_text(
    registry: Registry,
    kind: str,
    config: str,
    name: str,
    detail: str,
) -> str:
    """Буквальный текст карточки для MCP и JSON API SPA."""
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

def _hidden_reason(item) -> str:
    """Человекочитаемое объяснение фильтра версии для SPA."""
    if item.until:
        return f"описан по версию {item.until} включительно, дальше его нет"
    if item.since:
        return f"появился в {item.since}"
    return "недоступен в этой версии"

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

def _apply_dictionary_change(registry: Registry, mutation):
    """Записать словарь и подхватить его без перезапуска.

    Пересборки индексов не требуется: ни синонимы, ни псевдонимы не участвуют
    в построении постингов, они читаются в момент поиска.
    """
    return registry.mutate_dictionary(mutation)
