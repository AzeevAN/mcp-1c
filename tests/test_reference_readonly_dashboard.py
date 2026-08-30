"""Read-only страница общей справки повторяет публичный контракт провайдера."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_CLASSIC, DASHBOARD_SPA, routes
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


def _client(tmp_path, reference, *, mode=DASHBOARD_CLASSIC):
    registry = Registry(tmp_path / "data")
    return живой_клиент(
        Starlette(routes=routes(registry, mode=mode, reference=reference))
    )


def test_classic_страница_доступна_из_навигации_без_query(tmp_path):
    reference = ReferenceService.discover(tmp_path / "data")
    client = _client(tmp_path, reference)

    overview = client.get("/")
    page = client.get("/reference")

    assert 'href=/reference>Общая справка</a>' in overview.text
    assert page.status_code == 200
    assert "Общая справка не подключена" in page.text
    assert 'href="/sources"' in page.text
    assert "<form" not in page.text


def test_classic_готовая_страница_даёт_все_параметры_поиска(tmp_path):
    client = _client(tmp_path, _reference(tmp_path))
    page = client.get("/reference")

    assert page.status_code == 200
    for name in (
        "query", "domain", "kind", "platform", "limit",
        "include_explicit", "include_hidden",
    ):
        assert f"name={name}" in page.text

    invalid = client.get(
        "/reference", params={"query": "образец", "limit": "51"}
    )
    assert invalid.status_code == 200
    assert "превышает допустимый размер" in invalid.text


@pytest.mark.parametrize(
    ("state", "artifact"),
    [
        ("missing", None),
        ("untrusted", b"SQLite format 3\x00unsigned"),
        ("corrupt", b"not a bundle"),
    ],
)
def test_classic_неактивные_состояния_объясняются_без_форм(
    tmp_path, state, artifact
):
    path = tmp_path / "data" / "reference" / "reference.mcp1cref"
    if artifact is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(artifact)
    reference = ReferenceService.discover(tmp_path / "data")
    assert reference.status.state == state

    page = _client(tmp_path, reference).get("/reference")

    assert page.status_code == 200
    assert reference.status.message in page.text
    assert "<form" not in page.text


def test_classic_объясняет_disabled_и_incompatible_без_форм(tmp_path):
    disabled = ReferenceService.discover(tmp_path / "disabled", database_path="off")
    disabled_page = _client(tmp_path, disabled).get("/reference")

    signer = SyntheticReferenceSigner.generate()
    database = build_reference_database(tmp_path / "incompatible.sqlite3")
    artifact = signer.build(
        tmp_path / "incompatible" / "reference.mcp1cref",
        database,
        manifest_override={"format_version": "2"},
    )
    incompatible = ReferenceService.discover(
        tmp_path / "incompatible-data",
        database_path=artifact,
        verifier=signer.verifier(),
    )
    incompatible_page = _client(tmp_path, incompatible).get("/reference")

    assert disabled.status.state == "disabled"
    assert disabled.status.message in disabled_page.text
    assert "<form" not in disabled_page.text
    assert incompatible.status.state == "incompatible"
    assert incompatible.status.message in incompatible_page.text
    assert "<form" not in incompatible_page.text


def test_reference_api_требует_токен_чтения(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    reference = ReferenceService.discover(tmp_path / "data")
    client = _client(tmp_path, reference, mode=DASHBOARD_SPA)

    search = client.get("/api/v1/reference/search", params={"query": "образец"})
    item = client.get(
        "/api/v1/reference/item", params={"item_id": "bsl/Example"}
    )

    assert search.status_code == 401
    assert item.status_code == 401


def test_reference_api_готовая_база_ищет_и_выбирает_карточку(tmp_path):
    reference = _reference(tmp_path)
    client = _client(tmp_path, reference, mode=DASHBOARD_SPA)

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
    client = _client(tmp_path, _reference(tmp_path), mode=DASHBOARD_SPA)

    response = client.get(path, params=params)

    assert response.status_code == 400


def test_reference_api_неактивное_состояние_возвращает_причину(tmp_path):
    reference = ReferenceService.discover(tmp_path / "data")
    client = _client(tmp_path, reference, mode=DASHBOARD_SPA)

    response = client.get(
        "/api/v1/reference/search", params={"query": "образец"}
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "Каноническая база не загружена.",
        "state": "missing",
    }


def test_classic_экранирует_карточку_и_показывает_пустой_результат(tmp_path):
    reference = _reference(tmp_path, body="<script>alert('x')</script>")
    client = _client(tmp_path, reference)

    selected = client.get(
        "/reference",
        params={"query": "образец", "item_id": "bsl/Example"},
    )
    empty = client.get("/reference", params={"query": "небывалыйтокен"})

    assert selected.status_code == 200
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in selected.text
    assert "<script>alert('x')</script>" not in selected.text
    assert "Ничего не найдено" in empty.text
