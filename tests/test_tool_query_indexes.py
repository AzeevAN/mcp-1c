"""Карточки, подсказки и индексы запросов сохраняют наблюдаемый контракт.

До изменения production-кода verifier снимает baseline f217fa5 явным
MCP1C_CAPTURE_QUERY_GOLDEN=<private JSON path>. Затем артефакт вручную
интегрируется в tests/fixtures/tool_query_outputs_f217fa5.json.
Без этого флага golden-тест только читает готовую фикстуру и не обновляет её.

Ограничение вида проверяется на _similar_address для неизвестного модуля.
Существующие 40 random-тестов generic _close_matches и проверка ничьих
остаются без изменений: generic helper по-прежнему эквивалентен difflib.
"""

import difflib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp1c import tools, xdto
from mcp1c.model import Configuration
from mcp1c.registry import Registry
from test_intake_v2_converter import _collection
from test_intake_v2_generation import _materialized, _stage_b_generation
from test_xdto import _package


@pytest.fixture
def реестр_xdto_для_карточек(tmp_path):
    collection = _collection(tmp_path / "source-xdto", xdto_packages=True)
    materialized = _materialized(tmp_path, "query-xdto", collection)
    registry = Registry(tmp_path / "data-xdto")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    return registry


def _ответы_для_golden(реестр_с_кодом, реестр_xdto_для_карточек):
    """Только реальные вызовы; эталон не воспроизводит алгоритмы tools."""
    код = реестр_с_кодом
    ответы = {}
    for объект in ("Документ.Пример", "Справочник.Пример", "ОбщийМодуль.ОбщийПример"):
        for detail in ("brief", "fields", "full"):
            ответы[f"get_object:{объект}:{detail}"] = tools.get_object(
                код, объект, config="Пример", detail=detail
            )
    for адрес in (
        "ОбщийМодуль.ОбщийПример",
        "общиймодуль.общийпример::сложить",
        "ОбщийМодуль.ОбщийПример::Внутренняя",
        "ОбщийМодуль.ОбщийПример::Сложыть",
    ):
        ответы[f"get_procedure:{адрес}"] = tools.get_procedure(
            код, адрес, config="Пример"
        )
    for адрес in (
        "ОбщийМодуль.ОбщийПример::Сложить",
        "общиймодуль.общийпример::сложить",
        "Справочник.Пример.Форма.ФормаЭлемента::ПриОткрытии",
    ):
        ответы[f"get_callers:{адрес}"] = tools.get_callers(
            код, адрес, config="Пример"
        )
    for адрес in (
        "ПакетXDTO.Main",
        "ПакетXDTO.Main.ТипОбъекта.Item",
        "ПакетXDTO.Main.ТипЗначения.Status",
        "ПакетXDTO.Main.Свойство.Global",
    ):
        ответы[f"xdto:{адрес}"] = tools.get_object(
            реестр_xdto_для_карточек, адрес,
            config="DemoConfiguration", detail="full",
        )
    return ответы


def test_выдача_совпадает_с_побайтным_baseline_f217fa5(
    реестр_с_кодом, реестр_xdto_для_карточек
):
    ответы = _ответы_для_golden(реестр_с_кодом, реестр_xdto_для_карточек)
    assert len(ответы) == 20
    временные_корни = (
        str(реестр_с_кодом.data_dir.parent),
        str(реестр_xdto_для_карточек.data_dir.parent),
    )
    # Никаких нормализующих replace: если временный путь/generation попал
    # в выдачу, сначала выясняем источник нестабильности baseline.
    for ключ, текст in ответы.items():
        assert isinstance(текст, str) and текст, ключ
        assert "/Users/" not in текст and "/tmp/" not in текст, ключ
        assert all(корень not in текст for корень in временные_корни), ключ
    capture = os.environ.get("MCP1C_CAPTURE_QUERY_GOLDEN")
    if capture:
        target = Path(capture)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Явный baseline-прогон не может молча переписать прежний эталон.
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                {"baseline": "f217fa5", "outputs": ответы}, stream,
                ensure_ascii=False, sort_keys=True, indent=2,
            )
            stream.write("\n")
        return
    target = Path(__file__).parent / "fixtures" / "tool_query_outputs_f217fa5.json"
    assert target.is_file(), "Сначала снимите и интегрируйте baseline f217fa5"
    golden = json.loads(target.read_text(encoding="utf-8"))
    assert golden["baseline"] == "f217fa5"
    assert ответы.keys() == golden["outputs"].keys()
    for ключ, текст in ответы.items():
        assert текст.encode("utf-8") == golden["outputs"][ключ].encode("utf-8"), ключ


@pytest.mark.parametrize("detail", ["fields", "full"])
def test_warm_get_object_не_перебирает_чужие_модули_и_формы(
    реестр_с_кодом, monkeypatch, detail
):
    registry = реестр_с_кодом
    адрес = "Справочник.Пример"
    expected = tools.get_object(registry, адрес, config="Пример", detail=detail)
    assert "Код объекта" in expected or "Формы объекта" in expected
    вызовы = []
    original_belongs = tools._belongs_to_object
    original_is_form = tools._is_form_module

    def belongs(module, owner):
        вызовы.append(("принадлежность", module))
        return original_belongs(module, owner)

    def is_form(module):
        вызовы.append(("форма", module))
        return original_is_form(module)

    monkeypatch.setattr(tools, "_belongs_to_object", belongs)
    monkeypatch.setattr(tools, "_is_form_module", is_form)
    assert tools.get_object(registry, адрес, config="Пример", detail=detail) == expected
    assert вызовы == [], "Прогретая карточка не должна заново классифицировать корпус"


