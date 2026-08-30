"""Сетевой профиль не открывает MCP наружу неявно."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_compose_по_умолчанию_публикует_порт_только_на_loopback():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert '"127.0.0.1:${MCP1C_PORT:-5001}:8000"' in compose
    assert '- "5001:8000"' not in compose
    assert '"0.0.0.0:5001:8000"' not in compose


def test_compose_не_создаёт_data_с_непредсказуемым_владельцем():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "source: ${MCP1C_DATA_DIR:-./data}" in compose
    assert "target: /data" in compose
    assert "create_host_path: false" in compose
    assert "selinux: Z" in compose
    assert "- ./data:/data" not in compose


def test_образ_фиксирует_uid_и_gid_пользователя_данных():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "groupadd --gid 10001 mcp1c" in dockerfile
    assert "useradd --uid 10001 --gid 10001 --no-create-home" in dockerfile
    assert "--shell /usr/sbin/nologin mcp1c" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert '"--require-writable-data"' in dockerfile


def test_три_режима_дашборда_заданы_явными_compose_файлами():
    base = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    classic = (ROOT / "docker-compose.classic.yml").read_text(encoding="utf-8")
    spa = (ROOT / "docker-compose.dashboard.yml").read_text(encoding="utf-8")

    assert "MCP1C_DASHBOARD: off" in base
    assert "MCP1C_DASHBOARD: classic" in classic
    assert "target: runtime-core" in classic
    assert "MCP1C_DASHBOARD: spa" in spa
    assert "target: runtime-dashboard" in spa


def test_compose_сохраняет_защитные_ограничения_процесса():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "init: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "max-size: \"10m\"" in compose
    assert "max-file: \"3\"" in compose


def test_compose_передаёт_настройки_необязательной_общей_справки():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "MCP1C_REFERENCE_DB: ${MCP1C_REFERENCE_DB:-}" in compose
    assert (
        "MCP1C_REFERENCE_TRUST_UNSIGNED: "
        "${MCP1C_REFERENCE_TRUST_UNSIGNED:-}"
    ) in compose


def test_remote_override_требует_оба_токена_и_явно_доверяет_proxy():
    remote = (ROOT / "docker-compose.remote.yml").read_text(encoding="utf-8")

    assert "${API_TOKEN:?" in remote
    assert "${ADMIN_TOKEN:?" in remote
    assert "--trust-proxy-headers" in remote
    assert "5001:8000" not in remote
