"""Разбор справки платформы 1С (`shcntx_*.hbk`) в модель.

Путь до данных::

    .hbk  ->  контейнер 1С  ->  элемент FileStorage  ->  zip  ->  objects/**.html

Внутри — машинно сгенерированный HTML с устойчивой разметкой: разделы
размечены `<p class="V8SH_chapter">Название:</p>` (в справках до 8.3.х —
`<div>` с тем же классом), параметры —
`<div class="V8SH_rubric">`, примеры — `<pre class="V8SH_codesample">`.
Поэтому разбор идёт по этим маркерам, без сторонних HTML-библиотек.

Зависимостей нет. `7z` не нужен — см. `v8container`.
"""

from __future__ import annotations

import html
import io
import re
from pathlib import Path
from typing import Iterator

from .resource_limits import LimitedZipFile
from .syntax_model import (
    AVAILABILITY,
    KIND_EVENT,
    KIND_METHOD,
    KIND_OBJECT,
    KIND_PROPERTY,
    KIND_QUERY_FIELD,
    KIND_QUERY_TABLE,
    SyntaxIndex,
    SyntaxItem,
    SyntaxParam,
    SyntaxVariant,
)
from .v8container import V8Container, V8ContainerError

FILE_STORAGE = "FileStorage"

_RE_TAG = re.compile(r"<[^>]+>")
_RE_BR = re.compile(r"<br\s*/?>", re.I)
_RE_SPACE = re.compile(r"[ \t ]+")
_RE_LINK = re.compile(r'<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.I | re.S)

# Классы разметки одни и те же во всех версиях справки, а теги — нет: до
# какой-то версии разделы размечались `div`, позже `p`. Справка от 8.3.5
# целиком на `div`, от 8.3.27 — на `p`. Поэтому тег в выражениях не
# фиксируется: иначе на старой справке достаются имена и ничего больше —
# 18 936 элементов и ноль описаний.
_RE_PAGETITLE = re.compile(r'class="V8SH_pagetitle"[^>]*>(.*?)</h1>', re.I | re.S)
_RE_TITLE = re.compile(r'class="V8SH_title"[^>]*>(.*?)</(?:p|div)>', re.I | re.S)
_RE_HEADING = re.compile(r'class="V8SH_heading"[^>]*>(.*?)</(?:p|div)>', re.I | re.S)
_RE_CODESAMPLE = re.compile(r'class="V8SH_codesample"[^>]*>(.*?)</(?:pre|div|p)>', re.I | re.S)
_RE_VERSION = re.compile(r"начиная с версии\s*([\d.]+)")

# Разбивка тела страницы на разделы по заголовкам V8SH_chapter.
_RE_CHAPTER = re.compile(
    r'<(?:p|div) class="V8SH_chapter"[^>]*>(.*?)</(?:p|div)>', re.I | re.S
)

# «НазваниеРус (NameEng)»
_RE_BILINGUAL = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")

# «<Выражение> (обязательный)» в шапке параметра
_RE_PARAM_HEAD = re.compile(r"<([^<>]+)>\s*(?:\(([^)]*)\))?")

_CHAPTER_VARIANT = "вариант синтаксиса"
_MEMBER_CHAPTERS = {
    "методы": "methods",
    "свойства": "properties",
    "события": "events",
    "конструкторы": "constructors",
    "элементы коллекции": "collection",
}


# --------------------------------------------------------------- утилиты HTML


def _text(fragment: str) -> str:
    """HTML-фрагмент -> плоский текст."""
    fragment = _RE_BR.sub("\n", fragment)
    fragment = _RE_TAG.sub("", fragment)
    fragment = html.unescape(fragment)
    fragment = _RE_SPACE.sub(" ", fragment)
    return "\n".join(line.strip() for line in fragment.split("\n")).strip()


def _link_texts(fragment: str) -> list[str]:
    return [_text(text) for _, text in _RE_LINK.findall(fragment)]


def _link_hrefs(fragment: str) -> list[str]:
    return [href for href, _ in _RE_LINK.findall(fragment)]


def _split_bilingual(value: str) -> tuple[str, str]:
    """«ПоляСхемыЗапроса (QuerySchemaFields)» -> (рус, англ)."""
    value = _text(value)
    match = _RE_BILINGUAL.match(value)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return value, ""


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _chapters(body: str) -> list[tuple[str, str]]:
    """Разбить страницу на (заголовок раздела, содержимое до следующего)."""
    result: list[tuple[str, str]] = []
    matches = list(_RE_CHAPTER.finditer(body))
    for index, match in enumerate(matches):
        title = _text(match.group(1)).rstrip(":").strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        result.append((title, body[start:end]))
    return result


# --------------------------------------------------------------- разбор кусков


def _parse_availability(fragment: str) -> list[str]:
    text = _text(fragment)
    found: list[str] = []
    for part in re.split(r"[,.;]", text):
        key = part.strip().lower()
        canonical = AVAILABILITY.get(key)
        if canonical and canonical not in found:
            found.append(canonical)
    return found


def _parse_params(fragment: str) -> list[SyntaxParam]:
    """Параметры размечены блоками <div class="V8SH_rubric">Имя (обязательный)</div>."""
    params: list[SyntaxParam] = []
    blocks = re.split(r'<div class="V8SH_rubric"[^>]*>', fragment, flags=re.I)
    for block in blocks[1:]:
        head, _, tail = block.partition("</div>")
        head_text = _text(head)

        match = _RE_PARAM_HEAD.search(head_text)
        if match:
            name = match.group(1).strip()
            flag = (match.group(2) or "").strip().lower()
        else:
            name = head_text.split("(")[0].strip()
            flag = "обязательный" if "обязательный" in head_text.lower() else ""

        if not name:
            continue

        types: list[str] = []
        description = tail
        type_match = re.search(r"Тип:(.*?)(?:<br\s*/?>|$)", tail, re.I | re.S)
        if type_match:
            chunk = type_match.group(1)
            types = _link_texts(chunk)
            if not types:
                plain = _text(chunk).rstrip(".").strip()
                types = [t.strip() for t in plain.split(",") if t.strip()]
            description = tail[type_match.end():]

        default = ""
        default_match = re.search(r"Значение по умолчанию:(.*?)(?:<br\s*/?>|$)", tail, re.I | re.S)
        if default_match:
            default = _text(default_match.group(1)).rstrip(".").strip()

        params.append(
            SyntaxParam(
                name=name,
                required="необязательный" not in flag,
                types=types,
                description=_text(description),
                default=default,
            )
        )
    return params


def _parse_returns(fragment: str) -> tuple[list[str], str]:
    types = _link_texts(fragment)
    text = _text(fragment)
    if not types:
        types = [t.strip() for t in text.split(".", 1)[0].split(",") if t.strip()]
    return types, text


def _kind_from_path(path: str) -> str:
    if path.startswith("tables/"):
        return KIND_QUERY_FIELD if "/fields/" in path else KIND_QUERY_TABLE
    if "/methods/" in path:
        return KIND_METHOD
    if "/properties/" in path:
        return KIND_PROPERTY
    if "/events/" in path:
        return KIND_EVENT
    return KIND_OBJECT


# --------------------------------------------------------------- страница


def parse_page(path: str, raw: bytes) -> SyntaxItem | None:
    """Разобрать одну html-страницу справки."""
    body = _decode(raw)

    title_match = _RE_PAGETITLE.search(body)
    if not title_match:
        return None

    item = SyntaxItem(id=path.rsplit(".", 1)[0], kind=_kind_from_path(path))

    heading_match = _RE_HEADING.search(body)
    parent_match = _RE_TITLE.search(body)

    if heading_match:
        item.name_ru, item.name_en = _split_bilingual(heading_match.group(1))
        if parent_match:
            item.parent_ru, item.parent_en = _split_bilingual(parent_match.group(1))
    else:
        # Страница самого объекта: заголовок и есть имя.
        item.name_ru, item.name_en = _split_bilingual(title_match.group(1))

    version_match = _RE_VERSION.search(body)
    if version_match:
        item.since = version_match.group(1).rstrip(".")

    current = SyntaxVariant()
    variants: list[SyntaxVariant] = []

    for title, fragment in _chapters(body):
        key = title.lower()

        if key.startswith(_CHAPTER_VARIANT):
            if current.signature or current.params or current.description:
                variants.append(current)
            current = SyntaxVariant(title=title.split(":", 1)[-1].strip())

        elif key.startswith("синтаксис"):
            current.signature = _text(fragment)

        elif key.startswith("параметры"):
            current.params = _parse_params(fragment)

        elif key.startswith("возвращаемое значение"):
            current.returns, current.returns_description = _parse_returns(fragment)

        elif key.startswith("описание варианта"):
            current.description = _text(fragment)

        elif key.startswith("описание") or key.startswith("использование:"):
            item.description = _text(fragment)

        elif key.startswith("доступность"):
            item.availability = _parse_availability(fragment)

        elif key.startswith("пример"):
            samples = _RE_CODESAMPLE.findall(fragment)
            item.examples.extend(_text(s) for s in samples)
            if not samples:
                text = _text(fragment)
                if text:
                    item.examples.append(text)

        elif key.startswith("см. также"):
            item.see_also = [h for h in _link_hrefs(fragment) if h.startswith("v8help:")]

        elif key.startswith("примечание"):
            item.note = _text(fragment)

        elif key.startswith("значения"):
            item.values = _link_texts(fragment) or [
                line for line in _text(fragment).split("\n") if line
            ]

        else:
            member_key = _MEMBER_CHAPTERS.get(key)
            if member_key:
                item.members[member_key] = _link_texts(fragment)

    if current.signature or current.params or current.description or current.returns:
        variants.append(current)
    item.variants = variants

    if item.kind == KIND_PROPERTY:
        item.readonly = "только чтение" in body.lower()

    return item


# --------------------------------------------------------------- источник


def open_file_storage(hbk_path: str | Path) -> LimitedZipFile:
    """Достать zip с html-страницами из контейнера .hbk."""
    with V8Container(hbk_path) as container:
        if FILE_STORAGE not in container:
            raise V8ContainerError(
                f"В {hbk_path} нет элемента {FILE_STORAGE} — "
                "это не файл справки синтакс-помощника."
            )
        blob = container.read(FILE_STORAGE)
    return LimitedZipFile(io.BytesIO(blob), label="FileStorage справки")


def parse_hbk(hbk_path: str | Path, platform: str = "") -> SyntaxIndex:
    """Разобрать справку целиком."""
    index = SyntaxIndex(source=str(hbk_path))
    if platform:
        index.platforms.append(platform)

    with open_file_storage(hbk_path) as archive:
        for name in archive.namelist():
            if not name.endswith(".html"):
                continue
            item = parse_page(name, archive.read(name))
            if item is not None and item.name_ru:
                index.add(item)

    return index


def iter_pages(hbk_path: str | Path) -> Iterator[SyntaxItem]:
    """Потоковый разбор — когда весь индекс в памяти держать не нужно."""
    with open_file_storage(hbk_path) as archive:
        for name in archive.namelist():
            if not name.endswith(".html"):
                continue
            item = parse_page(name, archive.read(name))
            if item is not None and item.name_ru:
                yield item
