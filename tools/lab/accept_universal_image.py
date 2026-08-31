#!/usr/bin/env python3
"""Изолированно принять один image ID во всех runtime-режимах.

Контейнеры получают только временный ``tmpfs /data`` и удаляются после
проверки. Рабочий Compose, контейнер и каталог ``data/`` не затрагиваются.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import http.client
import http.server
import ipaddress
import json
import os
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MODES = (
    ("on", "local"),
    ("off", "local"),
    ("on", "https-proxy"),
    ("off", "https-proxy"),
)
BAD_ENVIRONMENTS = (
    {"MCP1C_DASHBOARD": "classic"},
    {"MCP1C_DASHBOARD": "spa"},
    {"MCP1C_DASHBOARD": ""},
    {"MCP1C_DASHBOARD": "unknown"},
    {"MCP1C_ACCESS": "remote"},
    {"MCP1C_ACCESS": "on"},
    {"MCP1C_ACCESS": ""},
    {"MCP1C_ACCESS": "unknown"},
)


def _run(
    command: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _inspect(name: str) -> dict:
    return json.loads(_run(["docker", "inspect", name]).stdout)[0]


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        return response.status, payload, response_headers
    finally:
        connection.close()


async def _mcp_snapshot(port: int, token: str) -> tuple[str, tuple[str, ...]]:
    client = httpx2.AsyncClient(
        headers={"X-Api-Token": token},
        timeout=20,
        trust_env=False,
    )
    async with client:
        async with streamable_http_client(
            f"http://127.0.0.1:{port}/mcp",
            http_client=client,
        ) as streams:
            read, write, *_ = streams
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
    return initialized.server_info.version, tuple(tool.name for tool in listed.tools)


def _certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "proxy-cert.pem"
    key_path = directory / "proxy-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _proxy_handler(backend_port: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - имя задаёт stdlib
            self._forward()

        def do_POST(self) -> None:  # noqa: N802 - имя задаёт stdlib
            self._forward()

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
            hop_headers = {
                "connection",
                "content-length",
                "host",
                "x-forwarded-for",
                "x-forwarded-host",
                "x-forwarded-proto",
            }
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in hop_headers
            }
            headers.update(
                {
                    "Host": "mcp.example.test",
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Forwarded-Host": "mcp.example.test",
                    "X-Forwarded-Proto": "https",
                }
            )
            backend = http.client.HTTPConnection(
                "127.0.0.1", backend_port, timeout=10
            )
            try:
                backend.request(self.command, self.path, body=body, headers=headers)
                response = backend.getresponse()
                payload = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {
                        "connection",
                        "content-length",
                        "transfer-encoding",
                    }:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                backend.close()

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


@contextmanager
def _https_proxy(backend_port: int) -> Iterator[int]:
    with tempfile.TemporaryDirectory(prefix="mcp1c-proxy-") as raw_directory:
        directory = Path(raw_directory)
        cert_path, key_path = _certificate(directory)
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _proxy_handler(backend_port)
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield int(server.server_address[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _proxy_request(
    proxy_port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(
        "127.0.0.1", proxy_port, timeout=10, context=context
    )
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        return response.status, payload, response_headers
    finally:
        connection.close()


def _wait_ready(names: list[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    pending = set(names)
    while pending and time.monotonic() < deadline:
        for name in tuple(pending):
            state = _inspect(name)["State"]
            if not state["Running"]:
                raise RuntimeError(f"{name} завершился до readiness.")
            if state.get("Health", {}).get("Status") == "healthy":
                pending.remove(name)
        if pending:
            time.sleep(1)
    if pending:
        raise TimeoutError(f"Не дождались healthy: {', '.join(sorted(pending))}")


def _start_mode(
    image: str,
    name: str,
    dashboard: str,
    access: str,
    api_token: str,
    admin_token: str,
) -> None:
    _run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--tmpfs",
            "/data:rw,uid=10001,gid=10001,mode=0750",
            "--publish",
            "127.0.0.1::8000",
            "--env",
            f"API_TOKEN={api_token}",
            "--env",
            f"ADMIN_TOKEN={admin_token}",
            "--env",
            f"MCP1C_DASHBOARD={dashboard}",
            "--env",
            f"MCP1C_ACCESS={access}",
            image,
        ]
    )


def _negative_cases(image: str, api_token: str, admin_token: str) -> int:
    base = {"API_TOKEN": api_token, "ADMIN_TOKEN": admin_token}
    cases = [{**base, **overrides} for overrides in BAD_ENVIRONMENTS]
    cases.extend(
        [
            {"ADMIN_TOKEN": admin_token},
            {"API_TOKEN": api_token},
            {**base, "API_TOKEN": ""},
            {**base, "API_TOKEN": "short"},
            {**base, "ADMIN_TOKEN": "short"},
            {**base, "API_TOKEN": "A" * 31},
            {**base, "API_TOKEN": "A" * 32 + "\t"},
            {**base, "API_TOKEN": "change-me"},
            {**base, "ADMIN_TOKEN": api_token},
        ]
    )
    for environment in cases:
        command = ["docker", "run", "--rm"]
        for key, value in environment.items():
            command.extend(("--env", f"{key}={value}"))
        command.append(image)
        result = _run(command, check=False, timeout=30)
        if result.returncode == 0:
            raise AssertionError(
                f"Невалидное окружение принято: {sorted(environment)}"
            )
        combined = result.stdout + result.stderr
        token_values = (
            value
            for key, value in environment.items()
            if key in {"API_TOKEN", "ADMIN_TOKEN"} and value
        )
        if any(value in combined for value in token_values):
            raise AssertionError("Токен попал в сообщение об ошибке.")
    return len(cases)


def _compose_contract(image: str, api_token: str, admin_token: str) -> int:
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="mcp1c-compose-") as raw_directory:
        directory = Path(raw_directory)
        empty_env = directory / "empty.env"
        empty_env.write_text("", encoding="utf-8")
        base = dict(os.environ)
        base.pop("API_TOKEN", None)
        base.pop("ADMIN_TOKEN", None)
        base.update(
            {
                "MCP1C_IMAGE": image,
                "MCP1C_DATA_DIR": str(directory),
            }
        )
        command = [
            "docker",
            "compose",
            "--env-file",
            str(empty_env),
            "--file",
            str(root / "compose.yaml"),
            "config",
            "--quiet",
        ]
        for present in ("api", "admin"):
            environment = dict(base)
            if present == "api":
                environment["API_TOKEN"] = api_token
            else:
                environment["ADMIN_TOKEN"] = admin_token
            result = _run(command, check=False, env=environment)
            if result.returncode == 0:
                raise AssertionError("Compose принял отсутствующий обязательный токен.")
        valid = {**base, "API_TOKEN": api_token, "ADMIN_TOKEN": admin_token}
        _run(command, env=valid)
    return 2


def _image_filesystem_is_public(image: str) -> None:
    command = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        image,
        "-c",
        "test ! -e /usr/local/bin/node && "
        "test ! -e /usr/local/bin/npm && "
        "test ! -d /app/node_modules && "
        "! find /app -type f \\( -name '*.mcp1cref' -o -name '*.sqlite*' "
        "-o -name '*.pem' -o -name '*.key' -o -name '?GENTS.md' \\) | grep -q .",
    ]
    _run(command)


def _accept_mode(
    name: str,
    dashboard: str,
    access: str,
    api_token: str,
) -> dict:
    inspected = _inspect(name)
    state = inspected["State"]
    port = int(inspected["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"])
    if inspected["Config"]["User"] != "10001:10001":
        raise AssertionError("Неверный image user.")
    if state["Restarting"] or state["OOMKilled"] or inspected["RestartCount"] != 0:
        raise AssertionError("Контейнер перезапускался или был убит OOM.")
    uid = _run(["docker", "exec", name, "id", "-u"]).stdout.strip()
    gid = _run(["docker", "exec", name, "id", "-g"]).stdout.strip()
    _run(["docker", "exec", name, "sh", "-c", "touch /data/acceptance-write"])
    if (uid, gid) != ("10001", "10001"):
        raise AssertionError(f"Неверный uid/gid: {uid}:{gid}")

    health_status, health_body, _ = _request(port, "GET", "/health")
    health = json.loads(health_body)
    if health_status != 200 or health.get("status") != "ok":
        raise AssertionError("Health не готов.")
    version, tools = asyncio.run(_mcp_snapshot(port, api_token))

    headers = {"X-Api-Token": api_token}
    page_status, page, _ = _request(port, "GET", "/", headers=headers)
    api_status, bootstrap, _ = _request(
        port, "GET", "/api/v1/dashboard/bootstrap", headers=headers
    )
    if dashboard == "on":
        if page_status != 200 or b'<div id="root"></div>' not in page:
            raise AssertionError("SPA не отдана в on.")
        dashboard_version = json.loads(bootstrap).get("server", {}).get("version")
        if api_status != 200 or dashboard_version != version:
            raise AssertionError("Dashboard bootstrap не совпал с MCP version.")
    elif (page_status, api_status) != (404, 404):
        raise AssertionError("UI/API зарегистрированы в off.")

    login_body = urllib.parse.urlencode({"token": api_token}).encode("ascii")
    login_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Forwarded-Host": "mcp.example.test",
        "X-Forwarded-Proto": "https",
    }
    login_status, _, response_headers = _request(
        port, "POST", "/login", headers=login_headers, body=login_body
    )
    if dashboard == "on":
        if login_status != 303:
            raise AssertionError("Login не вернул redirect.")
        secure = "; Secure" in response_headers.get("set-cookie", "")
        if secure != (access == "https-proxy"):
            raise AssertionError("Forwarded headers обработаны не по access mode.")
    elif login_status != 404:
        raise AssertionError("Login зарегистрирован в off.")

    proxy_status: int | None = None
    if dashboard == "on" and access == "https-proxy":
        with _https_proxy(port) as proxy_port:
            proxy_status, _, proxy_headers = _proxy_request(
                proxy_port,
                "POST",
                "/login",
                body=login_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            secure_cookie = "; Secure" in proxy_headers.get("set-cookie", "")
            if proxy_status != 303 or not secure_cookie:
                raise AssertionError("HTTPS proxy не дал Secure cookie.")

    return {
        "dashboard": dashboard,
        "access": access,
        "image_id": inspected["Image"],
        "version": version,
        "tools": len(tools),
        "uid_gid": f"{uid}:{gid}",
        "health": state["Health"]["Status"],
        "ui": page_status,
        "api": api_status,
        "https_proxy": proxy_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()

    api_token = "accept-api-" + secrets.token_urlsafe(32)
    admin_token = "accept-admin-" + secrets.token_urlsafe(32)
    prefix = f"mcp1c-accept-{secrets.token_hex(4)}"
    names = [
        f"{prefix}-{dashboard}-{access.replace('-', '')}"
        for dashboard, access in MODES
    ]
    results: list[dict] = []
    try:
        _image_filesystem_is_public(args.image)
        compose_negative = _compose_contract(args.image, api_token, admin_token)
        for name, (dashboard, access) in zip(names, MODES, strict=True):
            _start_mode(
                args.image, name, dashboard, access, api_token, admin_token
            )
        _wait_ready(names, args.timeout)
        for name, (dashboard, access) in zip(names, MODES, strict=True):
            results.append(_accept_mode(name, dashboard, access, api_token))
        if len({row["image_id"] for row in results}) != 1:
            raise AssertionError("Режимы запущены не из одного image ID.")
        if len({(row["version"], row["tools"]) for row in results}) != 1:
            raise AssertionError("MCP различается между runtime-режимами.")
        negative = _negative_cases(args.image, api_token, admin_token)
    finally:
        for name in names:
            _run(["docker", "rm", "--force", name], check=False, timeout=30)

    print(
        json.dumps(
            {
                "image": args.image,
                "modes": results,
                "negative_cases": negative,
                "compose_missing_tokens": compose_negative,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
