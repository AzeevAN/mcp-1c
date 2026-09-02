"""ASGI-граница тела запроса до form/json parsing."""

from __future__ import annotations

import asyncio

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp1c.server import mcp_guard


LOGIN_BODY_LIMIT = 16 * 1024
QUERY_BODY_LIMIT = 1024 * 1024
UPLOAD_FILE_LIMIT = 500 * 1024 * 1024
UPLOAD_OVERHEAD = 1024 * 1024
REFERENCE_FILE_LIMIT = 33 * 1024 * 1024
DEFAULT_BODY_LIMIT = 2 * 1024 * 1024


def _body_client() -> TestClient:
    async def consume(request: Request) -> PlainTextResponse:
        await request.body()
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/login", consume, methods=["POST"]),
            Route("/queries", consume, methods=["POST"]),
            Route("/sources", consume, methods=["POST"]),
            Route("/api/v1/sources/upload", consume, methods=["POST"]),
            Route("/api/v1/sources/intake/upload", consume, methods=["POST"]),
            Route("/api/v1/reference/upload", consume, methods=["POST"]),
            Route("/mcp", consume, methods=["POST"]),
        ]
    )
    return TestClient(mcp_guard(app))


def test_login_query_and_upload_have_different_declared_limits() -> None:
    client = _body_client()
    medium = b"x" * (LOGIN_BODY_LIMIT + 1)

    assert client.post("/login", content=b"x" * LOGIN_BODY_LIMIT).status_code == 200
    assert client.post("/login", content=medium).status_code == 413
    assert client.post("/queries", content=medium).status_code == 200
    assert client.post("/sources", content=medium).status_code == 200
    assert (
        client.post(
            "/api/v1/reference/upload",
            content=b"x",
            headers={
                "content-length": str(REFERENCE_FILE_LIMIT + UPLOAD_OVERHEAD)
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/reference/upload",
            content=b"x",
            headers={
                "content-length": str(REFERENCE_FILE_LIMIT + UPLOAD_OVERHEAD + 1)
            },
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/api/v1/sources/upload",
            content=b"x",
            headers={"content-length": str(DEFAULT_BODY_LIMIT + 1)},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/sources/intake/upload",
            content=b"x",
            headers={"content-length": str(DEFAULT_BODY_LIMIT + 1)},
        ).status_code
        == 200
    )

    assert (
        client.post(
            "/queries",
            content=b"x",
            headers={"content-length": str(QUERY_BODY_LIMIT + 1)},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/mcp",
            content=b"x",
            headers={"content-length": str(DEFAULT_BODY_LIMIT + 1)},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/sources",
            content=b"x",
            headers={
                "content-length": str(UPLOAD_FILE_LIMIT + UPLOAD_OVERHEAD + 1)
            },
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/api/v1/sources/upload",
            content=b"x",
            headers={
                "content-length": str(UPLOAD_FILE_LIMIT + UPLOAD_OVERHEAD + 1)
            },
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/api/v1/sources/intake/upload",
            content=b"x",
            headers={
                "content-length": str(UPLOAD_FILE_LIMIT + UPLOAD_OVERHEAD + 1)
            },
        ).status_code
        == 413
    )


def test_streamed_body_without_content_length_is_stopped_at_boundary() -> None:
    completed = False
    sent: list[dict] = []
    chunks = iter(
        [
            {"type": "http.request", "body": b"x" * 10_000, "more_body": True},
            {"type": "http.request", "body": b"x" * 10_000, "more_body": False},
        ]
    )

    async def receive() -> dict:
        return next(chunks)

    async def send(message: dict) -> None:
        sent.append(message)

    async def consume(request: Request) -> PlainTextResponse:
        nonlocal completed
        await request.body()
        completed = True
        return PlainTextResponse("ok")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
    }

    app = Starlette(routes=[Route("/login", consume, methods=["POST"])])
    asyncio.run(mcp_guard(app)(scope, receive, send))

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert starts[0]["status"] == 413
    assert not completed
