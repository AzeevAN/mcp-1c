"""Требуемый объём считается по центральному каталогу, без распаковки."""
import threading
import errno
import zipfile
from pathlib import Path

import pytest

from conftest import (
    build_configuration,
    extension_configuration_xml,
    modules_configuration_xml,
    write_export,
)
from mcp1c import intake
from mcp1c import registry as registry_module
from mcp1c.intake import FORMAT_TREE, enough_space, planned_size
from mcp1c.registry import Registry, RegistryError


def _архив(tmp_path: Path) -> Path:
    путь = tmp_path / "выгрузка.zip"
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml())
        zf.writestr("Catalogs/Товары/Ext/ObjectModule.bsl", "A" * 1000)
        zf.writestr("Catalogs/Товары/Forms/Ф/Ext/Form.xml", "B" * 500)
        zf.writestr("Ext/ParentConfigurations/Поставка.cf", "C" * 100_000)
    return путь


def test_считается_только_отобранное(tmp_path):
    нужно, формат = planned_size(_архив(tmp_path))

    assert формат == FORMAT_TREE
    # 1000 + 500 плюс запас под индекс; балласт в 100 КБ не учитывается.
    assert 1500 < нужно < 1500 + 26 * 1024 * 1024


def test_места_не_хватает_называется_свободное(tmp_path):
    хватает, свободно = enough_space(10 ** 18, tmp_path)

    assert хватает is False
    assert свободно > 0


def test_резерв_равен_пятнадцати_процентам_отобранного(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "INDEX_RESERVE_MIN", 0)
    архив = tmp_path / "доля.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 101)

    нужно, _ = planned_size(архив)

    assert нужно == 101 + 16


def test_резерв_не_меньше_двадцати_пяти_мегабайт(tmp_path):
    архив = tmp_path / "пол.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A")

    нужно, _ = planned_size(архив)

    assert нужно == 1 + 25 * 1024 * 1024


def test_существующий_корень_не_вычитается_из_уже_свободного_места(tmp_path):
    архив = tmp_path / "одна.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 100)

    новый, _ = planned_size(архив, existing=False)
    переразбор, _ = planned_size(архив, existing=True)

    assert новый == 100 + 25 * 1024 * 1024
    assert переразбор == новый


def _registry(tmp_path: Path) -> Registry:
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "metadata"
    incoming.mkdir()
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    return registry


def _code_archive(path: Path, *, extension: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            extension_configuration_xml(extension)
            if extension
            else modules_configuration_xml(),
        )
        zf.writestr("CommonModules/Пример/Ext/Module.bsl", "A" * 100)
    return path


@pytest.mark.parametrize(
    ("extension", "existing"),
    [(None, False), (None, True), ("Доп", False), ("Доп", True)],
)
def test_registry_проверяет_точный_корень_до_распаковки(
    tmp_path, monkeypatch, extension, existing
):
    registry = _registry(tmp_path)
    archive = _code_archive(
        tmp_path / ("extension.zip" if extension else "modules.zip"),
        extension=extension,
    )
    root = (
        registry.extensions_dir / "Пример" / extension
        if extension
        else registry.modules_dir / "Пример"
    )
    if existing:
        root.mkdir(parents=True)
    checks: list[tuple[int, Path, tuple[str, ...]]] = []

    def no_space(required, directory):
        checks.append(
            (required, directory, tuple(sorted(registry._module_operations)))
        )
        return False, required - 1

    monkeypatch.setattr(intake, "enough_space", no_space)

    with pytest.raises(RegistryError, match="нужно.*свободно"):
        registry.add_modules(archive, configuration="Пример")

    source_id = "Пример:ext:Доп" if extension else "Пример:modules"
    assert checks == [
        (100 + 25 * 1024 * 1024, registry.data_dir, (source_id,))
    ]
    assert source_id not in registry.sources


