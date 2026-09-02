import { useQuery } from "@tanstack/react-query";

export type IntakeAction = "create" | "update" | "update_full";
export type IntakeJobState = "accepted" | "probing" | "ready" | "parsing" | "done" | "failed";

export type IntakeCandidate = {
  id: string;
  transport: "browser" | "incoming" | "local-file" | "local-directory";
  source_kind: "configuration" | "extension";
  internal_name: string;
  configuration_version: string;
  layout: "unknown" | "flat" | "tree" | "mixed";
  origin_name: string;
  raw_sha256: string;
  requires_parent: boolean;
  actions: IntakeAction[];
};

export type IntakeLayerVersion = {
  state: "ready" | "error" | "unavailable";
  content_sha256: string;
  items_total: number;
  error: string;
};

export type IntakePreview = {
  action: IntakeAction;
  no_op: boolean;
  identity: {
    source_kind: "configuration" | "extension";
    configuration_name: string;
    extension_name: string;
    parent_configuration: string;
  };
  base_generation_id: string | null;
  candidate_generation_id: string;
  extension_impacts?: {
    total: number;
    items: Array<{
      extension: string;
      target: string;
      state: "resolved" | "target_missing";
    }>;
    truncated: boolean;
  };
  layers: Array<{
    kind: "base_structure" | "extended_structure" | "code" | "forms" | "roles";
    decision: "apply" | "preserve";
    reason: "none" | "added" | "content" | "state" | "reparse" | "provenance";
    current: IntakeLayerVersion | null;
    candidate: IntakeLayerVersion;
  }>;
};

export type IntakeCommit = {
  no_op: boolean;
  generation_id: string;
  manifest_sha256: string;
  applied_layers: string[];
};

export type IntakeJob = {
  job_id: string;
  candidate_id: string;
  state: IntakeJobState;
  stage: string;
  error: string;
  preview: IntakePreview | null;
  commit: IntakeCommit | null;
};

export type IntakeSnapshot = {
  api_version: "v1";
  configuration_names: string[];
  candidates: IntakeCandidate[];
  groups: Array<{
    source_kind: "configuration" | "extension";
    internal_name: string;
    candidate_ids: string[];
  }>;
  issues: Array<{ source_id: string; origin_name: string; message: string }>;
  jobs: IntakeJob[];
};

type ApiErrorPayload = { error?: string };

export class ConfigIntakeApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ConfigIntakeApiError";
  }
}

async function intakeRequest<T>(
  path: string,
  body?: Record<string, string>,
): Promise<T> {
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
    throw new ConfigIntakeApiError(
      payload.error || `Intake API ответил ${response.status}.`,
      response.status,
    );
  }
  return payload;
}

export function useConfigIntake() {
  return useQuery({
    queryKey: ["sources", "intake"],
    queryFn: () => intakeRequest<IntakeSnapshot>("/api/v1/sources/intake"),
  });
}

export function useIntakeJob(jobId: string) {
  return useQuery({
    queryKey: ["sources", "intake", "job", jobId],
    queryFn: () => intakeRequest<{ job: IntakeJob }>(
      `/api/v1/sources/intake/jobs/${encodeURIComponent(jobId)}`,
    ),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const state = query.state.data?.job.state;
      return state && state !== "done" && state !== "failed" ? 1_000 : false;
    },
  });
}

export function startConfigIntake(
  candidateId: string,
  action: IntakeAction,
  parentConfiguration = "",
): Promise<{ job: IntakeJob }> {
  const body: Record<string, string> = {
    candidate_id: candidateId,
    action,
  };
  if (parentConfiguration) body.parent_configuration = parentConfiguration;
  return intakeRequest("/api/v1/sources/intake/start", body);
}

export function confirmConfigIntake(jobId: string): Promise<{ job: IntakeJob }> {
  return intakeRequest("/api/v1/sources/intake/confirm", { job_id: jobId });
}

export function uploadConfigCandidate(
  file: File,
  onProgress: (percent: number) => void,
): Promise<{ candidate: IntakeCandidate }> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/v1/sources/intake/upload");
    request.responseType = "json";
    request.setRequestHeader("accept", "application/json");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.round((event.loaded * 100) / event.total));
      }
    });
    request.addEventListener("load", () => {
      const payload = (request.response || {}) as {
        candidate?: IntakeCandidate;
        error?: string;
      };
      if (request.status >= 200 && request.status < 300 && payload.candidate) {
        onProgress(100);
        resolve({ candidate: payload.candidate });
        return;
      }
      reject(new ConfigIntakeApiError(
        payload.error || `Загрузка завершилась ответом ${request.status}.`,
        request.status,
      ));
    });
    request.addEventListener("error", () => {
      reject(new ConfigIntakeApiError(
        "Соединение оборвалось до durable-приёма ZIP.",
        0,
      ));
    });
    const form = new FormData();
    form.append("file", file);
    request.send(form);
  });
}
