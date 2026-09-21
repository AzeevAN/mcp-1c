"""Dashboard общей справки остаётся только read-only."""

from __future__ import annotations

from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry

from reference_fixture import SyntheticReferenceSigner, build_reference_database


def _reference(tmp_path):
    signer = SyntheticReferenceSigner.generate()
    artifact = signer.build(
        tmp_path / "release" / "reference.mcp1cref",
        build_reference_database(tmp_path / "source.sqlite3"),
    )
    return ReferenceService.discover(
        tmp_path / "data",
        embedded_path=artifact,
        verifier=signer.verifier(),
    )


def _client(tmp_path, reference):
    return живой_клиент(Starlette(routes=routes(
        Registry(tmp_path / "registry"),
        mode=DASHBOARD_ON,
        reference=reference,
    )))


def test_справка_доступна_для_чтения(tmp_path):
    client = _client(tmp_path, _reference(tmp_path))
    status = client.get("/api/v1/reference")
    search = client.get("/api/v1/reference/search", params={"query": "образец"})
    item = client.get("/api/v1/reference/item", params={"item_id": "bsl/Example"})
    assert status.status_code == 200
    assert search.status_code == 200
    assert item.status_code == 200


def test_загрузка_и_удаление_справки_отсутствуют(tmp_path):
    client = _client(tmp_path, _reference(tmp_path))
    assert client.post("/api/v1/reference/upload").status_code == 404
    assert client.post("/api/v1/reference/remove").status_code == 404
