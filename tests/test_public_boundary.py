"""Публичные файлы не зависят от локальных рабочих материалов."""

from __future__ import annotations

import re
import subprocess
from posixpath import normpath
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
TASK_REFERENCE = (
    r"(?:\b(?:в|после|до|с)\s+)?задач[а-яё]*\s+(?:№\s*)?"
    r"(?:[0-9]+(?:\s*[-–—]\s*[0-9]+)?|"
    r"[a-z][a-z0-9]*[-_][a-z0-9_-]+)|"
    r"\bta" r"sk[0-9]+\b|"
    r"\bta" r"sk(?:\s+|[-_]+)(?:[0-9]+|[a-z][a-z0-9]*[-_][a-z0-9_-]+)\b|"
    r"\bотложенн[а-яё]*\s+задач[а-яё]*\b|"
    r"\bзадач[а-яё]*\s+отложен[а-яё]*\b"
)
PROCESS_LABEL = (
    r"\bре-?" r"ре" r"вью\b|\bре" r"вью\s+задач[а-яё]*\b|"
    r"\bнаходк[аи]\s+ре" r"вью\b|\bfix\s+rou" r"nd\b|"
    r"\brou" r"nd\s+[0-9]+\b|\bмелоч[ьи]\s+[0-9]+\b|"
    r"\bкандидат[а-яё]*\s+ра" r"унда\b"
)
FORBIDDEN = re.compile(
    r"AGENTS\s*" r"\.md|CLAUDE\s*" r"\.md|TASK" r"BOARD(?:\s*\.md)?|"
    r"доск[а-яё]*\s+" r"задач|бри" r"ф[а-яё]*|координа" r"тор[а-яё]*|"
    + TASK_REFERENCE
    + r"|отчёт(?:е|ом|у|а)?\s+" r"задач[а-яё]*|"
    r"market-" r"review|modules-and-" r"extensions|"
    r"(?:^|[/_.-])hand" r"off(?=$|[/_.-])|"
    r"(?:^|[/_.-])super" r"powers(?=$|[/_.-])|"
    r"(?<!к)лю" r"ч_|лок_(?:Скуп" r"ка|Про" r"бы)|"
    r"\bмир(?:а|у|е|ом)?\s*музык(?:а|и|е|у|ой)\b|"
    + PROCESS_LABEL,
    re.IGNORECASE,
)
MARKDOWN_NOISE = re.compile(
    r"`|<!--|-->|^[ \t]*(?:#{1,6}|//+|[*>\-]+)[ \t]*", re.MULTILINE
)
KNOWN_COMPACT = re.compile(
    r"agents\.md|claude\.md|task" r"board(?:\.md)?|"
    r"modules-" r"and-" r"extensions|market-" r"review",
    re.IGNORECASE,
)
KNOWN_COMPACT_CONTEXT = re.compile(
    r"(?:^|[/_.-])hand" r"off(?=$|[/_.-])|"
    r"(?:^|[/_.-])super" r"powers(?=$|[/_.-])",
    re.IGNORECASE,
)
RFC_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
COMMONMARK_ESCAPABLE = frozenset(r'''!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~''')
PUBLIC_DESIGNS = (
    "docs/schema-v1.md",
    "docs/data-sources.md",
    "docs/query-language-design.md",
    "docs/dashboard-design.md",
    "docs/modules-intake-design.md",
    "docs/modules-provider-design.md",
)


def _tracked_names(root: Path = ROOT) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return {
        raw_name.decode("utf-8")
        for raw_name in result.stdout.split(b"\0")
        if raw_name
    }


def _tracked_texts() -> list[tuple[str, str]]:
    names = _tracked_names()
    # До первого коммита новый тест ещё не входит в `git ls-files`, но обязан
    # сразу проверять и собственный публичный текст.
    names.add("tests/test_public_boundary.py")
    texts: list[tuple[str, str]] = []
    for name in sorted(names):
        if name == ".gitignore":
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        texts.append((name, text))
    return texts


def _searchable_text(text: str) -> str:
    """Убирает только Markdown-шум, сохраняя длину и номера строк."""
    return MARKDOWN_NOISE.sub(lambda match: " " * len(match.group()), text)


