"""Контракты безопасности и сравнения пилотного приёмочного стенда."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture(scope="module")
def stand():
    path = Path(__file__).resolve().parents[1] / "tools/lab/accept_live_stand.py"
    spec = importlib.util.spec_from_file_location("accept_live_stand_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path):
    path = tmp_path.resolve() / "source"
    path.mkdir()
    return path


def write_marker(path, source_dir, **overrides):
    marker = {"task": "audit-2026-10-23", "source": str(source_dir.resolve())}
    marker.update(overrides)
    (path / ".mcp1c-audit-copy").write_text(json.dumps(marker), encoding="utf-8")


@pytest.mark.parametrize("relationship", ["source", "child", "ancestor"])
def test_data_copy_rejects_paths_overlapping_source_even_with_marker(
    stand, source, relationship
):
    paths = {"source": source, "child": source / "copy", "ancestor": source.parent}
    target = paths[relationship]
    target.mkdir(exist_ok=True)
    write_marker(target, source)

    with pytest.raises(ValueError):
        stand.validate_data_copy(target, source)


def test_data_copy_accepts_new_separate_path(stand, source):
    target = source.parent / "new-copy"

    assert stand.validate_data_copy(target, source) == target


def test_data_copy_accepts_existing_marked_copy_for_same_source(stand, source):
    target = source.parent / "copy"
    target.mkdir()
    write_marker(target, source)

    assert stand.validate_data_copy(target, source) == target


def test_data_copy_rejects_existing_unmarked_directory(stand, source):
    target = source.parent / "unrelated"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("чужие данные", encoding="utf-8")

    with pytest.raises(ValueError):
        stand.validate_data_copy(target, source)

    assert sentinel.read_text(encoding="utf-8") == "чужие данные"


@pytest.mark.parametrize("overrides", [{"source": "/other/source"}, {"task": "other-task"}])
def test_data_copy_rejects_marker_for_other_source_or_task(stand, source, overrides):
    target = source.parent / "copy"
    target.mkdir()
    write_marker(target, source, **overrides)

    with pytest.raises(ValueError):
        stand.validate_data_copy(target, source)


@pytest.mark.parametrize("marker_kind", ["directory", "invalid-json", "empty-object"])
def test_data_copy_rejects_invalid_marker(stand, source, marker_kind):
    target = source.parent / "copy"
    target.mkdir()
    marker = target / ".mcp1c-audit-copy"
    if marker_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("{" if marker_kind == "invalid-json" else "{}", encoding="utf-8")

    with pytest.raises(ValueError):
        stand.validate_data_copy(target, source)


@pytest.mark.parametrize("through_parent", [False, True])
def test_data_copy_rejects_symlink_and_path_through_symlink(stand, source, through_parent):
    real = source.parent / "real"
    real.mkdir()
    write_marker(real, source)
    link = source.parent / "link"
    link.symlink_to(real, target_is_directory=True)
    target = link / "new-copy" if through_parent else link

    with pytest.raises(ValueError):
        stand.validate_data_copy(target, source)


def test_data_copy_rejects_existing_file(stand, source):
    target = source.parent / "file"
    target.write_text("не каталог", encoding="utf-8")

    with pytest.raises(ValueError):
        stand.validate_data_copy(target, source)


def test_copy_data_interruption_remains_identifiable_and_cannot_be_reused(
    stand, source, monkeypatch
):
    target = source.parent / "interrupted-copy"

    def interrupted_copytree(src, dst, *args, **kwargs):
        destination = Path(dst)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partial.txt").write_text("неполная запись", encoding="utf-8")
        raise OSError("искусственный обрыв копирования")

    # Подмена воспроизводит обрыв после первой записи, сохраняя реальную логику маркеров.
    with monkeypatch.context() as patch:
        patch.setattr(stand.shutil, "copytree", interrupted_copytree)
        with pytest.raises(OSError):
            stand._copy_data(target, source)

    marker = json.loads((target / ".mcp1c-audit-copy").read_text(encoding="utf-8"))
    assert marker == {"task": "audit-2026-10-23", "source": str(source.resolve())}
    assert (target / "partial.txt").read_text(encoding="utf-8") == "неполная запись"
    assert not (target / ".mcp1c-audit-copy-complete").exists()

    with pytest.raises(ValueError):
        stand._copy_data(target, source)


def test_copy_data_success_is_complete_and_changes_do_not_reach_source(stand, source):
    original = source / "sample.txt"
    original.write_text("исходное содержимое", encoding="utf-8")
    target = source.parent / "complete-copy"

    stand._copy_data(target, source)

    assert (target / ".mcp1c-audit-copy-complete").is_file()
    copied = target / "sample.txt"
    assert copied.read_text(encoding="utf-8") == "исходное содержимое"
    copied.write_text("изменённая копия", encoding="utf-8")
    assert original.read_text(encoding="utf-8") == "исходное содержимое"


@pytest.mark.parametrize(
    ("field", "before", "after"),
    [
        ("runtime_id", "runtime-a", "runtime-b"),
        ("request_id", "request-a", "request-b"),
        ("ticket", "ticket-a", "ticket-b"),
        ("download_url", "https://example.invalid/a", "https://example.invalid/b"),
        ("created_at", "2026-10-22T10:00:00Z", "2026-10-23T11:00:00Z"),
        ("updated_at", "2026-10-22T10:00:00Z", "2026-10-23T11:00:00Z"),
        ("loaded_at", "2026-10-22T10:00:00Z", "2026-10-23T11:00:00Z"),
        ("duration_seconds", 1.0, 25.0),
    ],
)
def test_normalization_ignores_explicit_volatile_fields_recursively(
    stand, field, before, after
):
    baseline = {"items": [{"response": {field: before, "object": "Справочник.Файлы"}}]}
    candidate = {"items": [{"response": {field: after, "object": "Справочник.Файлы"}}]}

    normalized = stand.normalize_response(baseline)
    assert normalized == stand.normalize_response(candidate)
    assert normalized["items"][0]["response"]["object"] == "Справочник.Файлы"
    assert normalized == stand.normalize_response(baseline)


def test_normalization_preserves_semantic_identifiers_addresses_and_errors(stand):
    response = {
        "id": "object-17",
        "source_id": "source-42",
        "object": "Справочник.Файлы",
        "version": "8.3.25",
        "codehash": "synthetic-code-hash",
        "sha256": "synthetic-content-hash",
        "address": "https://example.invalid/mcp?source_id=source-42",
        "url": "https://example.invalid/api/object-17",
        "error": {"code": "SOURCE_NOT_FOUND", "message": "Нет источника source-42"},
        "items": [{"id": "nested-1", "source_id": "nested-source", "count": 3}],
        "text": "runtime_id=runtime-a; 2026-10-23T11:00:00Z",
        "elapsed_seconds": 4.5,
    }

    assert stand.normalize_response(response) == response


def test_normalization_preserves_scalar_values_and_list_order(stand):
    response = ["source-42", 0, False, None, "2026-10-23T11:00:00Z"]

    assert stand.normalize_response(response) == response


def test_report_comparison_has_no_differences_for_equal_observations(stand):
    baseline = {"scenarios": {"S1": {"status": "PASS", "observations": {"count": 3}}}}
    candidate = {"scenarios": {"S1": {"status": "PASS", "observations": {"count": 3}}}}

    assert stand.compare_reports(baseline, candidate) == []


@pytest.mark.parametrize(
    ("field", "before", "after"),
    [
        ("id", "object-1", "object-2"),
        ("source_id", "source-1", "source-2"),
        ("object", "Справочник.Файлы", "Справочник.Клиенты"),
        ("version", "8.3.25", "8.3.26"),
        ("address", "https://example.invalid/a", "https://example.invalid/b"),
        ("error", {"code": "NOT_FOUND"}, {"code": "FORBIDDEN"}),
        ("count", 3, 2),
    ],
)
def test_report_comparison_detects_changed_mcp_data(stand, field, before, after):
    baseline = {"scenarios": {"S1": {"status": "PASS", "observations": [{field: before}]}}}
    candidate = {"scenarios": {"S1": {"status": "PASS", "observations": [{field: after}]}}}

    differences = stand.compare_reports(baseline, candidate)

    assert isinstance(differences, list)
    assert differences
    assert all(isinstance(item, dict) for item in differences)


def test_report_comparison_detects_changed_status(stand):
    baseline = {"scenarios": {"S1": {"status": "PASS", "observations": []}}}
    candidate = {"scenarios": {"S1": {"status": "FAIL", "observations": []}}}

    assert stand.compare_reports(baseline, candidate)


def test_report_comparison_detects_missing_scenario(stand):
    baseline = {"scenarios": {"S1": {"status": "PASS"}, "S2": {"status": "PASS"}}}
    candidate = {"scenarios": {"S1": {"status": "PASS"}}}

    assert stand.compare_reports(baseline, candidate)


def test_report_comparison_ignores_duration_and_nested_runtime_fields(stand):
    baseline = {
        "scenarios": {"S1": {"status": "PASS", "duration_seconds": 1,
                             "observations": [{"runtime_id": "run-a", "count": 3}]}}
    }
    candidate = {
        "scenarios": {"S1": {"status": "PASS", "duration_seconds": 20,
                             "observations": [{"runtime_id": "run-b", "count": 3}]}}
    }

    assert stand.compare_reports(baseline, candidate) == []


@pytest.mark.parametrize(("candidate_rss", "has_difference"), [(90, False), (110, False), (111, True)])
def test_report_comparison_flags_rss_growth_only_above_ten_percent(
    stand, candidate_rss, has_difference
):
    baseline = {"scenarios": {}, "resources": {"peak_rss_bytes": 100}}
    candidate = {"scenarios": {}, "resources": {"peak_rss_bytes": candidate_rss}}

    differences = stand.compare_reports(baseline, candidate)

    assert bool(differences) is has_difference


@pytest.mark.parametrize(
    ("changed_section", "changed_field", "changed_value", "has_difference"),
    [
        (None, None, None, False),
        ("object", "id", "obj2", True),
        ("object", "source_id", "s2", True),
        ("job", "configuration", "OtherCfg", True),
        ("job", "candidate_id", "unrelated-candidate", True),
    ],
)
def test_transport_ids_preserve_links_and_semantic_identifiers(
    stand, changed_section, changed_field, changed_value, has_difference
):
    baseline = {
        "scenarios": {"S7": {"status": "PASS", "observations": {
            "candidate": {"id": "c1", "transport": "incoming", "origin_name": "a.zip"},
            "job": {"job_id": "j1", "candidate_id": "c1", "configuration": "Cfg"},
            "object": {"id": "obj1", "source_id": "s1"},
        }}}
    }
    observations = {
        "candidate": {"id": "c2", "transport": "incoming", "origin_name": "a.zip"},
        "job": {"job_id": "j2", "candidate_id": "c2", "configuration": "Cfg"},
        "object": {"id": "obj1", "source_id": "s1"},
    }
    if changed_section is not None:
        observations[changed_section][changed_field] = changed_value
    candidate = {"scenarios": {"S7": {"status": "PASS", "observations": observations}}}

    assert bool(stand.compare_reports(baseline, candidate)) is has_difference


@pytest.mark.parametrize(
    ("traceback_count", "warnings", "has_difference"),
    [(0, [], False), (1, [], True), (0, ["новое предупреждение"], True)],
)
def test_log_digest_is_ignored_but_log_findings_are_compared(
    stand, traceback_count, warnings, has_difference
):
    baseline = {
        "scenarios": {"S17": {"status": "PASS", "observations": {
            "log_sha256": "a" * 64, "traceback_count": 0, "warnings": [],
        }}}
    }
    candidate = {
        "scenarios": {"S17": {"status": "PASS", "observations": {
            "log_sha256": "b" * 64,
            "traceback_count": traceback_count,
            "warnings": warnings,
        }}}
    }

    assert bool(stand.compare_reports(baseline, candidate)) is has_difference


@pytest.mark.parametrize(
    ("current_rss", "oom_killed", "restart_count", "has_difference"),
    [(110, False, 0, False), (111, False, 0, True),
     (110, True, 0, True), (110, False, 1, True)],
)
def test_resource_observation_metrics_use_top_level_rss_and_preserve_failures(
    stand, current_rss, oom_killed, restart_count, has_difference
):
    baseline = {
        "resources": {"peak_rss_bytes": 100},
        "scenarios": {"S18": {"status": "PASS", "observations": {
            "peak_rss_bytes": 80, "baseline_peak_rss_bytes": 75,
            "rss_ratio": 1.0667, "comparison": "PASS",
            "oom_killed": False, "restart_count": 0,
        }}}
    }
    candidate = {
        "resources": {"peak_rss_bytes": current_rss},
        "scenarios": {"S18": {"status": "PASS", "observations": {
            "peak_rss_bytes": 95, "baseline_peak_rss_bytes": 70,
            "rss_ratio": 1.3571, "comparison": "FAIL",
            "oom_killed": oom_killed, "restart_count": restart_count,
        }}}
    }

    assert bool(stand.compare_reports(baseline, candidate)) is has_difference


@pytest.mark.parametrize(
    ("scenario", "before", "after", "status", "has_difference"),
    [
        ("S8", {"first_seconds": 1, "second_seconds": 2},
         {"first_seconds": 3, "second_seconds": 4}, "PASS", False),
        ("S8", {"first_seconds": 1, "second_seconds": 2},
         {"first_seconds": 3, "second_seconds": 4}, "FAIL", True),
        ("S15", {"scans": [{"seconds": 1, "count": 3}]},
         {"scans": [{"seconds": 4, "count": 3}]}, "PASS", False),
        ("S15", {"scans": [{"seconds": 1, "count": 3}]},
         {"scans": [{"seconds": 4, "count": 3}]}, "FAIL", True),
        ("S1", {"object": {"seconds": 1}},
         {"object": {"seconds": 4}}, "PASS", True),
    ],
)
def test_scan_timing_is_ignored_only_in_measurement_scenarios(
    stand, scenario, before, after, status, has_difference
):
    baseline = {"scenarios": {scenario: {"status": "PASS", "observations": before}}}
    candidate = {"scenarios": {scenario: {"status": status, "observations": after}}}

    assert bool(stand.compare_reports(baseline, candidate)) is has_difference


def test_global_timeout_stops_scenarios_and_cleans_up(stand, tmp_path, monkeypatch):
    root = tmp_path.resolve() / "project"
    source = root / "data"
    source.mkdir(parents=True)
    target = tmp_path.resolve() / "timeout-copy"
    output = tmp_path.resolve() / "timeout-report.json"
    calls = []
    handlers = {stand.signal.SIGALRM: None}

    class FakeStand:
        def __init__(self, image, data_copy, port, report):
            self.report = report
            self.api_token = "synthetic-api-token"
            self.admin_token = "synthetic-admin-token"
            self._secret_values = {self.api_token, self.admin_token}

        def start(self):
            calls.append("start")

        def close(self):
            calls.append("close")

    def copy_data(destination, original):
        destination.mkdir()
        write_marker(destination, original)
        (destination / ".mcp1c-audit-copy-complete").touch()

    def install_handler(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    def scenario_one(active_stand):
        calls.append("S1")
        # Вызываем тот же обработчик, который установил main, без ожидания сигнала ОС.
        handlers[stand.signal.SIGALRM](stand.signal.SIGALRM, None)

    def scenario_two(active_stand):
        calls.append("S2")
        return {"unexpected": "таймаут уже истёк"}

    identity = {
        "Id": "synthetic-working-container",
        "State": {"StartedAt": "2026-10-23T00:00:00Z", "Health": {"Status": "healthy"}},
        "RestartCount": 0,
    }
    monkeypatch.setattr(stand, "ROOT", root)
    monkeypatch.setattr(stand, "Stand", FakeStand)
    monkeypatch.setattr(stand, "_safe_inspect", lambda container: identity)
    monkeypatch.setattr(stand, "_copy_data", copy_data)
    monkeypatch.setattr(stand, "_scenario_one", scenario_one)
    monkeypatch.setattr(stand, "_scenario_two", scenario_two)
    monkeypatch.setattr(stand.signal, "getsignal", lambda signum: handlers[signum])
    monkeypatch.setattr(stand.signal, "signal", install_handler)
    monkeypatch.setattr(stand.signal, "alarm", lambda seconds: None)

    result = stand.main([
        "--image", "synthetic-timeout-test", "--scenarios", "S1,S2",
        "--timeout-seconds", "1", "--report", str(output),
        "--data-copy", str(target), "--cleanup",
    ])

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == 1
    assert "S2" not in calls
    assert "S2" not in report["scenarios"]
    assert report["fatal_error"]
    assert "close" in calls
    assert report["cleanup"]["data_copy_removed"] is True
    assert not target.exists()
    assert source.is_dir()


@pytest.mark.parametrize(
    ("change", "has_difference"),
    [("renumbered", False), ("broken-link", True),
     ("content-digest", True), ("generic-digest", True)],
)
def test_generation_transport_ids_preserve_commit_links_and_content_digests(
    stand, change, has_difference
):
    baseline = {
        "scenarios": {"S7": {"status": "PASS", "observations": {
            "sha256": "semantic-digest-a",
            "job": {
                "preview": {
                    "base_generation_id": "generation-old-a",
                    "candidate_generation_id": "generation-new-a",
                    "layers": [{"current": {"content_sha256": "current-content"},
                                "candidate": {"content_sha256": "candidate-content"}}],
                },
                "commit": {
                    "no_op": False, "generation_id": "generation-new-a",
                    "manifest_sha256": "manifest-digest-a", "applied_layers": ["layer-1"],
                },
            },
        }}}
    }
    observations = {
        "sha256": "semantic-digest-a",
        "job": {
            "preview": {
                "base_generation_id": "generation-old-b",
                "candidate_generation_id": "generation-new-b",
                "layers": [{"current": {"content_sha256": "current-content"},
                            "candidate": {"content_sha256": "candidate-content"}}],
            },
            "commit": {
                "no_op": False, "generation_id": "generation-new-b",
                "manifest_sha256": "manifest-digest-b", "applied_layers": ["layer-1"],
            },
        },
    }
    if change == "broken-link":
        observations["job"]["commit"]["generation_id"] = "generation-unrelated"
    elif change == "content-digest":
        observations["job"]["preview"]["layers"][0]["candidate"]["content_sha256"] = "changed-content"
    elif change == "generic-digest":
        observations["sha256"] = "semantic-digest-b"
    candidate = {"scenarios": {"S7": {"status": "PASS", "observations": observations}}}

    assert bool(stand.compare_reports(baseline, candidate)) is has_difference


def test_isolation_cleanup_failure_preserves_copy_and_parent_retries(
    stand, source, monkeypatch
):
    (source / "sample.txt").write_text("исходные данные", encoding="utf-8")
    report = {"resources": {"peak_rss_bytes": 0}}
    parent = stand.Stand("synthetic-cleanup-test", source, 5091, report)
    child_id = "c" * 64
    commands = []
    failed_once = False
    original_close = stand.Stand.close

    def start(active_stand):
        active_stand.container_id = child_id

    def inspect(container):
        return {
            "Id": child_id, "Labels": {"mcp1c.task": stand.TASK},
            "State": {"OOMKilled": False}, "RestartCount": 0,
        }

    def run(command, **kwargs):
        if command[:2] in (["docker", "stop"], ["docker", "rm"]):
            assert child_path.is_dir(), "Копия удалена до успешной уборки контейнера"
        commands.append(command)
        return ""

    def close(active_stand):
        nonlocal failed_once
        if active_stand.container_name == "mcp1c-audit-isolation" and not failed_once:
            failed_once = True
            raise RuntimeError("искусственный сбой уборки дочернего стенда")
        return original_close(active_stand)

    monkeypatch.setattr(stand.Stand, "start", start)
    monkeypatch.setattr(stand.Stand, "logs", lambda active_stand: "")
    monkeypatch.setattr(stand.Stand, "close", close)
    monkeypatch.setattr(stand, "_safe_inspect", inspect)
    monkeypatch.setattr(stand, "_run", run)

    # Контракт допускает передачу ошибки вызывающему; копия нужна для повторной уборки.
    try:
        with parent.isolation_copy() as child:
            child_path = child.data_copy
            assert (child_path / "sample.txt").read_text(encoding="utf-8") == "исходные данные"
    except RuntimeError as error:
        assert "искусственный сбой уборки" in str(error)

    assert child_path.is_dir()
    assert (child_path / "sample.txt").read_text(encoding="utf-8") == "исходные данные"
    assert report["isolation"]["cleanup_error"]
    assert ["docker", "rm", child_id] not in commands

    parent.close()

    assert ["docker", "stop", "--time", "10", child_id] in commands
    assert ["docker", "rm", child_id] in commands
    assert report["isolation"]["cleanup"]["removed_container"] == child_id
    assert not child_path.exists()
    assert (source / "sample.txt").read_text(encoding="utf-8") == "исходные данные"


def test_container_cleanup_preserves_existing_resource_receipts(stand, source, monkeypatch):
    report = {"cleanup": {"browser_closed": True, "playwright_stopped": True}}
    active_stand = stand.Stand("synthetic-cleanup-test", source, 5091, report)
    container_id = "d" * 64
    active_stand.container_id = container_id
    inspected = {
        "Id": container_id, "Labels": {"mcp1c.task": stand.TASK},
        "State": {"OOMKilled": False}, "RestartCount": 0,
    }
    monkeypatch.setattr(stand, "_safe_inspect", lambda container: inspected)
    monkeypatch.setattr(stand, "_run", lambda command, **kwargs: "")
    monkeypatch.setattr(active_stand, "logs", lambda: "")

    active_stand.close()

    assert report["cleanup"] == {
        "browser_closed": True,
        "playwright_stopped": True,
        "removed_container": container_id,
    }


# M14 (ревью реализации аудита): S16 воспроизводится из репозитория по
# флагу --ui, а итог отличает полный PASS от PASS без UI — SKIP не PASS.

@pytest.fixture(scope="module")
def scenarios_module(stand):
    path = Path(__file__).resolve().parents[1] / "tools/lab/live_stand_scenarios.py"
    spec = importlib.util.spec_from_file_location("live_stand_scenarios_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _m14_report(statuses):
    return {
        "scenarios": {name: {"status": status} for name, status in statuses.items()},
        "working_unchanged": True,
        "working_healthy": True,
    }


@pytest.mark.parametrize(
    ("statuses", "ui", "expected"),
    [
        ({"S1": "PASS", "S16": "PASS"}, True, ("PASS", 0)),
        ({"S1": "PASS", "S16": "SKIP"}, False, ("PASS_WITHOUT_UI", 3)),
        ({"S1": "PASS", "S16": "SKIP"}, True, ("FAIL", 1)),
        ({"S1": "SKIP", "S16": "PASS"}, True, ("FAIL", 1)),
        ({"S1": "PASS", "S16": "FAIL"}, True, ("FAIL", 1)),
        ({"S1": "PASS"}, False, ("PASS", 0)),
    ],
)
def test_m14_итог_различает_pass_без_ui(stand, statuses, ui, expected):
    report = _m14_report(statuses)

    assert stand.overall_result(report, list(statuses), ui=ui) == expected


def test_m14_итог_fail_при_изменении_рабочего_контейнера(stand):
    report = _m14_report({"S1": "PASS"})
    report["working_unchanged"] = False

    assert stand.overall_result(report, ["S1"], ui=False) == ("FAIL", 1)


def test_m14_s16_без_флага_ui_пропускается_с_причиной(scenarios_module):
    class FakeStand:
        ui = None

    with pytest.raises(scenarios_module.ScenarioSkip, match="--ui"):
        scenarios_module.run_scenario("S16", FakeStand())


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_m14_s16_с_флагом_ui_проводит_браузерную_проверку(
    scenarios_module, monkeypatch, tmp_path, status
):
    calls = []

    def fake_proof(base_url, admin_token, evidence_dir, *, chrome=None):
        calls.append((base_url, admin_token, evidence_dir, chrome))
        return {"status": status, "steps": {}, "secret_values": ["cookie-secret"]}

    class FakeStand:
        port = 5099
        admin_token = "synthetic-admin"
        _secret_values = {"synthetic-admin"}
        ui = {"evidence_dir": tmp_path / "evidence", "chrome": None}

    monkeypatch.setattr(scenarios_module, "run_ui_proof", fake_proof)
    fake = FakeStand()
    if status == "PASS":
        observations = scenarios_module.run_scenario("S16", fake)
        assert observations["status"] == "PASS"
    else:
        with pytest.raises(AssertionError):
            scenarios_module.run_scenario("S16", fake)
    assert calls == [("http://127.0.0.1:5099", "synthetic-admin", tmp_path / "evidence", None)]
    assert "cookie-secret" in fake._secret_values


def test_m14_ui_требует_каталог_доказательств(stand, tmp_path):
    with pytest.raises(SystemExit):
        stand.main([
            "--image", "synthetic", "--data-copy", str(tmp_path / "copy"),
            "--report", str(tmp_path / "report.json"), "--ui",
        ])


def test_m14_symlink_путь_копии_подсказывает_private_tmp(stand, source, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="/private/tmp"):
        stand.validate_data_copy(link / "copy", source)
