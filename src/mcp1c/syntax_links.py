"""Точное разрешение внутренних ссылок исходной справки платформы.

Адрес берётся из HTML той же версии. Подпись ссылки не является ключом:
одинаковые подписи встречаются у разных владельцев.
"""

from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlsplit


def target_page_id(source_id: str, href: str, page_ids: set[str]) -> str:
    """Вернуть ID страницы цели либо пустую строку без предположений.

    ``source_id`` — путь исходной HTML-страницы без ``.html``. Обычные
    ссылки относительны её каталогу; ``SyntaxHelperContext`` указывает путь
    от корня архива. Ресурсы ``SyntaxHelperLanguage`` не являются карточками.
    """
    parsed = urlsplit(href)
    if parsed.query or parsed.fragment:
        return ""

    if parsed.scheme:
        if parsed.scheme.lower() != "v8help" or parsed.netloc != "SyntaxHelperContext":
            return ""
        path = unquote(parsed.path).lstrip("/")
    else:
        if parsed.netloc or href.startswith("/"):
            return ""
        path = posixpath.join(posixpath.dirname(source_id), unquote(parsed.path))

    path = posixpath.normpath(path)
    if path in {"", ".", ".."} or path.startswith("../") or not path.endswith(".html"):
        return ""
    page_id = path.removesuffix(".html")
    return page_id if page_id in page_ids else ""
