"""Сценарии живой приёмки; доступ к контейнеру даёт только объект стенда.

Модуль не запускает процессы при импорте. Все изменения входов выполняются
в копии данных стенда; исходные архивы и рабочий сервер только читаются
внешним runner. Секреты передаются HTTP-адаптеру и не входят в наблюдения.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import quote
import uuid
import xml.etree.ElementTree as ET
import zipfile


INTAKE = "/api/v1/sources/intake"
SOURCES = "/api/v1/sources"
CAPABILITIES = "/api/v1/capabilities"
CAPABILITY_TOOLS = {
    "reference": {"search_reference", "get_reference"},
    "role_access": {"find_roles_for_access", "get_role_access"},
    "module_source_download": {"get_module_source_file"},
}
POLL_SECONDS = 0.5
JOB_TIMEOUT_SECONDS = 1200


class ScenarioSkip(RuntimeError):
    """Условия сценария отсутствуют; это не доказательство успешной проверки."""

    def __init__(self, reason: str, observations: dict | None = None):
        super().__init__(reason)
        self.observations = observations or {}


def _check(condition: bool, message: str, observations: dict | None = None) -> None:
    if not condition:
        error = AssertionError(message)
        error.observations = observations or {}
        raise error


def check_syntax_import(before: set[str], after: list[dict], expected_id: str,
                        *, receipt: str | None = None) -> None:
    after_ids = {row["id"] for row in after}
    expected_ids = before | {expected_id}
    expected_platform = expected_id.removeprefix("syntax-")
    evidence = {
        "before_ids": sorted(before), "after_ids": sorted(after_ids),
        "expected_id": expected_id, "expected_platform": expected_platform,
        "unexpected_ids": sorted(after_ids - expected_ids),
        "missing_ids": sorted(expected_ids - after_ids),
    }
    failures = []
    if after_ids != expected_ids:
        failures.append("набор ID после загрузки отличается от ожидаемого")
    if receipt is not None:
        evidence["receipt"] = receipt
        matches = list(re.finditer(
            r"^добавлено: (?P<id>\S+)  \(syntax, платформа (?P<platform>[^,\r\n]+), "
            r"элементов (?P<items>\d+)\)[ \t]*$",
            receipt, re.MULTILINE,
        ))
        if len(matches) != 1:
            failures.append("CLI не вернул единственную квитанцию добавления справки")
        else:
            actual_id, actual_platform = matches[0]["id"], matches[0]["platform"]
            evidence.update(receipt_source_id=actual_id, receipt_platform=actual_platform)
            if actual_id != expected_id or actual_platform != expected_platform:
                failures.append("CLI вернул другой ID или другую платформу принятого источника")
    _check(not failures, "M1: " + "; ".join(failures), evidence)


def _sources(stand) -> dict:
    return stand.json("GET", SOURCES, admin=True)


def _configurations(stand) -> list[dict]:
    return _sources(stand)["configurations"]


def _mcp(stand, name: str, arguments: dict) -> dict:
    result = stand.mcp(name, arguments)
    _check(not result.get("is_error", False), f"MCP {name} вернул ошибку", result)
    return result


def _text(result: dict) -> str:
    return "\n".join(
        block["text"] if isinstance(block.get("text"), str)
        else json.dumps(block.get("text"), ensure_ascii=False)
        for block in result.get("content", []) if "text" in block
    )


def _mcp_json(stand, name: str, arguments: dict) -> dict:
    return json.loads(_text(_mcp(stand, name, arguments)))


def _response_payload(payload: bytes) -> object:
    try:
        return json.loads(payload)
    except (ValueError, UnicodeError):
        return payload.decode("utf-8", errors="replace")


def _inside_copy(stand, relative: str) -> Path:
    root = stand.data_copy.resolve()
    path = (root / relative).resolve()
    _check(root in path.parents, "Путь изменения выходит за копию данных стенда")
    return path


def _container_path(stand, path: Path) -> str:
    relative = path.resolve().relative_to(stand.data_copy.resolve())
    return "/data/" + relative.as_posix()


def _workdir(stand) -> Path:
    path = _inside_copy(stand, "live-acceptance")
    path.mkdir(exist_ok=True)
    return path


def _fixture_name(stand, prefix: str) -> str:
    # Отдельная последовательность на сценарий: ожидаемый baseline FAIL
    # не сдвигает имена входов во всех последующих candidate-сценариях.
    scenario = getattr(stand, "_fixture_scenario", "s13")
    counters = stand.report.setdefault("fixture_sequence", {})
    key = scenario + "/" + prefix
    counters[key] = counters.get(key, 0) + 1
    return f"live-accept-{scenario}-{prefix}-{counters[key]:03d}"


def _fixture_path(stand, directory: str, prefix: str, suffix: str = ".zip") -> Path:
    path = _inside_copy(stand, directory + "/" + _fixture_name(stand, prefix) + suffix)
    _check(not path.exists(), "Путь фикстуры уже занят; перезапись запрещена")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _zip_member(archive: zipfile.ZipFile, name: str, content: str) -> None:
    member = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(member, content.encode("utf-8"))


def _incoming_marker(stand) -> list[tuple]:
    root = _inside_copy(stand, "incoming")
    if not root.exists():
        return []
    return sorted(
        (path.relative_to(root).as_posix(), path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    )


def _remove(stand, source_id: str) -> dict:
    return stand.json("POST", SOURCES + "/remove", {
        "id": source_id, "confirmation": source_id,
    }, admin=True)


def _wait_job(stand, job_id: str) -> dict:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        job = stand.json("GET", INTAKE + "/jobs/" + job_id, admin=True)["job"]
        _check(job["state"] != "failed", "Разбор кандидата завершился ошибкой", job)
        if job["state"] == "done":
            _check(job.get("preview") is not None or job.get("commit") is not None,
                   "У готовой операции нет preview или commit", job)
            return job
        stand.sample_resources()
        time.sleep(POLL_SECONDS)
    raise AssertionError("Истекло время ожидания preview: " + job_id)


def _start(stand, candidate: dict, *, parent: str = "", confirm: bool = True) -> dict:
    actions = candidate.get("actions", [])
    _check(bool(actions), "У кандидата нет разрешённого действия", candidate)
    body = {"candidate_id": candidate["id"], "action": actions[0]}
    if parent:
        body["parent_configuration"] = parent
    job = stand.json("POST", INTAKE + "/start", body, admin=True)["job"]
    prepared = _wait_job(stand, job["job_id"])
    if not confirm:
        return prepared
    return stand.json("POST", INTAKE + "/confirm", {
        "job_id": job["job_id"],
    }, admin=True)["job"]


def _upload(stand, path: Path, *, intake: bool = True, name: str | None = None) -> dict:
    # Минимальный multipart нужен для настоящего browser-upload HTTP-пути.
    boundary = "mcp1c-accept-" + uuid.uuid4().hex
    filename = name or path.name
    _check(not any(char in filename for char in '\r\n"'), "Небезопасное имя upload")
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    body = prefix + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    status, payload, _ = stand.request(
        "POST", INTAKE + "/upload" if intake else SOURCES + "/upload",
        body, admin=True,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
    )
    _check(status in (201, 202), f"Upload завершился HTTP {status}", {
        "response": _response_payload(payload),
    })
    return json.loads(payload)


def _wait_legacy_upload(stand, name: str) -> dict:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        jobs = stand.json("GET", SOURCES + "/admin", admin=True)["jobs"]
        job = next((row for row in jobs if row["name"] == name), None)
        if job:
            _check(job["state"] != "ошибка", "Загрузка источника завершилась ошибкой", job)
            if job["state"] == "готово":
                return job
        stand.sample_resources()
        time.sleep(POLL_SECONDS)
    raise AssertionError("Истекло время ожидания загрузки источника")


def _archive_identity(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        manifests = [name for name in names if name.rsplit("/", 1)[-1] in {"manifest.json", "manifest.xml"}]
        if manifests:
            manifest_name = min(manifests, key=lambda name: (name.count("/"), len(name)))
            if manifest_name.endswith(".json"):
                manifest = json.loads(archive.read(manifest_name))
            else:
                root = ET.fromstring(archive.read(manifest_name))
                manifest = dict(root.attrib)
                files = root.find("files")
                manifest["files"] = [dict(row.attrib) for row in files] if files is not None else []
            return {"name": manifest["name"], "version": manifest.get("version", ""),
                    "kind": "source-a", "manifest": manifest,
                    "archive_root": manifest_name[:-len(manifest_name.rsplit("/", 1)[-1])]}
        descriptors = [name for name in names if name.split("/")[-1] == "Configuration.xml"]
        _check(bool(descriptors), "В архиве нет Configuration.xml")
        descriptor = min(descriptors, key=lambda name: (name.count("/"), len(name)))
        properties = {}
        # Properties идут до ChildObjects: большие дескрипторы целиком не читаем.
        with archive.open(descriptor) as stream:
            depth, properties_depth = 0, None
            for event, element in ET.iterparse(stream, events=("start", "end")):
                tag = element.tag.rsplit("}", 1)[-1]
                if event == "start":
                    depth += 1
                    if tag == "Properties" and properties_depth is None:
                        properties_depth = depth
                else:
                    if properties_depth is not None and depth == properties_depth + 1:
                        properties[tag] = element.text or ""
                    if tag == "Properties" and depth == properties_depth:
                        break
                    depth -= 1
        _check(bool(properties.get("Name")), "В дескрипторе нет имени конфигурации")
        return {"name": properties["Name"], "version": properties.get("Version", ""),
                "kind": "extension" if properties.get("ConfigurationExtensionPurpose")
                else "source-b"}


def _archives(stand, directory: str) -> list[Path]:
    root = stand.root / "__data" / directory
    if not root.is_dir():
        raise ScenarioSkip("Нет каталога исходных архивов: __data/" + directory)
    return sorted((path for path in root.rglob("*.zip") if not path.is_symlink()),
                  key=lambda path: (path.stat().st_size, path.name))


def _small_source_b(stand) -> Path:
    root = stand.root / "__data"
    paths = sorted((path for path in root.glob("*/ConfigFiles.zip") if not path.is_symlink()),
                   key=lambda path: path.stat().st_size)
    if not paths:
        raise ScenarioSkip("Нет пилотного ConfigFiles.zip в __data")
    return paths[0]


def _incoming_candidate(stand, path: Path) -> dict:
    snapshot = stand.json("GET", INTAKE, admin=True)
    row = next((row for row in snapshot["candidates"]
                if row["origin_name"] == path.name and row["transport"] == "incoming"), None)
    if row is None:
        # Источник обязан пройти настоящее окно стабильности приёмника.
        time.sleep(5)
        snapshot = stand.json("GET", INTAKE, admin=True)
        row = next((row for row in snapshot["candidates"]
                    if row["origin_name"] == path.name and row["transport"] == "incoming"), None)
    _check(row is not None, "Incoming-файл не стал кандидатом", {
        "origin_name": path.name, "issues": snapshot.get("issues", []),
    })
    return row


def _stage_incoming(stand, source: Path) -> Path:
    target = _fixture_path(stand, "incoming", "input")
    shutil.copyfile(source, target)
    return target


def _restart_api(stand) -> dict:
    old = stand.json("GET", "/health")["runtime_id"]
    result = stand.json("POST", "/api/v1/server/restart", {}, admin=True)
    _check(result.get("state") == "restarting", "Сервер не принял перезапуск", result)
    stand.requested_restarts += 1
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            health = stand.json("GET", "/health")
            if health.get("runtime_id") != old:
                stand.wait_ready()
                return {"old_runtime_id": old, "runtime_id": health["runtime_id"]}
        except (OSError, http.client.HTTPException, AssertionError, ValueError):
            pass
        time.sleep(POLL_SECONDS)
    raise AssertionError("После API-перезапуска не появился новый runtime_id")


@contextmanager
def _capabilities(stand, desired: set[str]):
    original = stand.json("GET", CAPABILITIES, admin=True)
    _check(original["active"] == original["desired"],
           "До сценария уже есть неприменённые capability-настройки", original)
    changed = set(original["active"]) != desired
    try:
        if changed:
            stand.json("PUT", CAPABILITIES, {"enabled": sorted(desired)}, admin=True)
            _restart_api(stand)
        yield
    finally:
        if changed:
            stand.json("PUT", CAPABILITIES, {"enabled": original["desired"]}, admin=True)
            _restart_api(stand)


def _s3(stand) -> dict:
    before = stand.snapshot()
    settings = stand.json("GET", CAPABILITIES, admin=True)
    observations = {"before": before["tools"], "active_before": settings["active"]}
    desired = (set(settings["active"]) | {"role_access", "reference"}) - {"module_source_download"}
    with _capabilities(stand, desired):
        after = set(stand.snapshot()["tools"])
        expected = set(before["tools"])
        for capability, names in CAPABILITY_TOOLS.items():
            if capability in desired:
                expected.update(names)
            else:
                expected.difference_update(names)
        observations["enabled_tools"] = sorted(after)
        _check(after == expected, "tools/list изменился сверх переключённых capability", observations)
        status, _, _ = stand.request("GET", "/api/v1/modules/source")
        observations["disabled_download_status"] = status
        _check(status == 404, "Скачивание модуля не отключено", observations)
    restored = stand.snapshot()
    observations["restored_tools"] = restored["tools"]
    _check(set(restored["tools"]) == set(before["tools"]), "tools/list не восстановлен", observations)
    return observations


def _s4(stand) -> dict:
    active = set(stand.json("GET", CAPABILITIES, admin=True)["active"])
    observations = {}
    with _capabilities(stand, active | {"reference"}):
        for platform in (None, "8.3.5", "8.3.27"):
            args = {"query": "УНИКАЛЬНО", "domain": "query", "limit": 5}
            if platform:
                args["platform"] = platform
            found = _mcp_json(stand, "search_reference", args)
            rows = found.get("results", []) + found.get("unavailable_matches", [])
            _check(bool(rows), "Общая справка не нашла известную конструкцию", found)
            card_args = {"item_id": rows[0]["id"]}
            if platform:
                card_args["platform"] = platform
            cards, seen = [], set()
            while True:
                card = _mcp_json(stand, "get_reference", card_args)
                cards.append(card)
                cursor = card.get("next_cursor")
                if cursor is None:
                    break
                _check(cursor not in seen and len(cards) < 100,
                       "Пагинация общей справки не завершается")
                seen.add(cursor)
                card_args["cursor"] = cursor
            observations[platform or "default"] = {"search": found, "cards": cards}
    return observations


def _s5(stand) -> dict:
    observations, added = {"sources": [], "calls": {}, "errors": []}, []
    initial = {row["id"] for row in _sources(stand)["references"]}
    configurations = _configurations(stand)
    if not configurations:
        raise ScenarioSkip("Для проверки выбора справки нужна конфигурация в копии стенда", observations)
    selected = configurations[0]["id"]
    original_declaration = configurations[0]["platform_declaration"]
    observations.update(configuration=selected, original_platform_declaration=original_declaration)
    try:
        for version in ("8.3.27", "8.5.1.1529"):
            candidates = []
            for path in sorted((stand.root / "reference-lab" / "hbk" / version).glob("shcntx_ru*.hbk")):
                match = re.fullmatch(r"shcntx_ru(?:[_-](8\.\d+\.\d+(?:\.\d+)?))?\.hbk", path.name)
                if path.is_file() and not path.is_symlink() and match:
                    full_version = match[1] or version
                    if full_version == version or full_version.startswith(version + "."):
                        candidates.append((path, full_version))
            if not candidates:
                # Точка после трёх частей версии — обязательная граница:
                # запрос 8.3.27 не должен выбрать файл для 8.3.270.
                suffix = r"(?:\.\d+)?" if len(version.split(".")) == 3 else ""
                pattern = re.compile(r"syntax-(" + re.escape(version) + suffix + r")\.hbk")
                for path in sorted((stand.root / "__data" / "1c_config").glob("syntax-*.hbk")):
                    match = pattern.fullmatch(path.name)
                    if path.is_file() and not path.is_symlink() and match:
                        candidates.append((path, match[1]))
            if not candidates:
                raise ScenarioSkip("Нет синтакс-помощника для " + version, observations)
            source, full_version = candidates[0]
            target = _workdir(stand) / ("shcntx_ru_" + full_version + ".hbk")
            _check(not target.exists(), "Путь HBK фикстуры уже занят")
            shutil.copyfile(source, target)
            before_ids = {row["id"] for row in _sources(stand)["references"]}
            receipt = stand.cli("reg-add", "--data", "/data", _container_path(stand, target))
            stand.json("POST", "/admin/reload", {}, admin=True)
            refs = _sources(stand)["references"]
            added.extend(row["id"] for row in refs if row["id"] not in initial and row["id"] not in added)
            imported = {
                "requested_version": version, "filename_version": full_version,
                "upload_name": target.name, "references": refs, "receipt": receipt,
                "before_ids": sorted(before_ids), "after_ids": sorted(row["id"] for row in refs),
            }
            observations["sources"].append(imported)
            try:
                check_syntax_import(before_ids, refs, "syntax-" + full_version, receipt=receipt)
            except AssertionError as error:
                imported["import_status"] = "failed"
                observations["errors"].append({
                    "version": full_version, "reason": str(error),
                    "evidence": getattr(error, "observations", {}),
                })
                # Старая корректная справка в копии не доказывает, что
                # текущий импорт прошёл: её ответы не подменяют результат.
                observations["calls"][version] = {"state": "not_checked_import_failed"}
                continue
            imported["import_status"] = "passed"
            try:
                stand.json("POST", SOURCES + "/platform/set", {
                    "configuration": selected, "platform_version": full_version,
                }, admin=True)
                overview = _mcp(stand, "list_configurations", {})
                block = re.search(
                    r"^## " + re.escape(selected) + r"\n(.*?)(?=^## |\Z)",
                    _text(overview), re.MULTILINE | re.DOTALL,
                )
                _check(block is not None, "Выбранная конфигурация отсутствует в MCP", overview)
                _check("платформа **" + full_version + "**" in block[1]
                       and "Синтаксис платформы: справка " + full_version + " — " in block[1],
                       "MCP не подтвердил выбор импортированной версии справки", {
                           "requested_platform": full_version, "configuration_block": block[0],
                       })
                observations["calls"][version] = {
                    "state": "checked", "platform_version": full_version,
                    "list_configurations": overview,
                    "search": _mcp(stand, "search_syntax", {
                        "query": "СтрРазделить", "limit": 5, "config": selected,
                    }),
                    "card": _mcp(stand, "get_syntax", {
                        "name": "СтрРазделить", "detail": "full", "config": selected,
                    }),
                }
            finally:
                if original_declaration is None:
                    stand.json("POST", SOURCES + "/platform/clear", {
                        "configuration": selected,
                    }, admin=True)
                else:
                    stand.json("POST", SOURCES + "/platform/set", {
                        "configuration": selected, "platform_version": original_declaration["version"],
                    }, admin=True)
                restored = next((row for row in _configurations(stand) if row["id"] == selected), None)
                _check(restored is not None and restored["platform_declaration"] == original_declaration,
                       "Исходная декларация платформы не восстановлена", {
                           "original": original_declaration, "restored": restored,
                       })
        if not observations["errors"]:
            refs = _sources(stand)["references"]
            latest = max(refs, key=lambda row: tuple(int(part) for part in row["platform"].split(".")))
            _check(latest["id"] == "syntax-8.5.1.1529", "Новая справка не стала самой новой", observations)
    finally:
        for source_id in reversed(added):
            _remove(stand, source_id)
        observations["removed"] = added
    _check(not observations["errors"], "M1: загрузка справки зарегистрировала неверный источник", observations)
    return observations


def _source_a_object(path: Path, identity: dict) -> str:
    with zipfile.ZipFile(path) as archive:
        for entry in identity["manifest"].get("files", []):
            member = identity.get("archive_root", "") + entry["path"]
            if entry["path"].endswith(".json"):
                payload = json.loads(archive.read(member))
                rows = payload if isinstance(payload, list) else payload.get("objects", [])
                if rows:
                    row = rows[0]
                    return row.get("full_name") or row["type"] + "." + row["name"]
            elif entry["path"].endswith(".xml"):
                root = ET.fromstring(archive.read(member))
                for row in root.iter():
                    if row.get("name") and row.get("type"):
                        return row.get("full_name") or row.get("type") + "." + row.get("name")
    raise ScenarioSkip("В Source A не найден объект для проверки get_object")


def _s6(stand) -> dict:
    inputs = _archives(stand, "1c_config")
    source_b = []
    for directory in (stand.root / "__data").iterdir():
        if directory.is_dir() and directory.name != "1c_config":
            for path in directory.rglob("*.zip"):
                if path.is_file() and not path.is_symlink():
                    # Имя архива произвольное; чужие ZIP отсеиваются по
                    # маркеру до чтения и разбора их содержимого.
                    try:
                        with zipfile.ZipFile(path) as archive:
                            if not any(name.rsplit("/", 1)[-1] == "Configuration.xml"
                                       for name in archive.namelist()):
                                continue
                    except zipfile.BadZipFile:
                        continue
                    identity = _archive_identity(path)
                    if identity["kind"] == "source-b":
                        source_b.append((path, identity))
    matched = []
    for path in inputs:
        identity = _archive_identity(path)
        other = next((item for item in source_b if item[1]["name"] == identity["name"]), None)
        if identity["kind"] == "source-a" and other:
            matched.append((path, identity, other[0]))
    pilot = _archive_identity(_small_source_b(stand))["name"]
    large = {identity["name"] for path, identity in source_b
             if set(path.relative_to(stand.root / "__data").parts) & {"doo", "doc_oborot"}}
    selected = [row for row in matched if row[1]["name"] == pilot]
    selected.extend(row for row in matched if row[1]["name"] in large)
    unique = {}
    for row in selected:
        unique.setdefault(row[1]["name"], row)
    selected = list(unique.values())
    if len(selected) < 2:
        raise ScenarioSkip("Нужны Source A пилота и Документооборота и полные Source B с теми же именами", {
            "matched_pairs": len(matched),
            "required_pairs": len(selected),
        })
    observations = {"configurations": [], "round_trip_exact": True, "round_trip_same_lines": True}
    for source_a, identity, source_b_path in selected[:2]:
        name = identity["name"]
        full_name = _source_a_object(source_a, identity)
        row = {"configuration": name, "object": full_name}
        local_a = _fixture_path(stand, "live-acceptance", "source-a")
        shutil.copyfile(source_a, local_a)
        _upload(stand, local_a, intake=False)
        _wait_legacy_upload(stand, local_a.name)
        row["source_a_card"] = _mcp(stand, "get_object", {"full_name": full_name, "config": name})
        incoming = _stage_incoming(stand, source_b_path)
        candidate = _incoming_candidate(stand, incoming)
        row["source_b_job"] = _start(stand, candidate)
        row["source_b_card"] = _mcp(stand, "get_object", {"full_name": full_name, "config": name})
        # Возврат B -> A проходит штатным путём полной загрузки структуры.
        local_return = _fixture_path(stand, "live-acceptance", "source-a-return")
        shutil.copyfile(source_a, local_return)
        _upload(stand, local_return, intake=False)
        _wait_legacy_upload(stand, local_return.name)
        row["returned_card"] = _mcp(stand, "get_object", {"full_name": full_name, "config": name})
        # Исходный порядок предопределённых A после B канонизируется;
        # ТЗ требует сравнения соответствующих этапов с baseline.
        row["round_trip_exact"] = row["source_a_card"] == row["returned_card"]
        row["round_trip_same_lines"] = (
            Counter(_text(row["source_a_card"]).splitlines())
            == Counter(_text(row["returned_card"]).splitlines())
        )
        observations["round_trip_exact"] &= row["round_trip_exact"]
        observations["round_trip_same_lines"] &= row["round_trip_same_lines"]
        _remove(stand, name)
        _check(name not in {item["id"] for item in _configurations(stand)},
               "Конфигурация осталась после удаления", row)
        _check(incoming.is_file(), "Удаление конфигурации удалило incoming-файл", row)
        observations["configurations"].append(row)
    return observations


def _browser_staging(stand, candidate_id: str) -> list[dict]:
    root = _inside_copy(stand, "intake-v2/uploads")
    return sorted(({
        "candidate_id": candidate_id,
        "suffix": path.relative_to(root).as_posix().replace(candidate_id, "", 1),
        "size_bytes": path.stat().st_size,
        "exists": path.is_file(),
    } for path in root.rglob("*") if path.is_file() and candidate_id in path.name),
        key=lambda row: row["suffix"])


def _s7(stand) -> dict:
    path = _small_source_b(stand)
    candidate = _upload(stand, path)["candidate"]
    committed = _start(stand, candidate)
    configurations = _configurations(stand)
    _check(candidate["internal_name"] in {row["id"] for row in configurations},
           "Browser-upload не опубликовал конфигурацию", committed)
    mcp = _mcp(stand, "list_configurations", {})
    _check(candidate["internal_name"] in _text(mcp), "MCP не видит browser-upload", mcp)
    second = _upload(stand, path)["candidate"]
    prepared = _start(stand, second, confirm=False)
    staging_before = _browser_staging(stand, second["id"])
    _check({row["suffix"] for row in staging_before} == {"payloads/.upload", "records/.json"}
           and all(row["exists"] and row["size_bytes"] > 0 for row in staging_before),
           "Перед discard отсутствуют ZIP или запись browser staging", {"staging": staging_before})
    discarded = stand.json("POST", INTAKE + "/discard", {
        "job_id": prepared["job_id"],
    }, admin=True)
    jobs = stand.json("GET", INTAKE + "/jobs", admin=True)["jobs"]
    observations = {"candidate": candidate, "commit": committed, "mcp": mcp,
                    "discarded": discarded, "staging_before": staging_before,
                    "staging_after": _browser_staging(stand, second["id"]),
                    "job_remains": any(row["job_id"] == prepared["job_id"] for row in jobs)}
    observations["staging_before_count"] = len(staging_before)
    observations["staging_after_count"] = len(observations["staging_after"])
    _check(not observations["job_remains"] and observations["staging_after"] == staging_before,
           "Discard должен удалить job и сохранить входной ZIP для повторной загрузки", observations)
    observations["candidate_deleted"] = stand.json("POST", INTAKE + "/candidates/delete", {
        "candidate_id": second["id"],
    }, admin=True)
    observations["staging_after_delete"] = _browser_staging(stand, second["id"])
    _check(not observations["staging_after_delete"],
           "Отдельное удаление candidate сохранило browser staging", observations)
    return observations


def _procedure_from_archive(path: Path) -> tuple[str, str] | None:
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            parts = entry.filename.split("/")
            if "CommonModules" not in parts or parts[-2:] != ["Ext", "Module.bsl"]:
                continue
            text = archive.read(entry).decode("utf-8-sig", errors="replace")
            match = re.search(r"(?mi)^\s*(?:Процедура|Функция)\s+([\wА-Яа-яЁё]+)\s*\(", text)
            if match:
                index = parts.index("CommonModules")
                return "ОбщийМодуль." + parts[index + 1] + "::" + match[1], match[1]
    return None


def _s8(stand) -> dict:
    inputs = _archives(stand, "zup")
    identities = [(path, _archive_identity(path)) for path in inputs]
    base = next((row for row in identities if row[1]["kind"] == "source-b"), None)
    extensions = [row for row in identities if row[1]["kind"] == "extension"]
    if base is None or len(extensions) < 2:
        raise ScenarioSkip("Нужны Source B ЗУП и два расширения", {
            "base_found": base is not None, "extensions_found": len(extensions),
        })
    staged = [(source, identity, _stage_incoming(stand, source))
              for source, identity in [base, *extensions[:2]]]
    time.sleep(5)
    started = time.perf_counter()
    first = stand.json("GET", INTAKE, admin=True)
    first_seconds = time.perf_counter() - started
    started = time.perf_counter()
    second = stand.json("GET", INTAKE, admin=True)
    second_seconds = time.perf_counter() - started
    observations = {"first_seconds": first_seconds, "second_seconds": second_seconds,
                    "jobs": [], "extension_procedures": []}
    for source, identity, incoming in staged:
        candidate = next((row for row in second["candidates"] if row["origin_name"] == incoming.name), None)
        _check(candidate is not None, "Входящий архив не обнаружен", second)
        observations["jobs"].append(_start(
            stand, candidate, parent=base[1]["name"] if identity["kind"] == "extension" else "",
        ))
        if identity["kind"] == "extension":
            procedure = _procedure_from_archive(source)
            if procedure is None:
                raise ScenarioSkip("В реальном расширении нет процедуры общего модуля для get_procedure",
                                   observations)
            result = _mcp(stand, "get_procedure", {
                "address": procedure[0], "config": base[1]["name"], "extension": identity["name"],
            })
            _check(procedure[1] in _text(result), "Не прочитана процедура расширения", result)
            observations["extension_procedures"].append(result)
    loaded = stand.json("GET", INTAKE, admin=True)
    observations["loaded_candidates"] = [row for row in loaded["candidates"]
                                         if row["origin_name"] in {entry[2].name for entry in staged}]
    uploaded_names = {entry[2].name for entry in staged}
    loaded_files = [row for row in stand.json("GET", SOURCES + "/admin", admin=True)["incoming"]
                    if row["name"] in uploaded_names]
    observations["loaded_incoming_files"] = loaded_files
    # ZIP-комментарий меняет хеш/размер, оставляя структуру настоящей выгрузки.
    incoming = staged[0][2]
    before_id = next(row["id"] for row in loaded["candidates"] if row["origin_name"] == incoming.name)
    with zipfile.ZipFile(incoming, "a") as archive:
        archive.comment = b"live-acceptance-candidate-change"
    changed = _incoming_candidate(stand, incoming)
    observations["changed_candidate"] = changed
    _check(changed["id"] != before_id, "H3: подмена incoming-файла не дала нового кандидата", observations)
    # Состояние incoming уже входит в dashboard API. Проверяется видимая
    # отметка, а не существование commit у ранее выполненной операции.
    marked = len(loaded_files) == 3 and all(
        row["state"] in {"загружено", "уже загружен", "уже загружено", "loaded"}
        for row in loaded_files
    )
    observations["loaded_marker_present"] = marked
    _check(second_seconds < 1.0 and second_seconds < max(first_seconds * 0.5, 0.25),
           "H3: повторный GET intake не стал быстрым", observations)
    _check(len(observations["loaded_candidates"]) == 3 and marked,
           "I8: входящие архивы не отмечены как загруженные", observations)
    return observations


def _synthetic(stand, name: str, version: str) -> Path:
    path = _fixture_path(stand, "live-acceptance", "synthetic")
    descriptor = (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        '<Configuration><Properties><Name>' + name + '</Name><Version>' + version + '</Version>'
        '<CompatibilityMode>Version8_3_21</CompatibilityMode></Properties>'
        '</Configuration></MetaDataObject>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _zip_member(archive, "Configuration.xml", descriptor)
    return path


def _s9(stand) -> dict:
    observations = {"same_edition": [], "empty_to_number": []}
    names = ["LiveAcceptanceEdition", "LiveAcceptanceEmptyVersion"]
    _check(not set(names) & {row["id"] for row in _configurations(stand)},
           "Синтетические имена уже заняты: нельзя перезаписывать чужие данные стенда")
    try:
        for version in ("1.0.1", "1.0.2"):
            candidate = _upload(stand, _synthetic(stand, names[0], version))["candidate"]
            observations["same_edition"].append(_start(stand, candidate))
        other = _upload(stand, _synthetic(stand, names[0], "2.0.1"))["candidate"]
        status, payload, _ = stand.request("POST", INTAKE + "/start", {
            "candidate_id": other["id"], "action": "update_full",
        }, admin=True)
        observations["other_edition"] = {"candidate": other, "status": status,
                                         "response": _response_payload(payload)}
        _check(status in (409, 422) and bool(other.get("blocked_reason")),
               "Другая редакция не получила понятный отказ", observations)
        stand.json("POST", INTAKE + "/candidates/delete", {"candidate_id": other["id"]}, admin=True)
        for version in ("", "1.0.1"):
            candidate = _upload(stand, _synthetic(stand, names[1], version))["candidate"]
            observations["empty_to_number"].append(_start(stand, candidate))
        versions = {row["id"]: row["version"] for row in _configurations(stand)}
        _check(versions.get(names[0]) == "1.0.2" and versions.get(names[1]) == "1.0.1",
               "I8: полное обновление не установило версию", {"versions": versions, **observations})
    finally:
        existing = {row["id"] for row in _configurations(stand)}
        for name in names:
            if name in existing:
                _remove(stand, name)
    return observations


def _s10(stand) -> dict:
    directory_api = SOURCES + "/directories"
    snapshot = stand.json("GET", directory_api, admin=True)
    if "config-a" not in snapshot["roots"]:
        raise ScenarioSkip("Стенду не подключён read-only /config-sources/config-a")
    # Только дескриптор разрешённого mount; окружение контейнера не читается.
    descriptor = stand.exec("python", "-c", (
        "import json,xml.etree.ElementTree as E; "
        "r=E.parse('/config-sources/config-a/Configuration.xml').getroot(); "
        "n={'m':'http://v8.1c.ru/8.3/MDClasses'}; "
        "print(json.dumps({'name':r.findtext('m:Configuration/m:Properties/m:Name',namespaces=n),"
        "'version':r.findtext('m:Configuration/m:Properties/m:Version',default='',namespaces=n)}))"
    ))
    identity = json.loads(descriptor)
    name = identity["name"]
    if name not in {row["id"] for row in _configurations(stand)}:
        # S6 проверяет удаление той же конфигурации. Для независимого S10
        # восстанавливаем минимальную структуру и штатно заменяем каталогом.
        path = _fixture_path(stand, "live-acceptance", "directory-base")
        with zipfile.ZipFile(path, "w") as archive:
            _zip_member(archive, "manifest.json", json.dumps({
                "schema_version": "1", "format": "json", "exporter_version": "live-acceptance",
                "name": name, "version": identity["version"], "platform": "8.3.21",
                "objects_total": 0, "truncated": False, "files": [],
            }, ensure_ascii=False))
        _upload(stand, path, intake=False)
        _wait_legacy_upload(stand, path.name)
    before = _incoming_marker(stand)
    original = snapshot["bindings"].get(name)
    observations = {}
    try:
        observations["bind"] = stand.json("POST", directory_api + "/bind", {
            "configuration": name, "source_id": "config-a",
        }, admin=True)
        candidate = stand.json("POST", directory_api + "/refresh", {
            "configuration": name,
        }, admin=True)["candidate"]
        observations["refresh"] = _start(stand, candidate)
        observations["unbind"] = stand.json("POST", directory_api + "/unbind", {
            "configuration": name,
        }, admin=True)
        _check(name not in observations["unbind"]["bindings"], "Каталог остался привязан", observations)
        _check(_incoming_marker(stand) == before, "Обновление из каталога изменило incoming", observations)
    finally:
        if original:
            stand.json("POST", directory_api + "/bind", {
                "configuration": name, "source_id": original,
            }, admin=True)
        else:
            stand.json("POST", directory_api + "/unbind", {"configuration": name}, admin=True)
    return observations


def _s11(stand) -> dict:
    configurations = _configurations(stand)
    selected = next((row for row in configurations
                     if any(corpus["kind"] == "extension" for corpus in row["corpora"])), None)
    if selected is None:
        raise ScenarioSkip("В стенде нет конфигурации с расширениями для каскадного удаления")
    name = selected["id"]
    before = _incoming_marker(stand)
    result = _remove(stand, name)
    after = _sources(stand)
    _check(name not in {row["id"] for row in after["configurations"]},
           "Каскадное удаление сохранило конфигурацию", after)
    registry = json.loads(_inside_copy(stand, "registry.json").read_text(encoding="utf-8"))
    remaining = [row["id"] for row in registry.get("sources", [])
                 if row["id"] == name or row["id"].startswith(name + ":")]
    _check(not remaining, "Каскадное удаление сохранило источники расширений", {"sources": remaining})
    mcp = _mcp(stand, "list_configurations", {})
    _check(name not in _text(mcp), "MCP по-прежнему видит удалённую конфигурацию", mcp)
    # Публикация B может не оставлять старый ZIP. Создаём отдельный
    # неиспользуемый файл источника только внутри копии данных.
    orphan = _fixture_path(stand, "sources", "orphan")
    shutil.copyfile(_synthetic(stand, "LiveAcceptanceOrphan", "1.0"), orphan)
    path = orphan.relative_to(stand.data_copy.resolve()).as_posix()
    orphans = stand.json("GET", SOURCES + "/admin", admin=True)["orphans"]
    _check(any(row["path"] == path for row in orphans), "Неиспользуемый файл sources не обнаружен")
    forgotten = stand.json("POST", SOURCES + "/forget", {
        "path": path, "confirmation": path,
    }, admin=True)
    _check(not _inside_copy(stand, path).exists(), "Forget сохранил файл источника", forgotten)
    _check(_incoming_marker(stand) == before, "Удаление/forget изменили incoming")
    return {"removed": result, "mcp": mcp, "forgotten": forgotten,
            "incoming_files_preserved": len(before)}


def _s12(stand) -> dict:
    observations = {"reload": stand.json("POST", "/admin/reload", {}, admin=True)}
    original = stand.json("GET", CAPABILITIES, admin=True)
    desired = set(original["active"]) ^ {"module_source_download"}
    candidate_path = _stage_incoming(stand, _small_source_b(stand))
    candidate = _incoming_candidate(stand, candidate_path)
    if not candidate["actions"]:
        raise ScenarioSkip("Для проверки рестарта загрузки нет разрешённого intake-действия", observations)
    job_id = None
    try:
        stand.json("PUT", CAPABILITIES, {"enabled": sorted(desired)}, admin=True)
        job = stand.json("POST", INTAKE + "/start", {
            "candidate_id": candidate["id"], "action": candidate["actions"][0],
        }, admin=True)["job"]
        job_id = job["job_id"]
        status, payload, _ = stand.request("POST", "/api/v1/server/restart", {}, admin=True)
        observations["restart_during_load"] = {
            "status": status, "response": _response_payload(payload), "job": job,
        }
        if status == 202:
            stand.requested_restarts += 1
            # Дожидаемся именно смены процесса: мгновенный /health ещё может
            # принадлежать старому runtime до отложенного завершения.
            time.sleep(1)
            stand.wait_ready()
        _check(status == 409, "L5: сервер разрешил рестарт во время загрузки", observations)
        prepared = _wait_job(stand, job_id)
        stand.json("POST", INTAKE + "/discard", {"job_id": prepared["job_id"]}, admin=True)
        job_id = None
        observations["idle_restart"] = _restart_api(stand)
    finally:
        stand.wait_ready()
        current = stand.json("GET", CAPABILITIES, admin=True)
        stand.json("PUT", CAPABILITIES, {"enabled": original["desired"]}, admin=True)
        if set(current["active"]) != set(original["desired"]):
            _restart_api(stand)
        if job_id:
            jobs = stand.json("GET", INTAKE + "/jobs", admin=True)["jobs"]
            job = next((row for row in jobs if row["job_id"] == job_id), None)
            if job and job.get("discardable"):
                stand.json("POST", INTAKE + "/discard", {"job_id": job_id}, admin=True)
    return observations


def _s13(stand) -> dict:
    observations = {"isolated_copy": True}
    with stand.isolation_copy() as other:
        before = _configurations(other)
        if len(before) < 2:
            candidate = _upload(other, _synthetic(other, "LiveAcceptanceHealthy", "1.0"))["candidate"]
            _start(other, candidate)
            before = _configurations(other)
        registry_path = _inside_copy(other, "registry.json")
        registry_original = registry_path.read_bytes()
        registry = json.loads(registry_original)
        pointers = [row for row in registry.get("generation_manifests", [])
                    if row["identity"]["source_kind"] == "configuration"]
        if not pointers or len(before) < 2:
            raise ScenarioSkip("Для S13 нужны два активных поколения конфигураций", observations)
        pointer = pointers[0]
        missing = pointer["identity"]["configuration_name"]
        root = _inside_copy(other, pointer["root_path"])
        other._fixture_scenario = "s13"
        backup = _fixture_path(other, "live-acceptance", "hidden-generation", "")
        _check(root.is_dir() and not root.is_symlink(), "Корень поколения отсутствует до проверки")
        root.rename(backup)
        try:
            try:
                other.restart()
            except (TimeoutError, RuntimeError, AssertionError, OSError) as error:
                observations["startup_error"] = type(error).__name__ + ": " + str(error)
                observations["logs"] = other.logs()
                _check(False, "H2: утраченное поколение не дало запустить остальные конфигурации", observations)
            surviving = _configurations(other)
            available = {row["id"] for row in surviving}
            expected = {row["id"] for row in before} - {missing}
            observations.update(missing_configuration=missing, surviving_configurations=sorted(available),
                                expected_configurations=sorted(expected), logs=other.logs())
            _check(expected <= available and missing not in available,
                   "H2: восстановлены не все исправные конфигурации", observations)
            logs = observations["logs"]
            _check(missing in logs or pointer["generation_id"] in logs,
                   "Нет диагностики пропущенного поколения", observations)
            healthy = next(row["id"] for row in surviving)
            other.json("POST", SOURCES + "/platform/set", {
                "configuration": healthy, "platform_version": "8.3.21",
            }, admin=True)
            after_save = json.loads(registry_path.read_text(encoding="utf-8"))
            _check(pointer in after_save.get("generation_manifests", []),
                   "H2: save потерял pointer повреждённого поколения", observations)
        finally:
            # Даже упавший baseline не должен оставить изоляционную копию
            # повреждённой; рабочий стенд не получает этот реестр.
            if backup.exists():
                _check(not root.exists(), "Путь поколения занят при восстановлении")
                backup.rename(root)
            registry_path.write_bytes(registry_original)
            other.restart()
        restored = {row["id"] for row in _configurations(other)}
        observations["restored_configurations"] = sorted(restored)
        _check({row["id"] for row in before} == restored,
               "Возвращённое поколение не восстановилось после рестарта", observations)
    return observations


def _s14(stand) -> dict:
    configurations = _configurations(stand)
    if not configurations:
        raise ScenarioSkip("Нет конфигурации для проверки словаря и поиска")
    config = configurations[0]["id"]
    queries = stand.json("POST", "/api/v1/queries", {
        "config": config, "scope": "objects", "phrases": ["Номенклатура", "Пользователи", "Документ"],
    })
    hits = [hit for row in queries["results"] for hit in row["hits"]]
    if not hits:
        raise ScenarioSkip("В выбранной конфигурации нет объекта для псевдонима")
    target = hits[0]["id"]
    phrase = "лабораторный псевдоним " + _fixture_name(stand, "alias")
    words = ["лабораторныйсиноним" + _fixture_name(stand, "synonym"),
             "лабораторныйэквивалент" + _fixture_name(stand, "synonym")]
    observations = {"config": config, "target": target}
    alias_added = synonyms_added = False
    try:
        observations["alias"] = stand.json("POST", "/api/v1/dictionary/aliases", {
            "phrase": phrase, "targets": [target], "config": config,
        }, admin=True)
        alias_added = True
        observations["synonyms"] = stand.json("POST", "/api/v1/dictionary/synonyms", {
            "words": words,
        }, admin=True)
        synonyms_added = True
        observations["search"] = _mcp(stand, "search_objects", {"query": phrase, "config": config})
        _check(target in _text(observations["search"]), "Псевдоним не изменил MCP-поиск", observations)
    finally:
        if synonyms_added:
            stand.json("POST", "/api/v1/dictionary/synonyms/remove", {"words": words}, admin=True)
        if alias_added:
            stand.json("POST", "/api/v1/dictionary/aliases/remove", {
                "phrase": observations["alias"]["changed"]["phrase"], "scope": config,
            }, admin=True)
    restored = stand.json("GET", "/api/v1/dictionary?config=" + quote(config),
                          admin=True)
    _check(not any(row["phrase"] == observations["alias"]["changed"]["phrase"]
                   for row in restored["aliases"]), "Псевдоним не удалён")
    _check(not any(set(group) == set(words) for group in restored["synonym_groups"]), "Синонимы не удалены")
    observations["removed"] = True
    return observations


def _s15(stand) -> dict:
    observations = {"checks": []}
    failures = []

    def check(label, method, path, *, expected, body=None, **kwargs):
        started = time.perf_counter()
        try:
            status, payload, _ = stand.request(method, path, body, **kwargs)
            row = {"check": label, "status": status, "seconds": time.perf_counter() - started,
                   "response_bytes": len(payload)}
            if status not in expected:
                failures.append(label + ": HTTP " + str(status))
        except (TimeoutError, OSError) as error:
            row = {"check": label, "seconds": time.perf_counter() - started,
                   "error": type(error).__name__}
            failures.append(label + ": " + type(error).__name__)
        observations["checks"].append(row)
        return row

    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "live-acceptance-security", "version": "1"},
    }}
    mcp_headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    check("mcp_without_token", "POST", "/mcp", expected={401}, body=initialize,
          authenticated=False, headers=mcp_headers)
    check("mcp_read_token", "POST", "/mcp", expected={200}, body=initialize, headers=mcp_headers)
    for method, path, body in (
        ("POST", "/admin/reload", {}),
        ("PUT", CAPABILITIES, {"enabled": []}),
        ("POST", "/api/v1/server/restart", {}),
        ("POST", SOURCES + "/remove", {"id": "forbidden", "confirmation": "forbidden"}),
        ("POST", "/api/v1/dictionary/aliases", {"phrase": "forbidden", "targets": []}),
    ):
        check("read_token_admin_" + path, method, path, expected={403, 404}, body=body)
    adversarial_body = {**initialize, "params": {**initialize["params"], "_meta": {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": "a=b" + " " * 60000 + "x",
    }}}
    adversarial = check("H1_tracestate_60000_spaces", "POST", "/mcp", expected={200},
                        body=adversarial_body, headers=mcp_headers)
    if adversarial["seconds"] >= 0.5:
        failures.append("H1: обработка tracestate заняла не меньше 0,5 с")
    check("oversized_body", "POST", "/mcp", expected={413},
          body=b" " * (2 * 1024 * 1024 + 1), headers=mcp_headers)
    for host, expected in (("localhost", {200}), ("127.0.0.1", {200}),
                           ("host.docker.internal", {200}), ("foreign.invalid", {421})):
        # /health исключён из части middleware; проверять Host нужно на API.
        check("Host_" + host, "GET", SOURCES, expected=expected, headers={"Host": host})
    observations["failures"] = failures
    _check(not failures, "Проверки защиты не прошли: " + "; ".join(failures), observations)
    return observations


def run_ui_proof(base_url, admin_token, evidence_dir, *, chrome=None) -> dict:
    """Браузерная проверка S16; Playwright загружается только по флагу --ui."""
    import importlib.util

    path = Path(__file__).with_name("live_stand_ui.py")
    spec = importlib.util.spec_from_file_location("live_stand_ui", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_ui_proof(base_url, admin_token, evidence_dir, chrome=chrome)


def _s16(stand) -> dict:
    options = getattr(stand, "ui", None)
    if not options:
        # Пропуск не считается успехом: итог прогона — PASS_WITHOUT_UI.
        raise ScenarioSkip(
            "UI-проверка выключена: запустите приёмку с --ui и --evidence-dir"
        )
    result = run_ui_proof(
        f"http://127.0.0.1:{stand.port}", stand.admin_token,
        options["evidence_dir"], chrome=options.get("chrome"),
    )
    stand._secret_values.update(result.pop("secret_values", ()))
    _check(result.get("status") == "PASS",
           "UI-проверка через навигацию не прошла", result)
    return result


def _s17(stand) -> dict:
    logs = stand.logs()
    warnings = [line for line in logs.splitlines() if re.search(r"\bWARN(?:ING)?\b", line, re.I)]
    expected = stand.report.get("expected_log_warnings", [])
    unexpected = [line for line in warnings if not any(re.search(pattern, line) for pattern in expected)]
    observations = {"traceback_count": logs.count("Traceback"), "warnings": warnings,
                    "unexpected_warnings": unexpected,
                    "log_sha256": hashlib.sha256(logs.encode("utf-8")).hexdigest()}
    _check(observations["traceback_count"] == 0, "В журнале стенда есть Traceback", observations)
    _check(not unexpected, "В журнале стенда есть неоговорённые предупреждения", observations)
    return observations


def _s18(stand) -> dict:
    stand.sample_resources()
    inspected = stand.inspect()
    resources = stand.report.get("resources", {})
    observations = {"restart_count": inspected["RestartCount"],
                    "requested_policy_restarts": stand.requested_restarts,
                    "oom_killed": inspected["State"]["OOMKilled"],
                    "peak_rss_bytes": resources.get("peak_rss_bytes"),
                    "baseline_peak_rss_bytes": resources.get("baseline_peak_rss_bytes"),
                    "restart_metric": "Docker RestartCount: автоматические policy-рестарты; docker restart не входит"}
    _check(not observations["oom_killed"], "Контейнер стенда был убит по OOM", observations)
    _check(observations["restart_count"] == stand.requested_restarts,
           "Число policy-рестартов не совпало с запросами сценариев", observations)
    peak = observations["peak_rss_bytes"]
    _check(isinstance(peak, (int, float)) and peak > 0, "Пик RSS стенда не измерен", observations)
    baseline = observations["baseline_peak_rss_bytes"]
    if baseline is None:
        if stand.report.get("run_kind") == "baseline":
            observations["comparison"] = "not_applicable_baseline"
            return observations
        observations["rss_comparison"] = "unmeasured"
        raise ScenarioSkip("Baseline: RSS измерен, сравнение +10% требует второго прогона", observations)
    _check(isinstance(baseline, (int, float)) and baseline > 0, "Некорректный baseline RSS", observations)
    observations["rss_ratio"] = peak / baseline
    _check(peak <= baseline * 1.1, "Пик RSS вырос больше чем на 10%", observations)
    return observations


_SCENARIOS = {"S" + str(number): globals()["_s" + str(number)] for number in range(3, 19)}


def run_scenario(scenario: str, stand) -> dict:
    """Выполнить один сценарий без создания или удаления основного стенда."""
    if scenario not in _SCENARIOS:
        raise ValueError("Неизвестный сценарий: " + scenario)
    stand._fixture_scenario = scenario.lower()
    return _SCENARIOS[scenario](stand)
