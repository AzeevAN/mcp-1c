import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import rolesCss from "../styles/global.css?raw";
import { RolesPage } from "./RolesPage";

const role = {
  uuid: "11111111-1111-1111-1111-111111111111",
  name: "Reader",
  label_ru: "Чтение",
  synonyms: [
    { language: "en", content: "Reading" },
    { language: "ru", content: "Чтение" },
  ],
  comment: "Синтетическая роль",
  comment_truncated: false,
  xml_version: "2.20",
  default_flags: {
    set_for_new_objects: true,
    set_for_attributes_by_default: false,
    independent_rights_of_child_objects: false,
    resolver_effect: "evidence_only",
  },
};

const base = {
  api_version: "v1",
  state: "ready",
  configuration: "Отраслевая конфигурация",
  configuration_names: ["Отраслевая конфигурация"],
  generation: "generation-1",
  source_sha256: "a".repeat(64),
  declaration_scope: "declared_role_rights",
  disclaimer: "Показаны объявленные права ролей, а не эффективный доступ пользователя.",
};

const catalog = {
  ...base,
  operations: [
    {
      operation: "read",
      label_ru: "Чтение данных",
      channel: "programmatic",
      platform_right: "Read",
    },
    {
      operation: "view",
      label_ru: "Интерактивный просмотр",
      channel: "interactive",
      platform_right: "View",
    },
    {
      operation: "update",
      label_ru: "Изменение данных",
      channel: "programmatic",
      platform_right: "Update",
    },
  ],
  roles_total: 1,
  roles: [role],
  page: { offset: 0, limit: 50, returned: 1, next_cursor: null },
};

const access = {
  ...base,
  mode: "objects",
  role,
  target: null,
  objects_all_total: 2,
  objects_total: 2,
  object_filters: { kind: "", query: "" },
  object_facets: [
    { kind: "Catalog", kind_ru: "Справочник", count: 1 },
    { kind: "Document", kind_ru: "Документ", count: 1 },
  ],
  objects: [{
    target: "Catalog.Orders",
    full_name: "Справочник.Orders",
    kind: "Catalog",
    kind_ru: "Справочник",
    name: "Orders",
    root_rights: [{
      target: "Catalog.Orders",
      name: "Read",
      label_ru: "Чтение данных",
      channel: "programmatic",
      value: true,
      state: "conditional_true",
      has_rls: true,
      rls_detail_available: true,
      next_action: "Запросите условие отдельно через restriction_ref.",
      restrictions: [{
        fields: [
          "Catalog.Orders.Attribute.Code",
          "Catalog.Orders.Attribute.Number",
        ],
        chars: 6000,
        bytes: 6000,
        ref: "restriction-ref",
      }],
    }, {
      target: "Catalog.Orders",
      name: "View",
      label_ru: "Интерактивный просмотр",
      channel: "interactive",
      value: true,
      state: "unconditional_true",
      has_rls: false,
      rls_detail_available: false,
      restrictions: [],
    }],
    descendants: {
      targets_with_grants: 2,
      granted_rights: 2,
      conditional_rights: 1,
      detail_available: true,
    },
    has_rls: true,
    rls_detail_available: true,
    next_action: "Откройте restriction_ref или detail=children.",
  }],
  templates_total: 0,
  templates: [],
  page: { offset: 0, limit: 50, returned: 1, next_cursor: "objects-next" },
  templates_page: { offset: 0, limit: 20, returned: 0, next_cursor: null },
};

const secondAccess = {
  ...access,
  objects: [{
    ...access.objects[0],
    target: "Document.Invoice",
    full_name: "Документ.Invoice",
    kind: "Document",
    kind_ru: "Документ",
    name: "Invoice",
    has_rls: false,
    rls_detail_available: false,
    root_rights: [access.objects[0].root_rights[1]],
    descendants: {
      targets_with_grants: 0,
      granted_rights: 0,
      conditional_rights: 0,
      detail_available: false,
    },
  }],
  page: { offset: 50, limit: 50, returned: 1, next_cursor: null },
};

const childrenAccess = {
  ...base,
  mode: "children",
  role,
  object: {
    target: "Catalog.Orders",
    full_name: "Справочник.Orders",
    kind: "Catalog",
    kind_ru: "Справочник",
    name: "Orders",
  },
  rights_total: 1,
  rights: [{
    target: "Catalog.Orders.Attribute.Code",
    child_path: "Attribute.Code",
    child_kind: "Attribute",
    child_kind_ru: "Реквизит",
    child_name: "Code",
    name: "Read",
    label_ru: "Чтение данных",
    channel: "programmatic",
    value: true,
    state: "unconditional_true",
    has_rls: false,
    rls_detail_available: false,
    restrictions: [],
  }],
  page: { offset: 0, limit: 50, returned: 1, next_cursor: null },
};

