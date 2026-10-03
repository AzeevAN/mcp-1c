"""Обезличенный замер стоимости модульных инструментов и размеров исходников.

Пример: PYTHONPATH=src .venv/bin/python tools/lab/measure_backlog_costs.py \
  --archives data/incoming --group ZUP:zup-base.zip \
  --group Retail:retail-base.zip --group Buh:buh-base.zip \
  --baseline-src /tmp/mcp1c-before/src --candidate-src src --work-root /tmp \
  --synthetic-extension

Каждый профиль и версия кода работают в отдельном процессе. ZIP читаются
неизменно; Registry, unpacked members и кэши живут только в TemporaryDirectory.
Имена архивов, конфигураций, расширений, модулей и процедур не печатаются.
Синтетическое расширение измеряет цену разрешения на настоящей базе, но не
утверждает ничего о составе её установленных расширений.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import zipfile
from collections import Counter
from pathlib import Path


LABELS = ("ZUP", "Retail", "Buh")


def _group(raw: str) -> tuple[str, tuple[str, ...]]:
    label, sep, members = raw.partition(":")
    paths = tuple(part.strip() for part in members.split(","))
    if sep != ":" or label not in LABELS or not paths or any(not p for p in paths):
        raise argparse.ArgumentTypeError("group: LABEL:base.zip[,extension.zip...]")
    if any(Path(p).name != p or p in (".", "..") for p in paths):
        raise argparse.ArgumentTypeError("group допускает только имена ZIP в --archives")
    return label, paths


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _metric(call, repeat: int) -> dict[str, object]:
    values = []
    digests = []
    for _ in range(repeat):
        start = time.perf_counter_ns()
        result = call()
        elapsed = time.perf_counter_ns() - start
        values.append(round(elapsed / 1_000_000, 3))
        digests.append(_digest(result))
        if len(set(digests)) != 1:
            raise RuntimeError("ответ изменился между повторами")
    return {
        "first_ms": values[0],
        "warm_median_ms": round(statistics.median(values[1:]), 3)
        if len(values) > 1 else None,
        "response_sha256": digests[0],
    }


def _heap_metric(call) -> dict[str, int]:
    """Отдельный вызов с удержанием результата до снимка памяти."""
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    result = call()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # До освобождения результата измерены retained и пиковая аллокация.
    del result
    return {"retained_bytes": max(0, current - before),
            "peak_delta_bytes": max(0, peak - before)}


def _publish_archive(archive: Path, coordinator, registry, number: int,
                     args, parent: str = "") -> str:
    from mcp1c.intake_v2 import CandidateTransport
    from mcp1c.intake_v2_operations import IntakeCoordinator
    from mcp1c.intake_v2_planner import IntakeAction
    from mcp1c.intake_v2_transport import ZipExportTree

    assert isinstance(coordinator, IntakeCoordinator)
    candidate_id = f"candidate-{number}"
    job_id = f"job-{number}"
    coordinator.create_job(job_id, candidate_id)
    with ZipExportTree(archive, transport=CandidateTransport.LOCAL_FILE,
                       origin_name=f"source-{number}.zip") as tree:
        args.stage = "intake_probe"
        candidate = coordinator.probe(job_id, tree, parent_configuration=parent)
        args.stage = "intake_prepare"
        action = IntakeAction.UPDATE_FULL if parent else IntakeAction.CREATE
        coordinator.prepare(job_id, tree, action=action,
                            active=None, generation_id=f"generation-{number}")
    args.stage = "intake_confirm"
    coordinator.confirm(job_id, registry)
    return (candidate.identity.configuration_name
            or candidate.identity.extension_name)


def _catalog_sizes(loaded) -> dict[str, object]:
    from mcp1c.member_pack import open_stored_member
    from mcp1c.module_content import ContentReadError, read_content_bytes

    counts: Counter[str] = Counter()
    maxima: dict[str, int] = {"file_body": 0, "container_raw": 0,
                              "container_extracted_bsl": 0}
    for item in loaded.каталог.entries.values():
        locator = item.locator
        if locator is None or locator.kind == "compiled":
            counts["compiled_or_without_locator"] += 1
            continue
        if locator.kind == "container":
            with open_stored_member(loaded.корень, locator.relative_path) as stream:
                raw = stream.read()
            maxima["container_raw"] = max(maxima["container_raw"], len(raw))
            del raw
            if locator.relative_path.casefold().endswith((".form", "/form.bin")):
                counts["form_containers"] += 1
            try:
                body = read_content_bytes(loaded.корень, item.address, locator)
            except ContentReadError:
                counts["unreadable_container_entries"] += 1
                continue
            maxima["container_extracted_bsl"] = max(
                maxima["container_extracted_bsl"], len(body))
            counts["readable_container_entries"] += 1
        else:
            body = read_content_bytes(loaded.корень, item.address, locator)
            maxima["file_body"] = max(maxima["file_body"], len(body))
            counts["readable_file_entries"] += 1
    return {"max_bytes": maxima, "counts": dict(sorted(counts.items()))}


def _synthetic_extension(root: Path, base_name: str, base) -> Path:
    """Минимальная native Source B выгрузка с собственным CommonModule."""
    tests = Path(__file__).parents[2] / "tests"
    sys.path.insert(1, str(tests))
    from native_export import выгрузка_в_файлы
    from mcp1c.model import Configuration

    code_root = root / "synthetic-code"
    module_root = code_root / "CommonModules" / "McpLabProbe" / "Ext"
    module_root.mkdir(parents=True)
    (module_root / "Module.bsl").write_text(
        "Функция Проба() Экспорт\n\tВозврат 1;\nКонецФункции\n",
        encoding="utf-8",
    )
    config = Configuration(
        name=base_name, version=base.config.version,
        platform=base.config.platform,
    )
    tree = выгрузка_в_файлы(
        root / "synthetic-export", configuration=config,
        корень_кода=code_root, extension="McpLabExtension",
    )
    # У собственного модуля расширения нет borrowed target в базовой
    # конфигурации. Helper размечает весь код как Adopted для других тестов.
    descriptor = tree / "CommonModules" / "McpLabProbe.xml"
    raw = descriptor.read_bytes()
    marker = b"<ObjectBelonging>Adopted</ObjectBelonging>"
    if raw.count(marker) != 1:
        raise RuntimeError("синтетический дескриптор изменил формат")
    descriptor.write_bytes(raw.replace(marker, b""))
    archive = root / "synthetic-extension.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(tree).as_posix())
    return archive


def _child(args) -> dict[str, object]:
    args.stage = "imports"
    args.synthetic_archive_active = False
    sys.path.insert(0, str(args.src.resolve()))
    from mcp1c.intake_v2 import DurableCandidateStore
    from mcp1c.intake_v2_operations import IntakeCoordinator
    from mcp1c import index_cache, source_ids
    from mcp1c.intake_v2_extensions import resolve_extension_structure
    from mcp1c.registry import Registry
    from mcp1c import tools

    args.stage = "intake"
    with tempfile.TemporaryDirectory(prefix="mcp1c-costs-", dir=args.work_root) as name:
        root = Path(name)
        registry = Registry(root / "registry")
        records = DurableCandidateStore(root / "intake" / "records")
        coordinator = IntakeCoordinator(root / "intake" / "operations", records)
        base_name = ""
        extension_names = []
        for number, filename in enumerate(args.files):
            archive = args.archives / filename
            identity_name = _publish_archive(archive, coordinator, registry, number,
                                             args, parent=base_name)
            if number == 0:
                base_name = identity_name
            else:
                extension_names.append(identity_name)

        if args.synthetic_extension:
            args.stage = "synthetic_extension"
            args.synthetic_base_name = base_name
            archive = _synthetic_extension(root, base_name,
                                           registry.configurations[base_name])
            args.synthetic_archive_active = True
            identity_name = _publish_archive(
                archive, coordinator, registry, len(args.files), args,
                parent=base_name)
            args.synthetic_archive_active = False
            extension_names.append(identity_name)

        args.stage = "selection"
        base = registry.configurations[base_name]
        loaded = registry.modules.get(source_ids.modules_id(base_name))
        if loaded is None or loaded.оглавление is None or loaded.каталог is None:
            raise RuntimeError("модульный индекс не готов")
        # Максимальная форма проверяет наиболее дорогую группировку событий.
        forms = [module for module in loaded.оглавление.модули
                 if loaded.каталог.entries.get(module) is not None
                 and loaded.каталог.entries[module].is_form]
        form = max(forms, key=lambda module: len(loaded.оглавление.модуля(module)),
                   default=None)
        targets = {}
        heaps = {}
        extension_structure = None
        if extension_names:
            ext = extension_names[0]
            ext_key = source_ids.extension_modules_id(
                base_name, index_cache.safe_name(ext))
            ext_loaded = registry.modules.get(ext_key)
            extension_structure = registry.extension_structures.get(ext_key)
            if extension_structure is None:
                raise RuntimeError("структура расширения не готова")
            if ext_loaded is None or ext_loaded.оглавление is None:
                raise RuntimeError("индекс расширения не готов")
            modules = tuple(ext_loaded.оглавление.модули)
            if modules:
                module = modules[0]
                targets["extension_toc"] = lambda: tools.get_procedure(
                    registry, module, config=base_name, extension=ext)
                records = ext_loaded.оглавление.модуля(module)
                if records:
                    address = f"{module}::{records[0].имя}"
                    targets["extension_procedure"] = lambda: tools.get_procedure(
                        registry, address, config=base_name, extension=ext)
                    targets["extension_search"] = lambda: tools.search_procedures(
                        registry, records[0].имя, config=base_name, extension=ext)
        if form is not None:
            targets["form_toc"] = lambda: tools.get_procedure(
                registry, form, config=base_name)
        args.stage = "tool_latency"
        metrics = {key: _metric(call, args.repeat) for key, call in targets.items()}
        args.stage = "heap"
        if extension_structure is not None:
            heaps["last_resolved_extension"] = _heap_metric(
                lambda: resolve_extension_structure(base.config, extension_structure))
        if form is not None:
            raw_group = getattr(type(loaded.формы),
                                "_собрать_события_формы_из_индекса", None)
            if raw_group is None:
                raw_group = type(loaded.формы)._события_формы_из_индекса
            samples = [
                _heap_metric(lambda i=i: raw_group(loaded.формы, i))
                for i in range(len(loaded.формы.модули))
            ]
            heaps["max_one_form_events_grouped"] = {
                "forms_scanned": len(samples),
                "max_retained_bytes": max(item["retained_bytes"] for item in samples),
                "max_peak_delta_bytes": max(item["peak_delta_bytes"] for item in samples),
            }
            heaps["all_form_events_grouped"] = _heap_metric(
                lambda: tuple(
                    raw_group(loaded.формы, i)
                    for i in range(len(loaded.формы.модули))))
        args.stage = "catalog_scan"
        result = {"label": args.label, "metrics": metrics,
                  "heap": heaps, "source_count": len(args.files),
                  "synthetic_extension": args.synthetic_extension,
                  "source_sizes": _catalog_sizes(loaded)}
        if args.acceptance:
            args.stage = "acceptance_digest"
            # Полный digest оглавлений всех модулей, без печати их адресов.
            digest = hashlib.sha256()
            count = 0
            for module in loaded.оглавление.модули:
                digest.update(tools.get_procedure(registry, module,
                                                  config=base_name).encode("utf-8"))
                count += 1
            result["acceptance"] = {"module_tocs": count,
                                    "response_sha256": digest.hexdigest()}
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--group", action="append", type=_group)
    parser.add_argument("--baseline-src", type=Path)
    parser.add_argument("--candidate-src", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--synthetic-extension", action="store_true")
    parser.add_argument("--synthetic-diagnostic", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--src", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--label", help=argparse.SUPPRESS)
    parser.add_argument("--files", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeat < 2:
        parser.error("repeat должен быть >= 2 для cold/warm")
    if not args.work_root.is_dir() or not args.archives.is_dir():
        parser.error("archives и work-root должны существовать")
    if args.child:
        try:
            print(json.dumps(_child(args), ensure_ascii=False, sort_keys=True))
            return 0
        except Exception as error:
            failure = {"completed": False, "stage": getattr(args, "stage", "setup"),
                       "error_type": type(error).__name__}
            if args.synthetic_diagnostic and args.synthetic_archive_active:
                cause = error.__cause__
                failure["synthetic_cause_type"] = (
                    type(cause).__name__ if cause is not None else None)
                detail = str(error)
                base = getattr(args, "synthetic_base_name", "")
                if base:
                    detail = detail.replace(base, "<base>")
                # Только сгенерированная выгрузка; запрещаем произвольный
                # текст и пути вне её известных XML/BSL частей.
                if len(detail) <= 300 and ("McpLab" in detail or "/" not in detail):
                    failure["synthetic_detail"] = detail
            print(json.dumps(failure, ensure_ascii=False))
            return 2
    if not args.group:
        parser.error("нужен хотя бы один --group")
    if len({label for label, _ in args.group}) != len(args.group):
        parser.error("group label повторяется")
    if args.synthetic_extension and any(len(files) != 1 for _, files in args.group):
        parser.error("synthetic-extension требует ровно один base ZIP в каждой group")
    if any(not (args.archives / filename).is_file()
           for _, files in args.group for filename in files):
        parser.error("один из архивов недоступен")
    runs = {}
    for version, src in (("baseline", args.baseline_src),
                         ("candidate", args.candidate_src)):
        if src is None:
            continue
        if not (src / "mcp1c" / "registry.py").is_file():
            parser.error(f"{version}-src не содержит mcp1c")
        rows = []
        for label, files in args.group:
            command = [sys.executable, str(Path(__file__).resolve()),
                       "--archives", str(args.archives), "--candidate-src", str(src),
                       "--work-root", str(args.work_root), "--repeat", str(args.repeat),
                       "--child", "--src", str(src), "--label", label,
                       "--files", *files]
            if args.acceptance:
                command.append("--acceptance")
            if args.synthetic_extension:
                command.append("--synthetic-extension")
            if args.synthetic_diagnostic:
                command.append("--synthetic-diagnostic")
            completed = subprocess.run(command, cwd=Path(__file__).parents[2],
                                       capture_output=True, text=True)
            if completed.returncode:
                try:
                    failure = json.loads(completed.stdout)
                except json.JSONDecodeError:
                    failure = {}
                print(json.dumps({"completed": False, "version": version,
                                  "label": label, "exit_code": completed.returncode,
                                  "stage": failure.get("stage", "child"),
                                  "error_type": failure.get("error_type", "unknown"),
                                  **({"synthetic_cause_type": failure.get("synthetic_cause_type"),
                                      "synthetic_detail": failure.get("synthetic_detail")}
                                     if args.synthetic_diagnostic else {})},
                                 sort_keys=True))
                return 2
            rows.append(json.loads(completed.stdout))
        runs[version] = rows
    parity = None
    if "baseline" in runs:
        parity = all(
            {key: value["response_sha256"] for key, value in left["metrics"].items()}
            == {key: value["response_sha256"] for key, value in right["metrics"].items()}
            and left.get("acceptance") == right.get("acceptance")
            for left, right in zip(runs["baseline"], runs["candidate"])
        )
    print(json.dumps({"schema": "backlog-costs-v1", "completed": True,
                      "repeat": args.repeat, "response_parity": parity, "runs": runs},
                     ensure_ascii=False, sort_keys=True))
    return 0 if parity is not False else 3


if __name__ == "__main__":
    raise SystemExit(main())
