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

it("возвращает карточку в сохранённое состояние связей", async () => {
  render(
    <MemoryRouter initialEntries={["/object?config=Отраслевая+конфигурация+А&name=Справочник.Контрагенты&from=%2Fgraph%3Fconfig%3DОтраслевая%2Bконфигурация%2BА%26name%3DСправочник.%25D0%259A%25D0%25BE%25D0%25BD%25D1%2582%25D1%2580%25D0%25B0%25D0%25B3%25D0%25B5%25D0%25BD%25D1%2582%25D1%258B%26limit%3D30"]}>
      <QueryClientProvider client={client()}><CardPage kind="object" /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Справочник: Контрагенты" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Вернуться к связям" })).toHaveAttribute(
    "href",
    "/graph?config=Отраслевая+конфигурация+А&name=Справочник.%D0%9A%D0%BE%D0%BD%D1%82%D1%80%D0%B0%D0%B3%D0%B5%D0%BD%D1%82%D1%8B&limit=30",
  );
});

it("сохраняет параметры разрешённого возврата к запросам", async () => {
  const from = "/queries?query=Контрагенты&limit=10";
  render(
    <MemoryRouter initialEntries={[`/object?name=Справочник.Контрагенты&from=${encodeURIComponent(from)}`]}>
      <QueryClientProvider client={client()}><CardPage kind="object" /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("link", { name: "К результатам запросов" })).toHaveAttribute("href", from);
});

it.each([
  "/\\evil.invalid",
  "//evil.invalid/graph",
  "https://evil.invalid/graph",
  "%2F%2Fevil.invalid/graph",
  "/%5Cevil.invalid/graph",
])("отклоняет небезопасный адрес возврата: %s", async (from) => {
  render(
    <MemoryRouter initialEntries={[`/object?name=Справочник.Контрагенты&from=${encodeURIComponent(from)}`]}>
      <QueryClientProvider client={client()}><CardPage kind="object" /></QueryClientProvider>
    </MemoryRouter>,
  );

  const link = await screen.findByRole("link", { name: /Вернуться к связям|К результатам запросов/ }) as HTMLAnchorElement;
  expect(link).toHaveAccessibleName("К результатам запросов");
  expect(link).toHaveAttribute("href", "/queries");
  expect(new URL(link.href).origin).toBe(window.location.origin);
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
  expect(screen.getByRole("link", { name: "К результатам запросов" })).toHaveAttribute("href", "/queries");
});

