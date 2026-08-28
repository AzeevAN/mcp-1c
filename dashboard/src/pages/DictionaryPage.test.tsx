import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { DictionaryPage } from "./DictionaryPage";

const base = {
  api_version: "v1",
  permissions: { read: true, admin: true },
  configuration_names: ["Отраслевая конфигурация А", "Отраслевая конфигурация Б"],
  configuration: "Отраслевая конфигурация А",
  aliases: [
    {
      phrase: "файлы",
      targets: ["Справочник.Файлы"],
      source: "встроенный",
      scope: null,
      removable: false,
    },
    {
      phrase: "общая фраза",
      targets: ["Справочник.Контрагенты"],
      source: "локальный — все конфигурации",
      scope: "*",
      removable: true,
    },
    {
      phrase: "локальная фраза",
      targets: ["Справочник.Контрагенты", "Справочник.Партнеры"],
      source: "локальный — Отраслевая конфигурация А",
      scope: "Отраслевая конфигурация А",
      removable: true,
    },
  ],
  synonym_groups: [["возчик", "перевозчик", "экспедитор"]],
  stats: {
    local_synonym_groups: 1,
    builtin_synonym_groups: 31,
    builtin_aliases: 29,
    configurations_with_aliases: 2,
    local_aliases: 4,
  },
};

function client() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="Текущий адрес">{location.pathname + location.search}</output>;
}

function renderPage(path = "/dictionary") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client()}>
        <DictionaryPage />
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => init?.method === "POST" ? { changed: {} } : base,
    })),
  );
});

it("открывается из меню, выбирает конфигурацию и объясняет происхождение", async () => {
  renderPage();

  expect(await screen.findByRole("heading", { name: "Словарь поиска" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Конфигурация" })).toHaveValue("Отраслевая конфигурация А");
  expect(screen.getByRole("combobox", { name: "Область псевдонима" })).toHaveValue("Отраслевая конфигурация А");
  expect(screen.getByText("локальная фраза")).toBeInTheDocument();
  expect(screen.getByText("локальный — Отраслевая конфигурация А")).toBeInTheDocument();
  expect(screen.getByText("Справочник.Партнеры")).toBeInTheDocument();
  expect(screen.getByText("возчик")).toBeInTheDocument();
  expect(screen.getByText(/31 встроенная группа/)).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.getByLabelText("Текущий адрес")).toHaveTextContent("config=%D0%9E");
  });
});

it("переход из промаха поиска сохраняет конфигурацию и подставляет фразу", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ ...base, configuration: "Отраслевая конфигурация Б" }),
  } as Response);
  renderPage("/dictionary?config=Отраслевая+конфигурация+Б&phrase=кто+нам+возит");

  expect(await screen.findByRole("textbox", { name: "Фраза псевдонима" })).toHaveValue("кто нам возит");
  expect(screen.getByRole("combobox", { name: "Конфигурация" })).toHaveValue("Отраслевая конфигурация Б");

  fireEvent.change(screen.getByRole("combobox", { name: "Конфигурация" }), {
    target: { value: "Отраслевая конфигурация А" },
  });
  await waitFor(() => {
    const address = screen.getByLabelText("Текущий адрес");
    expect(address).toHaveTextContent("phrase=%D0%BA%D1%82%D0%BE");
    expect(address).toHaveTextContent("config=%D0%9E");
  });
});

it("читатель видит правила, но не получает элементы записи", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ ...base, permissions: { read: true, admin: false } }),
  } as Response);

  renderPage();

  expect(await screen.findByText("локальная фраза")).toBeInTheDocument();
  expect(screen.getByText(/Править словарь может только администратор/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Завести псевдоним" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Удалить псевдоним/ })).not.toBeInTheDocument();
});

it("администратор заводит псевдоним в выбранной области", async () => {
  renderPage("/dictionary?config=Отраслевая+конфигурация+А&phrase=кто+нам+возит");

  const phrase = await screen.findByRole("textbox", { name: "Фраза псевдонима" });
  fireEvent.change(screen.getByRole("textbox", { name: "Полные имена объектов" }), {
    target: { value: "Справочник.Контрагенты, Справочник.Партнеры" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Завести псевдоним" }));

  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(
      ([input, init]) => String(input) === "/api/v1/dictionary/aliases" && init?.method === "POST",
    );
    expect(call).toBeDefined();
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      phrase: "кто нам возит",
      targets: ["Справочник.Контрагенты", "Справочник.Партнеры"],
      config: "Отраслевая конфигурация А",
    });
  });
  expect(phrase).toHaveValue("");
  expect(screen.getByText(/Псевдоним сохранён/)).toBeInTheDocument();
});

it("удаляет только локальный псевдоним с точным scope", async () => {
  renderPage();

  await screen.findByText("локальная фраза");
  expect(screen.queryByRole("button", { name: "Удалить псевдоним файлы" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Удалить псевдоним локальная фраза" }));

  await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(
      ([input, init]) => String(input) === "/api/v1/dictionary/aliases/remove" && init?.method === "POST",
    );
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      phrase: "локальная фраза",
      scope: "Отраслевая конфигурация А",
    });
  });
});

it("заводит и снимает локальную группу синонимов по полному составу", async () => {
  renderPage();

  const words = await screen.findByRole("textbox", { name: "Слова одной группы" });
  fireEvent.change(words, { target: { value: "поставщик, продавец контрагент" } });
  fireEvent.click(screen.getByRole("button", { name: "Завести группу" }));
  await waitFor(() => {
    expect(vi.mocked(fetch).mock.calls.some(
      ([input, init]) => String(input) === "/api/v1/dictionary/synonyms" && init?.method === "POST",
    )).toBe(true);
  });
  const removeButton = screen.getByRole("button", { name: "Снять группу возчик, перевозчик, экспедитор" });
  await waitFor(() => expect(removeButton).toBeEnabled());
  fireEvent.click(removeButton);

  await waitFor(() => {
    const add = vi.mocked(fetch).mock.calls.find(
      ([input, init]) => String(input) === "/api/v1/dictionary/synonyms" && init?.method === "POST",
    );
    const remove = vi.mocked(fetch).mock.calls.find(
      ([input, init]) => String(input) === "/api/v1/dictionary/synonyms/remove" && init?.method === "POST",
    );
    expect(JSON.parse(String(add?.[1]?.body))).toEqual({
      words: ["поставщик", "продавец", "контрагент"],
    });
    expect(JSON.parse(String(remove?.[1]?.body))).toEqual({
      words: ["возчик", "перевозчик", "экспедитор"],
    });
  });
});

it("без конфигураций оставляет доступными встроенные и общие правила", async () => {
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      ...base,
      configuration_names: [],
      configuration: "",
      aliases: base.aliases.slice(0, 2),
    }),
  } as Response);

  renderPage();

  expect(await screen.findByText(/Конфигурации не загружены/)).toBeInTheDocument();
  expect(screen.getByText("файлы")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Область псевдонима" })).toHaveValue("");
  expect(screen.getByRole("option", { name: "Все конфигурации" })).toBeInTheDocument();
});
