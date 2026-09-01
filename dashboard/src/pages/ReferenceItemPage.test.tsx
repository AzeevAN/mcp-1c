import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { ReferenceItemPage } from "./ReferenceItemPage";

const status = {
  api_version: "v1",
  active: { state: "ready", ready: true, message: "Подключена." },
  catalog: {
    domains: [{
      id: "query",
      title: "Язык запросов",
      description: "Операторы запросов.",
      access_scope: "default",
      items: 1,
    }],
    kinds: [{
      id: "query_keyword",
      domain: "query",
      title: "Оператор языка запросов",
      access_scope: "default",
      items: 1,
    }],
    platform_versions: ["8.3.20"],
  },
  pending: null,
  managed_upload: true,
  managed_file_present: true,
  limits: { upload_bytes: 1 },
};

const card = {
  card: {
    id: "query/Example",
    section_id: "query/Example#usage",
    domain: "query",
    kind: "query_keyword",
    title_ru: "Пример",
    title_en: "Example",
    source_key: "synthetic",
    source_path: "synthetic/example",
  },
  availability: { status: "available", platform: "8.3.20", reason: "Подтверждена версия появления 8.3.10." },
  content_format: "markdown",
  content: "## Описание\n\n<script>alert('x')</script>",
  html: "<h2>Описание</h2><p>&lt;script&gt;alert('x')&lt;/script&gt;</p>",
  continuation: { offset: 0, next_offset: 42, total_chars: 84, next_cursor: "next-token" },
};

function renderPage(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={client}><ReferenceItemPage /></QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => ({
    ok: true,
    status: 200,
    json: async () => String(input).includes("/item") ? card : status,
  } as Response)));
});

it("открывает отдельную карточку и сохраняет возврат к выдаче", async () => {
  renderPage("/reference/item?query=пример&domain=query&item_id=query%2FExample&section_id=query%2FExample%23usage&platform=8.3.20");

  expect(await screen.findByRole("heading", { name: "Пример" })).toBeInTheDocument();
  expect(screen.getAllByText("Язык запросов").length).toBeGreaterThan(0);
  expect(screen.getAllByText("Оператор языка запросов").length).toBeGreaterThan(0);
  expect(screen.getByText("Подтверждена версия появления 8.3.10.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /К результатам общей справки/ })).toHaveAttribute(
    "href",
    "/reference?query=%D0%BF%D1%80%D0%B8%D0%BC%D0%B5%D1%80&domain=query&platform=8.3.20",
  );
  expect(screen.getByRole("link", { name: /Открыть карточку целиком/ })).not.toHaveAttribute(
    "href",
    expect.stringContaining("section_id"),
  );
});

it("показывает безопасный html и позволяет увидеть исходный Markdown", async () => {
  renderPage("/reference/item?item_id=query%2FExample");

  expect(await screen.findByRole("heading", { name: "Описание" })).toBeInTheDocument();
  expect(screen.getByText("<script>alert('x')</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Как есть" }));

  expect(screen.getByText("## Описание", { exact: false })).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
});

it("без целевой платформы показывает известную версию появления", async () => {
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => ({
    ok: true,
    status: 200,
    json: async () => String(input).includes("/item") ? {
      ...card,
      availability: {
        status: "unknown",
        platform: null,
        introduced: "8.3.10",
        removed: null,
        known_present_in: null,
        reason: "Подтверждена версия появления 8.3.10. Целевая версия платформы не указана.",
        evidence: [],
      },
    } : status,
  } as Response));

  renderPage("/reference/item?item_id=query%2FExample");

  expect(await screen.findByText("Версия появления")).toBeInTheDocument();
  expect(screen.getByText(/Подтверждена версия появления 8.3.10/)).toBeInTheDocument();
  expect(screen.queryByText("Совместимость не определена")).not.toBeInTheDocument();
});

it("читает следующую часть по непрозрачному курсору", async () => {
  renderPage("/reference/item?item_id=query%2FExample");

  fireEvent.click(await screen.findByRole("button", { name: /Следующая часть/ }));

  await waitFor(() => {
    expect(vi.mocked(fetch).mock.calls.some(
      ([input]) => String(input).includes("/item") && String(input).includes("cursor=next-token"),
    )).toBe(true);
  });
});

it("объясняет прямой маршрут без выбранного результата", () => {
  renderPage("/reference/item");

  expect(screen.getByRole("heading", { name: "Карточка не выбрана" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Перейти к поиску" })).toHaveAttribute("href", "/reference");
});
