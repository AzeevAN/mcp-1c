"""Изолированная Docker-приёмка закрепляет все режимы и границы."""

from __future__ import annotations

from pathlib import Path

from tools.lab.accept_universal_image import BAD_ENVIRONMENTS, MODES


ROOT = Path(__file__).resolve().parents[1]


def test_acceptance_matrix_содержит_ровно_четыре_runtime_режима() -> None:
    assert set(MODES) == {
        ("on", "local"),
        ("off", "local"),
        ("on", "https-proxy"),
        ("off", "https-proxy"),
    }
    assert {item.get("MCP1C_DASHBOARD") for item in BAD_ENVIRONMENTS} >= {
        "",
        "classic",
        "spa",
        "unknown",
    }
    assert {item.get("MCP1C_ACCESS") for item in BAD_ENVIRONMENTS} >= {
        "",
        "on",
        "remote",
        "unknown",
    }


def test_acceptance_проверяет_mcp_proxy_tokens_и_один_image_id() -> None:
    source = (ROOT / "tools" / "lab" / "accept_universal_image.py").read_text(
        encoding="utf-8"
    )

    for contract in (
        "streamable_http_client",
        "session.initialize()",
        "session.list_tools()",
        '"; Secure"',
        '"X-Forwarded-Proto": "https"',
        '"10001:10001"',
        'row["image_id"]',
        'row["version"]',
        'row["tools"]',
        '"negative_cases"',
        '["git", "ls-files", "src/mcp1c"]',
        '"/app/requirements-lock.txt"',
        'actual != expected',
    ):
        assert contract in source
