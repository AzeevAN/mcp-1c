"""``Registry.resolve`` возвращает один identity-снимок всех провайдеров."""

from __future__ import annotations

import threading

import pytest

from mcp1c.registry import RegistryError
from mcp1c.store import save_syntax

from conftest import build_configuration, build_syntax, write_export


def _модуль(корень, маркер: str):
    каталог = корень / "CommonModules" / "Снимок" / "Ext"
    каталог.mkdir(parents=True, exist_ok=True)
    (каталог / "Module.bsl").write_text(
        f"Процедура {маркер}() Экспорт\nКонецПроцедуры\n", encoding="utf-8"
    )


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
def test_resolve_не_смешивает_старую_конфигурацию_с_новым_кодом(
    корень_кода,
    реестр_из_кода,
    архив_кода,
    monkeypatch,
    tmp_path,
    extension,
):
    _модуль(корень_кода, "СтарыйКод")
    старая = build_configuration(name="Пример", version="1.0")
    реестр = реестр_из_кода(
        корень_кода, extension=extension, configuration=старая
    )

    новая = build_configuration(name="Пример", version="2.0")
    каталог_метаданных = tmp_path / "new-config"
    каталог_метаданных.mkdir()
    новый_config = write_export(каталог_метаданных, новая)
    _модуль(корень_кода, "НовыйКод")
    новый_код = архив_кода(корень_кода, extension=extension)

    начато = threading.Event()
    отпустить = threading.Event()
    настоящее = реестр._compute_relation
    первый = True

    def задержать(*args):
        nonlocal первый
        if первый:
            первый = False
            начато.set()
            отпустить.wait(timeout=3)
        return настоящее(*args)

    monkeypatch.setattr(реестр, "_compute_relation", задержать)
    контексты = []
    ошибки = []

    def разрешить():
        try:
            контексты.append(реестр.resolve("Пример", extension=extension))
        except BaseException as error:
            ошибки.append(error)

    поток = threading.Thread(target=разрешить)
    поток.start()
    try:
        assert начато.wait(timeout=1)
        реестр.remove("Пример")
        реестр.add_configuration(новый_config)
        реестр.add_modules(новый_код, configuration="Пример")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive() and not ошибки
    context = контексты[0]
    assert context.configuration is реестр.configurations["Пример"]
    assert context.configuration.config.version == "2.0"
    selected = context.extension if extension else context.modules
    source_id = "Пример:ext:Доп" if extension else "Пример:modules"
    assert selected is реестр.modules[source_id]
    assert selected.source is реестр.sources[source_id]


def test_resolve_не_смешивает_старое_отношение_с_новой_справкой(
    корень_кода, реестр_из_кода, monkeypatch, tmp_path
):
    реестр = реестр_из_кода(корень_кода)
    справки = tmp_path / "syntax"
    справки.mkdir()
    реестр.add_syntax(save_syntax(build_syntax("8.3.5.1570"), справки / "8.3.5.json.gz"))

    начато = threading.Event()
    отпустить = threading.Event()
    настоящее = реестр._compute_relation
    первый = True

    def задержать(*args):
        nonlocal первый
        if первый:
            первый = False
            начато.set()
            отпустить.wait(timeout=3)
        return настоящее(*args)

    monkeypatch.setattr(реестр, "_compute_relation", задержать)
    контексты = []
    поток = threading.Thread(
        target=lambda: контексты.append(реестр.resolve("Пример"))
    )
    поток.start()
    try:
        assert начато.wait(timeout=1)
        реестр.add_syntax(
            save_syntax(build_syntax("8.3.27.2130"), справки / "8.3.27.json.gz")
        )
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    context = контексты[0]
    assert context.syntax is реестр.syntax
    assert (context.syntax_relation, context.syntax_hidden) == реестр._compute_relation(
        context.configuration, context.syntax
    )


def test_resolve_две_последовательные_смены_дают_стабильный_отказ(
    корень_кода, реестр_из_кода, monkeypatch, tmp_path
):
    реестр = реестр_из_кода(корень_кода)
    настоящее = реестр._compute_relation
    замены = []
    for номер in (2, 3):
        каталог = tmp_path / f"config-{номер}"
        каталог.mkdir()
        замены.append(
            write_export(
                каталог, build_configuration(name="Пример", version=f"{номер}.0")
            )
        )
    вызовов = 0

    def менять(*args):
        nonlocal вызовов
        результат = настоящее(*args)
        реестр.add_configuration(замены[вызовов])
        вызовов += 1
        return результат

    monkeypatch.setattr(реестр, "_compute_relation", менять)

    with pytest.raises(RegistryError, match="изменились.*повторите"):
        реестр.resolve("Пример")

    assert вызовов == 2


def test_resolve_считает_отношение_версий_без_замка(
    корень_кода, реестр_из_кода, monkeypatch
):
    реестр = реестр_из_кода(корень_кода)
    настоящее = реестр._compute_relation
    проверено = []

    def проверить(*args):
        свободен = реестр._lock.acquire(blocking=False)
        проверено.append(свободен)
        if свободен:
            реестр._lock.release()
        return настоящее(*args)

    monkeypatch.setattr(реестр, "_compute_relation", проверить)

    реестр.resolve("Пример")

    assert проверено == [True]
