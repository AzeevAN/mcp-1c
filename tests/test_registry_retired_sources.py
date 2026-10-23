"""Снятые виды строк registry.json и остатки приёма кода до 4.2.0."""

import itertools
import json

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import (
    _RETIRED_SOURCE_KINDS,
    KIND_EXTENSION,
    KIND_MODULES,
    STATUS_READY,
    Registry,
    Source,
)
from native_export import записать_старый_код

ИМЯ = "Розница"
РАСШИРЕНИЕ = "Расш"
ИД_СНИМКА = f"{ИМЯ}:extension-runtime"
ИД_КОДА = f"{ИМЯ}:modules"
ИД_РАСШИРЕНИЯ = f"{ИМЯ}:ext:{РАСШИРЕНИЕ}"


def _реестр_с_конфигурацией(tmp_path) -> Registry:
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(входящее, build_configuration(name=ИМЯ)))
    registry.save()
    return registry


def _дописать_снимок(registry: Registry):
    """Строка и файл сеансового снимка, как их оставляла прежняя версия."""
    файл = registry.data_dir / "sources" / f"{ИМЯ}.extension-runtime.json"
    файл.parent.mkdir(parents=True, exist_ok=True)
    файл.write_text('{"extensions": []}', encoding="utf-8")
    source = Source(
        id=ИД_СНИМКА,
        kind="extension-runtime",
        origin="снимок.json",
        sha256="a" * 64,
        loaded_at="2026-10-01T00:00:00+00:00",
        status=STATUS_READY,
        stored_path=registry._relative(файл),
    )
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    payload["sources"].append(source.to_dict())
    registry.registry_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return файл


def _идентификаторы(registry: Registry) -> list[str]:
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    return [row["id"] for row in payload["sources"]]


def _строки_про(сообщения: list[str], идентификатор: str) -> list[str]:
    return [m for m in сообщения if m.startswith(f"{идентификатор}: ")]


@pytest.fixture
def старый_код(tmp_path, корень_кода):
    """Реестр с Source A и тремя снятыми строками плюс их файлы на диске."""
    registry = _реестр_с_конфигурацией(tmp_path)
    записать_старый_код(registry, корень_кода, configuration=ИМЯ)
    записать_старый_код(
        registry, корень_кода, configuration=ИМЯ, extension=РАСШИРЕНИЕ
    )
    снимок = _дописать_снимок(registry)
    return registry, снимок


@pytest.mark.parametrize(
    "идентификатор, вид",
    [
        (ИД_КОДА, KIND_MODULES),
        (ИД_РАСШИРЕНИЯ, KIND_EXTENSION),
        (ИД_СНИМКА, "extension-runtime"),
    ],
    ids=["modules", "extension", "extension-runtime"],
)
def test_restore_снимает_строку_с_одним_сообщением(старый_код, идентификатор, вид):
    registry, _снимок = старый_код

    restored = Registry(registry.data_dir)
    problems = restored.restore()

    assert _строки_про(problems, идентификатор) == [
        f"{идентификатор}: {_RETIRED_SOURCE_KINDS[вид]}"
    ]
    assert идентификатор not in restored.snapshot().sources


def test_restore_даёт_ровно_по_одному_сообщению_на_строку(старый_код):
    registry, _снимок = старый_код

    problems = Registry(registry.data_dir).restore()

    assert sorted(m.split(": ", 1)[0] for m in problems) == sorted(
        [ИД_КОДА, ИД_РАСШИРЕНИЯ, ИД_СНИМКА]
    )


def test_restore_оставляет_конфигурацию_читаемой(старый_код):
    registry, _снимок = старый_код

    restored = Registry(registry.data_dir)
    restored.restore()

    context = restored.resolve(ИМЯ)
    assert context.name == ИМЯ
    assert context.modules is None


def test_restore_не_удаляет_файлы_на_диске(старый_код):
    registry, снимок = старый_код
    до = sorted(p.relative_to(registry.data_dir) for p in registry.data_dir.rglob("*"))

    Registry(registry.data_dir).restore()

    после = sorted(
        p.relative_to(registry.data_dir) for p in registry.data_dir.rglob("*")
    )
    assert после == до
    assert снимок.read_text(encoding="utf-8") == '{"extensions": []}'
    assert (registry.data_dir / "modules" / ИМЯ).is_dir()
    assert (registry.data_dir / "extensions" / ИМЯ / РАСШИРЕНИЕ).is_dir()


