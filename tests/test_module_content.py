"""Контракт локаторов: содержимое не выводится обратно из публичного адреса."""

from __future__ import annotations

import marshal
from random import Random
from types import SimpleNamespace

import pytest

from module_samples import v8_container_bytes
from mcp1c.module_content import (
    ContentReadError,
    LocatorEnvelope,
    LocatorIdentity,
    ModuleLocator,
    read_bsl,
    read_content_bytes,
)
from mcp1c import tools
from mcp1c.modules_index import Формы


def test_файл_и_запись_контейнера_проходят_один_нормализатор(tmp_path):
    raw = b"\xef\xbb\xbfProcedure A()\r\nEndProcedure\rProcedure B()\r\nEndProcedure"
    (tmp_path / "Module.bsl").write_bytes(raw)
    (tmp_path / "Form.bin").write_bytes(v8_container_bytes([("module", raw)]))

    file_text = read_bsl(
        tmp_path,
        "ОбщийМодуль.Пример",
        ModuleLocator.file("Module.bsl"),
    )
    container_text = read_bsl(
        tmp_path,
        "Документ.Пример.Форма.Основная.Модуль",
        ModuleLocator.container("Form.bin", "module"),
    )

    assert file_text == container_text
    assert file_text == "Procedure A()\nEndProcedure Procedure B()\nEndProcedure"


def test_пустая_запись_отличается_от_отсутствующей(tmp_path):
    (tmp_path / "Form.bin").write_bytes(v8_container_bytes([("module", b"")]))
    locator = ModuleLocator.container("Form.bin", "module")

    assert read_bsl(tmp_path, "Документ.Пример.Форма.Пустая.Модуль", locator) == ""

    with pytest.raises(ContentReadError) as caught:
        read_bsl(
            tmp_path,
            "Документ.Пример.Форма.БезМодуля.Модуль",
            ModuleLocator.container("Form.bin", "unknown"),
        )
    assert caught.value.category == "container_entry_missing"


def test_битый_контейнер_даёт_структурированную_обезличенную_ошибку(tmp_path):
    physical = tmp_path / "секретный-каталог" / "Form.bin"
    physical.parent.mkdir()
    physical.write_bytes(b"broken container")
    address = "Документ.Пример.Форма.Битая.Модуль"

    with pytest.raises(ContentReadError) as caught:
        read_bsl(
            tmp_path,
            address,
            ModuleLocator.container("секретный-каталог/Form.bin", "module"),
        )

    issue = caught.value
    assert issue.category == "container_unreadable"
    assert issue.address == address
    assert "секретный-каталог" not in str(issue)
    assert str(tmp_path) not in str(issue)
    assert "page_size" not in str(issue)


def test_пустой_контейнер_тоже_ограничивается_структурированной_ошибкой(tmp_path):
    (tmp_path / "empty.Form").write_bytes(b"")

    with pytest.raises(ContentReadError) as caught:
        read_bsl(
            tmp_path,
            "Документ.Пример.Форма.ПустойКонтейнер.Модуль",
            ModuleLocator.container("empty.Form", "module"),
        )

    assert caught.value.category == "container_unreadable"


@pytest.mark.parametrize("kind", ["file", "container"])
def test_symlink_локатора_не_может_прочитать_файл_за_корнем(tmp_path, kind):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / ("outside.Form" if kind == "container" else "outside.bsl")
    outside.write_bytes(
        v8_container_bytes([("module", b"outside")])
        if kind == "container"
        else b"outside"
    )
    link = root / outside.name
    link.symlink_to(outside)
    locator = (
        ModuleLocator.container(outside.name, "module")
        if kind == "container"
        else ModuleLocator.file(outside.name)
    )

    with pytest.raises(ContentReadError) as caught:
        read_bsl(root, "ОбщийМодуль.Безопасный", locator)

    assert caught.value.category in {"file_unreadable", "container_unreadable"}
    assert "outside" not in str(caught.value)


