"""Два процесса над одним data/: работающий сервер и CLI.

Сервер имитирует дочерний процесс, который поднял Registry и держит
каталог данных. Данные синтетические, в ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import (
    Registry,
    RegistryChangedExternally,
    RegistryOwnedElsewhere,
)
from test_intake_v2_registry import _manifest

ROOT = Path(__file__).parents[1]
fcntl = pytest.importorskip("fcntl")

_SERVER = """
import sys
from mcp1c.registry import Registry
registry = Registry(sys.argv[1])
registry.startup()
print("ready", flush=True)
sys.stdin.readline()
"""


def _start_server(data: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    process = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(data)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout.readline().strip() == "ready"
    return process


def _stop(process: subprocess.Popen) -> None:
    try:
        process.stdin.write("\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cli_при_живом_сервере_не_откатывает_его_публикацию(tmp_path, capsys):
    from mcp1c.cli import main

    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(tmp_path, "generation-002", suffix="-2")
    setup = Registry(tmp_path / "data")
    setup.publish_generation(setup.stage_generation(first, first_payloads))
    data = setup.data_dir

    server = _start_server(data)
    try:
        # Публикация сервера «в середине»: staging и WAL уже на диске.
        staged = setup.stage_generation(second, second_payloads)
        old_pointer = setup.active_generation_pointer(first.identity)
        setup.generation_recovery_path.write_text(
            json.dumps(
                {
                    "previous": old_pointer.to_dict(),
                    "staged": staged.pointer.to_dict(),
                    "phase": "prepared",
                }
            ),
            encoding="utf-8",
        )
        wal_before = setup.generation_recovery_path.read_bytes()
        incoming = tmp_path / "input"
        incoming.mkdir()
        archive = write_export(incoming, build_configuration(name="Другая"))

        assert main(["reg-add", str(archive), "--data", str(data)]) == 0

        assert "POST /admin/reload" in capsys.readouterr().err
        assert setup.generation_recovery_path.read_bytes() == wal_before
        assert staged.root.exists()
        payload = json.loads((data / "registry.json").read_text(encoding="utf-8"))
        assert "Другая" in {raw["id"] for raw in payload["sources"]}
        assert payload["generation_manifests"] == [old_pointer.to_dict()]
    finally:
        _stop(server)


def test_гость_не_публикует_поколение_и_не_снимает_источники(tmp_path):
    first, first_payloads = _manifest(tmp_path, "generation-001")
    setup = Registry(tmp_path / "data")
    data = setup.data_dir
    server = _start_server(data)
    try:
        guest = Registry(data)
        guest.startup()
        with pytest.raises(RegistryOwnedElsewhere, match="работающий сервер"):
            guest.publish_generation(guest.stage_generation(first, first_payloads))
        assert not guest.generation_recovery_path.exists()
    finally:
        _stop(server)


def test_гость_не_удаляет_осиротевшие_каталоги_кода(tmp_path):
    from test_registry_code_dir_sweep import _сироты

    data = tmp_path / "data"
    data.mkdir()
    server = _start_server(data)
    try:
        # Сироты появились уже после старта владельца: убирать их — его дело.
        сироты = _сироты(data)
        Registry(data).startup()
        assert all(путь.is_dir() for путь in сироты.values())
    finally:
        _stop(server)


def test_операция_сервера_перечитывает_чужой_registry_json(tmp_path):
    incoming = tmp_path / "input"
    incoming.mkdir()
    server = Registry(tmp_path / "data")
    server.startup()
    server.add_configuration(write_export(incoming, build_configuration(name="Пример")))
    server.save()

    # Второй процесс (CLI) дописал свою строку.
    other = Registry(server.data_dir)
    other.restore()
    other.add_configuration(write_export(incoming, build_configuration(name="Другая")))
    other.save()

    # У дашборда нет ручки reload: операция сервера сама перечитывает файл
    # и не стирает строку CLI.
    server.set_platform_version("Пример", "8.3.27.2130")
    payload = json.loads(server.registry_path.read_text(encoding="utf-8"))
    assert {"Пример", "Другая"} <= {raw["id"] for raw in payload["sources"]}
    assert {"Пример", "Другая"} <= set(server.configurations)
    assert payload["platform_declarations"]["Пример"]["version"] == "8.3.27.2130"


def test_смена_файла_во_время_операции_повторяется_после_reload(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "input"
    incoming.mkdir()
    server = Registry(tmp_path / "data")
    server.startup()
    server.add_configuration(write_export(incoming, build_configuration(name="Пример")))
    server.save()
    other = Registry(server.data_dir)
    other.restore()
    подлинный = server._registry_payload
    вызовов = {"n": 0}

    def payload_с_гонкой(*args, **kwargs):
        вызовов["n"] += 1
        if вызовов["n"] == 1:
            # CLI успевает записать между сверкой перед операцией и записью.
            other.add_configuration(
                write_export(incoming, build_configuration(name="Другая"))
            )
            other.save()
        return подлинный(*args, **kwargs)

    monkeypatch.setattr(server, "_registry_payload", payload_с_гонкой)
    # Операция с собственной записью повторяется после reload целиком.
    server.set_platform_version("Пример", "8.3.27.2130")

    payload = json.loads(server.registry_path.read_text(encoding="utf-8"))
    assert {"Пример", "Другая"} <= {raw["id"] for raw in payload["sources"]}
    assert payload["platform_declarations"]["Пример"]["version"] == "8.3.27.2130"


def test_save_не_перечитывает_и_не_откатывает_новую_память(tmp_path):
    incoming = tmp_path / "input"
    incoming.mkdir()
    server = Registry(tmp_path / "data")
    server.startup()
    old = server.add_configuration(
        write_export(incoming, build_configuration(name="Пример", version="1.0"))
    )
    server.save()
    other = Registry(server.data_dir)
    other.restore()
    # Двухфазная загрузка дашборда: память уже новая, save ещё впереди.
    new = server.add_configuration(
        write_export(incoming, build_configuration(name="Пример", version="2.0"))
    )
    other.add_configuration(write_export(incoming, build_configuration(name="Другая")))
    other.save()

    with pytest.raises(RegistryChangedExternally):
        server.save()
    assert server.configurations["Пример"].source is new
    assert new.sha256 != old.sha256


def test_загрузка_дашборда_повторяется_целиком_после_чужой_записи(
    tmp_path, monkeypatch
):
    from mcp1c import dashboard_backend

    incoming = tmp_path / "input"
    incoming.mkdir()
    server = Registry(tmp_path / "data")
    server.startup()
    server.add_configuration(
        write_export(incoming, build_configuration(name="Пример", version="1.0.1"))
    )
    server.save()
    other = Registry(server.data_dir)
    other.restore()
    (tmp_path / "upload").mkdir()
    # Версия новой выгрузки той же редакции: загрузка из дашборда сверяет её.
    upload = write_export(
        tmp_path / "upload", build_configuration(name="Пример", version="1.0.2")
    )
    подлинный_save = server.save
    вызовов = {"n": 0}

    def save_с_гонкой():
        вызовов["n"] += 1
        if вызовов["n"] == 1:
            other.add_configuration(
                write_export(incoming, build_configuration(name="Другая"))
            )
            other.save()
        return подлинный_save()

    monkeypatch.setattr(server, "save", save_с_гонкой)
    dashboard_backend._index_source(server, upload, ".zip")

    payload = json.loads(server.registry_path.read_text(encoding="utf-8"))
    rows = {raw["id"]: raw for raw in payload["sources"]}
    assert "Другая" in rows
    # Загруженная версия не откатилась строкой из файла.
    assert server.configurations["Пример"].config.version == "1.0.2"
    assert rows["Пример"]["sha256"] == server.configurations["Пример"].source.sha256


def test_старт_не_падает_если_гость_записал_registry_во_время_старта(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "input"
    incoming.mkdir()
    first = Registry(tmp_path / "data")
    first.add_configuration(write_export(incoming, build_configuration(name="Пример")))
    first.save()
    server = Registry(first.data_dir)
    other = Registry(first.data_dir)
    other.restore()
    подлинный_bootstrap = server.bootstrap

    def bootstrap_с_гостем():
        other.add_configuration(
            write_export(incoming, build_configuration(name="Другая"))
        )
        other.save()
        return подлинный_bootstrap()

    monkeypatch.setattr(server, "bootstrap", bootstrap_с_гостем)
    server.startup()

    assert {"Пример", "Другая"} <= set(server.configurations)
    payload = json.loads(server.registry_path.read_text(encoding="utf-8"))
    assert {"Пример", "Другая"} <= {raw["id"] for raw in payload["sources"]}


def test_параллельная_запись_json_не_теряет_временный_файл(tmp_path):
    registry = Registry(tmp_path / "data")
    target = registry.data_dir / "journal.json"
    ошибки: list[BaseException] = []

    def писать(номер: int) -> None:
        for шаг in range(150):
            try:
                registry._write_json_durably(target, {"writer": номер, "step": шаг})
            except BaseException as error:
                ошибки.append(error)
                return

    потоки = [threading.Thread(target=писать, args=(номер,)) for номер in range(3)]
    for поток in потоки:
        поток.start()
    for поток in потоки:
        поток.join(60)

    assert ошибки == []
    assert json.loads(target.read_text(encoding="utf-8"))["step"] == 149
    assert not list(registry.data_dir.glob("*.tmp"))


def test_прочие_ошибки_flock_не_дают_владения(tmp_path, monkeypatch):
    import errno

    import mcp1c.registry as registry_module

    подлинный = registry_module.fcntl.flock
    вызовов = {"n": 0}

    def ebadf(descriptor, operation):
        # Первый flock — замок владельца; короткие замки записи работают.
        вызовов["n"] += 1
        if вызовов["n"] == 1:
            raise OSError(errno.EBADF, "flock по дескриптору только для чтения")
        return подлинный(descriptor, operation)

    monkeypatch.setattr(registry_module.fcntl, "flock", ebadf)
    registry = Registry(tmp_path / "data")
    registry.startup()
    # Два процесса «без замка» оба сочли бы себя владельцами.
    assert registry.role == "guest"


def test_неоткрываемый_замок_не_даёт_владения(tmp_path):
    data = tmp_path / "data"
    (data / ".registry.owner.lock").mkdir(parents=True)
    registry = Registry(data)
    registry.startup()
    assert registry.role == "guest"


def test_неоткрываемый_замок_записи_отклоняет_save(tmp_path):
    from mcp1c.registry import RegistryError

    registry = Registry(tmp_path / "data")
    registry.startup()
    (registry.data_dir / ".registry.lock").unlink()
    (registry.data_dir / ".registry.lock").mkdir()
    with pytest.raises(RegistryError, match="замок записи"):
        registry.save()


def test_файл_замка_доступен_другим_при_строгом_umask(tmp_path):
    previous = os.umask(0o077)
    try:
        registry = Registry(tmp_path / "data")
        registry.startup()
        registry.save()
    finally:
        os.umask(previous)
    for name in (".registry.owner.lock", ".registry.lock"):
        assert (registry.data_dir / name).stat().st_mode & 0o777 == 0o644


def test_симлинк_вместо_замка_записи_не_меняет_права_цели(tmp_path):
    """Симлинк `.registry.lock` на чужой файл: fchmod 0644 по нему открыл бы
    цель на чтение всем."""
    from mcp1c.registry import RegistryError

    victim = tmp_path / "secret.txt"
    victim.write_text("секрет")
    victim.chmod(0o600)
    registry = Registry(tmp_path / "data")
    registry.startup()
    (registry.data_dir / ".registry.lock").unlink()
    (registry.data_dir / ".registry.lock").symlink_to(victim)

    with pytest.raises(RegistryError, match="замок записи"):
        registry.save()
    assert victim.stat().st_mode & 0o777 == 0o600


def test_симлинк_вместо_замка_владельца_не_даёт_владения(tmp_path):
    victim = tmp_path / "secret.txt"
    victim.write_text("секрет")
    victim.chmod(0o600)
    data = tmp_path / "data"
    data.mkdir()
    (data / ".registry.owner.lock").symlink_to(victim)

    registry = Registry(data)
    assert registry.acquire_ownership(0) is False
    assert victim.stat().st_mode & 0o777 == 0o600


def test_чужой_файл_замка_не_получает_fchmod(tmp_path, monkeypatch):
    """Права меняет только владелец файла: чужой (другой uid) файл замка
    остаётся с теми правами, что выставил его хозяин."""
    from mcp1c import registry as registry_module

    data = tmp_path / "data"
    data.mkdir()
    lock = data / ".registry.lock"
    lock.write_text("")
    lock.chmod(0o600)
    monkeypatch.setattr(registry_module.os, "geteuid", lambda: os.getuid() + 1)

    descriptor = registry_module._open_lock_file(lock)
    os.close(descriptor)
    assert lock.stat().st_mode & 0o777 == 0o600


def test_своя_запись_соседнего_потока_не_вызывает_reload(tmp_path, monkeypatch):
    incoming = tmp_path / "input"
    incoming.mkdir()
    server = Registry(tmp_path / "data")
    server.startup()
    server.add_configuration(write_export(incoming, build_configuration()))
    server.save()
    reloads: list = []
    подлинный_startup = server.startup
    monkeypatch.setattr(
        server, "startup", lambda: reloads.append(1) or подлинный_startup()
    )
    подлинная_запись = server._write_json_durably
    проверки: list[threading.Thread] = []

    def запись(target, payload, **kwargs):
        подлинная_запись(target, payload, **kwargs)
        if target == server.registry_path:
            # Окно между replace и обновлением штампа: сосед сверяет файл.
            поток = threading.Thread(target=server._refresh_if_changed_externally)
            поток.start()
            поток.join(0.3)
            проверки.append(поток)

    monkeypatch.setattr(server, "_write_json_durably", запись)
    server.set_platform_version(next(iter(server.configurations)), "8.3.27.2130")
    for поток in проверки:
        поток.join(5)
    assert проверки and reloads == []


def test_flock_без_поддержки_тома_не_делает_сервер_гостем(tmp_path, monkeypatch):
    import errno

    import mcp1c.registry as registry_module

    def нет_блокировок(*_args, **_kwargs):
        raise OSError(errno.ENOLCK, "flock не поддержан томом")

    monkeypatch.setattr(registry_module.fcntl, "flock", нет_блокировок)
    incoming = tmp_path / "input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.startup()

    assert registry.role == "owner"
    registry.add_configuration(write_export(incoming, build_configuration()))
    registry.save()


def test_гость_становится_владельцем_на_следующем_startup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    server = _start_server(data)
    try:
        registry = Registry(data)
        registry.startup()
        assert registry.role == "guest"
        assert registry.acquire_ownership(timeout=0.3) is False
    finally:
        _stop(server)
    registry.startup()
    assert registry.role == "owner"


def test_сервер_не_стартует_гостем(tmp_path, monkeypatch, capsys):
    from mcp1c import server as server_module

    data = tmp_path / "data"
    data.mkdir()
    holder = _start_server(data)
    try:
        monkeypatch.setattr(server_module, "OWNERSHIP_TIMEOUT", 0.3)
        code = server_module.main(["--data", str(data), "--transport", "stdio"])
    finally:
        _stop(holder)
    assert code == 2
    assert "не освободился" in capsys.readouterr().err


def test_гость_не_создаёт_staging_замены_native_базы(tmp_path):
    from test_intake_v2_extensions import _stage_active_base
    from test_intake_v2_legacy_cleanup import _native_generation

    setup = Registry(tmp_path / "data")
    setup.publish_generation(
        _stage_active_base(setup, _native_generation(tmp_path, "full"))
    )
    data = setup.data_dir
    server = _start_server(data)
    try:
        guest = Registry(data)
        guest.startup()
        incoming = tmp_path / "input"
        incoming.mkdir()
        archive = write_export(
            incoming, build_configuration(name="DemoConfiguration")
        )
        with pytest.raises(RegistryOwnedElsewhere):
            guest.add_configuration(archive)
        assert not list(data.glob(".schema-v1-base-*"))
        assert not list((data / "generations").glob(".staging-*"))
    finally:
        _stop(server)


def test_гость_повторяет_идентичную_native_a_без_смены_даты(tmp_path, monkeypatch):
    from test_intake_v2_extensions import _stage_active_base
    from test_intake_v2_legacy_cleanup import _native_generation

    def содержимое_реестра() -> dict:
        # Владелец на старте штатно пересохраняет реестр с новым `saved_at`
        # (точность — секунда), поэтому побайтовое сравнение падало, когда
        # старт процесса пересекал границу секунды.
        payload = json.loads(setup.registry_path.read_text(encoding="utf-8"))
        payload.pop("saved_at", None)
        return payload

    setup = Registry(tmp_path / "data")
    native = _native_generation(tmp_path, "full-for-noop")
    setup.publish_generation(_stage_active_base(setup, native))
    incoming = tmp_path / "input"
    incoming.mkdir()
    archive = write_export(
        incoming, build_configuration(name="DemoConfiguration")
    )
    setup.add_configuration(archive)
    before = setup.active_generation_pointer(native.manifest.identity)
    registry_before = содержимое_реестра()

    server = _start_server(setup.data_dir)
    try:
        guest = Registry(setup.data_dir)
        guest.startup()
        assert guest.role == "guest"
        записи_гостя = []
        monkeypatch.setattr(guest, "_write_registry_payload", записи_гостя.append)
        guest.add_configuration(archive)
        assert guest.active_generation_pointer(native.manifest.identity) == before
        assert записи_гостя == []
        assert содержимое_реестра() == registry_before
    finally:
        _stop(server)


def test_гость_дописывает_строки_поверх_свежего_файла(tmp_path):
    incoming = tmp_path / "input"
    incoming.mkdir()
    server = Registry(tmp_path / "data")
    server.startup()
    server.add_configuration(write_export(incoming, build_configuration(name="А")))
    server.save()
    guest = Registry(server.data_dir)
    guest.restore()
    guest._guest = True
    # Сервер записал своё уже после чтения гостем.
    server.add_configuration(write_export(incoming, build_configuration(name="Б")))
    server.save()

    guest.add_configuration(write_export(incoming, build_configuration(name="В")))
    guest.save()

    payload = json.loads(server.registry_path.read_text(encoding="utf-8"))
    assert {"А", "Б", "В"} <= {raw["id"] for raw in payload["sources"]}


def test_гость_не_удаляет_чужие_слитые_справки(tmp_path):
    from conftest import write_syntax

    registry = Registry(tmp_path / "data")
    registry.add_syntax(write_syntax(tmp_path / "s1", "8.3.20.1"))
    registry.add_syntax(write_syntax(tmp_path / "s2", "8.3.21.1"))
    чужой = registry._syntax_index_dir() / "merged-0000000000000000.json.gz"
    чужой.write_bytes(b"server view")
    registry._guest = True
    registry.add_syntax(write_syntax(tmp_path / "s3", "8.3.22.1"))
    assert чужой.read_bytes() == b"server view"


def test_старт_владельца_подметает_временные_файлы_записи(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    сироты = [
        data / ".registry.json.0123abcd.tmp",
        data / ".modules-swap.json.0123abcd.tmp",
    ]
    for path in сироты:
        path.write_text("{}", encoding="utf-8")

    Registry(data).startup()

    assert not any(path.exists() for path in сироты)


def test_замок_открывается_без_права_записи(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    lock = data / ".registry.owner.lock"
    lock.write_text("", encoding="utf-8")
    # Файл создан другим пользователем (root): у сервера только чтение.
    lock.chmod(0o444)
    (data / ".registry.lock").write_text("", encoding="utf-8")
    (data / ".registry.lock").chmod(0o444)
    registry = Registry(data)
    registry.startup()
    assert registry.role == "owner"
    registry.save()


def test_чтение_cli_при_сервере_без_подсказки_про_reload(tmp_path, capsys):
    from mcp1c.cli import main

    data = tmp_path / "data"
    data.mkdir()
    server = _start_server(data)
    try:
        main(["reg-list", "--data", str(data)])
    finally:
        _stop(server)
    assert "обслуживает" not in capsys.readouterr().err


def test_remove_сверяет_штамп_до_сноса_каталогов(
    tmp_path, корень_кода, monkeypatch
):
    from native_export import старый_реестр

    incoming = tmp_path / "input"
    incoming.mkdir()
    # Каталог кода снимает только строка кода прежней версии.
    data_dir = старый_реестр(
        tmp_path / "old", корень_кода, configuration=build_configuration(name="Пример")
    )
    server = Registry(data_dir)
    server.startup()
    assert server.wait_for_module_builds(timeout=10)
    other = Registry(server.data_dir)
    other.restore()
    other.add_configuration(write_export(incoming, build_configuration(name="Другая")))
    other.save()
    # Сверка перед операцией «не заметила» смену: проверяем сам remove.
    monkeypatch.setattr(server, "_refresh_if_changed_externally", lambda: None)
    отставлено: list = []
    подлинный = server._retire_code_root

    def учесть(*args, **kwargs):
        отставлено.append(args[0])
        return подлинный(*args, **kwargs)

    monkeypatch.setattr(server, "_retire_code_root", учесть)
    server.remove("Пример")
    server.save()

    # Первая попытка отказала до rename, повтор после reload снял один раз.
    assert len(отставлено) == 1
    payload = json.loads(server.registry_path.read_text(encoding="utf-8"))
    ids = {raw["id"] for raw in payload["sources"]}
    assert "Другая" in ids and "Пример" not in ids