def test_startup_убирает_снятые_строки_из_registry_json_и_не_трогает_файлы(
    старый_код,
):
    registry, снимок = старый_код
    assert len(_идентификаторы(registry)) == 4

    restored = Registry(registry.data_dir)
    messages = restored.startup()

    assert _идентификаторы(restored) == [ИМЯ]
    for идентификатор in (ИД_КОДА, ИД_РАСШИРЕНИЯ, ИД_СНИМКА):
        assert len(_строки_про(messages, идентификатор)) == 1
    assert снимок.is_file()
    assert (registry.data_dir / "modules" / ИМЯ).is_dir()
    assert (registry.data_dir / "extensions" / ИМЯ / РАСШИРЕНИЕ).is_dir()
    assert restored.resolve(ИМЯ).modules is None


def test_повторный_startup_не_повторяет_сообщений_о_снятых_строках(старый_код):
    registry, _снимок = старый_код
    Registry(registry.data_dir).startup()

    messages = Registry(registry.data_dir).startup()

    for идентификатор in (ИД_КОДА, ИД_РАСШИРЕНИЯ, ИД_СНИМКА):
        assert _строки_про(messages, идентификатор) == []


def test_cli_reg_add_не_принимает_json_снимка(tmp_path, capsys):
    from mcp1c.cli import main

    снимок = tmp_path / "снимок.json"
    снимок.write_text('{"extensions": []}', encoding="utf-8")
    data = tmp_path / "data"

    code = main(["reg-add", str(снимок), "--data", str(data)])

    captured = capsys.readouterr()
    assert code != 0
    assert "добавлено" not in captured.out
    assert captured.err.strip()
    registry_json = data / "registry.json"
    if registry_json.exists():
        assert ":extension-runtime" not in registry_json.read_text(encoding="utf-8")
    assert not list((data / "sources").glob("*.json"))


# --- Подсказка про остатки прежнего приёма кода ---------------------------

_ИМЕНА = ("modules", "extensions", ".modules-swap.json")
_КОМБИНАЦИИ = [
    имена
    for размер in range(1, 4)
    for имена in itertools.combinations(_ИМЕНА, размер)
]


def _создать_остатки(data_dir, имена, *, пустые=()):
    data_dir.mkdir(parents=True, exist_ok=True)
    for имя in имена:
        if имя == ".modules-swap.json":
            (data_dir / имя).write_text("{}", encoding="utf-8")
            continue
        (data_dir / имя).mkdir()
        if имя not in пустые:
            (data_dir / имя / "маркер.txt").write_text("x", encoding="utf-8")


def _подсказки(messages: list[str]) -> list[str]:
    return [m for m in messages if "приёма кода до версии 4.2.0" in m]


def _текст_подсказки(имена) -> str:
    return (
        "в каталоге данных остались файлы приёма кода до версии 4.2.0 ("
        + ", ".join(имена)
        + "); сервер их не использует, удалите вручную"
    )


@pytest.mark.parametrize(
    "имена", _КОМБИНАЦИИ, ids=["+".join(к) for к in _КОМБИНАЦИИ]
)
def test_startup_перечисляет_присутствующие_остатки_по_порядку(tmp_path, имена):
    data = tmp_path / "data"
    _создать_остатки(data, имена)

    messages = Registry(data).startup()

    assert _подсказки(messages) == [_текст_подсказки(имена)]
    for имя in имена:
        assert (data / имя).exists()
    for имя in set(имена) & {"modules", "extensions"}:
        assert (data / имя / "маркер.txt").read_text(encoding="utf-8") == "x"


def test_startup_без_остатков_не_пишет_подсказку(tmp_path):
    data = tmp_path / "data"
    data.mkdir()

    assert _подсказки(Registry(data).startup()) == []


@pytest.mark.parametrize(
    "каталоги",
    [("modules",), ("extensions",), ("modules", "extensions")],
    ids=["modules", "extensions", "оба"],
)
def test_startup_молча_убирает_пустые_каталоги(tmp_path, каталоги):
    data = tmp_path / "data"
    _создать_остатки(data, каталоги, пустые=каталоги)

    messages = Registry(data).startup()

    assert _подсказки(messages) == []
    for имя in каталоги:
        assert not (data / имя).exists()


def test_пустой_каталог_не_попадает_в_подсказку_рядом_с_непустым(tmp_path):
    data = tmp_path / "data"
    _создать_остатки(data, ("modules", "extensions"), пустые=("modules",))

    messages = Registry(data).startup()

    assert _подсказки(messages) == [_текст_подсказки(("extensions",))]
    assert not (data / "modules").exists()
    assert (data / "extensions" / "маркер.txt").is_file()


def test_пустой_каталог_и_файл_обмена_называется_только_файл(tmp_path):
    data = tmp_path / "data"
    _создать_остатки(
        data, ("extensions", ".modules-swap.json"), пустые=("extensions",)
    )

    messages = Registry(data).startup()

    assert _подсказки(messages) == [_текст_подсказки((".modules-swap.json",))]
    assert not (data / "extensions").exists()
    assert (data / ".modules-swap.json").is_file()


