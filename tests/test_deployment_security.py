"""Один Compose сохраняет fail-closed границы публичного Docker-запуска."""

from pathlib import Path


ROOT = Path(__file__).parents[1]
COMPOSE = ROOT / "compose.yaml"


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_compose_по_умолчанию_публикует_порт_только_на_loopback():
    compose = _compose()

    assert '"127.0.0.1:${MCP1C_PORT:-5001}:8000"' in compose
    assert '- "5001:8000"' not in compose
    assert '"0.0.0.0:5001:8000"' not in compose


def test_compose_получает_готовый_образ_и_ничего_не_собирает():
    compose = _compose()

    assert "image: ${MCP1C_IMAGE:-ghcr.io/azeevan/mcp-1c:2.0.1}" in compose
    assert "build:" not in compose
    assert "runtime-core" not in compose
    assert "runtime-dashboard" not in compose


def test_compose_не_создаёт_data_с_непредсказуемым_владельцем():
    compose = _compose()

    assert "source: ${MCP1C_DATA_DIR:-./data}" in compose
    assert "target: /data" in compose
    assert "create_host_path: false" in compose
    assert "selinux: Z" in compose
    assert "- ./data:/data" not in compose


def test_образ_фиксирует_uid_и_gid_и_fail_closed_запуск():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "groupadd --gid 10001 mcp1c" in dockerfile
    assert "useradd --uid 10001 --gid 10001 --no-create-home" in dockerfile
    assert "--shell /usr/sbin/nologin mcp1c" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert '"--require-writable-data"' in dockerfile
    assert '"--require-tokens"' in dockerfile


def test_образ_содержит_один_runtime_и_современный_ui():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM runtime-base AS runtime\n" in dockerfile
    assert "MCP1C_DASHBOARD=on" in dockerfile
    assert "MCP1C_ACCESS=local" in dockerfile
    assert "AS runtime-core" not in dockerfile
    assert "AS runtime-dashboard" not in dockerfile


def test_compose_задаёт_два_режима_без_override_файлов():
    compose = _compose()

    assert "MCP1C_DASHBOARD: ${MCP1C_DASHBOARD:-on}" in compose
    assert "MCP1C_ACCESS: ${MCP1C_ACCESS:-local}" in compose
    assert not (ROOT / "docker-compose.classic.yml").exists()
    assert not (ROOT / "docker-compose.dashboard.yml").exists()
    assert not (ROOT / "docker-compose.remote.yml").exists()


def test_compose_всегда_требует_оба_токена():
    compose = _compose()

    assert "API_TOKEN: ${API_TOKEN:?" in compose
    assert "ADMIN_TOKEN: ${ADMIN_TOKEN:?" in compose
    assert "API_TOKEN: ${API_TOKEN:-}" not in compose
    assert "ADMIN_TOKEN: ${ADMIN_TOKEN:-}" not in compose


def test_compose_сохраняет_защитные_ограничения_процесса():
    compose = _compose()

    assert "init: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "max-size: \"10m\"" in compose
    assert "max-file: \"3\"" in compose


def test_compose_передаёт_настройки_необязательной_общей_справки():
    compose = _compose()

    assert (
        "MCP1C_REFERENCE_ARTIFACT: ${MCP1C_REFERENCE_ARTIFACT:-}" in compose
    )
    assert "MCP1C_REFERENCE_TRUST_UNSIGNED" not in compose