@pytest.mark.parametrize("extension", [None, "Доп"])
def test_смена_конфигурации_во_время_проверки_не_публикует_старую_операцию(
    tmp_path, monkeypatch, extension
):
    registry = _registry(tmp_path)
    archive = _code_archive(
        tmp_path / ("extension.zip" if extension else "modules.zip"),
        extension=extension,
    )
    started = threading.Event()
    release = threading.Event()
    extract_calls = 0
    real_extract = intake.extract

    def enough(required, directory):
        started.set()
        release.wait(timeout=3)
        return True, required

    def extract(*args, **kwargs):
        nonlocal extract_calls
        extract_calls += 1
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(intake, "enough_space", enough)
    monkeypatch.setattr(intake, "extract", extract)
    errors: list[BaseException] = []

    def parse():
        try:
            registry.add_modules(archive, configuration="Пример")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=parse)
    thread.start()
    try:
        assert started.wait(timeout=1)
        registry.remove("Пример")
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        registry.add_configuration(
            write_export(
                replacement,
                build_configuration(name="Пример", version="2.0"),
            )
        )
    finally:
        release.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(errors) == 1 and "разбор отменён" in str(errors[0])
    assert extract_calls == 0
    source_id = "Пример:ext:Доп" if extension else "Пример:modules"
    root = (
        registry.extensions_dir / "Пример" / "Доп"
        if extension
        else registry.modules_dir / "Пример"
    )
    assert source_id not in registry.sources
    assert not root.exists()


