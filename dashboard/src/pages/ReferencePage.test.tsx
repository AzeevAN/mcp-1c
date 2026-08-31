import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { ReferencePage } from "./ReferencePage";

const status = {
  api_version: "v1",
  active: {
    state: "ready",
    ready: true,
    message: "Каноническая база подключена.",
  },
  catalog: {
    domains: [
      {
        id: "bsl",
        title: "Встроенный язык (BSL)",
        description: "Конструкции BSL.",
        access_scope: "default",
        items: 1,
      },
      {
        id: "query",
        title: "Язык запросов",
        description: "Операторы запросов.",
        access_scope: "default",
        items: 2,
      },
    ],
    kinds: [
      {
        id: "bsl_construct",
        domain: "bsl",
        title: "Конструкция BSL",
        access_scope: "default",
        items: 1,
      },
      {
        id: "query_keyword",
        domain: "query",
        title: "Оператор языка запросов",
        access_scope: "default",
        items: 2,
      },
    ],
    platform_versions: ["8.3.20"],
  },
  pending: null,
  managed_upload: true,
  managed_file_present: true,
  limits: { upload_bytes: 33 * 1024 * 1024 },
};

const searchResult = {
  query: "показать образец",
  domain: "query",
  kind: null,
  platform: "8.3.20",
  results: [{
    id: "query/Example",
    matched_section_id: "query/Example#usage",
    domain: "query",
    kind: "query_keyword",
    title_ru: "Пример",
    title_en: "Example",
    signature: "ПРИМЕР",
    access_scope: "default",
    availability: { status: "available", platform: "8.3.20", reason: "Доступен.", evidence: [] },
    score: 14.25,
    reason: "все слова запроса",
  }],
  has_more: false,
  unavailable_matches: [],
};

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderPage(entry = "/reference") {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={client()}><ReferencePage /></QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const payload = String(input).includes("/search") ? searchResult : status;
    return { ok: true, status: 200, json: async () => payload } as Response;
  }));
});

it("открывается из меню с короткой понятной формой", async () => {
  renderPage();

  expect(await screen.findByRole("heading", { name: "Общая справка" })).toBeInTheDocument();
  expect(screen.getByText("Каноническая база подключена.")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Что найти" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Раздел" })).toHaveTextContent("Язык запросов");
  expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  expect(screen.queryByText("Explicit")).not.toBeInTheDocument();
  expect(screen.queryByText("Hidden")).not.toBeInTheDocument();
  expect(screen.queryByText(/загрузить/i)).not.toBeInTheDocument();
});

it("передаёт объяснённые фильтры и ведёт в отдельную карточку", async () => {
  renderPage();
  fireEvent.change(await screen.findByRole("textbox", { name: "Что найти" }), {
    target: { value: "показать образец" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Раздел" }), {
    target: { value: "query" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Вид материала" }), {
    target: { value: "query_keyword" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Проверить совместимость с версией" }), {
    target: { value: "8.3.20" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: /Служебные и словарные/ }));
  fireEvent.click(screen.getByRole("button", { name: "Найти" }));

  const result = await screen.findByRole("link", { name: /Пример/ });
  expect(result).toHaveAttribute("href", expect.stringContaining("/reference/item?"));
  expect(result).toHaveAttribute("href", expect.stringContaining("item_id=query%2FExample"));
  expect(result).toHaveAttribute("href", expect.stringContaining("section_id=query%2FExample%23usage"));
  expect(screen.getByText("ПРИМЕР")).toBeInTheDocument();
  expect(screen.getByText("Язык запросов · Оператор языка запросов")).toBeInTheDocument();
  expect(screen.getByTitle("Оценка ранжирования")).toHaveTextContent("14,3");
  expect(screen.getByText("все слова запроса")).toBeInTheDocument();
  await waitFor(() => {
    const url = vi.mocked(fetch).mock.calls.map(([input]) => String(input)).find((item) => item.includes("/search")) || "";
    expect(url).toContain("domain=query");
    expect(url).toContain("kind=query_keyword");
    expect(url).toContain("platform=8.3.20");
    expect(url).toContain("include_explicit=1");
    expect(url).toContain("limit=5");
    expect(url).not.toContain("/item");
  });
});

it("показывает несовместимые совпадения отдельно", async () => {
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => ({
    ok: true,
    status: 200,
    json: async () => String(input).includes("/search")
      ? {
        ...searchResult,
        results: [],
        unavailable_matches: [{
          ...searchResult.results[0],
          availability: { status: "unavailable", platform: "8.3.5", reason: "Появился позже." },
        }],
        platform: "8.3.5",
      }
      : status,
  } as Response));
  renderPage("/reference?query=пример&domain=query&platform=8.3.5");

  expect(await screen.findByText("Скрыто версией")).toBeInTheDocument();
  expect(screen.getByText(/Не подходит для версии 8.3.5/)).toBeInTheDocument();
});

it("увеличивает внутренний лимит без отдельного поля", async () => {
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => ({
    ok: true,
    status: 200,
    json: async () => String(input).includes("/search")
      ? { ...searchResult, has_more: true }
      : status,
  } as Response));
  renderPage("/reference?query=пример&domain=query");

  fireEvent.click(await screen.findByRole("button", { name: "Показать ещё" }));

  await waitFor(() => {
    expect(vi.mocked(fetch).mock.calls.some(
      ([input]) => String(input).includes("/search") && String(input).includes("limit=10"),
    )).toBe(true);
  });
});

it("объясняет неактивное состояние и ведёт на Источники без формы", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      ...status,
      active: { state: "untrusted", ready: false, message: "Артефакт подписан неизвестным ключом." },
      catalog: null,
      managed_file_present: false,
    }),
  } as Response);

  renderPage();

  expect(await screen.findByText("Артефакт подписан неизвестным ключом.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Открыть Источники" })).toHaveAttribute("href", "/sources");
  expect(screen.queryByRole("textbox", { name: "Что найти" })).not.toBeInTheDocument();
});

it("показывает пустую выдачу", async () => {
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => ({
    ok: true,
    status: 200,
    json: async () => String(input).includes("/search")
      ? { ...searchResult, results: [] }
      : status,
  } as Response));
  renderPage();

  fireEvent.change(await screen.findByRole("textbox", { name: "Что найти" }), {
    target: { value: "небывалыйтокен" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Найти" }));

  expect(await screen.findByText(/Ничего не найдено/)).toBeInTheDocument();
});