def _forbidden_offsets(text: str) -> list[int]:
    searchable = _searchable_text(text)
    offsets = {match.start() for match in FORBIDDEN.finditer(searchable)}

    compact_chars = []
    compact_offsets = []
    for offset, char in enumerate(searchable):
        if char.isspace():
            continue
        compact_chars.append(char)
        compact_offsets.append(offset)
    compact = "".join(compact_chars)
    offsets.update(
        compact_offsets[match.start()]
        for match in KNOWN_COMPACT.finditer(compact)
    )
    offsets.update(
        compact_offsets[match.start()]
        for match in KNOWN_COMPACT_CONTEXT.finditer(compact)
    )
    return sorted(offsets)


def _relative_markdown_targets(text: str, source: str) -> list[str]:
    raw_targets: list[str] = []

    for match in re.finditer(r"!?\[[^\]\n]*\]\(", text):
        target = _markdown_destination(text, match.end())
        if target is not None:
            raw_targets.append(target)

    definitions: dict[str, str] = {}
    definition_order: list[str] = []
    for match in re.finditer(r"(?m)^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(.*)$", text):
        label = " ".join(match.group(1).split()).casefold()
        target = _markdown_destination(match.group(2), 0, line_end=True)
        if target is not None:
            definitions[label] = target
            definition_order.append(label)

    used: set[str] = set()
    for match in re.finditer(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]", text):
        label = match.group(2) or match.group(1)
        used.add(" ".join(label.split()).casefold())
    for match in re.finditer(r"(?<![!\]])\[([^\]\n]+)\](?![\[(])", text):
        if text[match.end() : match.end() + 1] == ":":
            continue
        label = " ".join(match.group(1).split()).casefold()
        if label in definitions:
            used.add(label)
    raw_targets.extend(
        definitions[label] for label in definition_order if label in used
    )

    targets = []
    source_parent = Path(source).parent.as_posix()
    for raw in raw_targets:
        target = _strip_fragment_query(raw)
        if not target or target.startswith("/") or RFC_URI.match(target):
            continue
        target = _commonmark_unescape(target)
        targets.append(normpath(f"{source_parent}/{target}"))
    return targets


def _strip_fragment_query(target: str) -> str:
    position = 0
    while position < len(target):
        if target[position] == "\\" and position + 1 < len(target):
            position += 2
            continue
        if target[position] in "#?":
            return target[:position]
        position += 1
    return target


def _commonmark_unescape(target: str) -> str:
    result = []
    position = 0
    while position < len(target):
        if (
            target[position] == "\\"
            and position + 1 < len(target)
            and target[position + 1] in COMMONMARK_ESCAPABLE
        ):
            result.append(target[position + 1])
            position += 2
            continue
        result.append(target[position])
        position += 1
    return "".join(result)


def _markdown_destination(
    text: str, start: int, *, line_end: bool = False
) -> str | None:
    """Читает одну цель CommonMark, не путая скобки пути с концом ссылки."""
    position = start
    while position < len(text) and text[position] in " \t":
        position += 1
    if position >= len(text):
        return None

    if text[position] == "<":
        end = text.find(">", position + 1)
        if end < 0 or "\n" in text[position + 1 : end]:
            return None
        return text[position + 1 : end]

    depth = 0
    end = position
    while end < len(text):
        char = text[end]
        if char == "\\" and end + 1 < len(text):
            end += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char in " \t" and depth == 0:
            break
        elif char == "\n":
            break
        end += 1

    if end == position or depth != 0:
        return None
    if not line_end and end >= len(text):
        return None
    return text[position:end]


def _untracked_markdown_targets(
    text: str, source: str, tracked: set[str]
) -> list[str]:
    return [
        target
        for target in _relative_markdown_targets(text, source)
        if target not in tracked
    ]


def test_отслеживаемые_тексты_не_ссылаются_на_локальные_материалы():
    findings = []
    for name, text in _tracked_texts():
        for offset in _forbidden_offsets(text):
            line_number = text.count("\n", 0, offset) + 1
            findings.append(f"{name}:{line_number}")

    assert findings == [], "\n".join(findings)


