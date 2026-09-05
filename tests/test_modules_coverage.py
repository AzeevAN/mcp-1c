"""Единое публичное покрытие основной конфигурации и расширения."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from module_samples import v8_container_bytes
from mcp1c import cli, tools
from mcp1c.form_reader import MAX_BYTES
from mcp1c.registry import STATUS_ERROR


def _write(root: Path, relative: str, payload: bytes | str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.encode() if isinstance(payload, str) else payload)


def _container(*, module: bytes = b"", form: bytes = b"{19}") -> bytes:
    return v8_container_bytes([("module", module), ("form", form)])


def _corpus(root: Path, variant: str) -> None:
    if variant == "unknown_marker":
        _write(root, "CommonForm.Ограниченная.Form", _container(form=b"{99}"))
    elif variant == "broken_container":
        _write(root, "CommonForm.Ограниченная.Form", b"broken")
    elif variant == "unsupported_name":
        _write(root, "Unknown.Ограниченная.Form", _container())
    elif variant == "budget_exceeded":
        form = b"{19}" + b" " * (MAX_BYTES - 3)
        assert len(form) == MAX_BYTES + 1
        _write(root, "CommonForm.Ограниченная.Form", _container(form=form))
    elif variant == "conflict":
        _write(
            root,
            "CommonForm.Ограниченная.Form.Module.txt",
            "Процедура ИзФайла()\nКонецПроцедуры",
        )
        _write(
            root,
            "CommonForm.Ограниченная.Form",
            _container(
                module="Процедура ИзКонтейнера()\nКонецПроцедуры".encode(),
            ),
        )
    else:  # pragma: no cover — ошибка самого параметризованного теста
        raise AssertionError(variant)


@pytest.mark.parametrize("extension", [None, "Доп"])
@pytest.mark.parametrize(
    ("variant", "field", "category"),
    [
        ("broken_container", "broken_containers", "broken_container"),
        ("unsupported_name", "unsupported_addresses", "unknown_address"),
        ("budget_exceeded", "budget_exceeded", "budget_exceeded"),
        ("conflict", "body_conflicts", "conflict"),
    ],
)
def test_ограничения_имеют_точный_агрегат_в_обоих_корпусах(
    tmp_path, реестр_из_кода, extension, variant, field, category
):
    root = tmp_path / "code"
    root.mkdir()
    _corpus(root, variant)
    registry = реестр_из_кода(root, extension=extension)

    snapshot = tools.sources_snapshot(registry)
    corpus = "Основная конфигурация" if extension is None else "Расширение Доп"
    row = next(item for item in snapshot.code if item.corpus == corpus)
    coverage = row.coverage

    assert coverage is not None
    assert getattr(coverage, field) == 1
    assert coverage.has_limitations
    assert row.state.startswith("готов с ограничениями")
    assert category in {problem.category for problem in coverage.problems}
    assert coverage.problems_total >= 1
    assert coverage.problems_omitted == max(
        0, coverage.problems_total - len(coverage.problems)
    )
    if variant == "unsupported_name":
        problem = next(item for item in coverage.problems if item.category == category)
        assert problem.address is None
        assert problem.ordinal > 0
        assert "Unknown" not in problem.reason
    else:
        assert any(
            problem.address == "ОбщаяФорма.Ограниченная"
            for problem in coverage.problems
        )


@pytest.mark.parametrize("extension", [None, "Доп"])
def test_неизвестный_маркер_виден_как_отложенная_семантика_без_ошибки(
    tmp_path, реестр_из_кода, extension
):
    root = tmp_path / "code"
    root.mkdir()
    _corpus(root, "unknown_marker")
    registry = реестр_из_кода(root, extension=extension)

    snapshot = tools.sources_snapshot(registry)
    corpus = "Основная конфигурация" if extension is None else "Расширение Доп"
    row = next(item for item in snapshot.code if item.corpus == corpus)
    coverage = row.coverage

    assert coverage is not None
    assert coverage.unknown_markers == 1
    assert coverage.form_structures_partial == 1
    assert coverage.form_modules_empty == 1
    assert not coverage.has_limitations
    assert row.state.startswith("готов —")
    assert coverage.problems_total == 0
    assert "семантика отложена 1" in "\n".join(
        tools.code_coverage_lines(coverage)
    )


def test_отсутствующий_модуль_формы_виден_как_норма(tmp_path, реестр_из_кода):
    root = tmp_path / "code"
    root.mkdir()
    _write(
        root,
        "CommonForm.БезМодуля.Form",
        v8_container_bytes([("form", b"{99}")]),
    )
    registry = реестр_из_кода(root)

    coverage = tools.sources_snapshot(registry).code[0].coverage

    assert coverage is not None
    assert coverage.modules_total == 0
    assert coverage.form_modules_missing == 1
    assert coverage.form_structures_partial == 1
    assert coverage.problems_total == 0
    assert not coverage.has_limitations


def test_пустой_модуль_формы_входит_в_общий_счётчик_пустых(
    tmp_path, реестр_из_кода
):
    root = tmp_path / "code"
    root.mkdir()
    _write(root, "CommonForm.Пустая.Form", _container(form=b"{99}"))
    registry = реестр_из_кода(root)

    coverage = tools.sources_snapshot(registry).code[0].coverage

    assert coverage is not None
    assert coverage.modules_total == 1
    assert coverage.modules_empty == 1
    assert coverage.modules_source_available == 0
    assert coverage.form_modules_empty == 1
    assert not coverage.has_limitations


def test_счётчики_структуры_и_модуля_формы_образуют_точные_разбиения(
    tmp_path, реестр_из_кода
):
    root = tmp_path / "code"
    root.mkdir()
    _write(root, "CommonForms/Полная/Ext/Form.xml", "<Form/>")
    _write(root, "CommonForms/Частичная/Ext/Form.bin", _container(form=b"{19}"))
    _write(root, "CommonForms/Непрочитанная/Ext/Form.bin", b"broken")
    registry = реестр_из_кода(root)

    coverage = tools.sources_snapshot(registry).code[0].coverage

    assert coverage.forms_total == 3
    assert (
        coverage.form_structures_full
        + coverage.form_structures_partial
        + coverage.form_structures_unread
    ) == coverage.forms_total
    assert (
        coverage.form_modules_read
        + coverage.form_modules_empty
        + coverage.form_modules_missing
        + coverage.form_modules_unread
    ) == coverage.forms_total


def test_публичный_список_ограничен_двадцатью_и_сохраняет_точный_остаток(
    tmp_path, реестр_из_кода
):
    root = tmp_path / "code"
    root.mkdir()
    for index in range(25):
        _write(
            root,
            f"CommonForm.Форма{index:02d}.Form",
            _container(form=b"{,19}"),
        )
    registry = реестр_из_кода(root)

    coverage = tools.sources_snapshot(registry).code[0].coverage

    assert coverage.problems_total == 25
    assert len(coverage.problems) == 20
    assert coverage.problems_omitted == 5
    assert [problem.address for problem in coverage.problems] == [
        f"ОбщаяФорма.Форма{index:02d}" for index in range(20)
    ]


@pytest.mark.parametrize("detail", ["fields", "full"])
def test_get_object_показывает_форму_и_её_ограничение_выше_списка(
    tmp_path, реестр_из_кода, detail
):
    root = tmp_path / "code"
    root.mkdir()
    _write(
        root,
        "Catalog.Контрагенты.Form.Ограниченная.Form",
        _container(form=b"{,19}"),
    )
    registry = реестр_из_кода(root)

    answer = tools.get_object(
        registry, "Справочник.Контрагенты", config="Пример", detail=detail
    )

    warning = answer.index("Покрытие форм объекта неполно")
    form = answer.index("Справочник.Контрагенты.Форма.Ограниченная")
    assert warning < form
    assert "invalid_syntax" in answer
    assert "Форм нет" not in answer


def test_get_object_не_выдаёт_ноль_форм_за_доказанное_отсутствие(
    tmp_path, реестр_из_кода
):
    root = tmp_path / "code"
    root.mkdir()
    _write(root, "Unknown.Неадресуемая.Form", _container())
    registry = реестр_из_кода(root)

    answer = tools.get_object(
        registry, "Справочник.Контрагенты", config="Пример", detail="full"
    )

    assert "неподдержанных адресов: 1" in answer
    assert answer.index("Список форм объекта может быть неполон") < answer.index(
        "Форм нет"
    )
    assert "Unknown.Неадресуемая.Form" not in answer


@pytest.mark.parametrize("extension", [None, "Доп"])
def test_инструменты_кода_ставят_предупреждение_выше_частичного_ответа(
    tmp_path, реестр_из_кода, extension
):
    root = tmp_path / "code"
    root.mkdir()
    body = "Процедура Обработать() Экспорт\nКонецПроцедуры\n"
    _write(
        root,
        "CommonForm.Ограниченная.Form",
        _container(module=body.encode(), form=b"{,19}"),
    )
    registry = реестр_из_кода(root, extension=extension)
    kwargs = {"config": "Пример", "extension": extension}

    answers = (
        tools.search_procedures(registry, "Обработать", **kwargs),
        tools.get_procedure(
            registry, "ОбщаяФорма.Ограниченная::Обработать", **kwargs
        ),
        tools.get_callers(
            registry, "ОбщаяФорма.Ограниченная::Обработать", **kwargs
        ),
    )

    for answer in answers:
        assert answer.index("Покрытие кода неполно") < answer.index(
            "ОбщаяФорма.Ограниченная"
        )
        assert "invalid_syntax=1" in answer


def test_get_related_предупреждает_о_неполном_корпусе_расширения(
    tmp_path, реестр_из_кода
):
    root = tmp_path / "code"
    root.mkdir()
    _write(root, "CommonForm.Ограниченная.Form", _container(form=b"{,19}"))
    registry = реестр_из_кода(root, extension="Доп")

    answer = tools.get_related(
        registry, "Справочник.Контрагенты", config="Пример"
    )

    assert "Покрытие кода расширения `Доп` неполно" in answer
    assert "invalid_syntax=1" in answer


def test_инструменты_кода_называют_точную_категорию_битой_структуры(
    tmp_path, реестр_из_кода
):
    root = tmp_path / "code"
    root.mkdir()
    body = "Процедура Обработать() Экспорт\nКонецПроцедуры\n"
    _write(
        root,
        "CommonForm.Ограниченная.Form",
        _container(module=body.encode(), form=b"{,19}"),
    )
    registry = реестр_из_кода(root)

    answers = (
        tools.search_procedures(registry, "Обработать", config="Пример"),
        tools.get_procedure(
            registry,
            "ОбщаяФорма.Ограниченная::Обработать",
            config="Пример",
        ),
        tools.get_callers(
            registry,
            "ОбщаяФорма.Ограниченная::Обработать",
            config="Пример",
        ),
    )

    for answer in answers:
        assert "invalid_syntax=1" in answer
        assert "структуры форм: полностью 0, семантика отложена 0, не прочитано 1" in answer
        assert answer.index("invalid_syntax=1") < answer.index(
            "ОбщаяФорма.Ограниченная"
        )


def test_короткое_предупреждение_не_материализует_полный_список_проблем(
    tmp_path, реестр_из_кода, monkeypatch
):
    root = tmp_path / "code"
    root.mkdir()
    _write(root, "CommonForm.Ограниченная.Form", _container(form=b"{,19}"))
    registry = реестр_из_кода(root)

    def fail(_loaded):
        raise AssertionError("полный список проблем не нужен")

    monkeypatch.setattr(tools, "_all_code_problems", fail)

    answer = tools.search_procedures(registry, "Нет", config="Пример")

    assert "invalid_syntax=1" in answer


def test_get_related_называет_точную_категорию_битой_структуры_расширения(
    tmp_path, реестр_из_кода
):
    root = tmp_path / "code"
    root.mkdir()
    _write(root, "CommonForm.Ограниченная.Form", _container(form=b"{,19}"))
    registry = реестр_из_кода(root, extension="Доп")

    answer = tools.get_related(
        registry, "Справочник.Контрагенты", config="Пример"
    )

    assert "Покрытие кода расширения `Доп` неполно" in answer
    assert "invalid_syntax=1" in answer
    assert "структуры форм: полностью 0, семантика отложена 0, не прочитано 1" in answer


@pytest.mark.parametrize("action", ["reparse", "remove"])
def test_публичный_снимок_не_смешивает_проблемы_двух_поколений(
    tmp_path, реестр_из_кода, архив_кода, monkeypatch, action
):
    old_root = tmp_path / "old"
    old_root.mkdir()
    _corpus(old_root, "unknown_marker")
    registry = реестр_из_кода(old_root)
    new_root = tmp_path / "new"
    new_root.mkdir()
    _corpus(new_root, "unsupported_name")
    new_archive = архив_кода(new_root)
    real = tools._summarize_code
    changed = False

    def summarize(loaded):
        nonlocal changed
        result = real(loaded)
        if not changed:
            changed = True
            if action == "reparse":
                registry.add_modules(new_archive, configuration="Пример")
            else:
                registry.remove("Пример:modules")
        return result

    monkeypatch.setattr(tools, "_summarize_code", summarize)

    snapshot = tools.sources_snapshot(registry)

    if action == "reparse":
        row = snapshot.code[0]
        assert row.coverage is not None
        assert row.coverage.unknown_markers == 0
        assert row.coverage.unsupported_addresses == 1
        assert {item.category for item in row.coverage.problems} == {
            "unknown_address"
        }
    else:
        assert snapshot.code == ()
        assert snapshot.configuration_names == ()


def test_категория_ограничения_пишется_в_журнал_один_раз_без_адреса(
    tmp_path, реестр_из_кода, caplog
):
    root = tmp_path / "code"
    root.mkdir()
    for name in ("Первая", "Вторая"):
        _write(root, f"CommonForm.{name}.Form", _container(form=b"{,19}"))
    caplog.set_level(logging.WARNING, logger="mcp1c.registry")

    реестр_из_кода(root)

    rows = [
        record.getMessage()
        for record in caplog.records
        if "категория=invalid_syntax" in record.getMessage()
    ]
    assert len(rows) == 1
    assert "количество=2" in rows[0]
    assert "ОбщаяФорма" not in rows[0]
    assert str(root) not in rows[0]


@pytest.mark.parametrize(
    "call",
    [
        lambda registry: tools.search_procedures(
            registry, "Проверить", config="Пример"
        ),
        lambda registry: tools.get_procedure(
            registry, "ОбщийМодуль.Пример", config="Пример"
        ),
        lambda registry: tools.get_callers(
            registry, "ОбщийМодуль.Пример::Проверить", config="Пример"
        ),
    ],
)
@pytest.mark.parametrize(
    "raw_error",
    [
        "Permission denied: /private/secret/Module.bsl",
        "Traceback (most recent call last):\nValueError: secret",
        "[Errno 13] Permission denied: secret.Form",
    ],
)
def test_ошибка_индекса_в_инструментах_кода_обезличена(
    реестр_из_кода, корень_кода, call, raw_error
):
    registry = реестр_из_кода(корень_кода)
    loaded = registry.resolve("Пример").modules
    with registry._lock:
        loaded.готов = False
        loaded.source.status = STATUS_ERROR
        loaded.source.error = raw_error

    answer = call(registry)

    assert "подробности ошибки доступны в журнале сервера" in answer
    assert "Traceback" not in answer
    assert "Permission denied" not in answer
    assert "/private/" not in answer
