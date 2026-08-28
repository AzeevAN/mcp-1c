import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { CardPage } from "./CardPage";

const objectCard = {
  api_version: "v1",
  kind: "object",
  name: "Справочник.Контрагенты",
  configuration: "Отраслевая конфигурация А",
  configuration_names: ["Отраслевая конфигурация А", "Отраслевая конфигурация Б"],
  configuration_required: true,
  detail: "fields",
  detail_levels: ["brief", "fields", "full"],
  markdown: "# Справочник: Контрагенты\n\n## Реквизиты\n\n- `Телефон` — Строка(20)",
  html: "<h1>Справочник: Контрагенты</h1><h2>Реквизиты</h2><ul><li><code>Телефон</code> — Строка(20)</li></ul>",
};

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="Текущий адрес">{location.pathname + location.search}</output>;
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => objectCard,
    }),
  );
});

it("показывает буквальную карточку MCP и переключает исходный Markdown", async () => {
  render(
    <MemoryRouter initialEntries={["/object?config=Отраслевая+конфигурация+А&name=Справочник.Контрагенты&detail=fields"]}>
      <QueryClientProvider client={client()}>
        <Routes>
          <Route path="/object" element={<><CardPage kind="object" /><LocationProbe /></>} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Справочник: Контрагенты" })).toBeInTheDocument();
  expect(screen.getByText("Телефон")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Конфигурация" })).toHaveValue("Отраслевая конфигурация А");
  expect(screen.getByRole("button", { name: "fields" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByText("Тот же ответ, что получает агент")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Как есть" }));
  expect(screen.getByText(/# Справочник: Контрагенты/)).toBeInTheDocument();
  expect(screen.getByLabelText("Текущий адрес")).toHaveTextContent("raw=1");

  fireEvent.click(screen.getByRole("button", { name: "brief" }));
  await waitFor(() => {
    expect(vi.mocked(fetch)).toHaveBeenLastCalledWith(expect.stringContaining("detail=brief"));
  });
});

it("карточка синтаксиса работает без конфигурации и сохраняет точный адрес", async () => {
  vi.mocked(fetch).mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({
      ...objectCard,
      kind: "syntax",
      name: "Запрос.СтрНайти",
      configuration: "",
      configuration_names: [],
      configuration_required: false,
      markdown: "# Функция запроса: СтрНайти",
      html: "<h1>Функция запроса: СтрНайти</h1>",
    }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/syntax?name=Запрос.СтрНайти"]}>
      <QueryClientProvider client={client()}><CardPage kind="syntax" /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Функция запроса: СтрНайти" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Конфигурация" })).toHaveValue("");
  expect(screen.getByRole("option", { name: "Без фильтра по версии" })).toBeInTheDocument();
  expect(vi.mocked(fetch)).toHaveBeenCalledWith(expect.stringContaining("name=%D0%97%D0%B0%D0%BF%D1%80%D0%BE%D1%81.%D0%A1%D1%82%D1%80%D0%9D%D0%B0%D0%B9%D1%82%D0%B8"));
});

it("прямая ссылка без имени объясняет путь и не обращается к API", () => {
  render(
    <MemoryRouter initialEntries={["/object"]}>
      <QueryClientProvider client={client()}><CardPage kind="object" /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(screen.getByRole("heading", { name: "Карточка не выбрана" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Перейти к запросам" })).toHaveAttribute("href", "/queries");
  expect(fetch).not.toHaveBeenCalled();
});

it("показывает предметную ошибку API без потери возврата к поиску", async () => {
  vi.mocked(fetch).mockResolvedValueOnce({
    ok: false,
    status: 409,
    json: async () => ({ error: "Справка платформы не подключена." }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/syntax?name=СтрНайти"]}>
      <QueryClientProvider client={client()}><CardPage kind="syntax" /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("alert")).toHaveTextContent("Справка платформы не подключена.");
  expect(screen.getByRole("link", { name: "Вернуться к запросам" })).toHaveAttribute("href", "/queries");
});
