"""Каталог `incoming/`: что там лежит и в каком оно состоянии.

Хранится кэш хеша (считать sha256 гигабайта на каждый показ страницы нельзя)
и причина неудачи прежнего частичного разбора. SHA активного поколения
позволяет пометить тот же архив как «загружено»; остальные файлы показываются
как «не разобрано». Загрузку выгрузки ведёт intake v2.

Новых неудач сюда никто не пишет: частичная загрузка Source B отключена, а
её писатель удалён. Записанные раньше отказы по-прежнему показываются, пока
файл лежит в `incoming/` и его содержимое не сменилось.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from . import fsio
from .registry import Registry

STATE_NEW = "не разобрано"
STATE_FAILED = "разбор не удался"
STATE_LOADED = "загружено"

# Файл считается дописанным, если `mtime` старше этого возраста. `cp` полутора
# гигабайт идёт минуты, и файл виден с первой секунды. Сверяется только `mtime`:
# растущий файл меняет и его, а лишняя сверка размера потребовала бы хранить
# ещё один снимок между показами страницы.
SETTLE_SECONDS = 5.0

# Что показываем про файл, который ещё копируется. Своего состояния у него
# нет намеренно: «не разобрано» — правда, просто с
# оговоркой, почему кнопки пока нет.
SETTLING_DETAIL = "файл ещё копируется, разбор недоступен"
UNSTABLE_DETAIL = (
    "файл изменился или недоступен во время проверки — повторите обновление"
)


def _sha256_файла(путь: Path) -> str:
    digest = hashlib.sha256()
    with путь.open("rb") as поток:
        for блок in iter(lambda: поток.read(1 << 20), b""):
            digest.update(блок)
    return digest.hexdigest()


def _file_identity(info) -> dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        # ctime ловит правку на месте с восстановленным mtime. Старые записи
        # без поля не совпадают и пересчитываются один раз.
        "ctime_ns": info.st_ctime_ns,
    }


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
        # Остаток атомарной записи, оборванной гибелью процесса.
        fsio.remove_temporary_records(self._state_path)
        self._state = self._load()
        # Словарь общий у потоков: показы страницы (`digest`, уборка
        # исчезнувших) идут каждый своим потоком из `run_in_threadpool`, и
        # `json.dumps` бежит поверх того же словаря. Без замка «dictionary
        # changed size during iteration» уронил бы показ страницы, а `_save`
        # ловит только `OSError`.
        self._замок = threading.RLock()
        # Проверка прежних неудач считает хеши изменившихся файлов. Каждый
        # `digest()` раньше сразу писал состояние — два fsync под замком.
        # Внутри `scan()` запись откладывается до конца прохода, причём только
        # в его собственном потоке: `digest()` вне прохода пишет сразу.
        self._пачка = threading.local()

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
        if getattr(self._пачка, "активна", False):
            self._пачка.изменено = True
            return
        try:
            # Запись идёт под тем же замком, что и сборка снимка. Иначе два
            # сохранения из разных потоков пула успевают разойтись между
            # `dumps` и `write_text`, и на диск ложится более старый снимок:
            # кэш хеша переживёт потерю, а записанный отказ — нет.
            #
            # Замена атомарная: `write_text` поверх файла при обрыве оставлял
            # обрезанный JSON, и `_load` молча начинал с пустого состояния —
            # терялся именно записанный отказ, ради которого файл и существует.
            with self._замок:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                fsio.write_record_atomic(
                    self._state_path,
                    json.dumps(self._state, ensure_ascii=False).encode("utf-8"),
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
        """sha256 с кэшем по имени и `(dev, ino, size, mtime_ns, ctime_ns)`."""
        отпечаток = путь.stat()
        identity = _file_identity(отпечаток)
        ключ = путь.name
        with self._замок:
            запись = self._state["digests"].get(ключ)
            sha = запись.get("sha256") if isinstance(запись, dict) else None
            if (
                isinstance(запись, dict)
                and all(запись.get(field) == value for field, value in identity.items())
                and isinstance(sha, str)
                and len(sha) == 64
                and all(char in "0123456789abcdef" for char in sha)
            ):
                return sha
        значение = _sha256_файла(путь)
        if _file_identity(путь.stat()) != identity:
            raise OSError("Входящий файл изменился во время расчёта SHA-256")
        with self._замок:
            self._state["digests"][ключ] = {
                **identity,
                "mtime": отпечаток.st_mtime,
                "sha256": значение,
            }
        self._save()
        return значение

    def _забыть_исчезнувшие(self, каталог: Path) -> None:
        """Убрать кэш хеша и отказ файлов, которых в `incoming/` больше нет.

        Ключ — имя файла, и без уборки каждый когда-либо положенный архив
        оставался в `incoming-state.json` навсегда. Наличие проверяется по
        диску в момент уборки, а не по списку начала обхода: отказ файла,
        положенного во время показа, не теряется. Вернувшийся под тем же
        именем файл всё равно сверяется заново: хеш — по stat4,
        отказ — по хешу.
        """
        with self._замок:
            изменено = False
            for раздел in ("digests", "failures"):
                for имя in list(self._state[раздел]):
                    try:
                        есть = (каталог / имя).exists()
                    except (OSError, ValueError):
                        есть = True
                    if not есть:
                        del self._state[раздел][имя]
                        изменено = True
        if изменено:
            self._save()

    def scan(self, *, active_hashes: frozenset[str] | None = None) -> list[dict]:
        self._пачка.активна = True
        self._пачка.изменено = False
        try:
            return self._scan(active_hashes)
        finally:
            self._пачка.активна = False
            if self._пачка.изменено:
                self._save()

    def _scan(self, active_hashes: frozenset[str] | None = None) -> list[dict]:
        каталог = self.registry.incoming_dir
        строки: list[dict] = []
        if not каталог.is_dir():
            return строки
        загруженные = self._активные_хеши() if active_hashes is None else active_hashes
        for путь in sorted(каталог.glob("*.zip")):
            try:
                отпечаток = путь.stat()
                # Каталог с расширением `.zip` — не входящая выгрузка. Раньше
                # его отсекала ошибка чтения при подсчёте хеша, но у свежего
                # каталога хеш не считается вовсе, и он попал бы в список.
                if not путь.is_file():
                    continue
                дописывается = self._дописывается(отпечаток)
                if дописывается:
                    # Хеш не считаем вовсе: он всё равно устареет к концу `cp`,
                    # а стоит секунды на каждом показе страницы.
                    состояние, подробность = STATE_NEW, SETTLING_DETAIL
                else:
                    try:
                        состояние, подробность = self._состояние(путь, загруженные)
                    except OSError:
                        # Хеш не досчитан: файл меняется или временно не
                        # читается. Строка остаётся с объяснением, а не
                        # исчезает до следующего обновления.
                        состояние, подробность = STATE_NEW, UNSTABLE_DETAIL
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
        self._забыть_исчезнувшие(каталог)
        return строки

    def _активные_хеши(self) -> frozenset[str]:
        return frozenset(
            generation.manifest.raw_sha256
            for generation in self.registry.snapshot().generations.values()
        )

    def _состояние(
        self, путь: Path, загруженные: frozenset[str] | None = None,
    ) -> tuple[str, str]:
        """Состояние дописанного файла по активным поколениям и прежним отказам."""
        if загруженные is None:
            загруженные = self._активные_хеши()
        with self._замок:
            запись = self._state["failures"].get(путь.name)
        if запись is None and not загруженные:
            return STATE_NEW, ""
        хеш = self.digest(путь)
        if хеш in загруженные:
            return STATE_LOADED, ""
        причина = _причина_неудачи(запись, хеш)
        if причина is not None:
            return STATE_FAILED, причина
        return STATE_NEW, ""
