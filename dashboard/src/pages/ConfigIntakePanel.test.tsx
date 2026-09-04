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

it.each([false, true])("публикует только после confirm и учитывает no_op=%s при обновлении кэша", async (noOp) => {
  const requests: Array<{ path: string; body: unknown }> = [];
  let previewReady = false;
  const readyJob = {
    job_id: "job-001",
    candidate_id: candidate.id,
    state: "done",
    stage: "done",
    error: "",
    preview,
    commit: null,
  };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    requests.push({ path, body });
    if (path === "/api/v1/sources/intake") {
      return response({
        ...snapshot(),
        jobs: previewReady ? [readyJob] : [],
      });
    }
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
      previewReady = true;
      return response({ job: readyJob });
    }
    if (path === "/api/v1/sources/intake/confirm") {
      return response({
        job: {
          job_id: "job-001",
          candidate_id: candidate.id,
          state: "done",
          stage: "done",
          error: "",
          preview: null,
          commit: {
            no_op: noOp,
            generation_id: "generation-001",
            manifest_sha256: "d".repeat(64),
            applied_layers: ["base_structure", "roles"],
          },
        },
      });
    }
    throw new Error(`Неожиданный запрос ${path}`);
  }));
  const client = renderPanel();
  const dependentKeys = [["card", "object", "Synthetic"], ["roles", "catalog", "Synthetic"], ["graph", "Synthetic"], ["queries-setup"]];
  dependentKeys.forEach((key) => client.setQueryData(key, "прежнее поколение"));

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

  expect(within(dialog).queryByRole("button", {
    name: "Сохранить preview и вернуться",
  })).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Закрыть" }));
  const reopen = await screen.findByRole("button", {
    name: "Открыть preview: СинтетическаяКонфигурация · configuration.zip",
  });
  fireEvent.click(reopen);
  const reopenedDialog = await screen.findByRole("dialog", { name: "Проверка изменений" });

  fireEvent.click(within(reopenedDialog).getByRole("button", { name: "Опубликовать изменения" }));
  expect(await screen.findByText(noOp ? "Изменений нет; активное поколение сохранено." : "Поколение опубликовано", { exact: false })).toBeInTheDocument();
  for (const key of dependentKeys) {
    expect(client.getQueryData(key)).toBe(noOp ? "прежнее поколение" : undefined);
  }
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
  expect(within(dialog).getByRole("button", { name: "Отменить и удалить preview" })).toBeDisabled();
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
      preview: null,
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

it("называет владельца preview и не запрашивает job после её удаления", async () => {
  const readyJob = {
    job_id: "job-ready-preview",
    candidate_id: candidate.id,
    state: "done",
    stage: "done",
    error: "",
    preview,
    commit: null,
  };
  const requests: Array<{ path: string; body: unknown }> = [];
  let discarded = false;
  let deletedJobRequests = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    requests.push({ path, body });
    if (path === "/api/v1/sources/intake") {
      return response({
        ...snapshot(),
        jobs: discarded ? [] : [readyJob],
      });
    }
    if (path === "/api/v1/sources/intake/jobs/job-ready-preview") {
      if (discarded) {
        deletedJobRequests += 1;
        return response({ error: "Job не найдена." }, 404);
      }
      return response({ job: readyJob });
    }
    if (path === "/api/v1/sources/intake/discard") {
      discarded = true;
      return response({ discarded: readyJob.job_id });
    }
    throw new Error(`Неожиданный запрос ${path}`);
  }));
  renderPanel();

  const open = await screen.findByRole("button", {
    name: "Открыть preview: СинтетическаяКонфигурация · configuration.zip",
  });
  fireEvent.click(open);
  const dialog = await screen.findByRole("dialog", { name: "Проверка изменений" });
  expect(within(dialog).getByText(/Preview уже сохранён/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", {
    name: "Отменить и удалить preview",
  }));

  expect(await screen.findByText("Preview отменён; его рабочие данные удалены.")).toBeInTheDocument();
  expect(requests).toContainEqual({
    path: "/api/v1/sources/intake/discard",
    body: { job_id: readyJob.job_id },
  });
  await waitFor(() => {
    expect(screen.queryByRole("button", { name: /Открыть preview:/ })).not.toBeInTheDocument();
  });
  expect(screen.queryByText("Job не найдена.")).not.toBeInTheDocument();
  expect(deletedJobRequests).toBe(0);
});