const auditAccess = {
  ...childrenAccess,
  mode: "audit",
  rights_total: 1,
  rights: [{
    ...childrenAccess.rights[0],
    name: "Edit",
    label_ru: "Интерактивное редактирование",
    channel: "interactive",
    value: false,
    state: "explicit_false",
  }],
};

const found = {
  ...base,
  source_target: "Catalog.Orders",
  checked_rights: [
    { operation: "read", label_ru: "Чтение данных", channel: "programmatic", platform_right: "Read" },
    { operation: "update", label_ru: "Изменение данных", channel: "programmatic", platform_right: "Update" },
  ],
  include_conditional: false,
  conditional_candidates_excluded: 1,
  candidates_total: 2,
  candidates: [{
    role,
    complete: false,
    matched_operations: ["read"],
    missing_operations: ["update"],
    conditional_operations: [],
    denied_operations: ["update"],
    has_rls: false,
    rls_detail_available: false,
    matched_rights: [{
      target: "Catalog.Orders",
      name: "Read",
      label_ru: "Чтение данных",
      channel: "programmatic",
      value: true,
      state: "unconditional_true",
    }],
  }],
  minimal_role_set: {
    roles: ["Editor", "Reader"],
    proof: "explicit_unconditional",
  },
  warnings: ["Default-флаги не участвуют в подборе."],
  page: { offset: 0, limit: 10, returned: 1, next_cursor: "find-next" },
};

function response(payload: object, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="Текущий адрес">{location.pathname + location.search}</output>;
}

function renderPage(entry = "/roles") {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={client()}>
        <RolesPage />
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

it("открывается без config, выбирает роль и не получает RLS до явного клика", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/objects") return response(access);
    if (url.pathname === "/api/v1/roles/restriction") {
      return response({
        ...base,
        mode: "restriction",
        role: "Reader",
        restriction_ref: "restriction-ref",
        fields: [
          "Catalog.Orders.Attribute.Code",
          "Catalog.Orders.Attribute.Number",
        ],
        template: "",
        target: "Catalog.Orders",
        right: "Read",
        content: "SyntheticAllowed(x)",
        total_chars: 6000,
        total_bytes: 6000,
        page: { offset: 0, max_chars: 2000, returned_chars: 19, next_cursor: "rls-next" },
      });
    }
    return response(catalog);
  }));
  renderPage();

  expect(await screen.findByRole("heading", { name: "Роли и права" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Конфигурация" })).toHaveValue("Отраслевая конфигурация");
  expect(screen.getByText(/не эффективный доступ пользователя/i)).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "Роль → доступы" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "Объект → роли" })).toBeInTheDocument();
  expect(screen.queryByText("SyntheticAllowed(x)")).not.toBeInTheDocument();
  expect(screen.queryByText("Читаем страницу прав…")).not.toBeInTheDocument();
  expect(screen.queryByText("Resolver проверяет объявленные права…")).not.toBeInTheDocument();

  fireEvent.click(await screen.findByRole("button", { name: "Чтение" }));
  expect(await screen.findByRole("heading", { name: "Orders" })).toBeInTheDocument();
  expect(screen.getByText(/Роль выбрана\. Начните вводить, чтобы сменить/)).toBeInTheDocument();
  expect(screen.queryByText("Найдено: 1.")).not.toBeInTheDocument();
  expect(screen.getByText("Справочник")).toBeInTheDocument();
  expect(screen.getByText("С ограничением RLS")).toBeInTheDocument();
  expect(screen.getByText("Интерактивный просмотр")).toBeInTheDocument();
  expect(screen.getByText(/Default-флаги — свидетельство/)).toBeInTheDocument();
  expect(screen.queryByText("SyntheticAllowed(x)")).not.toBeInTheDocument();
  expect(screen.queryByText("Catalog.Orders.Attribute.Code")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Показать RLS" }));
  expect(await screen.findByText("SyntheticAllowed(x)")).toBeInTheDocument();
  expect(screen.getByText("Catalog.Orders.Attribute.Code")).toBeInTheDocument();
  expect(screen.getByText("Catalog.Orders.Attribute.Number")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Дочитать RLS" })).toBeInTheDocument();
});

