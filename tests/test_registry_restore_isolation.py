"""Изоляция повреждённых записей при восстановлении синтетического реестра."""

from copy import deepcopy
import errno
from hashlib import sha256
import json
from pathlib import Path

import pytest

from conftest import build_configuration, write_export, write_syntax
from mcp1c.intake_v2 import ExportIdentity
from mcp1c.intake_v2_collector import collect_source_b
from mcp1c.intake_v2_converter import convert_collection
from mcp1c.intake_v2_generation import materialize_generation
from mcp1c.intake_v2_probe import probe_export
from mcp1c.registry import Registry, RegistryRecoveryBlocked, ReloadRequired
from test_intake_v2_converter import (
    MemoryTree,
    _catalog,
    _common_attribute,
    _configuration as configuration_xml,
    _session_parameter,
)
from test_intake_v2_extensions import _materialized, _stage_active_base


BROKEN = "DemoConfiguration"
HEALTHY = "HealthyConfiguration"


def read_registry(registry):
    return json.loads(registry.registry_path.read_text(encoding="utf-8"))


def write_registry(registry, payload):
    registry.registry_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def publish_base(registry, tmp_path, name, generation_id):
    # Имя меняется во входном XML до probe/conversion, а не только в модели слоя.
    xml = configuration_xml()
    previous_name = b"<Name>DemoConfiguration</Name>"
    requested_name = f"<Name>{name}</Name>".encode("utf-8")
    assert xml.count(previous_name) == 1
    renamed_xml = xml.replace(previous_name, requested_name)
    assert renamed_xml.count(requested_name) == 1
    if name != BROKEN:
        assert previous_name not in renamed_xml
    tree = MemoryTree({
        "Configuration.xml": renamed_xml,
        "Catalogs/Items.xml": _catalog(),
        "Catalogs/Items/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
        "Catalogs/Items/Forms/Card.xml": b"<form-descriptor/>",
        "Catalogs/Items/Forms/Card/Ext/Form.xml": b"<form/>",
        "CommonAttributes/Tenant.xml": _common_attribute(),
        "SessionParameters/Tenant.xml": _session_parameter(),
    })
    collection = collect_source_b(
        tree, probe_export(tree), tmp_path / f"collection-{generation_id}"
    )
    converted = convert_collection(collection)
    assert converted.base.name == name
    generation = materialize_generation(
        collection, converted, tmp_path / f"materialized-{generation_id}",
        generation_id=generation_id,
    )
    assert generation.manifest.identity.configuration_name == name
    registry.publish_generation(_stage_active_base(registry, generation))


def base_pointer(payload, name):
    return next(
        row for row in payload["generation_manifests"]
        if isinstance(row, dict) and row["identity"]["configuration_name"] == name
        and row["identity"]["source_kind"] == "configuration"
    )


@pytest.fixture
def two_configurations(tmp_path):
    registry = Registry(tmp_path / "data")
    publish_base(registry, tmp_path, BROKEN, "broken-base")
    publish_base(registry, tmp_path, HEALTHY, "healthy-base")
    return registry


def park_broken_root(registry, tmp_path):
    pointer = deepcopy(base_pointer(read_registry(registry), BROKEN))
    root = registry.data_dir / pointer["root_path"]
    parked = tmp_path / "temporarily-unavailable-generation"
    root.rename(parked)
    return pointer, root, parked


def assert_generation_warning(messages, name):
    assert any(name in message and "поколение не поднято" in message for message in messages), messages


def assert_only_healthy_configuration(registry):
    snapshot = registry.snapshot()
    assert set(snapshot.configurations) == {HEALTHY}
    assert "Справочник.Items" in snapshot.configurations[HEALTHY].config.objects


def test_удалённый_корень_поколения_не_роняет_startup(two_configurations, tmp_path):
    original = two_configurations
    park_broken_root(original, tmp_path)
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert_generation_warning(messages, BROKEN)
    assert_only_healthy_configuration(restarted)