@pytest.mark.parametrize(
    "содержимое",
    ["{}", "{ битый json", "", "[1, 2", "\x00\x01"],
    ids=["пустой-объект", "битый", "пустой-файл", "обрыв", "мусор"],
)
def test_файл_обмена_кода_не_делает_реестр_только_для_чтения(tmp_path, содержимое):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".modules-swap.json").write_text(содержимое, encoding="utf-8")
    registry = Registry(data)

    messages = registry.startup()
    registry.save()

    assert _подсказки(messages) == [_текст_подсказки((".modules-swap.json",))]
    assert not any("только для чтения" in m for m in messages)
    assert registry.registry_path.is_file()
    assert (data / ".modules-swap.json").read_text(encoding="utf-8") == содержимое


# --- Журналы покрытия снятых строк ------------------------------------------


def _положить_журнал(data_dir, идентификатор: str):
    from mcp1c import coverage_log

    путь = coverage_log.log_path(data_dir, идентификатор)
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("{}", encoding="utf-8")
    return путь


@pytest.mark.parametrize(
    "идентификатор", [ИД_КОДА, ИД_РАСШИРЕНИЯ], ids=["modules", "extension"]
)
def test_restore_удаляет_журнал_покрытия_снятой_строки(старый_код, идентификатор):
    registry, _снимок = старый_код
    журнал = _положить_журнал(registry.data_dir, идентификатор)
    чужой = _положить_журнал(registry.data_dir, "Другая:modules")

    Registry(registry.data_dir).restore()

    assert not журнал.exists()
    assert чужой.exists()


def test_restore_не_трогает_журналы_для_extension_runtime(старый_код):
    registry, _снимок = старый_код
    журнал = _положить_журнал(registry.data_dir, ИД_СНИМКА)

    Registry(registry.data_dir).restore()

    assert журнал.exists()


def test_журнал_native_корпуса_с_тем_же_id_сохраняется(tmp_path, корень_кода):
    from mcp1c import source_ids
    from native_export import опубликовать_код

    рабочий = tmp_path / "работа"
    registry = Registry(рабочий / "data")
    опубликовать_код(
        registry,
        корень_кода,
        configuration=build_configuration(name=ИМЯ),
        рабочий=рабочий,
    )
    registry.save()
    записать_старый_код(registry, корень_кода, configuration=ИМЯ)
    журнал = _положить_журнал(registry.data_dir, ИД_КОДА)

    restored = Registry(registry.data_dir)
    problems = restored.restore()

    # Предпосылка теста: native-корпус поднят, и снятая строка тоже видна.
    assert source_ids.modules_id(ИМЯ) == ИД_КОДА
    assert ИД_КОДА in restored.modules
    assert len(_строки_про(problems, ИД_КОДА)) == 1
    assert журнал.exists()


def test_гость_не_удаляет_журнал_снятой_строки(tmp_path, корень_кода):
    from test_registry_interprocess import _start_server, _stop

    registry = _реестр_с_конфигурацией(tmp_path)
    data = registry.data_dir
    server = _start_server(data)
    try:
        # Строка и журнал появляются уже после старта владельца: сам он
        # убрал бы их при своём restore.
        записать_старый_код(registry, корень_кода, configuration=ИМЯ)
        журнал = _положить_журнал(data, ИД_КОДА)
        guest = Registry(data)

        problems = guest.startup()

        assert guest.role == "guest"
        assert len(_строки_про(problems, ИД_КОДА)) == 1
        assert журнал.exists()
    finally:
        _stop(server)


# --- Временные остатки прежних версий ----------------------------------------


def test_startup_молча_убирает_временные_остатки_кода(tmp_path):
    data = tmp_path / "data"
    кэш = data / "index" / "cache"
    кэш.mkdir(parents=True)
    файл = data / ".modules-swap.json.123.tmp"
    файл.write_text("{}", encoding="utf-8")
    каталог = кэш / ".modules-cache.tmp-abc"
    каталог.mkdir()
    (каталог / "x.bin").write_bytes(b"x")
    чужой_файл = data / "other.json.123.tmp"
    чужой_файл.write_text("{}", encoding="utf-8")

    messages = Registry(data).startup()

    assert not файл.exists()
    assert not каталог.exists()
    assert чужой_файл.exists()
    assert _подсказки(messages) == []


def test_startup_не_идёт_по_симлинку_временного_каталога(tmp_path):
    data = tmp_path / "data"
    кэш = data / "index" / "cache"
    кэш.mkdir(parents=True)
    цель = tmp_path / "чужой"
    цель.mkdir()
    (цель / "важное.txt").write_text("x", encoding="utf-8")
    ссылка = кэш / ".modules-cache.tmp-link"
    ссылка.symlink_to(цель, target_is_directory=True)

    Registry(data).startup()

    assert (цель / "важное.txt").read_text(encoding="utf-8") == "x"
    assert ссылка.is_symlink()
