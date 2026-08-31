import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { GraphPage } from "./GraphPage";

const base = {
  api_version: "v1",
  configuration_names: ["Отраслевая конфигурация А", "Отраслевая конфигурация Б"],
  configuration: "Отраслевая конфигурация А",
  name: "",
  limit: 30,
  limit_options: [15, 30, 60, 150, 400],
  state: "awaiting_object",
  message: "Введите полное имя объекта или возьмите его со страницы «Запросы».",
  suggestions: [],
  graph: null,
};

const subject = {
  name: "Справочник.Контрагенты",
  short: "Контрагенты",
  kind: "Справочник",
  degree: 4,
  x: 0,
  y: 0,
  color: "#4c8ed9",
  graph_url: "/graph?config=Отраслевая&name=Справочник.Контрагенты&limit=30",
  object_url: "/object?config=Отраслевая&name=Справочник.Контрагенты",
};

const neighbour = {
  name: "Документ.РеализацияТоваровУслуг",
  short: "РеализацияТоваровУслуг",
  kind: "Документ",
  degree: 2,
  x: 0,
  y: -190,
  color: "#e0803c",
  graph_url: "/graph?config=Отраслевая+конфигурация+А&name=Документ.РеализацияТоваровУслуг&limit=30",
  object_url: "/object?config=Отраслевая+конфигурация+А&name=Документ.РеализацияТоваровУслуг",
};

const ready = {
  ...base,
  name: subject.name,
  state: "ready",
  message: "",
  graph: {
    depth: 1,
    total: 4,
    shown: 1,
    truncated: true,
    bounds: [-280, -280, 560, 560],
    subject,
    nodes: [neighbour],
    links: [{
      source: neighbour.name,
      target: subject.name,
      title: "Реквизит Контрагент",
      outgoing: false,
    }],
    kinds: [
      { kind: "Документ", color: "#e0803c" },
      { kind: "Справочник", color: "#4c8ed9" },
    ],
  },
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
    vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      const payload = url.includes("name=%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%87%D0%BD%D0%B8%D0%BA.%D0%9A%D0%BE%D0%BD%D1%82%D1%80%D0%B0%D0%B3%D0%B5%D0%BD%D1%82%D1%8B")
        ? ready
        : base;
      return { ok: true, status: 200, json: async () => payload };
    }),
  );
});

it("открывается из меню без параметров и даёт выбрать объект", async () => {
  render(
    <MemoryRouter initialEntries={["/graph"]}>
      <QueryClientProvider client={client()}><GraphPage /><LocationProbe /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Связи объектов" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Конфигурация" })).toHaveValue("Отраслевая конфигурация А");
  expect(screen.getByText(/Введите полное имя объекта/)).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.getByLabelText("Текущий адрес")).toHaveTextContent("config=%D0%9E");
  });

  fireEvent.change(screen.getByRole("textbox", { name: "Полное имя объекта" }), {
    target: { value: "Справочник.Контрагенты" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Показать связи" }));

  expect(await screen.findByRole("img", { name: /Окрестность объекта Справочник.Контрагенты/ })).toBeInTheDocument();
  expect(screen.getAllByText("РеализацияТоваровУслуг").length).toBeGreaterThan(0);
  expect(screen.getByText("ссылается на выбранный объект")).toBeInTheDocument();
  expect(screen.getAllByText("Реквизит Контрагент").length).toBeGreaterThan(0);
  expect(screen.getByText("Показано 1 из 4")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Открыть карточку объекта" })).toHaveAttribute("href", expect.stringContaining("/object?"));
});

it("клик по соседу перестраивает тот же прямой адрес", async () => {
  render(
    <MemoryRouter initialEntries={["/graph?config=Отраслевая+конфигурация+А&name=Справочник.Контрагенты&limit=30"]}>
      <QueryClientProvider client={client()}><GraphPage /><LocationProbe /></QueryClientProvider>
    </MemoryRouter>,
  );

  const link = await screen.findByRole("link", { name: /Построить вокруг Документ.РеализацияТоваровУслуг/ });
  fireEvent.click(link);

  await waitFor(() => {
    expect(screen.getByLabelText("Текущий адрес")).toHaveTextContent("name=Документ.РеализацияТоваровУслуг");
  });
});

it("прямая ссылка сохраняет произвольный предел безопасного диапазона", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ ...base, limit: 10 }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/graph?limit=10"]}>
      <QueryClientProvider client={client()}><GraphPage /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("combobox", { name: "Предел соседей" })).toHaveValue("10");
});