def test_restore_убирает_прежний_runtime_битой_базы_и_расширения(
    two_configurations, tmp_path
):
    original = two_configurations
    _collection, extension = _materialized(
        tmp_path, "reload-extension", configuration_name="DemoExtension", extension=True
    )
    original.publish_generation(original.stage_generation(extension.manifest, extension.payloads))
    original.startup()
    before = original.snapshot()
    assert BROKEN in before.configurations
    assert "DemoConfiguration:modules" in before.modules
    assert "DemoConfiguration:ext:DemoExtension" in before.modules
    assert before.extension_names(BROKEN) == ("DemoExtension",)
    park_broken_root(original, tmp_path)

    messages = original.restore()

    assert_generation_warning(messages, BROKEN)
    assert any("DemoExtension" in message for message in messages), messages
    assert_only_healthy_configuration(original)
    after = original.snapshot()
    assert not any(key.startswith(BROKEN + ":") for key in after.modules)
    assert after.extension_names(BROKEN) == ()


def test_pointer_битого_поколения_переживает_save_и_публикацию(
    two_configurations, tmp_path
):
    original = two_configurations
    pointer, root, parked = park_broken_root(original, tmp_path)
    restarted = Registry(original.data_dir)
    messages = restarted.startup()
    assert_generation_warning(messages, BROKEN)

    restarted.save()
    assert base_pointer(read_registry(restarted), BROKEN) == pointer
    publish_base(restarted, tmp_path, "PublishedLater", "published-later")
    assert base_pointer(read_registry(restarted), BROKEN) == pointer

    # Возвращаем ровно тот же bundle: сохранённый pointer снова должен сработать.
    root.parent.mkdir(parents=True, exist_ok=True)
    parked.rename(root)
    recovered = Registry(original.data_dir)
    recovery_messages = recovered.startup()

    assert set(recovered.snapshot().configurations) == {BROKEN, HEALTHY, "PublishedLater"}
    assert "Справочник.Items" in recovered.snapshot().configurations[BROKEN].config.objects
    assert not any(BROKEN in message and "поколение не поднято" in message
                   for message in recovery_messages), recovery_messages


def test_повреждённый_manifest_пропускается(two_configurations):
    original = two_configurations
    payload = read_registry(original)
    pointer = base_pointer(payload, BROKEN)
    # Формат sha256 верен, но он не соответствует существующему manifest.
    pointer["manifest_sha256"] = "0" * 64
    write_registry(original, payload)
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert_generation_warning(messages, BROKEN)
    assert_only_healthy_configuration(restarted)
    assert base_pointer(read_registry(restarted), BROKEN) == pointer


def test_расширение_битой_базы_пропускается(two_configurations, tmp_path):
    original = two_configurations
    _collection, extension = _materialized(
        tmp_path, "dependent-extension", configuration_name="DemoExtension", extension=True
    )
    original.publish_generation(original.stage_generation(extension.manifest, extension.payloads))
    original_rows = deepcopy(read_registry(original)["generation_manifests"])
    park_broken_root(original, tmp_path)
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert_generation_warning(messages, BROKEN)
    assert any("DemoExtension" in message for message in messages), messages
    assert_only_healthy_configuration(restarted)
    snapshot = restarted.snapshot()
    assert snapshot.extension_names(BROKEN) == ()
    assert "DemoConfiguration:ext:DemoExtension" not in snapshot.modules
    for row in original_rows:
        assert row in read_registry(restarted)["generation_manifests"]


def test_remove_битой_конфигурации_снимает_pointer(two_configurations, tmp_path):
    original = two_configurations
    park_broken_root(original, tmp_path)
    restarted = Registry(original.data_dir)
    assert_generation_warning(restarted.startup(), BROKEN)

    restarted.remove(BROKEN)

    rows = read_registry(restarted).get("generation_manifests", [])
    assert all(row["identity"]["configuration_name"] != BROKEN for row in rows)
    again = Registry(original.data_dir)
    messages = again.startup()
    assert_only_healthy_configuration(again)
    assert not any(BROKEN in message for message in messages), messages


def test_exists_permission_error_не_роняет_restore(two_configurations, tmp_path, monkeypatch):
    original = two_configurations
    legacy_dir = tmp_path / "legacy-input"
    legacy_dir.mkdir()
    unavailable = write_export(legacy_dir, build_configuration(name="LegacyUnavailable"))
    payload = read_registry(original)
    payload["sources"].append({
        "id": "LegacyUnavailable", "kind": "configuration", "status": "ready",
        "origin": unavailable.name, "stored_path": str(unavailable),
    })
    write_registry(original, payload)
    original_exists = Path.exists

    def exists(path):
        if path == unavailable:
            raise PermissionError(errno.EACCES, "синтетический запрет доступа", str(path))
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    restarted = Registry(original.data_dir)

    messages = restarted.restore()

    assert any("LegacyUnavailable" in message and "синтетический запрет доступа" in message
               for message in messages), messages
    assert set(restarted.snapshot().configurations) == {BROKEN, HEALTHY}


