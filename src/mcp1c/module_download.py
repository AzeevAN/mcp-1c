"""Короткоживущий билет на скачивание одного снимка модуля."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


DOWNLOAD_PATH = "/api/v1/modules/source"
TICKET_HEADER = "X-Mcp1c-Download-Ticket"
TICKET_LIFETIME_SECONDS = 300
MAX_TICKET_LENGTH = 8192


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


class ModuleDownloadTickets:
    """Билеты ограничены одним модулем и живут только в этом процессе."""

    def __init__(self, *, lifetime_seconds: int = TICKET_LIFETIME_SECONDS):
        self._key = secrets.token_bytes(32)
        self._lifetime_seconds = lifetime_seconds

    def issue(self, claims: dict[str, object]) -> tuple[str, str]:
        expires = int(time.time()) + self._lifetime_seconds
        payload = {"v": 1, "exp": expires, **claims}
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
                "v", "exp", "address", "config", "extension", "revision",
                "sha256", "size_bytes",
            }
            or payload["v"] != 1
            or type(payload["exp"]) is not int
            or type(payload["size_bytes"]) is not int
            or payload["size_bytes"] < 0
            or not all(
                isinstance(payload[key], str) and payload[key]
                for key in ("address", "config", "revision", "sha256")
            )
            or (payload["extension"] is not None and not isinstance(payload["extension"], str))
        ):
            raise DownloadTicketError("Билет скачивания недействителен.")
        if int(time.time()) >= payload["exp"]:
            raise DownloadTicketError("Срок билета скачивания истёк.")
        return payload
