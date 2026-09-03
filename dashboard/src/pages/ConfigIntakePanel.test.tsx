import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ConfigIntakePanel } from "./ConfigIntakePanel";

const candidate = {
  id: "candidate-001",
  transport: "browser",
  source_kind: "configuration",
  internal_name: "СинтетическаяКонфигурация",
  configuration_version: "2.0",
  layout: "tree",
  origin_name: "configuration.zip",
  raw_sha256: "a".repeat(64),
  requires_parent: false,
  actions: ["create"],
};

const preview = {
  action: "create",
  no_op: false,
  identity: {
    source_kind: "configuration",
    configuration_name: "СинтетическаяКонфигурация",
    extension_name: "",
    parent_configuration: "",
  },
  base_generation_id: null,
  candidate_generation_id: "generation-001",
  extension_impacts: {
    total: 1,
    items: [{
      extension: "СинтетическоеРасширение",
      target: "Справочник.Удалённый",
      state: "target_missing",
    }],
    truncated: false,
  },
  layers: [
    {
      kind: "base_structure",
      decision: "apply",
      reason: "added",
      current: null,
      candidate: {
        state: "ready",
        content_sha256: "b".repeat(64),
        items_total: 12,
        error: "",
      },
    },
    {
      kind: "roles",
      decision: "apply",
      reason: "added",
      current: null,
      candidate: {
        state: "ready",
        content_sha256: "c".repeat(64),
        items_total: 3,
        error: "",
      },
    },
  ],
};

function response(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}

function snapshot(candidates = [candidate]) {
  return {
    api_version: "v1",
    configuration_names: [],
    candidates,
    groups: candidates.length
      ? [{ source_kind: "configuration", internal_name: candidate.internal_name, candidate_ids: candidates.map((item) => item.id) }]
      : [],
    issues: [],
    jobs: [],
  };
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConfigIntakePanel />
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("показывает semantic preview и публикует только после отдельного confirm", async () => {
  const requests: Array<{ path: string; body: unknown }> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    requests.push({ path, body });
    if (path === "/api/v1/sources/intake") return response(snapshot());
    if (path === "/api/v1/sources/intake/start") {
      return response({
        job: {
          job_id: "job-001",
          candidate_id: candidate.id,
          state: "ready",
          stage: "ready",
          error: "",
          preview: null,
          commit: null,
        },
      }, 202);
    }
    if (path === "/api/v1/sources/intake/jobs/job-001") {
      return response({
        job: {
          job_id: "job-001",
          candidate_id: candidate.id,
          state: "done",
          stage: "done",
          error: "",
          preview,
          commit: null,
        },
      });
    }
    if (path === "/api/v1/sources/intake/confirm") {
      return response({
        job: {
          job_id: "job-001",
          candidate_id: candidate.id,
          state: "done",
          stage: "done",
          error: "",
          preview,
          commit: {
            no_op: false,
            generation_id: "generation-001",
            manifest_sha256: "d".repeat(64),
            applied_layers: ["base_structure", "roles"],
          },
        },
      });
    }
    throw new Error(`Неожиданный запрос ${path}`);
  }));
  renderPanel();

  expect(await screen.findByText("СинтетическаяКонфигурация")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Создать конфигурацию" }));

  const dialog = await screen.findByRole("dialog", { name: "Проверка изменений" });
  expect(within(dialog).getByText("Базовая структура")).toBeInTheDocument();
  expect(within(dialog).getByText("Роли")).toBeInTheDocument();
  expect(within(dialog).getByText("12 элементов")).toBeInTheDocument();
  expect(within(dialog).getByText("цель отсутствует в новой базе", { exact: false })).toBeInTheDocument();
  expect(requests).toContainEqual({
    path: "/api/v1/sources/intake/start",
    body: { candidate_id: "candidate-001", action: "create" },
  });
  expect(requests.some(({ path }) => path.endsWith("/confirm"))).toBe(false);

  fireEvent.click(within(dialog).getByRole("button", { name: "Опубликовать изменения" }));
  expect(await screen.findByText("Поколение опубликовано", { exact: false })).toBeInTheDocument();
  expect(requests).toContainEqual({
    path: "/api/v1/sources/intake/confirm",
    body: { job_id: "job-001" },
  });
});

it("после confirm блокирует закрытие окна и запуск второго intake", async () => {
  let finishConfirm: ((result: Response) => void) | undefined;
  const pendingConfirm = new Promise<Response>((resolve) => {
    finishConfirm = resolve;
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/v1/sources/intake") return response(snapshot());
    if (path === "/api/v1/sources/intake/start") {
      return response({
        job: {
          job_id: "job-locked",
          candidate_id: candidate.id,
          state: "ready",
          stage: "ready",
          error: "",
          preview: null,
          commit: null,
        },
      }, 202);
    }
    if (path === "/api/v1/sources/intake/jobs/job-locked") {
      return response({
        job: {
          job_id: "job-locked",
          candidate_id: candidate.id,
          state: "done",
          stage: "done",
          error: "",
          preview,
          commit: null,
        },
      });
    }
    if (path === "/api/v1/sources/intake/confirm") return pendingConfirm;
    throw new Error(`Неожиданный запрос ${path}`);
  }));
  renderPanel();

  expect(await screen.findByText("СинтетическаяКонфигурация")).toBeInTheDocument();
  const start = screen.getByRole("button", { name: "Создать конфигурацию" });
  fireEvent.click(start);
  const dialog = await screen.findByRole("dialog", { name: "Проверка изменений" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Опубликовать изменения" }));

  await waitFor(() => {
    expect(within(dialog).getByRole("button", { name: "Закрыть" })).toBeDisabled();
  });
  expect(within(dialog).getByRole("button", { name: "Вернуться без публикации" })).toBeDisabled();
  expect(start).toBeDisabled();
  expect(within(dialog).getByRole("status")).toHaveTextContent(
    "Публикация уже запущена",
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "Закрыть" }));
  expect(dialog).toBeInTheDocument();

  finishConfirm?.(response({
    job: {
      job_id: "job-locked",
      candidate_id: candidate.id,
      state: "done",
      stage: "done",
      error: "",
      preview,
      commit: {
        no_op: false,
        generation_id: "generation-001",
        manifest_sha256: "d".repeat(64),
        applied_layers: ["base_structure", "roles"],
      },
    },
  }));
  expect(await screen.findByText("Поколение опубликовано", { exact: false })).toBeInTheDocument();
});