def test_публичные_дизайны_перечислены_в_readme_и_contributing():
    tracked = _tracked_names()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    for path in PUBLIC_DESIGNS:
        assert path in tracked
        assert f"]({path})" in readme
        assert f"]({path})" in contributing


def test_относительные_markdown_ссылки_ведут_в_отслеживаемые_файлы():
    tracked = _tracked_names()
    findings = []
    for name, text in _tracked_texts():
        if not name.endswith(".md"):
            continue
        for target in _untracked_markdown_targets(text, name, tracked):
            findings.append(f"{name} -> {target}")
    assert findings == [], "\n".join(findings)


def test_существующий_но_неотслеживаемый_файл_не_считается_публичным(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    ignored = tmp_path / "ignored.md"
    ignored.write_text("локальный текст", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)

    assert ignored.is_file()
    assert "ignored.md" not in _tracked_names(tmp_path)
    assert _untracked_markdown_targets(
        "[скрыто](ignored.md)", "README.md", tracked=_tracked_names(tmp_path)
    ) == ["ignored.md"]


def test_дизайн_дашборда_описывает_текущую_реализацию():
    text = (ROOT / "docs/dashboard-design.md").read_text(encoding="utf-8")

    assert "Дашборд реализован" in text
    assert "фоновые задания и снимки /sources" in text
    assert "POST /sources/incoming/parse" in text
    assert "загрузка синхронная" not in text
    assert "dashboard.py   НОВЫЙ" not in text
    assert "registry.py    без изменений" not in text


def test_дизайн_языка_запросов_не_выдаёт_реализованное_за_план():
    text = (ROOT / "docs/query-language-design.md").read_text(encoding="utf-8")

    assert "Статус: реализовано" in text
    assert "Реализовано и проверено тестами" in text
    assert "Подтверждено и **требует работы**" not in text


@pytest.mark.parametrize(
    "example",
    [
        "в зада" + "че 6 описан переход",
        "зада" + "ча №6 описана внутри",
        "После задач " + "1-4 доступен источник",
        "задача " + "modules-binding",
        "с задачи " + "query-ranking",
        "ta" + "sk 12",
        "ta" + "sk-12",
        "ta" + "sk12",
        "ta" + "sk modules-binding",
        "отложенная " + "задача",
        "доску\n# " + "задач",
        "задача `" + "modules-binding`",
        "задача <!--" + " --> 6",
        "AGENTS\n# " + ".md",
        "CLAUDE\n// " + ".md",
        "TASK" + "BOARD\n* " + ".md",
        "AGENTS\n- " + ".md",
        "CLAUDE\n> " + ".md",
        "AG\n// ENTS\n> " + ".md",
        "TASK\n* BOARD\n- " + ".md",
        "modules-\n> and-\n> " + "extensions",
        "market-\n// " + "review",
        "docs/foo-" + "handoff.md",
        "modules-provider-" + "handoff",
        "docs/" + "handoff" + "/index.md",
        ".super" + "powers/",
        "docs/" + "superpowers" + "/index.md",
        "docs/modules-provider-\n# " + "handoff.md",
        "/x/.super\n# " + "powers/file",
        "ре-" + "ре" + "вью подтвердило гонку",
        "ре" + "вью задачи подтвердило гонку",
        "находка ре" + "вью",
        "fix rou" + "nd",
        "rou" + "nd 2",
        "кандидат ра" + "унда",
        "мелочь " + "3",
        "Документ.лок_" + "Скупка",
        "Мир " + "Музыки",
        "в Мире " + "музыки",
        "из Мира " + "музыки",
        "к Миру\n# " + "музыки",
        "Миром" + "Музыки",
    ],
)
def test_запрещённые_примеры_распознаются(example: str):
    assert _forbidden_offsets(example)


@pytest.mark.parametrize(
    "example",
    [
        "Claude Code подключается по streamable HTTP",
        "Вид метаданных Задача отображается как объект",
        "Класс Tasks переводится в Задача",
        "лок_ОбъектА — нейтральный синтетический идентификатор",
        "Изменение прошло независимое ревью.",
        "Критично сохранять каталог data при обновлении.",
        "Round — функция языка запросов.",
        "taskbar-item — CSS-компонент интерфейса.",
        "TaskManager-worker — технический идентификатор.",
        "taskmodules-binding — слитный продуктовый идентификатор.",
        "задача modules — обычное описание без внутреннего идентификатора.",
        "task modules — обычная английская фраза.",
        "Tasks metadata",
        "Round",
        "The server can hand " + "off work safely.",
        "Agents have no super " + "powers.",
        "The server can hand\n# " + "off work safely.",
        "Agents have no super\n# " + "powers.",
        "Музыка объединяет мир.",
        "В мире много хорошей музыки.",
        "История мировой музыки.",
    ],
)
def test_публичные_термины_не_дают_ложного_срабатывания(example: str):
    assert _forbidden_offsets(example) == []


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ('[пример](docs/a(1).md "заголовок")', ["docs/a(1).md"]),
        ('[пример](<docs/a(1).md> "заголовок")', ["docs/a(1).md"]),
        ("[пример](docs/a(1).md#часть?x=1)", ["docs/a(1).md"]),
        ("[пример][ref]\n\n[ref]: docs/a(1).md 'заголовок'", ["docs/a(1).md"]),
        (r"[пример](docs/a\(1\).md)", ["docs/a(1).md"]),
        (r"[пример](docs/a\#1.md)", ["docs/a#1.md"]),
    ],
)
def test_markdown_цели_поддерживают_commonmark_формы(markdown: str, expected: list[str]):
    assert _relative_markdown_targets(markdown, "README.md") == expected
    assert _untracked_markdown_targets(
        markdown, "README.md", tracked=set(expected)
    ) == []


