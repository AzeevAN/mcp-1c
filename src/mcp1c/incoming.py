"""Каталог `incoming/`: что там лежит и в каком оно состоянии.

Состояние вычисляется, а не хранится: пара `(kind, sha256)` в реестре плюс
версия правила отбора. Хранится ровно две вещи — кэш хеша (считать sha256
гигабайта на каждый показ страницы нельзя) и причина последней неудачи.

Неудача живёт здесь, а не в `_JOBS`: тот — список в памяти процесса, он
теряется при рестарте и вытесняется после десяти заданий, и через рестарт
отказ становился бы неотличим от «ещё не разбирали».
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .intake import SELECTION_VERSION
from .registry import KIND_MODULES, Registry

STATE_READY = "разобрано"
STATE_RUNNING = "разбирается"
STATE_NEW = "не разобрано"
STATE_FAILED = "разбор не удался"
STATE_UPDATED = "обновлённая выгрузка"
STATE_STALE = "отбор устарел"

# Файл считается дописанным, если размер и mtime не менялись столько секунд.
# `cp` полутора гигабайт идёт минуты, и файл виден с первой секунды.
SETTLE_SECONDS = 5.0


def _sha256_файла(путь: Path) -> str:
    digest = hashlib.sha256()
    with путь.open("rb") as поток:
        for блок in iter(lambda: поток.read(1 << 20), b""):
            digest.update(блок)
    return digest.hexdigest()


class IncomingScanner:
    """Состояние файлов `incoming/`. Одно на реестр, состояние на диске."""

    def __init__(self, registry: Registry):
        self.registry = registry
        self._state_path = registry.data_dir / "incoming-state.json"
        self._state = self._load()
        self.running: set[str] = set()

    def _load(self) -> dict:
        try:
            состояние = json.loads(self._state_path.read_text(encoding="utf-8"))
            # Валидируем форму: верхний уровень — словарь, и в нём есть ключи.
            if (
                isinstance(состояние, dict)
                and isinstance(состояние.get("digests"), dict)
                and isinstance(состояние.get("failures"), dict)
            ):
                return состояние
        except (OSError, ValueError):
            pass
        return {"digests": {}, "failures": {}}

    def _save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._state, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            # Кэш расходный: если записать не смогли (том read-only, нет места),
            # молча деградируем. В следующий раз пересчитаем.
            pass

    def digest(self, путь: Path) -> str:
        """sha256 с кэшем по `(путь, размер, mtime)`."""
        отпечаток = путь.stat()
        ключ = путь.name
        запись = self._state["digests"].get(ключ)
        если_то_же = (
            запись
            and запись["size"] == отпечаток.st_size
            and запись["mtime"] == отпечаток.st_mtime
        )
        if если_то_же:
            return запись["sha256"]
        значение = _sha256_файла(путь)
        self._state["digests"][ключ] = {
            "size": отпечаток.st_size,
            "mtime": отпечаток.st_mtime,
            "sha256": значение,
        }
        self._save()
        return значение

    def note_failure(self, путь: Path, причина: str) -> None:
        self._state["failures"][путь.name] = причина
        self._save()

    def clear_failure(self, путь: Path) -> None:
        self._state["failures"].pop(путь.name, None)
        self._save()

    def scan(self) -> list[dict]:
        каталог = self.registry.incoming_dir
        строки: list[dict] = []
        if not каталог.is_dir():
            return строки
        источники = [
            s for s in self.registry.sources.values() if s.kind == KIND_MODULES
        ]
        по_хешу = {s.sha256: s for s in источники}
        по_имени = {s.origin: s for s in источники}
        for путь in sorted(каталог.glob("*.zip")):
            try:
                хеш = self.digest(путь)
                источник = по_хешу.get(хеш)
                if путь.name in self.running:
                    состояние, подробность = STATE_RUNNING, ""
                elif источник is not None:
                    устарел = getattr(источник, "selection_version", SELECTION_VERSION)
                    состояние = STATE_STALE if устарел < SELECTION_VERSION else STATE_READY
                    подробность = источник.id
                elif путь.name in self._state["failures"]:
                    состояние = STATE_FAILED
                    подробность = self._state["failures"][путь.name]
                elif путь.name in по_имени:
                    состояние, подробность = STATE_UPDATED, по_имени[путь.name].id
                else:
                    состояние, подробность = STATE_NEW, ""
                строки.append(
                    {
                        "name": путь.name,
                        "size": путь.stat().st_size,
                        "state": состояние,
                        "detail": подробность,
                    }
                )
            except OSError:
                # Файл исчез между glob и stat, или это каталог вместо файла.
                # Одна строка не имеет права уносить всю страницу — пропускаем.
                pass
        return строки
