import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export type AdminJob = {
  name: string;
  size: number;
  state: "принимается" | "разбирается" | "готово" | "ошибка";
  error: string;
};

export type IncomingExport = {
  name: string;
  size: number;
  state: string;
  detail: string;
  settling: boolean;
  can_parse: boolean;
  action: "parse" | "reparse";
};

export type ReferenceState = {
  state: string;
  ready: boolean;
  message: string;
  signature?: string | null;
  schema_version?: string | null;
  content_sha256?: string | null;
  file_sha256?: string | null;
  items?: number | null;
  index_cache?: string | null;
  key_id?: string | null;
  action?: "activate" | "remove" | null;
};

export type ReferenceCatalog = {
  domains: Array<{
    id: string;
    title: string;
    description: string;
    access_scope: "default" | "explicit" | "hidden";
    items: number;
  }>;
  kinds: Array<{
    id: string;
    domain: string;
    title: string;
    access_scope: "default" | "explicit" | "hidden";
    items: number;
  }>;
  platform_versions: string[];
};

export type ReferenceAdminState = {
  api_version: "v1";
  active: ReferenceState;
  catalog: ReferenceCatalog | null;
  pending: ReferenceState | null;
  managed_upload: boolean;
  managed_file_present: boolean;
  limits: { upload_bytes: number };
};

export type AdminSourcesResponse = {
  api_version: "v1";
  limits: { upload_bytes: number };
  configuration_names: string[];
  jobs: AdminJob[];
  incoming: IncomingExport[];
  incoming_exists: boolean;
  incoming_dir: string;
  orphans: Array<{ path: string; size: number }>;
  snapshot_error: string;
  reference?: ReferenceAdminState;
  runtime?: { self_restart: boolean };
};

type ApiErrorPayload = { error?: string };

export class SourceAdminApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "SourceAdminApiError";
    this.status = status;
  }
}

async function adminRequest<T>(path: string, body?: Record<string, string>): Promise<T> {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body
      ? { accept: "application/json", "content-type": "application/json" }
      : { accept: "application/json" },
    credentials: "same-origin",
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = (await response.json().catch(() => ({}))) as T & ApiErrorPayload;
  if (!response.ok) {
    throw new SourceAdminApiError(
      payload.error || `Административный API ответил ${response.status}.`,
      response.status,
    );
  }
  return payload;
}

export function useAdminSources(enabled: boolean) {
  return useQuery({
    queryKey: ["sources", "admin"],
    queryFn: () => adminRequest<AdminSourcesResponse>("/api/v1/sources/admin"),
    enabled,
    refetchInterval: (query) => {
      const data = query.state.data;
      const runningJob = data?.jobs?.some(
        (job) => job.state === "принимается" || job.state === "разбирается",
      );
      const runningIncoming = data?.incoming?.some((item) => item.state === "разбирается");
      return runningJob || runningIncoming ? 2_000 : false;
    },
  });
}

export function uploadSource(
  file: File,
  allowTruncated: boolean,
  onProgress: (percent: number) => void,
): Promise<{ job: AdminJob }> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/v1/sources/upload");
    request.responseType = "json";
    request.setRequestHeader("accept", "application/json");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.round((event.loaded * 100) / event.total));
      }
    });
    request.addEventListener("load", () => {
      const payload = (request.response || {}) as { job?: AdminJob; error?: string };
      if (request.status >= 200 && request.status < 300 && payload.job) {
        onProgress(100);
        resolve({ job: payload.job });
      } else {
        reject(
          new SourceAdminApiError(
            payload.error || `Загрузка завершилась ответом ${request.status}.`,
            request.status,
          ),
        );
      }
    });
    request.addEventListener("error", () => {
      reject(new SourceAdminApiError("Соединение оборвалось во время загрузки.", 0));
    });
    const form = new FormData();
    form.append("file", file);
    if (allowTruncated) form.append("allow_truncated", "1");
    request.send(form);
  });
}

export function uploadReference(
  file: File,
  onProgress: (percent: number) => void,
): Promise<{ reference: ReferenceAdminState; pending: ReferenceState }> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/v1/reference/upload");
    request.responseType = "json";
    request.setRequestHeader("accept", "application/json");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.round((event.loaded * 100) / event.total));
      }
    });
    request.addEventListener("load", () => {
      const payload = (request.response || {}) as {
        reference?: ReferenceAdminState;
        pending?: ReferenceState;
        error?: string;
      };
      if (
        request.status >= 200
        && request.status < 300
        && payload.reference
        && payload.pending
      ) {
        onProgress(100);
        resolve({ reference: payload.reference, pending: payload.pending });
      } else {
        reject(
          new SourceAdminApiError(
            payload.error || `Загрузка завершилась ответом ${request.status}.`,
            request.status,
          ),
        );
      }
    });
    request.addEventListener("error", () => {
      reject(new SourceAdminApiError("Соединение оборвалось во время загрузки.", 0));
    });
    const form = new FormData();
    form.append("file", file);
    request.send(form);
  });
}

export function useRemoveReference() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (confirmation: string) => adminRequest<{
      removed: string;
      reference: ReferenceAdminState;
      pending: ReferenceState | null;
    }>("/api/v1/reference/remove", { confirmation }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["sources", "admin"] });
    },
  });
}

export async function requestServerRestart(): Promise<{
  state: "restarting";
  runtime_id: string;
}> {
  return adminRequest("/api/v1/server/restart", {});
}

export async function waitForServerRestart(
  previousRuntimeId: string,
  options: {
    timeoutMs?: number;
    intervalMs?: number;
    request?: typeof fetch;
  } = {},
): Promise<string> {
  const timeoutMs = options.timeoutMs ?? 5 * 60_000;
  const intervalMs = options.intervalMs ?? 500;
  const request = options.request ?? fetch;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await request("/health", {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (response.ok) {
        const payload = (await response.json()) as {
          status?: string;
          runtime_id?: string;
        };
        if (
          payload.status === "ok"
          && payload.runtime_id
          && payload.runtime_id !== previousRuntimeId
        ) {
          return payload.runtime_id;
        }
      }
    } catch {
      // Ожидаемое окно недоступности между остановкой и новым процессом.
    }
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
  }
  throw new SourceAdminApiError(
    "Сервер не подтвердил новый запуск. Проверьте состояние контейнера.",
    0,
  );
}

function useRefreshingMutation<TVariables, TResult>(
  mutationFn: (variables: TVariables) => Promise<TResult>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["sources"] }),
        queryClient.invalidateQueries({ queryKey: ["dashboard", "bootstrap"] }),
      ]);
    },
  });
}

export function useParseIncoming() {
  return useRefreshingMutation(
    ({ name, configuration }: { name: string; configuration: string }) =>
      adminRequest<{ job: AdminJob }>("/api/v1/sources/incoming/parse", {
        name,
        configuration,
      }),
  );
}

export function useClearJobs() {
  return useRefreshingMutation(() =>
    adminRequest<{ cleared: number }>("/api/v1/sources/jobs/clear", {}),
  );
}

export function useRemoveSource() {
  return useRefreshingMutation((id: string) =>
    adminRequest<{ removed: string }>("/api/v1/sources/remove", {
      id,
      confirmation: id,
    }),
  );
}

export function useForgetSource() {
  return useRefreshingMutation((path: string) =>
    adminRequest<{ forgotten: string }>("/api/v1/sources/forget", {
      path,
      confirmation: path,
    }),
  );
}