it("показывает ссылки синтаксиса отдельно от буквального ответа MCP и открывает точную карточку", async () => {
  vi.mocked(fetch).mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({
      ...objectCard,
      kind: "syntax",
      name: "Запрос",
      configuration: "Демо",
      configuration_names: ["Демо"],
      detail: "full",
      detail_levels: ["brief", "fields", "full", "links"],
      markdown: "# Запрос\n\nБуквальный текст MCP",
      html: "<h1>Запрос</h1><p>Буквальный текст MCP</p>",
      navigation: [{
        variant: "Запрос",
        state: "known",
        platform: "8.3.27.2130",
        total: 3,
        offset: 0,
        next_offset: null,
        items: [
          { section: "methods", label: "Выполнить", address: "Запрос.Выполнить", status: "ready", target_name: "Выполнить" },
          { section: "see_also", label: "Внешняя цель", address: "https://evil.invalid", status: "unresolved", target_name: "" },
          { section: "methods", label: "Старый метод", address: "Запрос.СтарыйМетод", status: "unavailable", target_name: "СтарыйМетод" },
        ],
      }],
    }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/syntax?name=Запрос&config=Демо&detail=full"]}>
      <QueryClientProvider client={client()}>
        <Routes>
          <Route path="/syntax" element={<><CardPage kind="syntax" /><LocationProbe /></>} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Связанные страницы справки" })).toBeInTheDocument();
  expect(screen.getByText("Буквальный текст MCP")).toBeInTheDocument();
  expect(screen.getAllByText("Методы:")).toHaveLength(2);
  expect(screen.getByText("См. также:")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Выполнить" })).toHaveAttribute("href", "/syntax?name=%D0%97%D0%B0%D0%BF%D1%80%D0%BE%D1%81.%D0%92%D1%8B%D0%BF%D0%BE%D0%BB%D0%BD%D0%B8%D1%82%D1%8C&detail=full&config=%D0%94%D0%B5%D0%BC%D0%BE");
  expect(screen.queryByRole("link", { name: "Внешняя цель" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Старый метод" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Как есть" }));
  expect(screen.getByText(/# Запрос/)).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Связанные страницы справки" })).toBeInTheDocument();
});

it("запрашивает страницы ссылок по offset и сбрасывает его при смене подробности", async () => {
  vi.mocked(fetch).mockImplementation(async (input) => {
    const url = new URL(String(input), "http://localhost");
    const detail = url.searchParams.get("detail") || "fields";
    const offset = Number(url.searchParams.get("links_offset") || "0");
    return {
      ok: true,
      status: 200,
      json: async () => ({
        ...objectCard,
        kind: "syntax",
        name: "Глобальный контекст",
        configuration: "",
        configuration_names: [],
        detail,
        detail_levels: ["brief", "fields", "full", "links"],
        navigation: [{
          variant: "Глобальный контекст",
          state: "known",
          platform: "8.3.27.2130",
          total: 120,
          offset,
          next_offset: offset < 100 ? offset + 50 : null,
          items: [{ section: "methods", label: `Метод ${offset}`, address: `Метод${offset}`, status: "ready", target_name: `Метод${offset}` }],
        }],
      }),
    } as Response;
  });

  render(
    <MemoryRouter initialEntries={["/syntax?name=Глобальный+контекст&detail=links"]}>
      <QueryClientProvider client={client()}>
        <Routes><Route path="/syntax" element={<><CardPage kind="syntax" /><LocationProbe /></>} /></Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("link", { name: "Метод 0" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Далее" }));
  expect(await screen.findByRole("link", { name: "Метод 50" })).toBeInTheDocument();
  expect(vi.mocked(fetch)).toHaveBeenLastCalledWith(expect.stringContaining("links_offset=50"));
  fireEvent.click(screen.getByRole("button", { name: "Назад" }));
  expect(await screen.findByRole("link", { name: "Метод 0" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "full" }));
  await waitFor(() => expect(screen.getByLabelText("Текущий адрес")).not.toHaveTextContent("links_offset"));
});

it("из полной карточки открывает следующую страницу ссылок одним переходом", async () => {
  vi.mocked(fetch).mockImplementation(async (input) => {
    const url = new URL(String(input), "http://localhost");
    const detail = url.searchParams.get("detail") || "full";
    const offset = Number(url.searchParams.get("links_offset") || "0");
    return {
      ok: true,
      status: 200,
      json: async () => ({
        ...objectCard,
        kind: "syntax",
        name: "Глобальный контекст",
        configuration: "Демо",
        configuration_names: ["Демо"],
        detail,
        detail_levels: ["brief", "fields", "full", "links"],
        navigation: [{
          variant: "Глобальный контекст",
          state: "known",
          platform: "8.3.27.2130",
          total: 70,
          offset,
          next_offset: offset === 0 ? 50 : null,
          items: [{ section: "methods", label: `Метод ${offset}`, address: `Метод${offset}`, status: "ready", target_name: `Метод${offset}` }],
        }],
      }),
    } as Response;
  });

  render(
    <MemoryRouter initialEntries={["/syntax?name=Глобальный+контекст&config=Демо&detail=full"]}>
      <QueryClientProvider client={client()}>
        <Routes><Route path="/syntax" element={<><CardPage kind="syntax" /><LocationProbe /></>} /></Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "Показать остальные ссылки" }));
  expect(await screen.findByRole("link", { name: "Метод 50" })).toBeInTheDocument();
  const lastRequest = new URL(String(vi.mocked(fetch).mock.lastCall?.[0]), "http://localhost");
  expect(lastRequest.searchParams.get("detail")).toBe("links");
  expect(lastRequest.searchParams.get("links_offset")).toBe("50");
  expect(screen.getByLabelText("Текущий адрес")).toHaveTextContent("config=%D0%94%D0%B5%D0%BC%D0%BE");
});

it("пустая страница ссылок не показывает обратный диапазон", async () => {
  vi.mocked(fetch).mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({
      ...objectCard,
      kind: "syntax",
      configuration: "",
      configuration_names: [],
      detail: "links",
      detail_levels: ["brief", "fields", "full", "links"],
      navigation: [{
        variant: "Глобальный контекст",
        state: "known",
        platform: "8.3.27.2130",
        total: 70,
        offset: 100,
        next_offset: null,
        items: [],
      }],
    }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/syntax?name=Глобальный+контекст&detail=links&links_offset=100"]}>
      <QueryClientProvider client={client()}><CardPage kind="syntax" /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText(/На этой странице ссылок нет/)).toBeInTheDocument();
  expect(screen.queryByText(/Показаны 101/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Назад" })).toBeEnabled();
});
