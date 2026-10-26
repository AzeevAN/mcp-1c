"""S16 живой приёмки: вход и навигация дашборда в настоящем браузере.

Модуль работает с уже поднятым стендом: стенд, копию данных и рабочий
контейнер ведёт `accept_live_stand.py`. Нужен Playwright (Python-пакет
`playwright`) и браузер Chromium; зависимости и браузеры модуль не
устанавливает. Путь к своему Chrome — аргумент `chrome`, иначе используется
Chromium из поставки Playwright. Общий дедлайн задаёт вызывающий runner,
здесь действуют только таймауты Playwright.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit


NAVIGATION = (
    ("overview", "/", "Обзор", "Центр конфигураций", ".metrics-grid .metric-card"),
    ("sources", "/sources", "Источники", "Источники", ".sources-master"),
    ("queries", "/queries", "Запросы", "Проверка поисковых формулировок", ".query-controls"),
    ("reference", "/reference", "Общая справка", "Общая справка", ".reference-status"),
    ("graph", "/graph", "Связи", "Связи объектов", ".graph-controls"),
    ("roles", "/roles", "Роли и права", "Роли и права", ".roles-context-card"),
    ("dictionary", "/dictionary", "Словарь", "Словарь поиска", ".dictionary-summary"),
    ("capabilities", "/capabilities", "Дополнительные модули", "Дополнительные модули",
     ".capabilities-status-grid"),
)
REQUIRED_STEPS = ("login", *(item[0] for item in NAVIGATION),
                  "object_detail", "syntax", "reference_detail")
CONSOLE_CRITERION = (
    "Ожидается только сетевое сообщение HTTP 409 для GET /api/v1/roles "
    "без config, подтверждённое ответом state=selection_required. "
    "Остальные ошибки console и все исключения JavaScript запрещены. "
    "Сырые сообщения сохраняются отдельно от классификации."
)


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def classify_console(report: dict) -> bool:
    """Отделить ожидаемый 409 выбора конфигурации от остальных ошибок console."""
    eligible: dict[str, int] = {}
    for response in report["role_responses"]:
        parsed = urlsplit(response["url"])
        if (response["method"] == "GET" and parsed.path == "/api/v1/roles"
                and "config" not in parse_qs(parsed.query, keep_blank_values=True)
                and response["status"] == 409
                and response.get("state") == "selection_required"):
            eligible[response["url"]] = eligible.get(response["url"], 0) + 1
    classifications, unexpected = [], []
    expected = 0
    for index, entry in enumerate(report["console"]):
        if entry["type"] != "error":
            continue
        url = entry.get("location", {}).get("url")
        network_409 = re.fullmatch(
            r"Failed to load resource: the server responded with a status of 409(?: \([^\n)]*\))?",
            entry["text"],
        )
        if network_409 and eligible.get(url, 0):
            eligible[url] -= 1
            expected += 1
            classification = "expected_selection_required"
        else:
            classification = "unexpected_error"
            unexpected.append({"console_index": index, **entry})
        classifications.append({"console_index": index, "classification": classification})
    report["console_raw"] = list(report["console"])
    report["console_classification"] = classifications
    report["unexpected_console_errors"] = unexpected
    report["console_summary"] = {
        "raw_errors": len(classifications), "expected_selection_required": expected,
        "unexpected_errors": len(unexpected), "pageerrors": len(report["pageerrors"]),
    }
    return not unexpected and not report["pageerrors"]


def run_ui_proof(
    base_url: str, admin_token: str, evidence_dir: Path, *, chrome: str | None = None,
) -> dict:
    """Пройти вход и все страницы через навигацию; вернуть отчёт S16.

    Значения сессионных cookie возвращаются в ``secret_values``: вызывающий
    обязан вычистить их из сохраняемого отчёта.
    """
    evidence = Path(evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "status": "FAIL",
        "steps": {name: {"status": "NOT_CHECKED"} for name in REQUIRED_STEPS},
        "errors": [], "auth_console": [], "console": [],
        "auth_pageerrors": [], "pageerrors": [],
        "role_responses": [], "queries_setup": [], "query_diagnostics": {},
        "console_criterion": CONSOLE_CRITERION,
        "secret_values": [],
    }
    secret_values: set[str] = set()
    phase = {"authenticated": False}
    browser = context = playwright = None

    def error(stage, exception):
        report["errors"].append({"stage": stage, "type": type(exception).__name__,
                                 "message": str(exception)})

    def step(name, action):
        started = time.monotonic()
        try:
            details = action() or {}
            report["steps"][name] = {"status": "PASS", **details}
            return True
        except Exception as exception:
            error(name, exception)
            report["steps"][name] = {"status": "FAIL", "error": str(exception)}
            return False
        finally:
            report["steps"][name]["duration_seconds"] = round(time.monotonic() - started, 3)

    try:
        # Унаследованный отладочный режим Playwright может печатать fill(token).
        os.environ.pop("DEBUG", None)
        os.environ.pop("PWDEBUG", None)
        from importlib.metadata import version
        from playwright.sync_api import expect, sync_playwright

        report["playwright_version"] = version("playwright")
        playwright = sync_playwright().start()
        launch = {"headless": True}
        if chrome:
            launch["executable_path"] = chrome
        browser = playwright.chromium.launch(**launch)
        report["browser"] = {"version": browser.version, "headless": True,
                             "custom_executable": bool(chrome),
                             "profile": "временный профиль Playwright"}
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      device_scale_factor=1)
        page = context.new_page()
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(60000)

        def console_message(message):
            key = "console" if phase["authenticated"] else "auth_console"
            report[key].append({"type": message.type, "text": message.text,
                                "location": message.location})

        def page_error(exception):
            key = "pageerrors" if phase["authenticated"] else "auth_pageerrors"
            report[key].append(str(exception))

        def api_response(response):
            parsed = urlsplit(response.url)
            if parsed.netloc != urlsplit(base_url).netloc or response.request.method != "GET":
                return
            if parsed.path not in ("/api/v1/roles", "/api/v1/queries"):
                return
            details = {"url": response.url, "method": response.request.method,
                       "status": response.status}
            destination = "role_responses" if parsed.path == "/api/v1/roles" else "queries_setup"
            try:
                payload = response.json()
                if destination == "role_responses":
                    details.update({"state": payload.get("state"),
                                    "configuration": payload.get("configuration"),
                                    "roles_total": payload.get("roles_total"),
                                    "roles_returned": len(payload.get("roles") or [])})
                else:
                    details["contract"] = {
                        key: payload.get(key) for key in (
                            "availability", "scopes", "default_configuration", "configuration_names")
                    }
            except Exception as exception:
                details["inspection_error"] = str(exception)
            report[destination].append(details)

        page.on("console", console_message)
        page.on("pageerror", page_error)
        page.on("response", api_response)

        def main_content():
            content = page.locator("main.content")
            expect(content).to_be_visible()
            return content

        def settled(content):
            expect(content.locator(".loading-dot:visible")).to_have_count(0)
            expect(content.locator(".sources-message.is-error:visible, "
                                   ".card-empty-state.is-error:visible, "
                                   ".capabilities-error:visible")).to_have_count(0)

        def snapshot(name, heading=None, marker=None):
            content = main_content()
            if heading:
                expect(content.get_by_role("heading", name=heading, exact=True).first).to_be_visible()
            else:
                expect(content.locator("h1").first).to_be_visible()
            if marker:
                expect(content.locator(marker).first).to_be_visible()
            settled(content)
            title = content.locator("h1").first.inner_text().strip()
            _require(bool(title), "Пустой заголовок страницы.")
            screenshot = evidence / f"{name}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            return {"url": page.url, "heading": title, "screenshot": screenshot.name,
                    "alerts": content.locator("[role=alert]:visible").all_inner_texts()}

        def navigate(label, expected_path):
            nav = page.get_by_role("navigation", name="Основная навигация", exact=True)
            link = nav.get_by_role("link", name=label, exact=True)
            expect(link).to_be_visible()
            link.click()
            page.wait_for_url(lambda url: urlsplit(str(url)).path == expected_path)

        def login():
            # Единственный прямой переход: все остальные адреса открываются ссылками UI.
            page.goto(base_url + "/login", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="Вход в дашборд", exact=True)).to_be_visible()
            token = page.get_by_label("Токен доступа", exact=True)
            expect(token).to_have_attribute("type", "password")
            token.fill(admin_token)
            page.get_by_role("button", name="Войти", exact=True).click()
            expect(page.get_by_role("navigation", name="Основная навигация", exact=True)).to_be_visible()
            cookies = context.cookies(base_url)
            secret_values.update(cookie["value"] for cookie in cookies)
            _require(any(cookie["httpOnly"] for cookie in cookies),
                     "После реального входа нет HttpOnly сессионной cookie.")
            _require(not token.is_visible(), "После входа осталась форма ввода токена.")
            phase["authenticated"] = True
            return {"session_cookie_received": True,
                    "cookie_names": [cookie["name"] for cookie in cookies]}

        def navigation_page(name, path, label, heading, marker):
            initial_roles = None
            if name == "roles":
                with page.expect_response(lambda response:
                                          response.request.method == "GET"
                                          and urlsplit(response.url).path == "/api/v1/roles"
                                          and "config" not in parse_qs(urlsplit(response.url).query,
                                                                      keep_blank_values=True)) as initial:
                    navigate(label, path)
                initial_roles = initial.value
            else:
                navigate(label, path)
            details = {}
            if name == "roles":
                selection = main_content().locator(".roles-context-card").get_by_label(
                    "Конфигурация", exact=True)
                expect(selection).to_be_visible()
                _require(initial_roles.status == 409
                         and initial_roles.json().get("state") == "selection_required",
                         "Первый GET ролей без config не подтверждён как HTTP 409 selection_required.")
                first = selection.locator('option:not([value=""])').first
                expect(first).to_be_attached()
                selected = first.get_attribute("value")
                _require(bool(selected), "Нет конфигурации для проверки готового снимка ролей.")
                with page.expect_response(lambda response:
                                          response.request.method == "GET"
                                          and urlsplit(response.url).path == "/api/v1/roles"
                                          and parse_qs(urlsplit(response.url).query).get("config") == [selected]) as loaded:
                    selection.select_option(value=selected)
                payload = loaded.value.json()
                _require(loaded.value.status == 200 and payload.get("state") == "ready",
                         "После выбора конфигурации роли не перешли в ready.")
                _require(payload.get("configuration") == selected and bool(payload.get("roles")),
                         "Готовый ответ ролей не содержит данных выбранной конфигурации.")
                expect(main_content().locator(".roles-tabs")).to_be_visible()
                expect(main_content().locator(".roles-unavailable:visible")).to_have_count(0)
                details = {"initial_state": "selection_required", "initial_status": 409,
                           "selected_configuration": selected, "roles_state": payload["state"],
                           "roles_returned": len(payload["roles"])}
            return {**snapshot(name, heading, marker), **details}

        def queries_canonicalized():
            # Только читаем DOM и адрес: React сам завершает свою синхронизацию маршрута.
            page.wait_for_function("""() => {
                const params = new URLSearchParams(location.search);
                const radio = document.querySelector('input[name="query-scope"]:checked');
                const config = document.querySelector('select[aria-label="Конфигурация"]');
                return location.pathname === '/queries' && radio && config
                    && params.get('scope') === radio.value
                    && (config.value ? params.get('config') === config.value : !params.has('config'));
            }""")

        def query_card(name, scope, phrase, prefix):
            diagnostic = {"requested_scope": scope, "url_before_navigation": page.url}
            report["query_diagnostics"][name] = diagnostic
            try:
                navigate("Запросы", "/queries")
                content = main_content()
                expect(content.locator(".query-controls")).to_be_visible()
                queries_canonicalized()
                configuration = content.get_by_label("Конфигурация", exact=True)
                if configuration.is_enabled():
                    first = configuration.locator("option").first
                    expect(first).to_be_attached()
                    value = first.get_attribute("value")
                    selected = value if value is not None else first.inner_text()
                    configuration.select_option(value=selected)
                    expect(configuration).to_have_value(selected)
                    page.wait_for_url(lambda url: parse_qs(urlsplit(str(url)).query).get("config") == [selected])
                queries_canonicalized()
                radio = content.locator(f'input[type="radio"][value="{scope}"]')
                expect(radio).to_be_enabled()
                radio.click()
                expect(radio).to_be_checked()
                page.wait_for_url(lambda url: parse_qs(urlsplit(str(url)).query).get("scope") == [scope])
                queries_canonicalized()
                content.get_by_label("Поисковые фразы", exact=True).fill(phrase)
                button = content.get_by_role("button", name="Прогнать запросы", exact=True)
                expect(button).to_be_enabled()
                # Ответ связывает проверку с новым поиском, а не с историей sessionStorage.
                with page.expect_response(lambda response:
                                          urlsplit(response.url).path == "/api/v1/queries"
                                          and response.request.method == "POST") as searched:
                    button.click()
                _require(searched.value.ok, "Поиск карточки завершился HTTP-ошибкой.")
                expect(button).to_be_enabled()
                link = content.locator(f'a[href^="{prefix}"]').first
                expect(link).to_be_visible()
                href, title = link.get_attribute("href"), link.inner_text().strip()
                _require(bool(href) and bool(title), "У результата нет адреса или заголовка.")
                link.click()
                page.wait_for_url(lambda url: urlsplit(str(url)).path == prefix.split("?")[0])
                details = snapshot(name, marker=".card-document .card-markdown, .card-document .card-raw-text")
                document = main_content().locator(".card-markdown, .card-raw-text").first
                _require(bool(document.inner_text().strip()), "Карточка не содержит данных.")
                return {**details, "clicked_href": href, "clicked_title": title, "phrase": phrase}
            finally:
                diagnostic["url_after"] = page.url

        def reference_card():
            navigate("Общая справка", "/reference")
            content = main_content()
            search = content.get_by_label("Что найти", exact=True)
            expect(search).to_be_visible()
            domain = content.get_by_label("Раздел", exact=True)
            if domain.locator('option[value="query"]').count():
                domain.select_option("query")
            search.fill("УНИКАЛЬНО")
            with page.expect_response(lambda response:
                                      "/api/v1/reference/search" in urlsplit(response.url).path) as searched:
                content.get_by_role("button", name="Найти", exact=True).click()
            _require(searched.value.ok, "Поиск общей справки завершился HTTP-ошибкой.")
            link = content.locator('a[href^="/reference/item?"]').first
            expect(link).to_be_visible()
            href, title = link.get_attribute("href"), link.inner_text().strip()
            _require(bool(href) and bool(title), "У результата справки нет адреса или заголовка.")
            link.click()
            page.wait_for_url(lambda url: urlsplit(str(url)).path == "/reference/item")
            details = snapshot("reference_detail", marker=".reference-item-page .card-document")
            document = main_content().locator(".card-markdown, .card-raw-text").first
            expect(document).to_be_visible()
            _require(bool(document.inner_text().strip()), "Карточка общей справки пуста.")
            return {**details, "clicked_href": href, "clicked_title": title, "phrase": "УНИКАЛЬНО"}

        if step("login", login):
            for item in NAVIGATION:
                step(item[0], lambda item=item: navigation_page(*item))
            step("object_detail", lambda: query_card("object_detail", "objects", "документ", "/object?"))
            step("syntax", lambda: query_card("syntax", "syntax", "массив", "/syntax?"))
            step("reference_detail", reference_card)
    except Exception as exception:
        error("setup", exception)
    finally:
        for stage, resource in (("browser_cleanup", browser), ("playwright_cleanup", playwright)):
            if resource is None:
                continue
            try:
                resource.close() if resource is browser else resource.stop()
            except Exception as exception:
                error(stage, exception)
    console_clean = classify_console(report)
    report["post_login_unexpected_errors_absent"] = (
        console_clean if phase["authenticated"] else None
    )
    complete = all(report["steps"][name]["status"] == "PASS" for name in REQUIRED_STEPS)
    if complete and console_clean and not report["errors"]:
        report["status"] = "PASS"
    report["secret_values"] = sorted(secret_values)
    return report