def test_неизвестная_запись_не_мешает_прочитать_соседний_контейнер(tmp_path):
    (tmp_path / "first.Form").write_bytes(v8_container_bytes([("form", b"{25}")]))
    (tmp_path / "second.Form").write_bytes(
        v8_container_bytes([("module", "Процедура А()\nКонецПроцедуры".encode())])
    )

    with pytest.raises(ContentReadError):
        read_bsl(
            tmp_path,
            "Документ.Пример.Форма.Первая.Модуль",
            ModuleLocator.container("first.Form", "module"),
        )

    assert read_bsl(
        tmp_path,
        "Документ.Пример.Форма.Вторая.Модуль",
        ModuleLocator.container("second.Form", "module"),
    ).startswith("Процедура А")


def test_дочитывание_сигнатур_и_тел_использует_локатор_каталога(
    tmp_path, monkeypatch
):
    address = "Документ.Пример.Форма.Основная.Модуль"
    locator = ModuleLocator.container("Form.bin", "module")
    (tmp_path / "Form.bin").write_bytes(
        v8_container_bytes([("module", "Процедура А()\nКонецПроцедуры".encode())])
    )
    identity = LocatorIdentity("Пример:modules", "a" * 64, 7)
    loaded = SimpleNamespace(
        корень=tmp_path,
        source=SimpleNamespace(
            id=identity.source_id,
            sha256=identity.source_sha256,
            locator_generation=identity.generation,
        ),
        каталог=SimpleNamespace(
            identity=identity,
            entries={address: SimpleNamespace(locator=locator)},
        ),
    )
    monkeypatch.setattr(
        tools,
        "прочитать_модуль",
        lambda *_: pytest.fail("путь нельзя выводить из публичного адреса"),
    )

    assert tools._прочитать_тело_модуля(loaded, address).startswith("Процедура А")


def test_локатор_сохраняется_без_корня_и_только_для_того_же_поколения(tmp_path):
    identity = LocatorIdentity("Конфигурация:modules", "a" * 64, 7)
    locator = ModuleLocator.container("Document.X.Form.Y.Form", "module")
    envelope = LocatorEnvelope(identity, {"Документ.X.Форма.Y.Модуль": locator})
    state = envelope.to_state()
    dumped = marshal.dumps(state)

    assert str(tmp_path).encode() not in dumped
    assert b"Procedure" not in dumped
    restored = LocatorEnvelope.from_state(state, identity)
    assert restored is not None
    assert restored.locators["Документ.X.Форма.Y.Модуль"] == locator
    assert LocatorEnvelope.from_state(
        state, LocatorIdentity(identity.source_id, identity.source_sha256, 8)
    ) is None
    assert LocatorEnvelope.from_state(
        state, LocatorIdentity(identity.source_id, "b" * 64, identity.generation)
    ) is None
    malformed = dict(state)
    malformed["identity"] = (identity.source_id, identity.source_sha256, True)
    assert LocatorEnvelope.from_state(malformed, identity) is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ModuleLocator.file("/absolute/Module.bsl"),
        lambda: ModuleLocator.file("../outside/Module.bsl"),
        lambda: ModuleLocator.file("bad\x00name.bsl"),
        lambda: ModuleLocator.file("bad\ud800name.bsl"),
        lambda: ModuleLocator.container("Form.bin", "../module"),
        lambda: ModuleLocator.container("Form.bin", "bad\x00entry"),
    ],
)
def test_локатор_не_принимает_небезопасный_путь(factory):
    with pytest.raises(ValueError):
        factory()


def test_повреждённый_nul_локатор_не_восстанавливается_из_кэша():
    identity = LocatorIdentity("source", "a" * 64, 1)
    state = {
        "identity": identity.to_state(),
        "locators": [("ОбщийМодуль.Пример", ("file", "bad\x00name.bsl", ""))],
    }

    assert LocatorEnvelope.from_state(state, identity) is None