it("показывает неизвестные default-флаги descriptor-only роли без false", async () => {
  const descriptorOnly = {
    ...role,
    name: "DescriptorOnly",
    default_flags: {
      ...role.default_flags,
      set_for_new_objects: null,
      set_for_attributes_by_default: null,
      independent_rights_of_child_objects: null,
    },
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/objects") {
      return response({
        ...access,
        role: descriptorOnly,
        objects: [],
        objects_total: 0,
      });
    }
    return response({ ...catalog, roles: [descriptorOnly] });
  }));
  renderPage("/roles?config=Отраслевая%20конфигурация&role=DescriptorOnly");

  await waitFor(() => expect(screen.getAllByText("не задано")).toHaveLength(3));
  expect(screen.queryByText("Новые объекты: false")).not.toBeInTheDocument();
});

it("объясняет пустое условие RLS и всё равно показывает его fields", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/objects") return response(access);
    if (url.pathname === "/api/v1/roles/restriction") {
      return response({
        ...base,
        mode: "restriction",
        role: "Reader",
        restriction_ref: "restriction-ref",
        fields: [
          "Catalog.Orders.Attribute.Code",
          "Catalog.Orders.Attribute.Number",
        ],
        template: "",
        target: "Catalog.Orders",
        right: "Read",
        content: "",
        total_chars: 0,
        total_bytes: 0,
        page: { offset: 0, max_chars: 2000, returned_chars: 0, next_cursor: null },
      });
    }
    return response(catalog);
  }));
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "Чтение" }));
  await screen.findByRole("heading", { name: "Orders" });
  fireEvent.click(screen.getByRole("button", { name: "Показать RLS" }));

  expect(await screen.findByText("Условие RLS пустое.")).toBeInTheDocument();
  expect(screen.getByText("Catalog.Orders.Attribute.Code")).toBeInTheDocument();
  expect(screen.getByText("Catalog.Orders.Attribute.Number")).toBeInTheDocument();
});

it("заменяет страницу объектов большой роли и не показывает false", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/objects") {
      return response(url.searchParams.get("cursor") ? secondAccess : access);
    }
    return response(catalog);
  }));
  renderPage("/roles?config=Отраслевая+конфигурация&role=Reader");

  expect(await screen.findByRole("heading", { name: "Orders" })).toBeInTheDocument();
  expect(screen.getByText("Страница 1 · показано 1 из 2 объектов")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Следующая страница объектов" }));

  expect(await screen.findByRole("heading", { name: "Invoice" })).toBeInTheDocument();
  expect(screen.getByText("Страница 2 · показано 1 из 2 объектов")).toBeInTheDocument();
  expect(screen.queryByText("Явный false")).not.toBeInTheDocument();
});