def test_смена_архива_после_выбора_типа_отменяет_разбор(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    archive = _code_archive(tmp_path / "modules.zip")
    started = threading.Event()
    release = threading.Event()

    def enough(required, directory):
        started.set()
        release.wait(timeout=3)
        return True, required

    monkeypatch.setattr(intake, "enough_space", enough)
    errors: list[BaseException] = []

    thread = threading.Thread(
        target=lambda: _capture_error(
            errors, registry.add_modules, archive, configuration="Пример"
        )
    )
    thread.start()
    try:
        assert started.wait(timeout=1)
        replacement = tmp_path / "replacement.zip"
        _code_archive(replacement, extension="Доп")
        replacement.replace(archive)
    finally:
        release.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(errors) == 1 and "архив изменился" in str(errors[0])
    assert "Пример:modules" not in registry.sources
    assert "Пример:ext:Доп" not in registry.sources


def _capture_error(errors, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except BaseException as error:
        errors.append(error)


def test_enospc_при_распаковке_не_раскрывает_путь_и_сохраняет_старый_корень(
    tmp_path, monkeypatch
):
    registry = _registry(tmp_path)
    old = _code_archive(tmp_path / "old.zip")
    registry.add_modules(old, configuration="Пример")
    previous_source = registry.sources["Пример:modules"]
    root = registry.modules_dir / "Пример"
    previous_file = root / "CommonModules/Пример/Ext/Module.bsl"
    assert previous_file.is_file()

    new = _code_archive(tmp_path / "new.zip")

    def no_space_during_extract(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space", "/private/secret/tmp")

    monkeypatch.setattr(intake, "extract", no_space_during_extract)

    with pytest.raises(RegistryError) as caught:
        registry.add_modules(new, configuration="Пример")

    message = str(caught.value)
    assert "место закончилось" in message
    assert "/private/secret" not in message
    assert registry.sources["Пример:modules"] is previous_source
    assert previous_file.is_file()
    assert not list(root.parent.glob(".Пример.tmp-*"))


def _снимок_дерева(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
@pytest.mark.parametrize("existing", [False, True], ids=["new", "reparse"])
@pytest.mark.parametrize("stage", ["sha256", "chmod", "extract", "build", "swap"])
def test_любая_fs_ошибка_после_проверки_места_безопасна_и_не_оставляет_мусор(
    tmp_path, monkeypatch, extension, existing, stage
):
    registry = _registry(tmp_path)
    source_id = "Пример:ext:Доп" if extension else "Пример:modules"
    root = (
        registry.extensions_dir / "Пример" / "Доп"
        if extension
        else registry.modules_dir / "Пример"
    )
    if existing:
        old = _code_archive(tmp_path / "old.zip", extension=extension)
        registry.add_modules(old, configuration="Пример")
        registry.save()
    old_source = registry.sources.get(source_id)
    old_loaded = registry.modules.get(source_id)
    old_root = _снимок_дерева(root)
    old_cache = _снимок_дерева(registry.cache_dir)
    new = _code_archive(tmp_path / "new.zip", extension=extension)

    def failure(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Denied", "/private/secret/current")

    if stage == "sha256":
        monkeypatch.setattr("mcp1c.registry._sha256", failure)
    elif stage == "chmod":
        original_chmod = Path.chmod

        def chmod(path, mode, *args, **kwargs):
            if ".tmp-" in path.name:
                return failure()
            return original_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "chmod", chmod)
    elif stage == "extract":
        monkeypatch.setattr(intake, "extract", failure)
    elif stage == "build":
        monkeypatch.setattr(registry, "_построить_индекс_кода", failure)
    else:
        original_rename = Path.rename

        def rename(path, target):
            if ".tmp-" in path.name:
                return failure()
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", rename)

    with pytest.raises(RegistryError) as caught:
        registry.add_modules(new, configuration="Пример")

    assert "/private/secret" not in str(caught.value)
    assert "Denied" not in str(caught.value)
    assert registry.sources.get(source_id) is old_source
    assert registry.modules.get(source_id) is old_loaded
    assert _снимок_дерева(root) == old_root
    assert _снимок_дерева(registry.cache_dir) == old_cache
    assert not list(root.parent.glob(f".{root.name}.tmp-*"))
    assert not list(root.parent.glob(f".{root.name}.old-*"))


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
def test_устаревшая_fs_ошибка_становится_явной_отменой(
    tmp_path, monkeypatch, extension
):
    registry = _registry(tmp_path)
    archive = _code_archive(tmp_path / "stale.zip", extension=extension)
    started = threading.Event()
    release = threading.Event()

    def stale_failure(*_args, **_kwargs):
        started.set()
        release.wait(timeout=3)
        raise OSError(errno.EACCES, "Denied", "/private/secret/stale")

    monkeypatch.setattr(intake, "extract", stale_failure)
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=lambda: _capture_error(
            errors, registry.add_modules, archive, configuration="Пример"
        )
    )
    thread.start()
    try:
        assert started.wait(timeout=1)
        registry.remove("Пример")
    finally:
        release.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RegistryError)
    assert "разбор отменён" in str(errors[0])
    assert "/private/secret" not in str(errors[0])
    assert not registry.sources and not registry.modules


def test_registry_и_распаковка_одинаково_игнорируют_опасные_признаки_формата(
    tmp_path
):
    registry = _registry(tmp_path)
    archive = tmp_path / "safe-selection.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml())
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А()\nКонецПроцедуры")
        zf.writestr("../escape.Form", "ложная форма")
        zf.writestr("__MACOSX/CommonModule.Ложный.Module", "мусор")

    source = registry.add_modules(archive, configuration="Пример")

    assert source.items_total == 1
    root = registry.modules_dir / "Пример"
    assert (root / "Catalogs/Т/Ext/ObjectModule.bsl").is_file()
    assert not any(path.suffix in {".Form", ".Module"} for path in root.rglob("*"))


@pytest.mark.parametrize(
    "junk",
    ["Junk/CommonModule.False.Module", "Junk/CommonModules/False.Module"],
)
def test_registry_не_теряет_bsl_из_за_вложенного_неканонического_module(
    tmp_path, junk
):
    registry = _registry(tmp_path)
    archive = tmp_path / "nested-junk.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml())
        zf.writestr(
            "Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура Доступная() Экспорт\nКонецПроцедуры",
        )
        zf.writestr(junk, "не канонический модуль")

    source = registry.add_modules(archive, configuration="Пример")

    loaded = registry.resolve("Пример").modules
    assert source.items_total == 1
    assert loaded.оглавление.по_имени("Доступная")
    assert not (loaded.корень / "Junk").exists()


def test_единая_карта_zip_нормализует_обёртку_и_берёт_последний_дубль(
    tmp_path
):
    registry = _registry(tmp_path)
    archive = tmp_path / "normalized.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("._Wrap", "ресурсная вилка корневого каталога")
        zf.writestr("Wrap/Configuration.xml", "<повреждённый />")
        zf.writestr("Wrap/./Configuration.xml", modules_configuration_xml())
        zf.writestr("Wrap/manifest.xml", "старый манифест")
        zf.writestr("Wrap//./manifest.xml", "последний манифест")
        zf.writestr(
            "Wrap/CommonModules/Пример/Ext/./Module.bsl",
            "Процедура Старая()\nКонецПроцедуры",
        )
        zf.writestr(
            "Wrap//CommonModules/Пример/Ext/Module.bsl",
            "Процедура Новая() Экспорт\nКонецПроцедуры",
        )

    source = registry.add_modules(archive, configuration="Пример")

    loaded = registry.resolve("Пример").modules
    assert source.items_total == 1
    assert loaded.оглавление.по_имени("Новая")
    assert not loaded.оглавление.по_имени("Старая")
    assert not (loaded.корень / "Wrap").exists()
    with zipfile.ZipFile(archive) as zf:
        карта = intake.карта_архива(zf)
        assert zf.read(карта["manifest.xml"]).decode() == "последний манифест"


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
@pytest.mark.parametrize("stage", ["precheck", "type"])
def test_ошибка_до_lifecycle_не_раскрывает_путь(
    tmp_path, monkeypatch, extension, stage
):
    registry = _registry(tmp_path)
    archive = _code_archive(tmp_path / "pre-lifecycle.zip", extension=extension)

    def failure(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Denied", "/private/secret/precheck")

    if stage == "precheck":
        monkeypatch.setattr(registry_module, "_отбираемых_членов", failure)
    else:
        original_read = zipfile.ZipFile.read

        def read(zf, name, *args, **kwargs):
            filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
            if Path(filename).name == "Configuration.xml":
                return failure()
            return original_read(zf, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "read", read)

    with pytest.raises(RegistryError) as caught:
        registry.add_modules(archive, configuration="Пример")

    message = str(caught.value)
    assert archive.name in message
    assert "/private/secret" not in message and "Denied" not in message
    assert not registry._module_operations


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
def test_двойной_отказ_rollback_сообщает_безопасный_путь_восстановления(
    tmp_path, monkeypatch, extension
):
    registry = _registry(tmp_path)
    old = _code_archive(tmp_path / "old-double.zip", extension=extension)
    registry.add_modules(old, configuration="Пример")
    source_id = "Пример:ext:Доп" if extension else "Пример:modules"
    old_source = registry.sources[source_id]
    old_loaded = registry.modules[source_id]
    root = (
        registry.extensions_dir / "Пример" / "Доп"
        if extension
        else registry.modules_dir / "Пример"
    )
    new = _code_archive(tmp_path / "new-double.zip", extension=extension)
    original_rename = Path.rename

    def rename(path, target):
        if ".tmp-" in path.name or ".old-" in path.name:
            raise OSError(errno.EACCES, "Denied", "/private/secret/rollback")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)

    with pytest.raises(RegistryError) as caught:
        registry.add_modules(new, configuration="Пример")

    message = str(caught.value)
    retired = list(root.parent.glob(f".{root.name}.old-*"))
    assert len(retired) == 1
    assert root.name in message and retired[0].name in message
    assert ("data/extensions" if extension else "data/modules") in message
    assert str(root.parent) not in message
    assert "/private/secret" not in message and "Denied" not in message
    assert registry.sources[source_id] is old_source
    assert registry.modules[source_id] is old_loaded
    assert not root.exists()
    assert not list(root.parent.glob(f".{root.name}.tmp-*"))