@pytest.mark.parametrize("corruption", ["missing-root", "invalid-sha", "not-object"])
def test_битая_pointer_строка_не_мешает_остальным_и_сохраняется(
    two_configurations, corruption
):
    original = two_configurations
    payload = read_registry(original)
    pointer = base_pointer(payload, BROKEN)
    if corruption == "missing-root":
        del pointer["root_path"]
        raw = deepcopy(pointer)
    elif corruption == "invalid-sha":
        pointer["manifest_sha256"] = "not-a-sha256"
        raw = deepcopy(pointer)
    else:
        index = payload["generation_manifests"].index(pointer)
        raw = "синтетическая повреждённая строка pointer"
        payload["generation_manifests"][index] = raw
    write_registry(original, payload)
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert messages
    assert HEALTHY in restarted.snapshot().configurations
    restarted.save()
    assert raw in read_registry(restarted)["generation_manifests"]


@pytest.mark.parametrize("raw", [{"id": "MalformedSource"}, 42])
def test_битая_source_строка_не_мешает_валидной_справке_и_конфигурациям(
    two_configurations, tmp_path, raw
):
    original = two_configurations
    original.add_syntax(write_syntax(tmp_path / "synthetic-syntax"))
    original.save()
    payload = read_registry(original)
    payload["sources"].insert(0, raw)
    write_registry(original, payload)
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert messages
    snapshot = restarted.snapshot()
    assert set(snapshot.configurations) == {BROKEN, HEALTHY}
    assert "syntax-8.3.99.1" in snapshot.syntax_versions
    restarted.save()
    assert raw in read_registry(restarted)["sources"]


@pytest.mark.parametrize("invalid", [{"version": "not-a-version"}, {"source": "user"}])
def test_битая_platform_declaration_не_мешает_валидной_и_конфигурациям(
    two_configurations, invalid
):
    original = two_configurations
    payload = read_registry(original)
    payload["platform_declarations"] = {
        "BrokenDeclaration": invalid,
        HEALTHY: {"version": "8.3.27.2130", "source": "user", "status": "declared"},
    }
    write_registry(original, payload)
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert any("BrokenDeclaration" in message for message in messages), messages
    assert set(restarted.snapshot().configurations) == {BROKEN, HEALTHY}
    assert restarted.platform_declaration(HEALTHY).version == "8.3.27.2130"
    restarted.save()
    assert read_registry(restarted)["platform_declarations"]["BrokenDeclaration"] == invalid


@pytest.mark.parametrize("set_before_clear", [False, True], ids=["clear-directly", "set-then-clear"])
def test_clear_platform_version_не_воскрешает_битую_декларацию(
    two_configurations, set_before_clear
):
    original = two_configurations
    invalid = {"version": "not-a-version", "source": "user", "status": "declared"}
    payload = read_registry(original)
    payload["platform_declarations"] = {HEALTHY: invalid}
    write_registry(original, payload)
    restarted = Registry(original.data_dir)
    messages = restarted.startup()
    assert any(HEALTHY in message for message in messages), messages
    assert restarted.platform_declaration(HEALTHY) is None

    if set_before_clear:
        restarted.set_platform_version(HEALTHY, "8.3.27.2130")
        assert restarted.platform_declaration(HEALTHY).version == "8.3.27.2130"
    restarted.clear_platform_version(HEALTHY)
    restarted.save()

    assert HEALTHY not in read_registry(restarted).get("platform_declarations", {})
    recovered = Registry(original.data_dir)
    recovered.startup()
    assert recovered.platform_declaration(HEALTHY) is None
    recovered.save()
    assert HEALTHY not in read_registry(recovered).get("platform_declarations", {})
    assert HEALTHY in recovered.snapshot().configurations