it("не откатывает выбранную конфигурацию, пока грузится её граф", async () => {
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.searchParams.get("config") === "Отраслевая конфигурация Б") {
      return new Promise<Response>(() => undefined);
    }
    return { ok: true, status: 200, json: async () => base } as Response;
  });

  render(
    <MemoryRouter initialEntries={["/graph"]}>
      <QueryClientProvider client={client()}><GraphPage /><LocationProbe /></QueryClientProvider>
    </MemoryRouter>,
  );

  const configuration = await screen.findByRole("combobox", { name: "Конфигурация" });
  fireEvent.change(configuration, { target: { value: "Отраслевая конфигурация Б" } });

  await waitFor(() => {
    expect(screen.getByLabelText("Текущий адрес")).toHaveTextContent("%D0%91");
  });
  expect(configuration).toHaveValue("Отраслевая конфигурация Б");
});

it("колесо над схемой не доходит до прокрутки страницы", async () => {
  render(
    <MemoryRouter initialEntries={["/graph?config=Отраслевая+конфигурация+А&name=Справочник.Контрагенты"]}>
      <QueryClientProvider client={client()}><GraphPage /></QueryClientProvider>
    </MemoryRouter>,
  );

  const canvas = await screen.findByRole("img", { name: /Окрестность объекта/ });
  const pageWheel = vi.fn();
  document.addEventListener("wheel", pageWheel);
  const event = new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY: 120 });
  canvas.dispatchEvent(event);
  document.removeEventListener("wheel", pageWheel);

  expect(event.defaultPrevented).toBe(true);
  expect(pageWheel).not.toHaveBeenCalled();
});

it("узлы используют неподвижную зону наведения и отдельный контур", async () => {
  render(
    <MemoryRouter initialEntries={["/graph?config=Отраслевая+конфигурация+А&name=Справочник.Контрагенты"]}>
      <QueryClientProvider client={client()}><GraphPage /></QueryClientProvider>
    </MemoryRouter>,
  );

  const canvas = await screen.findByRole("img", { name: /Окрестность объекта/ });
  const subject = canvas.querySelector(".graph-node.is-subject");
  const link = subject?.closest("a");

  expect(link?.querySelector(".graph-node-hit")).toHaveAttribute("r", "25");
  expect(link?.querySelector(".graph-node-halo")).toHaveAttribute("r", "21");
});

it("неизвестный объект показывает предложения вместо пустого холста", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      ...base,
      name: "Справочник.Контрагент",
      state: "not_found",
      message: "В конфигурации нет объекта `Справочник.Контрагент`.",
      suggestions: [{ name: subject.name, graph_url: subject.graph_url }],
    }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/graph?name=Справочник.Контрагент"]}>
      <QueryClientProvider client={client()}><GraphPage /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("alert")).toHaveTextContent("нет объекта");
  expect(screen.getByRole("link", { name: subject.name })).toHaveAttribute("href", expect.stringContaining("/graph?"));
});

it("пустой Registry объясняет источник и не показывает форму графа", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      ...base,
      configuration_names: [],
      configuration: "",
      state: "empty_registry",
      message: "Не загружено ни одной конфигурации — граф строить не по чему.",
    }),
  } as Response);

  render(
    <MemoryRouter initialEntries={["/graph"]}>
      <QueryClientProvider client={client()}><GraphPage /></QueryClientProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText(/Не загружено ни одной конфигурации/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Перейти к источникам" })).toHaveAttribute("href", "/sources");
  expect(screen.queryByRole("button", { name: "Показать связи" })).not.toBeInTheDocument();
});
