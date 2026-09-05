"""Разрешённые корни контейнера и устойчивые привязки конфигураций.

Входные каталоги принадлежат оператору: здесь записывается только малый
реестр в data, а чтение самой выгрузки остаётся за безопасным virtual tree.
"""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path

from .intake_v2_registry import _write_atomic

CONFIG_SOURCES_ROOT = Path("/config-sources")
_ROOT_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}\Z")


class ConfigSourceError(RuntimeError):
    """Корень или сохранённая привязка недоступны."""


def source_path(root: Path, source_id: str) -> Path:
    if not isinstance(source_id, str) or not _ROOT_ID.fullmatch(source_id):
        raise ConfigSourceError("Недопустимый идентификатор каталога.")
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise ConfigSourceError("Корень /config-sources недоступен или является ссылкой.")
        path = root / source_id
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ConfigSourceError("Источник должен быть каталогом, а не ссылкой.")
        return path
    except OSError as error:
        raise ConfigSourceError("Подключённый каталог недоступен.") from error


class ConfigSourceBindings:
    def __init__(self, path: Path, root: Path):
        self.path = path
        self.root = root

    def roots(self) -> list[str]:
        # Отсутствие дополнительных mounts — штатный режим без этой функции.
        if not self.root.exists():
            return []
        try:
            if not stat.S_ISDIR(self.root.lstat().st_mode):
                raise ConfigSourceError("Корень /config-sources не может быть ссылкой.")
            result = []
            for entry in self.root.iterdir():
                if _ROOT_ID.fullmatch(entry.name) and stat.S_ISDIR(entry.lstat().st_mode):
                    result.append(entry.name)
            return sorted(result)
        except OSError as error:
            raise ConfigSourceError("Список подключённых каталогов недоступен.") from error

    def load(self) -> dict[str, str]:
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise ConfigSourceError("Реестр привязок превышает допустимый размер.")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise ConfigSourceError("Не удалось прочитать реестр привязок.") from error
        if (
            not isinstance(raw, dict) or raw.get("version") != 1
            or not isinstance(raw.get("bindings"), dict)
            or any(not isinstance(k, str) or not k or not isinstance(v, str)
                   or not _ROOT_ID.fullmatch(v) for k, v in raw["bindings"].items())
        ):
            raise ConfigSourceError("Повреждён формат реестра привязок.")
        return raw["bindings"]

    def save(self, bindings: dict[str, str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps(
                {"version": 1, "bindings": bindings}, ensure_ascii=False, sort_keys=True,
            ).encode("utf-8"))
        except OSError as error:
            raise ConfigSourceError("Не удалось сохранить привязки каталогов.") from error
