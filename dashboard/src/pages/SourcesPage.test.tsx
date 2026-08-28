import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
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
  expect(screen.getByText("Расширение Доп")).toBeInTheDocument();
  expect(screen.getAllByText("Структуры форм")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Показать подробности Расширение Доп" }));
  expect(screen.getAllByText("Структуры форм")).toHaveLength(2);
  expect(screen.getAllByRole("link", { name: "Открыть JSON-журнал" })).toHaveLength(2);
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
