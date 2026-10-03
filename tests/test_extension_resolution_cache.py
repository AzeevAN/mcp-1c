"""Повторные публичные обращения не пересобирают структуру расширения.

RED до кэша: счётчик resolver растёт на каждом вызове. Источники синтетические,
но публикуются через тот же native B_FULL путь, что и файловая выгрузка.
"""

from __future__ import annotations

import shutil
import threading

from conftest import build_configuration, write_export
from mcp1c import registry as registry_module, tools
from mcp1c.registry import Registry
from native_export import опубликовать_код


CONFIG = "DemoConfiguration"
EXTENSION = "DemoExtension"
ADDRESS = "ОбщийМодуль.ОбщийПример::Сложить"


def _setup(tmp_path, корень_кода):
    # Фикстура кладёт код прямо в tmp_path; выделяем вход до создания Registry.
    code = tmp_path / "code"
    code.mkdir()
    for name in ("CommonModules", "Documents", "Catalogs"):
        shutil.move(str(корень_кода / name), str(code / name))
    registry = Registry(tmp_path / "data")
    configuration = build_configuration(name=CONFIG)
    base = опубликовать_код(
        registry, code, configuration=configuration, рабочий=tmp_path / "publish-base"
    )
    extension = опубликовать_код(
        registry, code, configuration=configuration, extension=EXTENSION,
        рабочий=tmp_path / "publish-extension"
    )
    return registry, configuration, code, base, extension


def _count_resolver(monkeypatch):
    real = registry_module.resolve_extension_structure
    calls = []

    def counted(base, extension):
        calls.append((base, extension))
        return real(base, extension)

    monkeypatch.setattr(registry_module, "resolve_extension_structure", counted)
    return calls


def test_public_tools_повторно_используют_одно_разрешение(tmp_path, корень_кода,
                                                          monkeypatch):
    registry, _configuration, _code, _base, _extension = _setup(tmp_path, корень_кода)
    calls = _count_resolver(monkeypatch)

    first = tools.get_procedure(registry, ADDRESS, config=CONFIG, extension=EXTENSION)
    second = tools.search_procedures(
        registry, "Сложить", config=CONFIG, extension=EXTENSION
    )
    third = tools.get_procedure(registry, ADDRESS, config=CONFIG, extension=EXTENSION)

    assert "Сложить" in first and "Сложить" in second and third == first
    assert len(calls) == 1


def test_пустое_имя_расширения_сохраняет_прежний_контекст(tmp_path, корень_кода):
    registry, _configuration, _code, _base, _extension = _setup(tmp_path, корень_кода)
    context = registry.resolve(CONFIG, extension="")
    assert context.name == CONFIG
    assert context.extension is None
    assert context.extension_resolution is None


def test_замена_базы_и_расширения_инвалидирует_разрешение(tmp_path, корень_кода,
                                                         monkeypatch):
    registry, configuration, code, _base, _extension = _setup(tmp_path, корень_кода)
    calls = _count_resolver(monkeypatch)

    first = registry.resolve(CONFIG, extension=EXTENSION).extension_resolution
    assert first is not None and len(calls) == 1
    replacement = build_configuration(name=CONFIG, version="2.0")
    опубликовать_код(
        registry, code, configuration=replacement,
        рабочий=tmp_path / "replace-base"
    )
    second = registry.resolve(CONFIG, extension=EXTENSION).extension_resolution
    assert second is not None and second is not first and len(calls) == 2
    assert second.configuration.version == "2.0"

    опубликовать_код(
        registry, code, configuration=configuration, extension=EXTENSION,
        рабочий=tmp_path / "replace-extension"
    )
    third = registry.resolve(CONFIG, extension=EXTENSION).extension_resolution
    assert third is not None and third is not second and len(calls) == 3
    assert registry.resolve(CONFIG, extension=EXTENSION).extension_resolution is third
    assert len(calls) == 3

    registry.remove(f"{CONFIG}:ext:{EXTENSION}")
    assert registry.resolve(CONFIG, extension=EXTENSION).extension_resolution is None
    опубликовать_код(
        registry, code, configuration=configuration, extension=EXTENSION,
        рабочий=tmp_path / "readd-extension"
    )
    fourth = registry.resolve(CONFIG, extension=EXTENSION).extension_resolution
    assert fourth is not None and fourth is not third and len(calls) == 4


def test_смена_поколения_во_время_сборки_не_публикует_устаревший_кэш(
    tmp_path, корень_кода, monkeypatch
):
    registry, _configuration, code, _base, _extension = _setup(tmp_path, корень_кода)
    real = registry_module.resolve_extension_structure
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def delayed(base, extension):
        calls.append(base)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=10)
        return real(base, extension)

    monkeypatch.setattr(registry_module, "resolve_extension_structure", delayed)
    outcome = []

    def reader():
        try:
            outcome.append(registry.resolve(CONFIG, extension=EXTENSION))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(timeout=10)
        опубликовать_код(
            registry, code,
            configuration=build_configuration(name=CONFIG, version="2.0"),
            рабочий=tmp_path / "concurrent-base",
        )
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(outcome) == 1 and not isinstance(outcome[0], Exception)
    resolved = outcome[0].extension_resolution
    assert resolved is not None and resolved.configuration.version == "2.0"
    assert len(calls) == 2
    assert registry.resolve(CONFIG, extension=EXTENSION).extension_resolution is resolved
    assert len(calls) == 2


def test_чужой_источник_не_заставляет_повторять_resolve_без_расширения(
    tmp_path, корень_кода, monkeypatch
):
    registry, _configuration, _code, _base, _extension = _setup(tmp_path, корень_кода)
    real = registry._compute_relation
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def delayed(loaded, syntax, platform_override=""):
        calls.append(loaded)
        entered.set()
        assert release.wait(timeout=10)
        return real(loaded, syntax, platform_override)

    monkeypatch.setattr(registry, "_compute_relation", delayed)
    with registry._lock:
        registry._relation_cache.clear()
    outcome = []

    def reader():
        try:
            outcome.append(registry.resolve(CONFIG))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(timeout=10)
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        registry.add_configuration(write_export(
            unrelated, build_configuration(name="OtherConfiguration")
        ))
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(outcome) == 1 and not isinstance(outcome[0], Exception)
    assert outcome[0].name == CONFIG
    assert len(calls) == 1


def test_чужая_публикация_не_воскрешает_освобождённый_кэш(
    tmp_path, корень_кода, monkeypatch
):
    registry, _configuration, _code, _base, _extension = _setup(tmp_path, корень_кода)
    calls = _count_resolver(monkeypatch)
    assert registry.resolve(CONFIG, extension=EXTENSION).extension_resolution is not None
    assert len(calls) == 1
    entered = threading.Event()
    release = threading.Event()
    real = registry._compute_relation

    def delayed(loaded, syntax, platform_override=""):
        entered.set()
        assert release.wait(timeout=10)
        return real(loaded, syntax, platform_override)

    monkeypatch.setattr(registry, "_compute_relation", delayed)
    with registry._lock:
        registry._relation_cache.clear()
    outcome = []

    def reader():
        try:
            outcome.append(registry.resolve(CONFIG, extension=EXTENSION))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(timeout=10)
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        registry.add_configuration(write_export(
            unrelated, build_configuration(name="OtherConfiguration")
        ))
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(outcome) == 1 and not isinstance(outcome[0], Exception)
    assert outcome[0].extension_resolution is not None
    assert registry.resolve(CONFIG, extension=EXTENSION).extension_resolution is not None
    assert len(calls) == 2