def test_guest_save_сохраняет_необъектную_source_строку(two_configurations, tmp_path):
    from test_registry_interprocess import _start_server, _stop

    original = two_configurations
    payload = read_registry(original)
    payload["sources"].append(42)
    write_registry(original, payload)
    # Отдельный процесс действительно держит lease: приватный _guest не подменяется.
    owner = _start_server(original.data_dir)
    try:
        guest = Registry(original.data_dir)
        guest.startup()
        assert guest.role == "guest"
        assert guest.acquire_ownership(timeout=0) is False

        source = guest.add_syntax(write_syntax(tmp_path / "guest-syntax"))
        guest.save()

        rows = read_registry(guest)["sources"]
        assert 42 in rows
        assert any(isinstance(row, dict) and row.get("id") == "syntax-8.3.99.1"
                   for row in rows)
        assert source.id == "syntax-8.3.99.1"
        assert "syntax-8.3.99.1" in guest.snapshot().syntax_versions
    finally:
        _stop(owner)


@pytest.mark.parametrize("entrypoint", ["bootstrap", "direct"])
def test_source_a_не_подменяет_неподнятое_native_поколение(
    two_configurations, tmp_path, entrypoint
):
    original = two_configurations
    pointer, _root, parked = park_broken_root(original, tmp_path)
    native_sha256 = json.loads((parked / "manifest.json").read_text(encoding="utf-8"))[
        "raw_sha256"
    ]
    incoming = original.data_dir / "bootstrap" if entrypoint == "bootstrap" else tmp_path / "source-a"
    incoming.mkdir(parents=True, exist_ok=True)
    archive = write_export(incoming, build_configuration(name=BROKEN))
    assert sha256(archive.read_bytes()).hexdigest() != native_sha256
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert_generation_warning(messages, BROKEN)
    if entrypoint == "bootstrap":
        assert any("bootstrap:" in message and archive.name in message
                   and ("поколен" in message.lower() or "reload" in message.lower())
                   for message in messages), messages
    else:
        with pytest.raises(ReloadRequired):
            restarted.add_configuration(archive)
    assert_only_healthy_configuration(restarted)
    assert base_pointer(read_registry(restarted), BROKEN) == pointer
    restarted.save()
    assert base_pointer(read_registry(restarted), BROKEN) == pointer


@pytest.mark.parametrize("corruption", ["checksum-mismatch", "missing-root"])
def test_дубли_pointer_не_выбирают_готовое_поколение_и_сохраняются_до_remove(
    two_configurations, corruption
):
    original = two_configurations
    payload = read_registry(original)
    first = deepcopy(base_pointer(payload, BROKEN))
    duplicate = deepcopy(first)
    if corruption == "checksum-mismatch":
        duplicate["manifest_sha256"] = "0" * 64
    else:
        del duplicate["root_path"]
    payload["generation_manifests"].append(duplicate)
    write_registry(original, payload)
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert any(BROKEN in message
               and any(word in message.lower() for word in ("дубли", "повтор", "duplicate"))
               for message in messages), messages
    assert_only_healthy_configuration(restarted)
    identity = ExportIdentity.configuration(BROKEN)
    assert restarted.active_generation_pointer(identity) is None
    assert restarted.active_generation(identity) is None
    restarted.save()
    rows = read_registry(restarted)["generation_manifests"]
    broken_rows = [row for row in rows if row["identity"]["configuration_name"] == BROKEN]
    assert len(broken_rows) == 2
    assert first in broken_rows
    assert duplicate in broken_rows

    restarted.remove(BROKEN)

    remaining = read_registry(restarted).get("generation_manifests", [])
    assert all(row["identity"]["configuration_name"] != BROKEN for row in remaining)
    assert base_pointer(read_registry(restarted), HEALTHY) in remaining
    assert_only_healthy_configuration(restarted)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("generation_manifests", {}),
        ("sources", {"raw": {"keep": 1}}),
        ("platform_declarations", ["keep"]),
    ],
)
def test_неверный_тип_контейнера_реестра_сохраняет_байты_и_закрывает_save(
    two_configurations, field, invalid
):
    original = two_configurations
    payload = read_registry(original)
    payload[field] = invalid
    write_registry(original, payload)
    before = original.registry_path.read_bytes()
    restarted = Registry(original.data_dir)

    messages = restarted.startup()

    assert any("только для чтения" in message.lower() for message in messages), messages
    assert restarted.registry_path.read_bytes() == before
    with pytest.raises(RegistryRecoveryBlocked):
        restarted.save()
    assert restarted.registry_path.read_bytes() == before