it("принимает полный ZIP как durable candidate и обновляет список", async () => {
  let uploaded = false;
  const open = vi.fn();
  const send = vi.fn((body: Document | XMLHttpRequestBodyInit | null) => {
    expect(body).toBeInstanceOf(FormData);
    uploaded = true;
    queueMicrotask(() => listeners.load?.(new Event("load")));
  });
  const listeners: Record<string, EventListener | undefined> = {};
  class XMLHttpRequestMock {
    status = 201;
    response = { candidate };
    responseType = "";
    upload = { addEventListener: vi.fn() };
    open = open;
    send = send;
    setRequestHeader = vi.fn();
    addEventListener(type: string, listener: EventListener) {
      listeners[type] = listener;
    }
  }
  vi.stubGlobal("XMLHttpRequest", XMLHttpRequestMock);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) !== "/api/v1/sources/intake") {
      throw new Error(`Неожиданный запрос ${String(input)}`);
    }
    return response(snapshot(uploaded ? [candidate] : []));
  }));
  renderPanel();

  expect(await screen.findByText("Кандидатов пока нет")).toBeInTheDocument();
  const input = document.querySelector<HTMLInputElement>('input[accept=".zip"]');
  fireEvent.change(input!, {
    target: {
      files: [new File(["synthetic"], "configuration.zip", { type: "application/zip" })],
    },
  });
  fireEvent.click(screen.getByRole("button", { name: "Принять ZIP" }));

  expect(await screen.findByText("СинтетическаяКонфигурация")).toBeInTheDocument();
  expect(open).toHaveBeenCalledWith("POST", "/api/v1/sources/intake/upload");
});

it("для расширения требует выбрать родительскую конфигурацию", async () => {
  const extensionCandidate = {
    ...candidate,
    id: "candidate-extension",
    source_kind: "extension",
    internal_name: "СинтетическоеРасширение",
    origin_name: "extension.zip",
    requires_parent: true,
    actions: ["update_full"],
  };
  const requests: Array<{ path: string; body: unknown }> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    requests.push({ path, body });
    if (path === "/api/v1/sources/intake") {
      return response({
        api_version: "v1",
        configuration_names: ["ПерваяКонфигурация", "РодительскаяКонфигурация"],
        candidates: [extensionCandidate],
        groups: [{
          source_kind: "extension",
          internal_name: extensionCandidate.internal_name,
          candidate_ids: [extensionCandidate.id],
        }],
        issues: [],
        jobs: [],
      });
    }
    if (path === "/api/v1/sources/intake/start") {
      return response({
        job: {
          job_id: "job-extension",
          candidate_id: extensionCandidate.id,
          state: "ready",
          stage: "ready",
          error: "",
          preview: null,
          commit: null,
        },
      }, 202);
    }
    if (path === "/api/v1/sources/intake/jobs/job-extension") {
      return response({
        job: {
          job_id: "job-extension",
          candidate_id: extensionCandidate.id,
          state: "parsing",
          stage: "collecting",
          error: "",
          preview: null,
          commit: null,
        },
      });
    }
    throw new Error(`Неожиданный запрос ${path}`);
  }));
  renderPanel();

  expect(await screen.findByText("СинтетическоеРасширение")).toBeInTheDocument();
  const action = screen.getByRole("button", { name: "Обновить полностью" });
  expect(action).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "Родительская конфигурация" }), {
    target: { value: "РодительскаяКонфигурация" },
  });
  expect(action).toBeEnabled();
  fireEvent.click(action);

  expect(requests).toContainEqual({
    path: "/api/v1/sources/intake/start",
    body: {
      candidate_id: "candidate-extension",
      action: "update_full",
      parent_configuration: "РодительскаяКонфигурация",
    },
  });
});

it("для legacy-конфигурации объясняет обязательное первое полное обновление", async () => {
  const legacyCandidate = {
    ...candidate,
    id: "candidate-legacy",
    actions: ["update_full"],
  };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/v1/sources/intake") {
      return response({
        ...snapshot([legacyCandidate]),
        configuration_names: [legacyCandidate.internal_name],
      });
    }
    throw new Error(`Неожиданный запрос ${path}`);
  }));
  renderPanel();

  expect(await screen.findByText("СинтетическаяКонфигурация")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Обновить код, формы и роли" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Обновить полностью" })).toBeInTheDocument();
  expect(screen.getByText(/Сначала выполните полное обновление/)).toBeInTheDocument();
});
