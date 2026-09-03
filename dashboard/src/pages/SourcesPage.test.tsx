import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { SourcesPage } from "./SourcesPage";

const coverage = {
  has_limitations: false,
  modules: {
    total: 12,
    source_available: 12,
    empty: 0,
    partial: 0,
    unreadable: 0,
    conflict: 0,
    compiled_without_source: 0,
  },
  procedures: { total: 48, full: 48, partial: 0 },
  form_structures: { total: 5, full: 4, partial: 1, unreadable: 0 },
  form_modules: { total: 5, read: 3, empty: 1, missing: 1, unreadable: 0 },
  problems_total: 0,
  problem_categories: [],
};

const corpus = (id: string, label: string, kind: "modules" | "extension") => ({
  id,
  label,
  kind,
  phase: "ready" as const,
  state: "готов — модулей 12, процедур 48, форм 5",
  source: {
    id,
    kind,
    platform: "",
    items_total: 20,
    status: "ready",
    loaded_at: "2026-08-28T10:00:00+00:00",
    code_version: "1.0",
    incomplete: false,
    warnings: [],
  },
  coverage,
  journal: "logs/code-test.json",
  journal_url: `/api/v1/sources/coverage?source_id=${id}`,
  error: "",
});

const configurationSource = (id: string) => ({
  id,
  kind: "configuration" as const,
  platform: "8.3.23.1997",
  items_total: 120,
  status: "ready",
  loaded_at: "2026-08-28T09:00:00+00:00",
  code_version: "",
  incomplete: false,
  warnings: [],
});

let referenceAdminState: Record<string, unknown>;
let runtimeAdminState: { self_restart: boolean };

function intakeSnapshot(withCandidate = false) {
  const candidates = withCandidate
    ? [{
        id: "candidate-incoming",
        transport: "incoming",
        source_kind: "configuration",
        internal_name: "Отраслевая конфигурация А",
        configuration_version: "2.0",
        layout: "tree",
        origin_name: "полная-выгрузка.zip",
        raw_sha256: "a".repeat(64),
        requires_parent: false,
        actions: ["update", "update_full"],
      }]
    : [];
  return {
    api_version: "v1",
    configuration_names: ["Отраслевая конфигурация А", "Отраслевая конфигурация Б"],
    candidates,
    groups: withCandidate
      ? [{
          source_kind: "configuration",
          internal_name: "Отраслевая конфигурация А",
          candidate_ids: ["candidate-incoming"],
        }]
      : [],
    issues: [],
    jobs: [],
  };
}

beforeEach(() => {
  referenceAdminState = {
    api_version: "v1",
    active: {
      state: "missing",
      ready: false,
      message: "Каноническая база не загружена.",
      signature: "not-checked",
      items: null,
      index_cache: null,
    },
    pending: null,
    managed_upload: true,
    managed_file_present: false,
    limits: { upload_bytes: 32 * 1024 * 1024 },
  };
  runtimeAdminState = { self_restart: true };
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/sources/admin") {
        return {
          ok: true,
          json: async () => ({
            api_version: "v1",
            limits: { upload_bytes: 500 * 1024 * 1024 },
            configuration_names: ["Отраслевая конфигурация А", "Отраслевая конфигурация Б"],
            jobs: [],
            incoming: [
              {
                name: "полная-выгрузка.zip",
                size: 1_800_000_000,
                state: "не разобрано",
                detail: "",
                settling: false,
                can_parse: true,
                action: "parse",
              },
            ],
            incoming_exists: true,
            incoming_dir: "data/incoming/",
            orphans: [],
            snapshot_error: "",
            reference: referenceAdminState,
            runtime: runtimeAdminState,
          }),
        };
      }
      if (path === "/api/v1/sources/intake") {
        return { ok: true, json: async () => intakeSnapshot(true) };
      }
      return {
        ok: true,
        json: async () => ({
        api_version: "v1",
        permissions: { read: true, admin: true },
        configurations: [
          {
            id: "Отраслевая конфигурация А",
            version: "1.0",
            platform: "8.3.23.1997",
            objects: 120,
            edges: 640,
            loaded_at: "2026-08-28T09:00:00+00:00",
            notes: [],
            source: configurationSource("Отраслевая конфигурация А"),
            corpora: [corpus("a:modules", "Основная конфигурация", "modules")],
          },
          {
            id: "Отраслевая конфигурация Б",
            version: "2.4",
            platform: "8.3.27.1000",
            objects: 340,
            edges: 980,
            loaded_at: "2026-08-28T10:00:00+00:00",
            notes: ["Проверить соответствие версии справки."],
            source: configurationSource("Отраслевая конфигурация Б"),
            corpora: [
              corpus("b:modules", "Основная конфигурация", "modules"),
              corpus("b:ext:Доп", "Расширение Доп", "extension"),
            ],
          },
        ],
        references: [],
      }),
      };
    }),
  );
});

