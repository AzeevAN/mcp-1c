"""Короткоживущий билет на скачивание одного снимка модуля."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from datetime import datetime, timezone
from urllib.parse import urlsplit


DOWNLOAD_PATH = "/api/v1/modules/source"
TICKET_HEADER = "X-Mcp1c-Download-Ticket"
# Клиент скачивает файл сразу после ответа инструмента; минуты хватает с
# запасом, а билет, попавший в транскрипт или лог, быстро теряет цену.
TICKET_LIFETIME_SECONDS = 60
MAX_TICKET_LENGTH = 8192
# Сжатие модуля до 16 МиБ стоит заметного CPU, а одноразовый билет заставляет
# агента запрашивать новую ссылку на тот же снимок. Кэш держит готовый gzip,
# но не больше 64 МиБ и 64 модулей: это память процесса, а не хранилище.
COMPRESSED_CACHE_MAX_BYTES = 64 * 1024 * 1024
COMPRESSED_CACHE_MAX_ENTRIES = 64


class DownloadTicketError(ValueError):
    """Билет отсутствует, повреждён или истёк."""


def public_base_url(value: str, *, https_required: bool = False) -> str:
    """Явный origin, без доверия к клиентскому Host и proxy-заголовкам."""
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MCP1C_PUBLIC_BASE_URL должен быть HTTP(S) origin без пути и параметров.")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("MCP1C_PUBLIC_BASE_URL содержит неверный порт.") from error
    if not (parsed.netloc and (port is None or 1 <= port <= 65535)):
        raise ValueError("MCP1C_PUBLIC_BASE_URL содержит неверный адрес.")
    if https_required and parsed.scheme != "https":
        raise ValueError("Для MCP1C_ACCESS=https-proxy нужен HTTPS-адрес скачивания.")
    return value.strip().rstrip("/")


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )


class CompressedModuleCache:
    """Ограниченный LRU готовых gzip-файлов по точному снимку модуля."""

    def __init__(
        self,
        *,
        max_bytes: int = COMPRESSED_CACHE_MAX_BYTES,
        max_entries: int = COMPRESSED_CACHE_MAX_ENTRIES,
    ):
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._items: OrderedDict[Hashable, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: Hashable, value: bytes) -> None:
        if len(value) > self._max_bytes:
            return
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._size -= len(previous)
            self._items[key] = value
            self._size += len(value)
            while self._size > self._max_bytes or len(self._items) > self._max_entries:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)


class ModuleDownloadTickets:
    """Билеты ограничены одним модулем, одним скачиванием и этим процессом."""

    def __init__(self, *, lifetime_seconds: int = TICKET_LIFETIME_SECONDS):
        self._key = secrets.token_bytes(32)
        self._lifetime_seconds = lifetime_seconds
        # jti использованных билетов до их истечения: подпись сама по себе
        # не мешает предъявить тот же билет повторно.
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    def issue(self, claims: dict[str, object]) -> tuple[str, str]:
        expires = int(time.time()) + self._lifetime_seconds
        payload = {"v": 1, "exp": expires, "jti": secrets.token_urlsafe(16), **claims}
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        signature = hmac.new(self._key, raw, hashlib.sha256).digest()
        token = _encode(raw) + "." + _encode(signature)
        if len(token) > MAX_TICKET_LENGTH:
            raise DownloadTicketError("Адрес модуля слишком длинный для билета.")
        until = datetime.fromtimestamp(expires, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        return token, until

    def verify(self, token: str) -> dict[str, object]:
        if not token or len(token) > MAX_TICKET_LENGTH or token.count(".") != 1:
            raise DownloadTicketError("Билет скачивания недействителен.")
        try:
            encoded_payload, encoded_signature = token.split(".")
            raw = _decode(encoded_payload)
            signature = _decode(encoded_signature)
            expected = hmac.new(self._key, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise DownloadTicketError("Билет скачивания недействителен.")
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise DownloadTicketError("Билет скачивания недействителен.") from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {
                "v", "exp", "jti", "address", "config", "extension", "revision",
                "sha256", "size_bytes",
            }
            or payload["v"] != 1
            or type(payload["exp"]) is not int
            or type(payload["size_bytes"]) is not int
            or payload["size_bytes"] < 0
            or not all(
                isinstance(payload[key], str) and payload[key]
                for key in ("jti", "address", "config", "revision", "sha256")
            )
            or (payload["extension"] is not None and not isinstance(payload["extension"], str))
        ):
            raise DownloadTicketError("Билет скачивания недействителен.")
        now = int(time.time())
        if now >= payload["exp"]:
            raise DownloadTicketError("Срок билета скачивания истёк.")
        with self._lock:
            for jti, expires in list(self._used.items()):
                if now >= expires:
                    del self._used[jti]
            if payload["jti"] in self._used:
                raise DownloadTicketError("Билет скачивания уже использован.")
            self._used[payload["jti"]] = payload["exp"]
        return payload
