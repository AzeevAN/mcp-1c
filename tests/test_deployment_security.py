"""Сетевой профиль не открывает MCP наружу неявно."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_compose_по_умолчанию_публикует_порт_только_на_loopback():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert '"127.0.0.1:5001:8000"' in compose
    assert '- "5001:8000"' not in compose
    assert '"0.0.0.0:5001:8000"' not in compose


def test_remote_override_требует_оба_токена_и_явно_доверяет_proxy():
    remote = (ROOT / "docker-compose.remote.yml").read_text(encoding="utf-8")

    assert "${API_TOKEN:?" in remote
    assert "${ADMIN_TOKEN:?" in remote
    assert "--trust-proxy-headers" in remote
    assert "5001:8000" not in remote
