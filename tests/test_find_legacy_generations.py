"""Диагностика старых поколений не запускает runtime и не меняет данные."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from mcp1c.intake_v2 import (
    CandidateTransport,
    ExportIdentity,
    GenerationManifest,
    LayerKind,
    LayerManifest,
    LayerState,
)
from mcp1c.intake_v2_converter import base_layer_data
from mcp1c.intake_v2_registry import (
    GenerationBundleStore,
    LayerPayload,
    LayerPayloadSource,
    hash_layer_payload,
    hash_layer_semantic,
)
from mcp1c.model import Configuration, Field, MetadataObject


SCRIPT = Path(__file__).resolve().parents[1] / "tools/lab/find_legacy_generations.py"
_spec = importlib.util.spec_from_file_location("find_legacy_generations", SCRIPT)
assert _spec is not None and _spec.loader is not None
diagnostic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diagnostic)


def _base() -> dict:
    obj = MetadataObject(
        "РегистрБухгалтерии.Учёт", "РегистрБухгалтерии", "Учёт",
        attributes=[Field("Реквизит", types=["Строка"])],
        dimensions=[Field("Измерение", types=["Строка"], balance=True)],
    )
    return base_layer_data(Configuration(name="Тест", objects={obj.full_name: obj}))


def _write_registry(data: Path, registry: dict) -> None:
    (data / "registry.json").write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")


def _bundle(
    tmp_path: Path, *, semantic: dict | None = None,
    extended: dict | None = None, identity: ExportIdentity | None = None,
    base_state: LayerState | None = LayerState.READY,
):
    data = tmp_path / "data"
    store = GenerationBundleStore(data)
    layers = []
    payloads = {}
    semantics = {}
    if base_state is LayerState.READY:
        semantics[LayerKind.BASE_STRUCTURE] = _base() if semantic is None else semantic
    elif base_state is not None:
        layers.append(LayerManifest(LayerKind.BASE_STRUCTURE, base_state))
    if extended is not None:
        semantics[LayerKind.EXTENDED_STRUCTURE] = extended
    for kind, value in semantics.items():
        payload = LayerPayload(kind, value)
        path = tmp_path / f"{kind.value}.json"
        path.write_bytes(payload.to_json_bytes())
        layers.append(LayerManifest(
            kind, LayerState.READY,
            content_sha256=hash_layer_semantic(kind, value),
            payload_sha256=hash_layer_payload(kind, path),
            relative_path=f"layers/{kind.value}.json",
        ))
        payloads[kind] = LayerPayloadSource(path)
    manifest = GenerationManifest(
        format_version=1,
        generation_id="generation-1",
        identity=identity or ExportIdentity.configuration("Тест"),
        parser_version=1,
        selection_version=1,
        source_transport=CandidateTransport.LOCAL_FILE,
        origin_name="source.zip",
        raw_sha256="a" * 64,
        layers=tuple(layers),
    )
    staged = store.stage(manifest, payloads)
    store.promote(staged)
    registry = {"registry_version": 1, "sources": [], "generation_manifests": [staged.pointer.to_dict()]}
    _write_registry(data, registry)
    return data, staged.pointer, registry


def _snapshot(data: Path) -> dict:
    # Имена каталогов тоже существенны: read-only сканер не создаёт lock/cache.
    return {
        path.relative_to(data).as_posix(): (
            ("symlink", str(path.readlink())) if path.is_symlink()
            else ("directory", None) if path.is_dir()
            else ("file", path.read_bytes())
        )
        for path in data.rglob("*")
    }


def _run(data: Path, capsys):
    before = _snapshot(data)
    code = diagnostic.main(["--data", str(data)])
    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    assert _snapshot(data) == before
    return code, json.loads(output.out)


def test_современное_поколение_проверено_без_изменения_данных(tmp_path, capsys):
    data, _, _ = _bundle(tmp_path)
    code, report = _run(data, capsys)
    assert code == 0
    assert report == {
        "schema_version": 1,
        "complete": True,
        "total_generations": 1,
        "checked_generations": 1,
        "field_records": 2,
        "legacy_field_records": 0,
        "legacy_generations": [],
        "retired_sources": [],
        "errors": [],
    }


def test_поколение_без_compatibility_mode_требует_перезагрузки(tmp_path, capsys):
    semantic = _base()
    del semantic["compatibility_mode"]
    data, pointer, _ = _bundle(tmp_path, semantic=semantic)
    code, report = _run(data, capsys)
    assert code == 1
    assert report["complete"] is True
    assert report["checked_generations"] == 1
    assert report["legacy_generations"] == [{
        "identity": pointer.identity.to_dict(),
        "generation_id": pointer.generation_id,
        "relative_path": pointer.root_path,
        "reasons": ["missing_compatibility_mode"],
        "affected_layers": ["base_structure"],
        "field_records": 2,
        "legacy_field_records": 0,
    }]


@pytest.mark.parametrize("collection", ["attributes", "dimensions"])
def test_старый_набор_полей_находится_без_runtime_normalization(tmp_path, capsys, collection):
    semantic = _base()
    del semantic["objects"][0][collection][0]["string_allowed_length"]
    data, _, _ = _bundle(tmp_path, semantic=semantic)
    code, report = _run(data, capsys)
    assert code == 1
    assert report["complete"] is True
    assert report["field_records"] == 2
    assert report["legacy_field_records"] == 1
    assert report["legacy_generations"][0]["reasons"] == ["legacy_field_shape"]


@pytest.mark.parametrize("legacy", [False, True])
def test_проверяется_также_структура_расширений_без_требования_compatibility(tmp_path, capsys, legacy):
    field = deepcopy(_base()["objects"][0]["attributes"][0])
    if legacy:
        del field["string_allowed_length"]
    data, _, _ = _bundle(tmp_path, extended={"objects": [{"attributes": [field]}]})
    code, report = _run(data, capsys)
    assert code == int(legacy)
    assert report["complete"] is True
    assert report["field_records"] == 3
    assert report["legacy_field_records"] == int(legacy)
    if legacy:
        assert report["legacy_generations"][0]["affected_layers"] == ["extended_structure"]
        assert report["legacy_generations"][0]["reasons"] == ["legacy_field_shape"]


@pytest.mark.parametrize("kind", ["modules", "extension", "extension-runtime"])
def test_устаревшая_строка_источника_попадает_в_отчёт(tmp_path, capsys, kind):
    data, _, registry = _bundle(tmp_path)
    registry["sources"] = [{"id": "старый", "kind": kind, "stored_path": "sources/old.zip"}]
    _write_registry(data, registry)
    code, report = _run(data, capsys)
    assert code == 1
    assert report["complete"] is True
    assert report["retired_sources"] == [{
        "identity": {"source_id": "старый", "source_kind": kind},
        "generation_id": None,
        "relative_path": "registry.json",
        "row_index": 0,
        "reasons": ["unsupported_source_kind"],
    }]


@pytest.mark.parametrize("base_state", [None, LayerState.UNAVAILABLE])
@pytest.mark.parametrize("legacy", [False, True])
def test_поколение_расширения_не_требует_базовой_структуры(
    tmp_path, capsys, base_state, legacy,
):
    field = deepcopy(_base()["objects"][0]["attributes"][0])
    if legacy:
        del field["string_allowed_length"]
    identity = ExportIdentity.extension("Расширение", parent_configuration="Тест")
    data, _, _ = _bundle(
        tmp_path, identity=identity, base_state=base_state,
        extended={"objects": [{"attributes": [field]}]},
    )
    code, report = _run(data, capsys)
    assert code == int(legacy)
    assert report["complete"] is True
    assert report["checked_generations"] == 1
    assert report["field_records"] == 1
    assert report["legacy_field_records"] == int(legacy)
    if legacy:
        finding = report["legacy_generations"][0]
        assert finding["identity"] == identity.to_dict()
        assert finding["reasons"] == ["legacy_field_shape"]
        assert finding["affected_layers"] == ["extended_structure"]


@pytest.mark.parametrize("base_state", [None, LayerState.UNAVAILABLE])
def test_расширение_без_структурных_слоёв_не_объявляется_устаревшим(
    tmp_path, capsys, base_state,
):
    identity = ExportIdentity.extension("Расширение", parent_configuration="Тест")
    data, _, _ = _bundle(tmp_path, identity=identity, base_state=base_state)
    code, report = _run(data, capsys)
    assert code == 0
    assert report["complete"] is True
    assert report["checked_generations"] == 1
    assert report["field_records"] == 0
    assert report["legacy_generations"] == []


@pytest.mark.parametrize("kind", ["configuration", "syntax"])
def test_внешний_путь_текущего_источника_не_читается_и_не_публикуется(
    tmp_path, capsys, kind,
):
    data, _, registry = _bundle(tmp_path)
    external = tmp_path / "external-index.gz"
    content = "Содержимое не требуется диагносту".encode("utf-8")
    external.write_bytes(content)
    registry["sources"] = [{"id": "current", "kind": kind, "stored_path": str(external)}]
    _write_registry(data, registry)
    code, report = _run(data, capsys)
    assert code == 0
    assert report["complete"] is True
    assert str(external) not in json.dumps(report)
    assert external.read_bytes() == content


def test_путь_устаревшего_источника_не_публикуется(tmp_path, capsys):
    data, _, registry = _bundle(tmp_path)
    external = str(tmp_path / "private-source.zip")
    registry["sources"] = [{"id": "old", "kind": "modules", "stored_path": external}]
    _write_registry(data, registry)
    code, report = _run(data, capsys)
    assert code == 1
    assert report["complete"] is True
    assert report["retired_sources"][0]["relative_path"] == "registry.json"
    assert report["retired_sources"][0]["row_index"] == 0
    assert external not in json.dumps(report)


@pytest.mark.parametrize("damage", [
    "registry_json", "registry_version", "sources_container", "source_row", "source_kind",
    "source_path", "pointers_container", "pointer_row", "pointer_path", "duplicate_pointer",
    "manifest_missing", "manifest_json", "payload_missing", "payload_json", "payload_hash",
])
def test_ошибка_не_превращается_в_пустой_успех(tmp_path, capsys, damage):
    data, pointer, registry = _bundle(tmp_path)
    root = data / pointer.root_path
    if damage == "registry_json":
        (data / "registry.json").write_bytes(b"{")
    elif damage == "registry_version":
        registry["registry_version"] = 2
    elif damage == "sources_container":
        registry["sources"] = {}
    elif damage == "source_row":
        registry["sources"] = [None]
    elif damage == "source_kind":
        registry["sources"] = [{"id": "x", "kind": "unknown"}]
    elif damage == "source_path":
        registry["sources"] = [{"id": "x", "kind": "modules", "stored_path": 42}]
    elif damage == "pointers_container":
        registry["generation_manifests"] = {}
    elif damage == "pointer_row":
        registry["generation_manifests"] = [None]
    elif damage == "pointer_path":
        registry["generation_manifests"][0]["root_path"] = "../outside"
    elif damage == "duplicate_pointer":
        registry["generation_manifests"].append(deepcopy(registry["generation_manifests"][0]))
    elif damage == "manifest_missing":
        (root / "manifest.json").unlink()
    elif damage == "manifest_json":
        (root / "manifest.json").write_bytes(b"{")
    elif damage == "payload_missing":
        (root / "layers/base_structure.json").unlink()
    elif damage == "payload_json":
        (root / "layers/base_structure.json").write_bytes(b"{")
    elif damage == "payload_hash":
        semantic = _base()
        semantic["version"] = "подмена"
        (root / "layers/base_structure.json").write_bytes(LayerPayload(LayerKind.BASE_STRUCTURE, semantic).to_json_bytes())
    else:
        raise AssertionError(damage)
    if damage != "registry_json":
        _write_registry(data, registry)
    code, report = _run(data, capsys)
    assert code == 2
    assert report["complete"] is False
    assert report["errors"]


def test_недоступный_payload_отмечается_как_неполная_проверка(tmp_path, capsys, monkeypatch):
    data, _, _ = _bundle(tmp_path)

    def denied(path):
        raise PermissionError("Закрытые данные не должны попасть в stdout")

    monkeypatch.setattr(diagnostic, "load_layer_payload", denied)
    code, report = _run(data, capsys)
    assert code == 2
    assert report["checked_generations"] == 0
    assert report["complete"] is False
    assert "Закрытые данные" not in json.dumps(report, ensure_ascii=False)


def test_проверяются_только_активные_указатели(tmp_path, capsys):
    data, _, registry = _bundle(tmp_path)
    registry["generation_manifests"] = []
    _write_registry(data, registry)
    code, report = _run(data, capsys)
    assert code == 0
    assert report["total_generations"] == 0
    assert report["checked_generations"] == 0


def test_несуществующий_каталог_не_создаётся(tmp_path, capsys):
    data = tmp_path / "нет-данных"
    assert diagnostic.main(["--data", str(data)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] is False
    assert report["errors"]
    assert not data.exists()


def test_относительный_data_работает_с_нормализованным_путём_store(
    tmp_path, capsys, monkeypatch,
):
    data, _, _ = _bundle(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        diagnostic, "GenerationBundleStore",
        lambda path: GenerationBundleStore(Path(path).resolve()),
    )
    code, report = _run(Path(data.name), capsys)
    assert code == 0
    assert report["complete"] is True
    assert report["checked_generations"] == 1


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_ошибка_создания_store_остаётся_json_ошибкой(
    tmp_path, capsys, monkeypatch, error_type,
):
    data, _, _ = _bundle(tmp_path)

    def unavailable(path):
        raise error_type("Закрытый путь не должен попасть в stdout")

    monkeypatch.setattr(diagnostic, "GenerationBundleStore", unavailable)
    code, report = _run(data, capsys)
    assert code == 2
    assert report["complete"] is False
    assert report["errors"]
    assert "Закрытый путь" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("args", [[], ["--data"], ["--unknown"]])
def test_ошибки_аргументов_дают_json_и_exit2(capsys, args):
    assert diagnostic.main(args) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] is False
    assert report["errors"]


@pytest.mark.parametrize("relative", [False, True])
def test_cli_работает_из_другого_каталога(tmp_path, relative):
    data, _, _ = _bundle(tmp_path)
    before = _snapshot(data)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--data", data.name if relative else str(data)],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["complete"] is True
    assert _snapshot(data) == before