it("открывает дочерние права и audit вкладками одного модального окна", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/objects") return response(access);
    if (url.pathname === "/api/v1/roles/access") {
      if (url.searchParams.get("detail") === "children") return response(childrenAccess);
      if (url.searchParams.get("detail") === "audit") return response(auditAccess);
    }
    return response(catalog);
  }));
  renderPage("/roles?config=Отраслевая+конфигурация&role=Reader");

  await screen.findByRole("heading", { name: "Orders" });
  expect(screen.queryByText("Code")).not.toBeInTheDocument();
  expect(screen.queryByText("Явный false")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Показать детали Orders" }));
  const dialog = await screen.findByRole("dialog", { name: "Права объекта Orders" });
  expect(dialog).toBeInTheDocument();
  expect(await screen.findByText("Code")).toBeInTheDocument();
  expect(screen.getByText("Реквизит")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("tab", { name: "Технический аудит" }));
  expect(await screen.findByText("Явный false")).toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "Права объекта Orders" })).toBe(dialog);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("ищет роли на сервере без бесконечной кнопки подгрузки", async () => {
  const secondRole = {
    ...role,
    uuid: "22222222-2222-2222-2222-222222222222",
    name: "Editor",
    label_ru: "Редактирование",
    synonyms: [{ language: "ru", content: "Редактирование" }],
  };
  const firstPage = {
    ...catalog,
    roles_total: 2,
    roles_matched: 2,
    page: { offset: 0, limit: 20, returned: 1, next_cursor: "catalog-next" },
  };
  const searchPage = {
    ...catalog,
    roles_total: 2,
    roles_matched: 1,
    roles: [secondRole],
    page: { offset: 0, limit: 20, returned: 1, next_cursor: null },
  };
  const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/objects") return response(access);
    return response(url.searchParams.get("query") === "ред" ? searchPage : firstPage);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage();

  expect(await screen.findByRole("button", { name: "Чтение" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Загрузить ещё роли" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("combobox", { name: "Поиск роли" }), {
    target: { value: "ред" },
  });

  expect(await screen.findByRole("button", { name: "Редактирование" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("query=%D1%80%D0%B5%D0%B4"));
});

it("фильтрует объекты выбранной роли по типу и имени на сервере", async () => {
  const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/objects") {
      if (url.searchParams.get("kind") === "Document") {
        return response({
          ...secondAccess,
          objects_all_total: 2,
          objects_total: 1,
          object_filters: {
            kind: "Document",
            query: url.searchParams.get("query") || "",
          },
          object_facets: access.object_facets,
          page: { offset: 0, limit: 50, returned: 1, next_cursor: null },
        });
      }
      return response(access);
    }
    return response(catalog);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage("/roles?config=Отраслевая+конфигурация&role=Reader");

  expect(await screen.findByRole("button", { name: "Справочник · 1" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Документ · 1" }));
  expect(await screen.findByRole("heading", { name: "Invoice" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Orders" })).not.toBeInTheDocument();

  fireEvent.change(screen.getByRole("searchbox", { name: "Поиск объекта роли" }), {
    target: { value: "invoice" },
  });
  await waitFor(() => {
    expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/kind=Document.*query=invoice|query=invoice.*kind=Document/));
  });
});

it("не размонтирует поиск объекта между буквами серверного запроса", async () => {
  const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/objects") {
      if (url.searchParams.has("query")) {
        return new Promise<Response>(() => undefined);
      }
      return response(access);
    }
    return response(catalog);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderPage("/roles?config=Отраслевая+конфигурация&role=Reader");

  const input = await screen.findByRole("searchbox", { name: "Поиск объекта роли" });
  input.focus();
  fireEvent.change(input, { target: { value: "i" } });
  await waitFor(() => {
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("query=i"));
  });

  const retained = screen.getByRole("searchbox", { name: "Поиск объекта роли" });
  expect(retained).toBe(input);
  expect(retained).toHaveFocus();
  expect(retained).toHaveValue("i");
  expect(screen.getByText("Фильтруем объекты…")).toBeInTheDocument();
  fireEvent.change(retained, { target: { value: "in" } });
  await waitFor(() => {
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("query=in"));
  });
  expect(screen.getByRole("searchbox", { name: "Поиск объекта роли" })).toBe(retained);
  expect(retained).toHaveFocus();
  expect(retained).toHaveValue("in");
});

it("показывает серверный resolver объект → роли и не вычисляет минимум сам", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://dashboard.test");
    if (url.pathname === "/api/v1/roles/find") return response(found);
    return response(catalog);
  }));
  renderPage();

  fireEvent.click(await screen.findByRole("tab", { name: "Объект → роли" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Полное имя объекта" }), {
    target: { value: "Справочник.Orders" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "read → Read" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "update → Update" }));
  fireEvent.click(screen.getByRole("button", { name: "Найти роли" }));

  expect(await screen.findByText("Editor + Reader")).toBeInTheDocument();
  expect(screen.getByText("Доказательство: explicit_unconditional")).toBeInTheDocument();
  expect(screen.getByText("Не хватает: Изменение данных (update)")).toBeInTheDocument();
  expect(screen.getByText("Не предоставляет: Изменение данных (update)")).toBeInTheDocument();
  expect(screen.getByText("Условных кандидатов исключено: 1")).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.getByLabelText("Текущий адрес")).toHaveTextContent("mode=object");
  });
});

it.each([
  ["missing", "Для конфигурации нет готового снимка объявленных прав ролей."],
  ["error", "Снимок ролей повреждён."],
])("объясняет состояние %s", async (state, message) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
    ...base,
    state,
    configuration_names: ["Отраслевая конфигурация"],
    message,
  }, 409)));
  renderPage();

  expect(await screen.findByRole("alert")).toHaveTextContent(message);
  expect(screen.queryByRole("combobox", { name: "Поиск роли" })).not.toBeInTheDocument();
});

it("удерживает поиск и модальное окно в границах страницы", () => {
  expect(rolesCss).toMatch(
    /\.role-picker-card\s*,\s*\.role-picker-card > \*\s*,\s*\.role-picker-card input\s*\{[^}]*min-width:\s*0;/s,
  );
  expect(rolesCss).toMatch(
    /\.role-picker-card > \*\s*\{[^}]*width:\s*100%;/s,
  );
  expect(rolesCss).toMatch(
    /\.role-picker-card input\s*\{[^}]*width:\s*100%;[^}]*max-width:\s*100%;/s,
  );
  expect(rolesCss).toMatch(/\.role-detail-dialog\s*\{[^}]*max-height:\s*min\(86vh,\s*900px\);/s);
});
