import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { QueriesPage } from "./QueriesPage";

const setup = {
  api_version: "v1",
  configuration_names: ["Отраслевая конфигурация А", "Отраслевая конфигурация Б"],
  default_configuration: "Отраслевая конфигурация А",
  scopes: [
    { id: "objects", label: "Объекты", requires_configuration: true },
    { id: "fields", label: "Реквизиты", requires_configuration: true },
    { id: "syntax", label: "Справка и язык запросов", requires_configuration: false },
  ],
  limits: { phrases: 32, phrase_chars: 4096, results_per_phrase: 5 },
  availability: { configurations: true, syntax: true, query_language: true },
};

const runResult = {
  api_version: "v1",
  request: {
    config: "Отраслевая конфигурация Б",
    scope: "fields",
    phrases: ["номер телефона"],
  },
  results: [
    {
      phrase: "номер телефона",
      alias_url: "/dictionary?config=%D0%9E%D1%82%D1%80%D0%B0%D1%81%D0%BB%D0%B5%D0%B2%D0%B0%D1%8F&phrase=%D0%BD%D0%BE%D0%BC%D0%B5%D1%80",
      hits: [
        {
          position: 1,
          id: "Справочник.Контрагенты.Телефон",
          title: "Справочник.Контрагенты.Телефон",
          kind: "Реквизит",
          score: 31.45,
          reason: "все слова запроса",
          card_url: "/object?config=%D0%9E%D1%82%D1%80%D0%B0%D1%81%D0%BB%D0%B5%D0%B2%D0%B0%D1%8F&name=%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%87%D0%BD%D0%B8%D0%BA",
        },
      ],
      hidden: [
        {
          title: "Метод.Новый",
          reason: "появился в 8.3.24",
        },
      ],
    },
  ],
};

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function BackPage() {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(-1)}>Вернуться назад</button>;
}

beforeEach(() => {
  window.sessionStorage.clear();
  vi.stubGlobal("scrollTo", vi.fn());
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => (init?.method === "POST" ? runResult : setup),
    })),
  );
});

it("запускает несколько фраз и показывает объяснимую выдачу", async () => {
  render(
    <MemoryRouter initialEntries={["/queries?config=Отраслевая+конфигурация+Б&scope=fields"]}>
      <QueryClientProvider client={client()}>
        <QueriesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Проверка поисковых формулировок" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Конфигурация" })).toHaveValue("Отраслевая конфигурация Б");
  expect(screen.getByRole("combobox", { name: "Конфигурация" }).querySelector('option[value=""]')).toBeNull();
  expect(screen.getByRole("radio", { name: /Реквизиты/ })).toBeChecked();

  fireEvent.change(screen.getByRole("textbox", { name: "Поисковые фразы" }), {
    target: { value: "номер телефона\n\nдата документа" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Прогнать запросы" }));

  expect(await screen.findByRole("heading", { name: "номер телефона" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Справочник.Контрагенты.Телефон" })).toHaveAttribute("href", expect.stringContaining("/object?"));
  expect(screen.getByText("Реквизит")).toBeInTheDocument();
  expect(screen.getByText("31,5")).toBeInTheDocument();
  expect(screen.getByText("все слова запроса")).toBeInTheDocument();
  expect(screen.getByText("Скрыто фильтром версии")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Завести псевдоним/ })).toHaveAttribute("href", expect.stringContaining("/dictionary?"));

  await waitFor(() => {
    const post = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === "POST");
    expect(post).toBeDefined();
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      config: "Отраслевая конфигурация Б",
      scope: "fields",
      phrases: ["номер телефона", "дата документа"],
    });
  });
});

it("восстанавливает введённую фразу и результат после перехода по карточке назад", async () => {
  const view = render(
    <MemoryRouter initialEntries={["/queries?config=Отраслевая+конфигурация+Б&scope=fields"]}>
      <QueryClientProvider client={client()}>
        <Routes>
          <Route path="/queries" element={<QueriesPage />} />
          <Route path="/object" element={<BackPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );

  const input = await screen.findByRole("textbox", { name: "Поисковые фразы" });
  fireEvent.change(input, { target: { value: "номер телефона" } });
  fireEvent.click(screen.getByRole("button", { name: "Прогнать запросы" }));
  const resultLink = await screen.findByRole("link", { name: "Справочник.Контрагенты.Телефон" });

  Object.defineProperty(window, "scrollY", { configurable: true, value: 280 });
  fireEvent.click(resultLink);
  fireEvent.click(await screen.findByRole("button", { name: "Вернуться назад" }));

  expect(await screen.findByRole("textbox", { name: "Поисковые фразы" })).toHaveValue("номер телефона");
  expect(screen.getByRole("link", { name: "Справочник.Контрагенты.Телефон" })).toBeInTheDocument();
  await waitFor(() => expect(window.scrollTo).toHaveBeenCalledWith({ top: 280, behavior: "auto" }));
  view.unmount();
});

it("объясняет пустой Registry и недоступность областей поиска", async () => {
  vi.mocked(fetch).mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({
      ...setup,
      configuration_names: [],
      default_configuration: "",
      availability: { configurations: false, syntax: false, query_language: false },
    }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/queries"]}>
      <QueryClientProvider client={client()}><QueriesPage /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText("Поисковые источники пока не загружены")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Прогнать запросы" })).toBeDisabled();
  expect(screen.getByRole("radio", { name: /Объекты/ })).toBeDisabled();
  expect(screen.getByRole("radio", { name: /Справка и язык запросов/ })).toBeDisabled();
});

it("без конфигураций сразу выбирает доступный поиск по справке", async () => {
  vi.mocked(fetch).mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({
      ...setup,
      configuration_names: [],
      default_configuration: "",
      availability: { configurations: false, syntax: true, query_language: true },
    }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/queries"]}>
      <QueryClientProvider client={client()}><QueriesPage /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("radio", { name: /Справка и язык запросов/ })).toBeChecked();
  expect(screen.getByRole("button", { name: "Прогнать запросы" })).toBeEnabled();
  expect(screen.getByRole("combobox", { name: "Конфигурация" })).toHaveValue("");
});
