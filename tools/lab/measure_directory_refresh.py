"""Изолированный замер: исходник только читается, весь Registry живёт в temp.

Без --source создаётся нагрузка из BSL-модулей; она не заменяет настоящую
конфигурацию с формами и ролями. Запуск из корня репозитория:
.venv/bin/python tools/lab/measure_directory_refresh.py --modules 1024 --kib 256
"""
from __future__ import annotations

import argparse
import json
import platform
import resource
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from mcp1c.intake_v2_api import IntakeApiService
from mcp1c.registry import Registry


def synthetic(root: Path, modules: int, kib: int) -> None:
    root.mkdir(parents=True)
    root.joinpath("Configuration.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Configuration>'
        '<Properties><Name>Benchmark</Name><Version>1.0</Version>'
        '<CompatibilityMode>Version8_3_21</CompatibilityMode></Properties>'
        '</Configuration></MetaDataObject>', encoding="utf-8")
    for index in range(modules):
        module = root / "CommonModules" / f"Module{index}" / "Ext" / "Module.bsl"
        module.parent.mkdir(parents=True)
        # Процедуры и исполняемые строки вместо балласта в комментариях.
        parts, size, number = [], 0, 0
        while size < kib * 1024:
            part = (f'Процедура Method{number}() Экспорт\n'
                    + ' Значение = 1;\n' * 20
                    + ' Сообщить(Значение);\nКонецПроцедуры\n')
            parts.append(part)
            size += len(part.encode("utf-8"))
            number += 1
        module.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Корень реальной выгрузки; только копируется")
    parser.add_argument("--modules", type=int, default=1024)
    parser.add_argument("--kib", type=int, default=256)
    args = parser.parse_args()
    if args.modules < 1 or args.kib < 1:
        parser.error("Размеры должны быть положительными")
    with tempfile.TemporaryDirectory(prefix="mcp1c-directory-bench-") as temporary:
        workdir = Path(temporary)
        source = workdir / "mounts" / "config-bench"
        if args.source:
            # Symlink сохраняем для штатного отказа, не читаем его назначение.
            shutil.copytree(args.source, source, symlinks=True)
        else:
            synthetic(source, args.modules, args.kib)
        if any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("Выгрузка с symlink не подходит для стенда")
        root = ET.parse(source / "Configuration.xml").getroot()
        namespace = {"md": "http://v8.1c.ru/8.3/MDClasses"}
        name = root.findtext("md:Configuration/md:Properties/md:Name", namespaces=namespace)
        if not name:
            raise ValueError("Нужен корень основной конфигурации с Configuration.xml")
        files = [p for p in source.rglob("*") if p.is_file() and not p.is_symlink()]
        print(json.dumps({"corpus": "real-copy" if args.source else "synthetic-bsl",
                          "files": len(files), "bytes": sum(p.stat().st_size for p in files),
                          "python": platform.python_version(), "system": platform.system(),
                          "machine": platform.machine()}), flush=True)
        archive = workdir / "structure.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("manifest.json", json.dumps({
                "schema_version": "1", "format": "json", "exporter_version": "benchmark",
                "name": name, "version": "1.0", "platform": "8.3.21",
                "objects_total": 0, "truncated": False, "files": [],
            }))
        registry = Registry(workdir / "data")
        registry.add_configuration(archive, keep_source=False)
        service = IntakeApiService.for_registry(registry, config_sources_root=source.parent)
        # Не обходим штатное окно стабильности файлов.
        time.sleep(5)

        def measured(stage, operation):
            start = time.perf_counter()
            result = operation()
            scale = 2**20 if sys.platform == "darwin" else 1024
            print(json.dumps({"stage": stage, "seconds": round(time.perf_counter() - start, 6),
                              "process_peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale, 2)}), flush=True)
            return result

        measured("bind", lambda: service.bind_directory(name, "config-bench"))
        for scenario in ("initial", "unchanged", "one-file-change"):
            if scenario == "one-file-change":
                modules = sorted(p for p in files if p.suffix.lower() == ".bsl")
                if not modules:
                    print(json.dumps({"scenario": scenario, "skipped": "no-bsl"}), flush=True)
                    continue
                with modules[0].open("a", encoding="utf-8") as output:
                    output.write('\nПроцедура BenchmarkAddedProcedure()\n Сообщить("benchmark");\nКонецПроцедуры\n')
                time.sleep(5)
            measured(scenario + "/directory-state", service.directory_sources)
            candidate = measured(scenario + "/refresh", lambda: service.refresh_directory(name))
            job = measured(scenario + "/start", lambda: service.start(candidate["id"], "update_full"))
            measured(scenario + "/prepare", lambda: service.prepare(job))
            payload = measured(scenario + "/preview", service.jobs_snapshot)
            preview = next(item for item in payload["jobs"] if item["job_id"] == job.job_id)
            if preview["state"] != "done" or preview["preview"] is None:
                raise RuntimeError("Подготовка не завершилась; замер недействителен")
            print(json.dumps({"scenario": scenario, "no_op": preview["preview"]["no_op"]}), flush=True)
            measured(scenario + "/confirm", lambda: service.confirm(job.job_id))


if __name__ == "__main__":
    main()