# R9 (ревью реализации аудита): reload не снимает runtime исправного
# расширения раньше, чем ставит его заново. Сборка runtime расширения идёт
# без `_lock`; читатель в это окно видел «расширение не загружено».

def _publish_extension(registry, tmp_path, label):
    _collection, extension = _materialized(
        tmp_path, label, configuration_name="DemoExtension", extension=True
    )
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )


def test_r9_reload_не_прячет_исправное_расширение(two_configurations, tmp_path):
    import threading

    from mcp1c.intake_v2 import SourceKind

    registry = two_configurations
    _publish_extension(registry, tmp_path, "r9-extension")
    registry.startup()
    assert registry.snapshot().extension_names(BROKEN) == ("DemoExtension",)

    paused, resume = threading.Event(), threading.Event()
    original_build = registry._build_native_generation_runtime

    def slow_build(root, manifest):
        if manifest.identity.source_kind is SourceKind.EXTENSION:
            paused.set()
            assert resume.wait(10), "тест не отпустил сборку расширения"
        return original_build(root, manifest)

    registry._build_native_generation_runtime = slow_build
    errors = []

    def run_restore():
        try:
            errors.extend(registry.restore())
        except BaseException as error:  # pragma: no cover - диагностика теста
            errors.append(error)

    worker = threading.Thread(target=run_restore)
    worker.start()
    try:
        assert paused.wait(10), "restore не дошёл до сборки расширения"
        during = registry.snapshot()
        assert during.extension_names(BROKEN) == ("DemoExtension",)
        assert "DemoConfiguration:ext:DemoExtension" in during.modules
    finally:
        resume.set()
        worker.join(10)
    assert not worker.is_alive()
    assert not [item for item in errors if not isinstance(item, str)], errors
    after = registry.snapshot()
    assert after.extension_names(BROKEN) == ("DemoExtension",)
    assert "DemoConfiguration:ext:DemoExtension" in after.modules


def test_r9_битое_поколение_расширения_снимается_при_reload(two_configurations, tmp_path):
    registry = two_configurations
    _publish_extension(registry, tmp_path, "r9-broken-extension")
    registry.startup()
    row = next(
        row for row in read_registry(registry)["generation_manifests"]
        if row["identity"]["source_kind"] == "extension"
    )
    root = registry.data_dir / row["root_path"]
    root.rename(tmp_path / "parked-extension-generation")

    messages = registry.restore()

    assert any("DemoExtension" in message for message in messages), messages
    snapshot = registry.snapshot()
    assert snapshot.extension_names(BROKEN) == ()
    assert "DemoConfiguration:ext:DemoExtension" not in snapshot.modules
    assert set(snapshot.configurations) == {BROKEN, HEALTHY}


def test_r9_расширение_базы_не_прошедшей_verify_не_видно_без_родителя(
    two_configurations, tmp_path
):
    # Финальное ревью R9: база с испорченным manifest не проходит verify и
    # не считается native-родителем; прежний runtime её расширения нельзя
    # оставлять видимым, когда runtime самой базы уже снят.
    from contextlib import contextmanager

    registry = two_configurations
    _publish_extension(registry, tmp_path, "r9-verify-extension")
    registry.startup()
    extension_key = "DemoConfiguration:ext:DemoExtension"
    assert extension_key in registry.snapshot().modules
    payload = read_registry(registry)
    base_pointer(payload, BROKEN)["manifest_sha256"] = "0" * 64
    write_registry(registry, payload)

    original_change = registry._registry_memory_change
    orphans = []

    @contextmanager
    def observed_change(*args, **kwargs):
        # Каждый вход видит состояние, опубликованное предыдущим блоком.
        if extension_key in registry.modules and BROKEN not in registry.configurations:
            orphans.append(True)
        with original_change(*args, **kwargs):
            yield

    registry._registry_memory_change = observed_change

    messages = registry.restore()

    assert_generation_warning(messages, BROKEN)
    assert not orphans, "расширение было видно без своей базы"
    assert extension_key not in registry.snapshot().modules
