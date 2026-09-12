from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import anyio
import pytest

from mcp1c.capability_modules.forms import tools as forms_tools


FIXTURES = Path(__file__).with_name("fixtures")


def _payload() -> dict:
    return json.loads((FIXTURES / "minimal_form.json").read_text(encoding="utf-8"))


class _Result:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, object]:
        return self.payload


@pytest.mark.anyio
async def test_compile_отклоняет_слишком_большую_спецификацию_до_compiler(
    monkeypatch,
):
    payload = _payload()
    payload["title"]["ru"] = "я" * forms_tools.MAX_SPECIFICATION_BYTES
    monkeypatch.setattr(
        forms_tools,
        "compile_managed_form",
        lambda _specification: pytest.fail("compiler не должен вызываться"),
    )

    with pytest.raises(forms_tools.FormsToolInputError, match="specification"):
        await forms_tools._compile_tool(payload)


@pytest.mark.anyio
async def test_checker_отклоняет_слишком_большой_module_bsl_до_checker(monkeypatch):
    monkeypatch.setattr(
        forms_tools,
        "check_managed_form",
        lambda *_args, **_kwargs: pytest.fail("checker не должен вызываться"),
    )

    with pytest.raises(forms_tools.FormsToolInputError, match="module_bsl"):
        await forms_tools._check_tool(
            "<Form/>",
            "Форма",
            "я" * forms_tools.MAX_MODULE_BYTES,
        )


@pytest.mark.anyio
async def test_десять_клиентов_ограничены_двумя_workers_и_не_блокируют_loop(
    monkeypatch,
):
    lock = threading.Lock()
    active = 0
    peak = 0
    completed = 0
    heartbeat_before_completion = False

    def compile_stub(_specification):
        nonlocal active, peak, completed
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
            completed += 1
        return _Result({"status": "compiled"})

    async def call() -> None:
        await forms_tools._compile_tool(_payload())

    async def heartbeat() -> None:
        nonlocal heartbeat_before_completion
        await anyio.sleep(0)
        with lock:
            heartbeat_before_completion = completed < 10

    monkeypatch.setattr(forms_tools, "compile_managed_form", compile_stub)
    async with anyio.create_task_group() as tasks:
        for _ in range(10):
            tasks.start_soon(call)
        tasks.start_soon(heartbeat)

    assert completed == 10
    assert peak == forms_tools.MAX_CONCURRENT_OPERATIONS
    assert heartbeat_before_completion is True


@pytest.mark.anyio
async def test_переполненная_очередь_отклоняется_без_запуска_лишней_работы(
    monkeypatch,
):
    release = threading.Event()

    def compile_stub(_specification):
        release.wait(2)
        return _Result({"status": "compiled"})

    async def call() -> None:
        await forms_tools._compile_tool(_payload())

    monkeypatch.setattr(forms_tools, "compile_managed_form", compile_stub)
    async with anyio.create_task_group() as tasks:
        for _ in range(forms_tools.MAX_PENDING_OPERATIONS):
            tasks.start_soon(call)
        with anyio.fail_after(1):
            while forms_tools._GATE.pending < forms_tools.MAX_PENDING_OPERATIONS:
                await anyio.sleep(0.001)
        with pytest.raises(forms_tools.FormsToolBusyError):
            await forms_tools._compile_tool(_payload())
        release.set()


@pytest.mark.anyio
async def test_timeout_возвращает_явную_ошибку(monkeypatch):
    monkeypatch.setattr(forms_tools._GATE, "timeout_seconds", 0.01)
    monkeypatch.setattr(
        forms_tools,
        "compile_managed_form",
        lambda _specification: (time.sleep(0.05), _Result({"status": "compiled"}))[
            1
        ],
    )

    with pytest.raises(forms_tools.FormsToolTimeoutError):
        await forms_tools._compile_tool(_payload())
    await anyio.sleep(0.06)
    assert forms_tools._GATE.pending == 0


@pytest.mark.anyio
async def test_слишком_большой_ответ_не_возвращается_клиенту(monkeypatch):
    monkeypatch.setattr(
        forms_tools,
        "compile_managed_form",
        lambda _specification: _Result(
            {"content": "я" * forms_tools.MAX_RESULT_BYTES}
        ),
    )

    with pytest.raises(forms_tools.FormsToolResultError):
        await forms_tools._compile_tool(_payload())
