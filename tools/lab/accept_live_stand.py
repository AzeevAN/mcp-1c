#!/usr/bin/env python3
"""Живая приёмка отдельного контейнера на временной копии данных."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

TASK = "audit-2026-10-23"
MARKER = ".mcp1c-audit-copy"
CONTAINER = "mcp1c-audit-stand"
ROOT = Path(__file__).resolve().parents[2]
VOLATILE = frozenset({
    "runtime_id", "request_id", "ticket", "download_url", "created_at",
    "updated_at", "loaded_at", "duration_seconds", "expires_at",
})
PUBLIC_ENV = frozenset({
    "MCP1C_ACCESS", "MCP1C_BIND_ADDRESS", "MCP1C_PUBLIC_BASE_URL", "MCP1C_ALLOWED_HOSTS",
})


class RunDeadlineExceeded(BaseException):
    """Общий дедлайн проходит сквозь локальные перехваты Exception/OSError SDK."""


def validate_data_copy(path: Path, source: Path) -> Path:
    path, source = Path(path).absolute(), Path(source).resolve()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Путь копии не должен проходить через символьную ссылку.")
    target = path.resolve()
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("Копия должна находиться отдельно от рабочего data/.")
    if target.exists():
        if not target.is_dir():
            raise ValueError("Путь копии занят файлом.")
        marker = target / MARKER
        try:
            if marker.is_symlink() or not marker.is_file():
                raise ValueError("У существующего каталога нет маркера стенда.")
            identity = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("Нельзя подтвердить принадлежность каталога стенду.") from error
        if identity != {"task": TASK, "source": str(source)}:
            raise ValueError("Маркер каталога относится к другому источнику или задаче.")
    return target


def normalize_response(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: ("<временное>" if key in VOLATILE else
                  "<секрет>" if str(key).lower().endswith(("token", "ticket")) else
                  normalize_response(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [normalize_response(item) for item in value]
    return value


def _comparison_view(value: object, scenario: str) -> object:
    """Транспортные ссылки сохраняют связь; предметные id не меняются."""
    ids: dict[tuple[str, str], str] = {}

    def transport_id(kind, item):
        if item is None:
            return None
        key = (kind, str(item))
        if key not in ids:
            number = 1 + sum(group == kind for group, _ in ids)
            ids[key] = f"<{kind}:{number}>"
        return ids[key]

    def visit(item):
        if isinstance(item, list):
            return [visit(child) for child in item]
        if not isinstance(item, dict):
            return item
        candidate = "transport" in item and "origin_name" in item
        if candidate and "id" in item:
            transport_id("candidate", item["id"])
        result = {}
        for key in sorted(item):
            child = item[key]
            if key in {"started_at", "finished_at", "prepared_at", "saved_at", "old_runtime_id"}:
                result[key] = "<временное>"
            elif key == "job_id":
                result[key] = transport_id("job", child)
            elif key == "candidate_id" or (candidate and key == "id"):
                result[key] = transport_id("candidate", child)
            elif key in {"generation_id", "base_generation_id", "candidate_generation_id"}:
                result[key] = transport_id("generation", child)
            elif key == "manifest_sha256" and item.get("generation_id") is not None:
                # Этот digest включает случайный ID поколения. Хеши содержимого
                # слоёв и модулей остаются предметными данными и сравниваются.
                result[key] = transport_id("manifest", child)
            elif scenario == "S8" and key in {"first_seconds", "second_seconds"}:
                continue
            elif scenario == "S15" and key == "seconds":
                continue
            elif scenario == "S17" and key == "log_sha256":
                continue
            elif scenario == "S18" and key in {"peak_rss_bytes", "baseline_peak_rss_bytes", "rss_ratio", "comparison"}:
                continue
            else:
                result[key] = visit(child)
        return result

    return visit(normalize_response(value))


def compare_reports(baseline: dict, candidate: dict) -> list[dict]:
    differences = []
    before, after = baseline.get("scenarios", {}), candidate.get("scenarios", {})
    for name in sorted(before.keys() | after.keys()):
        if name not in before or name not in after:
            differences.append({"scenario": name, "kind": "набор сценариев изменился"})
            continue
        for key in ("status", "observations", "reason"):
            if _comparison_view(before[name].get(key), name) != _comparison_view(after[name].get(key), name):
                differences.append({"scenario": name, "kind": key,
                                    "baseline": normalize_response(before[name].get(key)),
                                    "candidate": normalize_response(after[name].get(key))})
    old = baseline.get("resources", {}).get("peak_rss_bytes")
    new = candidate.get("resources", {}).get("peak_rss_bytes")
    if old and new and new * 10 > old * 11:
        differences.append({"scenario": "S18", "kind": "рост RSS более 10%",
                            "baseline_bytes": old, "candidate_bytes": new})
    return differences


def _run(command: list[str], timeout: float = 120) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Аргументы могут содержать билет или токен: команду целиком не печатаем.
        raise RuntimeError(f"{command[0]}: exit {result.returncode}: {result.stderr[-2000:]}")
    return result.stdout


def _safe_inspect(container: str) -> dict:
    template = ('{"Id":{{json .Id}},"State":{{json .State}},'
                '"RestartCount":{{json .RestartCount}},"Image":{{json .Image}},'
                '"Labels":{{json .Config.Labels}}}')
    return json.loads(_run(["docker", "inspect", "--format", template, container]))


def _working_settings() -> dict[str, str]:
    # Docker отбирает только четыре разрешённые переменные до вывода в Python.
    template = '{{range .Config.Env}}{{ $p := split . "=" }}{{if or (eq (index $p 0) "MCP1C_ACCESS") (eq (index $p 0) "MCP1C_BIND_ADDRESS") (eq (index $p 0) "MCP1C_PUBLIC_BASE_URL") (eq (index $p 0) "MCP1C_ALLOWED_HOSTS")}}{{println .}}{{end}}{{end}}'
    output = _run(["docker", "inspect", "--format", template, "mcp1c"])
    return dict(line.split("=", 1) for line in output.splitlines()
                if "=" in line and line.split("=", 1)[0] in PUBLIC_ENV)


def _request(port: int, method: str, path: str, body=None, headers=None, *, timeout=120) -> tuple:
    headers = dict(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode()
        headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def _decoded_tool(result) -> dict:
    blocks = []
    for block in result.content:
        value = block.model_dump(mode="json", by_alias=True, exclude_none=True)
        if value.get("type") == "text":
            try:
                value["text"] = json.loads(value["text"])
            except (ValueError, TypeError):
                pass
        blocks.append(value)
    return {"is_error": bool(result.is_error), "content": blocks}


async def _session_call(port: int, token: str, name=None, arguments=None) -> dict:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import MCPError

    async with asyncio.timeout(120):
        async with httpx2.AsyncClient(headers={"X-Api-Token": token},
                                     timeout=120, trust_env=False) as client:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as streams:
                async with ClientSession(*streams[:2]) as session:
                    initialized = await session.initialize()
                    if name is not None:
                        try:
                            return _decoded_tool(await session.call_tool(name, arguments or {}))
                        except MCPError as error:
                            if error.code != -32602:
                                raise
                            return {"is_error": True,
                                    "protocol_error": error.error.model_dump(mode="json", exclude_none=True),
                                    "content": [{"type": "text", "text": error.message}]}
                    listed = await session.list_tools()
                    return {"initialized": initialized.model_dump(mode="json", by_alias=True, exclude_none=True),
                            "tools": [tool.name for tool in listed.tools],
                            "schemas": {tool.name: tool.input_schema for tool in listed.tools}}


def _tool_text(result: dict) -> str:
    for block in result["content"]:
        if block.get("type") == "text":
            value = block["text"]
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ""


def _first_address(text: str, *, procedure: bool = False) -> str | None:
    matches = re.findall(r"^- `([^`]+)`", text, flags=re.MULTILINE)
    return next((value for value in matches if not procedure or "::" in value), None)


class Stand:
    def __init__(self, image: str, data_copy: Path, port: int, report: dict, *, name=CONTAINER):
        self.root, self.image, self.data_copy, self.port = ROOT, image, data_copy, port
        self.report = report
        self.api_token, self.admin_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.container_id = None
        self.container_name = name
        self.requested_restarts = 0
        self._stop_sampling = threading.Event()
        self._sampler = None
        self._sample_errors = []
        self._secret_values = {self.api_token, self.admin_token}
        self._children = []

    def request(self, method, path, body=None, *, admin=False, authenticated=True, headers=None):
        selected = dict(headers or {})
        if authenticated:
            selected.setdefault("X-Admin-Token" if admin else "X-Api-Token",
                                self.admin_token if admin else self.api_token)
        # Confirm синхронно строит индексы крупной конфигурации. Клиент должен
        # дождаться её публикации, иначе следующие сценарии упрутся в busy.
        timeout = 1200 if method == "POST" and path == "/api/v1/sources/intake/confirm" else 120
        result = _request(self.port, method, path, body, selected, timeout=timeout)
        status, payload, _ = result
        try:
            response = json.loads(payload)
        except ValueError:
            response = payload.decode("utf-8", errors="replace")[:10000]
        self.report.setdefault("events", []).append({"method": method, "path": path,
            "body": normalize_response(body) if isinstance(body, (dict, list)) else
            {"bytes": len(body)} if body is not None else None,
            "status": status, "response": normalize_response(response)})
        return result

    def json(self, method, path, body=None, *, admin=False):
        status, payload, _ = self.request(method, path, body, admin=admin)
        assert 200 <= status < 300, f"{method} {path}: HTTP {status}: {payload[:1000]!r}"
        return json.loads(payload)

    def inspect(self):
        if not self.container_id:
            raise RuntimeError("Стенд ещё не создан.")
        return _safe_inspect(self.container_id)

    def exec(self, *args):
        if not self.container_id:
            raise RuntimeError("Стенд ещё не создан.")
        return _run(["docker", "exec", self.container_id, *args], timeout=900)

    def cli(self, *args):
        return self.exec("python", "-m", "mcp1c.cli", *args)

    def logs(self):
        result = subprocess.run(["docker", "logs", self.container_id],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Не удалось прочитать журнал стенда.")
        return result.stdout + result.stderr

    def mcp(self, name, args):
        result = asyncio.run(_session_call(self.port, self.api_token, name, args))
        self.report.setdefault("events", []).append({"tool": name, "arguments": args,
                                                     "response": normalize_response(result)})
        return result

    def mcp_text(self, name, args):
        return _tool_text(self.mcp(name, args))

    def snapshot(self):
        snapshot = asyncio.run(_session_call(self.port, self.api_token))
        self.report.setdefault("events", []).append({"mcp_catalog": normalize_response(snapshot)})
        return snapshot

    def sample_resources(self):
        # RSS процесса из cgroup-контейнера, а не рабочий процесс на хосте.
        value = self.exec("python", "-c", "from pathlib import Path\ntotal=0\nfor p in Path('/proc').glob('[0-9]*/status'):\n try:\n  s=p.read_text()\n except (FileNotFoundError, PermissionError):\n  continue\n if 'VmRSS:' in s:\n  total+=int(s.split('VmRSS:')[1].split()[0])*1024\nprint(total)")
        rss = int(value.strip())
        resources = self.report.setdefault("resources", {})
        resources["peak_rss_bytes"] = max(resources.get("peak_rss_bytes", 0), rss)
        resources["samples"] = resources.get("samples", 0) + 1
        resources["method"] = "Сумма VmRSS процессов контейнера, выборка раз в 2 с; нижняя оценка пика."

    def _sample_loop(self):
        while not self._stop_sampling.wait(2):
            try:
                self.sample_resources()
            except Exception as error:
                self._sample_errors.append(type(error).__name__)

    def wait_ready(self):
        deadline = time.monotonic() + 600
        initial_restarts = self.inspect()["RestartCount"]
        while time.monotonic() < deadline:
            inspected = self.inspect()
            state = inspected["State"]
            if state.get("OOMKilled"):
                raise RuntimeError("Стенд завершён OOM killer.")
            if inspected["RestartCount"] > initial_restarts + 2 or state.get("Status") in ("dead", "exited"):
                raise RuntimeError("Стенд не поднялся: процесс завершился или повторно перезапускается.")
            try:
                status, body, _ = self.request("GET", "/health")
                if status == 200 and json.loads(body).get("status") == "ok":
                    return
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(1)
        raise TimeoutError("Стенд не готов за 600 секунд.")

    def restart(self):
        _run(["docker", "restart", self.container_id], timeout=120)
        self.requested_restarts += 1
        self.wait_ready()

    def start(self):
        present = _run(["docker", "ps", "-a", "--filter", f"name=^/{self.container_name}$", "--format", "{{.ID}}"])
        if present.strip():
            raise RuntimeError("Имя стенда занято: существующий контейнер не изменён.")
        settings = _working_settings()
        environment = {**settings, "API_TOKEN": self.api_token, "ADMIN_TOKEN": self.admin_token,
                       "MCP1C_DASHBOARD": "on", "MCP1C_ALLOW_SELF_RESTART": "1"}
        mounts = json.loads(_run(["docker", "inspect", "--format", "{{json .Mounts}}", "mcp1c"]))
        command = ["docker", "create", "--name", self.container_name,
                   "--label", f"mcp1c.task={TASK}", "--restart", "unless-stopped",
                   "--publish", f"127.0.0.1:{self.port}:8000",
                   "--mount", f"type=bind,src={self.data_copy},dst=/data"]
        for mount in mounts:
            if mount["Destination"] == "/config-sources/config-a":
                assert mount.get("RW") is False, "Каталог выгрузки должен быть read-only."
                command += ["--mount", f"type=bind,src={mount['Source']},dst=/config-sources/config-a,readonly"]
        with tempfile.TemporaryDirectory(prefix="mcp1c-audit-env-") as directory:
            env_file = Path(directory) / "stand-env"
            descriptor = os.open(env_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write("".join(f"{key}={value}\n" for key, value in environment.items()))
            self.container_id = _run([*command, "--env-file", str(env_file), self.image]).strip()
        assert re.fullmatch(r"[a-f0-9]{64}", self.container_id), "Docker не вернул полный ID."
        _run(["docker", "start", self.container_id])
        self.report["stand"] = {"container_id": self.container_id, "image": self.inspect()["Image"],
                                "settings": settings}
        self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
        self._sampler.start()
        self.wait_ready()

    @contextmanager
    def isolation_copy(self):
        """Порча поколения в S13 разрешена только во второй расходной копии."""
        directory = Path(tempfile.mkdtemp(prefix="mcp1c-audit-isolation-")).resolve()
        copied = directory / "data"
        child_report = {"resources": {"peak_rss_bytes": 0}}
        other = Stand(self.image, copied, self.port + 1, child_report,
                      name="mcp1c-audit-isolation")
        self._children.append((other, directory))
        self.report["isolation"] = child_report
        self._secret_values.update(other._secret_values)
        try:
            _copy_data(copied, self.data_copy)
            other.start()
            yield other
        finally:
            try:
                other.close()
            except Exception as error:
                child_report["cleanup_error"] = str(error)
                # Копия и ссылка на контейнер остаются у родителя для повтора.
                raise
            else:
                shutil.rmtree(directory)
                self._children.remove((other, directory))

    def close(self):
        errors = []
        for child, directory in list(self._children):
            try:
                child.close()
                shutil.rmtree(directory)
                self._children.remove((child, directory))
            except Exception as error:
                child.report["cleanup_error"] = str(error)
                errors.append(str(error))
        try:
            self._close_own()
        except Exception as error:
            errors.append(str(error))
        if errors:
            raise RuntimeError("Не завершена уборка стенда: " + "; ".join(errors))

    def _close_own(self):
        self._stop_sampling.set()
        if self._sampler:
            self._sampler.join(timeout=15)
        if self.container_id:
            state = self.inspect()
            if state.get("Labels", {}).get("mcp1c.task") != TASK or state["Id"] != self.container_id:
                raise RuntimeError("Принадлежность контейнера проверке не подтверждена; уборка отменена.")
            self.report["final_state"] = {"RestartCount": state["RestartCount"],
                                          "OOMKilled": state["State"].get("OOMKilled", False),
                                          "requested_restarts": self.requested_restarts}
            self.report.setdefault("resources", {})["sampling_errors"] = self._sample_errors
            try:
                self.report["container_log"] = self.logs()
            finally:
                _run(["docker", "stop", "--time", "10", self.container_id])
                _run(["docker", "rm", self.container_id])
                self.report.setdefault("cleanup", {})["removed_container"] = self.container_id
                self.container_id = None


def _scenario_one(stand: Stand) -> dict:
    health = stand.json("GET", "/health")
    assert health.get("status") == "ok"
    registry = json.loads((stand.root / "data/registry.json").read_text(encoding="utf-8"))
    names = sorted(row["identity"]["configuration_name"] for row in registry.get("generation_manifests", [])
                   if row["identity"].get("source_kind") == "configuration")
    # Для старых строк имя не дублируется в registry: не выдаём частичную сверку за полную.
    legacy = [row for row in registry.get("sources", []) if row.get("kind") == "configuration"]
    assert not legacy, "Полная сверка имён для legacy Source A пока не реализована."
    assert sorted(health["configurations"]) == names, "Список конфигураций отличается от рабочего registry.json."
    assert "Traceback" not in stand.logs(), "Traceback при старте."
    return {"health": health, "expected_configurations": names}


def _scenario_two(stand: Stand) -> dict:
    snapshot = stand.snapshot()
    cases = {}
    outcomes = {}

    def call(name, args):
        result = stand.mcp(name, args)
        outcomes[name] = {"arguments": args, "response": result}
        assert not result["is_error"], f"{name}: ошибка на корректном вызове."
        cases[name] = args
        return _tool_text(result)

    listing = call("list_configurations", {})
    available = stand.json("GET", "/health")["configurations"]
    configs = [name for name in re.findall(r"^## (.+)$", listing, re.MULTILINE)
               if name in available]
    assert configs, "Нет конфигураций для проверки инструментов."
    selected = configs[0]
    full_name = address = None
    for config in configs:
        objects = call("search_objects", {"query": "документ", "config": config, "limit": 3})
        procedures = call("search_procedures", {"query": "ПриЗаписи", "config": config, "limit": 3})
        full_name = _first_address(objects)
        address = _first_address(procedures, procedure=True)
        if full_name and address:
            selected = config
            break
    assert full_name and address, "Не найден объект и процедура для полного S2."
    config_args = {"config": selected}
    call("get_object", {**config_args, "full_name": full_name, "detail": "brief"})
    call("get_related", {**config_args, "full_name": full_name})
    call("get_procedure", {**config_args, "address": address, "lines": 20})
    call("get_callers", {**config_args, "address": address, "limit": 5})
    if "get_module_source_file" in snapshot["tools"]:
        call("get_module_source_file", {**config_args, "address": address.split("::")[0]})
    if "compare_configurations" in snapshot["tools"]:
        assert len(configs) >= 2, "Нужны две конфигурации для compare_configurations."
        call("compare_configurations", {"full_name": full_name, "configs": configs[:2], "limit": 5})
    syntax = call("search_syntax", {**config_args, "query": "СтрНайти", "kind": "method", "limit": 3})
    syntax_name = _first_address(syntax)
    assert syntax_name, "Поиск синтаксиса не дал адрес карточки."
    call("get_syntax", {**config_args, "name": syntax_name, "detail": "fields"})
    if "find_roles_for_access" in snapshot["tools"]:
        candidates = []
        for role_config in configs:
            objects = call("search_objects", {"query": "документ", "config": role_config, "limit": 3})
            role_object = _first_address(objects)
            if role_object is None:
                continue
            roles = json.loads(call("find_roles_for_access", {"config": role_config, "full_name": role_object,
                                    "operations": ["read"], "limit": 3}))
            candidates = roles.get("candidates", [])
            if candidates:
                break
        assert candidates, "Нет роли для проверки get_role_access."
        call("get_role_access", {"config": role_config, "role": candidates[0]["role"]["name"]})
    if "search_reference" in snapshot["tools"]:
        matches = []
        for query in ("массив", "Для Каждого", "Если"):
            reference = json.loads(call("search_reference", {"query": query, "domain": "bsl", "limit": 3}))
            matches = reference.get("results", [])
            if matches:
                break
        assert matches, "Нет карточки общей справки."
        call("get_reference", {"item_id": matches[0]["id"], "max_chars": 2000})
    assert set(cases) == set(snapshot["tools"]), f"Не покрыты инструменты: {set(snapshot['tools']) - cases.keys()}"
    for name in snapshot["tools"]:
        schema = snapshot["schemas"][name]
        required = schema.get("required", [])
        if required:
            wrong = {**cases[name], required[0]: {"неверный_тип": True}}
        elif name == "list_configurations":
            # У инструмента без аргументов лишние поля могут игнорироваться SDK.
            # Нарушаем протокольный тип arguments, а не выдумываем обязательное поле.
            status, payload = _invalid_no_argument_tool(stand, name)
            assert status >= 400 or b'"error"' in payload or b'"isError":true' in payload.replace(b" ", b""), "Неверный тип arguments принят."
            outcomes[name]["invalid"] = {"status": status, "body": payload.decode()}
            continue
        else:
            field = next(iter(schema.get("properties", {})), None)
            assert field, f"Нет способа проверить неверный аргумент {name}."
            wrong = {**cases[name], field: {"неверный_тип": True}}
        result = stand.mcp(name, wrong)
        assert result["is_error"], f"{name}: неверные аргументы приняты."
        outcomes[name]["invalid"] = {"arguments": wrong, "response": result}
    return {"initialized": snapshot["initialized"], "tools": snapshot["tools"], "calls": outcomes}


def _invalid_no_argument_tool(stand: Stand, name: str) -> tuple[int, bytes]:
    """Невалидный arguments отправляется после initialize, в той же сессии."""
    headers = {"Accept": "application/json, text/event-stream"}
    status, payload, response_headers = stand.request("POST", "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "mcp1c-audit", "version": "1"},
        },
    }, headers=headers)
    assert status == 200, f"initialize негативной сессии: HTTP {status}."
    session_id = next((value for key, value in response_headers.items()
                       if key.lower() == "mcp-session-id"), None)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    headers["MCP-Protocol-Version"] = "2025-03-26"
    try:
        status, _, _ = stand.request("POST", "/mcp", {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, headers=headers)
        assert status in (200, 202, 204), f"Инициализация негативной сессии: HTTP {status}."
        status, payload, _ = stand.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": []},
        }, headers=headers)
        return status, payload
    finally:
        if session_id:
            stand.request("DELETE", "/mcp", headers=headers)


def _copy_data(target: Path, source: Path):
    target = validate_data_copy(target, source)
    completion = target / (MARKER + "-complete")
    if target.exists():
        if completion.is_symlink() or not completion.is_file():
            raise ValueError("Копирование не завершено; неполную копию нельзя использовать.")
        return
    target.mkdir(parents=True)
    # Маркер появляется до первого копируемого файла: обрыв остаётся безопасно убираемым.
    (target / MARKER).write_text(json.dumps({"task": TASK, "source": str(source.resolve())}), encoding="utf-8")
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True,
                    ignore=lambda _directory, names: [name for name in names
                                                       if name in (MARKER, MARKER + "-complete")])
    completion.touch()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--data-copy", required=True, type=Path)
    parser.add_argument("--port", default=5091, type=int)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--scenarios", default=",".join(f"S{i}" for i in range(1, 19)))
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    options = parser.parse_args(argv)
    scenarios = options.scenarios.split(",")
    if not scenarios or len(set(scenarios)) != len(scenarios) or any(name not in {f"S{i}" for i in range(1, 19)} for name in scenarios):
        parser.error("Сценарии: уникальные S1–S18 через запятую.")
    if not 1024 <= options.port <= 65535 or options.port == 5001:
        parser.error("Нужен отдельный непривилегированный порт, отличный от рабочего 5001.")
    if options.timeout_seconds <= 0:
        parser.error("Таймаут должен быть положительным.")
    source = ROOT / "data"
    data_copy = validate_data_copy(options.data_copy, source)
    output = options.report.resolve()
    if output == source or source in output.parents or data_copy in output.parents:
        parser.error("Отчёт должен находиться отдельно от рабочих данных и их копии.")
    report = {"task": TASK, "created_at": datetime.now(timezone.utc).isoformat(),
              "run_kind": "candidate" if options.compare else "baseline",
              "image": options.image, "scenarios": {}, "resources": {"peak_rss_bytes": 0}}
    if options.compare:
        report["resources"]["baseline_peak_rss_bytes"] = json.loads(options.compare.read_text(encoding="utf-8")).get("resources", {}).get("peak_rss_bytes")
    working = _safe_inspect("mcp1c")
    working_identity = (working["Id"], working["State"]["StartedAt"], working["RestartCount"])
    report["working_before"] = {"id": working_identity[0], "started_at": working_identity[1], "restart_count": working_identity[2]}
    stand = Stand(options.image, data_copy, options.port, report)
    start = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)

    def timeout_handler(*_):
        raise RunDeadlineExceeded("Общий таймаут приёмки исчерпан.")

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(options.timeout_seconds)
    try:
        _copy_data(data_copy, source)
        stand.start()
        for name in scenarios:
            started = time.monotonic()
            try:
                if name == "S1":
                    observations = _scenario_one(stand)
                elif name == "S2":
                    observations = _scenario_two(stand)
                else:
                    from live_stand_scenarios import ScenarioSkip, run_scenario
                    observations = run_scenario(name, stand)
                report["scenarios"][name] = {"status": "PASS", "observations": normalize_response(observations)}
            except Exception as error:
                skipped = type(error).__name__ == "ScenarioSkip"
                report["scenarios"][name] = {"status": "SKIP" if skipped else "FAIL", "reason": str(error),
                    "observations": normalize_response(getattr(error, "observations", {}))}
            report["scenarios"][name]["duration_seconds"] = round(time.monotonic() - started, 3)
            print(f"{name}: {report['scenarios'][name]['status']}", flush=True)
    except (RunDeadlineExceeded, KeyboardInterrupt, Exception) as error:
        report["fatal_error"] = str(error) or type(error).__name__
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        try:
            stand.close()
        except Exception as error:
            report["cleanup_error"] = str(error)
        try:
            current = _safe_inspect("mcp1c")
            identity = (current["Id"], current["State"]["StartedAt"], current["RestartCount"])
            report["working_unchanged"] = identity == working_identity
            report["working_healthy"] = current["State"].get("Health", {}).get("Status") == "healthy"
        except Exception as error:
            report["working_check_error"] = str(error)
        if options.cleanup and "cleanup_error" not in report:
            try:
                if data_copy.exists():
                    validate_data_copy(data_copy, source)
                    shutil.rmtree(data_copy)
                    report.setdefault("cleanup", {})["data_copy_removed"] = True
            except Exception as error:
                report["cleanup_error"] = str(error)
        report["duration_seconds"] = round(time.monotonic() - start, 3)
        if options.compare:
            baseline = json.loads(options.compare.read_text(encoding="utf-8"))
            report["differences"] = compare_reports(baseline, report)
            report["timings"] = {
                name: {"baseline_seconds": baseline.get("scenarios", {}).get(name, {}).get("duration_seconds"),
                       "candidate_seconds": item["duration_seconds"]}
                for name, item in report["scenarios"].items()
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        for token in stand._secret_values:
            serialized = serialized.replace(token, "<секрет>")
        output.write_text(serialized + "\n", encoding="utf-8")
    complete = len(report["scenarios"]) == len(scenarios) and all(item["status"] == "PASS" for item in report["scenarios"].values())
    return 0 if (complete and report.get("working_unchanged") and report.get("working_healthy")
                 and not any(key in report for key in ("fatal_error", "cleanup_error", "working_check_error"))
                 and not report.get("differences")) else 1


if __name__ == "__main__":
    sys.exit(main())