it("переключает конфигурацию без ухода со страницы и показывает связанные корпуса", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Отраслевая конфигурация А" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Отраслевая конфигурация Б/ }));

  expect(screen.getByRole("heading", { name: "Отраслевая конфигурация Б" })).toBeInTheDocument();
  const composition = screen.getByLabelText("Состав конфигурации");
  expect(within(composition).getByText("Снимок активности расширений")).toBeInTheDocument();
  expect(within(composition).getByText("не загружен")).toBeInTheDocument();
  expect(screen.getByText("Расширение Доп")).toBeInTheDocument();
  expect(screen.getAllByText("Структуры форм")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Показать подробности Расширение Доп" }));
  expect(screen.getAllByText("Структуры форм")).toHaveLength(2);
  expect(screen.getAllByRole("link", { name: "Открыть JSON-журнал" })).toHaveLength(2);
});

it("позволяет снять native-расширение без отдельной legacy source-строки", async () => {
  const fetchMock = vi.mocked(fetch);
  const regularFetch = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const response = await regularFetch(input, init);
    if (String(input) !== "/api/v1/sources") return response;
    const payload = await response.json();
    payload.configurations[1].corpora[1] = {
      ...payload.configurations[1].corpora[1],
      phase: "missing",
      state: "не загружен",
      source: null,
      coverage: null,
      journal: "",
      journal_url: "",
    };
    return { ...response, json: async () => payload };
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  fireEvent.click(await screen.findByRole("button", { name: /Отраслевая конфигурация Б/ }));
  fireEvent.click(screen.getByRole("button", { name: "Удалить Расширение Доп" }));

  const dialog = screen.getByRole("dialog", { name: "Удалить «Расширение Доп»?" });
  expect(within(dialog).getByText(/все опубликованные слои этого расширения/)).toBeInTheDocument();
  expect(within(dialog).getByText("b:ext:Доп")).toBeInTheDocument();
});

it("не изображает состав конфигурации декоративными развилками", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Отраслевая конфигурация А" })).toBeInTheDocument();
  expect(container.querySelectorAll(".lucide-git-fork")).toHaveLength(0);
  expect(within(screen.getByLabelText("Состав конфигурации")).queryByText("Активность")).not.toBeInTheDocument();
});

it("показывает загруженный снимок активности расширений понятным статусом", async () => {
  const fetchMock = vi.mocked(fetch);
  const regularFetch = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const response = await regularFetch(input, init);
    if (String(input) !== "/api/v1/sources") return response;
    const payload = await response.json();
    payload.configurations[0].extension_runtime = {
      id: "a:extension-runtime",
      kind: "extension-runtime",
      platform: "8.3.23.1997",
      items_total: 2,
      status: "ready",
      loaded_at: "2026-08-29T09:00:00+00:00",
      code_version: "",
      incomplete: false,
      warnings: [],
    };
    return { ...response, json: async () => payload };
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Отраслевая конфигурация А" })).toBeInTheDocument();
  const composition = screen.getByLabelText("Состав конфигурации");
  expect(within(composition).getByText("Снимок активности расширений")).toBeInTheDocument();
  expect(within(composition).getByText("загружен")).toBeInTheDocument();
});

it("показывает администратору техническую причину ошибки корпуса", async () => {
  const fetchMock = vi.mocked(fetch);
  const regularFetch = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const response = await regularFetch(input, init);
    if (String(input) !== "/api/v1/sources") return response;
    const payload = await response.json();
    payload.configurations[0].corpora[0] = {
      ...payload.configurations[0].corpora[0],
      phase: "error",
      state: "ошибка — подробности ошибки доступны в журнале сервера",
      coverage: null,
      journal: "",
      journal_url: "",
      error: "индекс кода не построился — IndexError: string index out of range",
    };
    return { ...response, json: async () => payload };
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText(/IndexError: string index out of range/)).toBeInTheDocument();
  expect(screen.queryByText(/private-export\.zip/)).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Открыть JSON-журнал" })).not.toBeInTheDocument();
});

it("повторяет чтение снимка после временного 409", async () => {
  const fetchMock = vi.mocked(fetch);
  const regularFetch = fetchMock.getMockImplementation()!;
  let conflictReturned = false;
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/v1/sources" && !conflictReturned) {
      conflictReturned = true;
      return {
        ok: false,
        status: 409,
        json: async () => ({ error: "Источники изменились; повторите запрос." }),
      } as Response;
    }
    return regularFetch(input, init);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Отраслевая конфигурация А" })).toBeInTheDocument();
  expect(
    fetchMock.mock.calls.filter(([input]) => String(input) === "/api/v1/sources"),
  ).toHaveLength(2);
});

