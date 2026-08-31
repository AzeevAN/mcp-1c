"""Read-only страница общей справки повторяет публичный контракт провайдера."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from reference_fixture import SyntheticReferenceSigner, build_reference_database


def _reference(tmp_path, *, body="Синтетическое описание для ручной проверки."):
    signer = SyntheticReferenceSigner.generate()
    database = build_reference_database(tmp_path / "source.sqlite3", body=body)
    signer.build(tmp_path / "data" / "reference" / "reference.mcp1cref", database)
    return ReferenceService.discover(
        tmp_path / "data", verifier=signer.verifier()
    )


def _client(tmp_path, reference, *, mode=DASHBOARD_ON):
    registry = Registry(tmp_path / "data")
    return живой_клиент(
        Starlette(routes=routes(registry, mode=mode, reference=reference))
    )


def test_reference_api_требует_токен_чтения(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    reference = ReferenceService.discover(tmp_path / "data")
    client = _client(tmp_path, reference, mode=DASHBOARD_ON)

    search = client.get("/api/v1/reference/search", params={"query": "образец"})
    item = client.get(
        "/api/v1/reference/item", params={"item_id": "bsl/Example"}
    )

    assert search.status_code == 401
    assert item.status_code == 401


def test_reference_api_готовая_база_ищет_и_выбирает_карточку(tmp_path):
    reference = _reference(tmp_path)
    client = _client(tmp_path, reference, mode=DASHBOARD_ON)

    found = client.get(
        "/api/v1/reference/search",
        params={"query": "показать образец", "platform": "8.3.20", "limit": 1},
    )
    card = client.get(
        "/api/v1/reference/item",
        params={"item_id": "bsl/Example", "platform": "8.3.20", "max_chars": 256},
    )

    assert found.status_code == 200
    assert found.json()["results"][0]["id"] == "bsl/Example"
    assert card.status_code == 200
    assert card.json()["card"]["id"] == "bsl/Example"
    assert "Синтетическое описание" in card.json()["content"]
    assert "<p>Синтетическое описание" in card.json()["html"]


def test_reference_api_экранирует_html_в_карточке(tmp_path):
    reference = _reference(tmp_path, body="<script>alert('x')</script>")
    client = _client(tmp_path, reference, mode=DASHBOARD_ON)

    card = client.get(
        "/api/v1/reference/item", params={"item_id": "bsl/Example"}
    )

    assert card.status_code == 200
    assert "<script>" not in card.json()["html"]
    assert "&lt;script&gt;" in card.json()["html"]


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/v1/reference/search", {"query": ""}),
        ("/api/v1/reference/search", {"query": "x" * 4097}),
        ("/api/v1/reference/search", {"query": "x", "limit": "51"}),
        ("/api/v1/reference/search", {"query": "x", "include_hidden": "yes"}),
        ("/api/v1/reference/item", {"item_id": ""}),
        ("/api/v1/reference/item", {"item_id": "x", "max_chars": "255"}),
        ("/api/v1/reference/item", {"item_id": "x" * 513}),
        ("/api/v1/reference/item", {"item_id": "x", "cursor": "x" * 2049}),
    ],
)
def test_reference_api_отклоняет_пустые_и_предельные_параметры(
    tmp_path, path, params
):
    client = _client(tmp_path, _reference(tmp_path), mode=DASHBOARD_ON)

    response = client.get(path, params=params)

    assert response.status_code == 400


def test_reference_api_неактивное_состояние_возвращает_причину(tmp_path):
    reference = ReferenceService.discover(tmp_path / "data")
    client = _client(tmp_path, reference, mode=DASHBOARD_ON)

    response = client.get(
        "/api/v1/reference/search", params={"query": "образец"}
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "Каноническая база не загружена.",
        "state": "missing",
    }