def _m3_entropy(size):
    return Random(31).randbytes((size + 1) // 2).hex().encode("ascii")[:size]


def _m3_bsl(size):
    from module_samples import _deflate

    prefix, suffix = b"Procedure BudgetProbe()\n// ", b"\nEndProcedure"
    payload = prefix + _m3_entropy(size - len(prefix) - len(suffix)) + suffix
    assert len(payload) == size
    # Длинный комментарий остаётся валидным BSL и не попадает под прежний ratio cap.
    assert len(payload) <= 200 * len(_deflate(payload))
    return payload


def _m3_form(size):
    from module_samples import _deflate

    prefix, suffix = b'{19,"', b'"}'
    payload = prefix + _m3_entropy(size - len(prefix) - len(suffix)) + suffix
    assert len(payload) == size
    assert len(payload) <= 200 * len(_deflate(payload))
    return payload


def _m3_form_collection(tmp_path, *, form, module=None):
    from mcp1c.intake_v2_collector import collect_source_b
    from mcp1c.intake_v2_probe import probe_export
    from test_intake_v2_collector import _configuration
    from test_intake_v2_converter import MemoryTree

    entries = [("form", form)]
    if module is not None:
        entries.append(("module", module))
    tree = MemoryTree({
        "Configuration.xml": _configuration(),
        "CommonForm.Budget.Form": v8_container_bytes(entries),
    })
    return collect_source_b(tree, probe_export(tree), tmp_path / "collection")


def _m3_capture_decoder_output(monkeypatch):
    from mcp1c import v8container

    original = v8container.zlib.decompressobj
    decoders = []

    class Decoder:
        def __init__(self, *args, **kwargs):
            self.inner = original(*args, **kwargs)
            self.produced = 0
            decoders.append(self)

        def decompress(self, *args, **kwargs):
            result = self.inner.decompress(*args, **kwargs)
            self.produced += len(result)
            return result

        def flush(self, *args, **kwargs):
            result = self.inner.flush(*args, **kwargs)
            self.produced += len(result)
            return result

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(v8container.zlib, "decompressobj", Decoder)
    return decoders


@pytest.mark.parametrize("size", [883_773, 883_774, 883_773 + 65_536])
def test_embedded_bsl_ограничен_отдельным_пределом_формы(tmp_path, monkeypatch, size):
    payload = _m3_bsl(size)
    (tmp_path / "Form.bin").write_bytes(v8_container_bytes([("module", payload)]))
    locator = ModuleLocator.container("Form.bin", "module")
    decoders = _m3_capture_decoder_output(monkeypatch)

    if size <= 883_773:
        assert read_bsl(tmp_path, "ОбщаяФорма.Budget", locator) == payload.decode("ascii")
    else:
        with pytest.raises(ContentReadError) as caught:
            read_bsl(tmp_path, "ОбщаяФорма.Budget", locator)
        assert caught.value.category == "budget_exceeded"

    assert decoders
    assert all(decoder.produced <= 883_774 for decoder in decoders)


def test_обычный_bsl_сохраняет_предел_17_mib_выше_предела_form_module(tmp_path):
    payload = _m3_bsl(883_774)
    (tmp_path / "Module.bsl").write_bytes(payload)

    assert read_bsl(tmp_path, "ОбщийМодуль.Budget", ModuleLocator.file("Module.bsl")) == payload.decode("ascii")


@pytest.mark.parametrize("size", [5_263_508, 5_263_509, 5_263_508 + 65_536])
def test_form_content_и_индекс_соблюдают_предел_распакованной_form(tmp_path, monkeypatch, size):
    payload = _m3_form(size)
    (tmp_path / "CommonForm.Budget.Form").write_bytes(v8_container_bytes([("form", payload)]))
    (tmp_path / "CommonForm.Good.Form").write_bytes(v8_container_bytes([("form", b"{19}")]))
    locator = ModuleLocator.container("CommonForm.Budget.Form", "form")
    decoders = _m3_capture_decoder_output(monkeypatch)

    if size <= 5_263_508:
        assert read_content_bytes(tmp_path, "ОбщаяФорма.Budget", locator) == payload
    else:
        with pytest.raises(ContentReadError) as caught:
            read_content_bytes(tmp_path, "ОбщаяФорма.Budget", locator)
        assert caught.value.category == "budget_exceeded"

    forms = Формы.построить(tmp_path)
    assert not forms.состав("ОбщаяФорма.Good").битая
    if size <= 5_263_508:
        assert not forms.состав("ОбщаяФорма.Budget").битая
        assert forms.превышений_бюджета == 0
    else:
        assert forms.состав("ОбщаяФорма.Budget").битая
        assert forms.превышений_бюджета == 1
        assert any(problem.адрес == "ОбщаяФорма.Budget" and problem.категория == "budget_exceeded"
                   for problem in forms.проблемы)

    # Повторные чтения индексом создают новые decoder: бюджет проверяется для каждого.
    assert decoders
    assert all(decoder.produced <= 5_263_509 for decoder in decoders)
