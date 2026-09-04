"""Синтетический admission/read gate без сети и рабочих данных.

Запуск: PYTHONPATH=src .venv/bin/python tools/lab/measure_upload_admission.py
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
from statistics import median
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter
from unittest.mock import patch

import anyio
import httpx2 as httpx
from starlette.applications import Starlette

from mcp1c import dashboard_backend
from mcp1c.dashboard_runtime import routes
from mcp1c.registry import Registry
from mcp1c.server import mcp_guard


async def probe(capacity):
    entered, release = Event(), Event()
    directories = []

    def worker(registry, job, directory, *args, **kwargs):
        directories.append(Path(directory))
        entered.set()
        try:
            assert release.wait(10)
            job["state"] = dashboard_backend.JOB_DONE
        finally:
            shutil.rmtree(directory)

    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    limiter.total_tokens = capacity
    with TemporaryDirectory(prefix="mcp1c-admission-measure-") as folder:
        app = mcp_guard(Starlette(routes=routes(Registry(Path(folder) / "data"))))
        with patch.object(dashboard_backend, "_run_job", worker), patch.object(dashboard_backend, "_JOBS", []):
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                    headers={"x-api-token": "synthetic-admin"},
                ) as client:
                    statuses = []
                    for i in range(41):
                        result = await client.post("/api/v1/sources/upload", files={
                            "file": (f"sample-{i}.hbk", b"synthetic"),
                        })
                        statuses.append(result.status_code)
                    for _ in range(200):
                        if entered.is_set():
                            break
                        await asyncio.sleep(0.005)
                    assert entered.is_set()
                    samples = []
                    for _ in range(50):
                        started = perf_counter()
                        result = await asyncio.wait_for(client.get("/api/v1/sources"), 1)
                        assert result.status_code == 200
                        samples.append((perf_counter() - started) * 1000)
                    assert statuses == [202] + [409] * 40
                    assert limiter.borrowed_tokens == 0
                    print(json.dumps({
                        "read_pool_capacity": capacity, "accepted": statuses.count(202),
                        "rejected": statuses.count(409), "jobs": len(dashboard_backend._JOBS),
                        "temp_directories": len(directories), "borrowed_default_tokens": limiter.borrowed_tokens,
                        "read_runs": 50, "read_median_ms": round(median(samples), 3),
                        "read_p95_ms": round(sorted(samples)[47], 3),
                    }), flush=True)
            finally:
                release.set()
                await asyncio.gather(*tuple(dashboard_backend._ФОНОВЫЕ))
                limiter.total_tokens = original
            assert all(not directory.exists() for directory in directories)


async def main():
    await probe(1)
    await probe(40)


if __name__ == "__main__":
    with patch.dict(os.environ, {"API_TOKEN": "synthetic-read", "ADMIN_TOKEN": "synthetic-admin"}):
        asyncio.run(main())