it("показывает администратору два явных пути загрузки и подтверждение удаления", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Добавление и обслуживание данных" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Локальная общая справка" })).toBeInTheDocument();
  expect(screen.getByText("Каноническая база не загружена.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Загрузить справочную базу" })).not.toBeInTheDocument();
  expect(screen.getByText(/загрузка через общую форму выше/)).toBeInTheDocument();
  expect(await screen.findByRole("region", { name: "Полная файловая выгрузка" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Обновить код, формы и роли" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Обновить полностью" })).toBeEnabled();
  const truncated = screen.getByRole("checkbox", { name: /Разрешить неполную тестовую выгрузку/ });
  fireEvent.click(truncated);
  expect(truncated).toBeChecked();

  const input = container.querySelector<HTMLInputElement>(
    'input[accept=".zip,.hbk,.json,.mcp1cref"]',
  );
  const file = new File(["synthetic"], "структура.zip", { type: "application/zip" });
  fireEvent.change(input!, { target: { files: [file] } });
  expect(screen.getByText("структура.zip")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Загрузить и разобрать" })).toBeEnabled();

  const reference = new File(["synthetic"], "reference.mcp1cref", { type: "application/octet-stream" });
  fireEvent.change(input!, { target: { files: [reference] } });
  expect(screen.getByText("reference.mcp1cref")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Проверить и сохранить" })).toBeEnabled();
  expect(screen.queryByRole("checkbox", { name: /Разрешить неполную тестовую выгрузку/ })).not.toBeInTheDocument();
  expect(input).toHaveAttribute("accept", ".zip,.hbk,.json,.mcp1cref");

  fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
  expect(screen.getByRole("dialog", { name: "Удалить «Отраслевая конфигурация А»?" })).toBeInTheDocument();
  expect(screen.getByText(/каскадно удалены структура конфигурации/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Удалить без возможности отмены" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Скопировать точное имя" }));
  await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("Отраслевая конфигурация А"));
  expect(screen.getByRole("button", { name: "Точное имя скопировано" })).toBeInTheDocument();
});

it("требует точное подтверждение перед удалением общей базы", async () => {
  referenceAdminState = {
    ...referenceAdminState,
    active: {
      state: "ready",
      ready: true,
      message: "Каноническая база подключена.",
      signature: "unsigned-experimental",
      items: 445,
      index_cache: "hit",
    },
    managed_file_present: true,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("button", { name: "Удалить базу" })).toBeEnabled();
  expect(screen.getByRole("region", { name: "Локальная общая справка" })).toHaveClass(
    "reference-admin-card",
  );
  fireEvent.click(screen.getByRole("button", { name: "Удалить базу" }));
  const dialog = screen.getByRole("dialog", { name: "Удалить локальную общую базу?" });
  expect(dialog).toBeInTheDocument();
  const confirm = within(dialog).getByRole("button", { name: "Удалить базу" });
  expect(confirm).toBeDisabled();
  fireEvent.click(within(dialog).getByRole("button", { name: "Скопировать точное имя" }));
  await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("reference.mcp1cref"));
  expect(within(dialog).getByRole("button", { name: "Точное имя скопировано" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Для подтверждения введите точное имя:"), {
    target: { value: "reference.mcp1cref" },
  });
  expect(confirm).toBeEnabled();
  fireEvent.click(confirm);
  await waitFor(() => {
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/reference/remove",
      expect.objectContaining({
        body: JSON.stringify({ confirmation: "reference.mcp1cref" }),
      }),
    );
  });
  const success = await screen.findByRole("status");
  expect(success).toHaveClass("admin-feedback", "is-success");
  expect(success.nextElementSibling).toHaveClass("reference-actions");
});

