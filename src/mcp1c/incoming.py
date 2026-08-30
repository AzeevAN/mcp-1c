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

from . import intake
from .intake import SELECTION_VERSION
from .registry import KIND_EXTENSION, KIND_MODULES, Registry

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


def _входящие_пути(каталог: Path) -> list[Path]:
    """ZIP-файлы и непосредственные подкаталоги `incoming/`, без симлинков."""
    пути: list[Path] = []
    try:
        дети = list(каталог.iterdir())
    except OSError:
        return пути
    for путь in дети:
        try:
            if путь.is_symlink():
                continue
            имя = путь.name
            if путь.is_file() and имя.lower().endswith(".zip"):
                пути.append(путь)
            elif (
                путь.is_dir()
                and not имя.startswith(".")
                and not имя.lower().endswith(".zip")
            ):
                пути.append(путь)
        except OSError:
            continue
    return sorted(пути, key=lambda путь: путь.name)


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
        self._running: set[str] = set()

    @property
    def running(self) -> frozenset[str]:
        """Неизменяемый снимок имён, которые сейчас разбираются."""
        with self._замок:
            return frozenset(self._running)

    def try_start(self, name: str) -> tuple[bool, tuple[str, ...]]:
        """Атомарно занять единственный слот разбора incoming."""
        with self._замок:
            busy = tuple(sorted(self._running))
            if busy:
                return False, busy
            self._running.add(name)
            return True, ()

    def finish(self, name: str) -> None:
        with self._замок:
            self._running.discard(name)

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

        Для каталога смотрим mtime файлов идентичности: ConfigDumpInfo.xml
        или Configuration.xml и, если есть, VERSION gitSync.
        """
        if путь.is_dir():
            файлы = intake.identity_files(путь)
            if not файлы:
                return False
            return any(self._дописывается(файл.stat()) for файл in файлы)
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
        """sha256 с кэшем: ZIP — по размеру и mtime файла; каталог — по
        отпечатку файлов идентичности."""
        ключ = путь.name
        if путь.is_dir():
            отпечаток = intake.identity_fingerprint(путь)
            with self._замок:
                запись = self._state["digests"].get(ключ)
                кэш = запись.get("identity") if isinstance(запись, dict) else None
                if кэш is not None:
                    сохранённый = tuple(
                        tuple(часть) for часть in кэш if isinstance(часть, list)
                    )
                    if сохранённый == отпечаток:
                        return запись["sha256"]
            значение = intake.identity_digest(путь)
            with self._замок:
                self._state["digests"][ключ] = {
                    "size": 0,
                    "mtime": 0,
                    "identity": [list(часть) for часть in отпечаток],
                    "sha256": значение,
                }
            self._save()
            return значение
        отпечаток = путь.stat()
        with self._замок:
            запись = self._state["digests"].get(ключ)
            если_то_же = (
                запись
                and запись["size"] == отпечаток.st_size
                and запись["mtime"] == отпечаток.st_mtime
                and "identity" not in запись
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
        snapshot = self.registry.snapshot()
        источники = [
            s
            for s in snapshot.sources.values()
            # Расширение — тот же вид выгрузки в файлы, что и модули
            # конфигурации, просто в свой каталог и под ключом `:ext:<Имя>`.
            # Без учёта этого вида разобранное расширение навсегда
            # показывалось бы «не разобрано»: сверка идёт по sha256, а
            # источники этого вида сюда просто не попадали бы.
            if s.kind in (KIND_MODULES, KIND_EXTENSION)
        ]
        по_хешу = {s.sha256: s for s in источники}
        по_имени = {s.origin: s for s in источники}
        running = self.running
        for путь in _входящие_пути(каталог):
            try:
                kind = "directory" if путь.is_dir() else "archive"
                if kind == "directory":
                    размер = intake.listing_size(путь)
                    if not intake.identity_files(путь):
                        строки.append(
                            {
                                "name": путь.name,
                                "size": размер,
                                "state": STATE_FAILED,
                                "detail": intake.нет_идентичности(путь.name),
                                "settling": False,
                                "kind": kind,
                            }
                        )
                        continue
                    дописывается = self.дописывается(путь)
                else:
                    отпечаток = путь.stat()
                    размер = отпечаток.st_size
                    дописывается = self._дописывается(отпечаток)
                if путь.name in running:
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
                        "size": размер,
                        "state": состояние,
                        "detail": подробность,
                        "settling": дописывается,
                        "kind": kind,
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
            # `selection_version` читается напрямую, без запасного значения:
            # раньше поля у `Source` не было вовсе, и `getattr(..., SELECTION_
            # VERSION)` всегда подставлял текущую версию — STATE_STALE не
            # достигался ни при каких данных. Ноль (значение по умолчанию у
            # поля и то, что ставит `from_dict` для записи без него) обязан
            # считаться устаревшим, а не свежим: запись без известной версии
            # отбора — это ровно та запись, для которой человек должен увидеть
            # «переразобрать», а не «разобрано».
            состояние = (
                STATE_STALE
                if источник.selection_version < SELECTION_VERSION
                else STATE_READY
            )
            return состояние, источник.id
        with self._замок:
            запись = self._state["failures"].get(путь.name)
        причина = _причина_неудачи(запись, хеш) if запись is not None else None
        if причина is not None:
            return STATE_FAILED, причина
        if путь.name in по_имени:
            return STATE_UPDATED, по_имени[путь.name].id
        return STATE_NEW, ""
