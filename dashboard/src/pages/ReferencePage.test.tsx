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
  pending: null,
  managed_upload: true,
  managed_file_present: true,
  limits: { upload_bytes: 33 * 1024 * 1024 },
};

const searchResult = {
  query: "показать образец",
  domain: null,
  kind: null,
  platform: "8.3.20",
  results: [{
    id: "bsl/Example",
    matched_section_id: null,
    domain: "bsl",
    kind: "function",
    title_ru: "Пример",
    title_en: "Example",
    signature: "Пример()",
    access_scope: "public",
    availability: { status: "available", platform: "8.3.20", reason: "Доступен.", evidence: [] },
    score: 14.25,
    reason: "все слова запроса",
  }],
  unavailable_matches: [],
};

const card = {
  card: {
    id: "bsl/Example",
    section_id: null,
    domain: "bsl",
    kind: "function",
    title_ru: "Пример",
    title_en: "Example",
    source_key: "synthetic",
    source_path: "synthetic/example",
  },
  availability: { status: "available", platform: "8.3.20", reason: "Доступен.", evidence: [] },
  content_format: "markdown",
  content: "## Описание\n\n<script>alert('x')</script>",
  continuation: { offset: 0, next_offset: 42, total_chars: 42, next_cursor: null },
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
    const url = String(input);
    const payload = url.includes("/search")
      ? searchResult
      : url.includes("/item") ? card : status;
    return { ok: true, status: 200, json: async () => payload } as Response;
  }));
});

it("открывается из меню без параметров и остаётся read-only", async () => {
  renderPage();

  expect(await screen.findByRole("heading", { name: "Общая справка" })).toBeInTheDocument();
  expect(screen.getByText("Каноническая база подключена.")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Поиск" })).toBeInTheDocument();
  expect(screen.queryByText(/загрузить/i)).not.toBeInTheDocument();
});

it("ищет, выбирает карточку и экранирует её текст", async () => {
  renderPage();
  fireEvent.change(await screen.findByRole("textbox", { name: "Поиск" }), {
    target: { value: "показать образец" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Версия платформы" }), {
    target: { value: "8.3.20" },
  });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Лимит" }), {
    target: { value: "1" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Найти" }));

  const result = await screen.findByRole("button", { name: /Пример/ });
  expect(screen.getByText("все слова запроса")).toBeInTheDocument();
  fireEvent.click(result);

  expect(await screen.findByText("<script>alert('x')</script>", { exact: false })).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  await waitFor(() => {
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("platform=8.3.20"))).toBe(true);
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("limit=1"))).toBe(true);
  });
});

it("объясняет неактивное состояние и ведёт на Источники без формы", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      ...status,
      active: { state: "untrusted", ready: false, message: "Артефакт подписан неизвестным ключом." },
      managed_file_present: false,
    }),
  } as Response);

  renderPage();

  expect(await screen.findByText("Артефакт подписан неизвестным ключом.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Открыть Источники" })).toHaveAttribute("href", "/sources");
  expect(screen.queryByRole("textbox", { name: "Поиск" })).not.toBeInTheDocument();
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

  fireEvent.change(await screen.findByRole("textbox", { name: "Поиск" }), {
    target: { value: "небывалыйтокен" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Найти" }));

  expect(await screen.findByText("Ничего не найдено.")).toBeInTheDocument();
});

it("прямая ссылка выбирает карточку", async () => {
  renderPage("/reference?query=%D0%BE%D0%B1%D1%80%D0%B0%D0%B7%D0%B5%D1%86&item_id=bsl%2FExample&platform=8.3.20");

  expect(await screen.findByText("<script>alert('x')</script>", { exact: false })).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Поиск" })).toHaveValue("образец");
});