def test_ignored_reference_style_ссылка_отвергается(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("локальный текст", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)

    markdown = "[скрыто][internal]\n\n[internal]: ignored.md 'локальный'"

    assert _untracked_markdown_targets(
        markdown, "README.md", tracked=_tracked_names(tmp_path)
    ) == ["ignored.md"]


@pytest.mark.parametrize(
    "target",
    [
        "ftp://example.test/file.md",
        "ssh://example.test/file.md",
        "urn:isbn:0000000000",
        "custom+v1:resource",
    ],
)
def test_markdown_uri_схемы_не_считаются_путями_репозитория(target: str):
    assert _relative_markdown_targets(f"[внешняя]({target})", "README.md") == []


def test_источник_b_описывает_доступную_структуру_форм():
    text = (ROOT / "docs/data-sources.md").read_text(encoding="utf-8")

    assert "тексты модулей и доступная структура `Form.xml`" in text
    assert "элементы и события форм" in text
    assert "Команды, макеты и недостающие в schema v1 данные форм" in text


def test_текущие_агрегаты_провайдера_отделены_от_исторических():
    expected = ("137 116", "619 029", "3 194", "89 528", "24 202")
    for relative in ("docs/architecture.md", "docs/modules-provider-design.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        start = text.index("Текущий производственный срез")
        end = text.index("Исторический снимок прототипа", start)
        current = text[start:end]

        assert all(value in current for value in expected)
        assert "2026-08-21" in current
        assert "Исторический снимок прототипа" in text

    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    start = architecture.index("Текущий производственный срез")
    end = architecture.index("Исторический снимок прототипа", start)
    assert (
        '.venv/bin/python tools/lab/measure_modules_cache.py "$MODULES_ROOT"'
        in architecture[start:end]
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "](docs/architecture.md)" in readme

    design = (ROOT / "docs/modules-provider-design.md").read_text(encoding="utf-8")
    assert "## 1. Цена — исторический замер прототипа" in design


def test_дизайн_не_выдаёт_готовые_возможности_за_будущие():
    text = (ROOT / "docs/modules-provider-design.md").read_text(encoding="utf-8")

    assert "показывается `get_procedure`" in text
    assert "будущей карточкой процедуры" not in text
    assert "Деградация ответов во время сборки" not in text


def test_readme_фиксирует_три_docker_режима_и_проверку_прав():
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "docker-compose.classic.yml" in text
    assert "docker-compose.dashboard.yml" in text
    assert "docker-compose.remote.yml" in text
    assert "create_host_path: false" in text
    assert "10001:10001" in text
    assert "chmod -R 777" in text
