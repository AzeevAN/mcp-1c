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

beforeEach(() => {
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
          }),
        };
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

it("показывает администратору компактную загрузку, выбор конфигурации и подтверждение удаления", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <MemoryRouter initialEntries={["/sources"]}>
      <QueryClientProvider client={client}>
        <SourcesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Добавление и обслуживание данных" })).toBeInTheDocument();
  const truncated = screen.getByRole("checkbox", { name: /Разрешить неполную тестовую выгрузку/ });
  fireEvent.click(truncated);
  expect(truncated).toBeChecked();

  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  const file = new File(["synthetic"], "структура.zip", { type: "application/zip" });
  fireEvent.change(input!, { target: { files: [file] } });
  expect(screen.getByText("структура.zip")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Загрузить и разобрать" })).toBeEnabled();

  const parse = screen.getByRole("button", { name: "Разобрать" });
  expect(parse).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "Родительская конфигурация" }), {
    target: { value: "Отраслевая конфигурация Б" },
  });
  expect(parse).toBeEnabled();

  fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
  expect(screen.getByRole("dialog", { name: "Удалить «Отраслевая конфигурация А»?" })).toBeInTheDocument();
  expect(screen.getByText(/каскадно удалены структура конфигурации/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Удалить без возможности отмены" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Скопировать точное имя" }));
  await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("Отраслевая конфигурация А"));
  expect(screen.getByRole("button", { name: "Точное имя скопировано" })).toBeInTheDocument();
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
