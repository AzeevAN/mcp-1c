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
import threading
import time
from pathlib import Path

from .intake import SELECTION_VERSION
from .registry import KIND_MODULES, Registry

STATE_READY = "разобрано"
STATE_RUNNING = "разбирается"
STATE_NEW = "не разобрано"
STATE_FAILED = "разбор не удался"
STATE_UPDATED = "обновлённая выгрузка"
STATE_STALE = "отбор устарел"

# Файл считается дописанным, если `mtime` старше этого возраста. `cp` полутора
# гигабайт идёт минуты, и файл виден с первой секунды. Сверяется только `mtime`:
# растущий файл меняет и его, а лишняя сверка размера потребовала бы хранить
# ещё один снимок между показами страницы.
SETTLE_SECONDS = 5.0

# Что показываем про файл, который ещё копируется. Своего состояния у него
# нет намеренно: состояний ровно шесть, и «не разобрано» — правда, просто с
# оговоркой, почему кнопки пока нет.
SETTLING_DETAIL = "файл ещё копируется, разбор недоступен"


def _sha256_файла(путь: Path) -> str:
    digest = hashlib.sha256()
    with путь.open("rb") as поток:
        for блок in iter(lambda: поток.read(1 << 20), b""):
            digest.update(блок)
    return digest.hexdigest()


def _причина_неудачи(запись, хеш: str) -> str | None:
    """Причина, если записанная неудача относится к нынешнему содержимому.

    Неудача привязана к хешу файла: иначе исправленный архив, положенный под
    тем же именем, оставался бы в «разбор не удался» навсегда — снять отказ
    можно было бы только переименованием файла или правкой `incoming-state.json`.

    Старый формат (причина строкой, без хеша) читается как есть: файл
    расходный, но ронять показ он не имеет права ни в каком виде.
    """
    if isinstance(запись, str):
        return запись
    if not isinstance(запись, dict):
        return None
    записанный = запись.get("sha256")
    if записанный and записанный != хеш:
        return None
    причина = запись.get("reason")
    return причина if isinstance(причина, str) else ""


class IncomingScanner:
    """Состояние файлов `incoming/`. Одно на реестр, состояние на диске."""

    def __init__(self, registry: Registry):
        self.registry = registry
        self._state_path = registry.data_dir / "incoming-state.json"
        self._state = self._load()
        # Словарь общий у потоков: в него пишет разбор (`note_failure`,
        # `clear_failure`), а сканирование страницы (`digest`) идёт своим
        # потоком из `run_in_threadpool` — и `json.dumps` бежит поверх того же
        # словаря. Без замка «dictionary changed size during iteration» уронил
        # бы показ страницы, а `_save` ловит только `OSError`.
        self._замок = threading.RLock()
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
            # Запись идёт под тем же замком, что и сборка снимка. Иначе два
            # сохранения из разных потоков пула успевают разойтись между
            # `dumps` и `write_text`, и на диск ложится более старый снимок:
            # кэш хеша переживёт потерю, а записанный отказ — нет, он и
            # существует ради того, чтобы пережить рестарт.
            with self._замок:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                self._state_path.write_text(
                    json.dumps(self._state, ensure_ascii=False), encoding="utf-8"
                )
        except OSError:
            # Кэш расходный: если записать не смогли (том read-only, нет места),
            # молча деградируем. В следующий раз пересчитаем.
            pass

    def дописывается(self, путь: Path) -> bool:
        """Файл менялся только что — копирование могло не закончиться.

        Признак из постановки (§2): `cp` полутора гигабайт идёт минуты, и файл
        виден с первой секунды. Разбор такого архива даёт `BadZipFile`
        (центральный каталог лежит в конце файла) и вечную запись неудачи, а
        показ страницы — пересчёт sha256 на каждом обновлении: mtime растущего
        файла меняется, и кэш хеша не срабатывает.
        """
        return self._дописывается(путь.stat())

    @staticmethod
    def _дописывается(отпечаток) -> bool:
        возраст = time.time() - отпечаток.st_mtime
        # Метка в будущем — не признак копирования. Её ставят `cp -p`,
        # `rsync -t`, `mv` с другого тома и перекос часов контейнера, а
        # односторонняя проверка загоняла бы такой файл в вечное «копируется»:
        # ни кнопки, ни разбора, выйти через интерфейс нельзя.
        return 0 <= возраст < SETTLE_SECONDS

    def digest(self, путь: Path) -> str:
        """sha256 с кэшем по `(путь, размер, mtime)`."""
        отпечаток = путь.stat()
        ключ = путь.name
        with self._замок:
            запись = self._state["digests"].get(ключ)
            если_то_же = (
                запись
                and запись["size"] == отпечаток.st_size
                and запись["mtime"] == отпечаток.st_mtime
            )
            if если_то_же:
                return запись["sha256"]
        значение = _sha256_файла(путь)
        with self._замок:
            self._state["digests"][ключ] = {
                "size": отпечаток.st_size,
                "mtime": отпечаток.st_mtime,
                "sha256": значение,
            }
        self._save()
        return значение

    def note_failure(self, путь: Path, причина: str) -> None:
        """Запомнить отказ вместе с хешем файла, на котором он случился."""
        try:
            хеш = self.digest(путь)
        except OSError:
            # Файл мог исчезнуть, пока шёл разбор. Причина всё равно нужнее
            # хеша: без неё отказ через рестарт неотличим от «не разбирали».
            хеш = ""
        with self._замок:
            self._state["failures"][путь.name] = {"reason": причина, "sha256": хеш}
        self._save()

    def clear_failure(self, путь: Path) -> None:
        with self._замок:
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
                отпечаток = путь.stat()
                # Каталог с расширением `.zip` — не входящая выгрузка. Раньше
                # его отсекала ошибка чтения при подсчёте хеша, но у свежего
                # каталога хеш не считается вовсе, и он попал бы в список.
                if not путь.is_file():
                    continue
                дописывается = self._дописывается(отпечаток)
                if путь.name in self.running:
                    состояние, подробность = STATE_RUNNING, ""
                elif дописывается:
                    # Хеш не считаем вовсе: он всё равно устареет к концу `cp`,
                    # а стоит секунды на каждом показе страницы.
                    состояние, подробность = STATE_NEW, SETTLING_DETAIL
                else:
                    состояние, подробность = self._состояние(
                        путь, по_хешу, по_имени
                    )
                строки.append(
                    {
                        "name": путь.name,
                        "size": отпечаток.st_size,
                        "state": состояние,
                        "detail": подробность,
                        "settling": дописывается,
                    }
                )
            except OSError:
                # Файл исчез между glob и stat, или это каталог вместо файла.
                # Одна строка не имеет права уносить всю страницу — пропускаем.
                pass
        return строки

    def _состояние(self, путь: Path, по_хешу: dict, по_имени: dict) -> tuple[str, str]:
        """Состояние дописанного файла — по хешу, реестру и записи неудачи."""
        хеш = self.digest(путь)
        источник = по_хешу.get(хеш)
        if источник is not None:
            устарел = getattr(источник, "selection_version", SELECTION_VERSION)
            состояние = STATE_STALE if устарел < SELECTION_VERSION else STATE_READY
            return состояние, источник.id
        with self._замок:
            запись = self._state["failures"].get(путь.name)
        причина = _причина_неудачи(запись, хеш) if запись is not None else None
        if причина is not None:
            return STATE_FAILED, причина
        if путь.name in по_имени:
            return STATE_UPDATED, по_имени[путь.name].id
        return STATE_NEW, ""