it("показывает подтверждение рестарта только для pending общей базы", async () => {
  referenceAdminState = {
    ...referenceAdminState,
    pending: {
      state: "pending_restart",
      ready: false,
      message: "База проверена и будет активна после перезапуска сервера.",
      signature: "unsigned-experimental",
      items: 445,
      action: "activate",
    },
    managed_file_present: true,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("button", { name: "Перезапустить и применить" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Перезапустить и применить" }));
  expect(screen.getByRole("dialog", { name: "Перезапустить сервер?" })).toBeInTheDocument();
  expect(screen.getByText(/Текущие MCP-сеансы будут разорваны/)).toBeInTheDocument();
});

it("удаляет файл вне реестра через простое подтверждение без ввода пути", async () => {
  const orphanPath = "sources/configurations/source-очень-длинный-идентификатор.zip";
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/sources/admin") {
        return {
          ok: true,
          json: async () => ({
            api_version: "v1",
            limits: { upload_bytes: 500 * 1024 * 1024 },
            configuration_names: ["Отраслевая конфигурация А"],
            jobs: [],
            incoming: [],
            incoming_exists: true,
            incoming_dir: "data/incoming/",
            orphans: [{ path: orphanPath, size: 1024 }],
            snapshot_error: "",
          }),
        };
      }
      if (path === "/api/v1/sources/intake") {
        return { ok: true, json: async () => intakeSnapshot() };
      }
      return {
        ok: true,
        json: async () => ({
          api_version: "v1",
          permissions: { read: true, admin: true },
          configurations: [
            {
              id: "Отраслевая конфигурация А",
              version: "1.0",
              platform: "8.3.23.1997",
              objects: 120,
              edges: 640,
              loaded_at: "2026-08-28T09:00:00+00:00",
              notes: [],
              source: configurationSource("Отраслевая конфигурация А"),
              corpora: [],
            },
          ],
          references: [],
        }),
      };
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "Удалить файл" }));
  const dialog = screen.getByRole("dialog", { name: "Удалить исходный файл?" });
  expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
  expect(within(dialog).queryByText(orphanPath)).not.toBeInTheDocument();
  const remove = within(dialog).getByRole("button", { name: "Удалить файл" });
  expect(remove).toBeEnabled();
  fireEvent.click(remove);
  await waitFor(() => {
    const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input) === "/api/v1/sources/forget");
    expect(request).toBeDefined();
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      path: orphanPath,
      confirmation: orphanPath,
    });
  });
});

it("обновляет корпуса после перехода фоновой загрузки в готово", async () => {
  let sourceRequests = 0;
  let adminRequests = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/sources/admin") {
        adminRequests += 1;
        return {
          ok: true,
          json: async () => ({
            api_version: "v1",
            limits: { upload_bytes: 500 * 1024 * 1024 },
            configuration_names: ["Отраслевая конфигурация"],
            jobs: [
              {
                name: "СинтетическоеРасширение.zip",
                size: 1024,
                state: adminRequests > 1 ? "готово" : "разбирается",
                error: "",
              },
            ],
            incoming: [],
            incoming_exists: true,
            incoming_dir: "data/incoming/",
            orphans: [],
            snapshot_error: "",
          }),
        };
      }
      if (path === "/api/v1/sources/intake") {
        return { ok: true, json: async () => intakeSnapshot() };
      }
      sourceRequests += 1;
      return {
        ok: true,
        json: async () => ({
          api_version: "v1",
          permissions: { read: true, admin: true },
          configurations: [
            {
              id: "Отраслевая конфигурация",
              version: "1.0",
              platform: "8.3.23.1997",
              objects: 120,
              edges: 640,
              loaded_at: "2026-08-28T09:00:00+00:00",
              notes: [],
              source: configurationSource("Отраслевая конфигурация"),
              corpora: sourceRequests > 1
                ? [
                    corpus("synthetic:modules", "Основная конфигурация", "modules"),
                    corpus("synthetic:extension", "Загруженное расширение", "extension"),
                  ]
                : [corpus("synthetic:modules", "Основная конфигурация", "modules")],
            },
          ],
          references: [],
        }),
      };
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText("разбирается")).toBeInTheDocument();
  await client.refetchQueries({ queryKey: ["sources", "admin"], exact: true });
  expect(await screen.findByText("Загруженное расширение")).toBeInTheDocument();
  expect(sourceRequests).toBe(2);
});

it("сохраняет полное длинное имя конфигурации для подсказки", async () => {
  const longName = "ОтраслеваяКонфигурацияСОченьДлиннымИдентификаторомДляПроверки";
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/sources/admin") {
        return {
          ok: true,
          json: async () => ({
            api_version: "v1",
            limits: { upload_bytes: 500 * 1024 * 1024 },
            configuration_names: [longName],
            jobs: [],
            incoming: [],
            incoming_exists: true,
            incoming_dir: "data/incoming/",
            orphans: [],
            snapshot_error: "",
          }),
        };
      }
      if (path === "/api/v1/sources/intake") {
        return { ok: true, json: async () => intakeSnapshot() };
      }
      return {
        ok: true,
        json: async () => ({
          api_version: "v1",
          permissions: { read: true, admin: true },
          configurations: [
            {
              id: longName,
              version: "1.0",
              platform: "8.3.23.1997",
              objects: 120,
              edges: 640,
              loaded_at: "2026-08-28T09:00:00+00:00",
              notes: [],
              source: configurationSource(longName),
              corpora: [corpus("long:modules", "Основная конфигурация", "modules")],
            },
          ],
          references: [],
        }),
      };
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: longName })).toHaveAttribute("title", longName);
});