class _СчётчикПереборов(list):
    def __init__(self, values):
        super().__init__(values)
        self.переборов = 0

    def __iter__(self):
        self.переборов += 1
        return super().__iter__()


@pytest.mark.parametrize("инструмент", [tools.get_procedure, tools.get_callers])
def test_точный_адрес_без_учёта_регистра_не_перебирает_таблицы_модулей(
    реестр_с_кодом, monkeypatch, инструмент
):
    registry = реестр_с_кодом
    адрес = "ОБЩИЙМОДУЛЬ.ОБЩИЙПРИМЕР::сЛоЖиТь"
    expected = инструмент(registry, адрес, config="Пример")
    loaded = registry.resolve("Пример").modules
    assert loaded is not None
    assert "не найден" not in expected and "нет процедуры" not in expected
    toc_modules = _СчётчикПереборов(loaded.оглавление.модули)
    call_modules = _СчётчикПереборов(loaded.вызовы.модули)
    # Замена после конструктора и прогрева: проверяем работу запроса,
    # не выбирая имя/устройство будущего производного индекса.
    monkeypatch.setattr(loaded.оглавление, "модули", toc_modules)
    monkeypatch.setattr(loaded.вызовы, "модули", call_modules)
    assert инструмент(registry, адрес, config="Пример") == expected
    assert toc_modules.переборов == 0
    assert call_modules.переборов == 0


def test_xdto_не_сортирует_объекты_других_видов():
    configuration = Configuration()
    main = _package("ПакетXDTO.Main", "urn:main")
    for package in (
        _package("ПакетXDTO.Z", "urn:shared", object_types=["Thing"]),
        _package("ПакетXDTO.a", "urn:shared", object_types=["Thing"]),
        main,
    ):
        configuration.objects[package.full_name] = package
    payload = (
        '<package xmlns="http://v8.1c.ru/8.1/xdto" xmlns:s="urn:shared" '
        'targetNamespace="urn:main"><import namespace="urn:shared"/>'
        '<property name="Shared" type="s:Thing"/></package>'
    ).encode()
    expected = xdto.member_details(payload, main, configuration)
    assert [reference.target for reference in expected.references] == [
        "ПакетXDTO.a.ТипОбъекта.Thing"
    ]

    class НеПакет:
        kind = "Справочник"

        @property
        def full_name(self):
            raise AssertionError("Сортировка XDTO не должна читать имя справочника")

    configuration.objects["Справочник.Непакет"] = НеПакет()
    assert xdto.member_details(payload, main, configuration) == expected


def _oracle_подсказок(word, candidates, *, n, cutoff):
    """Спека меняет набор; оценка, cutoff и ничьи остаются у stdlib."""
    kind = word.partition(".")[0].casefold()
    same_kind = [
        candidate for candidate in candidates
        if candidate.partition(".")[0].casefold() == kind
    ]
    return difflib.get_close_matches(word, same_kind or candidates, n=n, cutoff=cutoff)


@pytest.mark.parametrize("word", ["Документ.Мя", "документ.Мя", "НеизвестныйВид.Мя"])
def test_подсказки_ограничивают_вид_а_при_его_отсутствии_берут_все(word, monkeypatch):
    candidates = [
        "Справочник.Мя", "Документ.Ма", "Документ.Мб", "Документ.Мв",
        "ОбщийМодуль.Мя", "Документ.Мг", "Документ.Мд", "Документ.Ме",
    ]
    expected = _oracle_подсказок(word, candidates, n=5, cutoff=0.45)
    loaded = SimpleNamespace(оглавление=SimpleNamespace(модули=candidates))
    real_matcher = difflib.SequenceMatcher
    scored = []

    class СчётчикКандидатов(real_matcher):
        def set_seq1(self, value):
            if value:
                scored.append(value)
            return super().set_seq1(value)

    monkeypatch.setattr(tools.difflib, "SequenceMatcher", СчётчикКандидатов)
    assert tools._similar_address(loaded, word, None) == expected
    assert scored
    if word.partition(".")[0].casefold() == "документ":
        assert all(candidate.startswith("Документ.") for candidate in scored)
    else:
        assert set(candidates) <= set(scored)


@pytest.mark.parametrize("инструмент", [tools.get_procedure, tools.get_callers])
def test_неизвестный_модуль_предлагает_адреса_того_же_вида(
    реестр_с_кодом, инструмент
):
    registry = реестр_с_кодом
    module = "Документ.Примр.МодульОбъекта"
    loaded = registry.resolve("Пример").modules
    expected = _oracle_подсказок(
        module, loaded.оглавление.модули, n=5, cutoff=0.45
    )
    assert expected == ["Документ.Пример.МодульОбъекта"]
    response = инструмент(registry, f"{module}::ПриЗаписи", config="Пример")
    suggestions = [line[3:-1] for line in response.splitlines() if line.startswith("- `")]
    assert suggestions == expected
