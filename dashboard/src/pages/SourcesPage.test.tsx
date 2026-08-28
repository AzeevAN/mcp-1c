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

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        api_version: "v1",
        permissions: { read: true, admin: true },
        configurations: [
          {
            id: "Конфигурация А",
            version: "1.0",
            platform: "8.3.23.1997",
            objects: 120,
            edges: 640,
            loaded_at: "2026-08-28T09:00:00+00:00",
            notes: [],
            source: null,
            corpora: [corpus("a:modules", "Основная конфигурация", "modules")],
          },
          {
            id: "Конфигурация Б",
            version: "2.4",
            platform: "8.3.27.1000",
            objects: 340,
            edges: 980,
            loaded_at: "2026-08-28T10:00:00+00:00",
            notes: ["Проверить соответствие версии справки."],
            source: null,
            corpora: [
              corpus("b:modules", "Основная конфигурация", "modules"),
              corpus("b:ext:Доп", "Расширение Доп", "extension"),
            ],
          },
        ],
        references: [],
      }),
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

  expect(await screen.findByRole("heading", { name: "Конфигурация А" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Конфигурация Б/ }));

  expect(screen.getByRole("heading", { name: "Конфигурация Б" })).toBeInTheDocument();
  expect(screen.getByText("Расширение Доп")).toBeInTheDocument();
  expect(screen.getAllByText("Структуры форм")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Показать подробности Расширение Доп" }));
  expect(screen.getAllByText("Структуры форм")).toHaveLength(2);
  expect(screen.getAllByRole("link", { name: "Открыть JSON-журнал" })).toHaveLength(2);
});
